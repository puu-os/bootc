#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import tempfile
import time


OKSA_NAMESPACE = "oksa"
KUBEAI_API = f"http://kubeai.{OKSA_NAMESPACE}.svc.cluster.local/openai/v1"


def parse_env_file(path: Path | str) -> dict[str, str]:
    res = {}
    path = Path(path)
    if not path.is_file():
        return res
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        try:
            val = " ".join(shlex.split(val))
        except ValueError:
            pass
        res[key] = val
    return res


def resolve_k3s_node_name() -> str:
    env_override = os.environ.get("K3S_NODE_NAME")
    if env_override:
        return env_override
    cfg = parse_env_file("/etc/rancher/k3s/puu-cluster.env")
    if cfg.get("K3S_NODE_NAME"):
        return cfg["K3S_NODE_NAME"]
    return socket.gethostname()


def _valid_fqdn(candidate: str) -> str | None:
    fqdn = candidate.strip().lower().rstrip(".")
    if not fqdn or "." not in fqdn or fqdn.startswith("api."):
        return None
    if fqdn in ("localhost", "localhost.localdomain") or fqdn.endswith(".localhost"):
        return None
    return fqdn if re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", fqdn) else None


def avahi_host_fqdn() -> str:
    raw_host = socket.gethostname().strip().lower().rstrip(".")
    short_host = (raw_host.split(".", 1)[0] if raw_host else "") or "puu"
    try:
        proc = subprocess.run(
            ["/usr/bin/busctl", "--system", "--json=short", "call",
             "org.freedesktop.Avahi", "/", "org.freedesktop.Avahi.Server", "GetHostNameFqdn"],
            capture_output=True, text=True, check=False, timeout=3,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout).get("data") or []
            if data and isinstance(data[0], str):
                fqdn = _valid_fqdn(data[0])
                if fqdn:
                    return fqdn
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return f"{short_host}.local"


def discover_host_fqdns() -> list[str]:
    raw_host = socket.gethostname().strip().lower().rstrip(".")
    short_host = (raw_host.split(".", 1)[0] if raw_host else "") or "puu"
    fqdns: list[str] = []

    def add(candidate: str | None) -> None:
        fqdn = _valid_fqdn(candidate or "")
        if fqdn and fqdn not in fqdns:
            fqdns.append(fqdn)

    add(avahi_host_fqdn())
    add(raw_host)
    try:
        add(socket.getfqdn())
    except OSError:
        pass
    try:
        for entry in socket.getaddrinfo(raw_host or short_host, None, flags=socket.AI_CANONNAME):
            add(entry[3])
    except OSError:
        pass

    seen_domains: set[str] = set()
    for resolv_path in (Path("/etc/resolv.conf"), Path("/run/systemd/resolve/resolv.conf"), Path("/run/NetworkManager/resolv.conf")):
        try:
            lines = resolv_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            parts = line.split("#", 1)[0].split()
            if len(parts) >= 2 and parts[0] in ("search", "domain"):
                for raw_domain in parts[1:]:
                    domain = raw_domain.strip().lstrip(".").lower()
                    if not domain or domain in seen_domains:
                        continue
                    seen_domains.add(domain)
                    candidate = f"{short_host}.{domain}"
                    try:
                        for entry in socket.getaddrinfo(candidate, None, flags=socket.AI_CANONNAME):
                            add(entry[3])
                    except OSError:
                        pass
                    add(candidate)

    if not fqdns:
        fqdns.append(f"{short_host}.local")
    return fqdns

def primary_gateway_hosts() -> tuple[str, str]:
    fqdn = avahi_host_fqdn()
    return fqdn, f"api.{fqdn}"


def get_kubeconfig() -> str:
    return os.environ.get("KUBECONFIG", "/var/lib/rancher/k3s/k3s.yaml")


def run_kubectl(*args: str, check: bool = True, timeout: float | None = None,
                kubeconfig: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["/usr/bin/kubectl", f"--kubeconfig={get_kubeconfig() if kubeconfig is None else kubeconfig}", *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout)
    except OSError as exc:
        if check:
            raise
        # kubectl is absent until k3s unpacks it; callers polling with
        # check=False must see a failed run, not an exception.
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: {exc.strerror}\n")


def wait_for_kubectl(*args: str, timeout_sec: int = 240) -> bool:
    start = time.time()
    while time.time() - start < timeout_sec:
        proc = run_kubectl(*args, check=False)
        if proc.returncode == 0:
            return True
        time.sleep(2)
    return False


def wait_for_node(node_name: str, timeout_sec: int = 240) -> bool:
    return wait_for_kubectl("get", "node", node_name, timeout_sec=timeout_sec)


def atomic_write_text(dest_path: Path | str, text: str, mode: int = 0o644) -> None:
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(dest.parent), delete=False) as tf:
        tf.write(text)
        tmp_name = tf.name
    try:
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, dest)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
