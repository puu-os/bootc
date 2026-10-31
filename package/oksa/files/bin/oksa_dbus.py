# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

import ctypes
from dataclasses import dataclass, field
import fcntl
import logging
import multiprocessing
import os
from pathlib import Path
import pwd
import re
import signal
import stat
import tempfile
import time
import uuid

from gi.repository import Gio, GLib

from oksa_control import Contract, OksaControl, OksaError
import oksa_model_serving as ms
from oksa_monitoring import StatusSampler


@dataclass
class Job:
    path: str
    operation: str
    started_at: float
    state: str = "running"
    progress: str = ""
    ended_at: float | None = None
    result: dict | None = None
    error: dict | None = None
    repo_id: str | None = None
    model_name: str | None = None
    subscribers: set[str] = field(default_factory=set)

    def snapshot(self) -> dict:
        return {"ok": True, "path": self.path, "operation": self.operation, "state": self.state,
                "progress": self.progress, "started_at": self.started_at, "ended_at": self.ended_at,
                "result": self.result, "error": self.error, "repo_id": self.repo_id, "model_name": self.model_name}


class JobStore:
    LIMIT = 64

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError("Job directory must be private and daemon-owned")
        self.lock = (directory / "daemon.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.lock.close()
            raise
        self.jobs: dict[str, Job] = {}
        try:
            self.load()
        except BaseException:
            self.lock.close()
            raise

    def load(self) -> None:
        files = sorted(self.directory.glob("*.json"), key=lambda path: path.name)
        for path in files:
            if not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > Contract.MAX_JSON:
                raise PermissionError(f"Unsafe job record: {path}")
            data = Contract.decode(path.read_text())
            job = Job(**{key: data[key] for key in ("path", "operation", "started_at", "state", "progress",
                                                   "ended_at", "result", "error", "repo_id", "model_name")})
            Contract.validate("GetJob", (job.path,))
            if job.path.rsplit("/", 1)[1] != path.stem or job.state not in ("running", "succeeded", "failed"):
                raise ValueError(f"Invalid job record: {path}")
            if job.state == "running":
                job.state = "failed"
                job.ended_at = time.time()
                job.error = {"code": "Interrupted", "message": "Daemon restarted before completion; verify operation state before retrying"}
                self.save(job)
            self.jobs[job.path] = job
        self.prune()

    def save(self, job: Job) -> None:
        text = Contract.encode(job.snapshot())
        fd, name = tempfile.mkstemp(prefix=".job-", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.directory / (job.path.rsplit("/", 1)[1] + ".json"))
            directory_fd = os.open(self.directory, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(name).unlink(missing_ok=True)

    def prune(self, reserve: int = 0) -> list[str]:
        removed = []
        for job in sorted(self.jobs.values(), key=lambda item: item.started_at):
            if len(self.jobs) + reserve <= self.LIMIT:
                break
            if job.state != "running":
                (self.directory / (job.path.rsplit("/", 1)[1] + ".json")).unlink()
                del self.jobs[job.path]
                removed.append(job.path)
        return removed

    def close(self) -> None:
        self.lock.close()


class Worker:
    def __init__(self, channel) -> None:
        self.channel = channel
        self.last_progress = 0.0
        self.pending = ""

    def send_progress(self, text: str) -> None:
        self.last_progress = time.monotonic()
        self.pending = ""
        self.channel.send_bytes(Contract.encode({"kind": "progress", "text": text[:4096]}).encode())

    def progress(self, text: str) -> None:
        if time.monotonic() - self.last_progress < 0.2:
            self.pending = text
            return
        self.send_progress(text)

    @classmethod
    def run(cls, channel, service_class, method: str, args: tuple) -> None:
        os.setsid()
        worker = cls(channel)
        try:
            channel.send_bytes(b'{"kind":"ready"}')
            result = service_class().execute(method, args, worker.progress)
            encoded = Contract.encode(result)
            if len(encoded) > Contract.MAX_JSON - 65536:
                raise OksaError("InvalidResult", "Result leaves insufficient space for a job snapshot")
            packet = {"kind": "result", "result": result}
        except Exception as error:
            code = error.code if isinstance(error, OksaError) else "InvalidInput" if isinstance(error, ValueError) else "OperationFailed"
            packet = {"kind": "error", "error": {"code": code, "message": str(error)[:4096]}}
        try:
            if worker.pending:
                worker.send_progress(worker.pending)
            channel.send_bytes(Contract.encode(packet).encode())
        finally:
            channel.close()


@dataclass
class Invocation:
    process: object
    channel: object
    started: float
    invocation: object = None
    operation: str = ""
    job: Job | None = None
    ready: bool = False


class OksaDBusService:
    READ_LIMIT = 2
    READ_TIMEOUT = 90
    JOB_TIMEOUT = 6 * 60 * 60
    JOB_XML = '''<node><interface name="org.puu.Oksa.Job">
      <method name="GetSnapshot"><arg name="snapshot" type="s" direction="out"/></method>
      <signal name="Progress"><arg name="progress" type="s"/></signal>
      <signal name="StateChanged"><arg name="state" type="s"/></signal>
    </interface></node>'''

    @classmethod
    def introspection(cls) -> str:
        methods = []
        for name, (args, output, _) in Contract.METHODS.items():
            fields = ''.join(f'<arg name="{field}" type="{kind}" direction="in"/>' for field, kind in args)
            methods.append(f'<method name="{name}">{fields}<arg name="result" type="{output}" direction="out"/></method>')
        return f'<node><interface name="{Contract.BUS}">{"".join(methods)}</interface></node>'

    def __init__(self, connection=None, job_dir: Path | None = None, service_class=OksaControl) -> None:
        self.connection = connection if connection is not None else Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.service_class = service_class
        self.loop = GLib.MainLoop()
        self.workers: list[Invocation] = []
        self.registrations: dict[str, int] = {}
        self.closed = False
        try:
            self.frontend_uid = pwd.getpwnam("oksa").pw_uid
        except KeyError:
            self.frontend_uid = None
        self.store = JobStore(Path(job_dir or os.environ.get("OKSA_JOB_DIR", "/var/lib/oksa/jobs")))
        self.monitoring = StatusSampler(ms.collect_serving_status)
        self.context = multiprocessing.get_context("spawn")
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            self.store.close()
            raise OSError(ctypes.get_errno(), "Unable to become child subreaper")
        self.job_info = Gio.DBusNodeInfo.new_for_xml(self.JOB_XML).interfaces[0]
        self.root_info = Gio.DBusNodeInfo.new_for_xml(self.introspection()).interfaces[0]
        try:
            self.register(Contract.PATH, self.root_info)
            for path in self.store.jobs:
                self.register(path, self.job_info)
            reply = self.connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                              "RequestName", GLib.Variant("(su)", (Contract.BUS, 4)),
                                              GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 5000, None)
            if reply.unpack()[0] != 1:
                raise OksaError("Busy", "Another Oksa daemon owns the bus name")
        except BaseException:
            self.close()
            raise
        self.connection.connect("closed", self.connection_closed)
        self.monitoring.start()
        self.timer = GLib.timeout_add(50, self.poll)

    def register(self, path: str, info) -> None:
        self.registrations[path] = self.connection.register_object(path, info, self.handle_call, None, None)

    def authorize(self, sender: str) -> None:
        reply = self.connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                          "GetConnectionUnixUser", GLib.Variant("(s)", (sender,)),
                                          GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 5000, None)
        uid = reply.unpack()[0]
        if uid != 0 and (self.frontend_uid is None or uid != self.frontend_uid):
            raise OksaError("AccessDenied", "Only root or the dedicated oksa frontend may call this service")

    @staticmethod
    def return_error(invocation, error: OksaError) -> None:
        invocation.return_dbus_error(Contract.BUS + ".Error." + error.code,
                                     Contract.encode({"ok": False, "code": error.code, "error": str(error)[:4096]}))

    def handle_call(self, connection, sender, path, interface, method, parameters, invocation) -> None:
        try:
            self.authorize(sender)
            if self.closed:
                raise OksaError("Unavailable", "Daemon is shutting down")
            args = parameters.unpack()
            if interface == Contract.JOB and method == "GetSnapshot":
                self.return_snapshot(path, sender, invocation)
                return
            if interface != Contract.BUS or path != Contract.PATH:
                raise OksaError("UnknownMethod", "Unknown domain method")
            Contract.validate(method, args)
            if method == "GetJob":
                self.return_snapshot(args[0], sender, invocation)
            elif method == "GetStatus":
                invocation.return_value(GLib.Variant("(s)", (Contract.encode(self.monitoring.snapshot().to_json()),)))
            elif method == "ListJobs":
                snapshots = []
                for job in self.store.jobs.values():
                    snapshot = job.snapshot()
                    snapshot["result"] = None
                    snapshot["progress"] = job.progress[:512]
                    if job.error:
                        snapshot["error"] = {"code": job.error["code"], "message": job.error["message"][:512]}
                    snapshots.append(snapshot)
                invocation.return_value(GLib.Variant("(s)", (Contract.encode({"ok": True, "jobs": snapshots}),)))
            else:
                mutation = Contract.METHODS[method][2]
                exclusive = Contract.MUTATIONS | {"ListModels"}
                if method in exclusive and any(worker.operation in exclusive for worker in self.workers):
                    raise OksaError("Busy", "A conflicting operation is already running")
                if not mutation and sum(worker.job is None for worker in self.workers) >= self.READ_LIMIT:
                    raise OksaError("Busy", "All read workers are busy; retry later")
                self.launch(method, args, sender, invocation, mutation)
        except OksaError as error:
            self.return_error(invocation, error)
        except GLib.Error as error:
            self.return_error(invocation, OksaError("AccessDenied", f"Unable to verify sender credentials: {error.message}"))
        except Exception as error:
            logging.exception("D-Bus request failed")
            self.return_error(invocation, OksaError("OperationFailed", str(error)))

    def return_snapshot(self, path: str, sender: str, invocation) -> None:
        job = self.store.jobs.get(path)
        if job is None:
            raise OksaError("NotFound", "Unknown or expired job")
        if len(job.subscribers) < 128:
            job.subscribers.add(sender)
        invocation.return_value(GLib.Variant("(s)", (Contract.encode(job.snapshot()),)))

    def launch(self, method: str, args: tuple, sender: str, invocation, mutation: bool) -> None:
        job = None
        if mutation:
            for path in self.store.prune(reserve=1):
                self.connection.unregister_object(self.registrations.pop(path))
            job = Job(Contract.PATH + "/jobs/" + uuid.uuid4().hex, method, time.time(), subscribers={sender})
            if method == "DownloadModel":
                job.repo_id, job.model_name = args[:2]
            self.store.save(job)
            self.store.jobs[job.path] = job
            self.register(job.path, self.job_info)
        parent, child = self.context.Pipe(duplex=False)
        process = self.context.Process(target=Worker.run, args=(child, self.service_class, method, args))
        try:
            process.start()
        except Exception as error:
            parent.close()
            if job:
                job.state = "failed"
                job.ended_at = time.time()
                job.error = {"code": "Unavailable", "message": str(error)[:4096]}
                self.store.save(job)
            raise OksaError("Unavailable", "Could not start operation worker") from error
        finally:
            child.close()
        worker = Invocation(process, parent, time.monotonic(), None if mutation else invocation, method, job)
        if method in Contract.MUTATIONS:
            self.monitoring.invalidate()
        self.workers.append(worker)
        if mutation:
            invocation.return_value(GLib.Variant("(o)", (job.path,)))

    def emit(self, job: Job, name: str, text: str) -> None:
        for sender in tuple(job.subscribers):
            try:
                self.connection.emit_signal(sender, job.path, Contract.JOB, name, GLib.Variant("(s)", (text,)))
            except GLib.Error:
                job.subscribers.discard(sender)
                logging.warning("Unable to deliver job signal to %s", sender)

    def finish(self, worker: Invocation, result: dict | None, error: dict | None) -> None:
        if result is not None and result.get("ok") is False and error is None:
            error = {"code": "OperationFailed", "message": str(result.get("error", "Operation failed"))[:4096]}
        if worker.job:
            job = worker.job
            job.result, job.error = result, error
            job.state = "failed" if error else "succeeded"
            job.ended_at = time.time()
            self.store.save(job)
            self.emit(job, "StateChanged", job.state)
        elif error:
            self.return_error(worker.invocation, OksaError(error["code"], error["message"]))
        else:
            worker.invocation.return_value(GLib.Variant("(s)", (Contract.encode(result),)))
        self.dispose(worker)
        if worker.operation in Contract.MUTATIONS:
            self.monitoring.invalidate()

    def dispose(self, worker: Invocation) -> None:
        process = worker.process
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.is_alive():
            process.kill()
        process.join()
        while True:
            try:
                os.waitpid(-process.pid, 0)
            except ChildProcessError:
                break
        worker.channel.close()
        process.close()
        self.workers.remove(worker)

    def poll(self) -> bool:
        try:
            for worker in tuple(self.workers):
                terminal = False
                eof = False
                for _ in range(8):
                    if not worker.channel.poll():
                        break
                    try:
                        packet = Contract.decode(worker.channel.recv_bytes(Contract.MAX_JSON).decode())
                    except EOFError:
                        eof = True
                        break
                    kind = packet["kind"]
                    if kind == "ready":
                        worker.ready = True
                    elif kind == "progress" and worker.job:
                        worker.job.progress = packet["text"]
                        self.store.save(worker.job)
                        self.emit(worker.job, "Progress", worker.job.progress)
                    elif kind in ("result", "error"):
                        self.finish(worker, packet.get("result"), packet.get("error"))
                        terminal = True
                        break
                if terminal:
                    continue
                timeout = self.JOB_TIMEOUT if worker.job else self.READ_TIMEOUT
                if time.monotonic() - worker.started > timeout:
                    self.finish(worker, None, {"code": "Timeout", "message": "Operation exceeded its time limit; verify state before retrying"})
                elif not worker.process.is_alive() and (eof or not worker.channel.poll()):
                    self.finish(worker, None, {"code": "WorkerFailed", "message": "Worker exited without a result"})
        except Exception:
            logging.exception("Job processing failed; shutting down to avoid losing operation errors")
            self.loop.quit()
            return False
        return not self.closed

    def connection_closed(self, connection, vanished, error) -> None:
        self.loop.quit()

    def run(self) -> None:
        try:
            self.loop.run()
        finally:
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.loop.quit()
        if hasattr(self, "timer"):
            GLib.source_remove(self.timer)
        self.monitoring.close()
        for worker in tuple(self.workers):
            try:
                self.finish(worker, None, {"code": "Interrupted", "message": "Daemon shut down before completion; verify state before retrying"})
            except Exception:
                logging.exception("Unable to persist interrupted operation")
                if worker in self.workers:
                    self.dispose(worker)
        for registration in self.registrations.values():
            self.connection.unregister_object(registration)
        self.registrations.clear()
        if not self.connection.is_closed():
            try:
                self.connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ReleaseName",
                                          GLib.Variant("(s)", (Contract.BUS,)), None, Gio.DBusCallFlags.NONE, 5000, None)
            except GLib.Error:
                logging.exception("Unable to release Oksa bus name")
        self.store.close()


class OksaDaemon:
    @classmethod
    def main(cls) -> None:
        logging.basicConfig(level=logging.INFO)
        service = OksaDBusService()
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, cls.stop, service)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, cls.stop, service)
        service.run()

    @staticmethod
    def stop(service: OksaDBusService) -> bool:
        service.loop.quit()
        return GLib.SOURCE_REMOVE
