# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

import asyncio
from collections.abc import Callable
import json
import math
import re
import time
from urllib.parse import urlencode
import uuid

from nicegui import core, run, ui

from oksa_client import OksaClient, OksaClientError
from oksa_control import Contract
import oksa_model_serving as ms
from oksa_web_kit import Emphasis, Kit, Meter, Tone
import oksa_web_views as views


class OksaWebUI:
    def __init__(self, service: OksaClient) -> None:
        self.service = service

    def mount(self) -> None:
        ui.add_css(Kit.STYLESHEET, shared=True)
        self._configure_root()
        core.app.on_startup(self._configure_root)
        for path in ("/", "/index.html"):
            ui.page(path, title="OKSA", language="en-US", dark=None)(self._page)

    def _configure_root(self) -> None:
        core.app.config.language = "en-US"
        core.app.colors(
            primary="var(--brand)", secondary="var(--ink-muted)", accent="var(--brand)",
            positive="var(--ok)", negative="var(--error)", warning="var(--warning)", info="var(--brand)",
            dark="var(--surface-alt)", dark_page="var(--surface)", on_primary="var(--on-brand)",
        )
        core.root = self._page

    def _page(self) -> None:
        _OksaPage(self.service).build()

class _OksaPage:
    NAVIGATION = (
        ("overview", "/", "Overview", "dashboard"),
        ("serving", "/serving", "Serving", "dns"),
        ("models", "/models", "Models", "inventory_2"),
        ("access", "/access", "API access", "vpn_key"),
        ("operations", "/operations", "Operations", "pending_actions"),
    )

    def __init__(self, service: OksaClient) -> None:
        self.service = service
        self.client = ui.context.client
        self.disconnected = False
        self.locks = {
            name: asyncio.Lock()
            for name in ("status", "daemon", "models", "calibration", "users", "search", "inspect", "jobs")
        }
        self.generations = dict.fromkeys(self.locks, 0)
        self.busy: set[str] = set()

        self.daemon_namespace: str = ""
        self.receipts: list[dict] = []
        self.unresolved: list[dict] = []

        self.snapshot: dict = {}
        self.status: dict | None = None
        self.status_received = 0.0
        self.status_failed = False
        self.status_stale = True
        self.monitoring_error_text = ""

        self.models: list[dict] = []
        self.models_loaded = False
        self.models_verified = False
        self.models_error_text = ""
        self.models_revision: str | None = None

        self.jobs: dict[str, dict] = {}
        self.jobs_loaded = False
        self.jobs_failed = False
        self.activity_error_text = ""

        self.users: list[dict] = []
        self.users_verified = False
        self.users_error_text = ""
        self.all_keys: list[dict] = []
        self.all_keys_verified = False
        self.all_keys_sampled_at: float | None = None
        self.all_keys_error_text = ""
        self.protected_users: set[str] = set()

        self.user_detail_id: str = ""
        self.user_detail_keys: list[dict] = []
        self.user_detail_keys_verified = False
        self.user_detail_error = ""

        self.calibration_model: str = ""
        self.calibration_data: dict | None = None
        self.calibration_error: str = ""

        self.search_query: str = ""
        self.search_results: list[dict] = []
        self.search_done: bool = False
        self.search_error: str = ""

        self.inspect_key: tuple[str, str] = ("", "")
        self.inspection: dict | None = None
        self.inspect_error: str = ""
        self.inspect_loading: bool = False

        self.secret: str = ""
        self.secret_meta: dict = {}

        self._active_updater: Callable[[], None] | None = None
        self._current_nav_section: str = "overview"

        # Pending modal state
        self._modal_action: dict = {}
        self._pending_request_id: str = ""
        self._review_request_id: str = ""
        self._cal_request_id: str = ""
        self._cal_force: bool = False

        # Form inputs tracked by server handlers
        self._create_user_id: str = ""
        self._create_user_email: str = ""
        self._create_key_alias: str = ""
        self._create_key_scope_mode: str = "selected"
        self._create_key_selected_models: set[str] = set()
        self._create_key_duration: str = "30d"
        self._revoke_key_target: dict = {}
        self._cancel_job_path: str = ""

    def build(self) -> None:
        with ui.header(wrap=False).classes("masthead"):
            ui.button(icon="menu", color=None, on_click=lambda: self.drawer.toggle()).props(
                'flat round dense aria-label="Toggle navigation"'
            )
            ui.link("OKSA", "/").classes(replace="brand")
            self.host = ui.html("", sanitize=False).classes("host")
            ui.space()
            self.activity = ui.html("", sanitize=False).classes("activity-slot")
            with ui.button(color=None).props("flat no-caps").classes("health"):
                self.health_summary = ui.html("", sanitize=False)
                with ui.menu().props('anchor="bottom right" self="top right"'):
                    self.health_panel = (
                        ui.html("", sanitize=False, tag="section")
                        .classes("health-panel")
                        .props('aria-labelledby="health-heading"')
                    )

        self.nav_items: dict[str, ui.item] = {}
        with ui.left_drawer(bordered=True).props(":width=208") as self.drawer:
            with ui.element("nav").classes("nav").props('aria-label="Primary"'), ui.list():
                for key, href, label, icon in self.NAVIGATION:
                    with ui.item().props(f'href="{href}"') as self.nav_items[key]:
                        with ui.item_section().props("avatar"):
                            ui.icon(icon)
                        ui.item_section(label)
                        if key == "operations":
                            with ui.item_section().props("side"):
                                self.running_badge = ui.badge().props("text-color=on-primary")

        self.main = ui.element("div").classes("page").props('tabindex="-1"')
        ui.skip_link("Skip to content", target=self.main)
        with self.main:
            self.global_banners = ui.html("", sanitize=False)
            self.sub_pages = ui.sub_pages(
                {
                    "/": self._route_overview,
                    "/index.html": self._route_overview,
                    "/serving": self._route_serving,
                    "/models": self._route_models,
                    "/models/download": self._route_download_search,
                    "/models/download/review": self._route_download_review,
                    "/models/{storage_id}": self._route_model_detail,
                    "/access": self._route_access,
                    "/access/user": self._route_user_detail,
                    "/operations": self._route_operations,
                    "/operations/{job_id}": self._route_operation_detail,
                },
            )

        self._build_modals()
        self.client.sub_pages_router.on_path_changed(self._on_path_changed)
        self.client.on_connect(self._on_connect)
        self.client.on_disconnect(self._on_disconnect)
        self.client.on_delete(self._clear_secret)
        self._render_shell()
        ui.timer(1.5, self._render_all)
        ui.timer(2.5, self._poll)

    def _host_name(self) -> str:
        if isinstance(self.status, dict) and self.status.get("host"):
            return str(self.status["host"])
        return "puu"

    def _api_base_url(self) -> str:
        if isinstance(self.status, dict) and self.status.get("api_base_url"):
            return str(self.status["api_base_url"])
        return "http://api.puu.local/v1"

    def _running_jobs(self) -> list[dict]:
        return [
            j for j in self.jobs.values()
            if isinstance(j, dict) and j.get("state") == "running"
        ]

    def _running_model_job(self) -> dict | None:
        for job in self._running_jobs():
            if job.get("operation") in Contract.MODEL_MUTATIONS:
                return job
        return None

    def _unresolved_for_scope(self, scope: str) -> dict | None:
        for item in self.unresolved:
            if isinstance(item, dict) and item.get("scope") == scope:
                return item
        return None

    def _model_block_reason(self) -> str:
        """Why model changes cannot be submitted now; empty when they can."""
        if self.disconnected or not self.models_verified or self.status is None or self.status_stale:
            return "Waiting for current serving status and model inventory before allowing changes."
        unresolved = self._unresolved_for_scope("model")
        if unresolved is not None:
            return f"Blocked until the unknown outcome of request {unresolved.get('request_id')} is resolved."
        running = self._running_model_job()
        if running is not None:
            return f"Blocked while {views.job_short_id(running.get('path'))} is running."
        return ""

    def _model_state(self) -> tuple[str, Tone]:
        return {
            "ready": ("Healthy", Tone.OK),
            "not-ready": ("Starting", Tone.BUSY),
            "stopped": ("Stopped", Tone.NEUTRAL),
        }.get(str((self.status or {}).get("model_status")), ("Unavailable", Tone.ERROR))

    def _health(self) -> views.Health:
        status = self.status if isinstance(self.status, dict) else None
        updated = f"Updated {views.fmt_ago(self.snapshot.get('sampled_at'), time.time())}"
        readings = []
        if self.unresolved:
            readings.append(views.Reading(
                "Blocked operations", f"{len(self.unresolved)} unresolved", Tone.ERROR,
                "Related changes stay blocked until each is resolved.", href="/operations",
            ))
        if self.disconnected or self.status_failed:
            detail = self.monitoring_error_text or updated
            readings.append(views.Reading("Status", "Unavailable", Tone.ERROR, detail, href="/serving"))
        elif status is None:
            readings.append(views.Reading("Status", "Waiting for first update", Tone.NEUTRAL, href="/serving"))
        elif self.status_stale:
            readings.append(views.Reading("Status", "Out of date", Tone.WARNING, updated, href="/serving"))
        else:
            readings.append(views.Reading("Status", "Current", Tone.OK, updated, href="/serving"))

        if status is not None:
            readings.append(views.Reading(
                "Model", *self._model_state(), str(status.get("active_model") or ""), href="/serving",
            ))
            gateway = {
                "ready": ("Ready", Tone.OK),
                "authentication-required": ("Reachable", Tone.OK),
            }.get(str(status.get("gateway_status")), ("Unreachable", Tone.ERROR))
            readings.append(views.Reading("Gateway", *gateway, href="/serving"))
            authentication = {
                "enforced": ("Enforced", Tone.OK),
                "keyless": ("Keyless access", Tone.WARNING),
            }.get(str(status.get("auth_enforcement")), ("Not verified", Tone.WARNING))
            readings.append(views.Reading("Authentication", *authentication, href="/access"))

        if self.models_error_text:
            readings.append(views.Reading("Inventory", "Read error", Tone.WARNING, self.models_error_text, href="/models"))
        elif self.models_loaded:
            readings.append(views.Reading("Inventory", f"{len(self.models)} installed", Tone.OK, href="/models"))
        else:
            readings.append(views.Reading("Inventory", "Loading…", Tone.NEUTRAL, href="/models"))

        running = len(self._running_jobs())
        retained = f"{len(self.jobs)} recent"
        if self.jobs_failed:
            readings.append(views.Reading(
                "Operations", "Read error", Tone.WARNING, self.activity_error_text, href="/operations",
            ))
        elif running:
            readings.append(views.Reading("Operations", f"{running} running", Tone.BUSY, retained, href="/operations"))
        elif not self.jobs_loaded:
            readings.append(views.Reading("Operations", "Loading…", Tone.NEUTRAL, href="/operations"))
        else:
            readings.append(views.Reading("Operations", "Idle", Tone.OK, retained, href="/operations"))
        return views.Health(tuple(readings))

    def _render_global_banners(self) -> str:
        parts = []
        now = time.time()
        if self.disconnected or self.status_failed:
            err = self.monitoring_error_text or self.snapshot.get("error") or "Daemon connection interrupted."
            parts.append(
                f'<div class="notice error" role="alert">'
                f"<p><strong>Status updates are interrupted.</strong> {views.esc(err)}</p>"
                f'<p class="small">Last update {views.fmt_time(self.snapshot.get("sampled_at"), now)}. '
                "The values shown may be out of date, and changes that depend on them are disabled.</p>"
                f"</div>"
            )
        if self.unresolved:
            parts.append(views.render_unresolved_banner(self.unresolved, now))
        return "".join(parts)

    def _render_shell(self) -> None:
        if self.client.is_deleted:
            return
        health = self._health()
        self.host.content = f"Host <strong>{views.esc(self._host_name())}</strong>"
        self.health_summary.content = f'<span class="visually-hidden">System health: </span>{health.summary_html()}'
        self.health_panel.content = health.panel_html()
        running = len(self._running_jobs())
        self.running_badge.set_text(str(running))
        self.running_badge.props["aria-label"] = f"{running} running operations"
        self.running_badge.set_visibility(running > 0)
        self.activity.content = views.render_activity(
            [job for job in self.jobs.values() if isinstance(job, dict)], time.time()
        )
        for key, item in self.nav_items.items():
            if key == self._current_nav_section:
                item.props('aria-current="page"')
            else:
                item.props(remove="aria-current")
        self.global_banners.content = self._render_global_banners()

    def _render_all(self) -> None:
        if self.client.is_deleted:
            return
        if not self.status_stale and self.status is not None:
            try:
                max_age = float(self.snapshot.get("max_age_seconds") or 10.0)
                base_age = float(self.snapshot.get("age_seconds") or 0.0)
            except (TypeError, ValueError):
                max_age = 10.0
                base_age = 0.0
            if self.status_received <= 0 or (base_age + max(0.0, time.monotonic() - self.status_received)) > max_age:
                self.status_stale = True
        self._render_shell()
        if self._active_updater is not None:
            self._active_updater()

    def _on_path_changed(self, path: str) -> None:
        self._clear_secret()
        clean = path.split("?", 1)[0].split("#", 1)[0].rstrip("/") or "/"
        if clean.startswith("/models"):
            self._current_nav_section = "models"
        elif clean.startswith("/access"):
            self._current_nav_section = "access"
        elif clean.startswith("/operations"):
            self._current_nav_section = "operations"
        elif clean.startswith("/serving"):
            self._current_nav_section = "serving"
        else:
            self._current_nav_section = "overview"
        self._render_shell()
        self.client.run_javascript(
            f"if (Quasar.Screen.lt.md) getElement({self.drawer.id}).hide();"
            f"requestAnimationFrame(() => getHtmlElement({self.main.id})?.focus({{preventScroll: true}}));"
        )

    def _set_page_title(self, title: str) -> None:
        full = f"{title} · OKSA"
        self.client.run_javascript(f"document.title = {json.dumps(full)};")

    def _navigate(self, path: str) -> None:
        ui.navigate.to(path)

    def _replace_url(self, path: str) -> None:
        if not self.client.is_deleted:
            self.client.run_javascript(f"window.history.replaceState(null, '', {json.dumps(path)});")

    @staticmethod
    def _success(reply: tuple[int, dict]) -> bool:
        return 200 <= reply[0] < 300 and reply[1].get("ok") is True

    @staticmethod
    def _error(reply: tuple[int, dict]) -> str:
        return str(reply[1].get("error") or f"Service request failed (status {reply[0]})")

    @staticmethod
    def _unconfirmed(reply: tuple[int, dict]) -> bool:
        return reply[1].get("code") in ("Unconfirmed", "Timeout", "Interrupted", "WorkerFailed")

    async def _call(self, method: str | Callable[..., dict], *args) -> tuple[int, dict]:
        try:
            if isinstance(method, str):
                result = await run.io_bound(self.service.call, method, *args)
            else:
                result = await run.io_bound(method, *args)
        except OksaClientError as error:
            return 0, {**error.payload, "ok": False, "code": error.code, "error": str(error)}
        except (OSError, ValueError, TimeoutError) as error:
            return 0, {"ok": False, "code": "Unconfirmed", "error": f"Service result could not be confirmed: {error}"}
        if not isinstance(result, dict):
            return 0, {"ok": False, "code": "Unconfirmed", "error": "Service result could not be confirmed."}
        return 200, result

    async def _load(self, name: str, method: str | Callable[..., dict], *args) -> tuple[int, dict] | None:
        cancel_stale = name in ("calibration", "search", "inspect")
        self.generations[name] += 1
        gen = self.generations[name]
        async with self.locks[name]:
            if self.client.is_deleted or (cancel_stale and gen != self.generations[name]):
                return None
            reply = await self._call(method, *args)
            if self.client.is_deleted or (cancel_stale and gen != self.generations[name]):
                return None
            return reply

    async def _ensure_namespace(self) -> str:
        if not self.daemon_namespace:
            await self._refresh_daemon_state()
        return self.daemon_namespace

    def _mint_request_id(self) -> str:
        if not self.daemon_namespace or not re.fullmatch(r"[0-9a-f]{32}", self.daemon_namespace):
            return ""
        return f"{self.daemon_namespace}:{uuid.uuid4().hex}"

    async def _refresh_daemon_state(self) -> None:
        reply = await self._load("daemon", "GetDaemonState")
        if reply is None:
            return
        if self._success(reply):
            data = reply[1]
            self.daemon_namespace = str(data.get("namespace") or "")
            self.receipts = [r for r in (data.get("receipts") or []) if isinstance(r, dict)]
            self.unresolved = [r for r in (data.get("unresolved") or []) if isinstance(r, dict)]

    async def _refresh_status(self) -> None:
        reply = await self._load("status", "GetStatus")
        if reply is None:
            return
        if not self._success(reply):
            self.status_failed = True
            self.status_stale = True
            self.monitoring_error_text = self._error(reply)
            return
        data = reply[1]
        snap = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else {}
        self.snapshot = snap
        if isinstance(data.get("status"), dict):
            self.status = data["status"]
        self.monitoring_error_text = str(snap.get("error") or "")
        self.status_failed = bool(self.monitoring_error_text)
        self.status_stale = bool(snap.get("stale")) or self.status is None or self.status_failed
        if not self.status_stale and isinstance(data.get("status"), dict):
            self.status_received = time.monotonic()

    async def _refresh_models(self) -> None:
        reply = await self._load("models", "ListModels")
        if reply is None:
            return
        if not self._success(reply):
            self.models_verified = False
            self.models_error_text = self._error(reply)
            return
        models = reply[1].get("models")
        if isinstance(models, list):
            self.models = [m for m in models if isinstance(m, dict)]
            self.models_loaded = True
            self.models_verified = True
            self.models_error_text = ""

    async def _refresh_jobs(self) -> None:
        reply = await self._load("jobs", self.service.get_managed_objects)
        if reply is None:
            return
        if reply[0] != 200:
            self.jobs_failed = True
            self.activity_error_text = self._error(reply)
            return
        managed = reply[1]
        paths = [
            p for p, ifaces in managed.items()
            if isinstance(p, str) and isinstance(ifaces, dict) and Contract.JOB in ifaces
        ]
        updated: dict[str, dict] = {}
        for path in paths:
            snap_reply = await self._call(self.service.get_job, path)
            if not self._success(snap_reply):
                self.jobs_failed = True
                self.activity_error_text = self._error(snap_reply)
                return
            updated[path] = snap_reply[1]
        self.jobs = updated
        self.jobs_loaded = True
        self.jobs_failed = False
        self.activity_error_text = ""

    async def _refresh_access(self) -> None:
        u_reply = await self._load("users", "ListUsers")
        if u_reply is not None:
            if self._success(u_reply):
                self.users = [u for u in (u_reply[1].get("users") or []) if isinstance(u, dict)]
                self.users_verified = True
                self.users_error_text = ""
            else:
                self.users_verified = False
                self.users_error_text = self._error(u_reply)

        k_reply = await self._call("ListKeys", "")
        if self._success(k_reply):
            self.all_keys = [k for k in (k_reply[1].get("keys") or []) if isinstance(k, dict)]
            self.all_keys_verified = k_reply[1].get("complete") is not False
            self.all_keys_sampled_at = k_reply[1].get("sampled_at")
            self.protected_users = {str(u) for u in (k_reply[1].get("protected_users") or []) if u}
            self.all_keys_error_text = ""
        else:
            self.all_keys_verified = False
            self.all_keys_error_text = self._error(k_reply)

    async def _refresh_user_detail(self, user_id: str) -> None:
        if not user_id:
            return
        await self._refresh_access()
        k_reply = await self._call("ListKeys", user_id)
        if self._success(k_reply):
            self.user_detail_id = user_id
            self.user_detail_keys = [k for k in (k_reply[1].get("keys") or []) if isinstance(k, dict)]
            self.user_detail_keys_verified = True
            self.user_detail_error = ""
            for u in k_reply[1].get("protected_users") or []:
                if u:
                    self.protected_users.add(str(u))
        else:
            self.user_detail_id = user_id
            self.user_detail_keys_verified = False
            self.user_detail_error = self._error(k_reply)
        self._render_all()

    async def _refresh_calibration(self, model_id: str) -> None:
        if not model_id:
            return
        reply = await self._load("calibration", "GetCalibration", model_id)
        if reply is None:
            return
        self.calibration_model = model_id
        if self._success(reply):
            self.calibration_data = reply[1]
            self.calibration_error = ""
        else:
            self.calibration_data = None
            self.calibration_error = self._error(reply)
        self._render_all()

    async def _on_connect(self) -> None:
        self.disconnected = False
        await self._poll()

    def _on_disconnect(self) -> None:
        self.disconnected = True
        self.status_stale = True
        self._clear_secret()
        self._render_all()

    async def _poll(self) -> None:
        if self.client.is_deleted or "poll" in self.busy:
            return
        self.busy.add("poll")
        try:
            await self._refresh_daemon_state()
            await self._refresh_status()
            await self._refresh_jobs()
            if not self.models_loaded or self._current_nav_section in ("overview", "models", "serving", "access"):
                await self._refresh_models()
            if self._current_nav_section == "access":
                await self._refresh_access()
                if self.user_detail_id:
                    await self._refresh_user_detail(self.user_detail_id)
            if self._current_nav_section == "models" and self.calibration_model:
                if any(m.get("id") == self.calibration_model for m in self.models):
                    await self._refresh_calibration(self.calibration_model)
        finally:
            self.busy.discard("poll")
            self._render_all()

    async def _submit_mutation(
        self,
        request_id: str,
        method: str,
        target: str,
        *args,
    ) -> tuple[bool, dict, str]:
        """Submit a mutation with durable request_id and handle Unconfirmed outcomes safely."""
        if not request_id:
            await self._ensure_namespace()
            request_id = self._mint_request_id()
        if not request_id:
            return False, {}, "Daemon request namespace is unavailable; check service connection before submitting."

        reply = await self._call(method, request_id, *args)
        await self._refresh_daemon_state()
        if self._success(reply):
            if method in Contract.MODEL_MUTATIONS or method == "DeleteUser":
                await self._refresh_jobs()
            return True, reply[1], ""

        if self._unconfirmed(reply):
            err_msg = self._error(reply)
            rec_reply = await self._call("RecordUnconfirmed", request_id, method, target, err_msg)
            await self._refresh_daemon_state()
            await self._refresh_jobs()
            if self._success(rec_reply):
                rec = rec_reply[1].get("receipt") or {}
                if rec.get("job_path"):
                    return True, {"ok": True, "job": rec["job_path"]}, ""
                if rec.get("state") == "succeeded" and method != "CreateKey":
                    return True, {"ok": True, "message": rec.get("summary") or "Completed"}, ""
                if rec.get("state") == "succeeded" and method == "CreateKey":
                    return (
                        False,
                        rec,
                        f"Key creation completed on the gateway ({rec.get('summary')}), but the one-time secret response was lost in transit. "
                        "Open the user's keys, revoke that key, and create a replacement.",
                    )
            return (
                False,
                reply[1],
                f"Outcome unknown (request {request_id}): {err_msg}. Related changes stay blocked until it is resolved.",
            )

        if reply[1].get("code") == "ExpiredNamespace":
            await self._refresh_daemon_state()
            return False, reply[1], "Request namespace rotated; review current state and submit again."

        return False, reply[1], self._error(reply)

    async def _copy_text(self, text: str) -> bool:
        """Copy through Quasar, which falls back to execCommand outside secure contexts."""
        try:
            return bool(await self.client.run_javascript(
                f"return Quasar.copyToClipboard({json.dumps(text)}).then(() => true, () => false);"
            ))
        except TimeoutError:
            return False

    async def _copy_api_base_url(self) -> None:
        if await self._copy_text(self._api_base_url()):
            ui.notify("API base URL copied", type="positive")
        else:
            ui.notify("The browser refused clipboard access; copy the URL manually", type="warning")

    def _request_metrics(self) -> dict:
        metrics = (self.status or {}).get("metrics")
        return metrics if isinstance(metrics, dict) else {}

    def _show_memory(self, meter: Meter) -> None:
        memory = (self.status or {}).get("host_memory")
        memory = memory if isinstance(memory, dict) else {}
        used, total = memory.get("used_mib"), memory.get("total_mib")
        if isinstance(used, int) and isinstance(total, int) and total > 0:
            sample_note = "out of date" if self.status_stale else "current"
            meter.show(
                f"{used / 1024:.1f} / {total / 1024:.1f} GiB",
                f"{used / total:.0%} host RAM used · {sample_note}",
                used / total,
            )
        else:
            meter.show("Unavailable", "Host memory reading unavailable")

    def _show_kv_cache(self, meter: Meter) -> None:
        cache_pct = self._request_metrics().get("kv_cache_usage_pct")
        if isinstance(cache_pct, (int, float)) and math.isfinite(cache_pct):
            meter.show(f"{cache_pct:g} %", "Reported vLLM cache usage", cache_pct / 100)
        else:
            meter.show("Unavailable", "Reported vLLM cache usage")

    def _route_overview(self) -> None:
        self._current_nav_section = "overview"
        self._set_page_title("Overview")

        with ui.element("header").classes("page-heading"):
            with ui.element("div"):
                ui.html("<h1>Overview</h1>", sanitize=False)
                ui.html('<p class="subtitle">Appliance status at a glance. Each item links to its details.</p>', sanitize=False)
            with ui.element("div").classes("actions"):
                Kit.button("Refresh", self._poll, icon="refresh")

        with ui.element("section").props('aria-labelledby="status-heading"'):
            ui.html('<div class="section-head"><h2 id="status-heading">Status</h2></div>', sanitize=False)
            tiles = ui.html("", sanitize=False)

        with ui.element("section").classes("section").props('aria-labelledby="glance-heading"'):
            ui.html('<div class="section-head"><h2 id="glance-heading">Serving &amp; resources</h2></div>', sanitize=False)
            with ui.element("div").classes("metrics"):
                with Kit.card("Serving model", icon="dns"):
                    model_box = ui.html("", sanitize=False)
                    ui.link("Manage serving →", "/serving").classes("card-link")
                with Kit.card("API endpoint", icon="api"):
                    endpoint_box = ui.html("", sanitize=False)
                    with ui.element("div").classes("actions"):
                        Kit.button("Copy", self._copy_api_base_url, icon="content_copy").props(
                            'dense aria-label="Copy API base URL"'
                        )
                        ui.link("API keys →", "/access").classes("card-link")
                with Kit.card("Host memory", icon="memory"):
                    memory_meter = Kit.meter("Host memory used")
                    ui.link("Serving details →", "/serving").classes("card-link")
                with Kit.card("KV cache", icon="storage"):
                    cache_meter = Kit.meter("KV cache usage")
                    ui.link("Serving details →", "/serving").classes("card-link")

        def update() -> None:
            health = self._health()
            tiles.content = health.tiles_html()
            status = self.status if isinstance(self.status, dict) else None
            if status is None:
                model_box.content = f'<p class="meter-value">Unknown</p><p class="meter-note">{views.esc(health.worst.text)}</p>'
            else:
                active_id = str(status.get("active_model_id") or "")
                installed = {m.get("id") for m in self.models}
                if not active_id:
                    name_html = "None active"
                elif active_id in installed:
                    name_html = f'<a href="/models/{views.url_part(active_id)}">{views.esc(active_id)}</a>'
                else:
                    name_html = f"<code>{views.esc(active_id)}</code>"
                model_box.content = (
                    f'<p class="meter-value">{name_html}</p>'
                    f'<p class="meter-note">{Kit.status(*self._model_state())}</p>'
                )
            endpoint_box.content = f'<p class="meter-value"><code>{views.esc(self._api_base_url())}</code></p>'
            self._show_memory(memory_meter)
            self._show_kv_cache(cache_meter)

        self._active_updater = update
        update()

    def _route_serving(self) -> None:
        self._current_nav_section = "serving"
        self._set_page_title("Serving")

        crumbs = ui.html("", sanitize=False)
        with ui.element("header").classes("page-heading"):
            with ui.element("div"):
                ui.html("<h1>Serving</h1>", sanitize=False)
                ui.html(
                    '<p class="subtitle">Selected and running model, host resources and workloads.</p>',
                    sanitize=False,
                )
            with ui.element("div").classes("actions"):
                Kit.button("Refresh", self._poll, icon="refresh")
                Kit.button("Choose model", href="/models")

        conflict_banner = ui.html("", sanitize=False)

        with ui.element("section").props('aria-labelledby="runtime-heading"'):
            ui.html(
                '<div class="section-head"><h2 id="runtime-heading">Serving status</h2></div>',
                sanitize=False,
            )
            status_box = ui.html("", sanitize=False)
            with ui.element("div").classes("fact-actions actions"):
                btn_restart = Kit.button("Review global restart…", icon="restart_alt").props('id="btn-serving-restart"')
                btn_stop = Kit.button("Review global stop…", emphasis=Emphasis.DANGER, icon="stop_circle").props('id="btn-serving-stop"')
            controls_help = ui.html("", sanitize=False)

        with ui.element("section").classes("section").props('aria-labelledby="metrics-heading"'):
            metrics_head = ui.html("", sanitize=False)
            with ui.element("div").classes("metrics"):
                with Kit.card("Host memory", icon="memory"):
                    memory_meter = Kit.meter("Host memory used")
                with Kit.card("Running requests", icon="bolt"):
                    running_meter = Kit.meter("Running requests")
                with Kit.card("Queued requests", icon="hourglass_empty"):
                    queued_meter = Kit.meter("Queued requests")
                with Kit.card("KV cache", icon="storage"):
                    cache_meter = Kit.meter("KV cache usage")

        with ui.element("section").classes("section").props('aria-labelledby="gpu-heading"'):
            ui.html(
                '<div class="section-head"><h2 id="gpu-heading">GPUs</h2>'
                '<p class="muted">All GPUs on the host, not only those serving the model</p></div>',
                sanitize=False,
            )
            gpu_box = ui.html("", sanitize=False)

        with ui.element("section").classes("section").props('aria-labelledby="workload-heading"'):
            ui.html(
                '<div class="section-head"><h2 id="workload-heading">Workload pods</h2>'
                '<p class="muted">Running pods do not guarantee that API requests succeed</p></div>',
                sanitize=False,
            )
            workload_box = ui.html("", sanitize=False)

        foot_box = ui.html("", sanitize=False)

        btn_restart.on("click", lambda: self._open_serving_modal("RestartModel", "btn-serving-restart"))
        btn_stop.on("click", lambda: self._open_serving_modal("StopServing", "btn-serving-stop"))

        def update() -> None:
            host = self._host_name()
            crumbs.content = views.render_breadcrumbs([(host, None), ("Serving", "/serving")])

            now = time.time()
            running_job = self._running_model_job()
            unresolved_model = self._unresolved_for_scope("model")
            prep = (self.status or {}).get("preparation") if isinstance(self.status, dict) else None

            notices = []
            if running_job is not None:
                jid = views.job_short_id(running_job.get("path"))
                op_name = views.operation_label(str(running_job.get("operation") or ""))
                target = running_job.get("model_name") or "runtime"
                notices.append(
                    f'<div class="notice warning">'
                    f"<strong>{views.esc(op_name)} is running on <code>{views.esc(target)}</code>.</strong> "
                    f"Model changes, including restart and stop, wait until it finishes. "
                    f'<a href="/operations/{views.url_part(jid)}">Open operation →</a>'
                    f"{views.render_job_progress(running_job.get('progress'), now, compact=True)}</div>"
                )
            elif isinstance(prep, dict) and prep.get("state") == "running":
                p_model = views.esc(prep.get("model") or "")
                p_msg = views.esc(prep.get("message") or "Calibrating")
                notices.append(
                    f'<div class="notice warning">'
                    f"<strong>Calibration is running for <code>{p_model}</code>.</strong> {p_msg}. "
                    f"It started automatically before serving, so it is not listed in Operations."
                    f"</div>"
                )
            conflict_banner.content = "".join(notices)

            status_box.content = views.render_serving_status(
                self.status,
                self.snapshot,
                self.models,
                disconnected=self.disconnected,
                status_failed=self.status_failed,
                now=now,
            )

            serving_enabled = isinstance(self.status, dict) and self.status.get("serving_enabled") is True
            selected_id = str((self.status or {}).get("selected_model_id") or "-")
            active_id = str((self.status or {}).get("active_model_id") or "-")
            target_restart = selected_id if selected_id != "-" else active_id

            block_reason = ""
            if self.disconnected or self.status is None or self.status_stale:
                block_reason = "Waiting for current serving status before allowing changes."
            elif unresolved_model is not None:
                block_reason = f"Blocked until the unknown outcome of request {unresolved_model.get('request_id')} is resolved."
            elif running_job is not None:
                block_reason = f"Blocked while {views.job_short_id(running_job.get('path'))} is running."

            can_restart = not block_reason and target_restart != "-"
            can_stop = not block_reason and (serving_enabled or active_id != "-" or selected_id != "-")
            btn_restart.set_enabled(can_restart)
            btn_stop.set_enabled(can_stop)

            if block_reason:
                controls_help.content = f'<p class="help">{views.esc(block_reason)}</p>'
            elif target_restart == "-" and not can_stop:
                controls_help.content = '<p class="help">Serving is currently stopped and no model is selected. Choose a model in Models to start serving.</p>'
            else:
                controls_help.content = '<p class="help">Both actions affect the shared appliance runtime, not an individual model installation.</p>'

            updated = views.fmt_time(self.snapshot.get("sampled_at"), now)
            metrics_head.content = (
                f'<div class="section-head"><h2 id="metrics-heading">Host &amp; requests</h2>'
                f'<p class="muted">Updated {updated}</p></div>'
            )
            self._show_memory(memory_meter)
            requests = self._request_metrics()
            running_requests, waiting_requests = requests.get("running_requests"), requests.get("waiting_requests")
            running_meter.show(
                str(running_requests) if isinstance(running_requests, int) else "Unavailable",
                "Active vLLM requests, not a slot limit",
            )
            queued_meter.show(
                str(waiting_requests) if isinstance(waiting_requests, int) else "Unavailable",
                "Waiting inference requests, not operations",
            )
            self._show_kv_cache(cache_meter)
            gpu_box.content = views.render_gpus_table(self.status)
            workload_box.content = views.render_workloads_table(self.status)

            run_cnt = len(self._running_jobs())
            foot_box.content = (
                f'<footer class="page-foot">'
                f"<span>Updated {updated}</span>"
                f'<a href="/operations">{run_cnt} running operation{"s" if run_cnt != 1 else ""} →</a>'
                f"</footer>"
            )

        self._active_updater = update
        update()

    def _route_models(self, q: str = "", state: str = "", **_kwargs) -> None:
        self._current_nav_section = "models"
        self._set_page_title("Models")
        filter_q = str(q or "")
        filter_state = str(state or "")

        crumbs = ui.html("", sanitize=False)
        with ui.element("header").classes("page-heading"):
            with ui.element("div"):
                ui.html("<h1>Models</h1>", sanitize=False)
                ui.html(
                    '<p class="subtitle">Installed models, their public names, sources and calibration.</p>',
                    sanitize=False,
                )
            with ui.element("div").classes("actions"):
                btn_refresh = Kit.button("Refresh inventory", icon="refresh")
                btn_refresh.on("click", self._poll)
                Kit.button("Download model", href="/models/download", emphasis=Emphasis.PRIMARY, icon="download")

        notice_box = ui.html("", sanitize=False)

        with ui.element("section").props('aria-labelledby="inventory-heading"'):
            inv_head = ui.html("", sanitize=False)
            with ui.element("div").classes("toolbar"):
                with ui.element("div").classes("toolbar-field"):
                    ui.html('<label for="models-filter-q">Find a local model</label>', sanitize=False)
                    inp_q = (
                        ui.element("input")
                        .props(
                            f'id="models-filter-q" type="search" placeholder="Storage ID, public name or repo" value="{views.esc(filter_q)}"'
                        )
                    )
                with ui.element("div").classes("toolbar-field"):
                    ui.html('<label for="models-filter-state">Serving</label>', sanitize=False)
                    sel_state = ui.element("select").props('id="models-filter-state"')
                    with sel_state:
                        for val, label in (
                            ("", "All installations"),
                            ("active", "Running"),
                            ("selected", "Selected"),
                            ("idle", "Not running"),
                        ):
                            selected_attr = " selected" if filter_state == val else ""
                            ui.html(f'<option value="{views.esc(val)}"{selected_attr}>{views.esc(label)}</option>', sanitize=False)
                count_box = ui.html("", sanitize=False).classes("count")

            table_box = ui.html("", sanitize=False)
            action_help = ui.html("", sanitize=False).classes("help").props('id="models-block-reason"')

        partial_box = ui.html("", sanitize=False)

        def sync_url_filter() -> None:
            params = {}
            if filter_q.strip():
                params["q"] = filter_q.strip()
            if filter_state.strip():
                params["state"] = filter_state.strip()
            self._replace_url("/models" + ("?" + urlencode(params) if params else ""))

        def on_q_input(event) -> None:
            nonlocal filter_q
            filter_q = str(event.args or "")
            sync_url_filter()
            update()

        def on_state_change(event) -> None:
            nonlocal filter_state
            filter_state = str(event.args or "")
            sync_url_filter()
            update()

        inp_q.on("input", on_q_input, js_handler="(e) => emit(e.target.value)")
        sel_state.on("change", on_state_change, js_handler="(e) => emit(e.target.value)")

        async def on_row_action(event) -> None:
            args = event.args if isinstance(event.args, dict) else {}
            model = next((m for m in self.models if m.get("id") == args.get("model")), None)
            action = views.model_action(model) if model is not None else None
            if action is None or self._model_block_reason():
                return
            await self._open_serving_modal(action[0], str(args.get("id") or ""), model_id=str(model.get("id")))

        table_box.on(
            "click",
            on_row_action,
            js_handler=(
                "(e) => { const b = e.target.closest('button[data-model]');"
                " if (b && !b.disabled) emit({id: b.id, model: b.dataset.model}); }"
            ),
        )

        def update() -> None:
            crumbs.content = views.render_breadcrumbs([(self._host_name(), None), ("Models", "/models")])
            running_job = self._running_model_job()
            if running_job is not None:
                jid = views.job_short_id(running_job.get("path"))
                op_name = views.operation_label(str(running_job.get("operation") or ""))
                notice_box.content = (
                    f'<div class="notice warning">'
                    f"<strong>{views.esc(op_name)} is running.</strong> "
                    f"Model changes wait until it finishes. "
                    f'<a href="/operations/{views.url_part(jid)}">Open operation →</a>'
                    f"</div>"
                )
            elif self.models_error_text and self.models_loaded:
                notice_box.content = (
                    f'<div class="notice warning">'
                    f"<strong>Inventory refresh failed; showing last known installations.</strong> "
                    f"{views.esc(self.models_error_text)}"
                    f"</div>"
                )
            else:
                notice_box.content = ""

            total_mib = sum(int(m.get("storage_mib") or 0) for m in self.models if isinstance(m, dict))
            inv_head.content = (
                f'<div class="section-head"><h2 id="inventory-heading">Local installations</h2>'
                f'<p class="muted">{len(self.models)} installation{"s" if len(self.models) != 1 else ""} · '
                f'{views.esc(views.fmt_mib(total_mib))} aggregate size</p></div>'
            )

            q_fold = filter_q.strip().casefold()
            filtered_models = []
            for m in self.models:
                if q_fold:
                    hay = " ".join(str(m.get(k) or "") for k in ("id", "name", "repo_id", "revision")).casefold()
                    if q_fold not in hay:
                        continue
                if filter_state == "active" and not m.get("active"):
                    continue
                if filter_state == "selected" and not m.get("selected"):
                    continue
                if filter_state == "idle" and (m.get("active") or m.get("selected")):
                    continue
                filtered_models.append(m)

            count_box.content = f"{len(filtered_models)} of {len(self.models)} shown"
            block_reason = self._model_block_reason()
            table_box.content = views.render_models_table(
                filtered_models,
                loaded=self.models_loaded,
                error_text=self.models_error_text,
                filtered=bool(q_fold or filter_state),
                model_status=str((self.status or {}).get("model_status") or ""),
                block_reason=block_reason,
            )
            action_help.content = views.esc(
                block_reason or "Serving is one shared runtime: serving a model replaces the active one."
            )

            partial_removals = [
                j for j in self.jobs.values()
                if isinstance(j, dict)
                and j.get("operation") == "RemoveModel"
                and isinstance(j.get("result"), dict)
                and j["result"].get("deleted") is True
                and j["result"].get("reconciled") is False
            ]
            if partial_removals:
                pjob = partial_removals[0]
                pjid = views.job_short_id(pjob.get("path"))
                pm = views.esc(pjob.get("model_name") or "model")
                partial_box.content = (
                    f'<section class="section" aria-labelledby="partial-rem-heading">'
                    f'<div class="section-head"><h2 id="partial-rem-heading">Partial removal · {pm}</h2>'
                    f'{Kit.status("Cleanup failed", Tone.ERROR)}</div>'
                    f"<p>The files of <code>{pm}</code> were deleted, but updating the model catalog or workloads failed. "
                    f"Saved calibration records are kept. Do not remove the model again.</p>"
                    f'<div class="actions"><a href="/operations/{views.url_part(pjid)}">Open operation {views.esc(pjid)} →</a>'
                    f'<a href="/serving">Open Serving →</a></div></section>'
                )
            else:
                partial_box.content = ""

        self._active_updater = update
        update()

    def _route_download_search(self, q: str = "") -> None:
        self._current_nav_section = "models"
        self._set_page_title("Download model")
        initial_q = str(q or "").strip()

        crumbs = ui.html(
            views.render_breadcrumbs([("Models", "/models"), ("Download model", "/models/download")]),
            sanitize=False,
        )
        with ui.element("header").classes("page-heading"):
            with ui.element("div"):
                ui.html("<h1>Download model</h1>", sanitize=False)
                ui.html(
                    '<p class="subtitle">Specify a Hugging Face repository and revision directly, or search text-generation repositories to review before downloading.</p>',
                    sanitize=False,
                )
            with ui.element("div").classes("actions"):
                Kit.button("Back to Models", href="/models", icon="arrow_back")

        # Direct repository entry
        with ui.element("section").classes("form-width").props('aria-labelledby="direct-source-heading"'):
            ui.html(
                '<div class="section-head"><h2 id="direct-source-heading">Direct repository &amp; revision</h2>'
                '<p class="muted">Step 1 of 2 · Review immutable commit and capacity before submission</p></div>',
                sanitize=False,
            )
            direct_err = ui.html("", sanitize=False)
            direct_State = {"repo": initial_q if "/" in initial_q else "", "revision": "main", "name": ""}
            with ui.element("div").classes("form-grid"):
                with ui.element("div"):
                    ui.html('<label for="direct-repo-input">Repository ID (org/model)</label>', sanitize=False)
                    inp_repo = ui.element("input").props(
                        f'id="direct-repo-input" type="text" placeholder="e.g. Qwen/Qwen2.5-7B-Instruct" value="{views.esc(direct_State["repo"])}"'
                    )
                with ui.element("div"):
                    ui.html('<label for="direct-rev-input">Revision (branch, tag or 40-hex commit)</label>', sanitize=False)
                    inp_rev = ui.element("input").props('id="direct-rev-input" type="text" value="main"')
                with ui.element("div"):
                    ui.html('<label for="direct-name-input">Local storage ID (optional override)</label>', sanitize=False)
                    inp_name = ui.element("input").props('id="direct-name-input" type="text" placeholder="Derived from repository basename"')
            with ui.element("div").classes("fact-actions actions"):
                btn_review_direct = Kit.button("Review source →", emphasis=Emphasis.PRIMARY)

        def open_direct_review() -> None:
            repo_val = direct_State["repo"].strip()
            rev_val = direct_State["revision"].strip() or "main"
            name_val = direct_State["name"].strip()
            if not re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9._-]{0,95}/)?[A-Za-z0-9][A-Za-z0-9._-]{0,95}", repo_val):
                direct_err.content = '<div class="notice error">Enter a valid Hugging Face repository ID (for example <code>org/model-name</code>).</div>'
                return
            direct_err.content = ""
            params = {"repo": repo_val, "revision": rev_val}
            if name_val:
                params["name"] = name_val
            self._navigate("/models/download/review?" + urlencode(params))

        inp_repo.on("input", lambda e: direct_State.__setitem__("repo", str(e.args or "")), js_handler="(e) => emit(e.target.value)")
        inp_rev.on("input", lambda e: direct_State.__setitem__("revision", str(e.args or "")), js_handler="(e) => emit(e.target.value)")
        inp_name.on("input", lambda e: direct_State.__setitem__("name", str(e.args or "")), js_handler="(e) => emit(e.target.value)")
        btn_review_direct.on("click", open_direct_review)

        # Search section
        with ui.element("section").classes("section").props('aria-labelledby="hf-search-heading"'):
            ui.html(
                '<div class="section-head"><h2 id="hf-search-heading">Search Hugging Face repositories</h2>'
                '<p class="muted">Text-generation pipeline · sorted by downloads</p></div>',
                sanitize=False,
            )
            search_state = {"q": initial_q}
            with ui.element("div").classes("toolbar"):
                with ui.element("div").classes("toolbar-field"):
                    ui.html('<label for="hf-search-input">Search query</label>', sanitize=False)
                    inp_search = ui.element("input").props(
                        f'id="hf-search-input" type="search" placeholder="Model family or repository name" value="{views.esc(initial_q)}"'
                    )
                btn_search = Kit.button("Search", icon="search")
            results_box = ui.html("", sanitize=False)
            ui.html(
                '<p class="help">Gated or private repositories require a host-configured Hugging Face token and cannot be unlocked from the browser.</p>',
                sanitize=False,
            )

        async def run_search(query_str: str) -> None:
            q_clean = query_str.strip()
            self.search_query = q_clean
            self.search_error = ""
            self.search_done = False
            results_box.content = Kit.empty("Searching Hugging Face…", icon="search")
            reply = await self._load("search", "SearchModels", q_clean)
            if reply is None:
                return
            self.search_done = True
            if self._success(reply):
                self.search_results = [m for m in (reply[1].get("models") or []) if isinstance(m, dict)]
                self.search_error = ""
            else:
                self.search_results = []
                self.search_error = self._error(reply)
            update()

        def trigger_search() -> None:
            q_val = search_state["q"].strip()
            target = "/models/download" + ("?" + urlencode({"q": q_val}) if q_val else "")
            if q_val != initial_q:
                self._navigate(target)
            else:
                asyncio.create_task(run_search(q_val))

        inp_search.on("input", lambda e: search_state.__setitem__("q", str(e.args or "")), js_handler="(e) => emit(e.target.value)")
        inp_search.on("keydown.enter", lambda _: trigger_search())
        btn_search.on("click", trigger_search)

        def update() -> None:
            crumbs.content = views.render_breadcrumbs([("Models", "/models"), ("Download model", "/models/download")])
            results_box.content = views.render_search_results_table(
                self.search_results,
                searched=self.search_done,
                error_text=self.search_error,
            )

        self._active_updater = update
        update()
        if initial_q and (not self.search_done or self.search_query != initial_q):
            asyncio.create_task(run_search(initial_q))

    def _route_download_review(self, repo: str = "", revision: str = "main", name: str = "") -> None:
        self._current_nav_section = "models"
        repo_clean = str(repo or "").strip()
        rev_clean = str(revision or "main").strip() or "main"
        name_clean = str(name or "").strip()
        effective_name = ms.normalize_storage_id(repo_clean, name_clean or None) if repo_clean else ""
        self._set_page_title(f"Review {effective_name or repo_clean or 'download'}")

        ui.html(
            views.render_breadcrumbs([
                ("Models", "/models"),
                ("Download model", "/models/download"),
                (f"Review {effective_name or repo_clean or 'source'}", None),
            ]),
            sanitize=False,
        )
        with ui.element("header").classes("page-heading"):
            with ui.element("div"):
                ui.html("<h1>Review model download</h1>", sanitize=False)
                ui.html(
                    '<p class="subtitle">Step 2 of 2 · Verify resolved immutable commit, local storage target, replacement impact and capacity before submitting.</p>',
                    sanitize=False,
                )
            with ui.element("div").classes("actions"):
                back_href = "/models/download" + ("?" + urlencode({"q": repo_clean}) if repo_clean else "")
                Kit.button("Change source", href=back_href, icon="arrow_back")

        if not repo_clean:
            ui.html(
                '<div class="notice error"><strong>No repository specified for review.</strong> '
                '<a href="/models/download">Return to Download model →</a></div>',
                sanitize=False,
            )
            self._active_updater = None
            return

        status_notice = ui.html("", sanitize=False)
        facts_box = ui.html("", sanitize=False)

        # Target local ID override & consent controls
        consent_state = {"replace": False, "force": False, "custom_name": name_clean}
        with ui.element("section").classes("section form-width").props('aria-labelledby="review-submit-heading"'):
            ui.html('<div class="section-head"><h2 id="review-submit-heading">Target &amp; submission controls</h2></div>', sanitize=False)
            with ui.element("div").classes("field"):
                ui.html('<label for="review-local-id">Local storage ID</label>', sanitize=False)
                with ui.element("div").classes("actions"):
                    inp_local_id = ui.element("input").props(
                        f'id="review-local-id" type="text" value="{views.esc(name_clean or effective_name)}" style="max-width: 320px;"'
                    )
                    btn_apply_id = Kit.button("Re-evaluate local ID")
                ui.html(
                    '<p class="help">Changing the local storage ID re-runs collision and replacement checks before submission.</p>',
                    sanitize=False,
                )

            replace_row = ui.element("label").classes("option")
            with replace_row:
                chk_replace = ui.element("input").props('type="checkbox" id="chk-replace-existing"')
                replace_text = ui.html("", sanitize=False)

            force_row = ui.element("label").classes("option")
            with force_row:
                chk_force = ui.element("input").props('type="checkbox" id="chk-force-capacity"')
                ui.html(
                    "<div><strong>Override host RAM capacity estimate (--force)</strong>"
                    "<small>Proceed even though estimated model memory exceeds total host RAM (for multi-node QSFP clustering).</small></div>",
                    sanitize=False,
                )

            submit_err = ui.html("", sanitize=False)
            with ui.element("div").classes("form-footer"):
                btn_submit = Kit.button("Start download", emphasis=Emphasis.PRIMARY, icon="download").props('id="btn-submit-download"')
                Kit.button("Cancel", href="/models")
                submit_help = ui.html("", sanitize=False).classes("help")

        def on_change_local_id() -> None:
            new_id = ms.normalize_storage_id(repo_clean, consent_state["custom_name"] or None)
            params = {"repo": repo_clean, "revision": rev_clean}
            if new_id:
                params["name"] = new_id
            self._navigate("/models/download/review?" + urlencode(params))

        inp_local_id.on("input", lambda e: (consent_state.__setitem__("custom_name", str(e.args or "")), update()), js_handler="(e) => emit(e.target.value)")
        btn_apply_id.on("click", on_change_local_id)
        chk_replace.on("change", lambda e: (consent_state.__setitem__("replace", bool(e.args)), update()), js_handler="(e) => emit(Boolean(e.target.checked))")
        chk_force.on("change", lambda e: (consent_state.__setitem__("force", bool(e.args)), update()), js_handler="(e) => emit(Boolean(e.target.checked))")

        async def load_inspection() -> None:
            self.inspect_loading = True
            self.inspect_error = ""
            self.inspection = None
            update()
            await self._ensure_namespace()
            await self._refresh_models()
            reply = await self._load("inspect", "InspectModel", repo_clean, rev_clean)
            if reply is None:
                return
            self.inspect_loading = False
            if self._success(reply):
                self.inspect_key = (repo_clean, rev_clean)
                self.inspection = reply[1]
                self.inspect_error = ""
                self._review_request_id = self._mint_request_id()
            else:
                self.inspection = None
                self.inspect_error = self._error(reply)
            update()

        async def submit_download() -> None:
            if not isinstance(self.inspection, dict) or not isinstance(self.inspection.get("metadata"), dict):
                return
            meta = self.inspection["metadata"]
            resolved_commit = str(meta.get("resolved_revision") or "")
            existing = next((m for m in self.models if m.get("id") == effective_name), None)
            exp_repo = str(existing.get("repo_id") or "") if existing else ""
            if exp_repo == "-":
                exp_repo = ""
            exp_rev = str(existing.get("revision") or "") if existing else ""

            btn_submit.disable()
            submit_err.content = ""
            ok, res, err_msg = await self._submit_mutation(
                self._review_request_id,
                "DownloadModel",
                effective_name,
                repo_clean,
                effective_name,
                resolved_commit,
                bool(consent_state["force"]),
                existing is not None and bool(consent_state["replace"]),
                exp_repo,
                exp_rev,
            )
            if ok and res.get("job"):
                jid = views.job_short_id(res["job"])
                self._navigate(f"/operations/{views.url_part(jid)}")
                return
            if reply_code := res.get("code"):
                if reply_code == "StateChanged":
                    await self._refresh_models()
                    self._review_request_id = self._mint_request_id()
            submit_err.content = f'<div class="notice error" role="alert"><strong>Download submission not accepted:</strong> {views.esc(err_msg)}</div>'
            update()

        btn_submit.on("click", submit_download)

        def update() -> None:
            existing = next((m for m in self.models if m.get("id") == effective_name), None)
            running_job = self._running_model_job()
            unresolved_model = self._unresolved_for_scope("model")

            if self.inspect_loading:
                status_notice.content = '<div class="notice">Checking the repository and pinning the exact commit…</div>'
                facts_box.content = ""
                replace_row.set_visibility(False)
                force_row.set_visibility(False)
                btn_submit.disable()
                submit_help.content = "Waiting for repository metadata…"
                return

            if self.inspect_error:
                status_notice.content = (
                    f'<div class="notice error" role="alert"><strong>Repository inspection failed.</strong> '
                    f"{views.esc(self.inspect_error)}</div>"
                )
                facts_box.content = ""
                replace_row.set_visibility(False)
                force_row.set_visibility(False)
                btn_submit.disable()
                submit_help.content = "Source metadata is required before download submission."
                return

            status_notice.content = ""
            facts_box.content = views.render_download_review_facts(self.inspection, effective_name, existing)

            meta = (self.inspection or {}).get("metadata") or {}
            fits = (self.inspection or {}).get("fits") is True
            gated = meta.get("gated") is True

            if existing is not None and existing.get("provenance") != "unknown":
                replace_row.set_visibility(True)
                ex_repo = views.esc(existing.get("repo_id") or "-")
                ex_rev = views.esc((existing.get("revision") or "")[:12] or "unknown")
                replace_text.content = (
                    f"<div><strong>Replace existing installation at <code>{views.esc(effective_name)}</code></strong>"
                    f"<small>Overwrites {ex_repo} @ {ex_rev}. Different revisions replace this local ID; OKSA is not a multi-version manager.</small></div>"
                )
            else:
                replace_row.set_visibility(False)

            force_row.set_visibility(not fits)

            pending_name = ms.normalize_storage_id(repo_clean, consent_state["custom_name"] or None)
            block_reason = ""
            if pending_name != effective_name:
                block_reason = "Local storage ID was edited; click Re-evaluate local ID before starting download."
            elif not self.models_verified:
                block_reason = "Local model inventory must be verified before download submission."
            elif unresolved_model is not None:
                block_reason = f"Blocked until the unknown outcome of request {unresolved_model.get('request_id')} is resolved."
            elif running_job is not None:
                block_reason = f"Blocked while {views.job_short_id(running_job.get('path'))} is running."
            elif gated:
                block_reason = "Gated repositories require a host-configured token and cannot be downloaded from the browser."
            elif existing is not None and existing.get("provenance") == "unknown":
                block_reason = f"Existing installation {effective_name} has unknown provenance; choose a different local storage ID or inspect storage."
            elif existing is not None and not consent_state["replace"]:
                block_reason = f"Confirm replacement of existing installation {effective_name} to continue."
            elif not fits and not consent_state["force"]:
                block_reason = "Confirm host RAM capacity override (--force) to continue."

            btn_submit.set_enabled(not block_reason)
            submit_help.content = views.esc(block_reason or f"Pins commit {str(meta.get('resolved_revision') or '')[:12]} into /var/lib/vllm/models/{effective_name}.")

        self._active_updater = update
        asyncio.create_task(load_inspection())

    def _route_model_detail(self, storage_id: str = "") -> None:
        self._current_nav_section = "models"
        model_id = str(storage_id or "").strip()
        self._set_page_title(f"{model_id} · Models")
        self._cal_force = False

        crumbs = ui.html("", sanitize=False)
        header_box = ui.element("header").classes("page-heading")
        with header_box:
            title_box = ui.html("", sanitize=False)
            with ui.element("div").classes("actions"):
                btn_refresh = Kit.button("Refresh model", icon="refresh")
                btn_serve = Kit.button("Serve this model…", emphasis=Emphasis.PRIMARY, icon="play_arrow").props('id="btn-model-serve"')
                btn_restart = Kit.button("Restart model…", icon="restart_alt").props('id="btn-model-restart"')
                Kit.button("All models", href="/models", icon="arrow_back")

        missing_box = ui.html("", sanitize=False)
        detail_container = ui.element("div")
        with detail_container:
            conflict_box = ui.html("", sanitize=False)
            with ui.element("section").props('aria-labelledby="model-identity-heading"'):
                ui.html('<div class="section-head"><h2 id="model-identity-heading">Model identity &amp; local storage</h2></div>', sanitize=False)
                identity_facts = ui.html("", sanitize=False)
                action_reason = ui.html("", sanitize=False).classes("help")

            with ui.element("section").classes("section").props('id="calibration" aria-labelledby="model-cal-heading"'):
                ui.html(
                    '<div class="section-head"><h2 id="model-cal-heading">Calibration</h2>'
                    '<p class="muted">Model-owned hardware/runtime capability profile</p></div>',
                    sanitize=False,
                )
                cal_summary = ui.html("", sanitize=False)
                with ui.element("div").classes("form-width"):
                    with ui.element("fieldset"):
                        ui.html("<legend>Review calibration measurement</legend>", sanitize=False)
                        with ui.element("label").classes("option"):
                            rad_reuse = ui.element("input").props('type="radio" name="cal-mode" value="reuse" checked')
                            ui.html(
                                "<div><strong>Reuse a matching profile if possible</strong>"
                                "<small>Uses saved limits for this hardware and runtime. Otherwise measures the GPU, "
                                "which stops serving for up to 4 hours plus setup.</small></div>",
                                sanitize=False,
                            )
                        with ui.element("label").classes("option"):
                            rad_force = ui.element("input").props('type="radio" name="cal-mode" value="force"')
                            ui.html(
                                "<div><strong>Measure again</strong>"
                                "<small>Ignores saved limits and measures the GPU. Serving stops for up to 4 hours plus setup.</small></div>",
                                sanitize=False,
                            )
                    cal_err = ui.html("", sanitize=False)
                    with ui.element("div").classes("actions"):
                        btn_calibrate = Kit.button("Start calibration").props('id="btn-model-calibrate"')
                        cal_help = ui.html("", sanitize=False).classes("help")

            with ui.element("section").classes("section").props('aria-labelledby="model-ops-heading"'):
                ui.html('<div class="section-head"><h2 id="model-ops-heading">Related operations</h2></div>', sanitize=False)
                related_ops_box = ui.html("", sanitize=False)

            with ui.element("section").classes("section").props('aria-labelledby="model-danger-heading"'):
                ui.html('<div class="section-head"><h2 id="model-danger-heading">Danger zone · Remove installation</h2></div>', sanitize=False)
                ui.html(
                    "<p class=\"small muted\">Deletes local model files and catalog registration. "
                    "If this model is active, serving is stopped before deletion. Saved calibration records are preserved.</p>",
                    sanitize=False,
                )
                with ui.element("div").classes("actions"):
                    btn_delete = Kit.button("Delete model installation…", emphasis=Emphasis.DANGER, icon="delete").props('id="btn-model-delete"')
                    delete_help = ui.html("", sanitize=False).classes("help")

        rad_reuse.on("change", lambda _: setattr(self, "_cal_force", False))
        rad_force.on("change", lambda _: setattr(self, "_cal_force", True))

        async def refresh_this() -> None:
            await self._ensure_namespace()
            await self._refresh_models()
            await self._refresh_status()
            await self._refresh_jobs()
            if any(m.get("id") == model_id for m in self.models):
                await self._refresh_calibration(model_id)
            self._cal_request_id = self._mint_request_id()
            update()

        async def submit_calibration() -> None:
            btn_calibrate.disable()
            cal_err.content = ""
            sel_id = str((self.status or {}).get("selected_model_id") or "-")
            act_id = str((self.status or {}).get("active_model_id") or "-")
            ok, res, err_msg = await self._submit_mutation(
                self._cal_request_id,
                "Calibrate",
                model_id,
                model_id,
                bool(self._cal_force),
                sel_id,
                act_id,
            )
            if ok and res.get("job"):
                jid = views.job_short_id(res["job"])
                self._navigate(f"/operations/{views.url_part(jid)}")
                return
            self._cal_request_id = self._mint_request_id()
            cal_err.content = f'<div class="notice error" role="alert"><strong>Calibration not started:</strong> {views.esc(err_msg)}</div>'
            update()

        btn_refresh.on("click", refresh_this)
        btn_serve.on("click", lambda: self._open_serving_modal("StartModel", "btn-model-serve", model_id=model_id))
        btn_restart.on("click", lambda: self._open_serving_modal("RestartModel", "btn-model-restart", model_id=model_id))
        btn_delete.on("click", lambda: self._open_serving_modal("RemoveModel", "btn-model-delete", model_id=model_id))
        btn_calibrate.on("click", submit_calibration)

        def update() -> None:
            crumbs.content = views.render_breadcrumbs([("Models", "/models"), (model_id, f"/models/{views.url_part(model_id)}")])
            valid_id = bool(re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", model_id))
            model = next((m for m in self.models if m.get("id") == model_id), None) if valid_id else None
            if model is None:
                detail_container.set_visibility(False)
                btn_serve.disable()
                btn_restart.disable()
                title_box.content = f"<h1>{views.esc(model_id or 'Unknown model')}</h1><p class=\"subtitle\">Model detail</p>"
                if valid_id and not self.models_loaded:
                    missing_box.content = '<div class="notice">Loading local model inventory…</div>'
                else:
                    rem_job = next(
                        (j for j in self.jobs.values() if isinstance(j, dict) and j.get("model_name") == model_id),
                        None,
                    )
                    op_link = ""
                    if rem_job:
                        rjid = views.job_short_id(rem_job.get("path"))
                        op_link = f' <a href="/operations/{views.url_part(rjid)}">Open operation {views.esc(rjid)} →</a>'
                    missing_box.content = (
                        f'<div class="notice error" role="alert">'
                        f"<p><strong>Model <code>{views.esc(model_id)}</code> is not installed.</strong> "
                        f"It may have been removed or never downloaded.{op_link}</p>"
                        f'<p><a href="/models">← Return to Models inventory</a></p>'
                        f"</div>"
                    )
                return

            missing_box.content = ""
            detail_container.set_visibility(True)
            pub = str(model.get("name") or model_id)
            title_box.content = (
                f"<h1>{views.esc(model_id)}</h1>"
                f'<p class="subtitle">Public inference name <code>{views.esc(pub)}</code> · local installation details and calibration.</p>'
            )

            running_job = self._running_model_job()
            if running_job is not None:
                jid = views.job_short_id(running_job.get("path"))
                op_name = views.operation_label(str(running_job.get("operation") or ""))
                conflict_box.content = (
                    f'<div class="notice warning">'
                    f"<strong>{views.esc(op_name)} is running.</strong> "
                    f"Model changes wait until it finishes. "
                    f'<a href="/operations/{views.url_part(jid)}">Open operation →</a></div>'
                )
            else:
                conflict_box.content = ""

            identity_facts.content = views.render_model_detail_facts(
                model, str((self.status or {}).get("model_status") or "")
            )

            block_reason = self._model_block_reason()

            unservable = views.model_unservable_reason(model)
            can_serve = not block_reason and not unservable
            can_restart = not block_reason and not unservable and (model.get("active") is True or model.get("selected") is True)
            can_cal = not block_reason
            can_del = not block_reason

            btn_serve.set_enabled(can_serve)
            btn_restart.set_enabled(can_restart)
            btn_calibrate.set_enabled(can_cal)
            btn_delete.set_enabled(can_del)

            if block_reason:
                action_reason.content = views.esc(block_reason)
            elif unservable:
                action_reason.content = views.esc(unservable)
            else:
                action_reason.content = "Serving this model updates the shared appliance runtime selection."

            cal_data = self.calibration_data if self.calibration_model == model_id else None
            cal_err_txt = self.calibration_error if self.calibration_model == model_id else ""
            running = self._running_model_job()
            cal_summary.content = views.render_calibration_summary(
                cal_data, error_text=cal_err_txt, now=time.time(),
                running_job=running if running is not None and running.get("model_name") == model_id else None,
            )
            cal_help.content = views.esc(block_reason or "4-hour search budget maximum; not an ETA.")
            delete_help.content = views.esc(block_reason or f"Removes /var/lib/vllm/models/{model_id}.")

            rel_jobs = sorted(
                (j for j in self.jobs.values() if isinstance(j, dict) and j.get("model_name") == model_id),
                key=lambda item: float(item.get("started_at") or 0.0),
                reverse=True,
            )
            related_ops_box.content = views.render_operations_table(
                rel_jobs, loaded=self.jobs_loaded, error_text=self.activity_error_text, filtered=True, now=time.time()
            )

        self._active_updater = update
        update()
        asyncio.create_task(refresh_this())

    def _route_access(self, q: str = "", key_q: str = "", **_kwargs) -> None:
        self._current_nav_section = "access"
        self._set_page_title("API access")
        user_q = str(q or "")
        lookup_q = str(key_q or "")

        crumbs = ui.html("", sanitize=False)
        with ui.element("header").classes("page-heading"):
            with ui.element("div"):
                ui.html("<h1>API access</h1>", sanitize=False)
                ui.html(
                    '<p class="subtitle">Inference consumers and API keys. API users are LiteLLM consumers, not appliance administrators or Linux accounts.</p>',
                    sanitize=False,
                )
            with ui.element("div").classes("actions"):
                btn_refresh = Kit.button("Refresh access", icon="refresh")
                btn_create_user = Kit.button("Create API user…", emphasis=Emphasis.PRIMARY, icon="person_add").props('id="btn-open-create-user"')

        auth_notice = ui.html("", sanitize=False)

        # Cross-owner key lookup
        with ui.element("section").props('aria-labelledby="key-lookup-heading"'):
            lookup_head = ui.html("", sanitize=False)
            with ui.element("div").classes("toolbar"):
                with ui.element("div").classes("toolbar-field").props('style="min-width: 280px;"'):
                    ui.html('<label for="access-key-lookup">Find key metadata across users</label>', sanitize=False)
                    inp_lookup = ui.element("input").props(
                        f'id="access-key-lookup" type="search" aria-label="Find key metadata across users" '
                        f'placeholder="Key alias, displayed prefix (sk-...abcd), key ID or owner" value="{views.esc(lookup_q)}"'
                    )
                ui.html(
                    '<span class="count">Metadata index only · never enter a complete secret key</span>',
                    sanitize=False,
                )
            lookup_table = ui.html("", sanitize=False)

        # Users list
        with ui.element("section").classes("section").props('aria-labelledby="users-heading"'):
            users_head = ui.html("", sanitize=False)
            with ui.element("div").classes("toolbar"):
                with ui.element("div").classes("toolbar-field"):
                    ui.html('<label for="access-user-filter">Find API user</label>', sanitize=False)
                    inp_user = ui.element("input").props(
                        f'id="access-user-filter" type="search" placeholder="User ID or email" value="{views.esc(user_q)}"'
                    )
                user_count = ui.html("", sanitize=False).classes("count")
            users_table = ui.html("", sanitize=False)

        def sync_access_url() -> None:
            params = {}
            if user_q.strip():
                params["q"] = user_q.strip()
            kq = lookup_q.strip()
            if kq and not (kq.startswith("sk-") and len(kq) >= 20 and "..." not in kq):
                params["key_q"] = kq
            self._replace_url("/access" + ("?" + urlencode(params) if params else ""))

        def on_lookup_input(event) -> None:
            nonlocal lookup_q
            lookup_q = str(event.args or "")
            sync_access_url()
            update()

        def on_user_input(event) -> None:
            nonlocal user_q
            user_q = str(event.args or "")
            sync_access_url()
            update()
        inp_lookup.on("input", on_lookup_input, js_handler="(e) => emit(e.target.value)")
        inp_user.on("input", on_user_input, js_handler="(e) => emit(e.target.value)")
        btn_refresh.on("click", self._poll)
        btn_create_user.on("click", lambda: self._open_create_user_modal("btn-open-create-user"))

        def update() -> None:
            now = time.time()
            crumbs.content = views.render_breadcrumbs([(self._host_name(), None), ("API access", "/access")])
            auth = str((self.status or {}).get("auth_enforcement") or "unknown")
            if auth == "enforced":
                auth_notice.content = (
                    '<div class="notice">'
                    "<strong>Gateway authentication enforced.</strong> Anonymous <code>/v1/models</code> probe was rejected. "
                    "Key revocation confirms database removal, not immediate in-memory cache eviction."
                    "</div>"
                )
            elif auth == "keyless":
                auth_notice.content = (
                    '<div class="notice warning">'
                    "<strong>Keyless inference is currently enabled.</strong> Anonymous requests to the gateway are accepted until "
                    "<code>litellm-master-key</code> is encrypted and <code>puu-k3s-addons.service</code> is restarted. "
                    "Revoking a key will not block anonymous requests while keyless mode is active."
                    "</div>"
                )
            else:
                auth_notice.content = (
                    '<div class="notice warning">'
                    "<strong>Authentication enforcement not verified.</strong> Gateway readiness does not prove that anonymous inference is denied."
                    "</div>"
                )

            unresolved_user = self._unresolved_for_scope("user")
            can_create = not self.disconnected and self.users_verified and unresolved_user is None
            btn_create_user.set_enabled(can_create)

            cov = (
                f"{len(self.all_keys)} keys · read {views.fmt_ago(self.all_keys_sampled_at, now)}"
                if self.all_keys_verified
                else "Index incomplete: a key missing here may still exist"
            )
            lookup_head.content = (
                f'<div class="section-head"><h2 id="key-lookup-heading">Find a key</h2>'
                f'<p class="muted">{views.esc(cov)}</p></div>'
            )
            lookup_table.content = views.render_key_lookup_table(
                self.all_keys,
                lookup_q,
                verified=self.all_keys_verified,
                sampled_at=self.all_keys_sampled_at,
                error_text=self.all_keys_error_text,
                now=now,
            )

            users_head.content = (
                f'<div class="section-head"><h2 id="users-heading">API users</h2>'
                f'<p class="muted">{len(self.users)} consumer account{"s" if len(self.users) != 1 else ""}</p></div>'
            )
            uq_fold = user_q.strip().casefold()
            filtered_users = [
                u for u in self.users
                if not uq_fold or uq_fold in f"{u.get('user_id', '')} {u.get('user_email', '')}".casefold()
            ]
            user_count.content = f"{len(filtered_users)} of {len(self.users)} shown"
            users_table.content = views.render_users_table(
                filtered_users,
                self.all_keys,
                self.protected_users,
                users_verified=self.users_verified,
                keys_verified=self.all_keys_verified,
                error_text=self.users_error_text,
                filtered=bool(uq_fold),
            )

        self._active_updater = update
        update()
        asyncio.create_task(self._refresh_access())

    def _route_user_detail(self, id: str = "") -> None:
        self._current_nav_section = "access"
        user_id = str(id or "").strip()
        self.user_detail_id = user_id
        self._set_page_title(f"{user_id or 'User'} · API access")

        crumbs = ui.html("", sanitize=False)
        with ui.element("header").classes("page-heading"):
            title_box = ui.html("", sanitize=False)
            with ui.element("div").classes("actions"):
                btn_refresh = Kit.button("Refresh user", icon="refresh")
                btn_create_key = Kit.button("Create API key…", emphasis=Emphasis.PRIMARY, icon="key").props('id="btn-open-create-key"')
                Kit.button("All API users", href="/access", icon="arrow_back")

        missing_box = ui.html("", sanitize=False)
        detail_container = ui.element("div")
        with detail_container:
            with ui.element("section").props('aria-labelledby="user-identity-heading"'):
                ui.html('<div class="section-head"><h2 id="user-identity-heading">Consumer identity &amp; handoff context</h2></div>', sanitize=False)
                user_facts = ui.html("", sanitize=False)

            with ui.element("section").classes("section").props('aria-labelledby="user-quickstart-heading"'):
                ui.html('<div class="section-head"><h2 id="user-quickstart-heading">Quick start</h2></div>', sanitize=False)
                quickstart_note = ui.html("", sanitize=False)
                quickstart = Kit.code_example(self._copy_text)

            with ui.element("section").classes("section").props('aria-labelledby="user-keys-heading"'):
                keys_head = ui.html("", sanitize=False)
                keys_notice = ui.html("", sanitize=False)
                keys_table = ui.html("", sanitize=False)

            with ui.element("section").classes("section").props('aria-labelledby="user-danger-heading"'):
                ui.html('<div class="section-head"><h2 id="user-danger-heading">Danger zone · Delete API user</h2></div>', sanitize=False)
                with ui.element("div").classes("actions"):
                    btn_delete_user = Kit.button("Delete user and revoke keys…", emphasis=Emphasis.DANGER, icon="delete").props('id="btn-delete-user"')
                    delete_user_help = ui.html("", sanitize=False).classes("help")

        btn_refresh.on("click", lambda: asyncio.create_task(self._refresh_user_detail(user_id)))
        btn_create_key.on("click", lambda: self._open_create_key_modal(user_id, "btn-open-create-key"))
        btn_delete_user.on("click", lambda: self._open_delete_user_modal(user_id, "btn-delete-user"))

        def block_reason() -> str:
            if self.disconnected or not self.users_verified or not self.user_detail_keys_verified:
                return "User and key records must be verified before mutating keys or deleting this user."
            unresolved_user = self._unresolved_for_scope("user")
            if unresolved_user is not None:
                return f"Blocked until the unknown outcome of request {unresolved_user.get('request_id')} is resolved."
            return ""

        async def on_key_action(event) -> None:
            args = event.args if isinstance(event.args, dict) else {}
            key = next((k for k in self.user_detail_keys if k.get("key_id") == args.get("key")), None)
            if key is None or block_reason():
                return
            await self._open_revoke_key_modal(dict(key), str(args.get("id") or ""))

        keys_table.on(
            "click",
            on_key_action,
            js_handler=(
                "(e) => { const b = e.target.closest('button[data-key]');"
                " if (b && !b.disabled) emit({id: b.id, key: b.dataset.key}); }"
            ),
        )

        def update() -> None:
            crumbs.content = views.render_breadcrumbs([
                ("API access", "/access"),
                (user_id or "User", f"/access/user?id={views.url_part(user_id)}"),
            ])
            user_obj = next((u for u in self.users if u.get("user_id") == user_id), None)
            if not user_id or (self.users_verified and user_obj is None):
                detail_container.set_visibility(False)
                btn_create_key.disable()
                title_box.content = f"<h1>{views.esc(user_id or 'Unknown user')}</h1><p class=\"subtitle\">API user</p>"
                del_job = next(
                    (j for j in self.jobs.values() if isinstance(j, dict) and j.get("user_id") == user_id),
                    None,
                )
                job_link = ""
                if del_job:
                    djid = views.job_short_id(del_job.get("path"))
                    job_link = f' <a href="/operations/{views.url_part(djid)}">Open deletion {views.esc(djid)} →</a>'
                missing_box.content = (
                    f'<div class="notice error" role="alert">'
                    f"<p><strong>API user <code>{views.esc(user_id or 'unspecified')}</code> was not found.</strong>{job_link}</p>"
                    f'<p><a href="/access">← Return to API access</a></p></div>'
                )
                return

            missing_box.content = ""
            detail_container.set_visibility(True)
            email = str((user_obj or {}).get("user_email") or "Not set")
            owns_service = user_id in self.protected_users
            title_box.content = (
                f"<h1>{views.esc(user_id)}</h1>"
                f'<p class="subtitle">LiteLLM inference consumer · not an appliance or OS login.</p>'
            )

            active_pub = str((self.status or {}).get("active_model") or "None active")
            user_facts.content = views.render_facts([
                ("User ID", f"<code>{views.esc(user_id)}</code>"),
                ("Email", views.esc(email)),
                ("Privilege scope", "Inference API consumer only (no OKSA admin or host privileges)"),
                (
                    "Protected service key",
                    Kit.status("Owns Open WebUI service key (blocks user deletion)", Tone.WARNING)
                    if owns_service
                    else "None",
                ),
                ("API base URL", f"<code>{views.esc(self._api_base_url())}</code>"),
                ("Running model", f"<code>{views.esc(active_pub)}</code>"),
            ])

            scopes = [k.get("models") or [] for k in self.user_detail_keys]
            scope = sorted({m for sc in scopes for m in sc}) if scopes and all(scopes) else []
            model = views.quickstart_model(str((self.status or {}).get("active_model") or ""), scope, self._model_names())
            quickstart_note.content = self._quickstart_note(
                model, f"Set <code>{views.API_KEY_ENV}</code> to one of this user's keys first. "
            )
            quickstart.show(views.quickstart_snippets(self._api_base_url(), model) if model else {})

            reason = block_reason()
            btn_create_key.set_enabled(not reason)
            can_del_user = not reason and not owns_service
            btn_delete_user.set_enabled(can_del_user)
            if owns_service:
                delete_user_help.content = "Protected: this user owns the Open WebUI service key and cannot be deleted."
            else:
                delete_user_help.content = views.esc(
                    reason or f"Revokes all {len(self.user_detail_keys)} key(s) owned by {user_id}, then deletes the user record."
                )

            keys_head.content = (
                f'<div class="section-head"><h2 id="user-keys-heading">API keys owned by {views.esc(user_id)}</h2>'
                f'<p class="muted">{len(self.user_detail_keys)} editable key{"s" if len(self.user_detail_keys) != 1 else ""}</p></div>'
            )
            if self.user_detail_error:
                keys_notice.content = f'<div class="notice error">Could not verify keys for {views.esc(user_id)}: {views.esc(self.user_detail_error)}</div>'
            else:
                keys_notice.content = ""

            keys_table.content = views.render_user_keys_table(
                self.user_detail_keys, verified=self.user_detail_keys_verified, blocked=bool(reason)
            )

        self._active_updater = update
        update()
        asyncio.create_task(self._refresh_user_detail(user_id))

    def _route_operations(self, state: str = "", kind: str = "", **_kwargs) -> None:
        self._current_nav_section = "operations"
        self._set_page_title("Operations")
        filter_state = str(state or "")
        filter_kind = str(kind or "")

        crumbs = ui.html("", sanitize=False)
        with ui.element("header").classes("page-heading"):
            with ui.element("div"):
                ui.html("<h1>Operations</h1>", sanitize=False)
                ui.html(
                    '<p class="subtitle">Daemon-tracked jobs, including model downloads, serving changes, calibration and user deletion.</p>',
                    sanitize=False,
                )
            with ui.element("div").classes("actions"):
                btn_refresh = Kit.button("Refresh", icon="refresh")
                btn_refresh.on("click", self._poll)

        ui.html(
            '<div class="notice">'
            "The latest 64 long-running operations, such as downloads and model changes, are kept. "
            "User and key changes finish immediately and are not listed. This is not a security audit log."
            "</div>",
            sanitize=False,
        )

        with ui.element("section").props('aria-labelledby="ops-list-heading"'):
            ops_head = ui.html("", sanitize=False)
            with ui.element("div").classes("toolbar"):
                with ui.element("div").classes("toolbar-field"):
                    ui.html('<label for="ops-filter-state">State</label>', sanitize=False)
                    sel_state = ui.element("select").props('id="ops-filter-state"')
                    with sel_state:
                        for val, label in (
                            ("", "All states"),
                            ("running", "Running"),
                            ("succeeded", "Succeeded"),
                            ("failed", "Failed"),
                            ("canceled", "Canceled"),
                        ):
                            sel_attr = " selected" if filter_state == val else ""
                            ui.html(f'<option value="{views.esc(val)}"{sel_attr}>{views.esc(label)}</option>', sanitize=False)
                with ui.element("div").classes("toolbar-field"):
                    ui.html('<label for="ops-filter-kind">Operation kind</label>', sanitize=False)
                    sel_kind = ui.element("select").props('id="ops-filter-kind"')
                    with sel_kind:
                        for val, label in (
                            ("", "All kinds"),
                            ("DownloadModel", "Downloads"),
                            ("serving", "Serving (Start / Restart / Stop)"),
                            ("Calibrate", "Calibration"),
                            ("RemoveModel", "Model removal"),
                            ("DeleteUser", "User deletion"),
                        ):
                            sel_attr = " selected" if filter_kind == val else ""
                            ui.html(f'<option value="{views.esc(val)}"{sel_attr}>{views.esc(label)}</option>', sanitize=False)
                count_box = ui.html("", sanitize=False).classes("count")
            table_box = ui.html("", sanitize=False)

        def apply_ops_filter(new_state: str, new_kind: str) -> None:
            nonlocal filter_state, filter_kind
            filter_state = new_state
            filter_kind = new_kind
            params = {}
            if filter_state:
                params["state"] = filter_state
            if filter_kind:
                params["kind"] = filter_kind
            self._replace_url("/operations" + ("?" + urlencode(params) if params else ""))
            update()

        sel_state.on("change", lambda e: apply_ops_filter(str(e.args or ""), filter_kind), js_handler="(e) => emit(e.target.value)")
        sel_kind.on("change", lambda e: apply_ops_filter(filter_state, str(e.args or "")), js_handler="(e) => emit(e.target.value)")

        def update() -> None:
            crumbs.content = views.render_breadcrumbs([(self._host_name(), None), ("Operations", "/operations")])
            ordered = sorted(
                (j for j in self.jobs.values() if isinstance(j, dict)),
                key=lambda item: float(item.get("started_at") or 0.0),
                reverse=True,
            )
            filtered = []
            for j in ordered:
                if filter_state and j.get("state") != filter_state:
                    continue
                op = str(j.get("operation") or "")
                if filter_kind == "serving" and op not in ("StartModel", "RestartModel", "StopServing"):
                    continue
                if filter_kind and filter_kind != "serving" and op != filter_kind:
                    continue
                filtered.append(j)

            run_cnt = len(self._running_jobs())
            ops_head.content = (
                f'<div class="section-head"><h2 id="ops-list-heading">Recent operations</h2>'
                f'<p class="muted">{run_cnt} running · {len(ordered)} kept</p></div>'
            )
            count_box.content = f"{len(filtered)} of {len(ordered)} shown"
            table_box.content = views.render_operations_table(
                filtered,
                loaded=self.jobs_loaded,
                error_text=self.activity_error_text,
                filtered=bool(filter_state or filter_kind),
                now=time.time(),
            )

        self._active_updater = update
        update()

    def _route_operation_detail(self, job_id: str = "") -> None:
        self._current_nav_section = "operations"
        short_id = str(job_id or "").strip().removeprefix("op-")
        full_path = f"{Contract.PATH}/jobs/{short_id}"
        self._set_page_title(f"Operation {short_id}")

        crumbs = ui.html("", sanitize=False)
        with ui.element("header").classes("page-heading"):
            title_box = ui.html("", sanitize=False)
            with ui.element("div").classes("actions"):
                btn_refresh = Kit.button("Refresh", icon="refresh")
                btn_cancel = Kit.button("Cancel operation…", emphasis=Emphasis.DANGER, icon="cancel").props('id="btn-cancel-job"')
                Kit.button("All operations", href="/operations", icon="arrow_back")

        missing_box = ui.html("", sanitize=False)
        detail_container = ui.element("div")
        with detail_container:
            with ui.element("section").props('aria-labelledby="op-identity-heading"'):
                ui.html('<div class="section-head"><h2 id="op-identity-heading">Details</h2></div>', sanitize=False)
                facts_box = ui.html("", sanitize=False)
            with ui.element("section").classes("section").props('aria-labelledby="op-status-heading"'):
                ui.html('<div class="section-head"><h2 id="op-status-heading">Status</h2></div>', sanitize=False)
                status_box = ui.html("", sanitize=False)
            with ui.element("section").classes("section").props('aria-labelledby="op-outcome-heading"'):
                ui.html('<div class="section-head"><h2 id="op-outcome-heading">Next steps</h2></div>', sanitize=False)
                outcome_box = ui.html("", sanitize=False)

        btn_refresh.on("click", self._poll)
        btn_cancel.on("click", lambda: self._open_cancel_job_modal(full_path, "btn-cancel-job"))

        def update() -> None:
            crumbs.content = views.render_breadcrumbs([("Operations", "/operations"), (short_id, f"/operations/{views.url_part(short_id)}")])
            valid_jid = bool(re.fullmatch(r"[0-9a-f]{32}", short_id))
            job = self.jobs.get(full_path) if valid_jid else None
            if job is None:
                detail_container.set_visibility(False)
                btn_cancel.set_visibility(False)
                title_box.content = f"<h1>Operation {views.esc(short_id or 'unknown')}</h1><p class=\"subtitle\">Operation details</p>"
                if valid_jid and not self.jobs_loaded:
                    missing_box.content = '<div class="notice">Loading daemon operations…</div>'
                else:
                    missing_box.content = (
                        f'<div class="notice error" role="alert">'
                        f"<p><strong>Operation <code>{views.esc(short_id)}</code> is no longer kept.</strong> "
                        "Only the latest 64 operations are kept; a removed entry does not mean its effects were undone.</p>"
                        f'<p><a href="/operations">← Return to Operations</a></p></div>'
                    )
                return

            missing_box.content = ""
            detail_container.set_visibility(True)
            op_label = views.operation_label(str(job.get("operation") or ""))
            target = job.get("model_name") or job.get("user_id") or ""
            heading = f"{op_label} · {target}" if target else op_label
            title_box.content = (
                f"<h1>{views.esc(heading)}</h1>"
            )
            is_running = job.get("state") == "running"
            btn_cancel.set_visibility(is_running)
            btn_cancel.set_enabled(is_running and not self.disconnected)

            f_html, s_html, o_html = views.render_operation_detail(job, time.time())
            facts_box.content = f_html
            status_box.content = s_html
            outcome_box.content = o_html

        self._active_updater = update
        update()

    def _open_dialog(self, dialog_id: str) -> None:
        self.client.run_javascript(
            f"const d = document.getElementById({json.dumps(dialog_id)}); if (d && !d.open) d.showModal();"
        )

    def _close_dialog(self, dialog_id: str, opener_id: str = "") -> None:
        self.client.run_javascript(
            f"const d = document.getElementById({json.dumps(dialog_id)}); if (d && d.open) d.close();"
            f"if ({json.dumps(opener_id)}) document.getElementById({json.dumps(opener_id)})?.focus();"
        )

    def _clear_secret(self) -> None:
        self.secret = ""
        self.secret_meta = {}
        if not self.client.is_deleted:
            self._secret_code_box.content = ""
            self._secret_example.show({})

    def _build_modals(self) -> None:
        # 1. Serving / Model action modal
        with ui.element("dialog").classes("oksa-modal").props('id="modal-serving-action" aria-labelledby="modal-serving-title"'):
            self._srv_modal_body = ui.html("", sanitize=False)
            self._srv_modal_err = ui.html("", sanitize=False)
            self._srv_actions = ui.element("div").classes("dialog-actions")

        # 2. Create user modal
        with ui.element("dialog").classes("oksa-modal").props('id="modal-create-user" aria-labelledby="modal-user-title"'):
            ui.html(
                '<h2 id="modal-user-title">Create API user</h2>'
                "<p>Creates an inference consumer identity in the LiteLLM gateway database (not an OS or OKSA admin account).</p>",
                sanitize=False,
            )
            self._user_modal_err = ui.html("", sanitize=False)
            with ui.element("div").classes("field"):
                ui.html('<label for="modal-input-user-id">User ID (required)</label>', sanitize=False)
                self._inp_user_id = ui.element("input").props('id="modal-input-user-id" type="text" placeholder="e.g. team-analytics"')
            with ui.element("div").classes("field"):
                ui.html('<label for="modal-input-user-email">Email (optional)</label>', sanitize=False)
                self._inp_user_email = ui.element("input").props('id="modal-input-user-email" type="email" placeholder="owner@example.org"')
            with ui.element("div").classes("dialog-actions"):
                btn_u_cancel = Kit.button("Cancel")
                self._btn_u_confirm = Kit.button("Create user", emphasis=Emphasis.PRIMARY)
        self._inp_user_id.on("input", lambda e: setattr(self, "_create_user_id", str(e.args or "")), js_handler="(e) => emit(e.target.value)")
        self._inp_user_email.on("input", lambda e: setattr(self, "_create_user_email", str(e.args or "")), js_handler="(e) => emit(e.target.value)")
        btn_u_cancel.on("click", lambda: self._close_dialog("modal-create-user", self._modal_action.get("opener", "")))
        self._btn_u_confirm.on("click", self._confirm_create_user)

        # 3. Create key modal
        with ui.element("dialog").classes("oksa-modal").props('id="modal-create-key" aria-labelledby="modal-key-title"'):
            self._key_modal_head = ui.html("", sanitize=False)
            self._key_modal_err = ui.html("", sanitize=False)
            with ui.element("div").classes("field"):
                ui.html('<label for="modal-input-key-alias">Key alias / name (required)</label>', sanitize=False)
                self._inp_key_alias = ui.element("input").props('id="modal-input-key-alias" type="text" placeholder="e.g. ci-eval-runner"')
            with ui.element("fieldset"):
                ui.html("<legend>Model access scope</legend>", sanitize=False)
                with ui.element("label").classes("option"):
                    self._rad_scope_sel = ui.element("input").props('type="radio" name="key-scope" value="selected" checked')
                    ui.html(
                        "<div><strong>Selected public model(s)</strong>"
                        "<small>Restricts this key to the specific public inference model names checked below.</small></div>",
                        sanitize=False,
                    )
                self._key_models_box = ui.element("div").classes("field")
                with ui.element("label").classes("option"):
                    self._rad_scope_all = ui.element("input").props('type="radio" name="key-scope" value="all"')
                    ui.html(
                        "<div><strong>All models, including future models</strong>"
                        "<small>Broad grant: permits inference against any model served on this appliance now or in the future.</small></div>",
                        sanitize=False,
                    )
            with ui.element("div").classes("field"):
                ui.html('<label for="modal-select-key-duration">Expiration</label>', sanitize=False)
                self._sel_key_dur = ui.element("select").props('id="modal-select-key-duration"')
                with self._sel_key_dur:
                    for val, label in (
                        ("24h", "24 hours"),
                        ("7d", "7 days"),
                        ("30d", "30 days"),
                        ("90d", "90 days"),
                        ("", "No expiration"),
                    ):
                        sel = " selected" if val == "30d" else ""
                        ui.html(f'<option value="{views.esc(val)}"{sel}>{views.esc(label)}</option>', sanitize=False)
            with ui.element("div").classes("dialog-actions"):
                btn_k_cancel = Kit.button("Cancel")
                self._btn_k_confirm = Kit.button("Create API key", emphasis=Emphasis.PRIMARY)
        self._inp_key_alias.on("input", lambda e: setattr(self, "_create_key_alias", str(e.args or "")), js_handler="(e) => emit(e.target.value)")
        self._rad_scope_sel.on("change", lambda _: setattr(self, "_create_key_scope_mode", "selected"))
        self._rad_scope_all.on("change", lambda _: setattr(self, "_create_key_scope_mode", "all"))
        self._sel_key_dur.on("change", lambda e: setattr(self, "_create_key_duration", str(e.args or "")), js_handler="(e) => emit(e.target.value)")
        btn_k_cancel.on("click", lambda: self._close_dialog("modal-create-key", self._modal_action.get("opener", "")))
        self._btn_k_confirm.on("click", self._confirm_create_key)

        # 4. One-time secret handoff modal
        self._secret_dialog = (
            ui.element("dialog")
            .classes("oksa-modal wide")
            .props('id="modal-one-time-secret" aria-labelledby="modal-secret-title"')
        )
        with self._secret_dialog:
            ui.html(
                '<h2 id="modal-secret-title">Record API key secret now</h2>'
                "<p>This secret is shown once and is never stored by the OKSA frontend or in daemon operation receipts.</p>",
                sanitize=False,
            )
            self._secret_facts_box = ui.html("", sanitize=False)
            self._secret_code_box = ui.html("", sanitize=False)
            self._secret_copy_note = ui.html("", sanitize=False)
            self._secret_example_head = ui.html("", sanitize=False)
            self._secret_example = Kit.code_example(self._copy_text)
            with ui.element("div").classes("dialog-actions"):
                btn_copy_sec = Kit.button("Copy secret", icon="content_copy")
                btn_dismiss_sec = Kit.button("I have recorded this key", emphasis=Emphasis.PRIMARY)
        btn_copy_sec.on("click", self._copy_secret_to_clipboard)
        btn_dismiss_sec.on("click", lambda: self._close_dialog("modal-one-time-secret"))
        self._secret_dialog.on("close", self._on_secret_dialog_closed)

        # 5. Revoke key modal
        with ui.element("dialog").classes("oksa-modal").props('id="modal-revoke-key" aria-labelledby="modal-revoke-title"'):
            self._revoke_modal_body = ui.html("", sanitize=False)
            self._revoke_modal_err = ui.html("", sanitize=False)
            with ui.element("div").classes("dialog-actions"):
                btn_rev_cancel = Kit.button("Cancel")
                self._btn_rev_confirm = Kit.button("Revoke key", emphasis=Emphasis.DANGER)
        btn_rev_cancel.on("click", lambda: self._close_dialog("modal-revoke-key", self._modal_action.get("opener", "")))
        self._btn_rev_confirm.on("click", self._confirm_revoke_key)

        # 6. Delete user modal
        with ui.element("dialog").classes("oksa-modal").props('id="modal-delete-user" aria-labelledby="modal-del-user-title"'):
            self._del_user_body = ui.html("", sanitize=False)
            self._del_user_err = ui.html("", sanitize=False)
            with ui.element("div").classes("dialog-actions"):
                btn_du_cancel = Kit.button("Cancel")
                self._btn_du_confirm = Kit.button("Delete user and revoke keys", emphasis=Emphasis.DANGER)
        btn_du_cancel.on("click", lambda: self._close_dialog("modal-delete-user", self._modal_action.get("opener", "")))
        self._btn_du_confirm.on("click", self._confirm_delete_user)

        # 7. Cancel operation modal
        with ui.element("dialog").classes("oksa-modal").props('id="modal-cancel-job" aria-labelledby="modal-cancel-job-title"'):
            self._cancel_job_body = ui.html("", sanitize=False)
            self._cancel_job_err = ui.html("", sanitize=False)
            with ui.element("div").classes("dialog-actions"):
                btn_cj_keep = Kit.button("Keep running")
                self._btn_cj_confirm = Kit.button("Cancel operation", emphasis=Emphasis.DANGER)
        btn_cj_keep.on("click", lambda: self._close_dialog("modal-cancel-job", self._modal_action.get("opener", "")))
        self._btn_cj_confirm.on("click", self._confirm_cancel_job)

    async def _open_serving_modal(self, method: str, opener_id: str, *, model_id: str = "") -> None:
        await self._ensure_namespace()
        sel_id = str((self.status or {}).get("selected_model_id") or "-")
        act_id = str((self.status or {}).get("active_model_id") or "-")
        sel_pub = str((self.status or {}).get("selected_model") or "-")
        act_pub = str((self.status or {}).get("active_model") or "-")
        target_id = model_id or (sel_id if sel_id != "-" else act_id)
        model_obj = next((m for m in self.models if m.get("id") == target_id), None)
        rev = str((model_obj or {}).get("revision") or "-")
        measure_note = {
            "measured": "A recorded calibration profile matches this hardware and runtime; no GPU measurement is needed.",
            "required": (
                "No recorded calibration profile matches this hardware and runtime: GPU measurement runs first and can take "
                "up to the 4-hour search budget plus setup and cleanup."
            ),
        }.get(
            str((model_obj or {}).get("calibration")),
            "If no matching calibrated profile exists, GPU measurement can take up to the 4-hour search budget plus setup and cleanup.",
        )

        self._pending_request_id = self._mint_request_id()
        self._modal_action = {
            "method": method,
            "target": target_id,
            "expected_selected": sel_id,
            "expected_active": act_id,
            "expected_revision": rev,
            "opener": opener_id,
        }
        self._srv_modal_err.content = ""

        if method == "StartModel":
            confirm, emphasis = f"Serve {target_id}", Emphasis.PRIMARY
            self._srv_modal_body.content = (
                f'<h2 id="modal-serving-title">Serve {views.esc(target_id)}</h2>'
                f"<p>Changes the shared appliance serving selection to <code>{views.esc(target_id)}</code> "
                f"(selected now: <code>{views.esc(sel_pub)}</code>, running: <code>{views.esc(act_pub)}</code>).</p>"
                "<p><strong>Impact:</strong> Any active model workload will be replaced, interrupting shared inference. "
                f"{measure_note}</p>"
            )
        elif method == "RestartModel":
            confirm, emphasis = f"Restart {target_id}", Emphasis.PRIMARY
            self._srv_modal_body.content = (
                f'<h2 id="modal-serving-title">Restart {views.esc(target_id)}</h2>'
                f"<p>Restarts the shared inference runtime for <code>{views.esc(target_id)}</code> "
                f"(running: <code>{views.esc(act_pub)}</code>).</p>"
                "<p><strong>Impact:</strong> In-flight inference requests will be interrupted while the workload pod restarts.</p>"
            )
        elif method == "StopServing":
            confirm, emphasis = "Stop serving", Emphasis.DANGER
            self._srv_modal_body.content = (
                '<h2 id="modal-serving-title">Stop all model serving</h2>'
                f"<p>Clears the desired serving selection (<code>{views.esc(sel_pub)}</code>) and scales down active inference workloads (<code>{views.esc(act_pub)}</code>).</p>"
                "<p><strong>Impact:</strong> All shared inference on this appliance will stop until a model is explicitly served again.</p>"
            )
        elif method == "RemoveModel":
            confirm, emphasis = f"Delete {target_id}", Emphasis.DANGER
            active_warn = (
                "<p><strong>Active model warning:</strong> This model is currently selected or active; deletion will stop serving before removing files.</p>"
                if target_id in (sel_id, act_id)
                else ""
            )
            self._srv_modal_body.content = (
                f'<h2 id="modal-serving-title">Delete {views.esc(target_id)}</h2>'
                f"<p>Permanently deletes local model files at <code>/var/lib/vllm/models/{views.esc(target_id)}</code> "
                f"(revision <code>{views.esc(rev[:12] if rev and rev != '-' else 'unknown')}</code>) and removes it from the catalog.</p>"
                f"{active_warn}"
                "<p>Saved calibration records in <code>/var/lib/puu/calibration</code> are not deleted by model removal.</p>"
            )
        else:
            raise ValueError(f"Unsupported serving action: {method}")
        self._srv_actions.clear()
        with self._srv_actions:
            Kit.button("Cancel", lambda: self._close_dialog("modal-serving-action", opener_id))
            self._btn_srv_confirm = Kit.button(confirm, self._confirm_serving_modal, emphasis=emphasis)
        self._open_dialog("modal-serving-action")

    async def _confirm_serving_modal(self) -> None:
        method = self._modal_action.get("method", "")
        target = self._modal_action.get("target", "")
        exp_sel = self._modal_action.get("expected_selected", "")
        exp_act = self._modal_action.get("expected_active", "")
        exp_rev = self._modal_action.get("expected_revision", "")
        opener = self._modal_action.get("opener", "")

        self._btn_srv_confirm.disable()
        self._srv_modal_err.content = ""

        if method in ("StartModel", "RestartModel"):
            args = (target, exp_sel, exp_act)
        elif method == "StopServing":
            args = (exp_sel, exp_act)
            target = "runtime"
        elif method == "RemoveModel":
            args = (target, exp_rev, exp_sel, exp_act)
        else:
            return

        ok, res, err_msg = await self._submit_mutation(self._pending_request_id, method, target, *args)
        if ok and res.get("job"):
            self._close_dialog("modal-serving-action", opener)
            jid = views.job_short_id(res["job"])
            self._navigate(f"/operations/{views.url_part(jid)}")
            return

        self._pending_request_id = self._mint_request_id()
        self._btn_srv_confirm.enable()
        self._srv_modal_err.content = f'<div class="notice error" role="alert">{views.esc(err_msg)}</div>'
        self._render_all()

    async def _open_create_user_modal(self, opener_id: str) -> None:
        await self._ensure_namespace()
        self._pending_request_id = self._mint_request_id()
        self._modal_action = {"opener": opener_id}
        self._create_user_id = ""
        self._create_user_email = ""
        self._user_modal_err.content = ""
        self._btn_u_confirm.enable()
        self.client.run_javascript(
            "const u = document.getElementById('modal-input-user-id'); if (u) u.value = '';"
            "const e = document.getElementById('modal-input-user-email'); if (e) e.value = '';"
        )
        self._open_dialog("modal-create-user")

    async def _confirm_create_user(self) -> None:
        uid = self._create_user_id.strip()
        email = self._create_user_email.strip()
        opener = self._modal_action.get("opener", "")
        if not uid:
            self._user_modal_err.content = '<div class="notice error" role="alert">User ID is required.</div>'
            return
        self._btn_u_confirm.disable()
        self._user_modal_err.content = ""
        ok, _, err_msg = await self._submit_mutation(self._pending_request_id, "CreateUser", uid, uid, email, -1.0)
        if ok:
            await self._refresh_access()
            self._close_dialog("modal-create-user", opener)
            self._navigate(f"/access/user?id={views.url_part(uid)}")
            return
        self._pending_request_id = self._mint_request_id()
        self._btn_u_confirm.enable()
        self._user_modal_err.content = f'<div class="notice error" role="alert">{views.esc(err_msg)}</div>'
        await self._refresh_access()
        self._render_all()

    async def _open_create_key_modal(self, user_id: str, opener_id: str) -> None:
        await self._ensure_namespace()
        await self._refresh_models()
        self._pending_request_id = self._mint_request_id()
        self._modal_action = {"user_id": user_id, "opener": opener_id}
        self._create_key_alias = ""
        self._create_key_scope_mode = "selected"
        self._create_key_selected_models = set()
        self._create_key_duration = "30d"
        self._key_modal_err.content = ""
        self._btn_k_confirm.enable()
        self._key_modal_head.content = (
            f'<h2 id="modal-key-title">Create API key for <code>{views.esc(user_id)}</code></h2>'
            "<p>Choose an explicit key alias, model scope, and expiration. The generated secret will be displayed once upon creation.</p>"
        )
        self._key_models_box.clear()
        with self._key_models_box:
            if not self.models_verified:
                ui.html(
                    '<div class="notice warning">Model inventory could not be verified; selected-model scope cannot be chosen until inventory is readable.</div>',
                    sanitize=False,
                )
            elif not self.models:
                ui.html(
                    '<div class="notice warning">No models are currently installed. Install a model first or explicitly select All models below.</div>',
                    sanitize=False,
                )
            else:
                for m in self.models:
                    pub = str(m.get("name") or m.get("id") or "")
                    mid = str(m.get("id") or "")
                    with ui.element("label").classes("option"):
                        chk = ui.element("input").props('type="checkbox"')
                        ui.html(
                            f"<div><strong><code>{views.esc(pub)}</code></strong>"
                            f"<small>Local storage ID: {views.esc(mid)}</small></div>",
                            sanitize=False,
                        )
                        chk.on(
                            "change",
                            lambda e, p=pub: (
                                self._create_key_selected_models.add(p)
                                if e.args
                                else self._create_key_selected_models.discard(p)
                            ),
                            js_handler="(e) => emit(Boolean(e.target.checked))",
                        )
        self.client.run_javascript(
            "const a = document.getElementById('modal-input-key-alias'); if (a) a.value = '';"
            "const r = document.querySelector('input[name=\"key-scope\"][value=\"selected\"]'); if (r) r.checked = true;"
            "const d = document.getElementById('modal-select-key-duration'); if (d) d.value = '30d';"
        )
        self._open_dialog("modal-create-key")

    async def _confirm_create_key(self) -> None:
        user_id = self._modal_action.get("user_id", "")
        opener = self._modal_action.get("opener", "")
        alias = self._create_key_alias.strip()
        if not alias:
            self._key_modal_err.content = '<div class="notice error" role="alert">Key alias is required.</div>'
            return
        if alias == "open-webui":
            self._key_modal_err.content = '<div class="notice error" role="alert">The open-webui alias is reserved for the protected service key.</div>'
            return
        if self._create_key_scope_mode == "selected":
            if not self.models_verified:
                self._key_modal_err.content = (
                    '<div class="notice error" role="alert">Model discovery failed; refusing to broaden scope automatically.</div>'
                )
                return
            if not self._create_key_selected_models:
                self._key_modal_err.content = (
                    '<div class="notice error" role="alert">Select at least one public model, or explicitly choose All models, including future models.</div>'
                )
                return
            models_arg = sorted(self._create_key_selected_models)
            scope_label = ", ".join(models_arg)
        else:
            models_arg = []
            scope_label = "All models, including future models"

        self._btn_k_confirm.disable()
        self._key_modal_err.content = ""
        ok, res, err_msg = await self._submit_mutation(
            self._pending_request_id,
            "CreateKey",
            user_id,
            user_id,
            alias,
            models_arg,
            -1.0,
            self._create_key_duration,
        )
        await self._refresh_user_detail(user_id)
        if ok and isinstance(res.get("key"), dict):
            key_obj = res["key"]
            self.secret = str(key_obj.get("key") or "")
            self._close_dialog("modal-create-key", "")
            self._show_secret_modal(
                owner=user_id,
                alias=alias,
                key_id=str(key_obj.get("key_id") or ""),
                scope_label=scope_label,
                models=models_arg,
                expires=str(key_obj.get("expires") or self._create_key_duration or "No expiration"),
                opener=opener,
            )
            return

        self._pending_request_id = self._mint_request_id()
        self._btn_k_confirm.enable()
        self._key_modal_err.content = f'<div class="notice error" role="alert">{views.esc(err_msg)}</div>'
        self._render_all()

    def _show_secret_modal(
        self,
        *,
        owner: str,
        alias: str,
        key_id: str,
        scope_label: str,
        models: list[str],
        expires: str,
        opener: str,
    ) -> None:
        self.secret_meta = {"opener": opener}
        active_pub = str((self.status or {}).get("active_model") or "None active")
        self._secret_facts_box.content = views.render_facts([
            ("Owner user ID", f"<code>{views.esc(owner)}</code>"),
            ("Key alias / ID", f"<strong>{views.esc(alias)}</strong> · <code>{views.esc(key_id)}</code>"),
            ("API base URL", f"<code>{views.esc(self._api_base_url())}</code>"),
            ("Running model", f"<code>{views.esc(active_pub)}</code>"),
            ("Granted model scope", views.esc(scope_label)),
            ("Expiration", views.esc(expires)),
        ])
        self._secret_code_box.content = f'<code class="secret-box" id="one-time-secret-code">{views.esc(self.secret)}</code>'
        self._secret_copy_note.content = ""
        model = views.quickstart_model(str((self.status or {}).get("active_model") or ""), models, self._model_names())
        self._secret_example_head.content = "<h3>Try it</h3>" + self._quickstart_note(model)
        self._secret_example.show(views.quickstart_snippets(self._api_base_url(), model, self.secret) if model else {})
        self._open_dialog("modal-one-time-secret")

    def _model_names(self) -> list[str]:
        return [name for m in self.models if (name := str(m.get("name") or m.get("id") or ""))]

    def _quickstart_note(self, model: str | None, lead: str = "") -> str:
        if not model:
            return '<p class="muted">Install a model to get a ready-to-run example.</p>'
        keyless = ""
        if (self.status or {}).get("auth_enforcement") == "keyless":
            keyless = " The gateway does not check keys yet, so requests succeed without one."
        return f"<p>{lead}A chat request to <code>{views.esc(model)}</code>.{keyless}</p>"

    async def _copy_secret_to_clipboard(self) -> None:
        if not self.secret:
            return
        if await self._copy_text(self.secret):
            self._secret_copy_note.content = '<p class="small">Copied to clipboard. Clear clipboard after storing in your secret manager.</p>'
        else:
            self._secret_copy_note.content = '<p class="small">The browser refused clipboard access. Select the secret and copy it manually.</p>'

    def _on_secret_dialog_closed(self) -> None:
        opener = self.secret_meta.get("opener")
        self._clear_secret()
        if opener and not self.client.is_deleted:
            self.client.run_javascript(f"document.getElementById({json.dumps(opener)})?.focus();")

    async def _open_revoke_key_modal(self, key_item: dict, opener_id: str) -> None:
        await self._ensure_namespace()
        self._pending_request_id = self._mint_request_id()
        self._revoke_key_target = dict(key_item)
        self._modal_action = {"opener": opener_id}
        self._revoke_modal_err.content = ""
        self._btn_rev_confirm.enable()

        alias = views.esc(key_item.get("key_alias") or "Unnamed key")
        kid = views.esc(key_item.get("key_id") or "")
        prefix = views.esc(key_item.get("key_prefix") or "—")
        owner = views.esc(key_item.get("user_id") or "")
        self._revoke_modal_body.content = (
            f'<h2 id="modal-revoke-title">Revoke API key {alias}</h2>'
            f"<p>Permanently removes key <strong>{alias}</strong> (ID <code>{kid}</code>, prefix <code>{prefix}</code>) "
            f"owned by user <code>{owner}</code> from the gateway database.</p>"
            "<p><strong>Caveat:</strong> Revocation confirms database absence only; it does not certify immediate cache propagation or block anonymous requests when keyless mode is enabled.</p>"
        )
        self._open_dialog("modal-revoke-key")

    async def _confirm_revoke_key(self) -> None:
        owner = str(self._revoke_key_target.get("user_id") or "")
        kid = str(self._revoke_key_target.get("key_id") or "")
        self._btn_rev_confirm.disable()
        self._revoke_modal_err.content = ""
        ok, _, err_msg = await self._submit_mutation(
            self._pending_request_id, "RevokeKey", f"{owner}:{kid}", owner, kid
        )
        await self._refresh_user_detail(owner)
        if ok:
            self._close_dialog("modal-revoke-key", "btn-open-create-key")
            return
        self._pending_request_id = self._mint_request_id()
        self._btn_rev_confirm.enable()
        self._revoke_modal_err.content = f'<div class="notice error" role="alert">{views.esc(err_msg)}</div>'
        self._render_all()

    async def _open_delete_user_modal(self, user_id: str, opener_id: str) -> None:
        await self._ensure_namespace()
        self._pending_request_id = self._mint_request_id()
        self._modal_action = {"user_id": user_id, "opener": opener_id}
        self._del_user_err.content = ""
        self._btn_du_confirm.enable()
        key_cnt = len(self.user_detail_keys)
        self._del_user_body.content = (
            f'<h2 id="modal-del-user-title">Delete API user {views.esc(user_id)}</h2>'
            f"<p>Revokes all <strong>{key_cnt}</strong> API key(s) owned by <code>{views.esc(user_id)}</code>, "
            "verifies their absence in the gateway database, and then deletes the user record.</p>"
            "<p>If key revocation or user deletion is only partially confirmed, the operation records how many were confirmed and blocks further changes to this user until the cleanup is finished.</p>"
        )
        self._open_dialog("modal-delete-user")

    async def _confirm_delete_user(self) -> None:
        user_id = self._modal_action.get("user_id", "")
        opener = self._modal_action.get("opener", "")
        self._btn_du_confirm.disable()
        self._del_user_err.content = ""
        ok, res, err_msg = await self._submit_mutation(
            self._pending_request_id, "DeleteUser", user_id, user_id
        )
        if ok and res.get("job"):
            self._close_dialog("modal-delete-user", opener)
            jid = views.job_short_id(res["job"])
            self._navigate(f"/operations/{views.url_part(jid)}")
            return
        self._pending_request_id = self._mint_request_id()
        self._btn_du_confirm.enable()
        self._del_user_err.content = f'<div class="notice error" role="alert">{views.esc(err_msg)}</div>'
        self._render_all()

    def _open_cancel_job_modal(self, job_path: str, opener_id: str) -> None:
        self._cancel_job_path = job_path
        self._modal_action = {"opener": opener_id}
        self._cancel_job_err.content = ""
        self._btn_cj_confirm.enable()
        job = self.jobs.get(job_path) or {}
        jid = views.job_short_id(job_path)
        op_name = views.operation_label(str(job.get("operation") or ""))
        target = job.get("model_name") or job.get("user_id") or "runtime"
        self._cancel_job_body.content = (
            f'<h2 id="modal-cancel-job-title">Cancel {views.esc(jid)}?</h2>'
            f"<p>Sends a termination request (<code>SIGTERM</code>) to running job <strong>{views.esc(op_name)}</strong> on <code>{views.esc(target)}</code>.</p>"
            "<p><strong>Important:</strong> Cancellation is a request, not a rollback. It may race completion, and partial changes to staged files, calibration records, or runtime state may remain.</p>"
        )
        self._open_dialog("modal-cancel-job")

    async def _confirm_cancel_job(self) -> None:
        path = self._cancel_job_path
        opener = self._modal_action.get("opener", "")
        self._btn_cj_confirm.disable()
        self._cancel_job_err.content = ""
        reply = await self._call(self.service.cancel_job, path)
        await self._refresh_daemon_state()
        await self._refresh_jobs()
        if reply[0] == 200 or (self.jobs.get(path) or {}).get("state") != "running":
            self._close_dialog("modal-cancel-job", opener)
            self._render_all()
            return
        self._btn_cj_confirm.enable()
        self._cancel_job_err.content = f'<div class="notice error" role="alert">{views.esc(self._error(reply))}</div>'
        self._render_all()
