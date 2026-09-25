#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

import dataclasses
import hashlib
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import tempfile
import time
from typing import Any, Dict

STATE_DIR = Path("/var/lib/puu/vllm")
MODELS_DIR = Path("/var/lib/vllm/models")


def parse_env_file(path: Path | str) -> Dict[str, str]:
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


def parse_int(val: Any, default: int = 0) -> int:
    try:
        v = int(val)
        return v if v > 0 else default
    except (ValueError, TypeError):
        return default


def parse_bool(val: Any, default: bool = False) -> bool:
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s == "true":
        return True
    if s == "false":
        return False
    return default


def parse_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def has_nvidia_hardware() -> bool:
    has_bin = Path("/usr/libexec/puu/has-nvidia-hardware")
    if has_bin.is_file() and os.access(has_bin, os.X_OK):
        return subprocess.run([str(has_bin)]).returncode == 0
    return False


def model_id(name: str) -> str:
    """Return a DNS-safe identity that fits the 52-character node-label budget."""
    stem = re.sub(r"[^a-z0-9-]+", "-", name.lower())[:35].strip("-") or "model"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    return f"{stem}-{digest}"


def model_node_label(name: str) -> str:
    return f"puu.ai/vllm-model-{model_id(name)}"


def resolve_k3s_node_name() -> str:
    env_override = os.environ.get("K3S_NODE_NAME")
    if env_override:
        return env_override
    cfg = parse_env_file("/etc/rancher/k3s/puu-cluster.env")
    if cfg.get("K3S_NODE_NAME"):
        return cfg["K3S_NODE_NAME"]
    return socket.gethostname()


def get_kubeconfig() -> str:
    return os.environ.get("KUBECONFIG", "/var/lib/rancher/k3s/k3s.yaml")


def run_kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["/usr/bin/kubectl", f"--kubeconfig={get_kubeconfig()}", *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=check)
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


def bool_str(value: bool) -> str:
    return "true" if value else "false"


SERVING_MODES = ("shared-gpu", "exclusive-gpu", "replicated", "topology-parallel")
SHARING_POLICIES = ("allow", "deny", "exclusive")


@dataclasses.dataclass
class ModelSpec:
    name: str = ""
    path: str = ""
    repo_id: str = ""
    estimated_memory_mib: int = 0
    storage_mib: int = 0
    max_context: int = 4096
    serving_mode: str = "shared-gpu"
    max_concurrent_sequences: int = 8
    max_queued_requests: int = 32
    gpu_memory_utilization: float = 0.70
    sharing_policy: str = "allow"
    gpu_memory_budget_mib: int = 0
    max_replicas: int = 1
    topology_min_gpus: int = 1
    requires_nvlink: bool = False
    requires_fast_nic: bool = False
    allow_fast_nic_fallback: bool = False
    requires_unified_memory: bool = False
    min_gpu_memory_mib: int = 0
    same_path_required: bool = True
    tensor_parallel_size: int = 1
    pipeline_parallel_size: int = 1
    autostart: bool = True
    autostart_priority: int = 0
    quantization: str = ""
    trust_remote_code: bool = False
    extra_args: str = ""
    license: str = ""
    gated: bool = False
    overload_status_code: int = 429

    @classmethod
    def from_env(
        cls,
        meta: Dict[str, str],
        *,
        name: str = "",
        path: str = "",
        estimated_default: int = 0,
    ) -> "ModelSpec":
        d = cls()

        serving_mode = meta.get("PUU_VLLM_SERVING_MODE", d.serving_mode)
        if serving_mode not in SERVING_MODES:
            serving_mode = d.serving_mode

        sharing_policy = meta.get("PUU_VLLM_SHARING_POLICY", d.sharing_policy)
        if sharing_policy not in SHARING_POLICIES:
            sharing_policy = d.sharing_policy

        estimated = parse_int(meta.get("PUU_VLLM_ESTIMATED_MEMORY_MIB"), estimated_default)
        topology_min_gpus = parse_int(meta.get("PUU_VLLM_TOPOLOGY_MIN_GPUS"), d.topology_min_gpus)

        if serving_mode == "topology-parallel":
            tensor_parallel_size = parse_int(
                meta.get("PUU_VLLM_TENSOR_PARALLEL_SIZE"), topology_min_gpus
            )
            pipeline_parallel_size = parse_int(
                meta.get("PUU_VLLM_PIPELINE_PARALLEL_SIZE"), d.pipeline_parallel_size
            )
        else:
            tensor_parallel_size = 1
            pipeline_parallel_size = 1

        return cls(
            name=name or meta.get("PUU_VLLM_MODEL_NAME", d.name),
            path=path or meta.get("PUU_VLLM_MODEL_PATH", d.path),
            repo_id=meta.get("PUU_VLLM_REPO_ID", d.repo_id),
            estimated_memory_mib=estimated,
            storage_mib=parse_int(meta.get("PUU_VLLM_STORAGE_MIB"), estimated_default),
            max_context=parse_int(meta.get("PUU_VLLM_MAX_CONTEXT"), d.max_context),
            serving_mode=serving_mode,
            max_concurrent_sequences=parse_int(
                meta.get("PUU_VLLM_MAX_CONCURRENT_SEQUENCES"), d.max_concurrent_sequences
            ),
            max_queued_requests=parse_int(
                meta.get("PUU_VLLM_MAX_QUEUED_REQUESTS"), d.max_queued_requests
            ),
            gpu_memory_utilization=parse_float(
                meta.get("PUU_VLLM_GPU_MEMORY_UTILIZATION"), d.gpu_memory_utilization
            ),
            sharing_policy=sharing_policy,
            gpu_memory_budget_mib=parse_int(
                meta.get("PUU_VLLM_GPU_MEMORY_BUDGET_MIB"), (estimated * 120 + 99) // 100
            ),
            max_replicas=parse_int(meta.get("PUU_VLLM_MAX_REPLICAS"), d.max_replicas),
            topology_min_gpus=topology_min_gpus,
            requires_nvlink=parse_bool(meta.get("PUU_VLLM_REQUIRES_NVLINK"), d.requires_nvlink),
            requires_fast_nic=parse_bool(
                meta.get("PUU_VLLM_REQUIRES_FAST_NIC"), d.requires_fast_nic
            ),
            allow_fast_nic_fallback=parse_bool(
                meta.get("PUU_VLLM_ALLOW_FAST_NIC_FALLBACK"), d.allow_fast_nic_fallback
            ),
            requires_unified_memory=parse_bool(
                meta.get("PUU_VLLM_REQUIRES_UNIFIED_MEMORY"), d.requires_unified_memory
            ),
            min_gpu_memory_mib=parse_int(
                meta.get("PUU_VLLM_MIN_GPU_MEMORY_MIB"), d.min_gpu_memory_mib
            ),
            same_path_required=parse_bool(
                meta.get("PUU_VLLM_SAME_PATH_REQUIRED"), d.same_path_required
            ),
            tensor_parallel_size=tensor_parallel_size,
            pipeline_parallel_size=pipeline_parallel_size,
            autostart=parse_bool(meta.get("PUU_VLLM_AUTOSTART"), d.autostart),
            autostart_priority=parse_int(
                meta.get("PUU_VLLM_AUTOSTART_PRIORITY"), d.autostart_priority
            ),
            quantization=meta.get("PUU_VLLM_QUANTIZATION", d.quantization),
            trust_remote_code=parse_bool(
                meta.get("PUU_VLLM_TRUST_REMOTE_CODE"), d.trust_remote_code
            ),
            extra_args=meta.get("PUU_VLLM_EXTRA_ARGS", d.extra_args),
            license=meta.get("PUU_VLLM_LICENSE", d.license),
            gated=parse_bool(meta.get("PUU_VLLM_GATED"), d.gated),
            overload_status_code=parse_int(
                meta.get("PUU_VLLM_OVERLOAD_STATUS_CODE"), d.overload_status_code
            ),
        )

    def to_env_text(self) -> str:
        lines = [
            f"PUU_VLLM_MODEL_NAME={shlex.quote(self.name)}",
            f"PUU_VLLM_MODEL_PATH={shlex.quote(self.path)}",
            f"PUU_VLLM_REPO_ID={shlex.quote(self.repo_id)}",
            f"PUU_VLLM_ESTIMATED_MEMORY_MIB={self.estimated_memory_mib}",
            f"PUU_VLLM_STORAGE_MIB={self.storage_mib}",
            f"PUU_VLLM_MAX_CONTEXT={self.max_context}",
            f"PUU_VLLM_SERVING_MODE={self.serving_mode}",
            f"PUU_VLLM_MAX_CONCURRENT_SEQUENCES={self.max_concurrent_sequences}",
            f"PUU_VLLM_MAX_QUEUED_REQUESTS={self.max_queued_requests}",
            f"PUU_VLLM_GPU_MEMORY_UTILIZATION={self.gpu_memory_utilization:.2f}",
            f"PUU_VLLM_SHARING_POLICY={self.sharing_policy}",
            f"PUU_VLLM_GPU_MEMORY_BUDGET_MIB={self.gpu_memory_budget_mib}",
            f"PUU_VLLM_MAX_REPLICAS={self.max_replicas}",
            f"PUU_VLLM_TOPOLOGY_MIN_GPUS={self.topology_min_gpus}",
            f"PUU_VLLM_REQUIRES_NVLINK={bool_str(self.requires_nvlink)}",
            f"PUU_VLLM_REQUIRES_FAST_NIC={bool_str(self.requires_fast_nic)}",
            f"PUU_VLLM_ALLOW_FAST_NIC_FALLBACK={bool_str(self.allow_fast_nic_fallback)}",
            f"PUU_VLLM_REQUIRES_UNIFIED_MEMORY={bool_str(self.requires_unified_memory)}",
            f"PUU_VLLM_MIN_GPU_MEMORY_MIB={self.min_gpu_memory_mib}",
            f"PUU_VLLM_SAME_PATH_REQUIRED={bool_str(self.same_path_required)}",
            f"PUU_VLLM_TENSOR_PARALLEL_SIZE={self.tensor_parallel_size}",
            f"PUU_VLLM_PIPELINE_PARALLEL_SIZE={self.pipeline_parallel_size}",
            f"PUU_VLLM_AUTOSTART={bool_str(self.autostart)}",
            f"PUU_VLLM_AUTOSTART_PRIORITY={self.autostart_priority}",
            f"PUU_VLLM_QUANTIZATION={shlex.quote(self.quantization)}",
            f"PUU_VLLM_TRUST_REMOTE_CODE={bool_str(self.trust_remote_code)}",
            f"PUU_VLLM_EXTRA_ARGS={shlex.quote(self.extra_args)}",
            f"PUU_VLLM_LICENSE={shlex.quote(self.license)}",
            f"PUU_VLLM_GATED={bool_str(self.gated)}",
            f"PUU_VLLM_OVERLOAD_STATUS_CODE={self.overload_status_code}",
        ]
        return "\n".join(lines) + "\n"

    def to_json_dict(self, metadata_env: Path | str) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "metadata_env": str(metadata_env),
            "estimated_memory_mib": self.estimated_memory_mib,
            "max_context": self.max_context,
            "serving_mode": self.serving_mode,
            "max_concurrent_sequences": self.max_concurrent_sequences,
            "max_queued_requests": self.max_queued_requests,
            "gpu_memory_utilization": self.gpu_memory_utilization,
            "sharing_policy": self.sharing_policy,
            "gpu_memory_budget_mib": self.gpu_memory_budget_mib,
            "max_replicas": self.max_replicas,
            "tensor_parallel_size": self.tensor_parallel_size,
            "pipeline_parallel_size": self.pipeline_parallel_size,
            "overload_status_code": self.overload_status_code,
            "repo_id": self.repo_id,
            "storage_mib": self.storage_mib,
            "autostart": self.autostart,
            "autostart_priority": self.autostart_priority,
            "quantization": self.quantization,
            "trust_remote_code": self.trust_remote_code,
            "extra_args": self.extra_args,
            "license": self.license,
            "gated": self.gated,
            "topology": {
                "min_gpus": self.topology_min_gpus,
                "requires_nvlink": self.requires_nvlink,
                "requires_fast_nic": self.requires_fast_nic,
                "allow_fast_nic_fallback": self.allow_fast_nic_fallback,
                "requires_unified_memory": self.requires_unified_memory,
                "min_gpu_memory_mib": self.min_gpu_memory_mib,
                "same_path_required": self.same_path_required,
            },
        }


@dataclasses.dataclass
class NodeCapacity:
    gpu_count: int = 0
    gpu_memory_max_mib: int = 0
    unified_memory: bool = False
    cuda_ready: bool = False
    cdi_ready: bool = False
    fast_nic_present: bool = False
    topology_parallel_capable: bool = False

    @classmethod
    def from_env(cls, capacity: Dict[str, str], topology: Dict[str, str]) -> "NodeCapacity":
        d = cls()
        return cls(
            gpu_count=parse_int(capacity.get("PUU_NODE_GPU_COUNT"), d.gpu_count),
            gpu_memory_max_mib=parse_int(
                capacity.get("PUU_NODE_GPU_MEMORY_MAX_MIB"), d.gpu_memory_max_mib
            ),
            unified_memory=parse_bool(capacity.get("PUU_NODE_UNIFIED_MEMORY"), d.unified_memory),
            cuda_ready=parse_bool(capacity.get("PUU_NODE_CUDA_READY"), d.cuda_ready),
            cdi_ready=parse_bool(capacity.get("PUU_NODE_CDI_READY"), d.cdi_ready),
            fast_nic_present=parse_bool(
                capacity.get("PUU_NODE_FAST_NIC_PRESENT"), d.fast_nic_present
            ),
            topology_parallel_capable=parse_bool(
                topology.get("PUU_NODE_TOPOLOGY_PARALLEL_CAPABLE"), d.topology_parallel_capable
            ),
        )

    @property
    def single_gpu_capacity_mib(self) -> int:
        return (self.gpu_memory_max_mib * 95) // 100


def read_models_list(path: Path | str) -> list[tuple[str, str, str]]:
    entries = []
    path = Path(path)
    if not path.is_file():
        return entries
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = [field.strip() for field in line.strip().split("\t")]
        if len(fields) >= 3 and fields[0]:
            entries.append((fields[0], fields[1], fields[2]))
    return entries


def read_first_line(path: Path | str) -> str:
    path = Path(path)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
    except (IndexError, OSError):
        return ""
