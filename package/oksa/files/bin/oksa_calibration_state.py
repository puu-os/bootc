# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile

RESULT_PATH = Path("/var/lib/puu/calibration/bonsai-2-27b.json")
STATUS_PATH = Path("/run/puu-calibration/status.json")
MODEL = "bonsai-2-27b"
ZERO_LIMITS = dict(max_model_len=0, max_num_seqs=0, max_num_batched_tokens=0,
                   gpu_memory_utilization=0, memory_bytes=0)

def result_path(model=MODEL):
    return Path(f"/var/lib/puu/calibration/{model}.json")


def status_path(model=MODEL):
    if model == MODEL:
        return STATUS_PATH
    return Path(f"/run/puu-calibration/status-{model}.json")


def read_record(path):
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise PermissionError(f"Unsafe calibration directory: {path.parent}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise PermissionError(f"Unsafe calibration record: {path}")
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("Calibration record too large")
    try:
        value = json.loads(data)
    except RecursionError as error:
        raise ValueError("Calibration record nested too deeply") from error
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
        raise ValueError("Invalid calibration schema")
    return value


def atomic_record(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise PermissionError(f"Unsafe calibration directory: {path.parent}")
    fd, name = tempfile.mkstemp(prefix=".calibration-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o644)
            json.dump(value, stream, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def valid_result(value, model=MODEL):
    if not isinstance(value, dict) or value.get("schema") != 1 or value.get("state") != "complete":
        return False
    if model is not None:
        if value.get("model") != model:
            return False
    elif not isinstance(value.get("model"), str) or not value.get("model"):
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("fingerprint", ""))):
        return False
    identity = value.get("identity")
    if not isinstance(identity, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(identity.get("machine_id", ""))):
        return False
    if not isinstance(identity.get("gpus"), list):
        return False
    completed = value.get("completed_at")
    if type(completed) not in (int, float) or not 0 < completed < 1e12:
        return False
    search_complete = value.get("search_complete")
    incomplete_reasons = value.get("incomplete_reasons")
    if type(search_complete) is not bool or not isinstance(incomplete_reasons, list):
        return False
    if any(not isinstance(reason, str) or reason not in ("candidate-timeout", "global-time-budget", "trial-budget")
           for reason in incomplete_reasons):
        return False
    if len(incomplete_reasons) != len(set(incomplete_reasons)) or search_complete != (not incomplete_reasons):
        return False
    limits = value.get("limits")
    if not isinstance(limits, dict) or set(limits) != set(ZERO_LIMITS):
        return False
    for key in ("max_model_len", "max_num_seqs", "max_num_batched_tokens", "memory_bytes"):
        if type(limits[key]) is not int or limits[key] < 0:
            return False
    utilization = limits["gpu_memory_utilization"]
    if type(utilization) not in (int, float) or not math.isfinite(utilization):
        return False
    if limits == ZERO_LIMITS:
        return search_complete and value.get("reason") == ("insufficient-memory" if identity["gpus"] else "no-gpu")
    metrics = value.get("metrics", {})
    if not isinstance(metrics, dict):
        return False
    for key in ("tokens_per_second", "peak_gpu_memory_bytes", "minimum_host_available_bytes", "kv_cache_tokens"):
        number = metrics.get(key)
        if type(number) not in (int, float) or not 0 < number < 1e18:
            return False
    if metrics["kv_cache_tokens"] < limits["max_model_len"] * limits["max_num_seqs"]:
        return False
    return (512 <= limits["max_model_len"] <= 262144
            and 1 <= limits["max_num_seqs"] <= 32
            and 512 <= limits["max_num_batched_tokens"] <= 65536
            and 0.5 <= utilization <= 0.95 and limits["memory_bytes"] > 0
            and bool(identity["gpus"]) and value.get("reason") == "measured")


def applied_result(model=MODEL):
    result = read_record(result_path(model))
    status = read_record(status_path(model))
    if (not valid_result(result, model=model) or status.get("boot_id") != boot_id()
            or status.get("state") != "complete" or status.get("fingerprint") != result["fingerprint"]):
        raise ValueError("Calibration has not been applied during this boot")
    return result
