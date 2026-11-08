# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

import ctypes
from dataclasses import dataclass, field
import fcntl
from fnmatch import fnmatchcase
import hashlib
import json
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

    def properties(self) -> dict[str, GLib.Variant]:
        return {
            "Path": GLib.Variant("o", self.path),
            "Operation": GLib.Variant("s", self.operation),
            "StartedAt": GLib.Variant("d", float(self.started_at)),
            "EndedAt": GLib.Variant("ad", [] if self.ended_at is None else [float(self.ended_at)]),
            "State": GLib.Variant("s", self.state),
            "Progress": GLib.Variant("s", self.progress),
            "Result": GLib.Variant("a{sv}", {} if self.result is None else Contract.response_to_variant(self.operation, self.result)),
            "Error": GLib.Variant("a{sv}", {} if self.error is None else Contract.ERROR.to_variant(self.error)),
            "RepoId": GLib.Variant("as", [] if self.repo_id is None else [self.repo_id]),
            "ModelName": GLib.Variant("as", [] if self.model_name is None else [self.model_name]),
        }


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
            Contract.validate_snapshot(data)
            job = Job(**{key: data[key] for key in ("path", "operation", "started_at", "state", "progress",
                                                   "ended_at", "result", "error", "repo_id", "model_name")})
            Contract.validate_job_path(job.path)
            if job.path.rsplit("/", 1)[1] != path.stem or job.state not in ("running", "succeeded", "failed", "canceled"):
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
            Contract.validate_response(method, result)
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
    READ_LIMIT = 8
    READ_TIMEOUT = 90
    JOB_TIMEOUT = 6 * 60 * 60
    MODEL_REVISION_INTERVAL = 2.5
    JOB_XML = f'''<node><interface name="{Contract.JOB}">
      <method name="GetSnapshot"><arg name="snapshot" type="a{{sv}}" direction="out"/></method>
      <method name="Cancel"/>
      <signal name="Progress"><arg name="progress" type="s"/></signal>
      <signal name="StateChanged"><arg name="state" type="s"/></signal>
      <property name="Path" type="o" access="read"><annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="const"/></property>
      <property name="Operation" type="s" access="read"><annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="const"/></property>
      <property name="StartedAt" type="d" access="read"><annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="const"/></property>
      <property name="EndedAt" type="ad" access="read"><annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="true"/></property>
      <property name="State" type="s" access="read"><annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="true"/></property>
      <property name="Progress" type="s" access="read"><annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="true"/></property>
      <property name="Result" type="a{{sv}}" access="read"><annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="true"/></property>
      <property name="Error" type="a{{sv}}" access="read"><annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="true"/></property>
      <property name="RepoId" type="as" access="read"><annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="const"/></property>
      <property name="ModelName" type="as" access="read"><annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="const"/></property>
    </interface></node>'''

    @classmethod
    def introspection(cls) -> str:
        methods = []
        for name, (args, output, _) in Contract.METHODS.items():
            fields = ''.join(f'<arg name="{field}" type="{kind}" direction="in"/>' for field, kind in args)
            methods.append(f'<method name="{name}">{fields}<arg name="result" type="{output}" direction="out"/></method>')
        return f'''<node>
          <interface name="{Contract.BUS}">
            {"".join(methods)}
            <property name="Status" type="a{{sv}}" access="read"><annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="true"/></property>
            <property name="ActiveModel" type="s" access="read"><annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="true"/></property>
            <property name="ServingState" type="s" access="read"><annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="true"/></property>
            <property name="ModelsRevision" type="s" access="read"><annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="true"/></property>
            <property name="Jobs" type="ao" access="read"><annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="true"/></property>
          </interface>
          <interface name="org.freedesktop.DBus.ObjectManager">
            <method name="GetManagedObjects">
              <arg name="objects" type="a{{oa{{sa{{sv}}}}}}" direction="out"/>
            </method>
            <signal name="InterfacesAdded">
              <arg name="object_path" type="o"/>
              <arg name="interfaces_and_properties" type="a{{sa{{sv}}}}"/>
            </signal>
            <signal name="InterfacesRemoved">
              <arg name="object_path" type="o"/>
              <arg name="interfaces" type="as"/>
            </signal>
          </interface>
        </node>'''

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
        self._last_active = None
        self._last_serving = None
        self._last_status = None
        self._last_models_revision = None
        self._models_revision = None
        self._models_revision_at = 0.0
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            self.store.close()
            raise OSError(ctypes.get_errno(), "Unable to become child subreaper")
        node_info = Gio.DBusNodeInfo.new_for_xml(self.introspection())
        self.root_info = node_info.lookup_interface(Contract.BUS)
        self.om_info = node_info.lookup_interface("org.freedesktop.DBus.ObjectManager")
        self.job_info = Gio.DBusNodeInfo.new_for_xml(self.JOB_XML).lookup_interface(Contract.JOB)
        try:
            self.registrations[Contract.PATH] = self.connection.register_object(
                Contract.PATH, self.root_info, self.handle_call, self.handle_root_property, None
            )
            self.registrations[Contract.PATH + ":om"] = self.connection.register_object(
                Contract.PATH, self.om_info, self.handle_om_call, None, None
            )
            for path in self.store.jobs:
                self.register_job(self.store.jobs[path])
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

    def register_job(self, job: Job) -> None:
        self.registrations[job.path] = self.connection.register_object(
            job.path, self.job_info, self.handle_job_call, self.handle_job_property, None
        )

    def models_revision(self, status: dict) -> str:
        """Fingerprint model metadata and stable, already-sampled serving capacity."""
        now = time.monotonic()
        if self._models_revision is not None and now - self._models_revision_at < self.MODEL_REVISION_INTERVAL:
            return self._models_revision
        state_dir = ms.STATE_DIR
        paths = {
            state_dir / "models.list", state_dir / "models.json",
            *ms.control_files(state_dir),
            Path(os.environ.get("PUU_VLLM_ACTIVE_MODEL_FILE", str(state_dir / "active-model"))),
        }
        signatures = []

        def directory_entries(directory):
            try:
                info = directory.lstat()
                if stat.S_ISDIR(info.st_mode):
                    with os.scandir(directory) as entries:
                        result = sorted(entries, key=lambda entry: entry.name)
                else:
                    result = []
            except FileNotFoundError:
                signatures.append((str(directory), None))
                return []
            signatures.append((str(directory), (info.st_mode, info.st_uid, info.st_gid)))
            return result

        def model_files(root):
            paths.update(root / name for name in ("config.json", "puu-serving.env", ms.PRESEED_MANIFEST))
            pending = [root]
            while pending:
                directory = pending.pop()
                for entry in directory_entries(directory):
                    if entry.name.startswith("."):
                        continue
                    path = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                    elif ms.is_serving_file(entry.name) and not entry.name.endswith((".incomplete", ".lock")):
                        if not entry.is_symlink() or not entry.is_dir():
                            paths.add(path)

        for directory, pattern in (
            (state_dir / "models.d", "*.env"),
            (Path("/var/lib/puu/calibration"), "*.json"),
            (Path("/run/puu-calibration"), "status*.json"),
            (Path(os.environ.get("PUU_VLLM_MODEL_CATALOG_DIR", "/usr/share/puu/vllm/catalog.d")), "*.env"),
            (state_dir / "cache-exclusions", "*"),
        ):
            paths.update(Path(entry.path) for entry in directory_entries(directory)
                         if fnmatchcase(entry.name, pattern))
        for entry in directory_entries(ms.MODELS_DIR):
            if (not entry.name.startswith(".") and re.fullmatch(r"[A-Za-z0-9._-]+", entry.name)
                    and entry.is_dir(follow_symlinks=False)):
                model_files(Path(entry.path))
        for repo in directory_entries(ms.ModelSpec.hf_cache_dir()):
            if (not repo.name.startswith("models--") or not repo.is_dir(follow_symlinks=False)
                    or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]*(?:/[A-Za-z0-9_][A-Za-z0-9._-]*)?",
                                        repo.name.removeprefix("models--").replace("--", "/"))):
                continue
            repo_dir = Path(repo.path)
            for entry in directory_entries(repo_dir / "refs"):
                if entry.name == "main" and not entry.is_dir():
                    paths.add(Path(entry.path))
            for snapshot in directory_entries(repo_dir / "snapshots"):
                if re.fullmatch(r"[0-9a-f]{40}", snapshot.name) and snapshot.is_dir(follow_symlinks=False):
                    model_files(Path(snapshot.path))
        for path in sorted(paths):
            try:
                if path in (state_dir / "models.list", state_dir / "models.json") or (
                    path.parent == state_dir / "models.d" and path.suffix == ".env"
                ):
                    with path.open("rb") as stream:
                        content = stream.read(Contract.MAX_JSON + 1)
                    if len(content) > Contract.MAX_JSON:
                        raise ValueError(f"Model metadata exceeds size limit: {path}")
                    signature = hashlib.sha256(content).hexdigest()
                else:
                    info = path.stat()
                    signature = (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                                 info.st_mtime_ns, info.st_ctime_ns)
            except FileNotFoundError:
                signature = None
            signatures.append((str(path), signature))
        stable_status = {key: status.get(key) for key in (
            "active_model", "model_status", "selected_model", "serving_enabled",
        )}
        stable_status["host_memory_total_mib"] = (status.get("host_memory") or {}).get("total_mib")
        stable_status["gpus"] = sorted(
            ({key: gpu.get(key) for key in ("index", "name", "vendor", "memory_total_mib")}
             for gpu in status.get("gpus") or []),
            key=lambda gpu: json.dumps(gpu, sort_keys=True),
        )
        encoded = json.dumps((signatures, stable_status), sort_keys=True, separators=(",", ":"))
        self._models_revision = hashlib.sha256(encoded.encode()).hexdigest()
        self._models_revision_at = time.monotonic()
        return self._models_revision

    def root_properties(self, prop_name: str | None = None) -> dict[str, GLib.Variant]:
        """Keep Status stable between samples; GetStatus also reports elapsed age."""
        snapshot = self.monitoring.snapshot()
        data = snapshot.to_json()
        data["snapshot"].pop("age_seconds")
        status = snapshot.status if isinstance(snapshot.status, dict) else {}
        active = status.get("active_model") or ""
        serving = status.get("model_status") or ("ready" if active else "stopped")
        properties = {
            "Status": GLib.Variant("a{sv}", Contract.response_to_variant("GetStatus", data)),
            "ActiveModel": GLib.Variant("s", active),
            "ServingState": GLib.Variant("s", serving),
            "Jobs": GLib.Variant("ao", list(self.store.jobs.keys())),
        }
        if prop_name is None or prop_name == "ModelsRevision":
            properties["ModelsRevision"] = GLib.Variant("s", self.models_revision(status))
        return properties

    def handle_root_property(self, connection, sender, path: str, interface: str, prop_name: str):
        if interface != Contract.BUS:
            return None
        return self.root_properties(prop_name).get(prop_name)

    def handle_job_property(self, connection, sender, path: str, interface: str, prop_name: str):
        if interface != Contract.JOB:
            return None
        job = self.store.jobs.get(path)
        if job is None:
            return None
        return job.properties().get(prop_name)

    def handle_om_call(self, connection, sender, path, interface, method, parameters, invocation) -> None:
        try:
            self.authorize(sender)
            if method != "GetManagedObjects":
                raise OksaError("UnknownMethod", f"Unknown method {method}")
            managed = {
                job.path: {Contract.JOB: job.properties()}
                for job in self.store.jobs.values()
            }
            invocation.return_value(GLib.Variant("(a{oa{sa{sv}}})", (managed,)))
        except OksaError as error:
            self.return_error(invocation, error)
        except Exception as error:
            logging.exception("ObjectManager call failed")
            self.return_error(invocation, OksaError("OperationFailed", str(error)))

    def handle_job_call(self, connection, sender, path: str, interface: str, method: str, parameters, invocation) -> None:
        try:
            self.authorize(sender)
            if self.closed:
                raise OksaError("Unavailable", "Daemon is shutting down")
            if interface != Contract.JOB:
                raise OksaError("UnknownMethod", f"Unknown interface {interface}")
            if method == "GetSnapshot":
                self.return_snapshot(path, sender, invocation)
            elif method == "Cancel":
                self.cancel_job(path, sender, invocation)
            else:
                raise OksaError("UnknownMethod", f"Unknown job method {method}")
        except OksaError as error:
            self.return_error(invocation, error)
        except Exception as error:
            logging.exception("Job call failed")
            self.return_error(invocation, OksaError("OperationFailed", str(error)))

    def cancel_job(self, path: str, sender: str, invocation) -> None:
        job = self.store.jobs.get(path)
        if job is None:
            raise OksaError("NotFound", "Unknown or expired job")
        if job.state != "running":
            raise OksaError("InvalidState", f"Job is not running (state: {job.state})")
        worker = next((w for w in self.workers if w.job and w.job.path == path), None)
        if worker is not None:
            try:
                os.killpg(worker.process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            self.finish(worker, None, {"code": "Canceled", "message": "Operation was canceled by user"})
        else:
            job.state = "canceled"
            job.ended_at = time.time()
            job.error = {"code": "Canceled", "message": "Operation was canceled by user"}
            self.store.save(job)
            self.emit(job, "StateChanged", job.state)
            self.notify_job_properties(job, ("State", "EndedAt", "Error"))
        invocation.return_value(None)

    def notify_status_if_changed(self) -> None:
        snapshot = self.monitoring.snapshot()
        data = snapshot.to_json()
        data["snapshot"].pop("age_seconds")
        status = snapshot.status if isinstance(snapshot.status, dict) else {}
        active = status.get("active_model") or ""
        serving = status.get("model_status") or ("ready" if active else "stopped")
        revision = self.models_revision(status)
        changed = {}
        if data != self._last_status:
            changed["Status"] = GLib.Variant("a{sv}", Contract.response_to_variant("GetStatus", data))
        if active != self._last_active:
            changed["ActiveModel"] = GLib.Variant("s", active)
        if serving != self._last_serving:
            changed["ServingState"] = GLib.Variant("s", serving)
        if revision != self._last_models_revision:
            changed["ModelsRevision"] = GLib.Variant("s", revision)
        if not changed:
            return
        try:
            self.connection.emit_signal(
                None, Contract.PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (Contract.BUS, changed, []))
            )
        except GLib.Error:
            logging.exception("Unable to emit status property changes")
            return
        self._last_status = data
        self._last_active = active
        self._last_serving = serving
        self._last_models_revision = revision

    def authorize(self, sender: str) -> None:
        reply = self.connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                          "GetConnectionUnixUser", GLib.Variant("(s)", (sender,)),
                                          GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 5000, None)
        uid = reply.unpack()[0]
        if uid != 0 and (self.frontend_uid is None or uid != self.frontend_uid):
            raise OksaError("AccessDenied", "Only root or the dedicated oksa frontend may call this service")

    @staticmethod
    def return_error(invocation, error: OksaError) -> None:
        invocation.return_dbus_error(Contract.BUS + ".Error." + error.code, str(error)[:4096])

    def handle_call(self, connection, sender, path, interface, method, parameters, invocation) -> None:
        try:
            self.authorize(sender)
            if self.closed:
                raise OksaError("Unavailable", "Daemon is shutting down")
            args = parameters.unpack()
            if interface != Contract.BUS or path != Contract.PATH:
                raise OksaError("UnknownMethod", "Unknown domain method")
            Contract.validate(method, args)
            if method == "GetStatus":
                invocation.return_value(GLib.Variant("(a{sv})", (Contract.response_to_variant("GetStatus", self.monitoring.snapshot().to_json()),)))
            else:
                mutation = Contract.METHODS[method][2]
                if method in Contract.MODEL_MUTATIONS and any(worker.operation in Contract.MODEL_MUTATIONS for worker in self.workers):
                    raise OksaError("Busy", "Another model operation is already running")
                if method in Contract.USER_MUTATIONS and any(worker.operation in Contract.USER_MUTATIONS for worker in self.workers):
                    raise OksaError("Busy", "Another user or API-key operation is already running")
                if method not in Contract.MUTATIONS and sum(worker.operation not in Contract.MUTATIONS for worker in self.workers) >= self.READ_LIMIT:
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
        invocation.return_value(GLib.Variant("(a{sv})", (Contract.snapshot_to_variant(job.snapshot()),)))

    def launch(self, method: str, args: tuple, sender: str, invocation, mutation: bool) -> None:
        job = None
        if mutation:
            for path in self.store.prune(reserve=1):
                reg = self.registrations.pop(path, None)
                if reg is not None:
                    self.connection.unregister_object(reg)
                self.connection.emit_signal(
                    None, Contract.PATH, "org.freedesktop.DBus.ObjectManager", "InterfacesRemoved",
                    GLib.Variant("(oas)", (path, [Contract.JOB]))
                )
                self.connection.emit_signal(
                    None, Contract.PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                    GLib.Variant("(sa{sv}as)", (Contract.BUS, {"Jobs": GLib.Variant("ao", list(self.store.jobs.keys()))}, []))
                )
            job = Job(Contract.PATH + "/jobs/" + uuid.uuid4().hex, method, time.time(), subscribers={sender})
            if method == "DownloadModel":
                job.repo_id, job.model_name = args[:2]
            elif method in ("StartModel", "RestartModel", "RemoveModel", "Calibrate"):
                job.model_name = args[0]
            self.store.save(job)
            self.store.jobs[job.path] = job
            self.register_job(job)
            self.connection.emit_signal(
                None, Contract.PATH, "org.freedesktop.DBus.ObjectManager", "InterfacesAdded",
                GLib.Variant("(oa{sa{sv}})", (job.path, {Contract.JOB: job.properties()}))
            )
            self.connection.emit_signal(
                None, Contract.PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (Contract.BUS, {"Jobs": GLib.Variant("ao", list(self.store.jobs.keys()))}, []))
            )
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
                self.emit(job, "StateChanged", job.state)
                self.notify_job_properties(job, ("State", "EndedAt", "Error"))
            raise OksaError("Unavailable", "Could not start operation worker") from error
        finally:
            child.close()
        worker = Invocation(process, parent, time.monotonic(), None if mutation else invocation, method, job)
        if method in Contract.MODEL_MUTATIONS:
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

    def notify_job_properties(self, job: Job, names: tuple[str, ...]) -> None:
        properties = job.properties()
        changed = {name: properties[name] for name in names}
        try:
            self.connection.emit_signal(
                None, job.path, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (Contract.JOB, changed, []))
            )
        except GLib.Error:
            logging.exception("Unable to emit job property changes for %s", job.path)

    def finish(self, worker: Invocation, result: dict | None, error: dict | None) -> None:
        if result is not None and result.get("ok") is False and error is None:
            error = {"code": "OperationFailed", "message": str(result.get("error", "Operation failed"))[:4096]}
        if worker.job:
            job = worker.job
            job.result, job.error = result, error
            job.state = "canceled" if (error and error.get("code") == "Canceled") else ("failed" if error else "succeeded")
            job.ended_at = time.time()
            self.store.save(job)
            self.emit(job, "StateChanged", job.state)
            self.notify_job_properties(job, ("State", "EndedAt", "Result", "Error"))
        elif error:
            self.return_error(worker.invocation, OksaError(error["code"], error["message"]))
        else:
            worker.invocation.return_value(GLib.Variant("(a{sv})", (Contract.response_to_variant(worker.operation, result),)))
        self.dispose(worker)
        if worker.operation in Contract.MODEL_MUTATIONS:
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
        try:
            worker.channel.close()
        except OSError:
            pass
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
                        if worker.job.progress != packet["text"]:
                            worker.job.progress = packet["text"]
                            self.store.save(worker.job)
                            self.emit(worker.job, "Progress", worker.job.progress)
                            self.notify_job_properties(worker.job, ("Progress",))
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
            self.notify_status_if_changed()
        except Exception:
            logging.exception("Job processing failed; shutting down to avoid losing operation errors")
            self.timer = None
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
        if getattr(self, "timer", None) is not None:
            try:
                GLib.source_remove(self.timer)
            except Exception:
                pass
            self.timer = None
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
