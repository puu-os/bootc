# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

import threading
import time

from gi.repository import Gio, GLib

from oksa_control import Contract, OksaError


class OksaClientError(OksaError):
    def __init__(self, code: str, message: str, payload: dict | None = None) -> None:
        super().__init__(code, message, payload)
        self.payload = payload if payload is not None else {"ok": False, "code": code, "error": message}


class OksaClient:
    def __init__(self, connection=None, timeout: float = 95) -> None:
        self.connection = connection
        self.owns_connection = connection is None
        self.timeout = timeout
        self.lock = threading.RLock()
        self.closed = False

    def connect(self):
        with self.lock:
            if self.closed:
                raise OksaClientError("Unavailable", "Client is closed")
            if self.connection is None or self.connection.is_closed():
                try:
                    address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SYSTEM, None)
                    self.connection = Gio.DBusConnection.new_for_address_sync(
                        address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                        None, None)
                    self.owns_connection = True
                except GLib.Error as error:
                    raise OksaClientError("Unavailable", error.message) from error
            return self.connection

    def call(self, method: str, *args) -> dict:
        try:
            Contract.validate(method, args)
        except OksaError as error:
            raise OksaClientError(error.code, str(error)) from error
        fields, output, mutation = Contract.METHODS[method]
        signature = "(" + "".join(kind for _, kind in fields) + ")"
        try:
            reply = self.connect().call_sync(Contract.BUS, Contract.PATH, Contract.BUS, method,
                                             GLib.Variant(signature, args), GLib.VariantType.new(f"({output})"),
                                             Gio.DBusCallFlags.NONE, int(self.timeout * 1000), None)
        except GLib.Error as error:
            remote = Gio.DBusError.get_remote_error(error)
            code = remote.rsplit(".", 1)[-1] if remote else "Unavailable"
            Gio.DBusError.strip_remote_error(error)
            try:
                payload = Contract.decode(error.message)
            except OksaError:
                payload = {"ok": False, "code": code, "error": error.message}
            raise OksaClientError(code, str(payload.get("error", error.message)), payload) from error
        if mutation:
            return {"ok": True, "job": reply.unpack()[0]}
        return reply.unpack()[0]

    def get_job(self, path: str) -> dict:
        Contract.validate("GetJob", (path,))
        try:
            reply = self.connect().call_sync(Contract.BUS, path, Contract.JOB, "GetSnapshot",
                                             None, GLib.VariantType.new("(a{sv})"),
                                             Gio.DBusCallFlags.NONE, int(self.timeout * 1000), None)
            return reply.unpack()[0]
        except GLib.Error:
            return self.call("GetJob", path)

    def cancel_job(self, path: str) -> bool:
        Contract.validate("GetJob", (path,))
        try:
            self.connect().call_sync(Contract.BUS, path, Contract.JOB, "Cancel",
                                     None, None, Gio.DBusCallFlags.NONE, int(self.timeout * 1000), None)
            return True
        except GLib.Error as error:
            remote = Gio.DBusError.get_remote_error(error)
            code = remote.rsplit(".", 1)[-1] if remote else "Unavailable"
            Gio.DBusError.strip_remote_error(error)
            raise OksaClientError(code, error.message, {"ok": False, "code": code, "error": error.message}) from error

    def get_managed_objects(self) -> dict:
        try:
            reply = self.connect().call_sync(Contract.BUS, Contract.PATH, "org.freedesktop.DBus.ObjectManager",
                                             "GetManagedObjects", None, GLib.VariantType.new("(a{oa{sa{sv}}})"),
                                             Gio.DBusCallFlags.NONE, int(self.timeout * 1000), None)
            return reply.unpack()[0]
        except GLib.Error as error:
            remote = Gio.DBusError.get_remote_error(error)
            code = remote.rsplit(".", 1)[-1] if remote else "Unavailable"
            Gio.DBusError.strip_remote_error(error)
            raise OksaClientError(code, error.message, {"ok": False, "code": code, "error": error.message}) from error

    def get_properties(self, interface: str = Contract.BUS, path: str = Contract.PATH) -> dict:
        try:
            reply = self.connect().call_sync(Contract.BUS, path, "org.freedesktop.DBus.Properties",
                                             "GetAll", GLib.Variant("(s)", (interface,)),
                                             GLib.VariantType.new("(a{sv})"), Gio.DBusCallFlags.NONE,
                                             int(self.timeout * 1000), None)
            return reply.unpack()[0]
        except GLib.Error as error:
            remote = Gio.DBusError.get_remote_error(error)
            code = remote.rsplit(".", 1)[-1] if remote else "Unavailable"
            Gio.DBusError.strip_remote_error(error)
            raise OksaClientError(code, error.message, {"ok": False, "code": code, "error": error.message}) from error

    def wait_job(self, path: str, timeout: float = 600) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            snapshot = self.get_job(path)
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
                raise OksaClientError("Timeout", "Waiting timed out; the job is still running",
                                      {"ok": False, "job": path, "error": "Waiting timed out"})
            time.sleep(min(0.2, remaining))
    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.owns_connection and self.connection is not None and not self.connection.is_closed():
                self.connection.close_sync(None)
            self.connection = None
