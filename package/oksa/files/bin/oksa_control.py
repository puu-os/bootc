# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

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


class Contract:
    BUS = "io.github.puu_os.Oksa"
    PATH = "/io/github/puu_os/Oksa"
    JOB = "io.github.puu_os.Oksa.Job"
    MAX_JSON = 1024 * 1024
    METHODS = {
        "GetStatus": ((), "a{sv}", False),
        "ListModels": ((), "a{sv}", False),
        "SearchModels": ((('query', 's'),), "a{sv}", False),
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
        "GetCalibration": ((), "a{sv}", False),
        "Calibrate": ((('force', 'b'),), "o", True),
        "GetJob": ((('path', 'o'),), "a{sv}", False),
        "ListJobs": ((), "a{sv}", False),
    }
    OPTIONAL_STRINGS = {"email", "duration", "user", "commit", "revision", "query"}
    MODEL_MUTATIONS = frozenset({"DownloadModel", "StartModel", "StopServing", "RemoveModel", "Calibrate"})
    USER_MUTATIONS = frozenset({"CreateUser", "DeleteUser", "CreateKey", "RevokeKey"})
    MUTATIONS = MODEL_MUTATIONS | USER_MUTATIONS

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
        self.labeler_bin = ms.resolve_helper("oksa-label-model-serving-node")
        self.planner_bin = ms.resolve_helper("oksa-plan-vllm-workloads")
        self.preseed_bin = ms.resolve_helper("oksa-preseed-vllm-models")

    def execute(self, method: str, args: tuple, progress) -> dict:
        Contract.validate(method, args)
        operations = {
            "ListModels": self.list_models,
            "SearchModels": self.search_models,
            "InspectModel": self.inspect_model, "DownloadModel": self.download_model,
            "StartModel": self.start_model, "StopServing": self.stop_serving,
            "RemoveModel": self.remove_model, "ListUsers": self.list_users,
            "GetUser": self.get_user, "CreateUser": self.create_user,
            "DeleteUser": self.delete_user, "ListKeys": self.list_keys,
            "CreateKey": self.create_key, "RevokeKey": self.revoke_key,
            "GetCalibration": self.get_calibration, "Calibrate": self.calibrate,
        }
        self.progress = progress
        return operations[method](*args)

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

    def download_model(self, repo: str, name: str, commit: str, force: bool) -> dict:
        effective_name = ms.normalize_storage_id(repo, name or None)
        if not effective_name:
            raise OksaError("InvalidInput", "Invalid model name")
        command = [str(self.preseed_bin), "--repo-id", repo, "--name", effective_name, "--revision", commit]
        if force:
            command.append("--force")
        self.run_helper(command)
        self.run_helper([str(self.discover_bin)])
        self.run_helper([str(self.labeler_bin)])
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
        return self.response(ms.litellm_key_list(user_id=user or None), "keys")

    def create_key(self, user: str, alias: str, models: list[str], budget: float, duration: str) -> dict:
        if not user:
            raise OksaError("InvalidInput", "User is required")
        return self.response(ms.litellm_key_create(user, alias=alias, models=models or None,
                             max_budget=None if budget == -1 else budget, duration=duration or None), "key")

    def revoke_key(self, token: str) -> dict:
        return self.response(ms.litellm_key_delete(token), "message")

    def get_calibration(self) -> dict:
        try:
            status = calibration.read_record(calibration.STATUS_PATH)
        except FileNotFoundError:
            status = None
        selected, _ = ms.control_files(self.state_dir)
        active = Path(os.environ.get("PUU_VLLM_ACTIVE_MODEL_FILE", str(self.state_dir / "active-model")))
        name = ms.read_first_line(selected) or ms.read_first_line(active)
        if name:
            name = ms.resolve_model_name(name, self.state_dir)
        elif status and status.get("boot_id") == calibration.boot_id():
            name = status.get("model", "")
        result = None
        if name:
            try:
                result = calibration.read_record(calibration.result_path(name))
            except FileNotFoundError:
                pass
            if result is not None and not calibration.valid_result(result, model=name):
                raise OksaError("OperationFailed", f"Invalid calibration result for {name}")
            try:
                status = calibration.read_record(calibration.status_path(name))
            except FileNotFoundError:
                status = None
        return {"ok": True, "model": name, "result": result, "status": status}

    def calibrate(self, force: bool) -> dict:
        current = self.get_calibration()
        name = current["model"]
        if not name:
            raise OksaError("NotFound", "Select an installed model before calibration")
        command = [str(ms.resolve_helper("oksa-calibrate")), "--model", name]
        if force:
            command.append("--force")
        self.run_helper(command)
        self.run_helper([str(self.planner_bin), "--require-reconcile"])
        return self.get_calibration()
