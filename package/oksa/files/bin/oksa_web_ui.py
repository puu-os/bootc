# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

import asyncio
from datetime import datetime, timezone
import math
import re
import time

from nicegui import run, ui

from oksa_client import OksaClient, OksaClientError


class OksaWebUI:
    def __init__(self, service: OksaClient) -> None:
        self.service = service

    def mount(self) -> None:
        for path in ("/", "/index.html"):
            ui.page(path)(self._page)

    def _page(self) -> None:
        _OksaPage(self.service).build()


class _OksaPage:
    def __init__(self, service: OksaClient) -> None:
        self.service = service
        self.client = ui.context.client
        self.locks = {name: asyncio.Lock() for name in ("status", "pull", "models", "calibration", "users", "search", "inspect")}
        self.generations = dict.fromkeys(self.locks, 0)
        self.busy: set[str] = set()
        self.warnings: dict[str, str] = {}
        self.models: list[dict] = []
        self.users: list[dict] = []
        self.keys: dict[str, dict] = {}
        self.owner: str | None = None
        self.snapshot: dict = {}
        self.status: dict | None = None
        self.status_received = 0.0
        self.status_failed = False
        self.partial = False
        self.status_stale = True
        self.models_verified = False
        self.serving_enabled = None
        self.jobs: dict[str, dict] = {}
        self.jobs_seen: set[str] = set()
        self.job_details: set[str] = set()
        self.jobs_received = 0.0
        self.jobs_failed = True
        self.job_widgets = []
        self.model_job: str | None = None
        self.model_operation: str | None = None
        self.model_uncertain = False
        self.calibration: dict | None = None
        self.calibration_received = 0.0
        self.inspection: dict | None = None
        self.inspection_generation = 0
        self.secret = ""
        self.models_revision: str | None = None
        self.search_generation = 0

    def build(self) -> None:
        ui.dark_mode(False)
        ui.label("OKSA").classes("text-xl font-semibold")
        self.warning_label = ui.label("").classes("text-warning")
        with ui.tabs().classes("max-w-full") as self.tabs:
            for name in ("Monitoring", "Models", "Users"):
                ui.tab(name)
        with ui.tab_panels(self.tabs, value="Monitoring").classes("w-full"):
            with ui.tab_panel("Monitoring"):
                self._build_monitoring()
            with ui.tab_panel("Models"):
                self._build_models()
            with ui.tab_panel("Users"):
                self._build_users()
        self._build_download_dialog()
        self._build_user_dialog()
        self._build_key_dialog()
        self._build_secret_dialog()
        self.tabs.on_value_change(self._tab_changed)
        self.client.on_disconnect(self._disconnect)
        self.client.on_delete(self._clear_secret)
        ui.timer(1, self._render_freshness)
        ui.timer(2.5, self._poll)

    @staticmethod
    def _table(title: str, fields: dict[str, str], *, selection: str | None = None, row_key: str = "id"):
        return ui.table(
            title=title,
            columns=[{"name": key, "field": key, "label": label, "sortable": True}
                     for key, label in fields.items()],
            rows=[], row_key=row_key, selection=selection, pagination=10,
        ).props("color=primary").classes("w-full")

    @staticmethod
    def _number(value, maximum: float = math.inf, *, integer: bool = False) -> bool:
        return (type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum
                and (not integer or int(value) == value))

    def _metric(self, value, suffix: str = "", maximum: float = math.inf, *, integer: bool = False) -> str:
        if not self._number(value, maximum, integer=integer):
            self.partial = True
            return "Unavailable"
        return f"{value:g}{suffix}"

    def _text(self, value) -> str:
        if not isinstance(value, str) or not value.strip():
            self.partial = True
            return "Unavailable"
        return "None" if value == "-" else value

    @staticmethod
    def _success(reply: tuple[int, dict]) -> bool:
        status, data = reply
        return 200 <= status < 300 and data.get("ok") is True

    @staticmethod
    def _error(reply: tuple[int, dict]) -> str:
        return str(reply[1].get("error") or f"Invalid service response (status {reply[0]})")

    async def _call(self, method: str, *args, wait: bool = True) -> tuple[int, dict]:
        try:
            result = await run.io_bound(self.service.call, method, *args)
            if isinstance(result, dict) and "job" in result and wait:
                result = await run.io_bound(self.service.wait_job, result["job"], timeout=600)
        except OksaClientError as error:
            return 0, {"ok": False, **error.payload, "error": str(error)}
        except (OSError, ValueError, TimeoutError) as error:
            return 0, {"ok": False, "error": f"Service result could not be confirmed: {error}"}
        if not isinstance(result, dict):
            return 0, {"ok": False, "error": "Service result could not be confirmed."}
        return 200, result

    async def _load(self, name: str, method: str) -> tuple[int, dict] | None:
        self.generations[name] += 1
        generation = self.generations[name]
        async with self.locks[name]:
            if generation != self.generations[name] or self.client.is_deleted:
                return None
            reply = await self._call(method)
            if generation != self.generations[name] or self.client.is_deleted:
                return None
            return reply

    def _warn(self, key: str, message: str) -> None:
        self.warnings[key] = message
        if not self.client.is_deleted:
            self.warning_label.set_text("\n".join(self.warnings.values()))

    def _begin(self, name: str, controls: list) -> bool:
        if name in self.busy or self.client.is_deleted:
            return False
        self.busy.add(name)
        for control in controls:
            control.disable()
        return True

    def _end(self, name: str, controls: list) -> None:
        self.busy.discard(name)
        if self.client.is_deleted:
            return
        for control in controls:
            control.enable()
        if name == "user":
            self._users_selection()

    async def _confirm(self, message: str, action: str, *, dangerous: bool = False) -> bool:
        with ui.dialog() as dialog, ui.card():
            ui.label(message).classes("whitespace-pre-line break-words max-h-96 overflow-y-auto")
            with ui.row():
                ui.button("Cancel", color="accent", on_click=lambda: dialog.submit(False)).props("outline")
                ui.button(action, color="negative" if dangerous else "primary", on_click=lambda: dialog.submit(True))
        try:
            return await dialog is True and not self.client.is_deleted
        finally:
            if not dialog.is_deleted:
                dialog.delete()

    async def _tab_changed(self) -> None:
        if self.tabs.value == "Monitoring":
            await self._load_monitoring()
        elif self.tabs.value == "Models":
            await self._refresh_model_state()
        elif self.tabs.value == "Users":
            await self._load_users()

    async def _poll(self) -> None:
        if self.client.is_deleted or not self.client.has_socket_connection:
            return
        if self.tabs.value in ("Monitoring", "Models") and not self.locks["status"].locked():
            await self._load_monitoring()
        if (self.tabs.value == "Models" or self.model_job or self.model_uncertain) and not self.locks["pull"].locked():
            await self._load_pull()
        if self.tabs.value == "Models":
            await self._refresh_models()
            if not self.locks["calibration"].locked():
                await self._load_calibration()

    def _build_monitoring(self) -> None:
        with ui.row().classes("w-full items-center justify-between gap-2"):
            self.freshness = ui.label("Waiting for first readings").classes("text-sm text-secondary tabular-nums")
            ui.button("Refresh monitoring", color="accent", on_click=self._load_monitoring).props("outline")
        self.monitoring_error = ui.label("").classes("text-negative")
        self.monitoring_error.set_visibility(False)
        with ui.grid().classes("w-full grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4"):
            with ui.card().props("flat").classes("w-full p-4 gap-1"):
                ui.label("Serving").classes("text-sm text-secondary")
                self.inference = ui.label("Unavailable").classes("text-xl font-semibold break-words")
                self.model_status = ui.label("No active model").classes("text-sm text-secondary w-full break-words")
            with ui.card().props("flat").classes("w-full p-4 gap-1"):
                ui.label("Host memory").classes("text-sm text-secondary")
                self.memory = ui.label("Unavailable").classes("text-xl font-semibold tabular-nums break-words")
                self.memory_percent = ui.label("").classes("text-sm text-secondary tabular-nums")
                self.memory_percent.set_visibility(False)
                self.memory_progress = ui.linear_progress(value=0, show_value=False).classes("w-full")
                self.memory_progress.set_visibility(False)
            with ui.card().props("flat").classes("w-full p-4 gap-1"):
                ui.label("Requests").classes("text-sm text-secondary")
                self.requests = ui.label("Unavailable").classes("text-xl font-semibold tabular-nums")
                self.requests_caption = ui.label("").classes("text-sm text-secondary")
                self.requests_caption.set_visibility(False)
            with ui.card().props("flat").classes("w-full p-4 gap-1"):
                ui.label("KV cache").classes("text-sm text-secondary")
                self.kv_cache = ui.label("Unavailable").classes("text-xl font-semibold tabular-nums")
                self.kv_caption = ui.label("").classes("text-sm text-secondary")
                self.kv_caption.set_visibility(False)
                self.kv_progress = ui.linear_progress(value=0, show_value=False).classes("w-full")
                self.kv_progress.set_visibility(False)
        self.gpus_label = ui.label("GPU data unavailable").classes("text-sm text-secondary")
        self.gpus_table = self._table("GPUs", {
            "index": "Index", "name": "Name", "utilization": "Utilization", "memory": "Memory (MiB)",
            "temperature": "Temperature", "power": "Power",
        })
        self.gpus_table.set_visibility(False)
        self.workloads_label = ui.label("Workload data unavailable").classes("text-sm text-secondary")
        self.workloads_table = self._table("Workloads", {
            "name": "Name", "phase": "Phase", "ready": "Ready", "restarts": "Restarts",
        })
        self.workloads_table.set_visibility(False)

    async def _load_monitoring(self) -> None:
        requested = time.monotonic()
        reply = await self._load("status", "GetStatus")
        if reply is None:
            return
        data = reply[1]
        if not self._success(reply) or ("status" in data and data["status"] is not None and not isinstance(data["status"], dict)):
            self.status_failed = True
            self.monitoring_error.set_text("Monitoring update failed. " + self._error(reply))
            self.monitoring_error.set_visibility(True)
            self._render_freshness()
            return
        self.status_failed = False
        self.status = data.get("status")
        self.snapshot = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else {}
        self.status_received = requested
        error_text = str(self.snapshot.get("error") or "")
        self.monitoring_error.set_text(error_text)
        self.monitoring_error.set_visibility(bool(error_text))
        self.partial = False
        if self.status is None:
            self._render_freshness()
            return
        status = self.status
        model_state = status.get("model_status")
        if (model_state not in ("stopped", "no-model", "not-ready", "ready")
                or status.get("gateway_status") not in ("ready", "authentication-required", "unreachable")):
            self.partial = True
        memory = status.get("host_memory") if isinstance(status.get("host_memory"), dict) else {}
        used, total, used_pct = memory.get("used_mib"), memory.get("total_mib"), memory.get("used_pct")
        if self._number(used) and self._number(total) and total > 0:
            self.memory.set_text(f"{self._storage(used)} / {self._storage(total)}")
        else:
            self.partial = True
            self.memory.set_text("Unavailable")
        if self._number(used_pct, 100):
            self.memory_percent.set_text(f"{used_pct:g}% used")
            self.memory_percent.set_visibility(True)
        else:
            self.partial = True
            self.memory_percent.set_text("")
            self.memory_percent.set_visibility(False)
        self._progress(self.memory_progress, used_pct)

        active_raw = status.get("active_model")
        selected_raw = status.get("selected_model")
        if not isinstance(active_raw, str) or not active_raw.strip():
            self.partial = True
            active_text = "Model unavailable"
        elif active_raw == "-":
            active_text = "No active model"
        else:
            active_text = active_raw
        if isinstance(selected_raw, str) and selected_raw.strip() and selected_raw not in ("-", active_raw):
            active_text += f" · Selected: {selected_raw}"
        elif not isinstance(selected_raw, str) or not selected_raw.strip():
            self.partial = True
        self.model_status.set_text(active_text)

        metrics = status.get("metrics") if isinstance(status.get("metrics"), dict) else {}
        running, waiting = metrics.get("running_requests"), metrics.get("waiting_requests")
        idle_caption = "starting" if model_state == "not-ready" else "no active runtime" if model_state in ("stopped", "no-model") else ""
        if self._number(running, integer=True) and self._number(waiting, integer=True):
            self.requests.set_text(f"{int(running)} / {int(waiting)}")
            self.requests_caption.set_text("running / waiting")
            self.requests_caption.set_visibility(True)
        elif idle_caption:
            self.requests.set_text("—")
            self.requests_caption.set_text(idle_caption)
            self.requests_caption.set_visibility(True)
        else:
            self.partial = True
            self.requests.set_text("Unavailable")
            self.requests_caption.set_text("")
            self.requests_caption.set_visibility(False)

        kv_pct = metrics.get("kv_cache_usage_pct")
        if self._number(kv_pct, 100):
            self.kv_cache.set_text(f"{kv_pct:g}%")
            self.kv_caption.set_text("cache used")
            self.kv_caption.set_visibility(True)
        elif idle_caption:
            self.kv_cache.set_text("—")
            self.kv_caption.set_text(idle_caption)
            self.kv_caption.set_visibility(True)
        else:
            self.partial = True
            self.kv_cache.set_text("Unavailable")
            self.kv_caption.set_text("")
            self.kv_caption.set_visibility(False)
        self._progress(self.kv_progress, kv_pct)

        gpus = status.get("gpus")
        self.gpus_table.rows = []
        self.gpus_label.set_text("No GPUs reported" if gpus == [] else "" if isinstance(gpus, list) else "GPU data unavailable")
        if isinstance(gpus, list):
            for index, gpu in enumerate(gpus):
                if not isinstance(gpu, dict):
                    self.partial = True
                    continue
                total = gpu.get("memory_total_mib")
                self.gpus_table.rows.append({
                    "id": index, "index": self._metric(gpu.get("index"), integer=True), "name": self._text(gpu.get("name")),
                    "utilization": self._metric(gpu.get("gpu_util_pct"), "%", 100),
                    "memory": self._metric(gpu.get("memory_used_mib")) + " / "
                    + self._metric(total if self._number(total) and total > 0 else None),
                    "temperature": self._metric(gpu.get("temperature_c"), " °C"),
                    "power": self._metric(gpu.get("power_w"), " W"),
                })
        else:
            self.partial = True
        has_gpus = bool(self.gpus_table.rows)
        self.gpus_table.set_visibility(has_gpus)
        self.gpus_label.set_visibility(not has_gpus)
        self.gpus_table.update()

        workloads = status.get("workloads")
        self.workloads_table.rows = []
        self.workloads_label.set_text("No workloads reported" if workloads == [] else "" if isinstance(workloads, list) else "Workload data unavailable")
        if isinstance(workloads, list):
            for index, workload in enumerate(workloads):
                if not isinstance(workload, dict):
                    self.partial = True
                    continue
                ready = workload.get("ready")
                if type(ready) is not bool:
                    self.partial = True
                self.workloads_table.rows.append({
                    "id": index, "name": self._text(workload.get("name")), "phase": self._text(workload.get("phase")),
                    "ready": "Yes" if ready is True else "No" if ready is False else "Unavailable",
                    "restarts": self._metric(workload.get("restarts"), integer=True),
                })
        else:
            self.partial = True
        has_workloads = bool(self.workloads_table.rows)
        self.workloads_table.set_visibility(has_workloads)
        self.workloads_label.set_visibility(not has_workloads)
        self.workloads_table.update()
        self._render_freshness()

    def _progress(self, progress, value) -> None:
        available = self._number(value, 100)
        progress.set_visibility(available)
        if available:
            progress.set_value(value / 100)

    def _render_freshness(self) -> None:
        if self.client.is_deleted:
            return
        metadata = self.snapshot
        sampled = metadata.get("sampled_at")
        try:
            parsed = datetime.fromisoformat(sampled.replace("Z", "+00:00")) if isinstance(sampled, str) else None
        except ValueError:
            parsed = None
        age, maximum = metadata.get("age_seconds"), metadata.get("max_age_seconds")
        error = metadata.get("error")
        known = (self.status is not None and parsed is not None and parsed.tzinfo is not None
                 and self._number(age) and self._number(maximum) and maximum > 0
                 and type(metadata.get("stale")) is bool
                 and (error is None or isinstance(error, str)))
        elapsed = age + max(0, time.monotonic() - self.status_received) if known else None
        stale = not known or self.status_failed or metadata.get("stale") or error is not None or elapsed > maximum
        if known:
            parts = []
            if stale:
                parts.append("Stale")
            parts.append(f"Updated {int(elapsed)}s ago")
            if self.partial:
                parts.append("Some readings unavailable")
            self.freshness.set_text(" · ".join(parts))
        else:
            self.freshness.set_text("Update failed" if self.status_failed else
                                    "Collection failed" if error else
                                    "Freshness unavailable" if self.status is not None else "Waiting for first readings")
        inference = "Unavailable"
        if self.status is not None:
            if stale:
                inference = "Unverified — stale readings" if known else "Unverified — freshness unavailable"
            else:
                model, gateway = self.status.get("model_status"), self.status.get("gateway_status")
                inference = {"stopped": "Stopped", "no-model": "No model loaded", "not-ready": "Starting…"}.get(model, "Unavailable")
                if model == "ready":
                    inference = {"ready": "Ready", "authentication-required": "Gateway auth required"}.get(gateway, "Gateway unavailable")
        self.inference.set_text(inference)
        self.status_stale = stale
        self.models_freshness.set_text(("Unverified · " if stale else "") + self.freshness.text)
        self.models_resources.set_text(f"Requests (running / waiting): {self.requests.text} · KV cache: {self.kv_cache.text}"
                                       + (" · Last readings, unverified" if stale else ""))
        gpu_lines = [f"GPU {gpu['index']} {gpu['name']}: utilization {gpu['utilization']} · memory used / total {gpu['memory']} MiB"
                     for gpu in self.gpus_table.rows]
        self.models_gpus.set_text("Device-wide (not per model):\n" + ("\n".join(gpu_lines) or self.gpus_label.text))
        jobs_fresh = self._jobs_fresh()
        self.activity_freshness.set_text(("Activity checked " + str(int(max(0, time.monotonic() - self.jobs_received))) + "s ago"
                                          if self.jobs_received else "Activity not loaded") + (" · Unverified" if not jobs_fresh else ""))
        for job, badge, spinner, elapsed_label, cancel in self.job_widgets:
            verified = jobs_fresh and job["path"] in self.jobs_seen
            running = job["state"] in ("queued", "running")
            badge.set_text(job["state"].title() + (" · Unverified" if not verified else ""))
            badge.props("color=" + {"failed": "negative", "canceled": "warning", "succeeded": "positive"}.get(job["state"], "primary"))
            spinner.set_visibility(running and verified)
            cancel.set_visibility(job["state"] == "running")
            cancel.set_enabled(verified and "cancel" not in self.busy)
            end = job.get("ended_at")
            elapsed_label.set_text(f"{int(max(0, (time.time() if running else end) - job['started_at']))}s"
                                   + (" elapsed" if running else " duration") if running or end is not None else "Duration unavailable")
        self._render_models()

    def _build_models(self) -> None:
        with ui.row().classes("w-full items-center"):
            ui.button("Find a model", icon="search", on_click=self._open_download)
            self.start_button = ui.button("Start selected", on_click=lambda: self._model_action("start"))
            self.stop_button = ui.button("Stop serving", on_click=lambda: self._model_action("stop")).props("outline")
            self.remove_button = ui.button("Remove selected", color="negative", on_click=lambda: self._model_action("remove")).props("outline")
            ui.button("Refresh confirmed state", icon="refresh", on_click=self._refresh_model_state).props("outline")
        self.model_controls = [self.start_button, self.stop_button, self.remove_button]
        self.models_summary = ui.label("Loading model state…")
        self.models_error = ui.label("").classes("text-negative")
        with ui.card().classes("w-full"):
            self.models_freshness = ui.label("Waiting for first readings")
            self.models_resources = ui.label("Runtime readings unavailable")
            self.models_gpus = ui.label("Device-wide GPU readings unavailable").classes("whitespace-pre-line")
        self.models_empty = ui.label("Loading installed models…")
        self.models_table = self._table("Installed models", {
            "name": "Model", "storage_mib": "On disk (MiB)", "quantization": "Format",
            "capabilities": "Host capabilities", "state": "State",
        }, selection="single")
        self.models_table.set_visibility(False)
        self.models_table.on_select(self._model_selection)
        with ui.card().classes("w-full"):
            self.model_detail = ui.label("Select an installed model")
            with ui.row():
                self.model_badge = ui.badge("No selection")
                self.selection_badge = ui.badge("Selected for serving")
                self.selection_badge.set_visibility(False)
            self.model_profile = ui.label("")
        with ui.card().classes("w-full"):
            ui.label("Latest calibration activity (backend record, not current job completion)")
            self.calibration_badge = ui.badge("Unavailable")
            self.calibration_context = ui.label("")
            self.calibration_message = ui.label("").classes("whitespace-pre-line break-words")
        ui.label("Running and recent activity").classes("text-lg")
        self.activity_freshness = ui.label("Activity not loaded")
        self.pull_error = ui.label("").classes("text-negative")
        self.activity = ui.column().classes("w-full")
        self._model_selection()

    @staticmethod
    def _storage(value) -> str:
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            return "Unavailable"
        return f"{value / 1024:.1f} GiB" if value >= 1024 else f"{value:g} MiB"

    def _selected_model(self) -> dict | None:
        selected = self.models_table.selected
        if not selected:
            return None
        return next((model for model in self.models if model.get("id") == selected[0].get("id")), None)

    def _jobs_fresh(self) -> bool:
        return not self.jobs_failed and time.monotonic() - self.jobs_received <= 10

    def _model_available(self) -> bool:
        return (self._jobs_fresh() and not self.model_job and not self.model_uncertain
                and not self.busy.intersection({"model", "download"})
                and not any(job["state"] in ("queued", "running") for job in self.jobs.values()))

    def _model_state(self, model: dict) -> str:
        status = self.status or {}
        if self.status_stale:
            return "Unverified" if model.get("active") or model.get("selected") else "Installed (runtime unverified)"
        if not self.models_verified:
            return "Installed (inventory unverified)"
        if model["name"] == status.get("active_model"):
            if status.get("model_status") == "ready":
                return "Serving" if status.get("gateway_status") == "ready" else "Runtime ready; gateway unverified"
            if status.get("model_status") == "not-ready":
                return "Starting"
        if model.get("active"):
            return "Active marker (serving unverified)"
        return "Installed"

    def _model_selection(self) -> None:
        model = self._selected_model()
        available = self._model_available() and self.models_verified
        self.start_button.set_enabled(available and model is not None and model.get("capabilities") == "ok"
                                      and self._model_state(model) != "Serving")
        self.stop_button.set_enabled(available)
        self.remove_button.set_enabled(available and model is not None and model.get("removable") is not False)
        if hasattr(self, "download_button"):
            self.download_button.set_enabled(self.inspection is not None and self._model_available() and "inspect" not in self.busy)
        self.model_detail.set_text(f"{model['name']} · {model.get('repo_id') or 'Repository unavailable'} · "
                                   + self._storage(model.get("storage_mib")) if model else "Select an installed model")
        state = self._model_state(model) if model else "No selection"
        self.model_badge.set_text(state)
        self.model_badge.props("color=" + ("positive" if state == "Serving" else "warning" if "nverified" in state else "primary"))
        self.selection_badge.set_visibility(model is not None and model.get("selected") is True)
        self.selection_badge.set_text("Selected for serving" if self.models_verified else "Selected marker (unverified)")
        profile = "Measured profile unavailable for this selection (not live limits)"
        calibration = self.calibration or {}
        result = calibration.get("result")
        if (model and self.models_verified and (model.get("selected") or model.get("active"))
                and calibration.get("model") == model["id"] and isinstance(result, dict)
                and result.get("model") == model["id"] and result.get("state") == "complete" and result.get("reason") == "measured"):
            limits = result.get("limits")
            if (isinstance(limits, dict) and self._number(limits.get("max_model_len"), integer=True)
                    and self._number(limits.get("max_num_seqs"), integer=True)
                    and limits["max_model_len"] > 0 and limits["max_num_seqs"] > 0):
                prefix = "Unverified measured profile" if self.status_stale or time.monotonic() - self.calibration_received > 10 else "Measured profile"
                profile = (f"{prefix} (not live): context {limits['max_model_len']} tokens · "
                           f"concurrency {limits['max_num_seqs']}")
        self.model_profile.set_text(profile if model else "")
        activity = calibration.get("status")
        valid_activity = (isinstance(activity, dict) and isinstance(calibration.get("model"), str)
                          and bool(calibration["model"].strip()) and activity.get("model") == calibration["model"]
                          and isinstance(activity.get("state"), str) and bool(activity["state"].strip())
                          and isinstance(activity.get("message"), str)
                          and self._number(activity.get("updated_at"), 253402300799))
        verified = bool(self.calibration_received) and time.monotonic() - self.calibration_received <= 10
        self.calibration_badge.set_text(activity["state"] + (" · Unverified" if not verified else "")
                                        if valid_activity else "Unavailable · Unverified")
        self.calibration_badge.props("color=" + ("primary" if verified and valid_activity else "warning"))
        context = "No identity-verified calibration activity available"
        if valid_activity:
            updated = datetime.fromtimestamp(activity["updated_at"], timezone.utc).isoformat(timespec="seconds")
            checked = (f"Checked {int(max(0, time.monotonic() - self.calibration_received))}s ago"
                       if self.calibration_received else "Refresh failed or unavailable")
            context = f"Model ID: {calibration['model']} · {checked} · Backend record timestamp: {updated}"
        self.calibration_context.set_text(("Unverified · " if not verified else "") + context)
        self.calibration_message.set_text(activity["message"] if valid_activity else "")

    def _render_models(self) -> None:
        selected = {row["id"] for row in self.models_table.selected}
        rows = [{
            "id": model["id"], "name": model["name"],
            "storage_mib": model.get("storage_mib") if self._number(model.get("storage_mib")) else None,
            "quantization": model.get("quantization") or "none",
            "capabilities": "Compatible" if model.get("capabilities") == "ok" else "Insufficient or unverified",
            "state": self._model_state(model) + (" · Selected" if model.get("selected") else ""),
        } for model in self.models]
        selection = [row for row in rows if row["id"] in selected]
        if self.models_table.rows != rows or self.models_table.selected != selection:
            self.models_table.rows = rows
            self.models_table.selected = selection
            self.models_table.update()
        waiting = (self.model_job is not None or bool(self.busy.intersection({"model", "download"}))
                   or any(job["state"] in ("queued", "running") for job in self.jobs.values()))
        if waiting:
            inventory = "Inventory refresh waits for the ongoing or unverified operation."
            if self.models:
                inventory += " Cached inventory is unverified."
        elif not self.models_verified:
            inventory = "Installed inventory unavailable or unverified; refresh confirmed state."
        else:
            inventory = "No installed models. Find a model to get started."
        self.models_empty.set_text(inventory)
        self.models_empty.set_visibility(waiting or not self.models or not self.models_verified)
        self.models_summary.set_text(self.inference.text + " · " + self.model_status.text
                                     + (" · Serving disabled" if self.models_verified and self.serving_enabled is False else ""))
        self._model_selection()

    async def _refresh_model_state(self) -> None:
        await self._load_pull()
        await self._load_monitoring()
        if not any(job["state"] in ("queued", "running") for job in self.jobs.values()):
            await self._load_models()
        await self._load_calibration()
        if self.client.is_deleted:
            return
        if (self._jobs_fresh() and self.models_verified and not self.status_stale and not self.model_job
                and not self.busy.intersection({"model", "download"})
                and not any(job["state"] in ("queued", "running") for job in self.jobs.values())):
            self.model_uncertain = False
        self._model_selection()

    async def _load_calibration(self) -> None:
        requested = time.monotonic()
        reply = await self._load("calibration", "GetCalibration")
        if reply is None:
            return
        data = reply[1]
        if (self._success(reply) and isinstance(data.get("model"), str)
                and (data.get("result") is None or isinstance(data["result"], dict))
                and (data.get("status") is None or isinstance(data["status"], dict))):
            self.calibration = data
            self.calibration_received = requested
        else:
            self.calibration_received = 0.0
        self._model_selection()

    async def _refresh_models(self) -> None:
        if (self.locks["models"].locked() or "model" in self.busy
                or any(job["state"] in ("queued", "running") for job in self.jobs.values())
                or not self._begin("model-refresh", [])):
            return
        generation = self.generations["models"]
        try:
            try:
                properties = await run.io_bound(self.service.get_properties)
            except OksaClientError as error:
                if self.client.is_deleted or generation != self.generations["models"]:
                    return
                self.models_revision = None
                self.models_verified = False
                self.models_error.set_text("Automatic model updates unavailable. " + str(error))
                return
            if self.client.is_deleted or generation != self.generations["models"]:
                return
            revision = properties.get("ModelsRevision") if isinstance(properties, dict) else None
            if not isinstance(revision, str) or not revision:
                self.models_revision = None
                self.models_verified = False
                self.models_error.set_text("Automatic model updates unavailable: missing model revision.")
                return
            if (revision != self.models_revision or not self.models_verified) and await self._load_models():
                self.models_revision = revision
        finally:
            self._end("model-refresh", [])
            if not self.client.is_deleted:
                self._render_models()

    async def _load_models(self) -> bool:
        if self.client.is_deleted:
            return False
        if (self.model_job or self.busy.intersection({"model", "download"})
                or any(job["state"] in ("queued", "running") for job in self.jobs.values())):
            self.models_verified = False
            self._render_models()
            return False
        reply = await self._load("models", "ListModels")
        if reply is None:
            return False
        models = reply[1].get("models")
        if (not self._success(reply) or not isinstance(models, list)
                or not all(isinstance(model, dict) and isinstance(model.get("id"), str) and model["id"]
                           and isinstance(model.get("name"), str) and model["name"]
                           and type(model.get("active")) is bool and type(model.get("selected")) is bool for model in models)
                or len({model["id"] for model in models}) != len(models)):
            self.models_verified = False
            self.models_error.set_text("Unable to update models; previous list may be stale. " + self._error(reply))
            self._render_models()
            return False
        self.models_error.set_text("")
        self.models = models
        self.models_verified = True
        self.serving_enabled = reply[1].get("serving_enabled")
        self.models_table.set_visibility(bool(models))
        self.models_empty.set_text("No installed models. Find a model to get started.")
        self.models_empty.set_visibility(not models)
        self._render_models()
        return True

    def _valid_job(self, job) -> bool:
        return (isinstance(job, dict) and isinstance(job.get("path"), str)
                and re.fullmatch(r"/io/github/puu_os/Oksa/jobs/[A-Za-z0-9_]+", job["path"]) is not None
                and isinstance(job.get("operation"), str) and bool(job["operation"])
                and job.get("state") in ("queued", "running", "succeeded", "failed", "canceled")
                and self._number(job.get("started_at")) and isinstance(job.get("progress"), str)
                and (job.get("ended_at") is None or self._number(job["ended_at"]))
                and (job.get("error") is None or isinstance(job["error"], dict))
                and (job.get("result") is None or isinstance(job["result"], dict)))

    def _valid_terminal_details(self, job, summary: dict) -> bool:
        return (self._valid_job(job) and job["state"] in ("succeeded", "failed", "canceled")
                and all(job.get(key) == summary.get(key) for key in
                        ("path", "operation", "state", "started_at", "ended_at", "repo_id", "model_name"))
                and self._number(job.get("ended_at")) and job["ended_at"] >= job["started_at"]
                and (job.get("result") is None or type(job["result"].get("ok")) is bool)
                and (isinstance(job.get("result"), dict) if job["state"] == "succeeded" else
                     isinstance(job.get("error"), dict) and isinstance(job["error"].get("code"), str)
                     and isinstance(job["error"].get("message"), str)))

    async def _load_pull(self) -> None:
        if not self._begin("activity-refresh", []):
            return
        try:
            requested = time.monotonic()
            reply = await self._load("pull", "ListJobs")
            if reply is None:
                return
            generation = self.generations["pull"]
            jobs = reply[1].get("jobs")
            if not self._success(reply) or not isinstance(jobs, list) or not all(self._valid_job(job) for job in jobs):
                self.jobs_failed = True
                self.pull_error.set_text("Activity unavailable; previous activity is unverified. " + self._error(reply))
                self._render_freshness()
                return
            self.jobs_received = requested
            self.jobs_failed = False
            self.pull_error.set_text("")
            self.jobs_seen = {job["path"] for job in jobs}
            previous = self.jobs.copy()
            previous_details = self.job_details.copy()
            for job in jobs:
                path = job["path"]
                old = self.jobs.get(path)
                if path not in self.job_details or not self._valid_terminal_details(old, job):
                    self.job_details.discard(path)
                    self.jobs[path] = job
            latest = {}
            for job in sorted(self.jobs.values(), key=lambda job: job["started_at"]):
                latest[job["operation"]] = job["path"]
            self.jobs = {path: job for path, job in self.jobs.items()
                         if path in latest.values() or path == self.model_job or job["state"] in ("queued", "running")}
            self.job_details.intersection_update(self.jobs)
            if any(job["state"] in ("queued", "running") for job in self.jobs.values()):
                self.models_verified = False
            if self.jobs != previous or self.job_details != previous_details or not self.job_widgets:
                self._render_activity()
            self._render_freshness()
            previous = self.jobs.copy()
            previous_details = self.job_details.copy()
            for path, job in list(self.jobs.items()):
                if job["state"] not in ("succeeded", "failed", "canceled") or path in self.job_details:
                    continue
                terminal = await self._call("GetJob", path, wait=False)
                if self.client.is_deleted or generation != self.generations["pull"]:
                    return
                if self._success(terminal) and self._valid_terminal_details(terminal[1], job):
                    self.jobs[path] = terminal[1]
                    self.job_details.add(path)
            if self.model_job:
                job = self.jobs.get(self.model_job)
                if job is None or self.model_job not in self.jobs_seen:
                    self.pull_error.set_text("Submitted job is unverified or missing. Do not retry the operation.")
                elif job["state"] in ("succeeded", "failed", "canceled"):
                    if self.model_job in self.job_details and job["operation"] == self.model_operation:
                        result = job.get("result") or {}
                        message = str((job.get("error") or {}).get("message") or result.get("warning") or result.get("error") or "")
                        if self.model_operation == "RemoveModel" and not (result.get("deleted") is True and result.get("reconciled") is True):
                            message = message or "Removal or reconciliation was not confirmed; files or serving may be partially changed."
                        if job["state"] != "succeeded" or result.get("ok") is not True or message:
                            self._warn("model-operation", f"{self.model_operation}: {job['state']}. " + message)
                        self.model_job = None
                        self.models_verified = False
                        await self._load_models()
                        await self._load_monitoring()
                        if self.client.is_deleted or generation != self.generations["pull"]:
                            return
                    else:
                        self.pull_error.set_text("Terminal job details are unverified (summary only). Do not retry.")
            if self.jobs != previous or self.job_details != previous_details:
                self._render_activity()
            self._render_freshness()
        finally:
            self._end("activity-refresh", [])

    def _render_activity(self) -> None:
        self.activity.clear()
        self.job_widgets = []
        with self.activity:
            if not self.jobs:
                ui.label("No recent jobs reported")
            for job in sorted(self.jobs.values(), key=lambda job: (job["state"] in ("queued", "running"), job["started_at"]), reverse=True):
                with ui.card().classes("w-full"):
                    with ui.row().classes("items-center"):
                        spinner = ui.spinner()
                        ui.label(job["operation"] + (" · " + str(job.get("repo_id") or job.get("model_name") or "")
                                                     if job["operation"] == "DownloadModel" else ""))
                        badge = ui.badge(job["state"])
                        elapsed = ui.label("")
                        cancel = ui.button("Cancel job", color="negative", on_click=lambda _, path=job["path"]: self._cancel_pull(path)).props("outline")
                    if job["state"] in ("succeeded", "failed", "canceled") and job["path"] not in self.job_details:
                        ui.label("Summary only · Full terminal details unverified").classes("text-warning")
                    ui.label(job.get("progress") or "No progress reported").classes("whitespace-pre-line break-words")
                    error = job.get("error") or {}
                    if error:
                        ui.label(str(error.get("message") or error)).classes("text-negative whitespace-pre-line break-words")
                    result = job.get("result") or {}
                    if result.get("warning") or result.get("error"):
                        ui.label(str(result.get("warning") or result.get("error"))).classes("text-warning break-words")
                    self.job_widgets.append((job, badge, spinner, elapsed, cancel))

    async def _cancel_pull(self, path: str) -> None:
        job = self.jobs.get(path)
        if not self._jobs_fresh() or path not in self.jobs_seen or not job or job["state"] != "running":
            return
        if not self._begin("cancel", []):
            return
        try:
            if not await self._confirm(f"Cancel {job['operation']}? Partial changes may remain.", "Cancel job", dangerous=True):
                return
            try:
                canceled = await run.io_bound(self.service.cancel_job, path)
                if not self.client.is_deleted:
                    ui.notify("Cancellation requested; checking job state" if canceled else "Cancellation was not confirmed", type="info" if canceled else "warning")
            except (OksaClientError, OSError, ValueError, TimeoutError) as error:
                self._warn("cancel", f"Could not confirm cancellation: {error}")
            await self._load_pull()
        finally:
            self._end("cancel", [])

    async def _model_action(self, action: str) -> None:
        model = self._selected_model()
        if not self._model_available() or not self.models_verified:
            return
        if action != "stop" and (model is None or not isinstance(model.get("id"), str)):
            return
        if action == "remove" and model.get("removable") is False:
            return
        if not self._begin("model", self.model_controls):
            return
        name = str(model.get("name")) if model else "model serving"
        try:
            message = {"start": f"Start {name}? This replaces the current serving selection.",
                       "stop": "Stop all model serving? Applications will lose inference access.",
                       "remove": f"Permanently remove {name}? If serving, it must be safely stopped before files are deleted."}[action]
            if not await self._confirm(message, {"start": "Start model", "stop": "Stop serving", "remove": "Remove model"}[action], dangerous=action != "start"):
                return
            if not self._jobs_fresh() or any(job["state"] in ("queued", "running") for job in self.jobs.values()):
                self._warn("model-operation", "Activity changed or became unverified. Refresh confirmed state before retrying.")
                return
            for key in ("models", "status", "pull", "calibration"):
                self.generations[key] += 1
            self.models_verified = False
            self.status_failed = True
            self.calibration_received = 0.0
            self.model_uncertain = True
            self._render_freshness()
            method = {"start": "StartModel", "stop": "StopServing", "remove": "RemoveModel"}[action]
            reply = await self._call(method, *(() if action == "stop" else (model["id"],)), wait=False)
            if self.client.is_deleted:
                return
            path = reply[1].get("job")
            if self._success(reply) and isinstance(path, str) and re.fullmatch(r"/io/github/puu_os/Oksa/jobs/[A-Za-z0-9_]+", path):
                self.model_job = path
                self.model_operation = method
                self.model_uncertain = False
                self._warn("model-operation", "")
                ui.notify("Operation accepted; following job state")
            else:
                self._warn("model-operation", f"{action.title()} acceptance was not confirmed. Refresh confirmed state before retrying. " + self._error(reply))
            await self._load_pull()
        finally:
            self._end("model", self.model_controls)
            if not self.client.is_deleted:
                self._model_selection()

    def _build_download_dialog(self) -> None:
        with ui.dialog() as self.download_dialog, ui.card().classes("w-full max-w-3xl"):
            ui.label("Find a model").classes("text-lg font-semibold").props("role=heading aria-level=2")
            ui.label("Browse downloadable AI models from Hugging Face.").classes("text-sm")
            with ui.row().classes("w-full items-start gap-2"):
                self.model_search = ui.input("Search models", placeholder="Model name",
                                             on_change=self._search_models).props("outlined clearable debounce=500").classes("grow")
                self.model_search.on("keydown.enter", self._search_models)
                self.search_button = ui.button("Search", icon="search", on_click=self._search_models)
            self.search_message = ui.label("Search by model name.").classes("text-sm")
            self.search_table = self._table("Search results", {
                "name": "Model", "author": "Publisher", "downloads": "Downloads", "access": "Access",
            }, selection="single", row_key="repo_id").props("flat dense wrap-cells")
            self.search_table.set_visibility(False)
            self.search_table.on_select(self._select_search_result)
            self.inspection_text = ui.label("Choose a model to review its download size.").classes("whitespace-pre-line break-words")
            self.download_error = ui.label("").classes("text-negative")
            with ui.expansion("Advanced", icon="tune").classes("w-full"):
                self.repo = ui.input("Model ID", placeholder="publisher/model", on_change=self._invalidate_inspection).props("outlined").classes("w-full")
                self.download_name = ui.input("Local name (optional)", on_change=self._invalidate_inspection).props("outlined").classes("w-full")
                self.revision = ui.input("Version or commit", value="main", on_change=self._invalidate_inspection).props("outlined").classes("w-full")
                self.inspection_revision = ui.label("").classes("text-sm font-mono break-all")
                self.inspect_button = ui.button("Review model", color="accent", on_click=self._inspect).props("outline")
            with ui.row().classes("w-full justify-end gap-2"):
                self.download_cancel = ui.button("Close", color="accent", on_click=self.download_dialog.close).props("flat")
                self.download_button = ui.button("Download", icon="download", on_click=self._download)
        self.download_button.disable()
        self.download_dialog.on_value_change(self._download_dialog_changed)

    def _download_dialog_changed(self) -> None:
        if not self.download_dialog.value:
            self.search_generation += 1
            self._invalidate_inspection()

    async def _open_download(self) -> None:
        if "download" in self.busy:
            return
        self._invalidate_inspection()
        self.download_error.set_text("")
        self.download_dialog.open()
        await self._search_models()

    async def _search_models(self) -> None:
        if not self.download_dialog.value or "download" in self.busy:
            return
        self.search_generation += 1
        generation = self.search_generation
        query = str(self.model_search.value or "").strip()
        self._invalidate_inspection()
        self.repo.set_value("")
        self.search_table.selected = []
        self.search_table.rows = []
        self.search_table.set_visibility(False)
        self.search_message.set_text("Searching…")
        self.download_error.set_text("")
        async with self.locks["search"]:
            if self.client.is_deleted or not self.download_dialog.value or generation != self.search_generation:
                return
            self.search_button.disable()
            try:
                reply = await self._call("SearchModels", query)
                if (self.client.is_deleted or not self.download_dialog.value or generation != self.search_generation
                        or query != str(self.model_search.value or "").strip()):
                    return
                models = reply[1].get("models")
                if (not self._success(reply) or not isinstance(models, list)
                        or not all(isinstance(model, dict) and isinstance(model.get("repo_id"), str)
                                   and isinstance(model.get("name"), str) and isinstance(model.get("author"), str)
                                   and self._number(model.get("downloads"), integer=True)
                                   and type(model.get("gated")) is bool for model in models)):
                    self.search_message.set_text("Search unavailable. " + self._error(reply))
                    return
                self.search_table.rows = [dict(model, access="Approval required" if model["gated"] else "Public") for model in models]
                self.search_table.update()
                self.search_table.set_visibility(bool(models))
                self.search_message.set_text("Select a model to review its download size." if models else "No models found. Try another model name.")
            finally:
                if not self.client.is_deleted:
                    self.search_button.enable()

    async def _select_search_result(self) -> None:
        if "download" in self.busy:
            return
        selected = self.search_table.selected
        self._invalidate_inspection()
        if not selected:
            self.repo.set_value("")
            return
        self.repo.set_value(selected[0]["repo_id"])
        self.revision.set_value("main")
        await self._inspect()

    def _download_inputs(self) -> tuple[str, str | None, str]:
        return (str(self.repo.value or "").strip(), str(self.download_name.value or "").strip() or None,
                str(self.revision.value or "").strip() or "main")

    def _invalidate_inspection(self) -> None:
        self.inspection_generation += 1
        self.inspection = None
        if hasattr(self, "download_button") and not self.client.is_deleted:
            self.download_button.disable()
            self.inspection_text.set_text("Choose a model to review its download size.")
            self.inspection_revision.set_text("")

    async def _inspect(self) -> None:
        if not self.download_dialog.value or "download" in self.busy:
            return
        self._invalidate_inspection()
        generation = self.inspection_generation
        inputs = self._download_inputs()
        self.inspection_text.set_text("Reviewing model…")
        self.download_error.set_text("")
        async with self.locks["inspect"]:
            if self.client.is_deleted or not self.download_dialog.value or generation != self.inspection_generation:
                return
            self._begin("inspect", [self.inspect_button])
            try:
                if not inputs[0]:
                    self.download_error.set_text("Choose a model from the search results or enter a model ID under Advanced.")
                    return
                reply = await self._call("InspectModel", inputs[0], inputs[2])
                if (self.client.is_deleted or not self.download_dialog.value or generation != self.inspection_generation
                        or inputs != self._download_inputs()):
                    return
                data = reply[1]
                metadata = data.get("metadata")
                if (not self._success(reply) or type(data.get("fits")) is not bool or not isinstance(metadata, dict)
                        or not isinstance(metadata.get("repo_id"), str) or not metadata["repo_id"].strip()
                        or not isinstance(metadata.get("resolved_revision"), str)
                        or re.fullmatch(r"[0-9a-fA-F]{40}", metadata["resolved_revision"]) is None):
                    self.inspection_text.set_text("Model review unavailable.")
                    self.download_error.set_text("Could not review the model; no download was started. " + self._error(reply))
                    return
                self.inspection = {"inputs": inputs, "generation": generation, "fits": data["fits"],
                                   "repo_id": metadata["repo_id"], "revision": metadata["resolved_revision"], "name": inputs[1]}
                author, _, name = metadata["repo_id"].rpartition("/")
                self.inspection_text.set_text(
                    f"{name or metadata['repo_id']}\nPublisher: {author or 'not specified'}"
                    + "\nDownload: " + self._storage(metadata.get("storage_mib"))
                    + "; estimated RAM/VRAM: " + self._storage(metadata.get("estimated_memory_mib"))
                    + "\n" + str(data.get("reason") or ("Within estimated host capacity" if data["fits"] else "Exceeds estimated host capacity"))
                    + "\nServing limits are measured after installation.")
                self.inspection_revision.set_text("Pinned commit: " + metadata["resolved_revision"])
                self.download_button.enable()
            finally:
                self._end("inspect", [self.inspect_button])
                if not self.client.is_deleted:
                    self._model_selection()

    async def _download(self) -> None:
        inspection = self.inspection
        if (not self.download_dialog.value or inspection is None or "inspect" in self.busy
                or not self._model_available()
                or inspection["inputs"] != self._download_inputs()
                or inspection["generation"] != self.inspection_generation):
            return
        controls = [self.download_button, self.inspect_button, self.download_cancel, self.repo, self.download_name,
                    self.revision, self.model_search, self.search_button]
        if not self._begin("download", controls):
            return
        self.download_dialog.props("persistent")
        try:
            if inspection["fits"] is False and not await self._confirm(
                f"Force download of incompatible model {inspection['repo_id']} @ {inspection['revision']}? It may not run on this host.",
                "Force download", dangerous=True,
            ):
                return
            if (not self.download_dialog.value or self.inspection is not inspection
                    or inspection["generation"] != self.inspection_generation
                    or inspection["inputs"] != self._download_inputs()):
                return
            if not self._jobs_fresh() or any(job["state"] in ("queued", "running") for job in self.jobs.values()):
                self.download_error.set_text("Activity changed or became unverified. Refresh confirmed state before downloading.")
                return
            self.inspection = None
            for key in ("models", "status", "pull", "calibration"):
                self.generations[key] += 1
            self.models_verified = False
            self.status_failed = True
            self.model_uncertain = True
            self._render_freshness()
            reply = await self._call("DownloadModel", inspection["repo_id"], inspection["name"] or "",
                                     inspection["revision"], inspection["fits"] is False, wait=False)
            if self.client.is_deleted:
                return
            path = reply[1].get("job")
            if self._success(reply) and isinstance(path, str) and re.fullmatch(r"/io/github/puu_os/Oksa/jobs/[A-Za-z0-9_]+", path):
                self.model_job = path
                self.model_operation = "DownloadModel"
                self.model_uncertain = False
                self._warn("download", "")
                self.download_dialog.close()
                ui.notify("Download accepted; following job state")
            else:
                message = "Download acceptance was not confirmed. Refresh confirmed state before any manual retry. " + self._error(reply)
                self.download_error.set_text(message)
                self._warn("download", message)
            await self._load_pull()
        finally:
            self._end("download", controls)
            if not self.client.is_deleted:
                self.download_dialog.props(remove="persistent")
                self._model_selection()

    def _build_users(self) -> None:
        self.users_error = ui.label("").classes("text-negative")
        with ui.column().classes("w-full") as self.users_list:
            with ui.row():
                self.create_user_button = ui.button("Create user", on_click=self._open_user)
                self.open_user_button = ui.button("Open selected user", on_click=self._open_selected_user)
                self.delete_users_button = ui.button("Delete selected users", color="negative", on_click=self._delete_selected_users).props("outline")
            self.delete_users_button.disable()
            self.open_user_button.disable()
            search = ui.input("Find user by ID or email")
            self.users_empty = ui.label("Loading users…")
            self.users_table = self._table("Users", {"user_id": "User ID", "email": "Email", "enabled": "Enabled keys"}, selection="multiple", row_key="user_id")
            self.users_table.set_visibility(False)
            self.users_table.on_select(self._users_selection)
            search.bind_value_to(self.users_table, "filter")
        with ui.column().classes("w-full") as self.user_detail:
            with ui.row():
                ui.button("All users", color="accent", on_click=self._show_users).props("outline")
                self.delete_user_button = ui.button("Delete user", color="negative", on_click=self._delete_user).props("outline")
                self.create_key_button = ui.button("Create key", on_click=self._open_key)
                self.revoke_button = ui.button("Revoke selected key", color="negative", on_click=self._revoke_key).props("outline")
            self.owner_label = ui.label("")
            self.keys_count = ui.label("Keys not loaded")
            self.keys_table = self._table("User API keys", {"name": "Name", "models": "Models", "status": "Status", "expires": "Expires"}, selection="single")
        self.user_detail.set_visibility(False)
        self.user_controls = [self.create_user_button, self.open_user_button, self.delete_users_button, self.delete_user_button, self.create_key_button, self.revoke_button]

    def _users_selection(self) -> None:
        count = len(self.users_table.selected)
        self.delete_users_button.set_text(f"Delete selected users ({count})" if count else "Delete selected users")
        self.delete_users_button.set_enabled(count > 0 and "user" not in self.busy)
        self.open_user_button.set_enabled(count == 1 and "user" not in self.busy)

    async def _open_selected_user(self) -> None:
        if len(self.users_table.selected) == 1:
            await self._show_user(self.users_table.selected[0]["user_id"])
    @staticmethod
    def _expiry(value) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return expiry.replace(tzinfo=timezone.utc) if expiry.tzinfo is None else expiry

    def _key_status(self, key: dict) -> str:
        if key.get("blocked"):
            return "Blocked"
        if key.get("expires"):
            expiry = self._expiry(key["expires"])
            if expiry is None:
                return "Expiry unknown"
            if expiry <= datetime.now(timezone.utc):
                return "Expired"
        return "Enabled"

    async def _load_users(self) -> None:
        self.generations["users"] += 1
        generation, owner = self.generations["users"], self.owner
        async with self.locks["users"]:
            if generation != self.generations["users"] or self.client.is_deleted:
                return
            users_reply, keys_reply = await asyncio.gather(
                self._call("ListUsers"),
                self._call("ListKeys", owner or ""),
            )
            if generation != self.generations["users"] or owner != self.owner or self.client.is_deleted:
                return
            errors = []
            users, keys = users_reply[1].get("users"), keys_reply[1].get("keys")
            users_ok = self._success(users_reply) and isinstance(users, list) and all(isinstance(user, dict) and isinstance(user.get("user_id"), str) for user in users)
            keys_ok = self._success(keys_reply) and isinstance(keys, list) and all(isinstance(key, dict) and isinstance(key.get("token"), str) for key in keys)
            if users_ok:
                self.users = [{"user_id": user["user_id"], "user_email": user.get("user_email")} for user in users]
            else:
                message = "Unable to refresh users; previous list may be stale. " if self.users else "Unable to load users. "
                errors.append(message + self._error(users_reply))
            if not keys_ok:
                errors.append("API keys unavailable. " + self._error(keys_reply))
            self.users_error.set_text(" ".join(errors))
            counts: dict[str, int] = {}
            if keys_ok:
                for key in keys:
                    if key.get("key_alias") != "open-webui" and self._key_status(key) == "Enabled":
                        counts[key.get("user_id")] = counts.get(key.get("user_id"), 0) + 1
            self.users_table.rows = [{"user_id": user["user_id"], "email": user.get("user_email") or "—",
                                      "enabled": str(counts.get(user["user_id"], 0)) if keys_ok and owner is None else "Unavailable"}
                                     for user in self.users]
            self.users_table.set_visibility(bool(self.users))
            self.users_empty.set_text("No users yet." if users_ok else "Users unavailable.")
            self.users_empty.set_visibility(not self.users)
            self.users_table.selected = []
            self._users_selection()
            self.users_table.update()
            self.keys = {}
            self.keys_table.selected = []
            self.keys_table.rows = []
            if owner is not None:
                user = next((user for user in self.users if user["user_id"] == owner), None)
                self.owner_label.set_text("User: " + owner + " · " + str(user.get("user_email") or "No email address") if user else "User: " + owner)
                if keys_ok:
                    self.keys = {f"{generation}:{index}": {
                        field: key.get(field) for field in ("token", "user_id", "key_alias", "key_name", "models", "blocked", "expires")
                    } for index, key in enumerate(keys)
                        if key.get("user_id") == owner and key.get("key_alias") != "open-webui"}
                    self.keys_table.rows = [{
                        "id": identity, "name": key.get("key_alias") or key.get("key_name") or "Unnamed key",
                        "models": ", ".join(str(model) for model in key["models"]) if isinstance(key.get("models"), list) and key["models"] else "All models (including future models)",
                        "status": self._key_status(key), "expires": str(key.get("expires")) if self._expiry(key.get("expires")) else "Unknown" if key.get("expires") else "Never",
                    } for identity, key in self.keys.items()]
                self.keys_count.set_text(f"{len(self.keys)} keys" if keys_ok else "API keys unavailable; reopen this user before acting.")
                if users_ok and user is None:
                    self.users_error.set_text("This user is no longer in the confirmed user list. Return to All users.")
            else:
                self.owner_label.set_text("")
                self.keys_count.set_text("Keys not loaded")
            self.keys_table.update()

    def _set_owner(self, owner: str | None) -> None:
        self.owner = owner
        self.keys = {}
        self.keys_table.rows = []
        self.keys_table.selected = []
        self.keys_table.update()
        self.owner_label.set_text("User: " + owner if owner is not None else "")
        self.keys_count.set_text("Loading API keys…" if owner is not None else "Keys not loaded")
        self.users_list.set_visibility(owner is None)
        self.user_detail.set_visibility(owner is not None)

    async def _show_user(self, owner: str) -> None:
        if not isinstance(owner, str) or "user" in self.busy or not any(user["user_id"] == owner for user in self.users):
            return
        self._set_owner(owner)
        await self._load_users()

    async def _show_users(self) -> None:
        if "user" in self.busy:
            return
        self._set_owner(None)
        await self._load_users()
    def _build_user_dialog(self) -> None:
        with ui.dialog() as self.user_dialog, ui.card():
            ui.label("Create user").classes("text-lg font-semibold uppercase").props("role=heading aria-level=2")
            self.new_user_id = ui.input("User ID")
            self.new_user_email = ui.input("Email (optional)", validation={
                "Enter a valid email address": lambda value: not value or re.fullmatch(r"[^@\s]+@[^@\s]+", value) is not None,
            }).props("type=email")
            self.new_user_error = ui.label("").classes("text-negative")
            with ui.row():
                self.user_cancel = ui.button("Cancel", color="accent", on_click=self.user_dialog.close).props("outline")
                self.user_submit = ui.button("Create user", on_click=self._create_user)

    def _open_user(self) -> None:
        if "user" in self.busy:
            return
        self.new_user_id.set_value("")
        self.new_user_email.set_value("")
        self.new_user_error.set_text("")
        self.user_dialog.open()

    async def _create_user(self) -> None:
        if not self.user_dialog.value:
            return
        owner = str(self.new_user_id.value or "").strip()
        email = str(self.new_user_email.value or "").strip()
        if not owner:
            self.new_user_error.set_text("User ID is required.")
            return
        if not self.new_user_email.validate():
            return
        controls = [*self.user_controls, self.user_submit, self.user_cancel, self.new_user_id, self.new_user_email]
        if not self._begin("user", controls):
            return
        self.user_dialog.props("persistent")
        self.generations["users"] += 1
        try:
            reply = await self._call("CreateUser", owner, email or "", -1.0)
            if self.client.is_deleted:
                return
            if self._success(reply) and isinstance(reply[1].get("result"), dict) and reply[1]["result"].get("user_id") == owner:
                self.user_dialog.close()
                ui.notify("User created")
                self._set_owner(owner)
            else:
                message = f"Creation of user {owner} was not confirmed. Check the user list before retrying. " + self._error(reply)
                self.new_user_error.set_text(message)
                self._warn("user-create:" + owner, message)
            await self._load_users()
        finally:
            self._end("user", controls)
            if not self.client.is_deleted:
                self.user_dialog.props(remove="persistent")

    async def _delete_user(self) -> None:
        if self.owner is not None:
            await self._delete_users([self.owner])

    async def _delete_selected_users(self) -> None:
        owners = [row.get("user_id") for row in self.users_table.selected]
        if not owners or any(not isinstance(owner, str) or not any(user["user_id"] == owner for user in self.users) for owner in owners):
            return
        await self._delete_users(list(dict.fromkeys(owners)))

    async def _delete_users(self, owners: list[str]) -> None:
        if not self._begin("user", self.user_controls):
            return
        try:
            message = (f"Permanently delete user {owners[0]} and ALL their API keys? This cannot be undone." if len(owners) == 1 else
                       f"Permanently delete {len(owners)} users and ALL their API keys? This cannot be undone.\n\n" + "\n".join(owners))
            if not await self._confirm(message, "Delete user" if len(owners) == 1 else f"Delete {len(owners)} users", dangerous=True):
                return
            if not self.client.has_socket_connection or not all(any(user["user_id"] == owner for user in self.users) for owner in owners):
                return
            if self.user_detail.visible and owners != [self.owner]:
                return
            self.generations["users"] += 1
            deleted = 0
            for index, owner in enumerate(owners):
                if self.client.is_deleted:
                    return
                if not self.client.has_socket_connection:
                    self._warn("user-delete:" + owner,
                               f"Browser disconnected during user deletion. Batch stopped: {deleted} deletion(s) confirmed; {len(owners) - index} user(s) not attempted.")
                    break
                reply = await self._call("DeleteUser", owner)
                if self.client.is_deleted:
                    return
                data = reply[1]
                if self._success(reply) and data.get("outcome") == "deleted" and data.get("deleted") is True:
                    deleted += 1
                    self.warnings.pop("user-delete:" + owner, None)
                    self.warning_label.set_text("\n".join(self.warnings.values()))
                    if self.owner == owner:
                        self._set_owner(None)
                else:
                    if data.get("outcome") in ("invalid", "protected", "not_started"):
                        message = self._error(reply)
                    else:
                        revoked = data.get("confirmed_revoked_keys")
                        summary = f" {revoked} key(s) confirmed revoked." if type(revoked) is int and revoked >= 0 else ""
                        message = str(data.get("warning") or
                                      f"Deletion of user {owner} was not confirmed; keys may have been revoked.{summary} Check the user list before retrying. " + self._error(reply))
                    if len(owners) > 1:
                        message = f"{owner}: {message} Batch stopped: {deleted} deletion(s) confirmed; {len(owners) - index - 1} user(s) not attempted."
                    self._warn("user-delete:" + owner, message)
                    break
            if deleted:
                ui.notify("User and their API keys deleted" if len(owners) == 1 else f"{deleted} users and their API keys deleted")
            await self._load_users()
        finally:
            self._end("user", self.user_controls)

    def _build_key_dialog(self) -> None:
        self.key_owner: str | None = None
        self.key_form_generation = 0
        self.key_model_names: set[str] = set()
        with ui.dialog() as self.key_dialog, ui.card().classes("w-full"):
            ui.label("Create key").classes("text-lg font-semibold uppercase").props("role=heading aria-level=2")
            self.key_owner_label = ui.label("")
            self.key_alias = ui.input("Name").classes("w-full")
            self.key_access = ui.select({"all": "All models (including future models)", "selected": "Selected models only"}, value="all", label="Allowed models")
            self.key_models = ui.select([], multiple=True, label="Installed models", with_input=True).classes("w-full")
            self.key_models.bind_visibility_from(self.key_access, "value", backward=lambda value: value == "selected")
            self.key_duration = ui.input("Expires in (optional, e.g. 30d, 24h)")
            self.key_model_error = ui.label("")
            self.key_error = ui.label("").classes("text-negative")
            with ui.row():
                self.key_cancel = ui.button("Cancel", color="accent", on_click=self.key_dialog.close).props("outline")
                self.key_submit = ui.button("Create key", on_click=self._create_key)
        self.key_dialog.on_value_change(lambda event: self._close_key_form() if not event.value else None)

    def _close_key_form(self) -> None:
        self.key_form_generation += 1
        self.key_owner = None
        self.key_model_names = set()

    async def _open_key(self) -> None:
        if self.owner is None or "user" in self.busy or self.key_dialog.value:
            return
        self.key_form_generation += 1
        generation = self.key_form_generation
        self.key_owner = self.owner
        self.key_owner_label.set_text("User: " + self.key_owner)
        self.key_alias.set_value("")
        self.key_duration.set_value("")
        self.key_access.set_value("all")
        self.key_models.set_options([], value=[])
        self.key_model_names = set()
        self.key_model_error.set_text("Loading installed models…")
        self.key_error.set_text("")
        self.key_dialog.open()
        async with self.locks["models"]:
            if self.client.is_deleted or generation != self.key_form_generation or not self.key_dialog.value:
                return
            reply = await self._call("ListModels")
        if self.client.is_deleted or generation != self.key_form_generation or not self.key_dialog.value:
            return
        models = reply[1].get("models")
        if self._success(reply) and isinstance(models, list) and all(isinstance(model, dict) and isinstance(model.get("name"), str) and model["name"] for model in models):
            self.key_model_names = {model["name"] for model in models}
            self.key_models.set_options(sorted(self.key_model_names), value=[])
            self.key_model_error.set_text("" if models else "No models installed. All models also permits future models.")
        else:
            self.key_model_error.set_text("Models unavailable. Close and reopen to retry, or explicitly choose All models.")

    async def _create_key(self) -> None:
        owner = self.key_owner
        if not self.key_dialog.value or owner is None or owner != self.owner:
            self.key_error.set_text("Open the intended user again before creating a key.")
            return
        alias = str(self.key_alias.value or "").strip()
        if not alias or alias == "open-webui":
            self.key_error.set_text("Enter a key name; open-webui is reserved for the service key.")
            return
        selected = self.key_access.value == "selected"
        models = list(self.key_models.value or []) if selected else []
        if self.key_access.value not in ("all", "selected") or (selected and (not models or not all(model in self.key_model_names for model in models))):
            self.key_error.set_text("Select at least one installed model, or explicitly choose All models.")
            return
        controls = [*self.user_controls, self.key_submit, self.key_cancel, self.key_alias, self.key_access, self.key_models, self.key_duration]
        if not self._begin("user", controls):
            return
        self.key_dialog.props("persistent")
        self.generations["users"] += 1
        generation = self.key_form_generation
        try:
            reply = await self._call("CreateKey", owner, alias, models, -1.0,
                                     str(self.key_duration.value or "").strip())
            data = reply[1].get("key")
            if self.client.is_deleted:
                if isinstance(data, dict):
                    data.pop("key", None)
                return
            if (self._success(reply) and isinstance(data, dict) and isinstance(data.get("key"), str) and data["key"].strip()
                    and ("user_id" not in data or data["user_id"] == owner)):
                connected = self.client.has_socket_connection and generation == self.key_form_generation
                self.key_dialog.close()
                self._clear_secret()
                if connected:
                    self.secret = data.pop("key")
                    self.secret_input.set_value(self.secret)
                    self.secret_owner_label.set_text(f"User: {owner}; key: {alias}")
                    self.secret_dialog.open()
                else:
                    data.pop("key", None)
                    self._warn("key-create:" + owner, f"Key created for {owner}, but the browser disconnected before the secret could be shown. Revoke it and create a replacement if needed.")
            else:
                if isinstance(data, dict):
                    data.pop("key", None)
                message = f"Key creation for {owner} was not confirmed. Check the key list before retrying."
                self.key_error.set_text(message)
                self._warn("key-create:" + owner, message)
            await self._load_users()
        finally:
            self._end("user", controls)
            if not self.client.is_deleted:
                self.key_dialog.props(remove="persistent")

    async def _revoke_key(self) -> None:
        owner = self.owner
        selected = self.keys_table.selected
        key = self.keys.get(selected[0].get("id")) if selected else None
        if owner is None or key is None or key.get("user_id") != owner or key.get("key_alias") == "open-webui" or not key.get("token"):
            return
        if not self._begin("user", self.user_controls):
            return
        try:
            if not await self._confirm(f"Revoke {key.get('key_alias') or key.get('key_name') or 'Unnamed key'} for {owner}? Applications using it will lose access.", "Revoke key", dangerous=True):
                return
            self.generations["users"] += 1
            reply = await self._call("RevokeKey", key["token"])
            if self.client.is_deleted:
                return
            if self._success(reply):
                ui.notify("API key revocation confirmed")
            else:
                self._warn("key-revoke:" + owner, f"Key revocation for {owner} was not confirmed; the key may still grant access. Check the key list before retrying. " + self._error(reply))
            await self._load_users()
        finally:
            self._end("user", self.user_controls)

    def _build_secret_dialog(self) -> None:
        with ui.dialog() as self.secret_dialog, ui.card().classes("w-full"):
            ui.label("Key created").classes("text-lg font-semibold uppercase").props("role=heading aria-level=2")
            ui.label("Shown once.").classes("text-warning")
            self.secret_owner_label = ui.label("")
            self.secret_input = ui.input("API key").props("readonly autocomplete=off").classes("w-full")
            with ui.row():
                ui.button("Copy", on_click=self._copy_secret)
                ui.button("Done", color="accent", on_click=self.secret_dialog.close).props("outline")
        self.secret_dialog.on_value_change(lambda event: self._clear_secret() if not event.value else None)

    def _copy_secret(self) -> None:
        if self.secret and self.secret_dialog.value:
            ui.clipboard.write(self.secret)
            ui.notify("Copy requested")

    def _clear_secret(self) -> None:
        self.secret = ""
        if hasattr(self, "secret_input") and not self.secret_input.is_deleted:
            self.secret_input.set_value("")
            self.secret_owner_label.set_text("")

    def _disconnect(self) -> None:
        self._clear_secret()
        self.search_generation += 1
        self._invalidate_inspection()
        self._close_key_form()
        for name in self.generations:
            self.generations[name] += 1
        self.status_failed = True
        self.jobs_failed = True
        self.models_verified = False
        self.calibration_received = 0.0
        self._render_freshness()
        if not self.secret_dialog.is_deleted:
            self.secret_dialog.close()
