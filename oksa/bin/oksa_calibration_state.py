# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import TYPE_CHECKING

from oksa_calibration_policy import CalibrationPolicy

if TYPE_CHECKING:
    from oksa_model_serving import ModelSpec

STATUS_PATH = Path("/run/puu-calibration/status.json")
ZERO_LIMITS = dict(max_model_len=0, max_num_seqs=0, max_num_batched_tokens=0,
                   gpu_memory_utilization=0, memory_bytes=0)


def result_path(model: str) -> Path:
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", model):
        raise ValueError("Invalid calibration model identifier")
    return Path(f"/var/lib/puu/calibration/{model}.json")


def status_path(model: str) -> Path:
    result_path(model)
    return STATUS_PATH.parent / f"status-{model}.json"


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


def valid_result(value: dict, model: str) -> bool:
    result_path(model)
    if (not isinstance(value, dict) or type(value.get("schema")) is not int
            or value["schema"] != 1 or value.get("state") != "complete" or value.get("model") != model):
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
    metrics = value.get("metrics")
    if not isinstance(metrics, dict):
        return False
    try:
        policy = CalibrationPolicy.from_dict(identity.get("policy"))
        if CalibrationPolicy.from_dict(metrics.get("policy")) != policy:
            return False
        if limits != ZERO_LIMITS:
            policy.validate_metrics(metrics, "passed")
    except ValueError:
        return False
    if limits == ZERO_LIMITS:
        reasons = ("insufficient-memory", "latency-budget") if identity["gpus"] else ("no-gpu",)
        return search_complete and value.get("reason") in reasons
    conservative_goodput = metrics.get("conservative_goodput")
    if (type(conservative_goodput) not in (int, float)
            or not 0 < conservative_goodput <= metrics["goodput"]
            or metrics["cache"]["enabled"] is not True
            or metrics["peak_host_memory_bytes"] > limits["memory_bytes"] + 64 * 1024**2
            or metrics["kv_cache_tokens"] < limits["max_model_len"] * limits["max_num_seqs"]):
        return False
    return (policy.minimum_context <= limits["max_model_len"] <= 262144
            and 1 <= limits["max_num_seqs"] <= 32
            and 512 <= limits["max_num_batched_tokens"] <= 65536
            and 0.5 <= utilization <= 0.95 and limits["memory_bytes"] > 0
            and bool(identity["gpus"]) and value.get("reason") == "measured")


def applied_result(model: str) -> dict:
    result = read_record(result_path(model))
    status = read_record(status_path(model))
    if (not valid_result(result, model=model) or status.get("boot_id") != boot_id()
            or status.get("state") != "complete" or status.get("fingerprint") != result["fingerprint"]):
        raise ValueError("Calibration has not been applied during this boot")
    return result


class CalibrationStore:
    @classmethod
    def hardware_identity(cls) -> dict:
        from oksa_accelerator import AcceleratorDiscovery, AcceleratorManager
        from oksa_model_serving import resolve_helper, resolve_k3s_node_name

        devices = AcceleratorDiscovery.scan_devices()
        gpus = (AcceleratorManager.execution_backend(AcceleratorDiscovery.primary_vendor(devices)).hardware_identity()
                if devices else [])
        memory_bytes = 0
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                memory_bytes = int(line.split()[1]) * 1024
                break
        if memory_bytes <= 0:
            raise ValueError("Host memory capacity is unavailable")
        return dict(
            machine_id=Path("/etc/machine-id").read_text().strip(), kernel=os.uname().release,
            architecture=os.uname().machine, memory_bytes=memory_bytes, gpus=gpus,
            node=resolve_k3s_node_name(),
            controller=hashlib.sha256(resolve_helper("oksa-calibrate").read_bytes()).hexdigest(),
            probe=hashlib.sha256(resolve_helper("oksa-calibration-probe").read_bytes()).hexdigest(),
            runtime=hashlib.sha256(resolve_helper("oksa_workload_manifest.py").read_bytes()).hexdigest(),
            backend=hashlib.sha256(resolve_helper("oksa_accelerator.py").read_bytes()).hexdigest(),
            calibration_policy=hashlib.sha256(resolve_helper("oksa_calibration_policy.py").read_bytes()).hexdigest(),
            calibration_benchmark=hashlib.sha256(resolve_helper("oksa_calibration_benchmark.py").read_bytes()).hexdigest(),
        )

    @classmethod
    def identity(cls, spec: ModelSpec, hardware: dict | None = None) -> dict:
        from oksa_model_serving import PRESEED_MANIFEST, is_serving_file, verified_preseed

        result_path(spec.name)
        model_dir = Path(spec.path)
        config = model_dir / "config.json"
        manifest = model_dir / PRESEED_MANIFEST
        manifest_digest = ""
        if manifest.is_file():
            if not verified_preseed(model_dir, repo_id=spec.repo_id, revision=spec.revision):
                raise ValueError(f"Preseed manifest does not match {spec.name}")
            manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        sources = {}
        for source in spec.metadata_sources:
            path = Path(source)
            spec.read_metadata(path)
            sources[source] = hashlib.sha256(path.read_bytes()).hexdigest()
        signatures = []
        for path in sorted(model_dir.rglob("*")):
            relative = path.relative_to(model_dir).as_posix()
            if not is_serving_file(relative):
                continue
            info = path.stat()
            if stat.S_ISREG(info.st_mode):
                signatures.append([relative, str(path.resolve()), info.st_size, info.st_mtime_ns,
                                   info.st_ctime_ns, info.st_ino])
        identity = dict(cls.hardware_identity() if hardware is None else hardware)
        metadata = {key: value for key, value in dataclasses.asdict(spec).items()
                    if key not in ("estimated_memory_mib", "storage_mib")}
        identity["model"] = json.loads(json.dumps(metadata))
        identity["policy"] = CalibrationPolicy.from_environment().to_dict()
        identity["metadata_sources"] = sources
        identity["config"] = hashlib.sha256(config.read_bytes()).hexdigest()
        identity["runtime_files"] = {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for argument in spec.runtime_command
            if (path := Path(argument)).is_absolute() and path.is_file()
        }
        identity["manifest"] = manifest_digest
        identity["files"] = hashlib.sha256(json.dumps(signatures).encode()).hexdigest()
        return identity

    @classmethod
    def fingerprint(cls, identity: dict) -> str:
        return hashlib.sha256(json.dumps(identity, sort_keys=True, allow_nan=False).encode()).hexdigest()

    @classmethod
    def load(cls, spec: ModelSpec, hardware: dict | None = None) -> dict:
        result = applied_result(spec.name)
        identity = cls.identity(spec, hardware=hardware)
        if result["identity"] != identity or result["fingerprint"] != cls.fingerprint(identity):
            raise ValueError(f"Calibration identity does not match {spec.name}")
        return result
