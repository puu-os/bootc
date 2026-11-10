# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum, auto
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

import oksa_model_serving as ms
from oksa_calibration_state import STATUS_PATH


def wait_for_stopped_pods(selector: str, *, kubeconfig: str | None = None) -> None:
    """Wait after scale-zero, pruning only terminal pods with the workload's labels."""
    if not selector:
        raise ValueError("A workload label selector is required for shutdown")
    deadline = time.monotonic() + 120
    kubeconfig = ms.get_kubeconfig() if kubeconfig is None else kubeconfig

    def run(*args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Pods did not stop within 120s: {selector}")
        return ms.run_kubectl(
            *args, f"--request-timeout={min(5, remaining)}s",
            timeout=min(10, remaining), kubeconfig=kubeconfig,
        )

    while True:
        result = run("get", "pod", "-n", ms.OKSA_NAMESPACE, "-l", selector, "-o", "json")
        pods = json.loads(result.stdout)
        if not isinstance(pods, dict) or not isinstance(pods.get("items"), list):
            raise ValueError("Invalid Kubernetes pod response during shutdown")
        if not pods["items"]:
            return
        # Recheck each phase server-side; never delete an active pod from a stale list.
        for phase in ("Failed", "Succeeded"):
            run(
                "delete", "pod", "-n", ms.OKSA_NAMESPACE, "-l", selector,
                f"--field-selector=status.phase={phase}", "--ignore-not-found", "--wait=false",
            )
        time.sleep(min(2, max(0, deadline - time.monotonic())))


class RemovalOutcome(Enum):
    INVALID = auto()
    BUSY = auto()
    NOT_STOPPED = auto()
    SHUTDOWN_FAILED = auto()
    REMOVAL_FAILED = auto()
    REMOVED = auto()
    RECONCILIATION_FAILED = auto()


@dataclass(frozen=True)
class RemovalResult:
    outcome: RemovalOutcome
    message: str
    public_name: str = ""

    @property
    def deleted(self) -> bool:
        return self.outcome in (RemovalOutcome.REMOVED, RemovalOutcome.RECONCILIATION_FAILED)

    @property
    def reconciled(self) -> bool:
        return self.outcome is RemovalOutcome.REMOVED


class ModelRemovalService:
    def __init__(self, state_dir: Path, discover_bin: Path, planner_bin: Path) -> None:
        self.state_dir = state_dir
        self.discover_bin = discover_bin
        self.planner_bin = planner_bin

    def remove(self, model_name: str, *, prevalidate: Callable[[str], None] | None = None) -> RemovalResult:
        if not isinstance(model_name, str) or not model_name:
            return RemovalResult(RemovalOutcome.INVALID, "Model name must be a non-empty string")
        failure_outcome = RemovalOutcome.SHUTDOWN_FAILED
        deleted = False
        public_name = ""
        cached_snapshot = False
        state_dir = self.state_dir
        try:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", model_name):
                try:
                    model_name = ms.resolve_model_name(model_name, state_dir)
                except ValueError as exc:
                    return RemovalResult(RemovalOutcome.INVALID, str(exc))
            if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", model_name)
                    or not (ms.MODELS_DIR / model_name).resolve().is_relative_to(ms.MODELS_DIR.resolve())):
                return RemovalResult(RemovalOutcome.INVALID, "Invalid model name")
            state_dir.mkdir(parents=True, exist_ok=True)
            STATUS_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            planner_env = dict(os.environ, PUU_PLANNER_LOCK_HELD="1")
            with (ms.MODELS_DIR.parent / f".{model_name}.preseed.lock").open("a") as model_lock, (state_dir / "planner.lock").open("a") as lock:
                try:
                    fcntl.flock(model_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return RemovalResult(
                        RemovalOutcome.BUSY,
                        "Model is busy. Wait for its download or update to finish and retry deletion.",
                    )
                fcntl.flock(lock, fcntl.LOCK_EX)
                if prevalidate is not None:
                    with ms.preseed_publish_lock(ms.MODELS_DIR, exclusive=False):
                        prevalidate(model_name)
                active_model_file = Path(os.environ.get("PUU_VLLM_ACTIVE_MODEL_FILE", str(state_dir / "active-model")))
                selected_model_file, serving_disabled_file = ms.control_files(state_dir)
                public_name = ms.public_model_name(model_name, state_dir)
                if ms.read_first_line(active_model_file) == model_name:
                    selected_model_file.unlink(missing_ok=True)
                    ms.atomic_write_text(serving_disabled_file, "")
                    subprocess.run([str(self.planner_bin), "--require-reconcile"], env=planner_env, capture_output=True, text=True, check=True)
                elif ms.read_first_line(selected_model_file) in (model_name, public_name):
                    selected_model_file.unlink(missing_ok=True)

                with (STATUS_PATH.parent / "lock").open("a") as gpu_lock:
                    fcntl.flock(gpu_lock, fcntl.LOCK_EX)
                    with ms.preseed_publish_lock(ms.MODELS_DIR, exclusive=True):
                        if ms.read_first_line(active_model_file) == model_name:
                            raise ValueError("The model is still active or activation is pending; stop serving and retry deletion")
                        identity = ms.model_id(model_name)
                        manifest = Path(os.environ.get("PUU_K3S_MANIFEST_DIR", "/var/lib/rancher/k3s/server/manifests")) / "32-vllm-generated.yaml"
                        if manifest.exists():
                            content = json.loads(manifest.read_text(encoding="utf-8"))
                            if not isinstance(content, dict) or content.get("kind") != "List":
                                raise ValueError("Invalid generated workload manifest")
                            items = content.get("items")
                            if not isinstance(items, list):
                                raise ValueError("Invalid generated workload manifest items")
                            for item in items:
                                if not isinstance(item, dict) or item.get("kind") not in ("Service", "Deployment"):
                                    raise ValueError("Unexpected generated workload manifest entry")
                                if item["kind"] != "Deployment":
                                    continue
                                metadata, spec = item.get("metadata"), item.get("spec")
                                if not isinstance(metadata, dict) or not isinstance(spec, dict) or not isinstance(metadata.get("labels", {}), dict):
                                    raise ValueError("Invalid generated deployment manifest")
                                if metadata.get("name") == f"puu-vllm-{identity}" or metadata.get("labels", {}).get(f"{ms.label_namespace()}/model-name") == identity:
                                    if type(spec.get("replicas", 1)) is not int or spec.get("replicas", 1) != 0:
                                        raise ValueError("A pending manifest can start this model; stop serving and retry deletion")
                        result = ms.run_kubectl(
                            "get", "deployment,replicaset,pod", "-n", ms.OKSA_NAMESPACE,
                            "-l", f"{ms.label_namespace()}/model-name={identity}",
                            "-o", "json", "--request-timeout=5s", timeout=10,
                        )
                        workloads = json.loads(result.stdout)
                        if not isinstance(workloads, dict) or not isinstance(workloads.get("items"), list):
                            raise ValueError("Invalid Kubernetes workload response")
                        for workload in workloads["items"]:
                            if not isinstance(workload, dict) or workload.get("kind") not in ("Deployment", "ReplicaSet", "Pod"):
                                raise ValueError("Invalid Kubernetes workload entry")
                            stopped = False
                            if workload["kind"] == "Pod":
                                status = workload.get("status", {})
                                if not isinstance(status, dict):
                                    raise ValueError("Invalid Kubernetes pod status")
                                stopped = status.get("phase") in ("Failed", "Succeeded")
                            if workload["kind"] != "Pod":
                                spec, status, metadata = (workload.get(field, {}) for field in ("spec", "status", "metadata"))
                                if not all(isinstance(field, dict) for field in (spec, status, metadata)):
                                    raise ValueError("Invalid Kubernetes controller status")
                                desired, remaining = spec.get("replicas", 1), status.get("replicas", 0)
                                generation, observed = metadata.get("generation"), status.get("observedGeneration")
                                stopped = (
                                    type(desired) is int and desired == 0 and type(remaining) is int and remaining == 0
                                    and type(generation) is int and generation > 0
                                    and type(observed) is int and observed == generation
                                )
                            if not stopped:
                                return RemovalResult(
                                    RemovalOutcome.NOT_STOPPED,
                                    "Model not deleted: shutdown is not confirmed. Stop or remove remaining workloads, then retry deletion.",
                                    public_name,
                                )
                        failure_outcome = RemovalOutcome.REMOVAL_FAILED
                        entries = [entry for entry in ms.read_models_list(state_dir / "models.list") if entry[0] == model_name]
                        if len(entries) != 1:
                            raise ValueError("Model registration is missing or ambiguous")
                        _, model_path, metadata_path = entries[0]
                        spec = ms.ModelSpec.from_env(ms.ModelSpec.read_metadata(Path(metadata_path)),
                                                     name=model_name, path=model_path)
                        model_dir = ms.MODELS_DIR / model_name
                        if spec.cached_model_root is not None:
                            exclusion = state_dir / "cache-exclusions" / hashlib.sha256(spec.path.encode()).hexdigest()
                            ms.atomic_write_text(exclusion, spec.path + "\n")
                            shutil.rmtree(Path(spec.path))
                            cached_snapshot = True
                        elif Path(spec.path).resolve() == model_dir.resolve() and model_dir.is_dir():
                            if spec.repo_id:
                                snapshots = ms.ModelSpec.hf_cache_dir() / ("models--" + spec.repo_id.replace("/", "--")) / "snapshots"
                                for snapshot in snapshots.glob("*"):
                                    if not re.fullmatch(r"[0-9a-f]{40}", snapshot.name):
                                        continue
                                    cached = replace(spec, path=str(snapshot), revision=snapshot.name)
                                    if cached.cached_model_root is not None:
                                        exclusion = state_dir / "cache-exclusions" / hashlib.sha256(cached.path.encode()).hexdigest()
                                        ms.atomic_write_text(exclusion, cached.path + "\n")
                            shutil.rmtree(model_dir)
                        else:
                            raise ValueError("Model storage is outside the managed model directories")
                        (state_dir / "models.d" / f"{model_name}.env").unlink(missing_ok=True)
                        deleted = True

                    subprocess.run([str(self.discover_bin)], capture_output=True, text=True, check=True)
                    catalog = (state_dir / "models.list").read_text(encoding="utf-8")
                    metadata = json.loads((state_dir / "models.json").read_text(encoding="utf-8"))
                    if not isinstance(metadata, list) or not all(isinstance(item, dict) for item in metadata):
                        raise ValueError("Invalid refreshed model catalog")
                    if (any(line.split("\t", 1)[0].strip() == model_name for line in catalog.splitlines())
                            or any(item.get("name") == model_name for item in metadata)
                            or ms.parse_env_file(state_dir / "default-model.env").get("PUU_VLLM_MODEL_NAME") == model_name):
                        raise ValueError("Deleted model remains in the catalog; discovery did not finish")
                subprocess.run([str(self.planner_bin), "--require-reconcile"], env=planner_env, capture_output=True, text=True, check=True)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
            if deleted:
                return RemovalResult(
                    RemovalOutcome.RECONCILIATION_FAILED,
                    f"Model {public_name} was deleted, but catalog/workload reconciliation failed: {detail}. "
                    "The model list may be out of date. Resolve the service error and refresh.",
                    public_name,
                )
            message = (
                "Shutdown could not be confirmed; model files and registration were preserved"
                if failure_outcome is RemovalOutcome.SHUTDOWN_FAILED
                else "Model removal failed and may be partial; check model storage before retrying"
            )
            return RemovalResult(failure_outcome, f"{message}: {detail}", public_name)
        detail = " (cached snapshot removed; shared Hugging Face blobs preserved)" if cached_snapshot else ""
        return RemovalResult(RemovalOutcome.REMOVED, f"Model {public_name} deleted{detail}", public_name)
