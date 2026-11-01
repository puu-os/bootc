# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

import dataclasses
import json
import os
from pathlib import Path
import shlex

import oksa_model_serving as ms
from oksa_model_removal import wait_for_stopped_pods
from oksa_workload_evaluation import PlannedWorkload


@dataclasses.dataclass(frozen=True)
class WorkloadManifestBuilder:
    plan: PlannedWorkload
    models_dir: Path

    def _model_root(self) -> Path:
        cached_root = self.plan.spec.cached_model_root
        if cached_root is not None:
            return cached_root
        path = Path(self.plan.spec.path)
        if (not path.is_absolute() or ".." in path.parts
                or path.resolve() == self.models_dir.resolve()
                or not path.resolve().is_relative_to(self.models_dir.resolve())):
            raise ValueError("Model path must be under the models directory or a validated cached snapshot")
        return self.models_dir

    @staticmethod
    def manifest_path() -> Path:
        return Path(os.environ.get(
            "PUU_K3S_MANIFEST_DIR", "/var/lib/rancher/k3s/server/manifests"
        )) / "32-vllm-generated.yaml"

    @staticmethod
    def stop_workloads(keep_app: str = "") -> None:
        namespace = ms.label_namespace()
        result = ms.run_kubectl(
            "get", "deployments,statefulsets", "-n", ms.OKSA_NAMESPACE,
            "-o", "json", "--request-timeout=5s", timeout=10,
        )
        workloads = json.loads(result.stdout)
        if not isinstance(workloads, dict) or not isinstance(workloads.get("items"), list):
            raise ValueError("Invalid Kubernetes workload response during shutdown")
        names: list[str] = []
        selectors: list[str] = []
        for workload in workloads["items"]:
            metadata = workload["metadata"]
            template = workload["spec"]["template"]
            labels = template["metadata"].get("labels", {})
            if not (labels.get(f"{namespace}/model-capacity") == "gpu"
                    or labels.get(f"{namespace}/model-name")):
                continue
            if keep_app and labels.get("app.kubernetes.io/name") == keep_app:
                continue
            selector = workload["spec"]["selector"]
            match_labels = selector.get("matchLabels", {})
            if (not isinstance(match_labels, dict) or not match_labels or selector.get("matchExpressions")
                    or any(not isinstance(value, str) for value in match_labels.values())):
                raise ValueError("Cannot safely identify serving pods for shutdown")
            names.append(f"{workload['kind'].lower()}/{metadata['name']}")
            selectors.append(",".join(f"{key}={value}" for key, value in match_labels.items()))
        if names:
            ms.run_kubectl(
                "scale", "-n", ms.OKSA_NAMESPACE, *names, "--replicas=0",
                "--request-timeout=20s", timeout=30,
            )
            for selector in selectors:
                wait_for_stopped_pods(selector)
        remaining = f"{namespace}/model-capacity=gpu"
        if keep_app:
            remaining += f",app.kubernetes.io/name!={keep_app}"
        wait_for_stopped_pods(remaining)

    @staticmethod
    def _match_expr(key: str, value: str) -> dict[str, object]:
        return {"key": key, "operator": "In", "values": [value]}

    def _affinity(self) -> dict[str, object]:
        plan = self.plan
        namespace = ms.label_namespace()
        required_keys = [
            f"{namespace}/model-serving-ready", f"{namespace}/cuda-ready",
            f"{namespace}/cdi-ready", plan.model_node_label,
        ]
        if plan.interconnect_selector == "topology":
            required_keys.append(f"{namespace}/topology-parallel-capable")
        elif plan.interconnect_selector == "fast-nic":
            required_keys.append(f"{namespace}/fast-nic-present")
        preferences = [
            (100, f"{namespace}/nvlink-present"),
            (75, f"{namespace}/fast-gpu-interconnect"),
        ]
        if plan.spec.allow_fast_nic_fallback:
            preferences.append((25, f"{namespace}/fast-nic-present"))
        affinity: dict[str, object] = {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [{
                        "matchExpressions": [self._match_expr(key, "true") for key in required_keys],
                    }],
                },
                "preferredDuringSchedulingIgnoredDuringExecution": [
                    {"weight": weight, "preference": {
                        "matchExpressions": [self._match_expr(key, "true")],
                    }} for weight, key in preferences
                ],
            },
        }
        if plan.serving_mode == "replicated":
            affinity["podAntiAffinity"] = {
                "requiredDuringSchedulingIgnoredDuringExecution": [{
                    "labelSelector": {"matchExpressions": [
                        self._match_expr("app.kubernetes.io/name", plan.app_name),
                    ]},
                    "topologyKey": "kubernetes.io/hostname",
                }],
            }
        return affinity

    def container(self, *, prefix_caching: bool = True) -> dict[str, object]:
        plan = self.plan
        spec = plan.spec
        command = [*spec.runtime_command, "serve", spec.path,
                   "--host", "0.0.0.0", "--port", "8000",
                   "--served-model-name", spec.repo_id or spec.name,
                   "--max-model-len", str(spec.max_context),
                   "--max-num-seqs", str(spec.max_concurrent_sequences),
                   "--max-num-batched-tokens", str(plan.max_num_batched_tokens),
                   "--gpu-memory-utilization", plan.gpu_memory_utilization,
                   "--tensor-parallel-size", str(spec.tensor_parallel_size),
                   "--pipeline-parallel-size", str(spec.pipeline_parallel_size)]
        if spec.quantization:
            command.extend(("--quantization", spec.quantization))
        if spec.kv_cache_dtype:
            command.extend(("--kv-cache-dtype", spec.kv_cache_dtype))
        if spec.trust_remote_code:
            command.append("--trust-remote-code")
        command.extend(("--enable-prefix-caching" if prefix_caching else "--no-enable-prefix-caching",
                        "--enable-chunked-prefill"))
        extra_args = shlex.split(spec.extra_args)
        managed = {arg for arg in command if arg.startswith("--")} | {
            "--enable-prefix-caching", "--no-enable-prefix-caching",
            "--enable-chunked-prefill", "--no-enable-chunked-prefill",
        }
        if any(arg.split("=", 1)[0] in managed for arg in extra_args):
            raise ValueError("Extra arguments cannot override managed runtime options")
        command.extend(extra_args)
        env = {
            "NVIDIA_DRIVER_CAPABILITIES": "compute,utility",
            "HF_HOME": "/var/lib/huggingface",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "VLLM_CACHE_ROOT": "/var/lib/vllm/cache",
            "TORCH_EXTENSIONS_DIR": "/var/lib/vllm/cache/torch_extensions",
            "TRITON_CACHE_DIR": "/var/lib/vllm/cache/triton",
            "TORCHINDUCTOR_CACHE_DIR": "/var/lib/vllm/cache/torchinductor",
            "FLASHINFER_WORKSPACE_BASE": "/var/lib/vllm/cache/flashinfer",
        }
        if plan.serving_mode == "shared-gpu":
            env.update({"NVIDIA_VISIBLE_DEVICES": plan.assigned_gpu_devices,
                        "CUDA_VISIBLE_DEVICES": plan.assigned_gpu_devices})
        requests = {"cpu": "1", "memory": str(min(4 * 1024**3, plan.memory_bytes)) if plan.memory_bytes else "4Gi"}
        limits: dict[str, str] = {}
        if plan.memory_bytes:
            limits["memory"] = str(plan.memory_bytes)
        if plan.resource_gpus > 0:
            requests["nvidia.com/gpu"] = str(plan.resource_gpus)
            limits["nvidia.com/gpu"] = str(plan.resource_gpus)
        resources = {"requests": requests, "limits": limits}
        return {
            "name": "vllm", "image": spec.runtime_image, "imagePullPolicy": "IfNotPresent",
            "command": command,
            "env": [{"name": name, "value": value} for name, value in env.items()],
            "ports": [{"name": "http", "containerPort": 8000}],
            "readinessProbe": {"httpGet": {"path": "/health", "port": "http"},
                               "initialDelaySeconds": 10, "periodSeconds": 10},
            "resources": resources,
            "volumeMounts": [
                {"name": "models", "mountPath": str(self._model_root()), "readOnly": True},
                {"name": "huggingface", "mountPath": "/var/lib/huggingface"},
                {"name": "vllm-state", "mountPath": "/var/lib/vllm"},
                {"name": "vllm-runtime", "mountPath": "/usr/share/puu/vllm/runtime", "readOnly": True},
                {"name": "nvidia-cdi", "mountPath": "/var/run/cdi", "readOnly": True},
            ],
        }

    def build(self, *, prefix_caching: bool = True) -> dict[str, object]:
        plan = self.plan
        spec = plan.spec
        namespace = ms.label_namespace()
        labels = {"app.kubernetes.io/name": plan.app_name,
                  f"{namespace}/model-name": plan.model_label,
                  f"{namespace}/serving-mode": plan.serving_mode}
        selector = {"app.kubernetes.io/name": plan.app_name,
                    f"{namespace}/model-name": plan.model_label}
        service = {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": plan.app_name, "namespace": ms.OKSA_NAMESPACE, "labels": labels},
            "spec": {"selector": {**selector, f"{namespace}/inference": "enabled",
                                  f"{namespace}/model-server": "vllm"},
                     "ports": [{"name": "http", "port": 8000, "targetPort": "http"}]},
        }
        deployment = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": plan.app_name, "namespace": ms.OKSA_NAMESPACE, "labels": labels},
            "spec": {
                "replicas": plan.replicas, "selector": {"matchLabels": selector},
                "template": {
                    "metadata": {
                        "labels": {**labels, f"{namespace}/inference": "enabled",
                                   f"{namespace}/model-server": "vllm",
                                   f"{namespace}/model-capacity": "gpu",
                                   f"{namespace}/model-source": "local-discovery",
                                   f"{namespace}/cluster-routing": "enabled",
                                   f"{namespace}/sharing-policy": spec.sharing_policy},
                        "annotations": {
                            f"{namespace}/model-path": spec.path,
                            f"{namespace}/gpu-memory-budget-mib": str(spec.gpu_memory_budget_mib),
                            f"{namespace}/max-concurrent-sequences": str(spec.max_concurrent_sequences),
                            f"{namespace}/max-model-length": str(spec.max_context),
                            f"{namespace}/gpu-memory-utilization": plan.gpu_memory_utilization,
                            f"{namespace}/tensor-parallel-size": str(spec.tensor_parallel_size),
                            f"{namespace}/pipeline-parallel-size": str(spec.pipeline_parallel_size),
                            f"{namespace}/assigned-gpu-devices": plan.assigned_gpu_devices,
                        },
                    },
                    "spec": {
                        "affinity": self._affinity(), "runtimeClassName": "nvidia-cdi",
                        "initContainers": [{
                            "name": "wait-for-nvidia-cdi",
                            "image": "busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0",
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["/bin/sh", "-ec", "until [ -s /var/run/cdi/nvidia.yaml ]; do sleep 10; done"],
                            "volumeMounts": [{"name": "nvidia-cdi", "mountPath": "/var/run/cdi", "readOnly": True}],
                        }],
                        "containers": [self.container(prefix_caching=prefix_caching)],
                        "volumes": [
                            {"name": "models", "hostPath": {"path": str(self._model_root()), "type": "Directory"}},
                            {"name": "huggingface", "hostPath": {"path": "/var/lib/huggingface", "type": "DirectoryOrCreate"}},
                            {"name": "vllm-state", "hostPath": {"path": "/var/lib/vllm", "type": "DirectoryOrCreate"}},
                            {"name": "vllm-runtime", "hostPath": {"path": "/usr/share/puu/vllm/runtime", "type": "Directory"}},
                            {"name": "nvidia-cdi", "hostPath": {"path": "/var/run/cdi", "type": "DirectoryOrCreate"}},
                        ],
                    },
                },
            },
        }
        return {"apiVersion": "v1", "kind": "List", "items": [service, deployment]}

    def render(self) -> str:
        return json.dumps(self.build(), indent=2) + "\n"
