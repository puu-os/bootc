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
        try:
            return Contract.decode(reply.unpack()[0])
        except OksaError as error:
            raise OksaClientError(error.code, str(error)) from error

    def wait_job(self, path: str, timeout: float = 600) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            snapshot = self.call("GetJob", path)
            if snapshot["state"] == "succeeded":
                return snapshot["result"]
            if snapshot["state"] == "failed":
                error = snapshot["error"] or {"code": "OperationFailed", "message": "Job failed"}
                payload = snapshot["result"] if snapshot["result"] is not None else {
                    "ok": False, "code": error["code"], "error": error["message"], "job": path}
                raise OksaClientError(error["code"], error["message"], payload)
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
