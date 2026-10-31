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
        self.locks = {name: asyncio.Lock() for name in ("status", "pull", "models", "users", "settings")}
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
        self.pull_running = False
        self.pull_finished = None
        self.inspection: dict | None = None
        self.inspection_generation = 0
        self.settings_generation = 0
        self.settings_loaded = False
        self.secret = ""
        self.active_pull_job: str | None = None

    def build(self) -> None:
        ui.dark_mode(False)
        ui.colors(
            primary="#2B5B84", secondary="#2F6B6E", accent="#3E4C5E",
            dark="#242933", dark_page="#E3E7EE", positive="#276749",
            negative="#9E2A38", info="#2B5B84", warning="#8A4B08",
        )
        ui.add_css('''
            :root { color-scheme: light; }
            body, .q-tab-panels { background: #E3E7EE; color: #242933; }
            .q-card, .q-table__card, .q-markup-table, .q-menu, .q-select__dialog, .q-expansion-item {
                background: #ECEFF4; color: #242933;
            }
            .q-menu .q-item { color: #242933; }
            .q-field__label,
            .q-field__marginal,
            .q-table__bottom,
            .q-checkbox__inner:not(.q-checkbox__inner--truthy):not(.q-checkbox__inner--indet) {
                color: #4C566A;
            }
            .q-field__native,
            .q-field__input { color: #242933; }
            .q-field__native::placeholder,
            .q-field__input::placeholder { color: #4C566A; opacity: 1; }
            /* Important Quasar utilities are layered, so overrides must be layered too. */
            @layer overrides {
                .q-btn--standard,
                .q-notification,
                .q-notification .q-btn,
                .q-checkbox__inner--truthy .q-checkbox__svg,
                .q-checkbox__inner--indet .q-checkbox__svg {
                    color: #F4F6F9 !important;
                }
                :focus-visible:not(.q-field__native) {
                    outline: 2px solid #2B5B84 !important; outline-offset: 3px;
                }
            }
            .q-field:has(:focus-visible) { outline: 2px solid #2B5B84; outline-offset: 3px; }
        ''')
        ui.label("OKSA").classes("text-xl font-semibold")
        self.warning_label = ui.label("").classes("text-warning")
        with ui.tabs().classes("max-w-full") as self.tabs:
            for name in ("Monitoring", "Models", "Users", "Settings"):
                ui.tab(name)
        with ui.tab_panels(self.tabs, value="Monitoring").classes("w-full"):
            with ui.tab_panel("Monitoring"):
                self._build_monitoring()
            with ui.tab_panel("Models"):
                self._build_models()
            with ui.tab_panel("Users"):
                self._build_users()
            with ui.tab_panel("Settings"):
                self._build_settings()
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
            ui.label(message).classes("whitespace-pre-line break-words max-h-[60vh] overflow-y-auto")
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
            await self._load_models()
            await self._load_pull()
        elif self.tabs.value == "Users":
            await self._load_users()
        elif self.tabs.value == "Settings" and not self.settings_loaded:
            await self._load_settings()

    async def _poll(self) -> None:
        if self.client.is_deleted or not self.client.has_socket_connection:
            return
        if self.tabs.value == "Monitoring" and not self.locks["status"].locked():
            await self._load_monitoring()
        if self.tabs.value == "Models" and not self.locks["pull"].locked():
            await self._load_pull()

    def _build_monitoring(self) -> None:
        with ui.column().classes("w-full min-w-0 gap-1"):
            self.model_status = ui.label("Active model: Unavailable").classes("text-2xl font-semibold w-full break-words")
            self.inference = ui.label("Inference: Unavailable").classes("text-xl font-medium w-full break-words")
            self.selected_model = ui.label("Selected model: Unavailable").classes("text-sm text-[#4C566A] w-full break-words")
        with ui.row().classes("w-full items-center justify-between gap-2"):
            with ui.column().classes("min-w-0 gap-1"):
                self.freshness = ui.label("Waiting for first readings").classes("tabular-nums")
                self.api_status = ui.label("Admin API: waiting for first update").classes("text-sm text-[#4C566A]")
            ui.button("Refresh monitoring", color="accent", on_click=self._load_monitoring).props("outline")
        self.monitoring_error = ui.label("").classes("text-negative")
        with ui.grid().classes("w-full grid-cols-1 md:grid-cols-3 gap-6"):
            with ui.column().classes("min-w-0 gap-1"):
                ui.label("Host memory").classes("text-sm text-[#4C566A]")
                with ui.row().classes("w-full flex-nowrap items-baseline gap-1"):
                    self.memory = ui.label("Unavailable / Unavailable").classes("text-xl tabular-nums min-w-0 break-words")
                    ui.label("MiB").classes("text-sm text-[#4C566A]")
                self.memory_percent = ui.label("used / total · Usage unavailable").classes("text-sm text-[#4C566A] tabular-nums")
                self.memory_progress = ui.linear_progress(value=0, show_value=False).classes("w-full")
                self.memory_progress.set_visibility(False)
            with ui.column().classes("min-w-0 gap-1"):
                ui.label("Requests").classes("text-sm text-[#4C566A]")
                self.requests = ui.label("Unavailable / Unavailable").classes("text-xl tabular-nums")
                ui.label("running / waiting").classes("text-sm text-[#4C566A]")
            with ui.column().classes("min-w-0 gap-1"):
                ui.label("KV cache").classes("text-sm text-[#4C566A]")
                with ui.row().classes("w-full flex-nowrap items-baseline gap-1"):
                    self.kv_cache = ui.label("Unavailable").classes("text-xl tabular-nums")
                    ui.label("%").classes("text-sm text-[#4C566A]")
                ui.label("cache used").classes("text-sm text-[#4C566A]")
                self.kv_progress = ui.linear_progress(value=0, show_value=False).classes("w-full")
                self.kv_progress.set_visibility(False)
        self.gpus_label = ui.label("GPU data unavailable")
        self.gpus_table = self._table("GPUs", {
            "index": "Index", "name": "Name", "utilization": "Utilization", "memory": "Memory (MiB)",
            "temperature": "Temperature", "power": "Power",
        })
        self.workloads_label = ui.label("Workload data unavailable")
        self.workloads_table = self._table("Workloads", {
            "name": "Name", "phase": "Phase", "ready": "Ready", "restarts": "Restarts",
        })

    async def _load_monitoring(self) -> None:
        requested = time.monotonic()
        reply = await self._load("status", "GetStatus")
        if reply is None:
            return
        data = reply[1]
        if not self._success(reply) or "status" not in data or (data["status"] is not None and not isinstance(data["status"], dict)):
            self.status_failed = True
            self.api_status.set_text("Admin API: unavailable")
            self.monitoring_error.set_text("Monitoring update failed. " + self._error(reply))
            self._render_freshness()
            return
        self.status_failed = False
        self.api_status.set_text("Admin API: reachable")
        self.status = data.get("status")
        self.snapshot = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else {}
        self.status_received = requested
        self.monitoring_error.set_text(str(self.snapshot.get("error") or ""))
        self.partial = False
        status = self.status or {}
        if (status.get("model_status") not in ("stopped", "no-model", "not-ready", "ready")
                or status.get("gateway_status") not in ("ready", "authentication-required", "unreachable")):
            self.partial = True
        memory = status.get("host_memory") if isinstance(status.get("host_memory"), dict) else {}
        total = memory.get("total_mib")
        self.memory.set_text(self._metric(memory.get("used_mib")) + " / "
                             + self._metric(total if self._number(total) and total > 0 else None))
        self.memory_percent.set_text("used / total · " + self._metric(memory.get("used_pct"), "%", 100))
        self._progress(self.memory_progress, memory.get("used_pct"))
        self.model_status.set_text("Active model: " + self._text(status.get("active_model")))
        self.selected_model.set_text("Selected model: " + self._text(status.get("selected_model")))
        metrics = status.get("metrics") if isinstance(status.get("metrics"), dict) else {}
        self.requests.set_text(self._metric(metrics.get("running_requests"), integer=True) + " / "
                               + self._metric(metrics.get("waiting_requests"), integer=True))
        self.kv_cache.set_text(self._metric(metrics.get("kv_cache_usage_pct"), maximum=100))
        self._progress(self.kv_progress, metrics.get("kv_cache_usage_pct"))
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
        known = (self.status is not None and parsed is not None and parsed.tzinfo is not None
                 and self._number(age) and self._number(maximum) and maximum > 0
                 and type(metadata.get("stale")) is bool
                 and "error" in metadata and (metadata["error"] is None or isinstance(metadata["error"], str)))
        elapsed = age + max(0, time.monotonic() - self.status_received) if known else None
        stale = not known or self.status_failed or metadata.get("stale") or metadata.get("error") is not None or elapsed > maximum
        if known:
            self.freshness.set_text(("Stale · " if stale else "") + f"{int(elapsed)}s old · Sampled {sampled}"
                                    + (" · Some readings unavailable" if self.partial else ""))
        else:
            self.freshness.set_text("Freshness unavailable" if self.status is not None else
                                    "Update failed · No successful readings" if self.status_failed else
                                    "Collection failed · No readings" if metadata.get("error") else "Waiting for first readings")
        inference = "Unavailable"
        if self.status is not None:
            if stale:
                inference = "Unverified — stale readings" if known else "Unverified — freshness unavailable"
            else:
                model, gateway = self.status.get("model_status"), self.status.get("gateway_status")
                inference = {"stopped": "Stopped", "no-model": "No model loaded", "not-ready": "Model not ready"}.get(model, "Unavailable")
                if model == "ready":
                    inference = {"ready": "Ready", "authentication-required": "Unverified — gateway authentication required"}.get(gateway, "Gateway unavailable")
        self.inference.set_text("Inference: " + inference)

    def _build_models(self) -> None:
        with ui.row():
            ui.button("Refresh models", color="accent", on_click=self._load_models).props("outline")
            self.start_button = ui.button("Start selected", on_click=lambda: self._model_action("start"))
            self.stop_button = ui.button("Stop serving", color="negative", on_click=lambda: self._model_action("stop")).props("outline")
            self.remove_button = ui.button("Remove selected", color="negative", on_click=lambda: self._model_action("remove")).props("outline")
            ui.button("Download model", on_click=self._open_download)
        self.model_controls = [self.start_button, self.stop_button, self.remove_button]
        self.models_summary = ui.label("Catalog not loaded")
        self.models_error = ui.label("").classes("text-negative")
        self.models_table = self._table("Installed models", {
            "name": "Name", "repo_id": "Repository", "storage": "Storage", "quantization": "Quantization", "state": "State",
        }, selection="single")
        self.models_table.on_select(self._model_selection)
        with ui.row().classes("w-full items-center justify-between gap-2"):
            self.pull_summary = ui.label("Download activity not loaded")
            self.pull_cancel_button = ui.button("Cancel download", color="negative", on_click=self._cancel_pull).props("outline sm")
            self.pull_cancel_button.set_visibility(False)
        self.pull_error = ui.label("").classes("text-negative")
        self.pull_progress = ui.linear_progress(show_value=False).props("indeterminate").classes("w-full")
        self.pull_progress.set_visibility(False)
        with ui.expansion("Download log").classes("w-full"):
            self.pull_log = ui.textarea("Download log").props("readonly").classes("w-full")
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

    def _model_selection(self) -> None:
        model = self._selected_model()
        available = "model" not in self.busy
        self.start_button.set_enabled(available and model is not None and model.get("active") is not True)
        self.stop_button.set_enabled(available)
        self.remove_button.set_enabled(available and model is not None and model.get("removable") is not False)

    async def _load_models(self) -> None:
        reply = await self._load("models", "ListModels")
        if reply is None:
            return
        models = reply[1].get("models")
        if not self._success(reply) or not isinstance(models, list) or not all(isinstance(model, dict) and isinstance(model.get("id"), str) for model in models):
            self.models_error.set_text("Unable to refresh models; previous catalog may be stale. " + self._error(reply))
            return
        self.models_error.set_text("")
        self.models = models
        self.models_table.selected = []
        self.models_table.rows = [{
            "id": model["id"], "name": model.get("name"), "repo_id": model.get("repo_id"),
            "storage": self._storage(model.get("storage_mib")), "quantization": model.get("quantization") or "none",
            "state": "Serving" if model.get("active") is True else "Ready" if model.get("capabilities") == "ok" else "Insufficient host capabilities",
        } for model in models]
        self.models_table.update()
        active = next((model for model in models if model.get("active") is True), None)
        self.models_summary.set_text("Serving " + str(active.get("name")) if active else
                                     "Serving is stopped. No model is loaded." if reply[1].get("serving_enabled") is False else "No model is serving.")
        self._model_selection()

    async def _load_pull(self) -> None:
        reply = await self._load("pull", "ListJobs")
        if reply is None:
            return
        jobs = reply[1].get("jobs")
        if (not self._success(reply) or not isinstance(jobs, list)
                or not all(isinstance(job, dict) and self._number(job.get("started_at")) for job in jobs)):
            self.pull_error.set_text("Download activity unavailable; previous activity may be stale. " + self._error(reply))
            return
        downloads = [job for job in jobs if job.get("operation") == "DownloadModel"]
        job = max(downloads, key=lambda item: item["started_at"], default={})
        state = job.get("state")
        running = state in ("queued", "running")
        err_val = job.get("error")
        error = err_val.get("message") if isinstance(err_val, dict) else str(err_val or "")
        self.pull_error.set_text(error)
        label = "Downloading " if running else "Download failed: " if error else "Canceled " if state == "canceled" else "Completed " if state == "succeeded" else "No download in progress: "
        self.pull_summary.set_text(label + str(job.get("repo_id") or ""))
        self.active_pull_job = job.get("path") if running else None
        self.pull_cancel_button.set_visibility(running)
        self.pull_progress.set_visibility(running)
        self.pull_log.set_value(str(job.get("progress") or error))
        finished = job.get("ended_at")
        refresh = not running and (self.pull_running or (finished and finished != self.pull_finished))
        self.pull_running = running
        self.pull_finished = finished
        if refresh:
            await self._load_models()

    async def _cancel_pull(self) -> None:
        if not self.active_pull_job:
            return
        if not await self._confirm("Cancel model download?", "Cancel download", dangerous=True):
            return
        try:
            await run.io_bound(self.service.cancel_job, self.active_pull_job)
            ui.notify("Download canceled", type="info")
        except OksaClientError as error:
            ui.notify(f"Could not cancel download: {error}", type="negative")
        await self._load_pull()
    async def _model_action(self, action: str) -> None:
        model = self._selected_model()
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
            self.generations["models"] += 1
            self.generations["status"] += 1
            self.status_failed = True
            self._render_freshness()
            method = {"start": "StartModel", "stop": "StopServing", "remove": "RemoveModel"}[action]
            reply = await self._call(method, *(() if action == "stop" else (model["id"],)))
            if self.client.is_deleted:
                return
            data = reply[1]
            if action == "remove":
                if self._success(reply) and data.get("deleted") is True and type(data.get("reconciled")) is bool:
                    if data["reconciled"] is False:
                        self._warn("model:" + model["id"], str(data.get("warning") or "Model deleted, but reconciliation failed. Resolve the service error and refresh."))
                    else:
                        ui.notify("Model removed")
                else:
                    self._warn("model:" + model["id"], f"Removal of {name} was not confirmed; files or serving state may be partially changed. " + self._error(reply))
            elif not self._success(reply):
                self._warn("serving", f"{action.title()} was not confirmed. Refresh confirmed state before retrying. " + self._error(reply))
            else:
                ui.notify(str(data.get("message") or "Serving selection updated"))
            await self._load_models()
            await self._load_monitoring()
        finally:
            self._end("model", self.model_controls)
            if not self.client.is_deleted:
                self._model_selection()

    def _build_download_dialog(self) -> None:
        with ui.dialog() as self.download_dialog, ui.card().classes("w-full"):
            ui.label("Download model").classes("text-lg font-semibold uppercase").props("role=heading aria-level=2")
            self.repo = ui.input("Hugging Face repository", on_change=self._invalidate_inspection).classes("w-full")
            self.download_name = ui.input("Storage ID (optional)", on_change=self._invalidate_inspection).classes("w-full")
            self.revision = ui.input("Revision", value="main", on_change=self._invalidate_inspection).classes("w-full")
            self.inspection_text = ui.label("Inspect before downloading.")
            self.download_error = ui.label("").classes("text-negative")
            with ui.row():
                self.download_cancel = ui.button("Close", color="accent", on_click=self.download_dialog.close).props("outline")
                self.inspect_button = ui.button("Inspect", color="accent", on_click=self._inspect).props("outline")
                self.download_button = ui.button("Download inspected revision", on_click=self._download)
        self.download_button.disable()
        self.download_dialog.on_value_change(lambda event: self._invalidate_inspection() if not event.value else None)

    def _open_download(self) -> None:
        if "download" in self.busy:
            return
        self._invalidate_inspection()
        self.download_error.set_text("")
        self.download_dialog.open()

    def _download_inputs(self) -> tuple[str, str | None, str]:
        return (str(self.repo.value or "").strip(), str(self.download_name.value or "").strip() or None,
                str(self.revision.value or "").strip() or "main")

    def _invalidate_inspection(self) -> None:
        self.inspection_generation += 1
        self.inspection = None
        if hasattr(self, "download_button") and not self.client.is_deleted:
            self.download_button.disable()
            self.inspection_text.set_text("Inspect before downloading.")

    async def _inspect(self) -> None:
        if not self.download_dialog.value or "download" in self.busy or not self._begin("inspect", [self.inspect_button]):
            return
        self._invalidate_inspection()
        generation = self.inspection_generation
        inputs = self._download_inputs()
        self.download_error.set_text("")
        try:
            if not inputs[0]:
                self.download_error.set_text("Repository ID is required.")
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
                self.download_error.set_text("Inspection failed; no download was started. " + self._error(reply))
                return
            self.inspection = {"inputs": inputs, "generation": generation, "fits": data["fits"],
                               "repo_id": metadata["repo_id"], "revision": metadata["resolved_revision"], "name": inputs[1]}
            architectures = metadata.get("architectures")
            self.inspection_text.set_text(
                f"{metadata['repo_id']} @ {metadata['resolved_revision']}\n"
                + "Architecture: " + (", ".join(str(value) for value in architectures) if isinstance(architectures, list) and architectures else "unknown")
                + "\nStorage: " + self._storage(metadata.get("storage_mib"))
                + "; estimated RAM/VRAM: " + self._storage(metadata.get("estimated_memory_mib"))
                + "\n" + str(data.get("reason") or ("Compatible" if data["fits"] else "Incompatible with available capacity")))
            self.download_button.enable()
        finally:
            self._end("inspect", [self.inspect_button])

    async def _download(self) -> None:
        inspection = self.inspection
        if (not self.download_dialog.value or inspection is None or "inspect" in self.busy
                or inspection["inputs"] != self._download_inputs()
                or inspection["generation"] != self.inspection_generation):
            return
        controls = [self.download_button, self.inspect_button, self.download_cancel, self.repo, self.download_name, self.revision]
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
            self.inspection = None
            reply = await self._call("DownloadModel", inspection["repo_id"], inspection["name"] or "",
                                     inspection["revision"], inspection["fits"] is False, wait=False)
            if self.client.is_deleted:
                return
            if self._success(reply):
                self.download_dialog.close()
                ui.notify("Download started")
            else:
                message = "Download start was not confirmed. Check Download Activity before any manual retry. " + self._error(reply)
                self.download_error.set_text(message)
                self._warn("download", message)
            await self._load_pull()
        finally:
            self._end("download", controls)
            if not self.client.is_deleted:
                self.download_dialog.props(remove="persistent")
                self.download_button.set_enabled(self.inspection is not None)

    def _build_users(self) -> None:
        self.users_error = ui.label("").classes("text-negative")
        with ui.column().classes("w-full") as self.users_list:
            with ui.row():
                self.create_user_button = ui.button("Create user", on_click=self._open_user)
                self.delete_users_button = ui.button("Delete selected users", color="negative", on_click=self._delete_selected_users).props("outline")
            self.delete_users_button.disable()
            search = ui.input("Find user by ID or email")
            self.users_empty = ui.label("Loading users…")
            self.users_table = self._table("Users", {"user_id": "User ID", "email": "Email", "enabled": "Enabled keys"}, selection="multiple", row_key="user_id")
            self.users_table.set_visibility(False)
            self.users_table.on_select(self._users_selection)
            self.users_table.add_slot("body-cell-user_id", '''
                <q-td :props="props">
                    <q-btn flat dense no-caps color="primary" class="underline" :label="props.value"
                        @click.stop="$parent.$emit('open-user', props.row.user_id)" />
                </q-td>
            ''')
            self.users_table.on("open-user", lambda event: self._show_user(event.args))
            self.users_table.on("rowClick", lambda event: self._show_user(event.args), js_handler="(evt, row) => { if (!evt?.target?.closest?.('.q-table--col-auto-width, .q-checkbox')) emit(row.user_id); }")
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
        self.user_controls = [self.create_user_button, self.delete_users_button, self.delete_user_button, self.create_key_button, self.revoke_button]

    def _users_selection(self) -> None:
        count = len(self.users_table.selected)
        self.delete_users_button.set_text(f"Delete selected users ({count})" if count else "Delete selected users")
        self.delete_users_button.set_enabled(count > 0 and "user" not in self.busy)
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
        self._invalidate_inspection()
        self._close_key_form()
        for name in self.generations:
            self.generations[name] += 1
        if not self.secret_dialog.is_deleted:
            self.secret_dialog.close()

    def _build_settings(self) -> None:
        ui.label("Outbound HTTP contact").classes("text-lg font-semibold uppercase").props("role=heading aria-level=2")
        self.contact = ui.input("Contact", placeholder="https://helpdesk.example.org", on_change=self._settings_edited).classes("w-full")
        self.settings_effective = ui.label("Effective User-Agent: unavailable")
        self.settings_error = ui.label("").classes("text-negative")
        with ui.row():
            self.settings_refresh = ui.button("Refresh settings", color="accent", on_click=self._load_settings).props("outline")
            self.settings_save = ui.button("Save", on_click=lambda: self._save_settings(False))
            self.settings_clear = ui.button("Clear", color="negative", on_click=lambda: self._save_settings(True)).props("outline")
        self.settings_controls = [self.contact, self.settings_refresh, self.settings_save, self.settings_clear]

    def _settings_edited(self) -> None:
        self.settings_generation += 1

    async def _load_settings(self) -> None:
        if "settings" in self.busy:
            return
        generation = self.settings_generation
        reply = await self._load("settings", "GetSettings")
        if reply is None:
            return
        data = reply[1]
        if not self._success(reply) or not isinstance(data.get("settings"), dict) or not isinstance(data.get("effective_user_agent"), str):
            self.settings_error.set_text("Settings unavailable. " + self._error(reply))
            return
        self.settings_loaded = True
        self.settings_effective.set_text("Effective User-Agent: " + data["effective_user_agent"]
                                         + (" · Applies at next install" if data.get("manifest_applied") is False else ""))
        if generation == self.settings_generation:
            self.contact.set_value(str(data["settings"].get("http_user_agent_contact") or ""))
        self.settings_error.set_text("")

    async def _save_settings(self, clear: bool) -> None:
        contact = "" if clear else str(self.contact.value or "").strip()
        if not self._begin("settings", self.settings_controls):
            return
        self.generations["settings"] += 1
        try:
            reply = await self._call("SetContact", contact)
            if self.client.is_deleted:
                return
            data = reply[1]
            if self._success(reply) and isinstance(data.get("settings"), dict) and isinstance(data.get("effective_user_agent"), str):
                self.contact.set_value(str(data["settings"].get("http_user_agent_contact") or ""))
                self.settings_effective.set_text("Effective User-Agent: " + data["effective_user_agent"])
                self.settings_loaded = True
                self.settings_error.set_text("")
                ui.notify("Saved outbound HTTP contact" + (" (applies at next install)" if data.get("applied") is not True else ""))
            else:
                message = "Settings save was not confirmed. Refresh before retrying. " + self._error(reply)
                self.settings_error.set_text(message)
                self._warn("settings", message)
        finally:
            self._end("settings", self.settings_controls)
