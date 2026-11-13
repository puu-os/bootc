# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

import argparse
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
import sys
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
    commit: str | None = None
    user_id: str | None = None
    request_id: str | None = None
    subscribers: set[str] = field(default_factory=set)

    def snapshot(self) -> dict:
        return {
            "ok": True, "path": self.path, "operation": self.operation, "state": self.state,
            "progress": self.progress, "started_at": self.started_at, "ended_at": self.ended_at,
            "result": self.result, "error": self.error, "repo_id": self.repo_id,
            "model_name": self.model_name, "commit": self.commit,
            "user_id": self.user_id, "request_id": self.request_id,
        }

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
            "Commit": GLib.Variant("as", [] if self.commit is None else [self.commit]),
            "UserId": GLib.Variant("as", [] if self.user_id is None else [self.user_id]),
            "RequestId": GLib.Variant("as", [] if self.request_id is None else [self.request_id]),
        }


class JobStore:
    LIMIT = 64
    RECEIPT_LIMIT = 128
    TOMBSTONE_LIMIT = 2048
    UNRESOLVED_LIMIT = 64

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
        self.namespace: str = uuid.uuid4().hex
        self.receipts: dict[str, dict] = {}
        self.tombstones: dict[str, str] = {}
        try:
            self.load()
        except BaseException:
            self.lock.close()
            raise

    def _atomic_write_json(self, target: Path, payload: dict) -> None:
        text = Contract.encode(payload)
        fd, name = tempfile.mkstemp(prefix=".store-", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, target)
            directory_fd = os.open(self.directory, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(name).unlink(missing_ok=True)

    def load(self) -> None:
        interrupted_jobs: list[Job] = []
        files = sorted(self.directory.glob("*.json"), key=lambda path: path.name)
        for path in files:
            if not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > Contract.MAX_JSON:
                raise PermissionError(f"Unsafe job record: {path}")
            data = Contract.decode(path.read_text())
            Contract.validate_snapshot(data)
            job = Job(**{
                key: data.get(key)
                for key in (
                    "path", "operation", "started_at", "state", "progress",
                    "ended_at", "result", "error", "repo_id", "model_name",
                    "commit", "user_id", "request_id",
                )
            })
            Contract.validate_job_path(job.path)
            if job.path.rsplit("/", 1)[1] != path.stem or job.state not in ("running", "succeeded", "failed", "canceled"):
                raise ValueError(f"Invalid job record: {path}")
            if job.state == "running":
                job.state = "failed"
                job.ended_at = time.time()
                job.error = {"code": "Interrupted", "message": "Daemon restarted before completion; verify operation state before retrying"}
                self.save(job)
                interrupted_jobs.append(job)
            self.jobs[job.path] = job
        self.prune()
        self._load_receipts(interrupted_jobs)

    def _load_receipts(self, interrupted_jobs: list[Job]) -> None:
        receipts_path = self.directory / "receipts.json"
        dirty = False
        if receipts_path.exists():
            info = receipts_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > Contract.MAX_JSON:
                raise PermissionError(f"Unsafe receipts record: {receipts_path}")
            raw = Contract.decode(receipts_path.read_text())
            ns = raw.get("namespace")
            if not isinstance(ns, str) or not re.fullmatch(r"[0-9a-f]{32}", ns):
                raise ValueError("Invalid receipts namespace")
            self.namespace = ns
            raw_receipts = raw.get("receipts")
            raw_tombstones = raw.get("tombstones")
            if not isinstance(raw_receipts, dict) or not isinstance(raw_tombstones, dict):
                raise ValueError("Invalid receipts store")
            for req_id, item in raw_receipts.items():
                if not isinstance(item, dict) or item.get("request_id") != req_id:
                    raise ValueError(f"Invalid receipt entry: {req_id}")
                self.receipts[req_id] = dict(item)
            for req_id, digest in raw_tombstones.items():
                if not isinstance(req_id, str) or not isinstance(digest, str):
                    raise ValueError("Invalid receipt tombstone")
                self.tombstones[req_id] = digest
        else:
            dirty = True

        interrupted_paths = {job.path: job for job in interrupted_jobs}
        covered_paths = set()
        now = time.time()
        for receipt in self.receipts.values():
            if receipt.get("state") == "running" or receipt.get("job_path") in interrupted_paths:
                receipt["state"] = "unresolved"
                receipt["updated_at"] = now
                receipt["error_code"] = "Interrupted"
                receipt["error_message"] = "Daemon restarted before completion; verify state and run operator reconciliation"
                dirty = True
            if receipt.get("job_path"):
                covered_paths.add(receipt["job_path"])
        for job in interrupted_jobs:
            if job.path in covered_paths:
                continue
            req_id = job.request_id if (job.request_id and re.fullmatch(r"[0-9a-f]{32}:[0-9a-f]{32}", job.request_id)) else f"{self.namespace}:{job.path.rsplit('/', 1)[1]}"
            scope = "model" if job.operation in Contract.MODEL_MUTATIONS else "user"
            target = job.model_name or job.user_id or ("runtime" if job.operation == "StopServing" else "")
            self.receipts[req_id] = {
                "request_id": req_id,
                "operation": job.operation,
                "scope": scope,
                "target": target,
                "args_hash": "",
                "state": "unresolved",
                "created_at": job.started_at,
                "updated_at": now,
                "job_path": job.path,
                "summary": "Interrupted by daemon restart",
                "error_code": "Interrupted",
                "error_message": "Daemon restarted before completion; verify state and run operator reconciliation",
            }
            dirty = True
        if dirty:
            self.save_receipts()

    def save_receipts(self) -> None:
        self._atomic_write_json(
            self.directory / "receipts.json",
            {"namespace": self.namespace, "receipts": self.receipts, "tombstones": self.tombstones},
        )

    @staticmethod
    def public_receipt(receipt: dict) -> dict:
        return {
            "request_id": receipt["request_id"],
            "operation": receipt["operation"],
            "scope": receipt["scope"],
            "target": receipt["target"],
            "state": receipt["state"],
            "created_at": float(receipt["created_at"]),
            "updated_at": float(receipt["updated_at"]),
            "job_path": receipt.get("job_path") or None,
            "summary": receipt.get("summary") or None,
            "error_code": receipt.get("error_code") or None,
            "error_message": receipt.get("error_message") or None,
        }

    def list_receipts(self) -> list[dict]:
        ordered = sorted(self.receipts.values(), key=lambda r: float(r.get("updated_at") or 0.0), reverse=True)
        return [self.public_receipt(item) for item in ordered[:64]]

    def list_unresolved(self, scope: str | None = None) -> list[dict]:
        items = [
            r for r in self.receipts.values()
            if r.get("state") == "unresolved" and (scope is None or r.get("scope") == scope)
        ]
        items.sort(key=lambda r: float(r.get("updated_at") or 0.0), reverse=True)
        return [self.public_receipt(item) for item in items]

    def _prune_receipts(self, *, workers_active: bool) -> None:
        unresolved_count = sum(1 for r in self.receipts.values() if r.get("state") == "unresolved")
        if unresolved_count >= self.UNRESOLVED_LIMIT:
            raise OksaError("UnresolvedState", "Unresolved operation capacity exhausted; operator reconciliation is required")
        if len(self.receipts) < self.RECEIPT_LIMIT:
            return
        prunable = sorted(
            (r for r in self.receipts.values() if r.get("state") not in ("running", "unresolved")),
            key=lambda item: float(item.get("updated_at") or 0.0),
        )
        for item in prunable:
            if len(self.receipts) < self.RECEIPT_LIMIT:
                break
            if len(self.tombstones) >= self.TOMBSTONE_LIMIT:
                if workers_active:
                    raise OksaError("Busy", "Replay history limit reached while operations are active; retry after completion")
                self.namespace = uuid.uuid4().hex
                self.tombstones.clear()
                self.save_receipts()
                raise OksaError("ExpiredNamespace", "Request ID namespace rotated; refresh state before submitting")
            req_id = item["request_id"]
            if req_id.startswith(self.namespace + ":"):
                self.tombstones[req_id] = item.get("args_hash") or ""
            del self.receipts[req_id]

    def begin_mutation(
        self,
        request_id: str,
        method: str,
        args: tuple,
        *,
        workers_active: bool,
        busy_error: str = "",
    ) -> dict:
        req_ns, _, _ = request_id.partition(":")
        args_hash = Contract.args_digest(method, args)
        if request_id in self.receipts:
            existing = self.receipts[request_id]
            if existing.get("args_hash") and existing["args_hash"] != args_hash:
                raise OksaError("InvalidInput", "Request ID was already used with different arguments")
            raise OksaError("ReplayRejected", f"Request {request_id} was already submitted (state: {existing['state']})")
        if request_id in self.tombstones:
            if self.tombstones[request_id] and self.tombstones[request_id] != args_hash:
                raise OksaError("InvalidInput", "Request ID was already used with different arguments")
            raise OksaError("ReplayRejected", f"Request {request_id} was already executed; receipt details were pruned")
        if req_ns != self.namespace:
            raise OksaError("ExpiredNamespace", "Request ID namespace is no longer active; refresh state before submitting")
        scope = "model" if method in Contract.MODEL_MUTATIONS else "user"
        unresolved = self.list_unresolved(scope)
        if unresolved:
            first = unresolved[0]
            raise OksaError(
                "UnresolvedState",
                f"{scope.capitalize()} mutations are blocked by unresolved {first['operation']} on {first['target']} ({first['request_id']}). "
                "Operator reconciliation is required.",
            )
        if busy_error:
            raise OksaError("Busy", busy_error)
        self._prune_receipts(workers_active=workers_active)
        now = time.time()
        is_job = Contract.METHODS[method][2]
        target = Contract.mutation_target(method, args)
        receipt = {
            "request_id": request_id,
            "operation": method,
            "scope": scope,
            "target": target,
            "args_hash": args_hash,
            "state": "running",
            "created_at": now,
            "updated_at": now,
            "job_path": "",
            "summary": "Accepted job" if is_job else "Synchronous mutation in flight",
            "error_code": "",
            "error_message": "",
        }
        self.receipts[request_id] = receipt
        self.save_receipts()
        return receipt

    def record_unconfirmed(
        self,
        request_id: str,
        operation: str,
        target: str,
        detail: str,
        *,
        active_request_ids: set[str] | None = None,
    ) -> dict:
        if request_id in self.receipts:
            existing = self.receipts[request_id]
            if existing.get("state") == "running" and not (active_request_ids and request_id in active_request_ids):
                existing["state"] = "unresolved"
                existing["updated_at"] = time.time()
                existing["summary"] = "Mutation outcome was not confirmed"
                existing["error_code"] = "Unconfirmed"
                existing["error_message"] = detail or "Request timed out or disconnected before confirmation"
                self.save_receipts()
            return self.public_receipt(existing)
        if request_id in self.tombstones:
            now = time.time()
            return {
                "request_id": request_id,
                "operation": "pruned",
                "scope": "model" if operation in Contract.MODEL_MUTATIONS else "user",
                "target": target or "",
                "state": "pruned",
                "created_at": now,
                "updated_at": now,
                "job_path": None,
                "summary": "Completed receipt details were pruned; replay remains blocked",
                "error_code": None,
                "error_message": None,
            }
        unresolved_count = sum(1 for r in self.receipts.values() if r.get("state") == "unresolved")
        if unresolved_count >= self.UNRESOLVED_LIMIT:
            raise OksaError("UnresolvedState", "Unresolved operation capacity exhausted; operator reconciliation is required")
        now = time.time()
        scope = "model" if operation in Contract.MODEL_MUTATIONS else "user"
        receipt = {
            "request_id": request_id,
            "operation": operation,
            "scope": scope,
            "target": target or ("runtime" if scope == "model" else "gateway"),
            "args_hash": "",
            "state": "unresolved",
            "created_at": now,
            "updated_at": now,
            "job_path": "",
            "summary": "Client request acceptance was not confirmed",
            "error_code": "Unconfirmed",
            "error_message": detail or "Request timed out or disconnected before confirmation",
        }
        self.receipts[request_id] = receipt
        self.save_receipts()
        return self.public_receipt(receipt)

    def update_receipt(
        self,
        request_id: str | None,
        *,
        state: str,
        job_path: str | None = None,
        summary: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if not request_id or request_id not in self.receipts:
            return
        receipt = self.receipts[request_id]
        receipt["state"] = state
        receipt["updated_at"] = time.time()
        if job_path is not None:
            receipt["job_path"] = job_path
        if summary is not None:
            receipt["summary"] = summary[:1024]
        if error_code is not None:
            receipt["error_code"] = error_code
        if error_message is not None:
            receipt["error_message"] = error_message[:1024]
        self.save_receipts()

    def save(self, job: Job) -> None:
        self._atomic_write_json(self.directory / (job.path.rsplit("/", 1)[1] + ".json"), job.snapshot())

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
    request_id: str | None = None
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
      <property name="Commit" type="as" access="read"><annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="const"/></property>
      <property name="UserId" type="as" access="read"><annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="const"/></property>
      <property name="RequestId" type="as" access="read"><annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="const"/></property>
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
        self.timer = None
        self.frontend_uid = pwd.getpwnam("oksa").pw_uid
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
            self.settle_job(job, "canceled", None, {"code": "Canceled", "message": "Operation was canceled by user"})
            self.store.update_receipt(
                job.request_id,
                state="canceled",
                job_path=job.path,
                summary="Operation was canceled by user",
                error_code="Canceled",
                error_message="Operation was canceled by user",
            )
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

    def authorize(self, sender: str, *, root_only: bool = False) -> None:
        reply = self.connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                          "GetConnectionUnixUser", GLib.Variant("(s)", (sender,)),
                                          GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 5000, None)
        uid = reply.unpack()[0]
        if root_only:
            if uid != 0:
                raise OksaError("AccessDenied", "Only root may reconcile unresolved operation state")
            return
        if uid not in (0, self.frontend_uid):
            raise OksaError("AccessDenied", "Only root or the dedicated oksa frontend may call this service")

    @staticmethod
    def return_error(invocation, error: OksaError) -> None:
        invocation.return_dbus_error(Contract.BUS + ".Error." + error.code, str(error)[:4096])

    def _handle_get_receipt(self, request_id: str) -> dict:
        if request_id in self.store.receipts:
            return {
                "ok": True,
                "found": True,
                "namespace": self.store.namespace,
                "receipt": self.store.public_receipt(self.store.receipts[request_id]),
            }
        if request_id in self.store.tombstones:
            now = time.time()
            return {
                "ok": True,
                "found": True,
                "namespace": self.store.namespace,
                "receipt": {
                    "request_id": request_id,
                    "operation": "pruned",
                    "scope": "unknown",
                    "target": "",
                    "state": "pruned",
                    "created_at": now,
                    "updated_at": now,
                    "job_path": None,
                    "summary": "Completed receipt details were pruned; replay remains blocked",
                    "error_code": None,
                    "error_message": None,
                },
            }
        return {"ok": True, "found": False, "namespace": self.store.namespace, "receipt": None}

    def _handle_reconcile(self, request_id: str) -> dict:
        receipt = self.store.receipts.get(request_id)
        if receipt is None:
            raise OksaError("NotFound", f"Unknown request ID: {request_id}")
        if receipt.get("state") != "unresolved":
            return {
                "ok": True,
                "found": True,
                "namespace": self.store.namespace,
                "receipt": self.store.public_receipt(receipt),
            }
        scope = receipt.get("scope", "model")
        scope_ops = Contract.MODEL_MUTATIONS if scope == "model" else Contract.USER_MUTATIONS
        if any(worker.operation in scope_ops for worker in self.workers):
            raise OksaError("Busy", "Wait for active operations in this scope to settle before reconciling")
        reconcile_result = self.service_class().reconcile_scope(scope, str(receipt.get("target") or ""))
        self.store.update_receipt(
            request_id,
            state="reconciled",
            summary=str(reconcile_result.get("message") or "Reconciled by root operator"),
            error_code="",
            error_message="",
        )
        if scope == "model":
            self.monitoring.invalidate()
        return {
            "ok": True,
            "found": True,
            "namespace": self.store.namespace,
            "receipt": self.store.public_receipt(self.store.receipts[request_id]),
        }

    def handle_call(self, connection, sender, path, interface, method, parameters, invocation) -> None:
        try:
            self.authorize(sender, root_only=(method == "ReconcileUnresolved"))
            if self.closed:
                raise OksaError("Unavailable", "Daemon is shutting down")
            args = parameters.unpack()
            if interface != Contract.BUS or path != Contract.PATH:
                raise OksaError("UnknownMethod", "Unknown domain method")
            Contract.validate(method, args)
            if method == "GetStatus":
                invocation.return_value(GLib.Variant("(a{sv})", (Contract.response_to_variant("GetStatus", self.monitoring.snapshot().to_json()),)))
            elif method == "GetDaemonState":
                payload = {
                    "ok": True,
                    "namespace": self.store.namespace,
                    "receipts": self.store.list_receipts(),
                    "unresolved": self.store.list_unresolved(),
                }
                invocation.return_value(GLib.Variant("(a{sv})", (Contract.response_to_variant("GetDaemonState", payload),)))
            elif method == "GetReceipt":
                payload = self._handle_get_receipt(args[0])
                invocation.return_value(GLib.Variant("(a{sv})", (Contract.response_to_variant("GetReceipt", payload),)))
            elif method == "RecordUnconfirmed":
                active_ids = {w.request_id for w in self.workers if w.request_id}
                receipt = self.store.record_unconfirmed(*args, active_request_ids=active_ids)
                payload = {"ok": True, "found": True, "namespace": self.store.namespace, "receipt": receipt}
                invocation.return_value(GLib.Variant("(a{sv})", (Contract.response_to_variant("RecordUnconfirmed", payload),)))
            elif method == "ReconcileUnresolved":
                payload = self._handle_reconcile(args[0])
                invocation.return_value(GLib.Variant("(a{sv})", (Contract.response_to_variant("ReconcileUnresolved", payload),)))
            else:
                mutation = Contract.METHODS[method][2]
                busy_error = ""
                if method in Contract.MODEL_MUTATIONS and any(worker.operation in Contract.MODEL_MUTATIONS for worker in self.workers):
                    busy_error = "Another model operation is already running"
                elif method in Contract.USER_MUTATIONS and any(worker.operation in Contract.USER_MUTATIONS for worker in self.workers):
                    busy_error = "Another user or API-key operation is already running"
                elif method not in Contract.MUTATIONS and sum(worker.operation not in Contract.MUTATIONS for worker in self.workers) >= self.READ_LIMIT:
                    raise OksaError("Busy", "All read workers are busy; retry later")
                request_id = None
                if method in Contract.MUTATIONS:
                    request_id = args[0]
                    workers_active = any(worker.operation in Contract.MUTATIONS for worker in self.workers)
                    self.store.begin_mutation(
                        request_id,
                        method,
                        args,
                        workers_active=workers_active,
                        busy_error=busy_error,
                    )
                elif busy_error:
                    raise OksaError("Busy", busy_error)
                self.launch(method, args, sender, invocation, mutation, request_id=request_id)
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

    def launch(self, method: str, args: tuple, sender: str, invocation, mutation: bool, *, request_id: str | None = None) -> None:
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
            job = Job(Contract.PATH + "/jobs/" + uuid.uuid4().hex, method, time.time(), request_id=request_id, subscribers={sender})
            if method == "DownloadModel":
                _, repo, name, commit, *_ = args
                job.repo_id = repo
                job.model_name = ms.normalize_storage_id(repo, name or None) or name
                job.commit = commit
            elif method in ("StartModel", "RestartModel", "RemoveModel", "Calibrate"):
                raw_name = args[1]
                try:
                    resolved = ms.resolve_model_name(raw_name, ms.STATE_DIR)
                except Exception:
                    resolved = raw_name
                job.model_name = resolved
                installed = ms.inspect_installed_model(resolved, ms.STATE_DIR, ms.MODELS_DIR)
                if installed is not None:
                    job.repo_id = installed.get("repo_id") or None
                    job.commit = installed.get("revision") or None
            elif method == "DeleteUser":
                job.user_id = args[1]
            self.store.save(job)
            self.store.jobs[job.path] = job
            if request_id:
                self.store.update_receipt(request_id, state="running", job_path=job.path, summary=f"Job {job.path.rsplit('/', 1)[1]} running")
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
                self.settle_job(job, "failed", None, {"code": "Unavailable", "message": str(error)[:4096]})
            if request_id:
                self.store.update_receipt(
                    request_id, state="failed", summary="Worker failed to start before execution",
                    error_code="Unavailable", error_message=str(error)[:1024],
                )
            raise OksaError("Unavailable", "Could not start operation worker") from error
        finally:
            child.close()
        worker = Invocation(process, parent, time.monotonic(), None if mutation else invocation, method, job, request_id=request_id)
        if method in Contract.MODEL_MUTATIONS:
            self.monitoring.invalidate()
        self.workers.append(worker)
        if mutation:
            invocation.return_value(GLib.Variant("(o)", (job.path,)))

    def settle_job(self, job: Job, state: str, result: dict | None, error: dict | None) -> None:
        job.state, job.result, job.error = state, result, error
        job.ended_at = time.time()
        self.store.save(job)
        self.emit(job, "StateChanged", job.state)
        self.notify_job_properties(job, ("State", "EndedAt", "Result", "Error"))

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

    @staticmethod
    def _download_publication_uncertain(model_name: str | None) -> bool:
        if not model_name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", model_name):
            return False
        backup_dir = ms.MODELS_DIR.parent / f".{model_name}.previous"
        dest_dir = ms.MODELS_DIR / model_name
        if backup_dir.exists():
            return True
        if dest_dir.exists():
            if not ms.verified_preseed(dest_dir):
                return True
            if not any(entry[0] == model_name for entry in ms.read_models_list(ms.STATE_DIR / "models.list")):
                return True
        return False

    @staticmethod
    def _receipt_summary(operation: str, result: dict | None, error: dict | None) -> str:
        if isinstance(result, dict):
            if operation == "CreateKey":
                key_obj = result.get("key") if isinstance(result.get("key"), dict) else {}
                alias = key_obj.get("key_alias") or "unnamed"
                key_id = key_obj.get("key_id") or "unknown-id"
                owner = key_obj.get("user_id") or ""
                return f"Key {alias} (id: {key_id}) created for user {owner}; one-time secret is not retained in receipt"
            if operation == "CreateUser":
                user_obj = result.get("result") if isinstance(result.get("result"), dict) else {}
                return f"User {user_obj.get('user_id') or ''} created"
            for field_name in ("message", "warning", "error", "outcome"):
                val = result.get(field_name)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        return operation

    def finish(self, worker: Invocation, result: dict | None, error: dict | None) -> None:
        if result is not None and result.get("ok") is False and error is None:
            code = "ReconciliationFailed" if result.get("outcome") == "reconciliation_failed" else "OperationFailed"
            msg = result.get("error") or result.get("warning") or result.get("message") or "Operation failed"
            error = {"code": code, "message": str(msg)[:4096]}
        if worker.job:
            state = "canceled" if (error and error.get("code") == "Canceled") else ("failed" if error else "succeeded")
            self.settle_job(worker.job, state, result, error)
        elif error:
            self.return_error(worker.invocation, OksaError(error["code"], error["message"]))
        else:
            worker.invocation.return_value(GLib.Variant("(a{sv})", (Contract.response_to_variant(worker.operation, result),)))
        self.dispose(worker)
        if worker.request_id:
            summary = self._receipt_summary(worker.operation, result, error)
            err_code = (error or {}).get("code", "")
            err_msg = (error or {}).get("message", "")
            unresolved = False
            if err_code in ("Unconfirmed", "Timeout", "WorkerFailed", "Interrupted"):
                unresolved = True
            elif worker.operation == "RemoveModel" and isinstance(result, dict) and result.get("outcome") in ("reconciliation_failed", "removal_failed"):
                unresolved = True
            elif worker.operation == "DeleteUser" and isinstance(result, dict) and result.get("outcome") in ("key_revocation_unconfirmed", "user_deletion_unconfirmed"):
                unresolved = True
            elif worker.operation == "DownloadModel" and worker.job and worker.job.state in ("failed", "canceled"):
                unresolved = self._download_publication_uncertain(worker.job.model_name)
            receipt_state = "unresolved" if unresolved else (worker.job.state if worker.job else ("failed" if error else "succeeded"))
            self.store.update_receipt(
                worker.request_id,
                state=receipt_state,
                job_path=worker.job.path if worker.job else "",
                summary=summary,
                error_code=err_code or ((result or {}).get("outcome") or "" if unresolved else ""),
                error_message=err_msg or (summary if unresolved else ""),
            )
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
        if self.timer is not None:
            GLib.source_remove(self.timer)
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
        parser = argparse.ArgumentParser(description="OKSA D-Bus administration daemon")
        parser.add_argument("--reconcile", metavar="REQUEST_ID", help="Verify system state and clear an unresolved write guard (root only)")
        parser.add_argument("--list-unresolved", action="store_true", help="List active unresolved write guards")
        args = parser.parse_args()
        if args.reconcile or args.list_unresolved:
            if os.geteuid() != 0:
                sys.exit("error: operator reconciliation requires root")
            from oksa_client import OksaClient, OksaClientError

            client = OksaClient()
            try:
                if args.reconcile:
                    result = client.call("ReconcileUnresolved", args.reconcile.strip())
                else:
                    result = client.call("GetDaemonState")
                print(json.dumps(result, indent=2))
            except OksaClientError as exc:
                sys.exit(f"error ({exc.code}): {exc}")
            finally:
                client.close()
            return
        logging.basicConfig(level=logging.INFO)
        service = OksaDBusService()
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, cls.stop, service)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, cls.stop, service)
        service.run()

    @staticmethod
    def stop(service: OksaDBusService) -> bool:
        service.loop.quit()
        return GLib.SOURCE_REMOVE
