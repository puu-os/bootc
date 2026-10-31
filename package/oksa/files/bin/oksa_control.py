# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

import json
import math
import os
from pathlib import Path
import re
import subprocess
import unicodedata
from gi.repository import GLib

import oksa_calibration_state as calibration
import oksa_model_removal as removal
import oksa_model_serving as ms
import oksa_settings as settings
import oksa_user_deletion as user_deletion


class OksaError(Exception):
    def __init__(self, code: str, message: str, result: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.result = result


class Contract:
    BUS = "org.puu.Oksa"
    PATH = "/org/puu/Oksa"
    JOB = "org.puu.Oksa.Job"
    MAX_JSON = 1024 * 1024
    METHODS = {
        "GetStatus": ((), "a{sv}", False),
        "ListModels": ((), "a{sv}", False),
        "InspectModel": ((('repo', 's'), ('revision', 's')), "a{sv}", False),
        "DownloadModel": ((('repo', 's'), ('name', 's'), ('commit', 's'), ('force', 'b')), "o", True),
        "StartModel": ((('name', 's'),), "o", True),
        "StopServing": ((), "o", True),
        "RemoveModel": ((('name', 's'),), "o", True),
        "ListUsers": ((), "a{sv}", False),
        "GetUser": ((('id', 's'),), "a{sv}", False),
        "CreateUser": ((('id', 's'), ('email', 's'), ('budget', 'd')), "a{sv}", False),
        "DeleteUser": ((('id', 's'),), "o", True),
        "ListKeys": ((('user', 's'),), "a{sv}", False),
        "CreateKey": ((('user', 's'), ('alias', 's'), ('models', 'as'), ('budget', 'd'), ('duration', 's')), "a{sv}", False),
        "RevokeKey": ((('token', 's'),), "a{sv}", False),
        "GetSettings": ((), "a{sv}", False),
        "SetContact": ((('contact', 's'),), "o", True),
        "GetCalibration": ((), "a{sv}", False),
        "Calibrate": ((('force', 'b'),), "o", True),
        "GetJob": ((('path', 'o'),), "a{sv}", False),
        "ListJobs": ((), "a{sv}", False),
    }
    OPTIONAL_STRINGS = {"email", "contact", "duration", "user", "commit", "revision"}
    MUTATIONS = frozenset({"DownloadModel", "StartModel", "StopServing", "RemoveModel", "CreateUser",
                           "DeleteUser", "CreateKey", "RevokeKey", "SetContact", "Calibrate"})

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
            if name == "repo" and not re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,95}/)?[A-Za-z0-9][A-Za-z0-9._-]{0,95}", value):
                raise OksaError("InvalidInput", "Invalid repository ID")
            if name == "commit" and not re.fullmatch(r"[0-9a-fA-F]{40}", value):
                raise OksaError("InvalidInput", "Download requires a pinned 40-hex commit")
            if name == "path" and not re.fullmatch(cls.PATH + r"/jobs/[0-9a-f]{32}", value):
                raise OksaError("InvalidInput", "Invalid job path")
            if name == "duration" and value and not re.fullmatch(r"[1-9][0-9]{0,8}[smhd]", value):
                raise OksaError("InvalidInput", "Invalid duration")

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
    def py_to_variant(cls, val: object) -> GLib.Variant:
        if val is None:
            return GLib.Variant("s", "")
        if isinstance(val, bool):
            return GLib.Variant("b", val)
        if isinstance(val, int):
            return GLib.Variant("x", val)
        if isinstance(val, float):
            return GLib.Variant("d", val)
        if isinstance(val, str):
            return GLib.Variant("s", val)
        if isinstance(val, (list, tuple)):
            return GLib.Variant("av", [GLib.Variant("v", cls.py_to_variant(x)) for x in val if x is not None])
        if isinstance(val, dict):
            return GLib.Variant("a{sv}", {str(k): GLib.Variant("v", cls.py_to_variant(v)) for k, v in val.items() if v is not None})
        return GLib.Variant("s", str(val))

    @classmethod
    def to_variant_dict(cls, d: dict) -> dict[str, GLib.Variant]:
        return {str(k): GLib.Variant("v", cls.py_to_variant(v)) for k, v in d.items() if v is not None}


class OksaControl:
    def __init__(self, state_dir: Path | None = None) -> None:
        self.state_dir = Path(state_dir or os.environ.get("PUU_VLLM_STATE_DIR", "/var/lib/puu/vllm"))
        self.discover_bin = ms.resolve_helper("oksa-discover-vllm-models")
        self.planner_bin = ms.resolve_helper("oksa-plan-vllm-workloads")
        self.preseed_bin = ms.resolve_helper("oksa-preseed-vllm-models")

    def execute(self, method: str, args: tuple, progress) -> dict:
        Contract.validate(method, args)
        operations = {
            "ListModels": self.list_models,
            "InspectModel": self.inspect_model, "DownloadModel": self.download_model,
            "StartModel": self.start_model, "StopServing": self.stop_serving,
            "RemoveModel": self.remove_model, "ListUsers": self.list_users,
            "GetUser": self.get_user, "CreateUser": self.create_user,
            "DeleteUser": self.delete_user, "ListKeys": self.list_keys,
            "CreateKey": self.create_key, "RevokeKey": self.revoke_key,
            "GetSettings": self.get_settings, "SetContact": self.set_contact,
            "GetCalibration": self.get_calibration, "Calibrate": self.calibrate,
        }
        self.progress = progress
        return operations[method](*args)

    def list_models(self) -> dict:
        return ms.collect_models_list(self.state_dir, self.discover_bin, self.planner_bin)

    def inspect_model(self, repo: str, revision: str) -> dict:
        meta = ms.inspect_hf_model(repo, revision)
        if not meta:
            raise OksaError("NotFound", f"Failed to fetch metadata for {repo}")
        fits, reason = ms.check_model_capacity(meta.get("estimated_memory_mib", 0))
        return {"ok": True, "metadata": meta, "fits": fits, "reason": reason}

    def run_helper(self, command: list[str], env: dict | None = None) -> None:
        with subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as process:
            for line in iter(lambda: process.stdout.readline(4096), ""):
                self.progress(line.strip())
            code = process.wait()
        if code:
            raise OksaError("OperationFailed", f"{Path(command[0]).name} exited with code {code}")

    def download_model(self, repo: str, name: str, commit: str, force: bool) -> dict:
        effective_name = ms.normalize_storage_id(repo, name or None)
        if not effective_name:
            raise OksaError("InvalidInput", "Invalid model name")
        if ms.is_calibration_model(repo) or ms.is_calibration_model(effective_name):
            raise OksaError("Protected", "The calibration model is installed automatically")
        command = [str(self.preseed_bin), "--repo-id", repo, "--name", effective_name, "--revision", commit]
        if force:
            command.append("--force")
        self.run_helper(command)
        self.run_helper([str(self.discover_bin)])
        return {"ok": True, "message": "Model download completed successfully."}

    def start_model(self, name: str) -> dict:
        model_name = ms.resolve_model_name(name, self.state_dir)
        if not ms.check_capabilities(self.planner_bin, model_name):
            raise OksaError("InvalidInput", "Model has insufficient host capabilities")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        selected, disabled = ms.control_files(self.state_dir)
        ms.atomic_write_text(selected, f"{model_name}\n")
        disabled.unlink(missing_ok=True)
        env = dict(os.environ, PUU_VLLM_SELECTED_MODEL=model_name, PUU_VLLM_REQUIRE_SELECTED_MODEL="true")
        self.run_helper([str(self.planner_bin), "--require-reconcile"], env)
        return {"ok": True, "message": f"Model {ms.public_model_name(model_name, self.state_dir)} activated"}

    def stop_serving(self) -> dict:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        selected, disabled = ms.control_files(self.state_dir)
        selected.unlink(missing_ok=True)
        ms.atomic_write_text(disabled, "")
        self.run_helper([str(self.planner_bin), "--require-reconcile"])
        return {"ok": True, "message": "Model serving stopped"}

    def remove_model(self, name: str) -> dict:
        result = removal.ModelRemovalService(self.state_dir, self.discover_bin, self.planner_bin).remove(name)
        data = {"ok": result.deleted, "deleted": result.deleted, "reconciled": result.reconciled,
                "outcome": result.outcome.name.lower()}
        data["message" if result.reconciled else "warning" if result.deleted else "error"] = result.message
        return data

    @staticmethod
    def response(response: tuple, key: str) -> dict:
        ok, result = response
        if not ok:
            raise OksaError("OperationFailed", str(result))
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
        return self.response(ms.litellm_key_list(user_id=user or None), "keys")

    def create_key(self, user: str, alias: str, models: list[str], budget: float, duration: str) -> dict:
        if not user:
            raise OksaError("InvalidInput", "User is required")
        return self.response(ms.litellm_key_create(user, alias=alias, models=models or None,
                             max_budget=None if budget == -1 else budget, duration=duration or None), "key")

    def revoke_key(self, token: str) -> dict:
        return self.response(ms.litellm_key_delete(token), "message")

    def get_settings(self) -> dict:
        contact = settings.stored_contact()
        return {"ok": True, "settings": {settings.CONTACT_KEY: contact},
                "effective_user_agent": settings.render_user_agent(contact), "manifest_applied": settings.manifest_exists()}

    def set_contact(self, raw_contact: str) -> dict:
        contact = settings.validate_contact(raw_contact)
        store = settings.load()
        if contact:
            store[settings.CONTACT_KEY] = contact
        else:
            store.pop(settings.CONTACT_KEY, None)
        settings.save(store)
        applied = settings.apply_user_agent(contact)
        return {"ok": True, "settings": {settings.CONTACT_KEY: contact},
                "effective_user_agent": settings.render_user_agent(contact), "applied": applied}

    def get_calibration(self) -> dict:
        records = {}
        for name, path in (("result", calibration.RESULT_PATH), ("status", calibration.STATUS_PATH)):
            try:
                records[name] = calibration.read_record(path)
            except FileNotFoundError:
                records[name] = None
        return {"ok": True, **records}

    def calibrate(self, force: bool) -> dict:
        command = [str(ms.resolve_helper("oksa-calibrate"))]
        if force:
            command.append("--force")
        self.run_helper(command)
        return self.get_calibration()
