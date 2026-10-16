# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

import json
import math
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import tempfile
import time
import sys

for _candidate in (Path("/usr/libexec/puu"), Path(__file__).resolve().parents[3] / "oksa-services" / "files" / "bin"):
    if _candidate.is_dir() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import oksa_calibration_state as cs

STATUS_PATH = Path("/run/puu-firstboot/status.json")
COMPLETION_PATH = Path("/var/lib/puu/firstboot/completed.json")
ENROLLMENT_PATH = Path("/var/lib/puu/firstboot/enrolled.json")
IDENTITY_PATH = Path("/var/lib/puu/identity.json")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
MACHINE_ID_PATH = Path("/etc/machine-id")
CMDLINE_PATH = Path("/proc/cmdline")
MODEL = "bonsai-2-27b"
SERVED_MODEL = "Bonsai-2-27B"
MAX_BYTES = 1024 * 1024
STATUS_TTL = 180
CORE_UNITS = (
    "puu-firstboot-homectl.service",
    "k3s.service",
    "postgresql.service",
    "puu-postgresql.service",
    "puu-k3s-addons.service",
    "puu-vllm-preseed.service",
    "puu-bonsai-calibration.service",
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


def read_record(path: Path) -> dict:
    directory = path.parent.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != 0 or stat.S_IMODE(directory.st_mode) != 0o755:
        raise ValueError("Unsafe record directory")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o644:
            raise ValueError("Unsafe record")
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Record too large")
    value = decode_json(data)
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
        raise ValueError("Invalid record schema")
    return value


def reject_constant(value):
    raise ValueError("Non-finite JSON number")


def decode_json(data):
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    depth = 0
    quoted = escaped = False
    for character in data:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > 64:
                raise ValueError("JSON nesting too deep")
        elif character in "]}":
            depth -= 1
    try:
        return json.loads(data, parse_constant=reject_constant)
    except RecursionError as error:
        raise ValueError("JSON nesting too deep") from error


def valid_timestamp(value) -> bool:
    return type(value) in (int, float) and 0 < value < 1e12 and math.isfinite(value)


def secure_parent(path: Path, missing_ok=False) -> bool:
    for directory in reversed(path.parents):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            if missing_ok:
                return False
            raise
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("Unsafe setup directory")
    return True


def pending_identity() -> bool:
    if not secure_parent(IDENTITY_PATH, missing_ok=True):
        return False
    try:
        info = IDENTITY_PATH.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("Unsafe pending identity")
    return True


def read_enrollment(current_machine=True):
    if not secure_parent(ENROLLMENT_PATH, missing_ok=True):
        return None
    try:
        ENROLLMENT_PATH.lstat()
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
    if value.get("verification") == "public-chat-completion":
        return True
    if value.get("verification") != "zero-capacity-calibration":
        return False
    calibration = cs.read_record(cs.RESULT_PATH)
    return (cs.valid_result(calibration) and calibration["limits"] == cs.ZERO_LIMITS
            and calibration.get("identity", {}).get("machine_id") == machine_id()
            and value.get("calibration_fingerprint") == calibration["fingerprint"])

def completion_valid() -> bool:
    try:
        enrollment = read_enrollment()
        if enrollment is None or pending_identity():
            return False
        value = read_record(COMPLETION_PATH)
        return (
            value.get("machine_id") == machine_id()
            and value.get("model") == MODEL
            and value.get("served_model") == SERVED_MODEL
            and verification_valid(value)
            and valid_timestamp(value.get("completed_at"))
            and type(value.get("enrolled_at")) in (int, float)
            and value["enrolled_at"] == enrollment["enrolled_at"]
        )
    except (OSError, ValueError):
        return False


def text(value, maximum=512) -> bool:
    return isinstance(value, str) and len(value) <= maximum and all(c.isprintable() for c in value)


def read_status() -> dict:
    value = read_record(STATUS_PATH)
    if value.get("boot_id") != boot_id():
        raise ValueError("Status belongs to another boot")
    updated = value.get("updated_at")
    if not valid_timestamp(updated) or not -30 <= time.time() - updated <= STATUS_TTL:
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
    if os.geteuid() != 0:
        raise PermissionError("First-boot records require root")
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o755:
        raise ValueError("Unsafe record directory")
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
        directory_fd = None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            os.fsync(directory_fd)
        except OSError:
            os.unlink(path)
            raise
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def command(arguments: list[str], timeout=15) -> bytes:
    deadline = time.monotonic() + timeout
    output = bytearray()
    with subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Command timed out")
                    for key, _ in selector.select(remaining):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output.extend(chunk)
                        if len(output) > MAX_BYTES:
                            raise ValueError("Command response too large")
            result = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if result != 0:
                raise OSError("Command failed")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    return bytes(output)


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
