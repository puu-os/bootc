# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

from dataclasses import dataclass, field
from datetime import datetime, timezone
import html
import json
import math
import shlex
from urllib.parse import quote

from oksa_web_kit import Kit, Tone


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def url_part(value: str) -> str:
    return quote(str(value or ""), safe="")


def _valid_time(timestamp: object) -> bool:
    return type(timestamp) in (int, float) and math.isfinite(timestamp) and timestamp > 0


def fmt_utc(timestamp: float | int | None) -> str:
    if not _valid_time(timestamp):
        return "Unknown time"
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def fmt_ago(timestamp: float | int | None, now: float) -> str:
    """A moment relative to now, to the minute: "just now", "5 min ago", "in 2 h", "3 days ago"."""
    if not _valid_time(timestamp):
        return "at an unknown time"
    delta = now - timestamp
    minutes = int(abs(delta) // 60)
    if minutes == 0:
        return "just now"
    if minutes < 60:
        amount = f"{minutes} min"
    elif minutes < 24 * 60:
        amount = f"{minutes // 60} h"
    else:
        days = minutes // (24 * 60)
        amount = f"{days} day" if days == 1 else f"{days} days"
    return f"{amount} ago" if delta > 0 else f"in {amount}"


def fmt_time(timestamp: float | int | None, now: float) -> str:
    """Markup for a moment relative to now, carrying the exact UTC time as tooltip and machine value."""
    if not _valid_time(timestamp):
        return esc(fmt_ago(timestamp, now))
    exact = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
    return (
        f'<time datetime="{exact.isoformat(timespec="seconds")}" title="{esc(fmt_utc(timestamp))}">'
        f"{esc(fmt_ago(timestamp, now))}</time>"
    )


def disclosure(summary: str, body_html: str) -> str:
    """Supporting explanation folded behind a one-line summary."""
    return f'<details class="disclosure"><summary>{esc(summary)}</summary>{body_html}</details>'


def fmt_mib(value: int | float | None) -> str:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return "Unavailable"
    if value >= 1024:
        return f"{value / 1024:.1f} GiB ({int(round(value))} MiB)"
    return f"{int(round(value))} MiB"


def job_short_id(path: str | None) -> str:
    if not isinstance(path, str) or "/" not in path:
        return str(path or "unknown")
    return path.rsplit("/", 1)[-1]


def fmt_bytes(value: int | float) -> str:
    for unit, size in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if value >= size:
            return f"{value / size:.1f} {unit}"
    return f"{int(value)} B"


def fmt_duration(seconds: float) -> str:
    hours, rest = divmod(max(0, int(seconds)), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


@dataclass(frozen=True)
class ProgressReading:
    """A job's reported progress as the phase, the completed fraction (None if unknown) and its figures."""

    phase: str
    message: str
    fraction: float | None
    percent: str
    figures: str

    @classmethod
    def of(cls, progress: dict | None, now: float) -> "ProgressReading":
        if not isinstance(progress, dict):
            return cls("Starting", "", None, "", "Waiting for the first progress report")
        phase, message = str(progress.get("phase") or "Working"), str(progress.get("message") or "")
        started = progress.get("phase_started_at") or now
        done, total = progress.get("bytes_done"), progress.get("bytes_total")
        rate, deadline = progress.get("bytes_per_second"), progress.get("deadline_at")
        if isinstance(done, int) and isinstance(total, int) and total > 0:
            figures = [f"{fmt_bytes(done)} of {fmt_bytes(total)}"]
            if isinstance(rate, (int, float)) and rate > 0:
                figures += [f"{fmt_bytes(rate)}/s", f"about {fmt_duration((total - done) / rate)} left"]
            fraction = done / total
            return cls(phase, message, fraction, f"{math.floor(fraction * 100)}%", " · ".join(figures))
        if isinstance(deadline, (int, float)) and deadline > started:
            elapsed, budget = now - started, deadline - started
            return cls(phase, message, min(max(elapsed / budget, 0.0), 1.0), "",
                       f"{fmt_duration(elapsed)} elapsed of {fmt_duration(budget)} budget")
        return cls(phase, message, None, "", f"{fmt_duration(now - started)} elapsed")


def render_job_progress(progress: dict | None, now: float, *, compact: bool = False) -> str:
    reading = ProgressReading.of(progress, now)
    value = "" if reading.fraction is None else f' value="{reading.fraction:.4f}"'
    percent = f'<span class="job-progress-percent">{esc(reading.percent)}</span>' if reading.percent else ""
    message = (f'<div class="job-progress-message mono">{esc(reading.message)}</div>'
               if reading.message and not compact else "")
    return (
        f'<div class="job-progress{" compact" if compact else ""}">'
        f'<div class="job-progress-head"><span class="job-progress-phase">{esc(reading.phase)}</span>{percent}</div>'
        f'<progress max="1"{value} aria-label="{esc(reading.phase)}"></progress>'
        f'<div class="job-progress-figures">{esc(reading.figures)}</div>{message}</div>'
    )


def render_activity(jobs: list[dict], now: float) -> str:
    """The header indicator naming the oldest running job and its progress, linked to its detail page."""
    running = sorted((job for job in jobs if job.get("state") == "running"),
                     key=lambda job: float(job.get("started_at") or 0.0))
    if not running:
        return ""
    job = running[0]
    reading = ProgressReading.of(job.get("progress"), now)
    label = operation_label(str(job.get("operation") or ""))
    more = f'<span class="activity-more">+{len(running) - 1}</span>' if len(running) > 1 else ""
    return (
        f'<a class="activity" href="/operations/{url_part(job_short_id(job.get("path")))}" '
        f'title="{esc(reading.phase)} · {esc(reading.figures)}">'
        f'<span class="activity-pulse" aria-hidden="true"></span>'
        f'<span class="activity-label">{esc(label)}</span>'
        f'<strong>{esc(reading.percent or reading.phase)}</strong>{more}</a>'
    )

def operation_label(operation: str) -> str:
    labels = {
        "DownloadModel": "Download model",
        "StartModel": "Serve model",
        "RestartModel": "Restart serving model",
        "StopServing": "Stop serving",
        "RemoveModel": "Remove model installation",
        "Calibrate": "Calibrate model",
        "DeleteUser": "Delete API user",
        "CreateUser": "Create API user",
        "CreateKey": "Create API key",
        "RevokeKey": "Revoke API key",
    }
    return labels.get(operation, operation or "Operation")


def render_breadcrumbs(items: list[tuple[str, str | None]]) -> str:
    parts = []
    for idx, (label, href) in enumerate(items):
        if idx > 0:
            parts.append('<span aria-hidden="true">/</span>')
        if href and idx < len(items) - 1:
            parts.append(f'<a href="{esc(href)}">{esc(label)}</a>')
        elif idx == len(items) - 1:
            parts.append(f'<span aria-current="page">{esc(label)}</span>')
        else:
            parts.append(f"<span>{esc(label)}</span>")
    return f'<nav class="breadcrumbs" aria-label="Breadcrumb">{"".join(parts)}</nav>'


def render_facts(rows: list[tuple[str, str]]) -> str:
    items = "".join(f"<dt>{esc(label)}</dt><dd>{value_html}</dd>" for label, value_html in rows)
    return f'<dl class="facts">{items}</dl>'


API_KEY_ENV = "OKSA_API_KEY"


def quickstart_model(active: str, scope: list[str], installed: list[str]) -> str | None:
    """The public model name a first request should use; an empty scope permits every model."""
    if active and (not scope or active in scope):
        return active
    if scope:
        return scope[0]
    return installed[0] if installed else None


def quickstart_snippets(base_url: str, model: str, api_key: str | None = None) -> dict[str, str]:
    """Ready-to-run chat requests; without a key they read it from the environment."""
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "Hello"}]})
    auth = shlex.quote(f"Authorization: Bearer {api_key}") if api_key else f'"Authorization: Bearer ${API_KEY_ENV}"'
    curl = "\n".join([
        f"curl {shlex.quote(base_url.rstrip('/') + '/chat/completions')} \\",
        f"  -H {auth} \\",
        '  -H "Content-Type: application/json" \\',
        f"  -d {shlex.quote(body)}",
    ])
    key_expr = json.dumps(api_key) if api_key else f'os.environ["{API_KEY_ENV}"]'
    python = "\n".join([
        "# pip install openai",
        *([] if api_key else ["import os"]),
        "from openai import OpenAI",
        "",
        "client = OpenAI(",
        f"    base_url={json.dumps(base_url)},",
        f"    api_key={key_expr},",
        ")",
        "reply = client.chat.completions.create(",
        f"    model={json.dumps(model)},",
        '    messages=[{"role": "user", "content": "Hello"}],',
        ")",
        "print(reply.choices[0].message.content)",
    ])
    return {"curl": curl, "Python": python}


@dataclass(frozen=True)
class Reading:
    """One aspect of the appliance's current state."""

    label: str
    text: str
    tone: Tone
    detail: str = ""
    href: str = field(kw_only=True)


@dataclass(frozen=True)
class Health:
    """Readings rolled up so that the most urgent one speaks for the appliance."""

    readings: tuple[Reading, ...]

    @property
    def worst(self) -> Reading:
        return max(self.readings, key=lambda reading: reading.tone.severity)

    @property
    def headline(self) -> str:
        worst = self.worst
        return "Healthy" if worst.tone is Tone.OK else f"{worst.label}: {worst.text}"

    def summary_html(self) -> str:
        return Kit.status(self.headline, self.worst.tone)

    def panel_html(self) -> str:
        rows = [
            (
                reading.label,
                Kit.status(reading.text, reading.tone)
                + (f'<div class="small muted">{esc(reading.detail)}</div>' if reading.detail else ""),
            )
            for reading in self.readings
        ]
        return f'<h2 id="health-heading">System health</h2>{render_facts(rows)}'

    def tiles_html(self) -> str:
        """Every reading as a tile linking to the page that explains it."""
        tiles = "".join(
            f'<li><a class="tile {reading.tone.name.lower()}" href="{esc(reading.href)}">'
            f'<span class="tile-label">{esc(reading.label)}</span>'
            f"{Kit.status(reading.text, reading.tone)}"
            + (f'<span class="tile-detail">{esc(reading.detail)}</span>' if reading.detail else "")
            + "</a></li>"
            for reading in self.readings
        )
        return f'<ul class="tiles">{tiles}</ul>'


def render_unresolved_banner(unresolved: list[dict], now: float) -> str:
    if not unresolved:
        return ""
    blocks = []
    for item in unresolved:
        req_id = esc(item.get("request_id") or "unknown")
        op = esc(operation_label(str(item.get("operation") or "")))
        changes = {"model": "model", "user": "API user and key"}.get(str(item.get("scope")), "related")
        target = esc(item.get("target") or "unknown")
        err_code = esc(item.get("error_code") or "Unconfirmed")
        err_msg = esc(item.get("error_message") or item.get("summary") or "Outcome could not be verified.")
        job_path = item.get("job_path")
        rows = [("Request ID", f"<code>{req_id}</code>")]
        if isinstance(job_path, str) and job_path.strip():
            jid = job_short_id(job_path)
            rows.append(("Operation", f'<a href="/operations/{url_part(jid)}" class="mono">{esc(jid)}</a>'))
        rows += [
            ("Recorded", fmt_time(item.get("updated_at"), now)),
            ("Last reported", f"{err_msg} <code>{err_code}</code>"),
        ]
        blocks.append(
            f'<div class="notice error" role="alert">'
            f"<p><strong>Outcome unknown · {op} on <code>{target}</code></strong></p>"
            f"{render_facts(rows)}"
            f"<p>Other {changes} changes stay blocked until this is resolved. "
            f"After checking the affected resources on the host, resolve it as root with:</p>"
            f'<pre class="mono">sudo /usr/libexec/puu/oksa-daemon --reconcile {req_id}</pre>'
            f"</div>"
        )
    return "".join(blocks)


def render_serving_status(
    status: dict | None,
    snapshot: dict,
    models: list[dict],
    *,
    disconnected: bool,
    status_failed: bool,
    now: float,
) -> str:
    if status is None:
        reason = snapshot.get("error") or ("Daemon connection unavailable." if disconnected else "Waiting for the first status update.")
        return (
            f'<div class="notice {"error" if status_failed or disconnected else "warning"}">'
            f"<strong>Serving status is unavailable.</strong> {esc(reason)} "
            f"An unknown state is never shown as stopped or healthy."
            f"</div>"
        )
    sampled_at = fmt_time(snapshot.get("sampled_at"), now)
    stale = bool(snapshot.get("stale")) or status_failed or disconnected
    serving_enabled = status.get("serving_enabled") is True
    selected_name = str(status.get("selected_model") or "-")
    active_name = str(status.get("active_model") or "-")
    selected_id = str(status.get("selected_model_id") or "-")
    active_id = str(status.get("active_model_id") or "-")
    installed_ids = {m.get("id") for m in models if isinstance(m, dict)}

    if not serving_enabled:
        desired_html = f'{Kit.status("Serving disabled", Tone.NEUTRAL)} <span class="muted">· stopped with Stop serving</span>'
    elif selected_id != "-" and selected_id in installed_ids:
        desired_html = f'<a href="/models/{url_part(selected_id)}">{esc(selected_id)}</a> <span class="muted">· public name <code>{esc(selected_name)}</code></span>'
    elif selected_id != "-":
        desired_html = f"<code>{esc(selected_id)}</code> <span class=\"muted\">· public name <code>{esc(selected_name)}</code></span>"
    else:
        desired_html = '<span class="muted">No explicit model pinned · planner uses autostart policy</span>'

    if active_id != "-" and active_id in installed_ids:
        running_html = f'<a href="/models/{url_part(active_id)}">{esc(active_id)}</a> <span class="muted">· public name <code>{esc(active_name)}</code></span>'
    elif active_id != "-":
        running_html = f"<code>{esc(active_id)}</code> <span class=\"muted\">· public name <code>{esc(active_name)}</code></span>"
    else:
        running_html = "None running"

    model_status = str(status.get("model_status") or "unavailable")
    if stale:
        health_html = f'{Kit.status(f"Stale ({model_status})", Tone.WARNING)} <span class="muted">· last updated {sampled_at}</span>'
    elif model_status == "ready":
        health_html = f'{Kit.status("Healthy (vLLM /health 200)")} <span class="muted">· checked {sampled_at}</span>'
    elif model_status == "not-ready":
        health_html = f'{Kit.status("Starting / not ready", Tone.BUSY)} <span class="muted">· checked {sampled_at}</span>'
    elif model_status == "stopped":
        health_html = f'{Kit.status("Stopped", Tone.NEUTRAL)} <span class="muted">· checked {sampled_at}</span>'
    else:
        health_html = f'{Kit.status("Unavailable", Tone.ERROR)} <span class="muted">· checked {sampled_at}</span>'

    gw = str(status.get("gateway_status") or "unreachable")
    if gw == "ready":
        gw_badge = Kit.status("Ready", Tone.WARNING if stale else Tone.OK)
    elif gw == "authentication-required":
        gw_badge = Kit.status("Reachable (auth required)", Tone.WARNING if stale else Tone.OK)
    else:
        gw_badge = Kit.status("Unreachable", Tone.ERROR)
    gw_html = f'{gw_badge} <span class="muted">· responds to health checks; API requests are not tested</span>'

    auth = str(status.get("auth_enforcement") or "unknown")
    if auth == "enforced":
        auth_html = f'{Kit.status("Enforced")} <span class="muted">· requests without a key are rejected</span>'
    elif auth == "keyless":
        auth_html = (
            f'{Kit.status("Keyless access enabled", Tone.WARNING)} '
            f'<span class="muted">· anonymous requests are accepted until litellm-master-key is configured</span>'
        )
    else:
        auth_html = (
            f'{Kit.status("Enforcement not verified", Tone.NEUTRAL)} '
            f'<span class="muted">· keyless access may be possible</span>'
        )

    api_url = str(status.get("api_base_url") or "http://api.puu.local/v1")
    prep = status.get("preparation")
    rows = [
        ("Selected model", desired_html),
        ("Running model", running_html),
        ("Model health", health_html),
        ("Gateway readiness", gw_html),
        ("Gateway authentication", auth_html),
        ("Applied configuration", "Not reported. Recorded calibration limits may differ from the running settings."),
        ("API base URL", f"<code>{esc(api_url)}</code>"),
    ]
    if isinstance(prep, dict) and prep.get("model"):
        p_model = str(prep.get("model") or "")
        p_state = str(prep.get("state") or "")
        p_msg = str(prep.get("message") or "")
        tone = Tone.BUSY if p_state == "running" else (Tone.ERROR if p_state == "error" else Tone.NEUTRAL)
        rows.append((
            "Calibration",
            f'{Kit.status(f"{p_state} ({p_model})", tone)} <span class="muted">· {esc(p_msg)} ({fmt_time(prep.get("updated_at"), now)})</span>',
        ))
    return render_facts(rows)


def render_gpus_table(status: dict | None) -> str:
    if not isinstance(status, dict) or status.get("gpus") is None:
        return Kit.empty("GPU telemetry is unavailable.")
    gpus = status.get("gpus") or []
    if not gpus:
        return Kit.empty("No supported GPU devices detected on this host.")
    rows = []
    for gpu in gpus:
        if not isinstance(gpu, dict):
            continue
        idx = esc(gpu.get("index") if gpu.get("index") is not None else "—")
        name = esc(gpu.get("name") or "Unknown accelerator")
        util = f"{gpu['gpu_util_pct']:g}%" if isinstance(gpu.get("gpu_util_pct"), (int, float)) else "Unavailable"
        used = gpu.get("memory_used_mib")
        total = gpu.get("memory_total_mib")
        if isinstance(used, (int, float)) and isinstance(total, (int, float)) and total > 0:
            mem = f"{used / 1024:.1f} / {total / 1024:.1f} GiB"
        else:
            mem = "Unavailable"
        temp = f"{gpu['temperature_c']:g} °C" if isinstance(gpu.get("temperature_c"), (int, float)) else "Unavailable"
        power = f"{gpu['power_w']:g} W" if isinstance(gpu.get("power_w"), (int, float)) else "Unavailable"
        rows.append(
            f"<tr><td>{idx}</td><td>{name}</td><td class=\"numeric\">{esc(util)}</td>"
            f"<td class=\"numeric\">{esc(mem)}</td><td class=\"numeric\">{esc(temp)}</td>"
            f"<td class=\"numeric\">{esc(power)}</td></tr>"
        )
    return (
        '<div class="table-scroll" tabindex="0" role="region" aria-label="GPUs">'
        '<table><thead><tr><th scope="col">Index</th><th scope="col">Name</th>'
        '<th scope="col" class="numeric">Utilization</th><th scope="col" class="numeric">Memory used / total</th>'
        '<th scope="col" class="numeric">Temperature</th><th scope="col" class="numeric">Power</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def render_workloads_table(status: dict | None) -> str:
    if not isinstance(status, dict) or status.get("workloads") is None:
        return Kit.empty("Kubernetes workload status is unavailable.")
    workloads = status.get("workloads") or []
    if not workloads:
        return Kit.empty("No pods are running in namespace oksa.")
    rows = []
    for item in workloads:
        if not isinstance(item, dict):
            continue
        name = esc(item.get("name") or "unknown")
        phase = esc(item.get("phase") or "Unknown")
        ready = item.get("ready")
        ready_text = "Ready" if ready is True else ("Not ready" if ready is False else "Unknown")
        restarts = str(item.get("restarts")) if isinstance(item.get("restarts"), int) else "Unavailable"
        rows.append(
            f'<tr><td class="mono">{name}</td><td>{phase}</td><td>{esc(ready_text)}</td>'
            f'<td class="numeric">{esc(restarts)}</td></tr>'
        )
    return (
        '<div class="table-scroll" tabindex="0" role="region" aria-label="Workload pods">'
        '<table><thead><tr><th scope="col">Pod name</th><th scope="col">Phase</th>'
        '<th scope="col">Ready</th><th scope="col" class="numeric">Restarts</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def model_serving_state(model: dict, model_status: str) -> tuple[str, Tone]:
    """How the shared runtime currently relates to one installed model."""
    active = model.get("active") is True
    selected = model.get("selected") is True
    if active and selected:
        return {
            "ready": ("Serving", Tone.OK),
            "not-ready": ("Starting", Tone.BUSY),
        }.get(model_status, ("Not ready", Tone.ERROR))
    if active:
        return "Active · not selected", Tone.WARNING
    if selected:
        return "Selected · not active", Tone.WARNING
    if model.get("capabilities") != "ok":
        return "Exceeds host capabilities", Tone.NEUTRAL
    return "Installed", Tone.NEUTRAL


def model_calibration_state(model: dict) -> tuple[str, Tone]:
    """Whether serving a model can use recorded calibration limits or must measure first."""
    return {
        "measured": ("Calibrated", Tone.OK),
        "zero-limits": ("Zero usable limits", Tone.ERROR),
        "required": ("Needs measurement", Tone.WARNING),
    }.get(str(model.get("calibration")), ("Unknown", Tone.NEUTRAL))


def model_unservable_reason(model: dict) -> str:
    """Why serving a model cannot succeed; empty when it can."""
    if model.get("capabilities") != "ok":
        return "This model exceeds detected host capabilities and cannot be started."
    if model.get("calibration") == "zero-limits":
        return "Calibration found no usable limits on this hardware; measure again before serving."
    return ""


def model_action(model: dict) -> tuple[str, str] | None:
    """The serving action that fits a model's state, as (method, label)."""
    if model.get("active") is True and model.get("selected") is True:
        return "StopServing", "Stop…"
    if model_unservable_reason(model):
        return None
    if model.get("selected") is True:
        return "RestartModel", "Restart…"
    return "StartModel", "Serve…"


def render_models_table(
    models: list[dict],
    *,
    loaded: bool,
    error_text: str,
    filtered: bool,
    model_status: str,
    block_reason: str,
) -> str:
    if not loaded and not error_text:
        return Kit.empty("Loading local model inventory…", icon="hourglass_empty")
    if not models:
        if error_text:
            return Kit.empty(f"Inventory could not be read: {error_text}", icon=Tone.ERROR.icon)
        msg = (
            "No installed models match the current filter."
            if filtered
            else "No models are installed on this appliance. Choose Download model to acquire one."
        )
        return Kit.empty(msg)
    disabled = ' disabled aria-describedby="models-block-reason"' if block_reason else ""
    rows = []
    for index, m in enumerate(models):
        mid = str(m.get("id") or "")
        pub_name = str(m.get("name") or mid)
        repo_id = str(m.get("repo_id") or "-")
        rev = str(m.get("revision") or "")
        prov = str(m.get("provenance") or "unknown")
        size, _, exact = fmt_mib(m.get("storage_mib")).partition(" (")
        size_html = esc(size) + (f'<span class="sub">{esc(exact.rstrip(")"))}</span>' if exact else "")

        if prov == "verified" and rev:
            prov_html = f'<code>{esc(rev[:12])}</code> · Verified manifest'
        elif prov == "catalog" and rev:
            prov_html = f'<code>{esc(rev[:12])}</code> · Catalog metadata'
        else:
            prov_html = "Unknown revision · Unverified provenance"

        action = model_action(m)
        if action is None:
            action_html = ""
        else:
            method, label = action
            danger = " danger" if method == "StopServing" else ""
            action_html = (
                f'<button type="button" class="row-action{danger}" id="model-action-{index}" '
                f'data-model="{esc(mid)}" aria-label="{esc(label.rstrip("…"))} {esc(mid)}"{disabled}>{esc(label)}</button>'
            )
        rows.append(
            f"<tr>"
            f'<td><a href="/models/{url_part(mid)}">{esc(mid)}</a><span class="sub">Serves as <code>{esc(pub_name)}</code></span></td>'
            f'<td>{esc(repo_id)}<span class="sub">{prov_html}</span></td>'
            f"<td>{Kit.status(*model_serving_state(m, model_status))}</td>"
            f"<td>{Kit.status(*model_calibration_state(m))}</td>"
            f'<td class="numeric">{size_html}</td>'
            f'<td class="numeric">{action_html}</td>'
            f"</tr>"
        )
    return (
        '<div class="table-scroll" tabindex="0" role="region" aria-label="Installed local models">'
        '<table><thead><tr>'
        '<th scope="col">Model / public name</th>'
        '<th scope="col">Source / provenance</th>'
        '<th scope="col">Serving</th>'
        '<th scope="col">Calibration</th>'
        '<th scope="col" class="numeric">Local size</th>'
        '<th scope="col" class="numeric"><span class="visually-hidden">Action</span></th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def render_search_results_table(results: list[dict], *, searched: bool, error_text: str) -> str:
    if error_text:
        return Kit.empty(f"Search failed: {error_text}", icon=Tone.ERROR.icon)
    if not searched:
        return Kit.empty("Enter a search term above or specify a direct repository ID to review.")
    if not results:
        return Kit.empty("No matching text-generation repositories found on Hugging Face.")
    rows = []
    for item in results:
        repo = str(item.get("repo_id") or "")
        author = str(item.get("author") or "—")
        downloads = item.get("downloads")
        dl_text = f"{downloads:,}" if isinstance(downloads, int) else "—"
        gated = item.get("gated") is True
        if gated:
            access_html = Kit.status("Gated · unsupported in UI", Tone.WARNING)
            action_html = '<span class="muted">Requires host HF token</span>'
        else:
            access_html = Kit.status("Public", Tone.NEUTRAL)
            href = f"/models/download/review?repo={url_part(repo)}&revision=main"
            action_html = f'<a href="{esc(href)}">Review source →</a>'
        rows.append(
            f"<tr>"
            f"<td><code>{esc(repo)}</code></td>"
            f"<td>{esc(author)}</td>"
            f'<td class="numeric">{esc(dl_text)}</td>'
            f"<td>{access_html}</td>"
            f"<td>{action_html}</td>"
            f"</tr>"
        )
    return (
        '<div class="table-scroll" tabindex="0" role="region" aria-label="Hugging Face search results">'
        '<table><thead><tr>'
        '<th scope="col">Repository ID</th><th scope="col">Publisher</th>'
        '<th scope="col" class="numeric">Downloads</th><th scope="col">Access</th><th scope="col">Review</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def render_key_lookup_table(
    keys: list[dict],
    query: str,
    *,
    verified: bool,
    sampled_at: float | None,
    error_text: str,
    now: float,
) -> str:
    raw_q = query.strip()
    if raw_q.startswith("sk-") and "..." not in raw_q and len(raw_q) >= 20:
        return (
            '<div class="notice error" role="alert">'
            "<strong>Do not enter a complete secret credential.</strong> "
            "Clear the input and search by key alias, displayed prefix (such as <code>sk-...abcd</code>), key ID, or owner user ID."
            "</div>"
        )
    if not verified:
        detail = error_text or "Cross-owner key index has not been loaded."
        return Kit.empty(
            f"Key index is incomplete or unavailable ({detail}). Absence of a match is not proof that a key does not exist.",
            icon=Tone.WARNING.icon,
        )
    q_fold = raw_q.casefold()
    matched = []
    for k in keys:
        if not isinstance(k, dict):
            continue
        haystack = " ".join(
            str(k.get(field) or "") for field in ("key_alias", "key_id", "key_prefix", "user_id")
        ).casefold()
        if not q_fold or q_fold in haystack:
            matched.append(k)
    if not matched:
        msg = (
            f"No keys match '{raw_q}' in the index read {fmt_ago(sampled_at, now)}. "
            "A key created since then appears on its owner's page."
            if raw_q
            else f"No user API keys existed when the index was read {fmt_ago(sampled_at, now)}."
        )
        return Kit.empty(msg)
    rows = []
    for k in matched:
        alias = str(k.get("key_alias") or "Unnamed key")
        kid = str(k.get("key_id") or "—")
        prefix = str(k.get("key_prefix") or "—")
        owner = str(k.get("user_id") or "")
        models = k.get("models") if isinstance(k.get("models"), list) else []
        scope = ", ".join(str(m) for m in models) if models else "All models (including future)"
        expires = str(k.get("expires") or "No expiration")
        owner_href = f"/access/user?id={url_part(owner)}" if owner else "/access"
        rows.append(
            f"<tr>"
            f"<td><strong>{esc(alias)}</strong></td>"
            f'<td class="mono">{esc(kid)}</td>'
            f'<td class="mono">{esc(prefix)}</td>'
            f'<td><a href="{esc(owner_href)}" class="mono">{esc(owner or "Unknown")}</a></td>'
            f"<td>{esc(scope)}</td>"
            f"<td>{esc(expires)}</td>"
            f'<td><a href="{esc(owner_href)}">Open owner keys →</a></td>'
            f"</tr>"
        )
    return (
        '<div class="table-scroll" tabindex="0" role="region" aria-label="Cross-owner key metadata lookup">'
        '<table><thead><tr>'
        '<th scope="col">Key alias</th><th scope="col">Key ID</th><th scope="col">Displayed prefix</th>'
        '<th scope="col">Owner user ID</th><th scope="col">Model scope</th><th scope="col">Expires</th>'
        '<th scope="col">Owner record</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def render_users_table(
    users: list[dict],
    keys: list[dict],
    protected_users: set[str],
    *,
    users_verified: bool,
    keys_verified: bool,
    error_text: str,
    filtered: bool,
) -> str:
    if not users_verified and not users:
        msg = f"API users could not be read: {error_text}" if error_text else "Loading API users…"
        return Kit.empty(msg)
    if not users:
        msg = "No API users match the current filter." if filtered else "No API users exist in the gateway database."
        return Kit.empty(msg)
    counts: dict[str, int] = {}
    if keys_verified:
        for k in keys:
            if isinstance(k, dict):
                uid = str(k.get("user_id") or "")
                counts[uid] = counts.get(uid, 0) + 1
    rows = []
    for u in users:
        uid = str(u.get("user_id") or "")
        email = str(u.get("user_email") or "—")
        if keys_verified:
            key_text = str(counts.get(uid, 0))
        else:
            key_text = "Unknown (key index unverified)"
        prot_note = '<span class="sub">Owns protected Open WebUI service key</span>' if uid in protected_users else ""
        href = f"/access/user?id={url_part(uid)}"
        rows.append(
            f"<tr>"
            f'<td><a href="{esc(href)}" class="mono">{esc(uid)}</a>{prot_note}</td>'
            f"<td>{esc(email)}</td>"
            f'<td class="numeric">{esc(key_text)}</td>'
            f"</tr>"
        )
    return (
        '<div class="table-scroll" tabindex="0" role="region" aria-label="API users">'
        '<table><thead><tr>'
        '<th scope="col">User ID</th><th scope="col">Email</th><th scope="col" class="numeric">Enabled keys</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def render_user_keys_table(keys: list[dict], *, verified: bool, blocked: bool) -> str:
    if not verified:
        return Kit.empty("Loading user keys…")
    if not keys:
        return Kit.empty("No editable API keys exist for this user.")
    disabled = " disabled" if blocked else ""
    rows = []
    for k in keys:
        kid = str(k.get("key_id") or "")
        alias = str(k.get("key_alias") or "Unnamed key")
        models = k.get("models") if isinstance(k.get("models"), list) else []
        scope = ", ".join(str(m) for m in models) if models else "All models (including future)"
        rows.append(
            f"<tr>"
            f"<td><strong>{esc(alias)}</strong></td>"
            f'<td class="mono">{esc(kid)}</td>'
            f'<td class="mono">{esc(k.get("key_prefix") or "—")}</td>'
            f"<td>{esc(scope)}</td>"
            f"<td>{esc(k.get('expires') or 'No expiration')}</td>"
            f"<td>{esc(k.get('created_at') or '—')}</td>"
            f'<td><button type="button" class="row-action danger" id="btn-revoke-{esc(kid)}" '
            f'data-key="{esc(kid)}" aria-label="Revoke {esc(alias)}"{disabled}>Revoke…</button></td>'
            f"</tr>"
        )
    return (
        '<div class="table-scroll" tabindex="0" role="region" aria-label="User API keys">'
        "<table><thead><tr>"
        '<th scope="col">Key alias</th><th scope="col">Key ID</th>'
        '<th scope="col">Displayed prefix</th><th scope="col">Model scope</th>'
        '<th scope="col">Expires</th><th scope="col">Created</th><th scope="col">Action</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def render_operations_table(jobs: list[dict], *, loaded: bool, error_text: str, filtered: bool, now: float) -> str:
    if not loaded and not jobs:
        msg = f"Operations could not be read: {error_text}" if error_text else "Loading operations…"
        return Kit.empty(msg)
    if not jobs:
        msg = "No operations match the filter." if filtered else "No operations have been recorded yet."
        return Kit.empty(msg)
    rows = []
    for job in jobs:
        jid = job_short_id(job.get("path"))
        op = str(job.get("operation") or "")
        state = str(job.get("state") or "unknown")
        res = job.get("result") if isinstance(job.get("result"), dict) else {}
        err = job.get("error") if isinstance(job.get("error"), dict) else {}
        model_name = str(job.get("model_name") or "")
        user_id = str(job.get("user_id") or "")

        if model_name:
            target_html = f'<a href="/models/{url_part(model_name)}" class="mono">{esc(model_name)}</a>'
        elif user_id:
            target_html = f'<a href="/access/user?id={url_part(user_id)}" class="mono">{esc(user_id)}</a>'
        elif op == "StopServing":
            target_html = '<a href="/serving">Shared runtime</a>'
        else:
            target_html = '<span class="muted">—</span>'

        partial = (
            (op == "RemoveModel" and res.get("deleted") is True and res.get("reconciled") is False)
            or (op == "DeleteUser" and res.get("outcome") in ("key_revocation_unconfirmed", "user_deletion_unconfirmed"))
        )
        if state == "running":
            badge = Kit.status("Running", Tone.BUSY)
        elif state == "succeeded":
            badge = Kit.status("Succeeded")
        elif partial:
            badge = Kit.status("Failed · partial effect", Tone.ERROR)
        elif state == "canceled":
            badge = Kit.status("Canceled", Tone.NEUTRAL)
        else:
            badge = Kit.status("Failed", Tone.ERROR)

        if state == "running":
            latest_html = render_job_progress(job.get("progress"), now, compact=True)
        else:
            latest = (
                str(err.get("message") or "")
                or str(res.get("warning") or "")
                or str(res.get("message") or "")
                or "No status message reported"
            )
            latest_html = esc(latest if len(latest) <= 96 else latest[:93] + "…")
        rows.append(
            f"<tr>"
            f'<td><a href="/operations/{url_part(jid)}" class="mono">{esc(jid)}</a></td>'
            f"<td>{esc(operation_label(op))}</td>"
            f"<td>{target_html}</td>"
            f"<td>{badge}</td>"
            f"<td>{fmt_time(job.get('started_at'), now)}</td>"
            f"<td>{latest_html}</td>"
            f"</tr>"
        )
    return (
        '<div class="table-scroll" tabindex="0" role="region" aria-label="Operations">'
        '<table><thead><tr>'
        '<th scope="col">Operation ID</th><th scope="col">Kind</th><th scope="col">Target</th>'
        '<th scope="col">State</th><th scope="col">Started</th><th scope="col">Status</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def render_download_review_facts(
    inspection: dict | None,
    effective_name: str,
    existing_model: dict | None,
) -> str:
    if not isinstance(inspection, dict) or not isinstance(inspection.get("metadata"), dict):
        return Kit.empty("No remote source metadata loaded yet.")
    meta = inspection["metadata"]
    fits = inspection.get("fits") is True
    reason = str(inspection.get("reason") or "")
    repo_id = str(meta.get("repo_id") or "")
    req_rev = str(meta.get("revision") or "main")
    resolved_rev = str(meta.get("resolved_revision") or "")
    storage_mib = meta.get("storage_mib")
    est_mem_mib = meta.get("estimated_memory_mib")
    max_ctx = meta.get("max_context")
    quant = str(meta.get("quantization") or "none")
    dtype = str(meta.get("torch_dtype") or "unknown")
    archs = ", ".join(str(a) for a in (meta.get("architectures") or [])) or "Unknown"
    gated = meta.get("gated") is True
    files = meta.get("files") if isinstance(meta.get("files"), list) else []

    fit_badge = Kit.status("Fits total host RAM estimate") if fits else Kit.status("Exceeds total host RAM estimate", Tone.WARNING)
    fit_html = f"{fit_badge} · {esc(fmt_mib(est_mem_mib))} ({esc(reason)})" + disclosure(
        "What this checks",
        "<p>Estimated model memory is compared with total host RAM. Free RAM, GPU memory and disk space are not checked.</p>",
    )
    if existing_model is None:
        collision_html = f'{Kit.status("New local installation", Tone.NEUTRAL)} · No existing model at <code>{esc(effective_name)}</code>'
    else:
        ex_repo = str(existing_model.get("repo_id") or "-")
        ex_rev = str(existing_model.get("revision") or "")
        ex_prov = str(existing_model.get("provenance") or "unknown")
        ex_active = existing_model.get("active") is True
        ex_selected = existing_model.get("selected") is True
        serving_note = (
            "currently active/desired"
            if (ex_active or ex_selected)
            else "not currently serving"
        )
        if ex_prov == "unknown":
            collision_html = (
                f'{Kit.status("Replacement blocked · unknown provenance", Tone.ERROR)} · '
                f"Existing installation at <code>{esc(effective_name)}</code> ({esc(serving_note)}) has unverified provenance. "
                "Check the files on the host or enter a different local storage ID."
            )
        else:
            collision_html = (
                f'{Kit.status("Replacement required", Tone.WARNING)} · '
                f"Existing installation at <code>{esc(effective_name)}</code>: source <code>{esc(ex_repo)}</code>, "
                f"revision <code>{esc(ex_rev[:12] if ex_rev else 'unknown')}</code> ({esc(ex_prov)}, {esc(serving_note)})."
            )

    rows = [
        ("Source repository", f"<code>{esc(repo_id)}</code>"),
        ("Requested revision", f"<code>{esc(req_rev)}</code>"),
        ("Resolved immutable commit", f"<code>{esc(resolved_rev)}</code> <span class=\"muted\">· pinned at submission</span>"),
        ("Local storage ID", f"<code>{esc(effective_name)}</code>"),
        ("Destination path", f"<code>/var/lib/vllm/models/{esc(effective_name)}</code>"),
        ("Existing installation check", collision_html),
        ("Estimated download size", f"{esc(fmt_mib(storage_mib))} across {len(files)} verified files (config.json + weights)"),
        ("Host RAM capacity check", fit_html),
        ("Architecture / dtype", f"{esc(archs)} · quantization <code>{esc(quant)}</code> · dtype <code>{esc(dtype)}</code> · max context {esc(max_ctx)}"),
        ("Access policy", Kit.status("Gated · unsupported without host token", Tone.ERROR) if gated else Kit.status("Public repository", Tone.NEUTRAL)),
        (
            "After download",
            "The model appears in Models. Serving changes only if the model is already selected or the autostart policy picks it.",
        ),
    ]
    return render_facts(rows)


def render_model_detail_facts(model: dict, model_status: str) -> str:
    mid = str(model.get("id") or "")
    pub = str(model.get("name") or mid)
    repo = str(model.get("repo_id") or "-")
    rev = str(model.get("revision") or "")
    prov = str(model.get("provenance") or "unknown")
    path = str(model.get("path") or f"/var/lib/vllm/models/{mid}")
    size = fmt_mib(model.get("storage_mib"))
    quant = str(model.get("quantization") or "none")
    cap = str(model.get("capabilities") or "insufficient")
    state_html = f'{Kit.status(*model_serving_state(model, model_status))} · <a href="/serving">Open Serving →</a>'

    prov_badge = (
        Kit.status("Verified manifest")
        if prov == "verified"
        else (Kit.status("Catalog metadata", Tone.NEUTRAL) if prov == "catalog" else Kit.status("Unknown provenance", Tone.WARNING))
    )
    rev_html = f"<code>{esc(rev)}</code> · {prov_badge}" if rev else f"Unknown revision · {prov_badge}"
    cap_html = Kit.status("Capable") if cap == "ok" else Kit.status("Insufficient host capabilities", Tone.ERROR)

    return render_facts([
        ("Local storage ID", f"<code>{esc(mid)}</code>"),
        ("Public inference name", f"<code>{esc(pub)}</code>"),
        ("Source repository", f"<code>{esc(repo)}</code>"),
        ("Installed revision", rev_html),
        ("Local path", f"<code>{esc(path)}</code>"),
        ("Aggregate local size", esc(size)),
        ("Quantization", f"<code>{esc(quant)}</code>"),
        ("Host compatibility", cap_html),
        ("Calibration", f'{Kit.status(*model_calibration_state(model))} · <a href="#calibration">Profile ↓</a>'),
        ("Serving", state_html),
    ])


def render_calibration_summary(cal: dict | None, *, error_text: str, running_job: dict | None, now: float) -> str:
    if error_text:
        return (
            f'<div class="notice error"><strong>Calibration record could not be read.</strong> '
            f"{esc(error_text)}</div>"
        )
    if not isinstance(cal, dict):
        return Kit.empty("Loading calibration record…", icon="hourglass_empty")
    result = cal.get("result") if isinstance(cal.get("result"), dict) else None
    status = cal.get("status") if isinstance(cal.get("status"), dict) else None

    notes = ""
    if status is not None:
        st_state = str(status.get("state") or "unknown")
        st_msg = str(status.get("message") or "No message")
        tone = Tone.BUSY if st_state == "running" else (Tone.ERROR if st_state == "error" else Tone.NEUTRAL)
        notes = f'<p class="small">This boot: {Kit.status(st_state, tone)} · {esc(st_msg)} ({fmt_time(status.get("updated_at"), now)})</p>'
        if st_state == "running" and running_job is not None:
            notes = render_job_progress(running_job.get("progress"), now)
    if result is None:
        return (
            f'<div class="profile"><div class="profile-head">{Kit.status("No recorded profile", Tone.NEUTRAL)}</div>{notes}'
            '<p class="small muted">Calibration runs automatically before serving, or when requested below.</p></div>'
        )

    limits = result.get("limits") if isinstance(result.get("limits"), dict) else {}
    max_seqs = limits.get("max_num_seqs", 0)
    max_len = limits.get("max_model_len", 0)
    max_batched = limits.get("max_num_batched_tokens", 0)
    gpu_util = limits.get("gpu_memory_utilization", 0.0)
    mem_bytes = limits.get("memory_bytes", 0)
    search_complete = result.get("search_complete") is True
    inc_reasons = result.get("incomplete_reasons") if isinstance(result.get("incomplete_reasons"), list) else []
    reason = str(result.get("reason") or "unknown")
    fp = str(result.get("fingerprint") or "")

    if isinstance(max_seqs, int) and max_seqs > 0 and search_complete:
        outcome = Kit.status("Usable measured profile · complete search")
    elif isinstance(max_seqs, int) and max_seqs > 0:
        inc_str = ", ".join(str(r) for r in inc_reasons) or "incomplete search"
        outcome = Kit.status(f"Usable limits · incomplete search ({inc_str})", Tone.WARNING)
    else:
        outcome = Kit.status(f"Zero usable limits ({reason})", Tone.ERROR)

    def count(value: object) -> str:
        return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else "Unavailable"

    mem_mib = mem_bytes / (1024 * 1024) if isinstance(mem_bytes, int) and mem_bytes > 0 else 0
    stats = (
        ("Context", count(max_len), "tokens · max_model_len"),
        ("Concurrency", count(max_seqs), "sequences · max_num_seqs"),
        ("Batched tokens", count(max_batched), "max_num_batched_tokens"),
        ("GPU memory", f"{gpu_util:.0%}" if isinstance(gpu_util, (int, float)) else "Unavailable", "utilization target"),
        ("Host memory", fmt_mib(mem_mib), "budget"),
    )
    stats_html = "".join(
        f'<div><dt>{esc(label)}</dt><dd class="profile-value">{esc(value)}</dd><dd class="profile-unit">{esc(unit)}</dd></div>'
        for label, value, unit in stats
    )
    fp_html = f"{esc(fp[:24])}…" if len(fp) > 24 else esc(fp)
    return (
        f'<div class="profile"><div class="profile-head">{outcome}'
        f'<span class="small muted">Recorded {fmt_time(result.get("completed_at"), now)}</span></div>{notes}'
        f'<dl class="profile-stats">{stats_html}</dl>'
        f'<p class="small muted">Recorded limits, not verified as applied to the live runtime · '
        f"fingerprint <code>{fp_html}</code></p></div>"
    )


def render_operation_detail(job: dict, now: float) -> tuple[str, str, str]:
    jid = job_short_id(job.get("path"))
    op = str(job.get("operation") or "")
    state = str(job.get("state") or "unknown")
    res = job.get("result") if isinstance(job.get("result"), dict) else {}
    err = job.get("error") if isinstance(job.get("error"), dict) else {}
    model_name = str(job.get("model_name") or "")
    repo_id = str(job.get("repo_id") or "")
    commit = str(job.get("commit") or "")
    user_id = str(job.get("user_id") or "")
    req_id = str(job.get("request_id") or "")

    partial = (
        (op == "RemoveModel" and res.get("deleted") is True and res.get("reconciled") is False)
        or (op == "DeleteUser" and res.get("outcome") in ("key_revocation_unconfirmed", "user_deletion_unconfirmed"))
    )
    if state == "running":
        badge = Kit.status("Running", Tone.BUSY)
    elif state == "succeeded":
        badge = Kit.status("Succeeded")
    elif partial:
        badge = Kit.status("Failed · partial effect", Tone.ERROR)
    elif state == "canceled":
        badge = Kit.status("Canceled", Tone.NEUTRAL)
    else:
        badge = Kit.status("Failed", Tone.ERROR)

    if model_name:
        target_html = f'<a href="/models/{url_part(model_name)}" class="mono">{esc(model_name)}</a>'
    elif user_id:
        target_html = f'<a href="/access/user?id={url_part(user_id)}" class="mono">{esc(user_id)}</a>'
    elif op == "StopServing":
        target_html = '<a href="/serving">Shared runtime</a>'
    else:
        target_html = "—"

    fact_rows = [
        ("Operation ID", f"<code>{esc(jid)}</code>"),
        ("Request ID", f"<code>{esc(req_id)}</code>" if req_id else '<span class="muted">Not recorded</span>'),
        ("Kind", esc(operation_label(op))),
        ("State", badge),
        ("Target", target_html),
        ("Repository / source", f"<code>{esc(repo_id)}</code>" if repo_id else '<span class="muted">Not applicable</span>'),
        ("Pinned commit / revision", f"<code>{esc(commit)}</code>" if commit else '<span class="muted">Not recorded</span>'),
        ("Started", fmt_time(job.get("started_at"), now)),
        ("Ended", fmt_time(job.get("ended_at"), now) if job.get("ended_at") else "Still running"),
    ]
    facts_html = render_facts(fact_rows)

    lines = []
    if res.get("message"):
        lines.append(f"Result: {res['message']}")
    if res.get("warning"):
        lines.append(f"Warning: {res['warning']}")
    if err.get("message"):
        lines.append(f"Error ({err.get('code', 'OperationFailed')}): {err['message']}")
    if state == "running":
        status_html = render_job_progress(job.get("progress"), now)
    else:
        status_text = "\n".join(lines) if lines else "No status text reported."
        status_html = f'<div class="progress-text"><div class="mono">{esc(status_text)}</div></div>'

    if state == "running":
        recovery_html = (
            '<div class="notice warning">'
            "<p><strong>Running in the background.</strong> Leaving or reloading this page does not cancel it.</p>"
            + disclosure(
                "About canceling",
                "<p>Cancel asks the operation to stop (SIGTERM) and can race its completion. Published files, "
                "calibration records and runtime changes are not rolled back.</p>",
            )
            + "</div>"
        )
    elif op == "RemoveModel" and partial:
        recovery_html = (
            '<div class="notice error">'
            "<p><strong>Partly done · files deleted, cleanup failed.</strong></p>"
            f"<p>The files of <code>{esc(model_name)}</code> were deleted. Updating the model catalog or workloads failed: "
            f"{esc(res.get('warning') or err.get('message') or 'no reason reported')}</p>"
            "<p>Saved calibration records are kept.</p>"
            '<p><strong>Next:</strong> do not remove the model again. Check <a href="/serving">Serving</a> and '
            '<a href="/models">Models</a>, and finish the cleanup on the host before changing models.</p>'
            "</div>"
        )
    elif op == "DeleteUser" and partial:
        revoked_cnt = res.get("confirmed_revoked_keys", 0)
        recovery_html = (
            '<div class="notice error">'
            f"<p><strong>Partly done · {esc(str(res.get('outcome')).replace('_', ' '))}.</strong></p>"
            f"<p>Keys confirmed revoked: <code>{esc(revoked_cnt)}</code>. {esc(res.get('warning') or err.get('message') or '')}</p>"
            f"<p><strong>Next:</strong> open <a href=\"/access/user?id={url_part(user_id)}\">API user {esc(user_id)}</a> to check "
            "the remaining keys, and finish the cleanup before deleting again.</p>"
            "</div>"
        )
    elif op == "DownloadModel":
        if state == "succeeded":
            recovery_html = (
                '<div class="notice">'
                "<p><strong>Download published.</strong> The model is listed in Models; downloading does not start serving it.</p>"
                f'<p><a href="/models/{url_part(model_name)}">Open model {esc(model_name)} →</a> '
                f'<a href="/serving">Open Serving →</a></p>'
                "</div>"
            )
        else:
            recovery_html = (
                '<div class="notice warning">'
                f"<p><strong>Download did not complete ({esc(state)}).</strong> Files may still have changed; check Models to confirm.</p>"
                f'<p><a href="/models">Open Models →</a></p>'
                "</div>"
            )
    elif op in ("StartModel", "RestartModel", "StopServing"):
        recovery_html = (
            f'<div class="notice {"" if state == "succeeded" else "warning"}">'
            f"<p><strong>{esc(operation_label(op))} {esc(state)}.</strong> Serving shows what is running now. "
            "After a failed or canceled change, the earlier model may not be running.</p>"
            f'<p><a href="/serving">Open Serving →</a></p>'
            "</div>"
        )
    elif op == "Calibrate":
        recovery_html = (
            f'<div class="notice {"" if state == "succeeded" else "warning"}">'
            f"<p><strong>Calibration {esc(state)}.</strong> The model's calibration profile shows whether usable limits were recorded.</p>"
            f'<p><a href="/models/{url_part(model_name)}#calibration">Open {esc(model_name)} calibration →</a> '
            f'<a href="/serving">Open Serving →</a></p>'
            "</div>"
        )
    else:
        recovery_html = (
            f'<div class="notice {"" if state == "succeeded" else "warning"}">'
            f"<p><strong>{esc(operation_label(op))} {esc(state)}.</strong> Refresh the affected pages to confirm the current state.</p>"
            "</div>"
        )
    return facts_html, status_html, recovery_html
