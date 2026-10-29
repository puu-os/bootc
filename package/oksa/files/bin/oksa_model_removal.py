# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

from dataclasses import dataclass
from enum import Enum, auto
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import oksa_model_serving as ms


class RemovalOutcome(Enum):
    PROTECTED = auto()
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

    def remove(self, model_name: str) -> RemovalResult:
        if not isinstance(model_name, str) or not model_name:
            return RemovalResult(RemovalOutcome.INVALID, "Model name must be a non-empty string")
        if ms.is_calibration_model(model_name):
            return RemovalResult(RemovalOutcome.PROTECTED, "The calibration model cannot be deleted")
        failure_outcome = RemovalOutcome.SHUTDOWN_FAILED
        deleted = False
        public_name = ""
        state_dir = self.state_dir
        try:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", model_name):
                try:
                    model_name = ms.resolve_model_name(model_name, state_dir)
                except ValueError as exc:
                    return RemovalResult(RemovalOutcome.INVALID, str(exc))
            if ms.is_calibration_model(model_name):
                return RemovalResult(RemovalOutcome.PROTECTED, "The calibration model cannot be deleted")
            if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", model_name)
                    or not (ms.MODELS_DIR / model_name).resolve().is_relative_to(ms.MODELS_DIR.resolve())):
                return RemovalResult(RemovalOutcome.INVALID, "Invalid model name")
            with (ms.MODELS_DIR.parent / f".{model_name}.preseed.lock").open("a") as model_lock:
                try:
                    fcntl.flock(model_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return RemovalResult(
                        RemovalOutcome.BUSY,
                        "Model is busy. Wait for its download or update to finish and retry deletion.",
                    )
                active_model_file = Path(os.environ.get("PUU_VLLM_ACTIVE_MODEL_FILE", str(state_dir / "active-model")))
                selected_model_file, serving_disabled_file = ms.control_files(state_dir)
                public_name = ms.public_model_name(model_name, state_dir)
                if ms.read_first_line(active_model_file) == model_name:
                    selected_model_file.unlink(missing_ok=True)
                    ms.atomic_write_text(serving_disabled_file, "")
                    subprocess.run([str(self.planner_bin), "--require-reconcile"], capture_output=True, text=True, check=True)
                elif ms.read_first_line(selected_model_file) in (model_name, public_name):
                    selected_model_file.unlink(missing_ok=True)

                state_dir.mkdir(parents=True, exist_ok=True)
                with (state_dir / "planner.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    with ms.preseed_publish_lock(ms.MODELS_DIR, exclusive=True):
                        if ms.read_first_line(active_model_file) == model_name:
                            raise ValueError("The model is still active or activation is pending; stop serving and retry deletion")
                        identity = ms.model_id(model_name)
                        manifest = Path(os.environ.get("PUU_K3S_MANIFEST_DIR", str(ms.CALIBRATED_MANIFEST_PATH.parent))) / "32-vllm-generated.yaml"
                        if manifest.exists():
                            manifest_text = manifest.read_text(encoding="utf-8")
                            try:
                                content = json.loads(manifest_text)
                            except json.JSONDecodeError:
                                if manifest_text.strip() == "apiVersion: v1\nkind: List\nitems: []":
                                    content = {"kind": "List", "items": []}
                                else:
                                    result = ms.run_kubectl(
                                        "create", "--dry-run=client", "--validate=false", "-f", str(manifest),
                                        "-o", "json", "--request-timeout=5s", timeout=10,
                                    )
                                    text = result.stdout.strip()
                                    if not text:
                                        raise ValueError("Empty generated workload response")
                                    decoder = json.JSONDecoder()
                                    objects, pos = [], 0
                                    while pos < len(text):
                                        item, pos = decoder.raw_decode(text, pos)
                                        objects.append(item)
                                        while pos < len(text) and text[pos].isspace():
                                            pos += 1
                                    content = objects[0] if len(objects) == 1 else {"kind": "List", "items": objects}
                            if not isinstance(content, dict):
                                raise ValueError("Invalid generated workload manifest")
                            items = content.get("items") if content.get("kind") == "List" else [content]
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
                        model_dir = ms.MODELS_DIR / model_name
                        if model_dir.exists():
                            shutil.rmtree(model_dir)
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
                subprocess.run([str(self.planner_bin), "--require-reconcile"], capture_output=True, text=True, check=True)
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
        return RemovalResult(RemovalOutcome.REMOVED, f"Model {public_name} deleted", public_name)
