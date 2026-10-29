#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oksa_model_serving as ms

SETTINGS_PATH = Path(os.environ.get("OKSA_SETTINGS_PATH", "/var/lib/oksa/settings.json"))
MANIFESTS_DIR = Path(os.environ.get("OKSA_MANIFESTS_DIR", "/var/lib/rancher/k3s/server/manifests"))

CONTACT_KEY = "http_user_agent_contact"
DEFAULT_USER_AGENT = "PuuOS-OpenWebUI/1.0"
USER_AGENT_MANIFEST = "45-open-webui.yaml"

USER_AGENT_RE = re.compile(r"(\{'User-Agent': ')([^']*)('\})")
CONTACT_RE = re.compile(r"^(?:https://[^\s'\"<>]+|mailto:[^\s'\"<>@]+@[^\s'\"<>@]+)$")


def load() -> dict:
    try:
        if not SETTINGS_PATH.is_file():
            return {}
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict) -> None:
    ms.atomic_write_text(SETTINGS_PATH, json.dumps(data, indent=2, sort_keys=True) + "\n", 0o600)


def stored_contact() -> str:
    data = load()
    contact = data.get(CONTACT_KEY)
    if not isinstance(contact, str):
        return ""
    try:
        return validate_contact(contact)
    except ValueError:
        return ""


def validate_contact(value: str) -> str:
    contact = value.strip()
    if contact and not CONTACT_RE.match(contact):
        raise ValueError("Contact must be an https:// URL or a mailto: address without spaces")
    return contact


def render_user_agent(contact: str) -> str:
    contact = contact.strip()
    if not contact:
        return DEFAULT_USER_AGENT
    return f"{DEFAULT_USER_AGENT} (+{contact})"


def user_agent_manifest(manifests_dir: Path = MANIFESTS_DIR) -> Path:
    return manifests_dir / USER_AGENT_MANIFEST


def manifest_exists(manifests_dir: Path = MANIFESTS_DIR) -> bool:
    try:
        return user_agent_manifest(manifests_dir).is_file()
    except OSError:
        return False


def apply_user_agent(contact: str, manifests_dir: Path = MANIFESTS_DIR) -> bool:
    manifest = user_agent_manifest(manifests_dir)
    if not manifest_exists(manifests_dir):
        return False
    text = manifest.read_text(encoding="utf-8")
    user_agent = render_user_agent(contact)
    updated, replacements = USER_AGENT_RE.subn(
        lambda match: f"{match.group(1)}{user_agent}{match.group(3)}", text, count=1
    )
    if not replacements:
        return False
    if updated != text:
        ms.atomic_write_text(manifest, updated, manifest.stat().st_mode & 0o777)
    return True
