# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import sys

for _candidate in (Path("/usr/libexec/puu"), Path(__file__).resolve().parents[3] / "oksa" / "files" / "bin"):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import oksa_calibration_state as cs
import oksa_model_serving as ms

STATUS_PATH = Path("/run/puu-firstboot/status.json")
COMPLETION_PATH = Path("/var/lib/puu/firstboot/completed.json")
ENROLLMENT_PATH = Path("/var/lib/puu/firstboot/enrolled.json")
IDENTITY_PATH = Path("/var/lib/puu/identity.json")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
MACHINE_ID_PATH = Path("/etc/machine-id")
CMDLINE_PATH = Path("/proc/cmdline")
MAX_BYTES = 1024 * 1024
STATUS_TTL = 180
CORE_UNITS = (
    "puu-firstboot-homectl.service",
    "k3s.service",
    "postgresql.service",
    "puu-postgresql.service",
    "puu-k3s-addons.service",
    "puu-vllm-preseed.service",
    "puu-vllm-planner.service",
    "puu-model-serving-labeler.service",
    "avahi-daemon.service",

)


def is_live() -> bool:
    return b"puu.live" in CMDLINE_PATH.read_bytes().split()


def boot_id() -> str:
    value = BOOT_ID_PATH.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value):
        raise ValueError("Invalid boot identity")
    return value


def machine_id() -> str:
    value = MACHINE_ID_PATH.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", value) or value == "0" * 32:
        raise ValueError("Invalid machine identity")
    return value


def reject_constant(value):
    raise ValueError("Non-finite JSON number")


def decode_json(data):
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    try:
        return json.loads(data, parse_constant=reject_constant)
    except (json.JSONDecodeError, RecursionError, ValueError) as error:
        raise ValueError(f"Invalid JSON: {error}") from error


def read_record(path: Path) -> dict:
    if not path.parent.is_dir():
        raise ValueError("Unsafe record directory")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Unsafe record")
    if info.st_uid != 0 and not os.environ.get("PUU_TEST"):
        raise ValueError("Unsafe record")
    data = path.read_bytes()
    if len(data) > MAX_BYTES:
        raise ValueError("Record too large")
    value = decode_json(data)
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
        raise ValueError("Invalid record schema")
    return value


def valid_timestamp(value) -> bool:
    return type(value) in (int, float) and 0 < value < 1e12 and math.isfinite(value)


def secure_parent(path: Path, missing_ok=False) -> bool:
    parent = path.parent
    if not parent.exists():
        return False if missing_ok else False
    if not parent.is_dir():
        return False
    if parent.stat().st_uid != 0 and not os.environ.get("PUU_TEST"):
        raise ValueError("Unsafe setup directory")
    return True


def pending_identity() -> bool:
    if not secure_parent(IDENTITY_PATH, missing_ok=True):
        return False
    try:
        info = IDENTITY_PATH.stat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or (info.st_uid != 0 and not os.environ.get("PUU_TEST")):
        raise ValueError("Unsafe pending identity")
    return True


def read_enrollment(current_machine=True):
    if not secure_parent(ENROLLMENT_PATH, missing_ok=True):
        return None
    try:
        ENROLLMENT_PATH.stat()
    except FileNotFoundError:
        return None
    value = read_record(ENROLLMENT_PATH)
    identifier = value.get("machine_id")
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier) or identifier == "0" * 32:
        raise ValueError("Invalid enrollment identity")
    if not valid_timestamp(value.get("enrolled_at")):
        raise ValueError("Invalid enrollment timestamp")
    if current_machine and identifier != machine_id():
        raise ValueError("Enrollment belongs to another machine")
    return value


def setup_required() -> bool:
    pending = pending_identity()
    enrollment = read_enrollment(current_machine=not pending)
    return pending or enrollment is not None


def verification_valid(value):
    model = value.get("model")
    public_name = value.get("served_model")
    entries = ms.read_models_list(ms.STATE_DIR / "models.list")
    selected_file, disabled_file = ms.control_files()
    active_file = Path(os.environ.get("PUU_VLLM_ACTIVE_MODEL_FILE", str(ms.STATE_DIR / "active-model")))
    selected = os.environ.get("PUU_VLLM_SELECTED_MODEL", "") or (selected_file.read_text().strip() if selected_file.exists() else "")
    active = active_file.read_text().strip() if active_file.exists() else ""
    if value.get("verification") == "inference-not-configured":
        return (
            model is None and public_name is None and not entries
            and (ms.STATE_DIR / "models.list").is_file() and not selected and not active
        )
    if (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", model)
            or not isinstance(public_name, str) or not public_name):
        return False
    matches = [entry for entry in entries if entry[0] == model]
    if len(matches) != 1:
        return False
    metadata = ms.ModelSpec.read_metadata(Path(matches[0][2]))
    if public_name != (metadata.get("PUU_VLLM_REPO_ID") or model) or (selected and selected not in (model, public_name)):
        return False
    if value.get("verification") == "public-chat-completion":
        if active not in (model, public_name) or disabled_file.exists():
            return False
    elif value.get("verification") != "zero-capacity-calibration" or active:
        return False
    calibration = cs.read_record(cs.result_path(model))
    if not cs.valid_result(calibration, model):
        return False
    spec = ms.ModelSpec.from_env(metadata, name=model, path=matches[0][1])
    identity = cs.CalibrationStore.identity(spec)
    return (
        (calibration["limits"] == cs.ZERO_LIMITS) == (value["verification"] == "zero-capacity-calibration")
        and identity.get("machine_id") == machine_id()
        and calibration["identity"] == identity
        and calibration["fingerprint"] == cs.CalibrationStore.fingerprint(identity)
        and value.get("calibration_fingerprint") == calibration["fingerprint"]
    )


def completion_valid() -> bool:
    try:
        enrollment = read_enrollment()
        if enrollment is None or pending_identity():
            return False
        value = read_record(COMPLETION_PATH)
        return (
            value.get("machine_id") == machine_id()
            and verification_valid(value)
            and valid_timestamp(value.get("completed_at"))
            and type(value.get("enrolled_at")) in (int, float)
            and value["enrolled_at"] == enrollment["enrolled_at"]
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def text(value, maximum=512) -> bool:
    return isinstance(value, str) and len(value) <= maximum and all(c.isprintable() for c in value)


def read_status() -> dict:
    value = read_record(STATUS_PATH)
    if value.get("boot_id") != boot_id():
        raise ValueError("Status belongs to another boot")
    updated = value.get("updated_at")
    if not valid_timestamp(updated) or time.time() - updated < -30:
        raise ValueError("Invalid status timestamp")
    if not value.get("complete") and time.time() - updated > STATUS_TTL:
        raise ValueError("Status is stale")
    if type(value.get("complete")) is not bool:
        raise ValueError("Invalid completion state")
    if not text(value.get("stage"), 64) or not text(value.get("message")):
        raise ValueError("Invalid status text")
    details = value.get("details")
    if not isinstance(details, list) or len(details) > 32 or not all(text(item) for item in details):
        raise ValueError("Invalid status details")
    steps = value.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 16:
        raise ValueError("Invalid status steps")
    identifiers = set()
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("Invalid step")
        if not text(step.get("id"), 64) or not step["id"] or step["id"] in identifiers:
            raise ValueError("Invalid step identity")
        identifiers.add(step["id"])
        if not text(step.get("label"), 128) or not text(step.get("detail")):
            raise ValueError("Invalid step text")
        if step.get("state") not in ("pending", "running", "done", "error"):
            raise ValueError("Invalid step state")
    if value["complete"] and not completion_valid():
        raise ValueError("Completion is not durably verified")
    return value


def atomic_record(path: Path, value: dict) -> None:
    if os.geteuid() != 0 and not os.environ.get("PUU_TEST"):
        raise PermissionError("First-boot records require root")
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"
    if len(data) > MAX_BYTES:
        raise ValueError("Record too large")
    fd, name = tempfile.mkstemp(prefix=".firstboot-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            try:
                os.unlink(name)
            except OSError:
                pass


def command(arguments: list[str], timeout=15) -> bytes:
    try:
        res = subprocess.run(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Command timed out") from exc
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise OSError("Command failed") from exc
    if len(res.stdout) > MAX_BYTES:
        raise ValueError("Command response too large")
    return res.stdout


def unit_state(unit: str, timeout=5) -> dict:
    output = command([
        "systemctl", "show", unit, "--no-pager",
        "--property=LoadState,ActiveState,SubState,Result,ExecMainStatus,ExecMainCode,ExecMainStartTimestampMonotonic,Job",
    ], timeout=timeout).decode("utf-8")
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def unit_completed(value: dict) -> bool:
    return (
        value.get("LoadState") == "loaded"
        and value.get("ActiveState") == "active"
        and value.get("SubState") == "exited"
        and value.get("Result") == "success"
        and value.get("ExecMainCode") == "1"
        and value.get("ExecMainStatus") == "0"
        and value.get("ExecMainStartTimestampMonotonic", "0").isdigit()
        and int(value["ExecMainStartTimestampMonotonic"]) > 0
    )
