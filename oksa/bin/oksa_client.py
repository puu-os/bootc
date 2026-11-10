# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

import math
import threading
import time
from contextlib import contextmanager

from gi.repository import Gio, GLib

from oksa_control import Contract, OksaError


class OksaClientError(OksaError):
    def __init__(self, code: str, message: str, payload: dict | None = None) -> None:
        super().__init__(code, message, payload)
        self.payload = payload if payload is not None else {"ok": False, "code": code, "error": message}

    @classmethod
    def from_dbus(cls, error: GLib.Error) -> "OksaClientError":
        remote = Gio.DBusError.get_remote_error(error)
        if not remote:
            return cls("Unconfirmed", error.message)
        code = remote.rsplit(".", 1)[-1]
        if code in ("NoReply", "Timeout", "TimedOut", "Disconnected"):
            code = "Unconfirmed"
        message = error.message.removeprefix(f"GDBus.Error:{remote}: ")
        return cls(code, message)


class OksaClient:
    def __init__(self, connection=None, timeout: float = 95) -> None:
        self._validate_timeout(timeout)
        self.connection = connection
        self.owns_connection = connection is None
        self.timeout = timeout
        self.lock = threading.RLock()
        self.closed = False

    @staticmethod
    def _validate_timeout(timeout: float, allow_zero: bool = False) -> None:
        if (type(timeout) not in (int, float) or not 0 <= timeout <= threading.TIMEOUT_MAX
                or not math.isfinite(timeout) or (timeout == 0 and not allow_zero)):
            raise OksaClientError("InvalidInput", "Timeout must be finite and positive within the supported range")

    @staticmethod
    def _timeout_error(deadline=None, path=None) -> OksaClientError:
        if deadline is not None and time.monotonic() >= deadline:
            return OksaClientError("Timeout", "Waiting timed out; job outcome is unknown",
                                   {"ok": False, "code": "Timeout", "job": path, "error": "Waiting timed out"})
        return OksaClientError("Unconfirmed", "Request timed out; outcome is unknown")

    @contextmanager
    def _operation(self, deadline=None, path=None):
        end = time.monotonic() + self.timeout
        if deadline is not None:
            end = min(end, deadline)
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise self._timeout_error(deadline, path)
        cancellable = Gio.Cancellable.new()
        timer = threading.Timer(min(remaining, threading.TIMEOUT_MAX), cancellable.cancel)
        timer.start()
        try:
            yield end, cancellable
            if time.monotonic() >= end:
                raise self._timeout_error(deadline, path)
        except TimeoutError as error:
            raise self._timeout_error(deadline, path) from error
        except GLib.Error as error:
            if not Gio.DBusError.get_remote_error(error) and (cancellable.is_cancelled() or time.monotonic() >= end):
                raise self._timeout_error(deadline, path) from error
            raise OksaClientError.from_dbus(error) from error
        finally:
            timer.cancel()
            timer.join()

    def _connect(self, deadline, cancellable):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self.lock.acquire(timeout=min(remaining, threading.TIMEOUT_MAX)):
            raise TimeoutError
        try:
            if self.closed:
                raise OksaClientError("Unavailable", "Client is closed")
            if time.monotonic() >= deadline:
                raise TimeoutError
            if self.connection is None or self.connection.is_closed():
                address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SYSTEM, cancellable)
                self.connection = Gio.DBusConnection.new_for_address_sync(
                    address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                    None, cancellable)
                self.owns_connection = True
            return self.connection
        except GLib.Error as error:
            if (Gio.DBusError.get_remote_error(error) or cancellable.is_cancelled()
                    or time.monotonic() >= deadline):
                raise
            raise OksaClientError("Unavailable", error.message) from error
        finally:
            self.lock.release()

    def connect(self):
        with self._operation() as (deadline, cancellable):
            return self._connect(deadline, cancellable)

    def _request(self, path, interface, method, parameters, reply_type, *, deadline=None):
        with self._operation(deadline, path) as (end, cancellable):
            connection = self._connect(end, cancellable)
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            return connection.call_sync(Contract.BUS, path, interface, method, parameters,
                                        GLib.VariantType.new(reply_type) if reply_type is not None else None,
                                        Gio.DBusCallFlags.NONE, min(2147483647, math.ceil(remaining * 1000)), cancellable)

    def call(self, method: str, *args) -> dict:
        try:
            Contract.validate(method, args)
        except OksaError as error:
            raise OksaClientError(error.code, str(error), error.result) from error
        fields, output, job = Contract.METHODS[method]
        signature = "(" + "".join(kind for _, kind in fields) + ")"
        reply = self._request(Contract.PATH, Contract.BUS, method, GLib.Variant(signature, args), f"({output})")
        try:
            if job:
                path = reply.unpack()[0]
                Contract.validate_job_path(path)
                return {"ok": True, "job": path}
            return Contract.response_from_variant(method, reply.get_child_value(0))
        except OksaError as error:
            code = "Unconfirmed" if job else error.code
            raise OksaClientError(code, str(error), error.result) from error

    def get_job(self, path: str, *, _deadline=None) -> dict:
        try:
            Contract.validate_job_path(path)
        except OksaError as error:
            raise OksaClientError(error.code, str(error), error.result) from error
        reply = self._request(path, Contract.JOB, "GetSnapshot", None, "(a{sv})", deadline=_deadline)
        try:
            return Contract.snapshot_from_variant(reply.get_child_value(0))
        except OksaError as error:
            raise OksaClientError(error.code, str(error), error.result) from error

    def cancel_job(self, path: str) -> bool:
        try:
            Contract.validate_job_path(path)
        except OksaError as error:
            raise OksaClientError(error.code, str(error), error.result) from error
        self._request(path, Contract.JOB, "Cancel", None, None)
        return True

    def get_managed_objects(self) -> dict:
        """Return raw ObjectManager properties; use get_job for decoded snapshots."""
        reply = self._request(Contract.PATH, "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
                              None, "(a{oa{sa{sv}}})")
        return reply.unpack()[0]

    def get_properties(self, interface: str = Contract.BUS, path: str = Contract.PATH) -> dict:
        """Return raw wire properties; use call for decoded domain responses."""
        reply = self._request(path, "org.freedesktop.DBus.Properties", "GetAll",
                              GLib.Variant("(s)", (interface,)), "(a{sv})")
        return reply.unpack()[0]

    def wait_job(self, path: str, timeout: float = 600) -> dict:
        self._validate_timeout(timeout, allow_zero=True)
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() >= deadline:
                raise self._timeout_error(deadline, path)
            snapshot = self.get_job(path, _deadline=deadline)
            if time.monotonic() >= deadline:
                raise self._timeout_error(deadline, path)
            if snapshot["state"] == "succeeded":
                return snapshot["result"]
            if snapshot["state"] in ("failed", "canceled"):
                error = snapshot.get("error") or {"code": "OperationFailed", "message": "Job failed"}
                payload = snapshot.get("result") if snapshot.get("result") is not None else {
                    "ok": False, "code": error.get("code", "OperationFailed"),
                    "error": error.get("message", "Job failed"), "job": path}
                raise OksaClientError(error.get("code", "OperationFailed"), error.get("message", "Job failed"), payload)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._timeout_error(deadline, path)
            time.sleep(min(0.2, remaining))

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.owns_connection and self.connection is not None and not self.connection.is_closed():
                self.connection.close_sync(None)
            self.connection = None
