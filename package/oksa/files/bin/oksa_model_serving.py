#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

from contextlib import contextmanager
import copy
import dataclasses
import fcntl
import hashlib
import http.client
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Any, Callable, Dict, TYPE_CHECKING
import urllib.error
import urllib.parse
import urllib.request

if TYPE_CHECKING:
    from oksa_workload_evaluation import WorkloadSnapshot

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
        CALIBRATION_MODEL, CALIBRATION_REPO.lower(),
    )


def normalize_storage_id(repo_id: str, name: str | None = None) -> str:
    raw = (name if isinstance(name, str) and name.strip() else repo_id.rsplit("/", 1)[-1]).strip().lower()
    normalized = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("._-")
    return normalized if re.fullmatch(r"[a-z0-9][a-z0-9._-]*", normalized) else ""


def public_model_name(name: str, state_dir: Path = STATE_DIR) -> str:
    if is_calibration_model(name):
        return CALIBRATION_REPO
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        return name
    return parse_env_file(state_dir / "models.d" / f"{name}.env").get("PUU_VLLM_REPO_ID") or name


class InvalidModelSelection(ValueError):
    """The requested model is absent from or ambiguous in the catalogue."""


def resolve_model_name(
    name: str, state_dir: Path = STATE_DIR, *, catalogue: tuple[tuple[str, str], ...] | None = None,
) -> str:
    if not isinstance(name, str) or not name:
        raise InvalidModelSelection("Model name must be a non-empty string")
    if is_calibration_model(name):
        return CALIBRATION_MODEL
    if catalogue is None:
        catalogue = tuple((internal, public_model_name(internal, state_dir))
                          for internal, _, _ in read_models_list(state_dir / "models.list"))
    matches = [internal for internal, public_name in catalogue if name in (internal, public_name)]
    if len(matches) != 1:
        raise InvalidModelSelection(f"Unknown or ambiguous model: {name}")
    return matches[0]


def calibration_model_info(capable: bool | None = None) -> dict:
    from oksa_calibration_state import applied_result

    if capable is None:
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


def resolve_helper(name: str) -> Path:
    installed = Path("/usr/libexec/puu") / name
    if installed.is_file():
        return installed
    sibling = Path(__file__).resolve().parent / name
    return sibling if sibling.is_file() else installed


def control_files(state_dir: Path = STATE_DIR) -> tuple[Path, Path]:
    selected = Path(os.environ.get("PUU_VLLM_SELECTED_MODEL_FILE", str(state_dir / "selected-model")))
    disabled = Path(os.environ.get("PUU_VLLM_SERVING_DISABLED_FILE", str(state_dir / "serving.disabled")))
    return selected, disabled


def run_discover(discover_bin: Path | None = None) -> None:
    binary = discover_bin or resolve_helper("oksa-discover-vllm-models")
    if os.geteuid() == 0 and binary.is_file() and os.access(binary, os.X_OK):
        subprocess.run([str(binary)], check=False)


def check_capabilities(planner_bin: Path, model_name: str) -> bool:
    if not planner_bin.is_file() or not os.access(planner_bin, os.X_OK):
        return False
    from oksa_workload_evaluation import WorkloadEvaluator

    try:
        state = WorkloadState.load()
        selected_model = state.resolve_model_name(model_name)
        return WorkloadEvaluator(state.snapshot).evaluate(selected_model).capable
    except (OSError, ValueError):
        return False


def collect_models_list(
    state_dir: Path = STATE_DIR,
    discover_bin: Path | None = None,
    planner_bin: Path | None = None,
) -> dict:
    run_discover(discover_bin)
    planner = planner_bin or resolve_helper("oksa-plan-vllm-workloads")
    from oksa_workload_evaluation import WorkloadEvaluator

    state = WorkloadState.load(state_dir, capture_capability_errors=True)
    evaluator = WorkloadEvaluator(state.snapshot) if state.snapshot is not None else None
    planner_available = planner.is_file() and os.access(planner, os.X_OK)
    active_model_file = Path(os.environ.get("PUU_VLLM_ACTIVE_MODEL_FILE", str(state_dir / "active-model")))
    active_model = read_first_line(active_model_file)
    selected_file, disabled_file = control_files(state_dir)
    serving_enabled = not disabled_file.exists()
    selected_model = read_first_line(selected_file)
    if selected_model:
        try:
            selected_model = state.resolve_model_name(selected_model)
        except ValueError:
            pass

    meta_map = {}
    models_json_path = state_dir / "models.json"
    if models_json_path.is_file():
        try:
            data = json.loads(models_json_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                meta_map = {item["name"]: item for item in data if isinstance(item, dict) and "name" in item}
        except (OSError, ValueError):
            pass

    builtin = calibration_model_info(state.calibration_positive)
    builtin["selected"] = is_calibration_model(selected_model) or (not selected_model and serving_enabled)
    models = [builtin]
    if builtin["active"]:
        active_model = CALIBRATION_MODEL
    for name, public_name in state.catalogue:
        if is_calibration_model(name):
            continue
        meta = meta_map.get(name, {})
        try:
            resolved_name = state.resolve_model_name(name)
            capable = planner_available and evaluator is not None and evaluator.evaluate(resolved_name).capable
        except (OSError, ValueError):
            capable = False
        models.append({
            "name": public_name,
            "id": name,
            "repo_id": meta.get("repo_id") or "-",
            "storage_mib": meta.get("storage_mib") or 0,
            "quantization": meta.get("quantization") or "none",
            "capabilities": "ok" if capable else "insufficient",
            "active": (name == active_model),
            "selected": (name == selected_model),
        })

    return {
        "ok": True,
        "active_model": state.public_model_name(active_model, state_dir),
        "selected_model": state.public_model_name(selected_model, state_dir),
        "serving_enabled": serving_enabled,
        "models": models,
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


def label_namespace() -> str:
    hostname = socket.getfqdn().lower().rstrip(".")
    if (len(hostname) > 253 or "." not in hostname
            or hostname == "localhost.localdomain" or hostname.endswith(".localhost")
            or hostname.replace(".", "").isdigit()
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                   for part in hostname.split("."))):
        raise ValueError(f"NSS did not provide a valid fully qualified hostname: {hostname!r}")
    return hostname


def render_manifest(text: str) -> str:
    if "@OKSA_LABEL_NAMESPACE@/" not in text:
        return text
    return text.replace("@OKSA_LABEL_NAMESPACE@/", label_namespace() + "/")


def model_node_label(name: str) -> str:
    return f"{label_namespace()}/vllm-model-{model_id(name)}"


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


@dataclasses.dataclass(frozen=True)
class CalibratedWorkload:
    manifest: dict
    deployment_index: int

    @classmethod
    def load(cls) -> "CalibratedWorkload":
        manifest = json.loads(CALIBRATED_MANIFEST_PATH.read_text())
        if not isinstance(manifest, dict) or manifest.get("kind") != "List" or not isinstance(manifest.get("items"), list):
            raise ValueError("Expected calibrated Bonsai manifest List")
        deployments = [
            index for index, item in enumerate(manifest["items"])
            if isinstance(item, dict) and item.get("kind") == "Deployment"
            and isinstance(item.get("metadata"), dict)
            and item["metadata"].get("name") == CALIBRATION_MODEL
        ]
        if len(deployments) != 1 or not isinstance(manifest["items"][deployments[0]].get("spec"), dict):
            raise ValueError("Missing calibrated Bonsai Deployment")
        return cls(manifest, deployments[0])

    @property
    def replicas(self) -> int:
        return self.manifest["items"][self.deployment_index]["spec"].get("replicas", 1)

    def manifest_with_replicas(self, replicas: int) -> dict:
        manifest = copy.deepcopy(self.manifest)
        manifest["items"][self.deployment_index]["spec"]["replicas"] = replicas
        return manifest


@dataclasses.dataclass(frozen=True)
class WorkloadState:
    snapshot: "WorkloadSnapshot | None"
    calibrated: CalibratedWorkload | None
    calibration_positive: bool
    catalogue: tuple[tuple[str, str], ...]
    capability_error: OSError | ValueError | None = None

    @classmethod
    def load(
        cls,
        state_dir: Path | None = None,
        models_dir: Path | None = None,
        *,
        capture_capability_errors: bool = False,
        check_model_paths: bool = True,
        resolve_public_names: bool = True,
        selected_model: str = "",
        on_invalid_selection: Callable[[InvalidModelSelection], None] | None = None,
    ) -> "WorkloadState":
        """Load strictly, or retain listing data without a snapshot on capability read failure.

        Validate selection before parsing specs; a returning error handler permits loading to continue.
        """
        from oksa_calibration_state import applied_result
        from oksa_workload_evaluation import CalibrationSnapshot, ModelSnapshot, WorkloadSnapshot

        state_dir = STATE_DIR if state_dir is None else state_dir
        models_dir = MODELS_DIR if models_dir is None else models_dir

        capability_error = None
        node = None
        try:
            node = NodeCapacity.from_env(
                parse_env_file(state_dir / "node-capacity.env"),
                parse_env_file(state_dir / "topology.env"),
            )
        except (OSError, ValueError) as err:
            if not capture_capability_errors:
                raise
            capability_error = err
        models = []
        catalogue = []
        metadata_cache: dict[Path, dict[str, str]] = {}
        entries = read_models_list(state_dir / "models.list")
        for name, _, _ in entries:
            if is_calibration_model(name):
                public_name = CALIBRATION_REPO
            elif resolve_public_names and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
                public_path = state_dir / "models.d" / f"{name}.env"
                if public_path not in metadata_cache:
                    metadata_cache[public_path] = parse_env_file(public_path)
                public_name = metadata_cache[public_path].get("PUU_VLLM_REPO_ID") or name
            else:
                public_name = name
            catalogue.append((name, public_name))
        catalogue = tuple(catalogue)
        if selected_model:
            try:
                resolve_model_name(selected_model, catalogue=catalogue)
            except InvalidModelSelection as err:
                if on_invalid_selection is None:
                    raise
                on_invalid_selection(err)

        for (name, path, env_path), (_, public_name) in zip(entries, catalogue):
            spec = None
            # The calibrated workload is not planned from a discovered model spec.
            if name != CALIBRATION_MODEL:
                try:
                    metadata_path = Path(env_path)
                    if metadata_path not in metadata_cache:
                        metadata_cache[metadata_path] = parse_env_file(metadata_path)
                    spec = ModelSpec.from_env(metadata_cache[metadata_path], name=name, path=path)
                except (OSError, ValueError) as err:
                    if not capture_capability_errors:
                        raise
                    capability_error = err
            if spec is not None:
                path_exists = False
                path_error = None
                if check_model_paths:
                    try:
                        path_exists = Path(spec.path).is_dir()
                    except (OSError, ValueError) as err:
                        path_error = err
                models.append(ModelSnapshot(spec, path_exists, model_id(name), public_name, path_error))

        calibrated = None
        calibration_positive = False
        applied_max_sequences = None
        calibration_error = ""
        try:
            result = applied_result()
            calibration_positive = result["limits"]["max_num_seqs"] > 0
            calibrated = CalibratedWorkload.load()
            applied_max_sequences = result["limits"]["max_num_seqs"]
        except (OSError, ValueError) as err:
            calibration_error = str(err)

        limits, valid = read_calibration_limits()
        utilization = limits.get("gpu_memory_utilization") if valid else None
        max_context = limits.get("max_model_len") if valid else None
        max_sequences = limits.get("max_num_seqs") if valid else None
        calibration = CalibrationSnapshot(
            applied_max_sequences=applied_max_sequences,
            replicas=calibrated.replicas if calibrated is not None else 1,
            error=calibration_error,
            gpu_memory_utilization=(utilization if isinstance(utilization, (int, float)) and 0.05 <= utilization <= 0.98 else None),
            max_context=max_context if isinstance(max_context, int) and max_context > 0 else None,
            max_sequences=max_sequences if isinstance(max_sequences, int) and max_sequences > 0 else None,
        )
        snapshot = (WorkloadSnapshot(tuple(models), node, models_dir, CALIBRATION_MODEL, calibration)
                    if capability_error is None else None)
        return cls(snapshot, calibrated, calibration_positive, catalogue, capability_error)

    def resolve_model_name(self, name: str) -> str:
        return resolve_model_name(name, catalogue=self.catalogue)

    def public_model_name(self, name: str, state_dir: Path = STATE_DIR) -> str:
        if is_calibration_model(name):
            return CALIBRATION_REPO
        for internal, public_name in self.catalogue:
            if internal == name:
                return public_name
        return public_model_name(name, state_dir)


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
    repo_pattern = r"[A-Za-z0-9_][A-Za-z0-9._-]*(?:/[A-Za-z0-9_][A-Za-z0-9._-]*)?"
    if (not isinstance(repo_id, str) or not re.fullmatch(repo_pattern, repo_id)
            or not isinstance(revision, str) or not revision.strip()
            or re.search(r"[\x00-\x20\x7f]", revision)):
        return None
    rev = urllib.parse.quote(revision, safe="")
    api_url = f"https://huggingface.co/api/models/{repo_id}/revision/{rev}?blobs=true"
    headers = {"User-Agent": "puu-oksa/1.0"}
    token = os.environ.get("HF_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(api_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return None

    if not isinstance(data, dict):
        return None
    resolved_revision = data.get("sha")
    if not isinstance(resolved_revision, str) or not re.fullmatch(r"[0-9a-f]{40}", resolved_revision):
        return None
    canonical_repo = data.get("id")
    if not isinstance(canonical_repo, str) or not re.fullmatch(repo_pattern, canonical_repo):
        return None
    siblings = data.get("siblings")
    if not isinstance(siblings, list):
        return None

    repo_id = canonical_repo
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
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            continue
        lfs = entry.get("lfs", {})
        if not isinstance(lfs, dict):
            return None
        sha256 = lfs.get("sha256")
        blob_id = entry.get("blobId")
        files.append({"path": rfilename, "size": size, "sha256": sha256, "blob_id": blob_id})
        if rfilename == "config.json":
            config_entry = entry
        if rfilename.endswith((".safetensors", ".bin")):
            weight_bytes += size

    if not config_entry or weight_bytes == 0:
        return None

    config_dict = data.get("config", {})
    if not isinstance(config_dict, dict):
        return None
    if not config_dict:
        config_url = f"https://huggingface.co/{repo_id}/raw/{resolved_revision}/config.json"
        cfg_req = urllib.request.Request(config_url, headers=headers)
        try:
            with urllib.request.urlopen(cfg_req, timeout=timeout_sec) as resp:
                config_dict = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
            return None
        if not isinstance(config_dict, dict):
            return None

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
        "resolved_revision": resolved_revision,
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


_litellm_cache: dict[str, str | None] = {}


def get_litellm_master_key(timeout: float | None = None) -> str | None:
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
                timeout=timeout,
            )
            if res.returncode == 0:
                key = res.stdout.decode("utf-8").strip()
                if key:
                    return key
    except (subprocess.SubprocessError, OSError):
        pass
    if _litellm_cache.get("master_key"):
        return _litellm_cache["master_key"]
    try:
        proc = run_kubectl("get", "secret", "puu-litellm-masterkey", "-n", OKSA_NAMESPACE,
                           "-o", "jsonpath={.data.masterkey}", check=False, timeout=timeout)
        if proc.returncode == 0 and proc.stdout.strip():
            import base64
            key = base64.b64decode(proc.stdout.strip()).decode("utf-8").strip()
            if key:
                _litellm_cache["master_key"] = key
                return key
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def litellm_base_url(timeout: float | None = None) -> str:
    env_url = os.environ.get("PUU_LITELLM_API_URL")
    if env_url:
        return env_url.rstrip("/")
    if _litellm_cache.get("base_url"):
        return str(_litellm_cache["base_url"])
    try:
        proc = run_kubectl(
            "get", "service", "litellm", "--namespace", OKSA_NAMESPACE, "-o", "jsonpath={.spec.clusterIP}",
            check=False, timeout=timeout
        )
        if proc.returncode == 0 and proc.stdout.strip():
            url = f"http://{proc.stdout.strip()}:4000"
            _litellm_cache["base_url"] = url
            return url
    except Exception:
        pass
    return "http://127.0.0.1:4000"


def litellm_api_call(
    method: str,
    endpoint: str,
    payload: dict | None = None,
    master_key: str | None = None,
    timeout_sec: int = 15,
    subprocess_timeout: float | None = None,
) -> tuple[int, dict]:
    base_url = litellm_base_url(timeout=subprocess_timeout)
    key = master_key if master_key is not None else get_litellm_master_key(timeout=subprocess_timeout)
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
        if exc.code in (401, 403):
            _litellm_cache.pop("master_key", None)
        raw = exc.read()
        try:
            parsed = json.loads(raw.decode("utf-8"))
            return exc.code, parsed if isinstance(parsed, dict) else {"error": str(parsed)}
        except ValueError:
            return exc.code, {"error": raw.decode("utf-8", errors="replace")}
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        _litellm_cache.pop("base_url", None)
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
    warning = "Warning: User creation not confirmed. The user may exist; check before retrying."
    try:
        status, resp = litellm_api_call("POST", "/user/new", payload=payload, master_key=master_key)
    except (http.client.HTTPException, OSError, subprocess.SubprocessError, ValueError) as exc:
        return False, f"{warning} {type(exc).__name__}: {exc}"
    if status != 200:
        detail = (resp.get("error") or resp.get("message")) if isinstance(resp, dict) else None
        return False, f"{warning} HTTP {status}" + (f": {detail}" if detail else "")
    if (not isinstance(resp, dict) or resp.get("user_id") != user_id
            or any(field in resp for field in ("error", "errors", "detail"))
            or resp.get("status") in ("error", "failed", "failure") or resp.get("success") is False):
        return False, f"{warning} Invalid LiteLLM user-create response (HTTP 200)"
    return True, resp


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
    warning = "Warning: Key creation not confirmed. The key may exist; check before retrying."
    try:
        status, resp = litellm_api_call("POST", "/key/generate", payload=payload, master_key=master_key)
    except (http.client.HTTPException, OSError, subprocess.SubprocessError, ValueError) as exc:
        return False, f"{warning} {type(exc).__name__}: {exc}"
    if status != 200:
        detail = (resp.get("error") or resp.get("message")) if isinstance(resp, dict) else None
        return False, f"{warning} HTTP {status}" + (f": {detail}" if detail else "")
    if (not isinstance(resp, dict) or not isinstance(resp.get("key"), str) or not resp["key"].strip()
            or ("user_id" in resp and resp["user_id"] != user_id)
            or any(field in resp for field in ("error", "errors", "detail"))
            or resp.get("status") in ("error", "failed", "failure") or resp.get("success") is False):
        return False, f"{warning} Invalid LiteLLM key-create response (HTTP 200)"
    return True, resp


def litellm_key_list(
    user_id: str | None = None, master_key: str | None = None, *, include_service: bool = False,
) -> tuple[bool, list[dict] | str]:
    keys = []
    seen = set()
    expected_count = None
    page = 1
    while True:
        query = {"return_full_object": "true", "page": page, "size": 100}
        if user_id:
            query["user_id"] = user_id
        status, resp = litellm_api_call("GET", "/key/list?" + urllib.parse.urlencode(query), master_key=master_key)
        if status != 200:
            detail = (resp.get("error") or resp.get("message")) if isinstance(resp, dict) else None
            return False, f"HTTP {status}" + (f": {detail}" if detail else "")
        if (not isinstance(resp, dict) or any(field in resp for field in ("error", "errors", "detail"))
                or resp.get("status") in ("error", "failed", "failure") or resp.get("success") is False):
            return False, "Invalid LiteLLM key-list response"
        batch = resp.get("keys")
        pages = resp.get("total_pages")
        count = resp.get("total_count")
        if (not isinstance(batch, list) or not all(
                isinstance(key, dict) and isinstance(key.get("token"), str) and key["token"].strip()
                for key in batch)
                or type(pages) is not int or pages < 0 or type(count) is not int or count < 0
                or type(resp.get("current_page")) is not int or resp["current_page"] != page
                or page > max(1, pages) or (expected_count is not None and count != expected_count)):
            return False, "Invalid LiteLLM key-list response"
        if pages != (count + 99) // 100 and not (count == 0 and pages == 1):
            return False, "Inconsistent LiteLLM key-list page count"
        tokens = {key["token"] for key in batch}
        if (len(batch) != min(100, max(0, count - (page - 1) * 100))
                or len(tokens) != len(batch) or seen.intersection(tokens)):
            return False, "Incomplete or duplicate LiteLLM key-list response"
        expected_count = count
        seen.update(tokens)
        keys.extend(batch)
        if page >= pages:
            if len(seen) != count:
                return False, "Incomplete LiteLLM key-list response"
            return True, keys if include_service else [key for key in keys if key.get("key_alias") != "open-webui"]
        page += 1


def litellm_key_absent(token: str, master_key: str | None = None) -> tuple[bool, str]:
    """Confirm database absence, not a generic missing route or cache propagation."""
    if not isinstance(token, str) or not token.strip() or token.startswith("sk-"):
        return False, "A token ID is required to verify absence"
    endpoint = "/key/info?" + urllib.parse.urlencode({"key": token})
    try:
        status, response = litellm_api_call("GET", endpoint, master_key=master_key)
    except (http.client.HTTPException, OSError, subprocess.SubprocessError, ValueError) as exc:
        return False, f"Key absence could not be confirmed: {type(exc).__name__}: {exc}"
    error = response.get("error") if isinstance(response, dict) else None
    if status == 404 and error == {
        "message": "Key not found in database",
        "type": "not_found_error",
        "param": "key",
        "code": "404",
    }:
        return True, "Original token is absent from the gateway database"
    if status == 200:
        if isinstance(response, dict) and response.get("key") == token and isinstance(response.get("info"), dict):
            return False, "The original token is still present in the gateway database"
        return False, "Invalid LiteLLM key-info response (HTTP 200)"
    detail = error.get("message") if isinstance(error, dict) else error
    return False, f"Key absence could not be confirmed (HTTP {status})" + (f": {detail}" if detail else "")


def litellm_key_delete(key_or_alias: str, master_key: str | None = None) -> tuple[bool, str]:
    if not isinstance(key_or_alias, str) or not key_or_alias.strip():
        return False, "Key or alias is required"
    try:
        ok, keys = litellm_key_list(master_key=master_key, include_service=True)
    except (http.client.HTTPException, OSError, subprocess.SubprocessError, ValueError) as exc:
        return False, f"Cannot list keys before revocation; no write attempted: {type(exc).__name__}: {exc}"
    if not ok:
        return False, f"Cannot list keys before revocation; no write attempted: {keys}"
    if not isinstance(keys, list) or not all(
            isinstance(key, dict) and isinstance(key.get("token"), str) and key["token"].strip()
            for key in keys):
        return False, "Invalid LiteLLM key-list response; no write attempted"
    hashed = hashlib.sha256(key_or_alias.encode("utf-8")).hexdigest()
    matches = [key for key in keys if key["token"] in (key_or_alias, hashed)]
    if not matches and len(key_or_alias) >= 8:
        matches = [key for key in keys if key["token"].startswith(key_or_alias)]
    if not matches:
        matches = [key for key in keys if key.get("key_alias") == key_or_alias]
    if not matches:
        matches = [key for key in keys if key.get("key_name") == key_or_alias]
    if len(matches) != 1:
        return False, "Key not found or identifier is ambiguous; use the full key or its token ID"
    key = matches[0]
    if key.get("key_alias") == "open-webui":
        return False, "The Open WebUI service key cannot be revoked here"
    token = key.get("token")
    payload = {"keys": [token]}
    warning = "Warning: Key revocation not confirmed. The key may still grant access; check the key list before retrying."
    try:
        status, resp = litellm_api_call("POST", "/key/delete", payload=payload, master_key=master_key)
    except (http.client.HTTPException, OSError, subprocess.SubprocessError, ValueError) as exc:
        return False, f"{warning} {type(exc).__name__}: {exc}"
    if status != 200:
        detail = (resp.get("error") or resp.get("message")) if isinstance(resp, dict) else None
        return False, f"{warning} HTTP {status}" + (f": {detail}" if detail else "")
    if (not isinstance(resp, dict) or resp.get("deleted_keys") != [token]
            or any(field in resp for field in ("error", "errors", "detail"))
            or resp.get("status") in ("error", "failed", "failure") or resp.get("success") is False):
        return False, f"{warning} Invalid LiteLLM key-delete acknowledgement (HTTP 200)"
    # LiteLLM 1.90.1 echoes deletion requests; an unfiltered scan is not a snapshot.
    absent, detail = litellm_key_absent(token, master_key=master_key)
    if not absent:
        return False, f"{warning} {detail}"
    return True, f"Key {key_or_alias} revoked"


def sync_litellm_models(
    active_model: str | None,
    state_dir: Path = STATE_DIR,
    master_key: str | None = None,
) -> bool:
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
            and params.get("allowed_openai_params") == ["reasoning_effort"]
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
                    "allowed_openai_params": ["reasoning_effort"],
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


def collect_gpu_telemetry() -> list[dict] | None:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return None
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
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    gpus = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 7:
            return None
        gpu = {}
        try:
            gpu["index"] = int(parts[0])
        except ValueError:
            pass
        if parts[1] and parts[1] != "N/A":
            gpu["name"] = parts[1]
        for key, raw in zip(
            ("gpu_util_pct", "memory_used_mib", "memory_total_mib", "temperature_c", "power_w"),
            parts[2:],
        ):
            try:
                value = float(raw)
            except ValueError:
                continue
            if math.isfinite(value):
                gpu[key] = value
        gpus.append(gpu)
    return gpus


def collect_host_memory() -> dict:
    try:
        lines = Path("/proc/meminfo").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        return {}
    values = {}
    for line in lines:
        parts = line.split()
        if not parts or parts[0] not in ("MemTotal:", "MemAvailable:"):
            continue
        try:
            value = int(parts[1])
        except (ValueError, IndexError):
            continue
        if value >= 0 and len(parts) == 3 and parts[2] == "kB":
            values[parts[0]] = value
    memory = {}
    total_kb = values.get("MemTotal:")
    avail_kb = values.get("MemAvailable:")
    if total_kb is not None:
        memory["total_mib"] = total_kb // 1024
    if avail_kb is not None:
        memory["available_mib"] = avail_kb // 1024
    if total_kb is not None and avail_kb is not None and avail_kb <= total_kb:
        used_kb = total_kb - avail_kb
        memory["used_mib"] = used_kb // 1024
        if total_kb > 0:
            memory["used_pct"] = round(used_kb / total_kb * 100.0, 1)
    return memory


def collect_k8s_workloads() -> list[dict] | None:
    try:
        proc = run_kubectl("get", "pods", "-n", OKSA_NAMESPACE, "--request-timeout=5s",
                           "-o", "json", check=False, timeout=10)
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    workloads = []
    for item in data["items"]:
        if not isinstance(item, dict):
            return None
        meta = item.get("metadata", {})
        status = item.get("status", {})
        if not isinstance(meta, dict) or not isinstance(status, dict):
            return None
        conditions = status.get("conditions")
        ready = None
        if isinstance(conditions, list) and all(isinstance(c, dict) for c in conditions):
            if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
                ready = True
            elif not conditions or any(c.get("type") == "Ready" and c.get("status") == "False" for c in conditions):
                ready = False
        container_statuses = status.get("containerStatuses")
        restarts = None
        if isinstance(container_statuses, list) and all(
            isinstance(c, dict) and type(c.get("restartCount")) is int and c["restartCount"] >= 0
            for c in container_statuses
        ):
            restarts = sum(c["restartCount"] for c in container_statuses)
        workloads.append({
            "name": meta.get("name", ""),
            "phase": status.get("phase", "Unknown"),
            "ready": ready,
            "restarts": restarts,
            "start_time": status.get("startTime", ""),
        })
    return workloads


def collect_vllm_metrics(active_model: str) -> tuple[str, dict]:
    if not active_model or active_model == "-":
        return "no-model", {}
    service_name = CALIBRATION_MODEL if is_calibration_model(active_model) else f"puu-vllm-{model_id(active_model)}"
    try:
        proc = run_kubectl("get", "service", service_name, "-n", OKSA_NAMESPACE, "--request-timeout=5s",
                           "-o", "jsonpath={.spec.clusterIP}", check=False, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return "unavailable", {}
    cluster_ip = proc.stdout.strip()
    if proc.returncode != 0 or not cluster_ip or cluster_ip == "None":
        return "unavailable", {}
    host = f"[{cluster_ip}]" if ":" in cluster_ip else cluster_ip
    base_url = f"http://{host}:8000"
    model_status = "unavailable"
    req = urllib.request.Request(f"{base_url}/health", headers={"User-Agent": "puu-oksa/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            model_status = "ready" if resp.status == 200 else "not-ready"
    except urllib.error.HTTPError as exc:
        model_status = "unavailable" if exc.code in (401, 403, 404) else "not-ready"
        exc.close()
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        pass
    req = urllib.request.Request(f"{base_url}/metrics", headers={"User-Agent": "puu-oksa/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status != 200:
                return model_status, {}
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        exc.close()
        return model_status, {}
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return model_status, {}

    metric_names = {
        "vllm:num_requests_running": "running_requests",
        "vllm:num_requests_waiting": "waiting_requests",
        "vllm:kv_cache_usage_perc": "kv_cache_usage_pct",
        "vllm:gpu_cache_usage_factor": "kv_cache_usage_pct",
        "vllm:num_requests_swapped": "swapped_requests",
    }
    samples = {}
    for line in raw.splitlines():
        match = re.fullmatch(r'([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{.*\})?\s+(\S+)(?:\s+\S+)?', line.strip())
        if not match or match[1] not in metric_names:
            continue
        try:
            value = float(match[2])
        except ValueError:
            continue
        samples.setdefault(match[1], []).append(value)
    parsed = {}
    for name, key in metric_names.items():
        values = samples.get(name, [])
        if len(values) != 1 or key in parsed:
            continue
        value = values[0]
        if not math.isfinite(value) or value < 0:
            continue
        if key == "kv_cache_usage_pct":
            if value <= 1:
                parsed[key] = round(value * 100.0, 1)
        elif value.is_integer():
            parsed[key] = int(value)
    return model_status, parsed


def collect_litellm_status() -> str:
    try:
        status, _ = litellm_api_call("GET", "/health/readiness", timeout_sec=3, subprocess_timeout=5)
    except (OSError, http.client.HTTPException):
        return "unreachable"
    if status == 200:
        return "ready"
    if status in (401, 403):
        return "authentication-required"
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
    if active_model == "-" and any(
        isinstance(w["name"], str) and w["name"].startswith(CALIBRATION_MODEL + "-") for w in (workloads or [])
    ):
        active_model = CALIBRATION_MODEL
    model_status, metrics = collect_vllm_metrics(active_model)
    if model_status == "no-model" and (workloads is None or selected_model != "-"):
        model_status = "unavailable"
    if not serving_enabled and model_status != "ready":
        model_status = "stopped"
    gateway_status = collect_litellm_status()

    return {
        "serving_enabled": serving_enabled,
        "selected_model": public_model_name(selected_model),
        "active_model": public_model_name(active_model),
        "host_memory": host_mem,
        "gpus": gpus,
        "workloads": workloads,
        "metrics": metrics,
        "model_status": model_status,
        "gateway_status": gateway_status,
    }
