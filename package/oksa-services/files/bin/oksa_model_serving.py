#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

from contextlib import contextmanager
import dataclasses
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Any, Dict
import urllib.error
import urllib.parse
import urllib.request

STATE_DIR = Path(os.environ.get("PUU_VLLM_STATE_DIR", "/var/lib/puu/vllm"))
MODELS_DIR = Path(os.environ.get("PUU_VLLM_MODELS_DIR", "/var/lib/vllm/models"))
PRESEED_MANIFEST = ".puu-preseed.json"
CALIBRATION_RESULT_PATH = Path(os.environ.get("PUU_CALIBRATION_FILE", "/var/lib/puu/calibration/bonsai-2-27b.json"))
OKSA_NAMESPACE = "oksa"
CALIBRATION_MODEL = "bonsai-2-27b"
CALIBRATION_REPO = "fraserprice/Ternary-Bonsai-2-27B-vllm"
CALIBRATED_MANIFEST_PATH = Path("/var/lib/rancher/k3s/server/manifests/33-bonsai-2-27b.yaml")


def is_calibration_model(name: str) -> bool:
    return isinstance(name, str) and name.lower() in (
        CALIBRATION_MODEL, f"prism-ml/{CALIBRATION_MODEL}",
        CALIBRATION_REPO.lower(), CALIBRATION_REPO.rsplit("/", 1)[-1].lower(),
    )


def public_model_name(name: str, state_dir: Path = STATE_DIR) -> str:
    if is_calibration_model(name):
        return CALIBRATION_REPO
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        return name
    return parse_env_file(state_dir / "models.d" / f"{name}.env").get("PUU_VLLM_REPO_ID") or name


def resolve_model_name(name: str, state_dir: Path = STATE_DIR) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("Model name must be a non-empty string")
    if is_calibration_model(name):
        return CALIBRATION_MODEL
    matches = [internal for internal, _, _ in read_models_list(state_dir / "models.list")
               if name in (internal, public_model_name(internal, state_dir))]
    if len(matches) != 1:
        raise ValueError(f"Unknown or ambiguous model: {name}")
    return matches[0]


def calibration_model_info() -> dict:
    from oksa_calibration_state import applied_result

    try:
        capable = applied_result()["limits"]["max_num_seqs"] > 0
    except (OSError, ValueError):
        capable = False
    proc = run_kubectl("get", "deployment", CALIBRATION_MODEL, "-n", OKSA_NAMESPACE,
                       "--ignore-not-found", "--request-timeout=5s", "-o", "json", check=False)
    deployment = json.loads(proc.stdout) if proc.returncode == 0 and proc.stdout.strip() else {}
    containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    repo_id = CALIBRATION_REPO
    if containers:
        command = containers[0].get("command", []) + containers[0].get("args", [])
        if "serve" in command and command.index("serve") + 1 < len(command):
            repo_id = command[command.index("serve") + 1]
    cache = Path("/var/lib/huggingface/hub") / ("models--" + repo_id.replace("/", "--")) / "blobs"
    storage = sum(path.stat().st_size for path in cache.glob("*")
                  if path.is_file() and not path.name.endswith(".incomplete"))
    status = deployment.get("status", {})
    return {
        "name": repo_id,
        "id": CALIBRATION_MODEL,
        "repo_id": repo_id,
        "storage_mib": storage // (1024 * 1024),
        "quantization": "ternary",
        "capabilities": "ok" if capable else "insufficient",
        "active": (status.get("readyReplicas", 0) > 0
                   and status.get("observedGeneration") == deployment.get("metadata", {}).get("generation")),
        "removable": False,
    }

@contextmanager
def preseed_publish_lock(models_dir: Path, exclusive: bool = False):
    lock_path = models_dir.parent / ".puu-preseed-publish.lock"
    with lock_path.open("a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield


def valid_model_file(name: str) -> bool:
    if not isinstance(name, str) or not name:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and all(part not in ("", ".", "..") for part in name.split("/")) and "\\" not in name


def is_serving_file(name: str) -> bool:
    if not valid_model_file(name):
        return False
    if name.startswith(".") or "/." in name:
        return False
    lower = name.lower()
    if lower.endswith((".md", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".gitattributes", ".gitignore")):
        return False
    return True


def verified_preseed(model_dir: Path, repo_id: str | None = None, revision: str | None = None) -> bool:
    try:
        manifest = json.loads((model_dir / PRESEED_MANIFEST).read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not manifest.get("repo_id") or not manifest.get("revision"):
            return False
        if repo_id is not None and manifest["repo_id"] != repo_id:
            return False
        if revision is not None and manifest["revision"] != revision:
            return False
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            return False
        names = set()
        for entry in files:
            name, size = entry["path"], entry["size"]
            mtime_ns = entry["mtime_ns"]
            if not valid_model_file(name) or name in names or not isinstance(size, int) or size <= 0:
                return False
            names.add(name)
            path = model_dir / name
            if not path.is_file():
                return False
            stat = path.stat()
            if stat.st_size != size or stat.st_mtime_ns != mtime_ns:
                return False
        return "config.json" in names and any(name.endswith((".safetensors", ".bin")) for name in names)
    except (OSError, ValueError, TypeError, KeyError):
        return False


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

    for resolv_path in (Path("/etc/resolv.conf"), Path("/run/systemd/resolve/resolv.conf"), Path("/run/NetworkManager/resolv.conf")):
        try:
            lines = resolv_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            parts = line.split("#", 1)[0].split()
            if len(parts) >= 2 and parts[0] in ("search", "domain"):
                for domain in parts[1:]:
                    candidate = f"{short_host}.{domain.strip().lstrip('.')}"
                    try:
                        for entry in socket.getaddrinfo(candidate, None, flags=socket.AI_CANONNAME):
                            add(entry[3] or candidate)
                        add(candidate)
                    except OSError:
                        add(candidate)

    if not fqdns:
        fqdns.append(f"{short_host}.local")
    return fqdns


def discover_api_hostnames() -> list[str]:
    return [f"api.{fqdn}" for fqdn in discover_host_fqdns()]


def primary_gateway_hosts() -> tuple[str, str]:
    fqdn = avahi_host_fqdn()
    return fqdn, f"api.{fqdn}"


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


def read_calibration_limits(path: Path | str | None = None) -> tuple[dict, bool]:
    cal_path = Path(path) if path is not None else CALIBRATION_RESULT_PATH
    if not cal_path.is_file():
        return {}, False
    try:
        data = json.loads(cal_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("schema") == 1 and data.get("state") == "complete":
            limits = data.get("limits")
            if isinstance(limits, dict):
                return limits, True
    except (OSError, ValueError, TypeError):
        pass
    return {}, False


def host_memory_capacity_mib(calibration_path: Path | str | None = None) -> int:
    limits, valid = read_calibration_limits(calibration_path)
    if valid:
        mem_bytes = limits.get("memory_bytes", 0)
        if isinstance(mem_bytes, int) and mem_bytes > 0:
            return mem_bytes // (1024 * 1024)
        return 0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemTotal:"):
                kb = int(line.split()[1])
                return (kb * 1024) // (1024 * 1024)
    except (OSError, ValueError, IndexError):
        pass
    return 0


def check_model_capacity(
    estimated_memory_mib: int,
    calibration_path: Path | str | None = None,
) -> tuple[bool, str]:
    limits, valid = read_calibration_limits(calibration_path)
    if valid:
        mem_bytes = limits.get("memory_bytes", 0)
        if not isinstance(mem_bytes, int) or mem_bytes <= 0:
            return False, "Host has zero calibrated capacity for standalone execution (requires multi-node QSFP clustering)"
        host_mib = mem_bytes // (1024 * 1024)
        if estimated_memory_mib > host_mib:
            return False, f"Model requires ~{estimated_memory_mib} MiB, exceeding calibrated host limit of {host_mib} MiB"
        return True, f"Model fits within calibrated host limit ({estimated_memory_mib} MiB <= {host_mib} MiB)"
    host_mib = host_memory_capacity_mib(calibration_path)
    if host_mib > 0 and estimated_memory_mib > host_mib:
        return False, f"Model requires ~{estimated_memory_mib} MiB, exceeding uncalibrated host memory of {host_mib} MiB"
    return True, f"Model fits within available memory (~{estimated_memory_mib} MiB)"


def inspect_hf_model(
    repo_id: str,
    revision: str = "main",
    timeout_sec: int = 30,
) -> dict | None:
    rev = urllib.parse.quote(revision, safe="/")
    api_url = f"https://huggingface.co/api/models/{repo_id}/revision/{rev}?blobs=true"
    headers = {"User-Agent": "puu-oksa/1.0"}
    token = os.environ.get("HF_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(api_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None

    siblings = data.get("siblings")
    if not isinstance(siblings, list):
        return None

    repo_id = data.get("id") or repo_id
    files = []
    weight_bytes = 0
    config_entry = None
    for entry in siblings:
        if not isinstance(entry, dict):
            continue
        rfilename = entry.get("rfilename", "")
        if not is_serving_file(rfilename):
            continue
        size = entry.get("size", 0)
        if not isinstance(size, int) or size <= 0:
            continue
        sha256 = entry.get("lfs", {}).get("sha256")
        blob_id = entry.get("blobId")
        files.append({"path": rfilename, "size": size, "sha256": sha256, "blob_id": blob_id})
        if rfilename == "config.json":
            config_entry = entry
        if rfilename.endswith((".safetensors", ".bin")):
            weight_bytes += size

    if not config_entry or weight_bytes == 0:
        return None

    config_dict = {}
    if isinstance(data.get("config"), dict):
        config_dict = data["config"]
    if not config_dict:
        config_url = f"https://huggingface.co/{repo_id}/raw/{rev}/config.json"
        cfg_req = urllib.request.Request(config_url, headers=headers)
        try:
            with urllib.request.urlopen(cfg_req, timeout=timeout_sec) as resp:
                config_dict = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            pass

    storage_mib = max(1, (sum(f["size"] for f in files) + 1048575) // 1048576)
    weight_mib = max(1, (weight_bytes + 1048575) // 1048576)
    estimated_memory_mib = weight_mib + max(1024, (weight_mib * 20) // 100)

    raw_context = config_dict.get("max_position_embeddings") or config_dict.get("seq_length") or config_dict.get("n_positions") or 4096
    max_context = parse_int(raw_context, 4096)

    quantization = ""
    quant_config = config_dict.get("quantization_config")
    if isinstance(quant_config, dict):
        quant_method = quant_config.get("quant_method")
        if quant_method:
            quantization = str(quant_method)
    if not quantization:
        lower_repo = repo_id.lower()
        if "nvfp4" in lower_repo:
            quantization = "compressed-tensors"
        elif "awq" in lower_repo:
            quantization = "awq"
        elif "gptq" in lower_repo:
            quantization = "gptq"
        elif "fp8" in lower_repo:
            quantization = "fp8"

    architectures = config_dict.get("architectures", [])
    if not isinstance(architectures, list):
        architectures = []

    model_name = repo_id.split("/")[-1].lower()
    model_name = re.sub(r"[^a-z0-9._-]+", "-", model_name).strip("-")

    return {
        "repo_id": repo_id,
        "revision": revision,
        "model_name": model_name,
        "storage_mib": storage_mib,
        "estimated_memory_mib": estimated_memory_mib,
        "max_context": max_context,
        "quantization": quantization,
        "architectures": architectures,
        "torch_dtype": str(config_dict.get("torch_dtype", "")),
        "gated": bool(data.get("gated", False)),
        "files": files,
    }


def generate_serving_env_text(meta: dict, model_path: Path | str) -> str:
    name = meta.get("model_name", "")
    repo_id = meta.get("repo_id", "")
    estimated = parse_int(meta.get("estimated_memory_mib"), 0)
    storage = parse_int(meta.get("storage_mib"), 0)
    context = parse_int(meta.get("max_context"), 4096)
    quant = meta.get("quantization", "")
    gated = parse_bool(meta.get("gated"), False)

    spec = ModelSpec(
        name=name,
        path=str(model_path),
        repo_id=repo_id,
        estimated_memory_mib=estimated,
        storage_mib=storage,
        max_context=context,
        serving_mode="exclusive-gpu",
        gpu_memory_utilization=0.90,
        quantization=quant,
        gated=gated,
        autostart=False,
    )
    return spec.to_env_text()


def get_litellm_master_key() -> str | None:
    env_key = os.environ.get("PUU_LITELLM_MASTER_KEY")
    if env_key:
        return env_key.strip()
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred_dir:
        cred_file = Path(cred_dir) / "litellm-master-key"
        if cred_file.is_file():
            try:
                return cred_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass
    try:
        encrypted_store = Path("/etc/credstore.encrypted/litellm-master-key")
        if encrypted_store.is_file():
            res = subprocess.run(
                ["systemd-creds", "decrypt", str(encrypted_store), "-"],
                capture_output=True,
                check=False,
            )
            if res.returncode == 0:
                key = res.stdout.decode("utf-8").strip()
                if key:
                    return key
    except (subprocess.SubprocessError, OSError):
        pass
    try:
        proc = run_kubectl("get", "secret", "puu-litellm-masterkey", "-n", OKSA_NAMESPACE, "-o", "jsonpath={.data.masterkey}", check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            import base64
            return base64.b64decode(proc.stdout.strip()).decode("utf-8").strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def litellm_base_url() -> str:
    env_url = os.environ.get("PUU_LITELLM_API_URL")
    if env_url:
        return env_url.rstrip("/")
    try:
        proc = run_kubectl(
            "get", "service", "litellm", "--namespace", OKSA_NAMESPACE, "-o", "jsonpath={.spec.clusterIP}", check=False
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return f"http://{proc.stdout.strip()}:4000"
    except Exception:
        pass
    return "http://127.0.0.1:4000"


def litellm_api_call(
    method: str,
    endpoint: str,
    payload: dict | None = None,
    master_key: str | None = None,
    timeout_sec: int = 15,
) -> tuple[int, dict]:
    base_url = litellm_base_url()
    key = master_key if master_key is not None else get_litellm_master_key()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    url = f"{base_url}{endpoint}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read()
            try:
                parsed = json.loads(raw.decode("utf-8"))
                return resp.status, parsed if isinstance(parsed, dict) else {"data": parsed}
            except ValueError:
                return resp.status, {"raw": raw.decode("utf-8", errors="replace")}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
            return exc.code, parsed if isinstance(parsed, dict) else {"error": str(parsed)}
        except ValueError:
            return exc.code, {"error": raw.decode("utf-8", errors="replace")}
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return 0, {"error": str(exc)}


def litellm_user_create(
    user_id: str,
    email: str | None = None,
    max_budget: float | None = None,
    master_key: str | None = None,
) -> tuple[bool, dict | str]:
    if not isinstance(user_id, str) or not user_id.strip():
        return False, "User ID is required"
    if max_budget is not None and not (0 <= max_budget < float("inf")):
        return False, "Budget must be finite and non-negative"
    payload: dict[str, Any] = {"user_id": user_id, "auto_create_key": False}
    if email:
        payload["user_email"] = email
    if max_budget is not None:
        payload["max_budget"] = max_budget
    status, resp = litellm_api_call("POST", "/user/new", payload=payload, master_key=master_key)
    if status == 200:
        return True, resp
    err = resp.get("error") or resp.get("message") or f"HTTP {status}"
    return False, str(err)


def litellm_user_list(master_key: str | None = None) -> tuple[bool, list[dict] | str]:
    users = []
    page = 1
    while True:
        status, resp = litellm_api_call(
            "GET",
            "/user/list?" + urllib.parse.urlencode({"page": page, "page_size": 100}),
            master_key=master_key,
        )
        if status != 200:
            err = resp.get("error") or resp.get("message") or f"HTTP {status}"
            return False, str(err)
        if isinstance(resp.get("data"), list) and "users" not in resp:
            return True, [u for u in resp["data"] if isinstance(u, dict)]
        batch = resp.get("users")
        if not isinstance(batch, list) or not all(isinstance(u, dict) for u in batch):
            return False, "Invalid LiteLLM user-list response"
        users.extend(batch)
        pages = resp.get("total_pages")
        if type(pages) is not int or page >= pages:
            return True, users
        page += 1


def litellm_user_delete(user_id: str, master_key: str | None = None) -> tuple[bool, str]:
    if not isinstance(user_id, str) or not user_id.strip():
        return False, "User ID is required"
    ok, keys = litellm_key_list(user_id=user_id, master_key=master_key, include_service=True)
    if not ok:
        return False, str(keys)
    if any(key.get("key_alias") == "open-webui" for key in keys):
        return False, "This user owns the Open WebUI service key and cannot be deleted"
    if keys:
        tokens = [key.get("token") for key in keys]
        if not all(isinstance(token, str) and token for token in tokens):
            return False, "Invalid LiteLLM key-list response"
        status, resp = litellm_api_call("POST", "/key/delete", payload={"keys": tokens}, master_key=master_key)
        if status != 200:
            return False, str(resp.get("error") or resp.get("message") or f"HTTP {status}")
    payload = {"user_ids": [user_id]}
    status, resp = litellm_api_call("POST", "/user/delete", payload=payload, master_key=master_key)
    if status == 200:
        return True, f"User {user_id} deleted"
    err = resp.get("error") or resp.get("message") or f"HTTP {status}"
    return False, str(err)


def litellm_user_info(user_id: str, master_key: str | None = None) -> tuple[bool, dict | str]:
    quoted_id = urllib.parse.quote(user_id, safe="")
    status, resp = litellm_api_call("GET", f"/user/info?user_id={quoted_id}", master_key=master_key)
    if status == 200:
        if isinstance(resp.get("keys"), list):
            resp["keys"] = [key for key in resp["keys"] if isinstance(key, dict) and key.get("key_alias") != "open-webui"]
        return True, resp
    err = resp.get("error") or resp.get("message") or f"HTTP {status}"
    return False, str(err)


def litellm_key_create(
    user_id: str,
    alias: str | None = None,
    models: list[str] | None = None,
    max_budget: float | None = None,
    duration: str | None = None,
    master_key: str | None = None,
) -> tuple[bool, dict | str]:
    if not isinstance(user_id, str) or not user_id.strip():
        return False, "User ID is required"
    if alias == "open-webui":
        return False, "The open-webui alias is reserved for the service key"
    if models is not None and (not isinstance(models, list) or not all(isinstance(m, str) and m.strip() for m in models)):
        return False, "Models must be a list of non-empty model IDs"
    if max_budget is not None and not (0 <= max_budget < float("inf")):
        return False, "Budget must be finite and non-negative"
    payload: dict[str, Any] = {"user_id": user_id}
    if alias:
        payload["key_alias"] = alias
    if models:
        payload["models"] = list(dict.fromkeys(public_model_name(m) for m in models))
    if max_budget is not None:
        payload["max_budget"] = max_budget
    if duration:
        payload["duration"] = duration
    status, resp = litellm_api_call("POST", "/key/generate", payload=payload, master_key=master_key)
    if status == 200:
        return True, resp
    err = resp.get("error") or resp.get("message") or f"HTTP {status}"
    return False, str(err)


def litellm_key_list(
    user_id: str | None = None, master_key: str | None = None, *, include_service: bool = False,
) -> tuple[bool, list[dict] | str]:
    keys = []
    page = 1
    while True:
        query = {"return_full_object": "true", "page": page, "size": 100}
        if user_id:
            query["user_id"] = user_id
        status, resp = litellm_api_call("GET", "/key/list?" + urllib.parse.urlencode(query), master_key=master_key)
        if status != 200:
            return False, str(resp.get("error") or resp.get("message") or f"HTTP {status}")
        batch = resp.get("keys")
        pages = resp.get("total_pages")
        if (not isinstance(batch, list) or not all(isinstance(key, dict) for key in batch)
                or type(pages) is not int or pages < 0 or resp.get("current_page") != page):
            return False, "Invalid LiteLLM key-list response"
        keys.extend(batch)
        if page >= pages:
            return True, keys if include_service else [key for key in keys if key.get("key_alias") != "open-webui"]
        page += 1


def litellm_key_delete(key_or_alias: str, master_key: str | None = None) -> tuple[bool, str]:
    ok, keys = litellm_key_list(master_key=master_key, include_service=True)
    if not ok:
        return False, str(keys)
    hashed = hashlib.sha256(key_or_alias.encode("utf-8")).hexdigest()
    matches = [key for key in keys if key.get("token") in (key_or_alias, hashed)]
    if not matches:
        matches = [key for key in keys if key.get("key_alias") == key_or_alias]
    if len(matches) != 1:
        return False, "Key not found or alias is ambiguous; use the full key or its token ID"
    key = matches[0]
    if key.get("key_alias") == "open-webui":
        return False, "The Open WebUI service key cannot be revoked here"
    token = key.get("token")
    if not isinstance(token, str) or not token:
        return False, "Invalid LiteLLM key-list response"
    payload = {"keys": [token]}
    status, resp = litellm_api_call("POST", "/key/delete", payload=payload, master_key=master_key)
    if status == 200:
        return True, f"Key {key_or_alias} revoked"
    err = resp.get("error") or resp.get("message") or f"HTTP {status}"
    return False, str(err)


def _legacy_model_map(state_dir: Path = STATE_DIR) -> dict[str, str]:
    mapping = {
        CALIBRATION_MODEL: CALIBRATION_REPO,
        f"prism-ml/{CALIBRATION_MODEL}": CALIBRATION_REPO,
        CALIBRATION_REPO.rsplit("/", 1)[-1].lower(): CALIBRATION_REPO,
    }
    for internal, _, _ in read_models_list(state_dir / "models.list"):
        public = public_model_name(internal, state_dir)
        if public != internal:
            mapping[internal] = public
            mapping[internal.lower()] = public
    return mapping


def _map_model_ref(value: str, mapping: dict[str, str]) -> str:
    return mapping.get(value) or mapping.get(value.lower()) or value


def _policy_patch(obj: dict, mapping: dict[str, str]) -> tuple[bool, dict]:
    patch: dict[str, Any] = {}
    budget_map = obj.get("model_max_budget")
    if isinstance(budget_map, dict) and any(
        isinstance(k, str) and _map_model_ref(k, mapping) != k for k in budget_map
    ):
        return False, {}
    models = obj.get("models")
    if isinstance(models, list) and models and all(isinstance(m, str) for m in models):
        migrated = list(dict.fromkeys(_map_model_ref(m, mapping) for m in models))
        if migrated != models:
            patch["models"] = migrated
    aliases = obj.get("aliases")
    if isinstance(aliases, dict) and aliases:
        migrated_aliases = {
            k: (_map_model_ref(v, mapping) if isinstance(v, str) else v)
            for k, v in aliases.items()
        }
        if migrated_aliases != aliases:
            patch["aliases"] = migrated_aliases
    metadata = obj.get("metadata")
    if isinstance(metadata, dict):
        meta_budget = metadata.get("model_max_budget")
        if isinstance(meta_budget, dict) and any(
            isinstance(k, str) and _map_model_ref(k, mapping) != k for k in meta_budget
        ):
            return False, {}
        new_meta = dict(metadata)
        meta_changed = False
        for field in ("model_rpm_limit", "model_tpm_limit"):
            limits = metadata.get(field)
            if not isinstance(limits, dict) or not limits:
                continue
            rekeyed = {}
            for k, v in limits.items():
                nk = _map_model_ref(k, mapping) if isinstance(k, str) else k
                if nk in rekeyed and rekeyed[nk] != v:
                    return False, {}
                if nk != k and nk in limits and limits[nk] != v:
                    return False, {}
                rekeyed[nk] = v
            if rekeyed != limits:
                new_meta[field] = rekeyed
                meta_changed = True
        if meta_changed:
            patch["metadata"] = new_meta
    return True, patch


def migrate_litellm_model_restrictions(
    state_dir: Path = STATE_DIR,
    master_key: str | None = None,
) -> bool:
    mapping = _legacy_model_map(state_dir)
    ok, keys = litellm_key_list(master_key=master_key, include_service=True)
    if not ok or not isinstance(keys, list):
        return False
    team_ids: set[str] = set()
    for key_obj in keys:
        tid = key_obj.get("team_id")
        if isinstance(tid, str) and tid:
            team_ids.add(tid)
        valid, patch = _policy_patch(key_obj, mapping)
        if not valid:
            return False
        if not patch:
            continue
        token = key_obj.get("token") or key_obj.get("key")
        if not isinstance(token, str) or not token:
            return False
        status, _ = litellm_api_call("POST", "/key/update", payload={"key": token, **patch}, master_key=master_key)
        if status != 200:
            return False
    ok, users = litellm_user_list(master_key=master_key)
    if not ok or not isinstance(users, list):
        return False
    for user_obj in users:
        for tid in user_obj.get("teams") or []:
            if isinstance(tid, str) and tid:
                team_ids.add(tid)
        valid, patch = _policy_patch(user_obj, mapping)
        if not valid:
            return False
        if not patch:
            continue
        uid = user_obj.get("user_id")
        if not isinstance(uid, str) or not uid:
            return False
        status, _ = litellm_api_call("POST", "/user/update", payload={"user_id": uid, **patch}, master_key=master_key)
        if status != 200:
            return False
    for tid in sorted(team_ids):
        status, resp = litellm_api_call(
            "GET", "/team/info?" + urllib.parse.urlencode({"team_id": tid}), master_key=master_key
        )
        if status != 200:
            return False
        team_obj = resp.get("team_info") if isinstance(resp.get("team_info"), dict) else resp
        if not isinstance(team_obj, dict):
            return False
        valid, patch = _policy_patch(team_obj, mapping)
        if not valid:
            return False
        if not patch:
            continue
        status, _ = litellm_api_call("POST", "/team/update", payload={"team_id": tid, **patch}, master_key=master_key)
        if status != 200:
            return False
    return True


def sync_litellm_models(
    active_model: str | None,
    state_dir: Path = STATE_DIR,
    master_key: str | None = None,
) -> bool:
    if not migrate_litellm_model_restrictions(state_dir, master_key=master_key):
        return False
    status, resp = litellm_api_call("GET", "/model/info", master_key=master_key)
    if status != 200 or not isinstance(resp.get("data"), list):
        return False
    expected = None
    if active_model:
        internal = CALIBRATION_MODEL if is_calibration_model(active_model) else active_model
        public_name = public_model_name(internal, state_dir)
        label = CALIBRATION_MODEL if internal == CALIBRATION_MODEL else model_id(internal)
        service_name = CALIBRATION_MODEL if internal == CALIBRATION_MODEL else f"puu-vllm-{label}"
        expected = {
            "id": f"puu-runtime-{label}",
            "model_name": public_name,
            "model": f"openai/{public_name}",
            "api_base": f"http://{service_name}.{OKSA_NAMESPACE}.svc.cluster.local:8000/v1",
        }

    def _matches(dep: dict) -> bool:
        if expected is None:
            return False
        info = dep.get("model_info") if isinstance(dep.get("model_info"), dict) else {}
        params = dep.get("litellm_params") if isinstance(dep.get("litellm_params"), dict) else {}
        return (
            info.get("id") == expected["id"]
            and dep.get("model_name") == expected["model_name"]
            and params.get("model") == expected["model"]
            and params.get("api_base") == expected["api_base"]
        )

    kept = False
    for dep in resp["data"]:
        if not isinstance(dep, dict):
            continue
        info = dep.get("model_info") if isinstance(dep.get("model_info"), dict) else {}
        dep_id = info.get("id")
        if not isinstance(dep_id, str) or not dep_id.startswith("puu-runtime-"):
            continue
        if _matches(dep) and not kept:
            kept = True
            continue
        del_status, _ = litellm_api_call("POST", "/model/delete", payload={"id": dep_id}, master_key=master_key)
        if del_status != 200:
            return False

    if expected is not None and not kept:
        new_status, _ = litellm_api_call(
            "POST",
            "/model/new",
            payload={
                "model_name": expected["model_name"],
                "litellm_params": {
                    "model": expected["model"],
                    "api_base": expected["api_base"],
                    "api_key": "none",
                },
                "model_info": {"id": expected["id"]},
            },
            master_key=master_key,
        )
        if new_status != 200:
            return False

    v_status, v_resp = litellm_api_call("GET", "/model/info", master_key=master_key)
    if v_status != 200 or not isinstance(v_resp.get("data"), list):
        return False
    managed = [
        dep for dep in v_resp["data"]
        if isinstance(dep, dict)
        and isinstance(dep.get("model_info"), dict)
        and str(dep["model_info"].get("id") or "").startswith("puu-runtime-")
    ]
    if expected is None:
        return len(managed) == 0
    return len(managed) == 1 and _matches(managed[0])


def collect_gpu_telemetry() -> list[dict]:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return []
    try:
        proc = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if proc.returncode != 0:
            return []
        gpus = []
        for line in proc.stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 7:
                try:
                    gpus.append({
                        "index": int(parts[0]),
                        "name": parts[1],
                        "gpu_util_pct": float(parts[2]),
                        "memory_used_mib": float(parts[3]),
                        "memory_total_mib": float(parts[4]),
                        "temperature_c": float(parts[5]),
                        "power_w": float(parts[6]),
                    })
                except ValueError:
                    pass
        return gpus
    except (subprocess.SubprocessError, OSError):
        return []


def collect_host_memory() -> dict:
    total_kb = 0
    avail_kb = 0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail_kb = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    total_mib = total_kb // 1024
    avail_mib = avail_kb // 1024
    used_mib = max(0, total_mib - avail_mib)
    return {
        "total_mib": total_mib,
        "used_mib": used_mib,
        "available_mib": avail_mib,
        "used_pct": round((used_mib / total_mib * 100.0), 1) if total_mib > 0 else 0.0,
    }


def collect_k8s_workloads() -> list[dict]:
    try:
        proc = run_kubectl("get", "pods", "-n", OKSA_NAMESPACE, "-o", "json", check=False)
        if proc.returncode != 0:
            return []
        data = json.loads(proc.stdout)
        items = data.get("items", [])
        workloads = []
        for item in items:
            meta = item.get("metadata", {})
            name = meta.get("name", "")
            status = item.get("status", {})
            phase = status.get("phase", "Unknown")
            conditions = status.get("conditions", [])
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
            container_statuses = status.get("containerStatuses", [])
            restarts = sum(c.get("restartCount", 0) for c in container_statuses)
            start_time = status.get("startTime", "")
            workloads.append({
                "name": name,
                "phase": phase,
                "ready": ready,
                "restarts": restarts,
                "start_time": start_time,
            })
        return workloads
    except Exception:
        return []


def collect_vllm_metrics(active_model: str) -> dict:
    if not active_model or active_model == "-":
        return {}
    service_name = CALIBRATION_MODEL if is_calibration_model(active_model) else f"puu-vllm-{model_id(active_model)}"
    try:
        proc = run_kubectl("get", "service", service_name, "-n", OKSA_NAMESPACE, "-o", "jsonpath={.spec.clusterIP}", check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            return {}
        cluster_ip = proc.stdout.strip()
        metrics_url = f"http://{cluster_ip}:8000/metrics"
        req = urllib.request.Request(metrics_url, headers={"User-Agent": "puu-oksa/1.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        parsed = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                key, val_str = parts[0], parts[1]
                try:
                    val = float(val_str)
                    if "num_requests_running" in key:
                        parsed["running_requests"] = int(val)
                    elif "num_requests_waiting" in key:
                        parsed["waiting_requests"] = int(val)
                    elif "gpu_cache_usage_factor" in key:
                        parsed["kv_cache_usage_pct"] = round(val * 100.0, 1)
                    elif "num_requests_swapped" in key:
                        parsed["swapped_requests"] = int(val)
                except ValueError:
                    pass
        return parsed
    except Exception:
        return {}


def collect_litellm_status() -> str:
    status, _ = litellm_api_call("GET", "/health/readiness", timeout_sec=3)
    if status == 200:
        return "ready"
    if status in (401, 403):
        return "ready (authenticated)"
    status_models, _ = litellm_api_call("GET", "/v1/models", timeout_sec=3)
    if status_models == 200:
        return "ready"
    if status_models in (401, 403):
        return "ready (authenticated)"
    return "unreachable"


def collect_serving_status() -> dict:
    selected_file = Path(os.environ.get("PUU_VLLM_SELECTED_MODEL_FILE", str(STATE_DIR / "selected-model")))
    disabled_file = Path(os.environ.get("PUU_VLLM_SERVING_DISABLED_FILE", str(STATE_DIR / "serving.disabled")))
    active_file = Path(os.environ.get("PUU_VLLM_ACTIVE_MODEL_FILE", str(STATE_DIR / "active-model")))

    serving_enabled = not disabled_file.exists()
    selected_model = read_first_line(selected_file) or "-"
    active_model = read_first_line(active_file) or "-"

    gpus = collect_gpu_telemetry()
    host_mem = collect_host_memory()
    workloads = collect_k8s_workloads()
    if active_model == "-" and any(w["name"].startswith(CALIBRATION_MODEL + "-") and w["ready"] for w in workloads):
        active_model = CALIBRATION_MODEL
    metrics = collect_vllm_metrics(active_model)
    gateway_status = collect_litellm_status()

    return {
        "serving_enabled": serving_enabled,
        "selected_model": public_model_name(selected_model),
        "active_model": public_model_name(active_model),
        "host_memory": host_mem,
        "gpus": gpus,
        "workloads": workloads,
        "metrics": metrics,
        "gateway_status": gateway_status,
    }
