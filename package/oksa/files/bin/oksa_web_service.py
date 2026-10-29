# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

import os
from pathlib import Path
import re
import subprocess
import threading
import time
import urllib.parse

import oksa_model_serving as ms
import oksa_model_removal as removal
import oksa_settings as settings
from oksa_monitoring import StatusSampler
import oksa_user_deletion as user_deletion


class OksaWebService:
    def __init__(self, state_dir: Path | None = None) -> None:
        self.state_dir = Path(state_dir) if state_dir is not None else Path(
            os.environ.get("PUU_VLLM_STATE_DIR", "/var/lib/puu/vllm")
        )
        self.discover_bin = ms.resolve_helper("oksa-discover-vllm-models")
        self.planner_bin = ms.resolve_helper("oksa-plan-vllm-workloads")
        self.preseed_bin = ms.resolve_helper("oksa-preseed-vllm-models")
        self._monitoring = StatusSampler(ms.collect_serving_status)
        self.pull_lock = threading.Lock()
        self.control_lock = threading.Lock()
        self.pull_state = {
            "running": False,
            "repo_id": None,
            "model_name": None,
            "progress": "",
            "error": None,
            "complete": False,
            "started_at": 0,
            "ended_at": 0,
        }

    @property
    def monitoring(self) -> StatusSampler:
        return self._monitoring

    def start(self) -> None:
        self.monitoring.start()

    def close(self) -> None:
        self.monitoring.close()

    def control_files(self) -> tuple[Path, Path]:
        return ms.control_files(self.state_dir)

    def run_discover(self) -> None:
        ms.run_discover(self.discover_bin)

    def check_capabilities(self, model_name: str) -> bool:
        return ms.check_capabilities(self.planner_bin, model_name)

    def collect_models_list(self) -> dict:
        return ms.collect_models_list(self.state_dir, self.discover_bin, self.planner_bin)

    def background_pull_worker(self, repo_id: str, name: str | None, revision: str, force: bool) -> None:
        cmd = [str(self.preseed_bin), "--repo-id", repo_id, "--revision", revision]
        if name:
            cmd.extend(["--name", name])
        if force:
            cmd.append("--force")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            if proc.stdout:
                for line in iter(proc.stdout.readline, ""):
                    with self.pull_lock:
                        self.pull_state["progress"] = line.strip()
                proc.stdout.close()
            proc.wait()

            with self.pull_lock:
                self.pull_state["running"] = False
                self.pull_state["ended_at"] = time.time()
                if proc.returncode == 0:
                    self.pull_state["complete"] = True
                    self.pull_state["progress"] = "Model download completed successfully."
                else:
                    self.pull_state["complete"] = False
                    self.pull_state["error"] = f"Download process exited with code {proc.returncode}"
        except Exception as exc:
            with self.pull_lock:
                self.pull_state["running"] = False
                self.pull_state["ended_at"] = time.time()
                self.pull_state["error"] = str(exc)

        try:
            self.run_discover()
        finally:
            self.monitoring.invalidate()

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        parsed = urllib.parse.urlsplit(path)
        if method == "GET":
            return self._get(parsed.path, urllib.parse.parse_qs(parsed.query))
        if method == "POST":
            if body is None:
                body = {}
            if not isinstance(body, dict):
                return 400, {"ok": False, "error": "Invalid JSON body"}
            return self._post(parsed.path, body)
        if method == "DELETE":
            return self._delete(parsed.path)
        return 404, {"ok": False, "error": "Not Found"}

    def _get(self, path: str, query: dict) -> tuple[int, dict]:
        if path == "/api/status":
            return 200, self.monitoring.snapshot().to_json()

        if path == "/api/users":
            ok, users = ms.litellm_user_list()
            return 200 if ok else 500, {
                "ok": ok,
                "users": users if ok else [],
                "error": None if ok else str(users),
            }

        if path.startswith("/api/users/"):
            user_id = urllib.parse.unquote(path[len("/api/users/"):])
            ok, user_info = ms.litellm_user_info(user_id)
            return 200 if ok else 404, {
                "ok": ok,
                "user": user_info if ok else None,
                "error": None if ok else str(user_info),
            }

        if path == "/api/keys":
            user_id = query.get("user_id", [None])[0]
            ok, keys = ms.litellm_key_list(user_id=user_id)
            return 200 if ok else 500, {
                "ok": ok,
                "keys": keys if ok else [],
                "error": None if ok else str(keys),
            }

        if path == "/api/models":
            return 200, self.collect_models_list()

        if path == "/api/models/pull/status":
            with self.pull_lock:
                state_copy = dict(self.pull_state)
            return 200, {"ok": True, "pull": state_copy}

        if path == "/api/settings":
            contact = settings.stored_contact()
            return 200, {
                "ok": True,
                "settings": {settings.CONTACT_KEY: contact},
                "effective_user_agent": settings.render_user_agent(contact),
                "manifest_applied": settings.manifest_exists(),
            }

        return 404, {"ok": False, "error": "Not Found"}

    def _post(self, path: str, body: dict) -> tuple[int, dict]:
        if path == "/api/users":
            user_id = body.get("user_id")
            if not user_id:
                return 400, {"ok": False, "error": "user_id is required"}
            email = body.get("email")
            if email is not None and not isinstance(email, str):
                return 400, {"ok": False, "error": "email must be a string"}
            budget = body.get("budget")
            if budget is not None:
                try:
                    budget = float(budget)
                except (TypeError, ValueError, OverflowError):
                    return 400, {"ok": False, "error": "budget must be a number"}
            ok, res = ms.litellm_user_create(user_id, email=email, max_budget=budget)
            return 200 if ok else 400, {
                "ok": ok,
                "result": res if ok else None,
                "error": None if ok else str(res),
            }

        if path == "/api/keys":
            user_id = body.get("user_id")
            if not user_id:
                return 400, {"ok": False, "error": "user_id is required"}
            alias = body.get("alias")
            if not isinstance(alias, str) or not alias.strip():
                return 400, {"ok": False, "error": "alias is required"}
            alias = alias.strip()
            models = body.get("models")
            if isinstance(models, str):
                models = [m.strip() for m in models.split(",") if m.strip()]
            spend = body.get("spend")
            if spend is not None:
                try:
                    spend = float(spend)
                except (TypeError, ValueError, OverflowError):
                    return 400, {"ok": False, "error": "spend must be a number"}
            duration = body.get("duration")
            if duration is not None and not isinstance(duration, str):
                return 400, {"ok": False, "error": "duration must be a string"}
            ok, res = ms.litellm_key_create(
                user_id,
                alias=alias,
                models=models,
                max_budget=spend,
                duration=duration,
            )
            return 200 if ok else 400, {
                "ok": ok,
                "key": res if ok else None,
                "error": None if ok else str(res),
            }

        if path == "/api/models/inspect":
            repo_id = body.get("repo_id")
            revision = body.get("revision", "main")
            if not isinstance(repo_id, str) or not repo_id.strip() or not isinstance(revision, str) or not revision.strip():
                return 400, {"ok": False, "error": "Repository and revision must be non-empty strings"}
            meta = ms.inspect_hf_model(repo_id, revision)
            if not meta:
                return 404, {"ok": False, "error": f"Failed to fetch metadata for {repo_id}"}
            mem_mib = meta.get("estimated_memory_mib", 0)
            fits, reason = ms.check_model_capacity(mem_mib)
            return 200, {
                "ok": True,
                "metadata": meta,
                "fits": fits,
                "reason": reason,
            }

        if path == "/api/models/pull":
            repo_id = body.get("repo_id")
            if not repo_id:
                return 400, {"ok": False, "error": "repo_id is required"}
            name = body.get("name")
            if not isinstance(repo_id, str) or (name is not None and not isinstance(name, str)):
                return 400, {"ok": False, "error": "Invalid repository or model name"}
            effective_name = ms.normalize_storage_id(repo_id, name)
            if not effective_name:
                return 400, {"ok": False, "error": "Invalid repository or model name"}
            if ms.is_calibration_model(repo_id) or ms.is_calibration_model(effective_name):
                return 403, {"ok": False, "error": "The calibration model is installed automatically"}
            revision = body.get("revision")
            force = body.get("force", False)
            if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", revision) or type(force) is not bool:
                return 400, {"ok": False, "error": "Download requires an inspected commit revision and a Boolean override"}

            with self.pull_lock:
                if self.pull_state["running"]:
                    return 409, {"ok": False, "error": "Another model download is currently running"}
                self.pull_state.update(running=True, repo_id=repo_id, model_name=effective_name, progress="Starting download...",
                                       error=None, complete=False, started_at=time.time(), ended_at=0)

            try:
                thread = threading.Thread(
                    target=self.background_pull_worker,
                    args=(repo_id, effective_name, revision, force),
                    daemon=True,
                )
                thread.start()
            except RuntimeError as exc:
                with self.pull_lock:
                    self.pull_state.update(running=False, error=str(exc), ended_at=time.time())
                return 503, {"ok": False, "error": "Could not start download worker; retry Download"}
            return 200, {"ok": True, "message": f"Download started for {repo_id}"}

        if path == "/api/models/start":
            model_name = body.get("name")
            if not model_name:
                return 400, {"ok": False, "error": "name is required"}
            try:
                model_name = ms.resolve_model_name(model_name, self.state_dir)
            except ValueError as exc:
                return 400, {"ok": False, "error": str(exc)}
            if not self.check_capabilities(model_name):
                return 400, {"ok": False, "error": "Model has insufficient host capabilities"}
            with self.control_lock, self.monitoring.changing():
                self.state_dir.mkdir(parents=True, exist_ok=True)
                selected_model_file, serving_disabled_file = self.control_files()
                ms.atomic_write_text(selected_model_file, f"{model_name}\n")
                serving_disabled_file.unlink(missing_ok=True)
                env = dict(os.environ, PUU_VLLM_SELECTED_MODEL=model_name, PUU_VLLM_REQUIRE_SELECTED_MODEL="true")
                res = subprocess.run([str(self.planner_bin)], env=env, capture_output=True)
            if res.returncode != 0:
                return 500, {"ok": False, "error": "Failed to activate model workload"}
            return 200, {"ok": True, "message": f"Model {ms.public_model_name(model_name, self.state_dir)} activated"}

        if path == "/api/models/stop":
            with self.control_lock, self.monitoring.changing():
                self.state_dir.mkdir(parents=True, exist_ok=True)
                selected_model_file, serving_disabled_file = self.control_files()
                selected_model_file.unlink(missing_ok=True)
                ms.atomic_write_text(serving_disabled_file, "")
                res = subprocess.run([str(self.planner_bin)], capture_output=True)
            if res.returncode != 0:
                return 500, {"ok": False, "error": "Failed to stop model workload"}
            return 200, {"ok": True, "message": "Model serving stopped"}

        if path == "/api/settings":
            raw_contact = body.get(settings.CONTACT_KEY, "")
            if not isinstance(raw_contact, str):
                return 400, {"ok": False, "error": "Contact must be a string"}
            try:
                contact = settings.validate_contact(raw_contact)
            except ValueError as exc:
                return 400, {"ok": False, "error": str(exc)}
            store = settings.load()
            if contact:
                store[settings.CONTACT_KEY] = contact
            else:
                store.pop(settings.CONTACT_KEY, None)
            try:
                settings.save(store)
                applied = settings.apply_user_agent(contact)
            except OSError as exc:
                return 500, {"ok": False, "error": f"Failed to store settings: {exc}"}
            return 200, {
                "ok": True,
                "settings": {settings.CONTACT_KEY: contact},
                "effective_user_agent": settings.render_user_agent(contact),
                "applied": applied,
            }

        return 404, {"ok": False, "error": "Not Found"}

    def _delete(self, path: str) -> tuple[int, dict]:
        if path.startswith("/api/users/"):
            user_id = urllib.parse.unquote(path[len("/api/users/"):])
            result = user_deletion.UserDeletionService().delete(user_id)
            status = {
                user_deletion.UserDeletionOutcome.INVALID: 400,
                user_deletion.UserDeletionOutcome.PROTECTED: 403,
                user_deletion.UserDeletionOutcome.NOT_STARTED: 502,
                user_deletion.UserDeletionOutcome.KEY_REVOCATION_UNCONFIRMED: 502,
                user_deletion.UserDeletionOutcome.USER_DELETION_UNCONFIRMED: 502,
                user_deletion.UserDeletionOutcome.DELETED: 200,
            }[result.outcome]
            data = {
                "ok": result.deleted is True,
                "outcome": result.outcome.name.lower(),
                "deleted": result.deleted,
                "confirmed_revoked_keys": result.confirmed_revoked_keys,
                "message": result.message if result.deleted is True else None,
                "error": result.message if result.deleted is not True else None,
            }
            if result.uncertain:
                data["warning"] = result.message
            return status, data

        if path.startswith("/api/keys/"):
            key_or_alias = urllib.parse.unquote(path[len("/api/keys/"):])
            ok, res = ms.litellm_key_delete(key_or_alias)
            return 200 if ok else 400, {
                "ok": ok,
                "message": res if ok else None,
                "error": None if ok else str(res),
            }

        if path.startswith("/api/models/"):
            model_name = urllib.parse.unquote(path[len("/api/models/"):])
            with self.control_lock, self.monitoring.changing():
                result = removal.ModelRemovalService(self.state_dir, self.discover_bin, self.planner_bin).remove(model_name)
            if result.deleted:
                data = {"ok": True, "deleted": True, "reconciled": result.reconciled}
                data["message" if result.reconciled else "warning"] = result.message
                return 200, data
            status = {
                removal.RemovalOutcome.PROTECTED: 403,
                removal.RemovalOutcome.INVALID: 400,
                removal.RemovalOutcome.BUSY: 409,
                removal.RemovalOutcome.NOT_STOPPED: 409,
                removal.RemovalOutcome.SHUTDOWN_FAILED: 503,
                removal.RemovalOutcome.REMOVAL_FAILED: 500,
            }[result.outcome]
            data = {"ok": False, "error": result.message}
            if result.outcome not in (removal.RemovalOutcome.PROTECTED, removal.RemovalOutcome.INVALID):
                data["deleted"] = False
            return status, data

        return 404, {"ok": False, "error": "Not Found"}
