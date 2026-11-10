# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

from datetime import datetime, timezone
import html
import math
from urllib.parse import quote


OKSA_CSS = """
* { box-sizing: border-box; }
:root {
  color-scheme: light;
  --ink: #20262d;
  --muted: #52606d;
  --line: #d4d9df;
  --paper: #fff;
  --green: #174ea6;
  --pale: #eaf1fc;
  --amber: #785000;
  --red: #a32121;
}
html, body, #app, .q-layout, .q-page-container, .q-page, .nicegui-content {
  margin: 0 !important;
  padding: 0 !important;
  height: 100dvh;
  width: 100%;
  overflow: hidden;
  background: var(--paper);
  color: var(--ink);
  font: 14px/1.5 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.nicegui-content {
  display: flex !important;
  flex-direction: column !important;
  gap: 0 !important;
}
.nicegui-sub-pages {
  display: block;
  width: 100%;
  min-width: 0;
}
.skip-link {
  position: absolute;
  left: 16px;
  top: -100px;
  z-index: 100;
  padding: 10px 16px;
  background: #fff;
  color: var(--ink);
  border: 2px solid var(--green);
  font-weight: 600;
}
.skip-link:focus { top: 8px; }
main:focus-visible { outline: 3px solid #8a4700; outline-offset: -3px; }
a { color: var(--green); text-decoration: underline; text-underline-offset: 3px; }
a:hover { color: #103b7c; }
button, input, select, textarea { font: inherit; }
button, .button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 38px;
  padding: 8px 14px;
  border: 1px solid #8c9aa6;
  border-radius: 3px;
  background: #fff;
  color: var(--ink);
  text-decoration: none;
  font-weight: 600;
  cursor: pointer;
  line-height: 1.35;
}
button:hover, .button:hover { background: #eef2f6; color: var(--ink); border-color: #5d6d7e; }
.primary { background: var(--green); border-color: var(--green); color: #fff; }
.primary:hover { background: #103b7c; border-color: #103b7c; color: #fff; }
.danger { color: var(--red); border-color: #c98a82; background: #fff; }
.danger:hover { background: #f9ece9; color: var(--red); border-color: var(--red); }
button:disabled, input:disabled, select:disabled { opacity: 0.55; cursor: not-allowed; }
button:disabled:hover { background: #fff; }
.primary:disabled:hover { background: var(--green); }
:focus-visible { outline: 3px solid #8a4700; outline-offset: 3px; }
.nav-link:focus-visible { outline-offset: -3px; }
.topbar {
  flex: none;
  height: 48px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 0 24px;
  border-bottom: 1px solid #313b47;
  background: #202a35;
  color: #fff;
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 17px;
  font-weight: 700;
  color: #fff;
  text-decoration: none;
}
.brand:hover { color: #fff; }
.topbar :focus-visible { outline-color: #ffcf80; }
.host {
  display: flex;
  align-items: center;
  gap: 12px;
  color: #c6d2de;
  font-size: 12px;
}
.host strong {
  color: #f6faf5;
  font-weight: 600;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
}
.host-divider { width: 1px; height: 18px; background: #486062; }
.app {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 192px minmax(0, 1fr);
}
.sidebar {
  display: flex;
  flex-direction: column;
  padding: 16px 8px;
  border-right: 1px solid var(--line);
  background: #f5f6f8;
  min-width: 0;
  overflow-y: auto;
}
.nav-label {
  margin: 0 12px 12px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 600;
}
.sidebar nav { display: flex; flex-direction: column; gap: 4px; }
.nav-link {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 12px;
  color: #36454f;
  text-decoration: none;
  border-left: 2px solid transparent;
  font-weight: 550;
}
.nav-link:hover { background: #e8ecf2; color: var(--ink); }
.nav-link[aria-current="page"] {
  color: #103b7c;
  background: #e5edf9;
  border-left-color: var(--green);
  font-weight: 700;
}
.sidebar-foot {
  margin-top: auto;
  padding: 20px 12px 0;
  color: var(--muted);
  font-size: 12px;
  border-top: 1px solid var(--line);
}
.sidebar-foot p { margin: 0 0 6px; }
main {
  min-width: 0;
  min-height: 0;
  padding: 20px 24px 32px;
  overflow-y: auto;
}
.breadcrumbs {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  color: var(--muted);
  font-size: 12px;
  margin-bottom: 16px;
}
.breadcrumbs a { color: var(--muted); text-decoration: none; }
.breadcrumbs a:hover { text-decoration: underline; }
.breadcrumbs span[aria-current="page"] { color: var(--ink); font-weight: 600; }
.page-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}
h1 { font-size: 20px; font-weight: 650; line-height: 1.3; margin: 0 0 6px; overflow-wrap: anywhere; }
h2 { font-size: 16px; font-weight: 650; margin: 0; }
h3 { font-size: 14px; font-weight: 650; margin: 0 0 8px; }
p { margin: 0 0 12px; }
.subtitle { margin: 0; color: var(--muted); font-size: 13px; max-width: 76ch; }
.actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--green);
  background: var(--pale);
  border: 1px solid #b8d0f2;
  padding: 2px 7px;
  border-radius: 2px;
  font-size: 11px;
  font-weight: 650;
  white-space: nowrap;
}
.status::before { content: ''; width: 5px; height: 5px; background: currentColor; border-radius: 50%; }
.status.neutral { color: #435359; background: #eef1f1; border-color: #cfd6d8; }
.status.warning { color: var(--amber); background: #f9efdc; border-color: #e0c389; }
.status.error { color: var(--red); background: #f9eae7; border-color: #e2b4ac; }
.notice {
  padding: 14px 16px;
  background: #edf3ee;
  border-left: 3px solid #4e7a56;
  margin-bottom: 20px;
  font-size: 13px;
}
.notice p:last-child { margin-bottom: 0; }
.notice.warning { background: #fbf3e4; border-color: #b48a40; }
.notice.error { background: #fbede9; border-color: #b76252; }
.notice strong { font-weight: 650; }
.section { margin-top: 28px; }
.section-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 12px;
}
.section-head p, .section-head a { margin: 0; font-size: 12px; }
.muted, .help { color: var(--muted); }
.help { font-size: 12px; margin-top: 7px; }
.small { font-size: 12px; }
.metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
  margin: 16px 0 8px;
}
.metric { padding: 10px 14px 10px 0; }
.metric + .metric { border-left: 1px solid var(--line); padding-left: 14px; }
.metric-label { color: var(--muted); font-size: 12px; font-weight: 500; }
.metric-value {
  display: block;
  font-size: 16px;
  line-height: 1.5;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  margin: 3px 0;
}
.metric-value small { color: var(--muted); font-size: 13px; font-weight: 450; }
.metric-note { font-size: 12px; color: var(--muted); }
.table-scroll {
  overflow-x: auto;
  max-width: 100%;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
}
table { width: 100%; border-collapse: collapse; text-align: left; white-space: nowrap; font-size: 13px; }
th { color: #3b4652; font-size: 12px; font-weight: 650; background: #f1f3f6; }
th, td { padding: 9px 12px; border-bottom: 1px solid #e0e4e9; vertical-align: middle; }
th:first-child, td:first-child { padding-left: 14px; }
tr:last-child td { border-bottom: 0; }
td strong { font-weight: 650; }
td .sub { display: block; color: var(--muted); font-size: 11px; margin-top: 2px; }
td a { font-weight: 600; }
.numeric { text-align: right; font-variant-numeric: tabular-nums; }
.mono, code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 0.93em; }
code { overflow-wrap: anywhere; }
.toolbar {
  display: flex;
  gap: 16px;
  align-items: flex-end;
  flex-wrap: wrap;
  padding: 14px 0;
  border-top: 1px solid var(--line);
}
.toolbar-field { display: flex; flex-direction: column; gap: 4px; min-width: 180px; }
.toolbar-field label { margin: 0; font-size: 11px; font-weight: 600; }
.toolbar .count { margin-left: auto; margin-bottom: 8px; font-size: 12px; color: var(--muted); }
label, legend { display: block; font-weight: 600; font-size: 12px; margin-bottom: 6px; }
input:not([type=radio]):not([type=checkbox]), select, textarea {
  display: block;
  width: 100%;
  max-width: 100%;
  border: 1px solid #6c7a86;
  background: #fff;
  color: var(--ink);
  border-radius: 3px;
  padding: 8px 11px;
  min-height: 38px;
}
input[readonly] { background: #f1f4ef; color: #52635e; }
input[type=checkbox], input[type=radio] { accent-color: var(--green); width: 16px; height: 16px; }
.form-width { max-width: 760px; }
.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 14px;
  align-items: end;
}
.field { margin: 18px 0; }
.field .help { margin-bottom: 0; }
fieldset { border: 0; padding: 0; margin: 18px 0; min-width: 0; }
.option {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  border-top: 1px solid var(--line);
  padding: 12px 0;
  font-weight: 400;
  margin: 0;
  cursor: pointer;
}
.option:last-child { border-bottom: 1px solid var(--line); }
.option input { margin-top: 3px; }
.option strong { display: block; font-size: 13px; font-weight: 600; }
.option small { display: block; color: var(--muted); font-size: 12px; margin-top: 3px; }
.form-footer {
  border-top: 1px solid var(--line);
  margin-top: 24px;
  padding-top: 16px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
}
.facts {
  display: grid;
  grid-template-columns: 190px minmax(0, 1fr);
  margin: 0;
  border-top: 1px solid var(--line);
}
.facts dt, .facts dd {
  margin: 0;
  padding: 10px 0;
  border-bottom: 1px solid var(--line);
  font-size: 13px;
}
.facts dt { color: var(--muted); font-weight: 500; padding-right: 12px; }
.facts dd { overflow-wrap: anywhere; }
.fact-actions { margin-top: 16px; }
.progress-text {
  border: 1px solid var(--line);
  background: #f5f7f9;
  padding: 16px;
  margin: 14px 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.page-foot {
  color: var(--muted);
  font-size: 11px;
  border-top: 1px solid var(--line);
  padding-top: 14px;
  margin-top: 32px;
  display: flex;
  justify-content: space-between;
  gap: 15px;
  flex-wrap: wrap;
}
.empty { padding: 18px 14px; color: var(--muted); border-bottom: 1px solid var(--line); font-size: 13px; }
[hidden] { display: none !important; }
dialog.oksa-modal {
  border: 1px solid #8c9aa6;
  border-radius: 4px;
  padding: 24px;
  max-width: 540px;
  width: calc(100% - 32px);
  color: var(--ink);
  background: var(--paper);
  box-shadow: 0 18px 60px rgba(18, 47, 52, 0.25);
  max-height: calc(100dvh - 40px);
  overflow-y: auto;
}
dialog.oksa-modal::backdrop { background: rgba(23, 43, 48, 0.5); }
dialog.oksa-modal h2 { font-size: 18px; margin: 0 0 12px; line-height: 1.3; }
dialog.oksa-modal p { font-size: 13px; }
dialog.oksa-modal .dialog-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  flex-wrap: wrap;
  margin-top: 20px;
}
dialog.oksa-modal code.secret-box {
  display: block;
  background: #edf1f7;
  border: 1px solid #b8c4d0;
  padding: 12px;
  margin: 12px 0;
  font-size: 12px;
  user-select: all;
  overflow-wrap: anywhere;
}
@media (max-width: 1000px) {
  .app { grid-template-columns: 174px minmax(0, 1fr); }
  main { padding: 20px 24px 32px; }
  .page-heading { flex-wrap: wrap; gap: 14px; }
}
@media (max-width: 680px) {
  .topbar { height: 48px; padding: 0 16px; gap: 10px; }
  .brand { font-size: 17px; }
  .host { font-size: 12px; gap: 8px; }
  .host .host-extra, .host-divider { display: none; }
  .app { display: flex; flex-direction: column; }
  .sidebar { flex: none; padding: 0; border-right: 0; border-bottom: 1px solid var(--line); overflow: visible; }
  .nav-label, .sidebar-foot { display: none; }
  .sidebar nav { flex-direction: row; overflow-x: auto; gap: 0; }
  .nav-link {
    white-space: nowrap;
    flex: 1 0 auto;
    justify-content: center;
    padding: 10px 12px;
    font-size: 13px;
    border-left: 0;
    border-bottom: 2px solid transparent;
  }
  .nav-link[aria-current="page"] { border-left: 0; border-bottom-color: var(--green); }
  main { flex: 1; padding: 16px; }
  .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .metric, .metric + .metric { padding: 10px; border-left: 0; }
  .metric:nth-child(even) { border-left: 1px solid var(--line); }
  .metric:nth-child(n+3) { border-top: 1px solid var(--line); }
  .facts { grid-template-columns: 112px minmax(0, 1fr); gap: 0 12px; }
  .toolbar-field { flex: 1 1 150px; min-width: 0; }
  .toolbar .count { margin-left: 0; }
}
"""


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def url_part(value: str) -> str:
    return quote(str(value or ""), safe="")


def fmt_utc(timestamp: float | int | None) -> str:
    if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp <= 0:
        return "Unknown time"
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


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


def status_badge(text: str, tone: str = "neutral") -> str:
    tone_cls = f" {tone}" if tone in ("neutral", "warning", "error") else ""
    return f'<span class="status{tone_cls}">{esc(text)}</span>'


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


def render_unresolved_banner(unresolved: list[dict]) -> str:
    if not unresolved:
        return ""
    blocks = []
    for item in unresolved:
        req_id = esc(item.get("request_id") or "unknown")
        op = esc(operation_label(str(item.get("operation") or "")))
        scope = esc(item.get("scope") or "unknown")
        target = esc(item.get("target") or "unknown")
        err_code = esc(item.get("error_code") or "Unconfirmed")
        err_msg = esc(item.get("error_message") or item.get("summary") or "Outcome could not be verified.")
        updated = esc(fmt_utc(item.get("updated_at")))
        job_path = item.get("job_path")
        job_link = ""
        if isinstance(job_path, str) and job_path.strip():
            jid = job_short_id(job_path)
            job_link = f' · Job <a href="/operations/{url_part(jid)}" class="mono">{esc(jid)}</a>'
        blocks.append(
            f'<div class="notice error" role="alert">'
            f"<p><strong>Unresolved {scope} operation guard · {op} on <code>{target}</code></strong></p>"
            f"<p>Request ID: <code>{req_id}</code>{job_link} · Recorded: {updated} · Code: <code>{err_code}</code></p>"
            f"<p>Last reported detail: {err_msg}</p>"
            f"<p class=\"small\">Conflicting {scope} mutations remain blocked across tabs and reloads until state is verified. "
            f"After settling work and inspecting the affected resources on the appliance host, a root maintainer can clear this guard with: "
            f"<code>sudo /usr/libexec/puu/oksa-daemon --reconcile {req_id}</code></p>"
            f"</div>"
        )
    return "".join(blocks)


def render_serving_observations(
    status: dict | None,
    snapshot: dict,
    models: list[dict],
    *,
    disconnected: bool,
    status_failed: bool,
) -> str:
    if status is None:
        reason = snapshot.get("error") or ("Daemon connection unavailable." if disconnected else "Waiting for initial status observation.")
        return (
            f'<div class="notice {"error" if status_failed or disconnected else "warning"}">'
            f"<strong>Serving observations are currently unavailable.</strong> {esc(reason)} "
            f"Unknown state is not reported as stopped or healthy."
            f"</div>"
        )
    sampled_at = snapshot.get("sampled_at") or "Unknown sample time"
    stale = bool(snapshot.get("stale")) or status_failed or disconnected
    serving_enabled = status.get("serving_enabled") is True
    selected_name = str(status.get("selected_model") or "-")
    active_name = str(status.get("active_model") or "-")
    selected_id = str(status.get("selected_model_id") or "-")
    active_id = str(status.get("active_model_id") or "-")
    installed_ids = {m.get("id") for m in models if isinstance(m, dict)}

    if not serving_enabled:
        desired_html = f'{status_badge("Serving disabled", "neutral")} <span class="muted">· StopServing is active</span>'
    elif selected_id != "-" and selected_id in installed_ids:
        desired_html = f'<a href="/models/{url_part(selected_id)}">{esc(selected_id)}</a> <span class="muted">· public name <code>{esc(selected_name)}</code></span>'
    elif selected_id != "-":
        desired_html = f"<code>{esc(selected_id)}</code> <span class=\"muted\">· public name <code>{esc(selected_name)}</code></span>"
    else:
        desired_html = '<span class="muted">No explicit model pinned · planner uses autostart policy</span>'

    if active_id != "-" and active_id in installed_ids:
        observed_html = f'<a href="/models/{url_part(active_id)}">{esc(active_id)}</a> <span class="muted">· public inference name <code>{esc(active_name)}</code></span>'
    elif active_id != "-":
        observed_html = f"<code>{esc(active_id)}</code> <span class=\"muted\">· public inference name <code>{esc(active_name)}</code></span>"
    else:
        observed_html = "None observed active"

    model_status = str(status.get("model_status") or "unavailable")
    if stale:
        health_html = f'{status_badge(f"Stale ({model_status})", "warning")} <span class="muted">· last sample {esc(sampled_at)}</span>'
    elif model_status == "ready":
        health_html = f'{status_badge("Healthy (vLLM /health 200)", "")} <span class="muted">· observed {esc(sampled_at)}</span>'
    elif model_status == "not-ready":
        health_html = f'{status_badge("Starting / not ready", "warning")} <span class="muted">· observed {esc(sampled_at)}</span>'
    elif model_status == "stopped":
        health_html = f'{status_badge("Stopped", "neutral")} <span class="muted">· observed {esc(sampled_at)}</span>'
    else:
        health_html = f'{status_badge("Unavailable", "error")} <span class="muted">· observed {esc(sampled_at)}</span>'

    gw = str(status.get("gateway_status") or "unreachable")
    if gw == "ready":
        gw_badge = status_badge("Ready", "warning" if stale else "")
    elif gw == "authentication-required":
        gw_badge = status_badge("Reachable (auth required)", "warning" if stale else "")
    else:
        gw_badge = status_badge("Unreachable", "error")
    gw_html = f'{gw_badge} <span class="muted">· readiness probe only; does not certify end-to-end inference</span>'

    auth = str(status.get("auth_enforcement") or "unknown")
    if auth == "enforced":
        auth_html = f'{status_badge("Enforced", "")} <span class="muted">· anonymous /v1/models probe rejected</span>'
    elif auth == "keyless":
        auth_html = (
            f'{status_badge("Keyless access enabled", "warning")} '
            f'<span class="muted">· anonymous requests are accepted until litellm-master-key is configured</span>'
        )
    else:
        auth_html = (
            f'{status_badge("Enforcement not verified", "neutral")} '
            f'<span class="muted">· keyless access may be possible</span>'
        )

    api_url = str(status.get("api_base_url") or "http://api.puu.local/v1")
    prep = status.get("preparation")
    rows = [
        ("Desired model", desired_html),
        ("Observed model", observed_html),
        ("Model health", health_html),
        ("Gateway readiness", gw_html),
        ("Gateway authentication", auth_html),
        ("Applied configuration", "Unknown with the current API. Recorded calibration limits are not verified live runtime settings."),
        ("API base URL", f"<code>{esc(api_url)}</code>"),
    ]
    if isinstance(prep, dict) and prep.get("model"):
        p_model = str(prep.get("model") or "")
        p_state = str(prep.get("state") or "")
        p_msg = str(prep.get("message") or "")
        p_time = fmt_utc(prep.get("updated_at"))
        tone = "warning" if p_state == "running" else ("error" if p_state == "error" else "neutral")
        rows.append((
            "Observed calibration state",
            f'{status_badge(f"{p_state} ({p_model})", tone)} <span class="muted">· {esc(p_msg)} ({esc(p_time)})</span>',
        ))
    return render_facts(rows)


def render_serving_metrics(status: dict | None, snapshot: dict, *, stale: bool) -> str:
    mem = (status or {}).get("host_memory") if isinstance(status, dict) else {}
    metrics = (status or {}).get("metrics") if isinstance(status, dict) else {}
    mem = mem if isinstance(mem, dict) else {}
    metrics = metrics if isinstance(metrics, dict) else {}

    used_mib = mem.get("used_mib")
    total_mib = mem.get("total_mib")
    if isinstance(used_mib, int) and isinstance(total_mib, int) and total_mib > 0:
        mem_val = f'{used_mib / 1024:.1f} <small>/ {total_mib / 1024:.1f} GiB</small>'
        mem_note = f"{mem.get('used_pct', 0)}% host RAM used"
    else:
        mem_val = "Unavailable"
        mem_note = "Host memory reading unavailable"

    running_req = metrics.get("running_requests")
    waiting_req = metrics.get("waiting_requests")
    kv_pct = metrics.get("kv_cache_usage_pct")

    run_val = str(running_req) if isinstance(running_req, int) else "Unavailable"
    wait_val = str(waiting_req) if isinstance(waiting_req, int) else "Unavailable"
    kv_val = f"{kv_pct:g} <small>%</small>" if isinstance(kv_pct, (int, float)) and math.isfinite(kv_pct) else "Unavailable"
    sample_note = "Stale sample" if stale else "Current sample"

    return (
        f'<div class="metrics">'
        f'<div class="metric"><span class="metric-label">Host memory used / total</span>'
        f'<strong class="metric-value">{mem_val}</strong><span class="metric-note">{esc(mem_note)} · {sample_note}</span></div>'
        f'<div class="metric"><span class="metric-label">Running requests</span>'
        f'<strong class="metric-value">{esc(run_val)}</strong><span class="metric-note">Active vLLM requests, not a slot limit</span></div>'
        f'<div class="metric"><span class="metric-label">Queued requests</span>'
        f'<strong class="metric-value">{esc(wait_val)}</strong><span class="metric-note">Waiting inference requests, not daemon jobs</span></div>'
        f'<div class="metric"><span class="metric-label">KV cache usage</span>'
        f'<strong class="metric-value">{kv_val}</strong><span class="metric-note">Reported vLLM cache usage</span></div>'
        f"</div>"
    )


def render_gpus_table(status: dict | None) -> str:
    if not isinstance(status, dict) or status.get("gpus") is None:
        return '<div class="table-scroll"><div class="empty">GPU telemetry is unavailable for this sample.</div></div>'
    gpus = status.get("gpus") or []
    if not gpus:
        return '<div class="table-scroll"><div class="empty">No supported GPU devices detected on this host.</div></div>'
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
        '<div class="table-scroll" tabindex="0" role="region" aria-label="GPU observations">'
        '<table><thead><tr><th scope="col">Index</th><th scope="col">Name</th>'
        '<th scope="col" class="numeric">Utilization</th><th scope="col" class="numeric">Memory used / total</th>'
        '<th scope="col" class="numeric">Temperature</th><th scope="col" class="numeric">Power</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def render_workloads_table(status: dict | None) -> str:
    if not isinstance(status, dict) or status.get("workloads") is None:
        return '<div class="table-scroll"><div class="empty">Kubernetes workload observations are unavailable for this sample.</div></div>'
    workloads = status.get("workloads") or []
    if not workloads:
        return '<div class="table-scroll"><div class="empty">No pods observed in namespace oksa.</div></div>'
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
        '<div class="table-scroll" tabindex="0" role="region" aria-label="Workload pod observations">'
        '<table><thead><tr><th scope="col">Pod name</th><th scope="col">Phase</th>'
        '<th scope="col">Ready</th><th scope="col" class="numeric">Restarts</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def render_models_table(
    models: list[dict],
    *,
    loaded: bool,
    error_text: str,
    filtered: bool,
) -> str:
    if not loaded and not error_text:
        return '<div class="table-scroll"><div class="empty">Loading local model inventory…</div></div>'
    if not models:
        if error_text:
            return f'<div class="table-scroll"><div class="empty">Inventory could not be read: {esc(error_text)}</div></div>'
        msg = (
            "No installed models match the current filter."
            if filtered
            else "No models are installed on this appliance. Choose Download model to acquire one."
        )
        return f'<div class="table-scroll"><div class="empty">{esc(msg)}</div></div>'
    rows = []
    for m in models:
        mid = str(m.get("id") or "")
        pub_name = str(m.get("name") or mid)
        repo_id = str(m.get("repo_id") or "-")
        rev = str(m.get("revision") or "")
        prov = str(m.get("provenance") or "unknown")
        active = m.get("active") is True
        selected = m.get("selected") is True
        cap = str(m.get("capabilities") or "insufficient")
        size_str = fmt_mib(m.get("storage_mib"))

        if active and selected:
            obs = status_badge("Observed serving", "")
        elif active:
            obs = status_badge("Observed active (unselected)", "warning")
        elif selected:
            obs = status_badge("Desired · not active", "warning")
        else:
            obs = '<span class="muted">Not observed serving</span>'

        if prov == "verified" and rev:
            prov_html = f'<code>{esc(rev[:12])}</code><span class="sub">Verified manifest</span>'
        elif prov == "catalog" and rev:
            prov_html = f'<code>{esc(rev[:12])}</code><span class="sub">Catalog metadata</span>'
        else:
            prov_html = '<span class="muted">Unknown revision</span><span class="sub">Unverified provenance</span>'

        cap_html = "Capable" if cap == "ok" else '<span class="muted">Insufficient host capabilities</span>'
        rows.append(
            f"<tr>"
            f'<td><a href="/models/{url_part(mid)}">{esc(mid)}</a><span class="sub">{esc(repo_id)}</span></td>'
            f"<td><code>{esc(pub_name)}</code></td>"
            f"<td>{prov_html}</td>"
            f"<td>{obs}</td>"
            f"<td>{cap_html}</td>"
            f'<td class="numeric">{esc(size_str)}</td>'
            f"</tr>"
        )
    return (
        '<div class="table-scroll" tabindex="0" role="region" aria-label="Installed local models">'
        '<table><thead><tr>'
        '<th scope="col">Model / local storage ID</th>'
        '<th scope="col">Public inference name</th>'
        '<th scope="col">Revision / provenance</th>'
        '<th scope="col">Serving observation</th>'
        '<th scope="col">Compatibility</th>'
        '<th scope="col" class="numeric">Aggregate local size</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def render_search_results_table(results: list[dict], *, searched: bool, error_text: str) -> str:
    if error_text:
        return f'<div class="table-scroll"><div class="empty">Search failed: {esc(error_text)}</div></div>'
    if not searched:
        return '<div class="table-scroll"><div class="empty">Enter a search term above or specify a direct repository ID to review.</div></div>'
    if not results:
        return '<div class="table-scroll"><div class="empty">No matching text-generation repositories found on Hugging Face.</div></div>'
    rows = []
    for item in results:
        repo = str(item.get("repo_id") or "")
        author = str(item.get("author") or "—")
        downloads = item.get("downloads")
        dl_text = f"{downloads:,}" if isinstance(downloads, int) else "—"
        gated = item.get("gated") is True
        if gated:
            access_html = status_badge("Gated · unsupported in UI", "warning")
            action_html = '<span class="muted">Requires host HF token</span>'
        else:
            access_html = status_badge("Public", "neutral")
            href = f"/models/download/review?repo={url_part(repo)}&revision=main"
            action_html = f'<a class="button" href="{esc(href)}">Review source →</a>'
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
    sampled_at: str,
    error_text: str,
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
        return (
            f'<div class="table-scroll"><div class="empty">'
            f"Key index is incomplete or unavailable ({esc(detail)}). Absence of a match is not proof that a key does not exist."
            f"</div></div>"
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
            f"No indexed keys matched '{raw_q}' (index sampled at {sampled_at}). "
            "Verify owner records directly if a key was created after this sample."
            if raw_q
            else f"No user API keys exist in the index (sampled at {sampled_at})."
        )
        return f'<div class="table-scroll"><div class="empty">{esc(msg)}</div></div>'
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
        return f'<div class="table-scroll"><div class="empty">{esc(msg)}</div></div>'
    if not users:
        msg = "No API users match the current filter." if filtered else "No API users exist in the gateway database."
        return f'<div class="table-scroll"><div class="empty">{esc(msg)}</div></div>'
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


def render_operations_table(jobs: list[dict], *, loaded: bool, error_text: str, filtered: bool) -> str:
    if not loaded and not jobs:
        msg = f"Operations could not be read: {error_text}" if error_text else "Loading retained daemon operations…"
        return f'<div class="table-scroll"><div class="empty">{esc(msg)}</div></div>'
    if not jobs:
        msg = "No retained operations match the current filter." if filtered else "No daemon jobs have been recorded yet."
        return f'<div class="table-scroll"><div class="empty">{esc(msg)}</div></div>'
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
            badge = status_badge("Running", "warning")
        elif state == "succeeded":
            badge = status_badge("Succeeded", "")
        elif partial:
            badge = status_badge("Failed · partial effect", "error")
        elif state == "canceled":
            badge = status_badge("Canceled", "neutral")
        else:
            badge = status_badge("Failed", "error")

        latest = (
            str(err.get("message") or "")
            or str(res.get("warning") or "")
            or str(res.get("message") or "")
            or str(job.get("progress") or "")
            or "No status message reported"
        )
        if len(latest) > 96:
            latest = latest[:93] + "…"
        rows.append(
            f"<tr>"
            f'<td><a href="/operations/{url_part(jid)}" class="mono">{esc(jid)}</a></td>'
            f"<td>{esc(operation_label(op))}</td>"
            f"<td>{target_html}</td>"
            f"<td>{badge}</td>"
            f"<td>{esc(fmt_utc(job.get('started_at')))}</td>"
            f"<td>{esc(latest)}</td>"
            f"</tr>"
        )
    return (
        '<div class="table-scroll" tabindex="0" role="region" aria-label="Retained daemon operations">'
        '<table><thead><tr>'
        '<th scope="col">Operation ID</th><th scope="col">Kind</th><th scope="col">Target</th>'
        '<th scope="col">State</th><th scope="col">Started</th><th scope="col">Latest reported status</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def render_download_review_facts(
    inspection: dict | None,
    effective_name: str,
    existing_model: dict | None,
) -> str:
    if not isinstance(inspection, dict) or not isinstance(inspection.get("metadata"), dict):
        return '<div class="empty">No remote source metadata loaded yet.</div>'
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

    fit_badge = status_badge("Fits total host RAM estimate", "") if fits else status_badge("Exceeds total host RAM estimate", "warning")
    fit_html = (
        f"{fit_badge} · {esc(fmt_mib(est_mem_mib))} ({esc(reason)}). "
        f'<span class="muted">Compares estimated model memory against total host RAM, not free RAM, GPU VRAM fit, or disk space.</span>'
    )
    if existing_model is None:
        collision_html = f'{status_badge("New local installation", "neutral")} · No existing model at <code>{esc(effective_name)}</code>'
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
                f'{status_badge("Replacement blocked · unknown provenance", "error")} · '
                f"Existing installation at <code>{esc(effective_name)}</code> ({esc(serving_note)}) has unverified provenance. "
                "Inspect storage or enter a different local storage ID."
            )
        else:
            collision_html = (
                f'{status_badge("Replacement required", "warning")} · '
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
        ("Access policy", status_badge("Gated · unsupported without host token", "error") if gated else status_badge("Public repository", "neutral")),
        (
            "Planner & serving disclosure",
            "Download completion publishes files, runs model discovery, and notifies the background planner. "
            "Existing selection or autostart policy can affect serving; download completion is not itself serving readiness.",
        ),
    ]
    return render_facts(rows)


def render_model_detail_facts(model: dict) -> str:
    mid = str(model.get("id") or "")
    pub = str(model.get("name") or mid)
    repo = str(model.get("repo_id") or "-")
    rev = str(model.get("revision") or "")
    prov = str(model.get("provenance") or "unknown")
    path = str(model.get("path") or f"/var/lib/vllm/models/{mid}")
    size = fmt_mib(model.get("storage_mib"))
    quant = str(model.get("quantization") or "none")
    cap = str(model.get("capabilities") or "insufficient")
    active = model.get("active") is True
    selected = model.get("selected") is True

    if active and selected:
        state_html = f'{status_badge("Observed serving (desired & active)", "")} · <a href="/serving">Inspect Serving →</a>'
    elif active:
        state_html = f'{status_badge("Observed active (unselected)", "warning")} · <a href="/serving">Inspect Serving →</a>'
    elif selected:
        state_html = f'{status_badge("Desired · not observed active", "warning")} · <a href="/serving">Inspect Serving →</a>'
    else:
        state_html = f'{status_badge("Not serving", "neutral")} · Global stop is controlled on <a href="/serving">Serving</a>'

    prov_badge = (
        status_badge("Verified manifest", "")
        if prov == "verified"
        else (status_badge("Catalog metadata", "neutral") if prov == "catalog" else status_badge("Unknown provenance", "warning"))
    )
    rev_html = f"<code>{esc(rev)}</code> · {prov_badge}" if rev else f"Unknown revision · {prov_badge}"
    cap_html = status_badge("Capable", "") if cap == "ok" else status_badge("Insufficient host capabilities", "error")

    return render_facts([
        ("Local storage ID", f"<code>{esc(mid)}</code>"),
        ("Public inference name", f"<code>{esc(pub)}</code>"),
        ("Source repository", f"<code>{esc(repo)}</code>"),
        ("Installed revision", rev_html),
        ("Local path", f"<code>{esc(path)}</code>"),
        ("Aggregate local size", esc(size)),
        ("Quantization", f"<code>{esc(quant)}</code>"),
        ("Host compatibility", cap_html),
        ("Serving observation", state_html),
    ])


def render_calibration_summary(cal: dict | None, *, error_text: str) -> str:
    if error_text:
        return (
            f'<div class="notice error"><strong>Calibration record could not be read.</strong> '
            f"{esc(error_text)}</div>"
        )
    if not isinstance(cal, dict):
        return '<div class="empty">Loading calibration record…</div>'
    result = cal.get("result") if isinstance(cal.get("result"), dict) else None
    status = cal.get("status") if isinstance(cal.get("status"), dict) else None

    rows: list[tuple[str, str]] = [
        (
            "Applied runtime status",
            "Recorded profile only — not verified as currently applied to the live vLLM runtime.",
        )
    ]
    if status is not None:
        st_state = str(status.get("state") or "unknown")
        st_msg = str(status.get("message") or "No message")
        st_time = fmt_utc(status.get("updated_at"))
        tone = "warning" if st_state == "running" else ("error" if st_state == "error" else "neutral")
        rows.append((
            "Current boot calibration status",
            f'{status_badge(st_state, tone)} · {esc(st_msg)} ({esc(st_time)})',
        ))
    if result is None:
        rows.append(("Recorded profile", status_badge("No recorded profile", "neutral") + " · Calibration will run automatically before serving or when requested below."))
        return render_facts(rows)

    limits = result.get("limits") if isinstance(result.get("limits"), dict) else {}
    max_seqs = limits.get("max_num_seqs", 0)
    max_len = limits.get("max_model_len", 0)
    max_batched = limits.get("max_num_batched_tokens", 0)
    gpu_util = limits.get("gpu_memory_utilization", 0.0)
    mem_bytes = limits.get("memory_bytes", 0)
    search_complete = result.get("search_complete") is True
    inc_reasons = result.get("incomplete_reasons") if isinstance(result.get("incomplete_reasons"), list) else []
    reason = str(result.get("reason") or "unknown")
    completed_at = fmt_utc(result.get("completed_at"))
    fp = str(result.get("fingerprint") or "")

    if isinstance(max_seqs, int) and max_seqs > 0 and search_complete:
        outcome_html = f'{status_badge("Usable measured profile · complete search", "")} · recorded {esc(completed_at)}'
    elif isinstance(max_seqs, int) and max_seqs > 0:
        inc_str = ", ".join(str(r) for r in inc_reasons) or "incomplete search"
        outcome_html = f'{status_badge(f"Usable limits · incomplete search ({inc_str})", "warning")} · recorded {esc(completed_at)}'
    else:
        outcome_html = f'{status_badge(f"Zero usable limits ({reason})", "error")} · recorded {esc(completed_at)}'

    mem_mib = mem_bytes / (1024 * 1024) if isinstance(mem_bytes, int) and mem_bytes > 0 else 0
    rows.extend([
        ("Profile outcome", outcome_html),
        ("Measured max context (max_model_len)", f"<code>{esc(max_len)}</code> tokens"),
        ("Measured concurrency (max_num_seqs)", f"<code>{esc(max_seqs)}</code> sequences"),
        ("Max batched tokens", f"<code>{esc(max_batched)}</code>"),
        ("GPU memory utilization target", f"<code>{gpu_util:.2f}</code>" if isinstance(gpu_util, (int, float)) else "Unavailable"),
        ("Host memory budget", esc(fmt_mib(mem_mib))),
        ("Hardware/runtime fingerprint", f"<code>{esc(fp[:24])}…</code>" if len(fp) > 24 else f"<code>{esc(fp)}</code>"),
    ])
    return render_facts(rows)


def render_operation_detail(job: dict) -> tuple[str, str, str]:
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
        badge = status_badge("Running", "warning")
    elif state == "succeeded":
        badge = status_badge("Succeeded", "")
    elif partial:
        badge = status_badge("Failed · partial effect", "error")
    elif state == "canceled":
        badge = status_badge("Canceled", "neutral")
    else:
        badge = status_badge("Failed", "error")

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
        ("Started", esc(fmt_utc(job.get("started_at")))),
        ("Ended", esc(fmt_utc(job.get("ended_at"))) if job.get("ended_at") else "Still running"),
    ]
    facts_html = render_facts(fact_rows)

    lines = []
    if job.get("progress"):
        lines.append(f"Progress: {job['progress']}")
    if res.get("message"):
        lines.append(f"Result: {res['message']}")
    if res.get("warning"):
        lines.append(f"Warning: {res['warning']}")
    if err.get("message"):
        lines.append(f"Error ({err.get('code', 'OperationFailed')}): {err['message']}")
    status_text = "\n".join(lines) if lines else "No status text reported yet."
    status_html = (
        f'<div class="progress-text"><div class="mono">{esc(status_text)}</div>'
        f'<p class="help">Latest reported status message only (not retained full output or an audit log; no numeric percentage or ETA).</p></div>'
    )

    if state == "running":
        recovery_html = (
            '<div class="notice warning">'
            "<p><strong>Operation is currently running in the daemon.</strong> Navigating away or reloading does not cancel or resubmit this job.</p>"
            "<p>Cancellation requests termination (SIGTERM) and may race completion; it does not roll back published files, calibration records, or runtime changes.</p>"
            "</div>"
        )
    elif op == "RemoveModel" and partial:
        recovery_html = (
            '<div class="notice error">'
            f"<p><strong>Partial outcome · files deleted, catalog/workload reconciliation failed.</strong></p>"
            f"<p>Confirmed effect: local files for <code>{esc(model_name)}</code> were deleted. Failed effect: catalog or workload reconciliation ({esc(res.get('warning') or err.get('message') or '')}). "
            "Saved calibration records are not deleted by model removal.</p>"
            "<p><strong>Safe next step:</strong> Do not repeat model deletion to repair reconciliation. Inspect <a href=\"/serving\">Serving</a> and <a href=\"/models\">Models</a>, "
            "and complete operator reconciliation before starting another model mutation.</p>"
            "</div>"
        )
    elif op == "DeleteUser" and partial:
        revoked_cnt = res.get("confirmed_revoked_keys", 0)
        recovery_html = (
            '<div class="notice error">'
            f"<p><strong>Partial outcome · {esc(res.get('outcome'))}.</strong></p>"
            f"<p>Confirmed revoked keys: <code>{esc(revoked_cnt)}</code>. {esc(res.get('warning') or err.get('message') or '')}</p>"
            f"<p><strong>Safe next step:</strong> Open <a href=\"/access/user?id={url_part(user_id)}\">API user {esc(user_id)}</a> to inspect surviving keys, "
            "and complete operator reconciliation before retrying deletion.</p>"
            "</div>"
        )
    elif op == "DownloadModel":
        if state == "succeeded":
            recovery_html = (
                '<div class="notice">'
                f"<p><strong>Download published.</strong> Discovery and the background planner were notified. Download completion is not itself serving readiness.</p>"
                f'<p><a class="button" href="/models/{url_part(model_name)}">Inspect model {esc(model_name)} →</a> '
                f'<a class="button" href="/serving">Observe Serving →</a></p>'
                "</div>"
            )
        else:
            recovery_html = (
                '<div class="notice warning">'
                f"<p><strong>Download did not complete ({esc(state)}).</strong> Absent or failed job completion is not proof of an untouched destination until inventory is re-read.</p>"
                f'<p><a class="button" href="/models">Inspect Models inventory →</a></p>'
                "</div>"
            )
    elif op in ("StartModel", "RestartModel", "StopServing"):
        recovery_html = (
            f'<div class="notice {"" if state == "succeeded" else "warning"}">'
            f"<p><strong>{esc(operation_label(op))} {esc(state)}.</strong> Desired selection and live runtime readiness are separate observations; do not assume previous availability after a failed or canceled transition.</p>"
            f'<p><a class="button" href="/serving">Observe Serving →</a></p>'
            "</div>"
        )
    elif op == "Calibrate":
        recovery_html = (
            f'<div class="notice {"" if state == "succeeded" else "warning"}">'
            f"<p><strong>Calibration {esc(state)}.</strong> Inspect the model's calibration section to verify whether a usable profile was recorded, and check Serving for current runtime health.</p>"
            f'<p><a class="button" href="/models/{url_part(model_name)}#calibration">Inspect {esc(model_name)} calibration →</a> '
            f'<a class="button" href="/serving">Observe Serving →</a></p>'
            "</div>"
        )
    else:
        recovery_html = (
            f'<div class="notice {"" if state == "succeeded" else "warning"}">'
            f"<p><strong>{esc(operation_label(op))} {esc(state)}.</strong> Refresh affected resources to verify current state.</p>"
            "</div>"
        )
    return facts_html, status_html, recovery_html
