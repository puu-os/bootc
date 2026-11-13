# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from gi.repository import GLib
import oksa_calibration_state as calibration
import oksa_model_removal as removal
import oksa_model_serving as ms
import oksa_user_deletion as user_deletion


class OksaError(Exception):
    def __init__(self, code: str, message: str, result: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.result = result


@dataclass(frozen=True)
class RecordSchema:
    """Absent optional fields mean None; opaque JSON preserves upstream data and provenance."""

    fields: "dict[str, str | type | RecordSchema | tuple[RecordSchema]]"
    optional: frozenset[str] = frozenset()

    def to_variant(self, data: dict) -> dict[str, GLib.Variant]:
        if not isinstance(data, dict) or data.keys() - self.fields.keys():
            raise OksaError("InvalidResult", "Unexpected response fields")
        values = {}
        for name, kind in self.fields.items():
            if name not in data or data[name] is None:
                if name in self.optional:
                    continue
                raise OksaError("InvalidResult", f"Missing response field: {name}")
            value = data[name]
            if isinstance(kind, RecordSchema):
                values[name] = GLib.Variant("a{sv}", kind.to_variant(value))
            elif isinstance(kind, tuple):
                if not isinstance(value, list):
                    raise OksaError("InvalidResult", f"{name} must be an array")
                values[name] = GLib.Variant("aa{sv}", [kind[0].to_variant(item) for item in value])
            elif kind in (dict, list):
                if not isinstance(value, kind):
                    raise OksaError("InvalidResult", f"Invalid JSON field: {name}")
                values[name] = GLib.Variant("s", json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":")))
            else:
                valid = ((kind in ("s", "o") and isinstance(value, str) and "\x00" not in value
                          and not any(unicodedata.category(char) == "Cs" for char in value))
                         or (kind == "b" and type(value) is bool)
                         or (kind == "x" and type(value) is int and -(2**63) <= value < 2**63)
                         or (kind == "d" and ((type(value) is float and math.isfinite(value))
                                             or (type(value) is int and -(2**53) <= value <= 2**53))))
                if not valid:
                    raise OksaError("InvalidResult", f"Invalid response type for {name}")
                if kind == "o" and not GLib.Variant.is_object_path(value):
                    raise OksaError("InvalidResult", f"Invalid object path: {name}")
                values[name] = GLib.Variant(kind, value)
        return values

    def from_variant(self, value: GLib.Variant) -> dict:
        if value.get_type_string() != "a{sv}":
            raise OksaError("InvalidResult", "Expected a response dictionary")
        fields = {}
        for index in range(value.n_children()):
            entry = value.get_child_value(index)
            name = entry.get_child_value(0).get_string()
            if name in fields or name not in self.fields:
                raise OksaError("InvalidResult", f"Unexpected response field: {name}")
            fields[name] = entry.get_child_value(1).get_variant()
        result = {}
        for name, kind in self.fields.items():
            if name not in fields:
                if name in self.optional:
                    result[name] = None
                    continue
                raise OksaError("InvalidResult", f"Missing response field: {name}")
            item = fields[name]
            if isinstance(kind, RecordSchema):
                result[name] = kind.from_variant(item)
            elif isinstance(kind, tuple):
                if item.get_type_string() != "aa{sv}":
                    raise OksaError("InvalidResult", f"Invalid array field: {name}")
                result[name] = [kind[0].from_variant(item.get_child_value(i)) for i in range(item.n_children())]
            else:
                expected = "s" if kind in (dict, list) else kind
                if item.get_type_string() != expected:
                    raise OksaError("InvalidResult", f"Invalid wire type for {name}")
                if kind in (dict, list):
                    try:
                        result[name] = json.loads(item.get_string())
                    except (ValueError, RecursionError) as error:
                        raise OksaError("InvalidResult", f"Invalid JSON field: {name}") from error
                else:
                    result[name] = item.unpack()
        return result


class Contract:
    BUS = "io.github.puu_os.Oksa"
    PATH = "/io/github/puu_os/Oksa"
    JOB = "io.github.puu_os.Oksa.Job"
    MAX_JSON = 1024 * 1024
    METHODS = {
        "GetStatus": ((), "a{sv}", False),
        "GetDaemonState": ((), "a{sv}", False),
        "GetReceipt": ((('request_id', 's'),), "a{sv}", False),
        "RecordUnconfirmed": ((('request_id', 's'), ('operation', 's'), ('target', 's'), ('detail', 's')), "a{sv}", False),
        "ReconcileUnresolved": ((('request_id', 's'),), "a{sv}", False),
        "ListModels": ((), "a{sv}", False),
        "SearchModels": ((('query', 's'),), "a{sv}", False),
        "InspectModel": ((('repo', 's'), ('revision', 's')), "a{sv}", False),
        "DownloadModel": (
            (('request_id', 's'), ('repo', 's'), ('name', 's'), ('commit', 's'), ('force', 'b'),
             ('replace_existing', 'b'), ('expected_repo', 's'), ('expected_revision', 's')),
            "o", True,
        ),
        "StartModel": ((('request_id', 's'), ('name', 's'), ('expected_selected', 's'), ('expected_active', 's')), "o", True),
        "RestartModel": ((('request_id', 's'), ('name', 's'), ('expected_selected', 's'), ('expected_active', 's')), "o", True),
        "StopServing": ((('request_id', 's'), ('expected_selected', 's'), ('expected_active', 's')), "o", True),
        "RemoveModel": ((('request_id', 's'), ('name', 's'), ('expected_revision', 's'), ('expected_selected', 's'), ('expected_active', 's')), "o", True),
        "ListUsers": ((), "a{sv}", False),
        "GetUser": ((('id', 's'),), "a{sv}", False),
        "CreateUser": ((('request_id', 's'), ('id', 's'), ('email', 's'), ('budget', 'd')), "a{sv}", False),
        "DeleteUser": ((('request_id', 's'), ('id', 's')), "o", True),
        "ListKeys": ((('user', 's'),), "a{sv}", False),
        "CreateKey": ((('request_id', 's'), ('user', 's'), ('alias', 's'), ('models', 'as'), ('budget', 'd'), ('duration', 's')), "a{sv}", False),
        "RevokeKey": ((('request_id', 's'), ('user', 's'), ('key_id', 's')), "a{sv}", False),
        "GetCalibration": ((('name', 's'),), "a{sv}", False),
        "Calibrate": ((('request_id', 's'), ('name', 's'), ('force', 'b'), ('expected_selected', 's'), ('expected_active', 's')), "o", True),
    }
    OPTIONAL_STRINGS = {
        "email", "duration", "user", "commit", "revision", "query",
        "expected_repo", "expected_revision", "expected_selected", "expected_active", "target", "detail",
    }
    MODEL_MUTATIONS = frozenset({"DownloadModel", "StartModel", "RestartModel", "StopServing", "RemoveModel", "Calibrate"})
    USER_MUTATIONS = frozenset({"CreateUser", "DeleteUser", "CreateKey", "RevokeKey"})
    MUTATIONS = MODEL_MUTATIONS | USER_MUTATIONS
    ERROR = RecordSchema({"code": "s", "message": "s"})
    MEMORY = RecordSchema({"total_mib": "x", "available_mib": "x", "used_mib": "x", "used_pct": "d"},
                          frozenset({"total_mib", "available_mib", "used_mib", "used_pct"}))
    GPU = RecordSchema({"index": "x", "name": "s", "vendor": "s", "memory_used_mib": "d",
                        "memory_total_mib": "d", "gpu_util_pct": "d", "temperature_c": "d", "power_w": "d"},
                       frozenset({"index", "name", "memory_used_mib", "memory_total_mib", "gpu_util_pct", "temperature_c", "power_w"}))
    WORKLOAD = RecordSchema({"name": "s", "phase": "s", "ready": "b", "restarts": "x", "start_time": "s"},
                            frozenset({"ready", "restarts"}))
    METRICS = RecordSchema({"running_requests": "x", "waiting_requests": "x", "swapped_requests": "x", "kv_cache_usage_pct": "d"},
                           frozenset({"running_requests", "waiting_requests", "swapped_requests", "kv_cache_usage_pct"}))
    PREPARATION = RecordSchema(
        {"model": "s", "state": "s", "message": "s", "updated_at": "d"},
        frozenset({"message", "updated_at"}),
    )
    STATUS = RecordSchema(
        {
            "host": "s", "api_base_url": "s", "auth_enforcement": "s",
            "serving_enabled": "b", "selected_model": "s", "active_model": "s",
            "selected_model_id": "s", "active_model_id": "s", "host_memory": MEMORY,
            "gpus": (GPU,), "workloads": (WORKLOAD,), "metrics": METRICS,
            "model_status": "s", "gateway_status": "s", "preparation": PREPARATION,
        },
        frozenset({"host", "api_base_url", "auth_enforcement", "selected_model_id", "active_model_id", "gpus", "workloads", "preparation"}),
    )
    FRESHNESS = RecordSchema({"sampled_at": "s", "age_seconds": "d", "stale": "b", "error": "s", "max_age_seconds": "d"},
                            frozenset({"sampled_at", "age_seconds", "error"}))
    MODEL = RecordSchema(
        {
            "name": "s", "id": "s", "repo_id": "s", "revision": "s", "provenance": "s", "path": "s",
            "storage_mib": "x", "quantization": "s", "capabilities": "s", "calibration": "s",
            "active": "b", "selected": "b",
        },
        frozenset({"revision", "provenance", "path"}),
    )
    SEARCH_MODEL = RecordSchema({"repo_id": "s", "name": "s", "author": "s", "downloads": "x", "gated": "b"})
    MODEL_FILE = RecordSchema({"path": "s", "size": "x", "sha256": "s", "blob_id": "s"},
                              frozenset({"sha256", "blob_id"}))
    MODEL_METADATA = RecordSchema({"repo_id": "s", "revision": "s", "resolved_revision": "s", "model_name": "s",
                                   "storage_mib": "x", "estimated_memory_mib": "x", "max_context": "x",
                                   "quantization": "s", "architectures": list, "torch_dtype": "s", "gated": "b",
                                   "files": (MODEL_FILE,)})
    MESSAGE = RecordSchema({"ok": "b", "message": "s"})
    CALIBRATION_LIMITS = RecordSchema({"max_model_len": "x", "max_num_seqs": "x", "max_num_batched_tokens": "x",
                                      "gpu_memory_utilization": "d", "memory_bytes": "x"})
    CALIBRATION_RESULT = RecordSchema({"schema": "x", "model": "s", "state": "s", "fingerprint": "s",
                                      "identity": dict, "completed_at": "d", "reason": "s", "limits": CALIBRATION_LIMITS,
                                      "trials": list, "search_complete": "b", "incomplete_reasons": list, "metrics": dict},
                                     frozenset({"metrics"}))
    CALIBRATION_STATUS = RecordSchema({"schema": "x", "model": "s", "boot_id": "s", "state": "s",
                                      "message": "s", "fingerprint": "s", "updated_at": "d"})
    CALIBRATION = RecordSchema({"ok": "b", "model": "s", "result": CALIBRATION_RESULT, "status": CALIBRATION_STATUS},
                              frozenset({"result", "status"}))
    KEY_RECORD = RecordSchema(
        {
            "key_id": "s", "user_id": "s", "key_alias": "s", "key_prefix": "s",
            "models": list, "expires": "s", "created_at": "s",
        },
        frozenset({"expires", "created_at"}),
    )
    RECEIPT = RecordSchema(
        {
            "request_id": "s", "operation": "s", "scope": "s", "target": "s", "state": "s",
            "created_at": "d", "updated_at": "d", "job_path": "s", "summary": "s",
            "error_code": "s", "error_message": "s",
        },
        frozenset({"job_path", "summary", "error_code", "error_message"}),
    )
    DAEMON_STATE = RecordSchema({"ok": "b", "namespace": "s", "receipts": (RECEIPT,), "unresolved": (RECEIPT,)})
    RECEIPT_RESPONSE = RecordSchema({"ok": "b", "found": "b", "namespace": "s", "receipt": RECEIPT}, frozenset({"receipt"}))
    RESPONSES = {
        "GetStatus": RecordSchema({"ok": "b", "status": STATUS, "snapshot": FRESHNESS}, frozenset({"status"})),
        "GetDaemonState": DAEMON_STATE,
        "GetReceipt": RECEIPT_RESPONSE,
        "RecordUnconfirmed": RECEIPT_RESPONSE,
        "ReconcileUnresolved": RECEIPT_RESPONSE,
        "ListModels": RecordSchema(
            {
                "ok": "b", "active_model": "s", "selected_model": "s",
                "active_model_id": "s", "selected_model_id": "s",
                "serving_enabled": "b", "models": (MODEL,),
            },
            frozenset({"active_model_id", "selected_model_id"}),
        ),
        "SearchModels": RecordSchema({"ok": "b", "models": (SEARCH_MODEL,)}),
        "InspectModel": RecordSchema({"ok": "b", "metadata": MODEL_METADATA, "fits": "b", "reason": "s"}),
        "DownloadModel": MESSAGE,
        "StartModel": MESSAGE,
        "RestartModel": MESSAGE,
        "StopServing": MESSAGE,
        "RemoveModel": RecordSchema({"ok": "b", "deleted": "b", "reconciled": "b", "outcome": "s",
                                    "message": "s", "warning": "s", "error": "s"}, frozenset({"message", "warning", "error"})),
        "ListUsers": RecordSchema({"ok": "b", "users": list}),
        "GetUser": RecordSchema({"ok": "b", "user": dict}),
        "CreateUser": RecordSchema({"ok": "b", "result": dict}),
        "DeleteUser": RecordSchema({"ok": "b", "outcome": "s", "deleted": "b", "confirmed_revoked_keys": "x",
                                   "message": "s", "error": "s", "warning": "s"}, frozenset({"deleted", "message", "error", "warning"})),
        "ListKeys": RecordSchema(
            {"ok": "b", "keys": (KEY_RECORD,), "complete": "b", "sampled_at": "s", "protected_users": list},
            frozenset({"complete", "sampled_at", "protected_users"}),
        ),
        "CreateKey": RecordSchema({"ok": "b", "key": dict}),
        "RevokeKey": MESSAGE,
        "GetCalibration": CALIBRATION,
        "Calibrate": CALIBRATION,
    }

    @classmethod
    def validate_job_path(cls, path: str) -> None:
        cls.text(path, "path")
        if not re.fullmatch(re.escape(cls.PATH) + r"/jobs/[0-9a-f]{32}", path):
            raise OksaError("InvalidInput", "Invalid job path")

    @classmethod
    def response_to_variant(cls, method: str, data: dict) -> dict[str, GLib.Variant]:
        cls.encode(data)
        if method not in cls.RESPONSES:
            raise OksaError("InvalidResult", "Unknown response schema")
        values = cls.RESPONSES[method].to_variant(data)
        if GLib.Variant("a{sv}", values).get_size() > cls.MAX_JSON:
            raise OksaError("InvalidResult", "Wire response exceeds 1 MiB")
        return values

    @classmethod
    def response_from_variant(cls, method: str, data: GLib.Variant) -> dict:
        if method not in cls.RESPONSES or data.get_size() > cls.MAX_JSON:
            raise OksaError("InvalidResult", "Invalid response schema or size")
        result = cls.RESPONSES[method].from_variant(data)
        cls.validate_response(method, result)
        return result

    @classmethod
    def validate_response(cls, method: str, data: dict) -> None:
        cls.response_to_variant(method, data)

    @classmethod
    def snapshot_schema(cls, operation: str) -> RecordSchema:
        if not isinstance(operation, str) or operation not in cls.RESPONSES or not cls.METHODS[operation][2]:
            raise OksaError("InvalidResult", "Invalid job operation")
        return RecordSchema(
            {
                "ok": "b", "path": "o", "operation": "s", "state": "s", "progress": "s",
                "started_at": "d", "ended_at": "d", "result": cls.RESPONSES[operation],
                "error": cls.ERROR, "repo_id": "s", "model_name": "s",
                "commit": "s", "user_id": "s", "request_id": "s",
            },
            frozenset({"ended_at", "result", "error", "repo_id", "model_name", "commit", "user_id", "request_id"}),
        )
    @classmethod
    def validate_snapshot(cls, data: dict) -> None:
        cls.encode(data)
        values = cls.snapshot_schema(data.get("operation")).to_variant(data)
        if GLib.Variant("a{sv}", values).get_size() > cls.MAX_JSON:
            raise OksaError("InvalidResult", "Wire snapshot exceeds 1 MiB")
        cls.validate_job_path(data["path"])
        if data["state"] not in ("running", "succeeded", "failed", "canceled"):
            raise OksaError("InvalidResult", "Invalid job state")
        if data["state"] == "succeeded" and data.get("result") is None:
            raise OksaError("InvalidResult", "Successful job has no result")
        if data["state"] in ("failed", "canceled") and data.get("error") is None:
            raise OksaError("InvalidResult", "Failed job has no error")

    @classmethod
    def snapshot_to_variant(cls, data: dict) -> dict[str, GLib.Variant]:
        cls.validate_snapshot(data)
        return cls.snapshot_schema(data["operation"]).to_variant(data)

    @classmethod
    def snapshot_from_variant(cls, data: GLib.Variant) -> dict:
        if data.get_type_string() != "a{sv}" or data.get_size() > cls.MAX_JSON:
            raise OksaError("InvalidResult", "Invalid job snapshot")
        operation = data.lookup_value("operation", GLib.VariantType.new("s"))
        if operation is None:
            raise OksaError("InvalidResult", "Missing job operation")
        result = cls.snapshot_schema(operation.get_string()).from_variant(data)
        cls.validate_snapshot(result)
        return result

    @classmethod
    def validate(cls, method: str, args: tuple) -> None:
        if method not in cls.METHODS:
            raise OksaError("UnknownMethod", "Unknown domain method")
        fields = cls.METHODS[method][0]
        if len(fields) != len(args):
            raise OksaError("InvalidInput", "Incorrect argument count")
        for (name, kind), value in zip(fields, args):
            if kind in ("s", "o"):
                cls.text(value, name, optional=name in cls.OPTIONAL_STRINGS or (method == "DownloadModel" and name == "name"))
                if method == "CreateKey" and name == "user" and not value.strip():
                    raise OksaError("InvalidInput", "User is required")
            elif kind == "b":
                if type(value) is not bool:
                    raise OksaError("InvalidInput", f"{name} must be Boolean")
            elif kind == "d":
                if type(value) not in (float, int) or not math.isfinite(value) or not (value == -1 or 0 <= value <= 1e12):
                    raise OksaError("InvalidInput", "Budget must be finite and nonnegative, or -1 for unset")
            elif kind == "as":
                if not isinstance(value, (tuple, list)) or len(value) > 128:
                    raise OksaError("InvalidInput", "At most 128 models are allowed")
                for model in value:
                    cls.text(model, "model")
            if name == "request_id" and not re.fullmatch(r"[0-9a-f]{32}:[0-9a-f]{32}", value):
                raise OksaError("InvalidInput", "Invalid request ID")
            if method == "RecordUnconfirmed" and name == "operation" and value not in cls.MUTATIONS:
                raise OksaError("InvalidInput", "Invalid mutation operation")
            if name == "repo" and not re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,95}/)?[A-Za-z0-9][A-Za-z0-9._-]{0,95}", value):
                raise OksaError("InvalidInput", "Invalid repository ID")
            if name == "commit" and not re.fullmatch(r"[0-9a-fA-F]{40}", value):
                raise OksaError("InvalidInput", "Download requires a pinned 40-hex commit")
            if name == "duration" and value and not re.fullmatch(r"[1-9][0-9]{0,8}[smhd]", value):
                raise OksaError("InvalidInput", "Invalid duration")

    @classmethod
    def args_digest(cls, method: str, args: tuple) -> str:
        payload = list(args[1:] if method in cls.MUTATIONS else args)
        encoded = json.dumps({"method": method, "args": payload}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def mutation_target(cls, method: str, args: tuple, state_dir: Path | None = None) -> str:
        if method == "DownloadModel":
            _, repo, name, *_ = args
            return ms.normalize_storage_id(repo, name or None) or name or repo
        if method in ("StartModel", "RestartModel", "RemoveModel", "Calibrate"):
            raw = args[1]
            try:
                return ms.resolve_model_name(raw, state_dir or ms.STATE_DIR)
            except Exception:
                return raw
        if method == "StopServing":
            return "runtime"
        if method in ("CreateUser", "DeleteUser", "CreateKey"):
            return args[1]
        if method == "RevokeKey":
            return f"{args[1]}:{args[2]}"
        return ""
    @staticmethod
    def text(value: str, name: str, optional: bool = False) -> None:
        limit = 4096 if name == "token" else 256
        if not isinstance(value, str) or len(value) > limit or (not optional and not value.strip()):
            raise OksaError("InvalidInput", f"Invalid {name} length")
        if any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in value):
            raise OksaError("InvalidInput", f"Control characters are forbidden in {name}")

    @classmethod
    def encode(cls, value: dict) -> str:
        if not isinstance(value, dict):
            raise OksaError("InvalidResult", "Expected a domain object")
        try:
            cls.validate_json(value)
            encoded = json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"))
        except (TypeError, ValueError, RecursionError) as error:
            raise OksaError("InvalidResult", "Domain result is not finite JSON") from error
        if len(encoded) > cls.MAX_JSON:
            raise OksaError("InvalidResult", "Domain result exceeds 1 MiB")
        return encoded

    @classmethod
    def decode(cls, value: str) -> dict:
        if len(value) > cls.MAX_JSON:
            raise OksaError("InvalidResult", "Domain result exceeds 1 MiB")
        try:
            result = json.loads(value)
            cls.encode(result)
        except (ValueError, RecursionError) as error:
            raise OksaError("InvalidResult", "Invalid JSON object") from error
        return result

    @classmethod
    def validate_json(cls, value: object) -> None:
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise OksaError("InvalidResult", "JSON object keys must be strings")
                cls.validate_json(item)
        elif type(value) is list:
            for item in value:
                cls.validate_json(item)
        elif value is not None and type(value) not in (str, bool, int, float):
            raise OksaError("InvalidResult", "Unsupported JSON value type")


class OksaControl:
    def __init__(self, state_dir: Path | None = None) -> None:
        self.state_dir = Path(state_dir or os.environ.get("PUU_VLLM_STATE_DIR", "/var/lib/puu/vllm"))
        self.discover_bin = ms.resolve_helper("oksa-discover-vllm-models")
        self.labeler_bin = ms.resolve_helper("oksa-label-model-serving-node")
        self.planner_bin = ms.resolve_helper("oksa-plan-vllm-workloads")
        self.preseed_bin = ms.resolve_helper("oksa-preseed-vllm-models")
        self.progress = lambda text: None

    def execute(self, method: str, args: tuple, progress) -> dict:
        Contract.validate(method, args)
        operations = {
            "ListModels": self.list_models,
            "SearchModels": self.search_models,
            "InspectModel": self.inspect_model, "DownloadModel": self.download_model,
            "StartModel": self.start_model, "RestartModel": self.restart_model,
            "StopServing": self.stop_serving, "RemoveModel": self.remove_model,
            "ListUsers": self.list_users,
            "GetUser": self.get_user, "CreateUser": self.create_user,
            "DeleteUser": self.delete_user, "ListKeys": self.list_keys,
            "CreateKey": self.create_key, "RevokeKey": self.revoke_key,
            "GetCalibration": self.get_calibration, "Calibrate": self.calibrate,
        }
        self.progress = progress
        call_args = args[1:] if method in Contract.MUTATIONS else args
        return operations[method](*call_args)
    def list_models(self) -> dict:
        return ms.collect_models_list(self.state_dir, self.discover_bin, self.planner_bin)

    def search_models(self, query: str) -> dict:
        deadline = time.monotonic() + 12
        models = self._search_hf_models(query.strip(), deadline)
        catalog_dir = Path(os.environ.get("PUU_VLLM_MODEL_CATALOG_DIR", "/usr/share/puu/vllm/catalog.d"))
        terms = re.findall(r"[^\W_]+", query.casefold())
        preferred = []
        lookups = 0
        seen = set()
        for path in sorted(catalog_dir.glob("*.env")):
            if time.monotonic() >= deadline:
                break
            try:
                meta = ms.ModelSpec.read_metadata(path)
            except (OSError, ValueError):
                continue
            repo = meta.get("PUU_VLLM_REPO_ID", "")
            searchable = " ".join((path.stem, meta.get("PUU_VLLM_MODEL_NAME", ""), repo)).casefold()
            if query.strip() and (not terms or not all(term in searchable for term in terms)):
                continue
            try:
                Contract.validate("InspectModel", (repo, ""))
            except OksaError:
                continue
            if repo in seen:
                continue
            seen.add(repo)
            match = next((model for model in models if model["repo_id"] == repo), None)
            if match is None and lookups < 3:
                lookups += 1
                try:
                    matches = self._search_hf_models(repo, deadline)
                except OksaError as error:
                    if error.code != "SearchFailed":
                        raise
                    continue
                match = next((model for model in matches if model["repo_id"] == repo), None)
            if match is not None:
                preferred.append(match)
                if len(preferred) == 3:
                    break
        combined = []
        seen = set()
        for model in preferred + models:
            if model["repo_id"] not in seen:
                combined.append(model)
                seen.add(model["repo_id"])
        return {"ok": True, "models": combined[:20]}

    def _search_hf_models(self, query: str, deadline: float) -> list[dict]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OksaError("SearchFailed", "Hugging Face model search timed out")
        params = urllib.parse.urlencode({
            "search": query, "filter": "text-generation", "pipeline_tag": "text-generation",
            "sort": "downloads", "direction": "-1", "limit": "20",
            "expand": ["downloads", "gated", "pipeline_tag"],
        }, doseq=True)
        request = urllib.request.Request("https://huggingface.co/api/models?" + params,
                                         headers={"User-Agent": "puu-oksa/1.0", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=min(3.0, remaining)) as response:
                raw = bytearray()
                while len(raw) <= Contract.MAX_JSON:
                    if time.monotonic() >= deadline:
                        raise OksaError("SearchFailed", "Hugging Face model search timed out")
                    chunk = response.read1(min(65536, Contract.MAX_JSON + 1 - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
        except urllib.error.HTTPError as error:
            raise OksaError("SearchFailed", f"Hugging Face search failed (HTTP {error.code})") from error
        except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
            raise OksaError("SearchFailed", "Could not reach Hugging Face model search; try again") from error
        if len(raw) > Contract.MAX_JSON:
            raise OksaError("InvalidResult", "Hugging Face search response exceeds 1 MiB")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError) as error:
            raise OksaError("InvalidResult", "Invalid Hugging Face search response") from error
        if not isinstance(data, list) or len(data) > 20:
            raise OksaError("InvalidResult", "Expected at most 20 Hugging Face search results")
        models = []
        for item in data:
            if not isinstance(item, dict):
                raise OksaError("InvalidResult", "Invalid Hugging Face search result")
            repo = item.get("id")
            if (not isinstance(repo, str)
                    or not re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,95}/)?[A-Za-z0-9][A-Za-z0-9._-]{0,95}", repo)
                    or ".." in repo or "--" in repo
                    or any(part.endswith((".git", ".")) for part in repo.split("/"))):
                raise OksaError("InvalidResult", "Invalid repository ID in Hugging Face search result")
            downloads = item.get("downloads")
            gated = item.get("gated")
            if (type(downloads) is not int or not 0 <= downloads <= 2**63 - 1
                    or not (type(gated) is bool or (isinstance(gated, str) and gated in ("auto", "manual")))
                    or item.get("pipeline_tag") != "text-generation"):
                raise OksaError("InvalidResult", "Invalid metadata in Hugging Face search result")
            models.append({"repo_id": repo, "name": repo.rsplit("/", 1)[-1],
                           "author": repo.split("/", 1)[0] if "/" in repo else "",
                           "downloads": downloads, "gated": gated is not False})
        return models

    def inspect_model(self, repo: str, revision: str) -> dict:
        meta = ms.inspect_hf_model(repo, revision or "main")
        if not meta:
            raise OksaError("NotFound", f"Failed to fetch metadata for {repo}")
        fits, reason = ms.check_model_capacity(meta.get("estimated_memory_mib", 0))
        return {"ok": True, "metadata": meta, "fits": fits, "reason": reason}

    def run_helper(self, command: list[str], env: dict | None = None) -> None:
        last_line = ""
        with subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as process:
            for line in iter(lambda: process.stdout.readline(4096), ""):
                text = line.strip()
                if text:
                    last_line = text
                self.progress(text)
            code = process.wait()
        if code:
            detail = f": {last_line}" if last_line else ""
            raise OksaError("OperationFailed", f"{Path(command[0]).name} failed (exit {code}){detail}")

    def _current_serving_ids(self) -> tuple[str, str]:
        selected_file, disabled_file = ms.control_files(self.state_dir)
        active_file = Path(os.environ.get("PUU_VLLM_ACTIVE_MODEL_FILE", str(self.state_dir / "active-model")))
        serving_enabled = not disabled_file.exists()
        selected = ms.read_first_line(selected_file) if serving_enabled else ""
        if selected:
            try:
                selected = ms.resolve_model_name(selected, self.state_dir)
            except ValueError:
                pass
        active = ms.read_first_line(active_file)
        if active:
            try:
                active = ms.resolve_model_name(active, self.state_dir)
            except ValueError:
                pass
        return selected or "-", active or "-"

    def _verify_serving_preconditions(self, expected_selected: str, expected_active: str) -> None:
        cur_selected, cur_active = self._current_serving_ids()
        if expected_selected and cur_selected != expected_selected:
            raise OksaError("StateChanged", "State changed; review again")
        if expected_active and cur_active != expected_active:
            raise OksaError("StateChanged", "State changed; review again")

    def download_model(
        self,
        repo: str,
        name: str,
        commit: str,
        force: bool,
        replace_existing: bool = False,
        expected_repo: str = "",
        expected_revision: str = "",
    ) -> dict:
        effective_name = ms.normalize_storage_id(repo, name or None)
        if not effective_name:
            raise OksaError("InvalidInput", "Invalid model name")
        ms.MODELS_DIR.mkdir(parents=True, exist_ok=True)
        lock_path = ms.MODELS_DIR.parent / f".{effective_name}.preseed.lock"
        with lock_path.open("a") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OksaError("Busy", f"Model {effective_name} is busy; wait for current work to finish") from exc
            with ms.preseed_publish_lock(ms.MODELS_DIR, exclusive=False):
                installed = ms.inspect_installed_model(effective_name, self.state_dir, ms.MODELS_DIR)
                if installed is None:
                    if replace_existing or expected_repo or expected_revision:
                        raise OksaError("StateChanged", "State changed; review again")
                else:
                    if installed["provenance"] == "unknown":
                        raise OksaError(
                            "InvalidState",
                            f"Existing installation {effective_name} has unknown provenance; inspect storage before replacing",
                        )
                    if not replace_existing:
                        raise OksaError("StateChanged", "State changed; review again")
                    if installed["repo_id"] != expected_repo or installed["revision"] != expected_revision:
                        raise OksaError("StateChanged", "State changed; review again")
            command = [str(self.preseed_bin), "--repo-id", repo, "--name", effective_name, "--revision", commit]
            if force:
                command.append("--force")
            env = dict(os.environ, PUU_PRESEED_LOCK_HELD=f"1:{effective_name}")
            self.run_helper(command, env)
        self.run_helper([str(self.discover_bin)])
        self.run_helper([str(self.labeler_bin)])
        return {"ok": True, "message": "Model download completed successfully."}

    def start_model(self, name: str, expected_selected: str = "", expected_active: str = "") -> dict:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "planner.lock").open("a") as planner_lock:
            fcntl.flock(planner_lock, fcntl.LOCK_EX)
            with ms.preseed_publish_lock(ms.MODELS_DIR, exclusive=False):
                try:
                    model_name = ms.resolve_model_name(name, self.state_dir)
                except ValueError as exc:
                    raise OksaError("NotFound", str(exc)) from exc
                self._verify_serving_preconditions(expected_selected, expected_active)
                if not ms.check_capabilities(self.planner_bin, model_name):
                    raise OksaError("InvalidInput", "Model has insufficient host capabilities")
                selected, disabled = ms.control_files(self.state_dir)
                ms.atomic_write_text(selected, f"{model_name}\n")
                disabled.unlink(missing_ok=True)
            env = dict(
                os.environ,
                PUU_VLLM_SELECTED_MODEL=model_name,
                PUU_VLLM_REQUIRE_SELECTED_MODEL="true",
                PUU_PLANNER_LOCK_HELD="1",
            )
            self.run_helper([str(self.planner_bin), "--require-reconcile"], env)
        return {"ok": True, "message": f"Model {ms.public_model_name(model_name, self.state_dir)} activated"}

    def restart_model(self, name: str, expected_selected: str = "", expected_active: str = "") -> dict:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "planner.lock").open("a") as planner_lock:
            fcntl.flock(planner_lock, fcntl.LOCK_EX)
            with ms.preseed_publish_lock(ms.MODELS_DIR, exclusive=False):
                try:
                    model_name = ms.resolve_model_name(name, self.state_dir)
                except ValueError as exc:
                    raise OksaError("NotFound", str(exc)) from exc
                self._verify_serving_preconditions(expected_selected, expected_active)
                if not ms.check_capabilities(self.planner_bin, model_name):
                    raise OksaError("InvalidInput", "Model has insufficient host capabilities")
                selected, disabled = ms.control_files(self.state_dir)
                ms.atomic_write_text(selected, f"{model_name}\n")
                disabled.unlink(missing_ok=True)
            env = dict(
                os.environ,
                PUU_VLLM_SELECTED_MODEL=model_name,
                PUU_VLLM_REQUIRE_SELECTED_MODEL="true",
                PUU_PLANNER_LOCK_HELD="1",
            )
            self.run_helper([str(self.planner_bin), "--require-reconcile", "--restart"], env)
        return {"ok": True, "message": f"Model {ms.public_model_name(model_name, self.state_dir)} restarted"}

    def stop_serving(self, expected_selected: str = "", expected_active: str = "") -> dict:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "planner.lock").open("a") as planner_lock:
            fcntl.flock(planner_lock, fcntl.LOCK_EX)
            self._verify_serving_preconditions(expected_selected, expected_active)
            selected, disabled = ms.control_files(self.state_dir)
            selected.unlink(missing_ok=True)
            ms.atomic_write_text(disabled, "")
            env = dict(os.environ, PUU_PLANNER_LOCK_HELD="1")
            self.run_helper([str(self.planner_bin), "--require-reconcile"], env)
        return {"ok": True, "message": "Model serving stopped"}

    def remove_model(
        self,
        name: str,
        expected_revision: str = "",
        expected_selected: str = "",
        expected_active: str = "",
    ) -> dict:
        try:
            resolved = ms.resolve_model_name(name, self.state_dir)
        except ValueError:
            resolved = name

        def _prevalidate(target_name: str) -> None:
            installed = ms.inspect_installed_model(target_name, self.state_dir, ms.MODELS_DIR)
            if installed is None:
                raise OksaError("NotFound", f"Unknown model: {name}")
            if expected_revision and (installed.get("revision") or "-") != expected_revision:
                raise OksaError("StateChanged", "State changed; review again")
            self._verify_serving_preconditions(expected_selected, expected_active)

        result = removal.ModelRemovalService(self.state_dir, self.discover_bin, self.planner_bin).remove(
            resolved, prevalidate=_prevalidate
        )
        data = {
            "ok": result.deleted and result.reconciled,
            "deleted": result.deleted,
            "reconciled": result.reconciled,
            "outcome": result.outcome.name.lower(),
        }
        if result.reconciled:
            data["message"] = result.message
        elif result.deleted:
            data["warning"] = result.message
            data["error"] = result.message
        else:
            data["error"] = result.message
        return data

    @staticmethod
    def response(response: tuple, key: str) -> dict:
        ok, result = response
        if not ok:
            code = "Unconfirmed" if isinstance(result, ms.UnconfirmedMessage) else "OperationFailed"
            raise OksaError(code, str(result))
        return {"ok": True, key: result}

    def list_users(self) -> dict:
        return self.response(ms.litellm_user_list(), "users")

    def get_user(self, user: str) -> dict:
        return self.response(ms.litellm_user_info(user), "user")

    def create_user(self, user: str, email: str, budget: float) -> dict:
        return self.response(ms.litellm_user_create(user, email=email or None, max_budget=None if budget == -1 else budget), "result")

    def delete_user(self, user: str) -> dict:
        result = user_deletion.UserDeletionService().delete(user)
        data = {"ok": result.deleted is True, "outcome": result.outcome.name.lower(), "deleted": result.deleted,
                "confirmed_revoked_keys": result.confirmed_revoked_keys,
                "message": result.message if result.deleted is True else None,
                "error": result.message if result.deleted is not True else None}
        if result.uncertain:
            data["warning"] = result.message
        return data

    def list_keys(self, user: str) -> dict:
        ok, raw_keys = ms.litellm_key_list(user_id=user or None, include_service=True)
        if not ok:
            code = "Unconfirmed" if isinstance(raw_keys, ms.UnconfirmedMessage) else "OperationFailed"
            raise OksaError(code, str(raw_keys))
        protected_users = sorted({
            str(k.get("user_id") or "")
            for k in raw_keys
            if isinstance(k, dict) and k.get("key_alias") == "open-webui" and k.get("user_id")
        })
        sanitized = [
            ms.sanitize_key_record(k)
            for k in raw_keys
            if isinstance(k, dict) and k.get("key_alias") != "open-webui"
        ]
        sampled_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return {
            "ok": True,
            "keys": sanitized,
            "complete": True,
            "sampled_at": sampled_at,
            "protected_users": protected_users,
        }

    def create_key(self, user: str, alias: str, models: list[str], budget: float, duration: str) -> dict:
        if not user:
            raise OksaError("InvalidInput", "User is required")
        ok, result = ms.litellm_key_create(
            user, alias=alias, models=models or None,
            max_budget=None if budget == -1 else budget, duration=duration or None,
        )
        if not ok:
            code = "Unconfirmed" if isinstance(result, ms.UnconfirmedMessage) else "OperationFailed"
            raise OksaError(code, str(result))
        key_data = dict(result)
        raw_token = str(key_data.pop("token", "") or "")
        if raw_token:
            key_data["key_id"] = ms.key_ref(raw_token)
        key_data["key_prefix"] = ms.mask_key_prefix(str(key_data.get("key_name") or key_data.get("key") or ""))
        return {"ok": True, "key": key_data}

    def revoke_key(self, user: str, key_id: str) -> dict:
        return self.response(ms.litellm_key_delete(key_id, user_id=user), "message")

    def get_calibration(self, name: str) -> dict:
        name = ms.resolve_model_name(name, self.state_dir)
        try:
            result = calibration.read_record(calibration.result_path(name))
        except FileNotFoundError:
            result = None
        if result is not None and not calibration.valid_result(result, model=name):
            raise OksaError("OperationFailed", f"Invalid calibration result for {name}")
        try:
            status = calibration.read_record(calibration.status_path(name))
        except FileNotFoundError:
            status = None
        if status is not None:
            if status.get("model") != name:
                raise OksaError("OperationFailed", f"Invalid calibration status for {name}")
            if status.get("boot_id") != calibration.boot_id():
                status = None
        return {"ok": True, "model": name, "result": result, "status": status}

    def calibrate(self, name: str, force: bool, expected_selected: str = "", expected_active: str = "") -> dict:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "planner.lock").open("a") as planner_lock:
            fcntl.flock(planner_lock, fcntl.LOCK_EX)
            try:
                name = ms.resolve_model_name(name, self.state_dir)
            except ValueError as exc:
                raise OksaError("NotFound", str(exc)) from exc
            self._verify_serving_preconditions(expected_selected, expected_active)
            command = [str(ms.resolve_helper("oksa-calibrate")), "--model", name]
            if force:
                command.append("--force")
            self.run_helper(command)
            self.run_helper([str(self.planner_bin), "--require-reconcile"], dict(os.environ, PUU_PLANNER_LOCK_HELD="1"))
        return self.get_calibration(name)

    def reconcile_scope(self, scope: str, target: str) -> dict:
        if scope == "model":
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with (self.state_dir / "planner.lock").open("a") as planner_lock:
                fcntl.flock(planner_lock, fcntl.LOCK_EX)
                with ms.preseed_publish_lock(ms.MODELS_DIR, exclusive=False):
                    if target and target != "runtime" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", target):
                        backup = ms.MODELS_DIR.parent / f".{target}.previous"
                        if backup.exists():
                            raise OksaError(
                                "ReconciliationFailed",
                                f"Unpublished backup directory {backup} still exists; inspect model storage before clearing guard",
                            )
                        dest = ms.MODELS_DIR / target
                        if dest.exists() and not (dest / "config.json").is_file():
                            raise OksaError(
                                "ReconciliationFailed",
                                f"Model directory {dest} is incomplete (missing config.json); repair or remove it before clearing guard",
                            )
                self.run_helper([str(self.discover_bin)])
                env = dict(os.environ, PUU_PLANNER_LOCK_HELD="1")
                self.run_helper([str(self.planner_bin), "--require-reconcile"], env)
            return {"ok": True, "message": f"Model scope reconciled ({target or 'runtime'})"}
        if scope == "user":
            ok_u, users = ms.litellm_user_list()
            if not ok_u:
                raise OksaError("ReconciliationFailed", f"Gateway user list unavailable: {users}")
            ok_k, keys = ms.litellm_key_list(include_service=True)
            if not ok_k:
                raise OksaError("ReconciliationFailed", f"Gateway key index unavailable: {keys}")
            return {"ok": True, "message": f"API access scope reconciled ({target or 'gateway'})"}
        raise OksaError("InvalidInput", f"Unknown reconciliation scope: {scope}")
