# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

from oksa_accelerator import AcceleratorManager, AcceleratorVendor

if TYPE_CHECKING:
    from oksa_model_serving import ModelSpec, NodeCapacity


from oksa_model_serving import label_namespace


@dataclasses.dataclass(frozen=True)
class CalibrationSnapshot:
    applied_max_sequences: int | None = None
    error: str = ""
    gpu_memory_utilization: float | None = None
    max_context: int | None = None
    max_sequences: int | None = None
    max_num_batched_tokens: int = 8192
    memory_bytes: int = 0

    @property
    def pending(self) -> bool:
        return self.applied_max_sequences is None

    @property
    def positive(self) -> bool:
        return self.applied_max_sequences is not None and self.applied_max_sequences > 0


@dataclasses.dataclass(frozen=True)
class ModelSnapshot:
    spec: ModelSpec
    path_exists: bool
    model_label: str
    public_name: str
    calibration: CalibrationSnapshot = dataclasses.field(default_factory=CalibrationSnapshot)
    path_error: OSError | ValueError | None = None


@dataclasses.dataclass(frozen=True)
class WorkloadSnapshot:
    models: tuple[ModelSnapshot, ...]
    node: NodeCapacity
    models_dir: Path


@dataclasses.dataclass(frozen=True)
class PlannedWorkload:
    spec: ModelSpec
    serving_mode: str
    gpu_memory_utilization: str
    resource_gpus: int
    replicas: int
    assigned_gpu_devices: str
    interconnect_selector: str
    shared_reserved_mib: int
    app_name: str
    model_label: str
    model_node_label: str
    accelerator_vendor: AcceleratorVendor
    max_num_batched_tokens: int = 8192
    memory_bytes: int = 0


@dataclasses.dataclass(frozen=True)
class WorkloadEvaluation:
    plan: PlannedWorkload | None = None
    reasons: tuple[str, ...] = ()

    @property
    def capable(self) -> bool:
        return self.plan is not None


class PlanError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class WorkloadEvaluator:
    snapshot: WorkloadSnapshot

    def evaluate(
        self, selected_model: str = "", disabled: bool = False, require_calibration: bool = True,
    ) -> WorkloadEvaluation:
        reasons = []
        if disabled:
            return WorkloadEvaluation()
        if selected_model:
            matching = [model for model in self.snapshot.models if model.spec.name == selected_model]
            if not matching:
                return WorkloadEvaluation(reasons=(f"selected model {selected_model} was not discovered",))
            candidates = matching[:1]
        else:
            candidates = sorted(
                (model for model in self.snapshot.models if model.spec.autostart),
                key=lambda model: -model.spec.autostart_priority,
            )
        for model in candidates:
            try:
                plan = self.plan_workload(model, manual_selection=bool(selected_model),
                                          require_calibration=require_calibration)
            except PlanError as err:
                reasons.append(f"skipping {model.spec.name}: {err}")
            else:
                return WorkloadEvaluation(plan=plan, reasons=tuple(reasons))
        return WorkloadEvaluation(reasons=tuple(reasons))

    @staticmethod
    def serving_mode_for_model(requested_mode: str, sharing_policy: str) -> str:
        if requested_mode == "shared-gpu":
            return "shared-gpu" if sharing_policy == "allow" else "exclusive-gpu"
        if requested_mode in ("exclusive-gpu", "replicated", "topology-parallel"):
            return requested_mode
        return "shared-gpu"

    @staticmethod
    def utilization_from_budget(budget_mib: int, gpu_memory_mib: int) -> str:
        if gpu_memory_mib <= 0:
            return "0.70"
        permille = (budget_mib * 1000) // gpu_memory_mib
        if permille < 50:
            permille = 50
        elif permille > 950:
            permille = 950
        return f"0.{permille:03d}"

    def plan_workload(
        self,
        model: ModelSnapshot,
        shared_reserved_mib: int = 0,
        manual_selection: bool = False,
        require_calibration: bool = True,
    ) -> PlannedWorkload:
        spec = dataclasses.replace(model.spec)
        node = self.snapshot.node
        models_dir = self.snapshot.models_dir
        if not manual_selection and not spec.autostart:
            raise PlanError("autostart is disabled")
        try:
            backend = AcceleratorManager.execution_backend()
        except ValueError as error:
            raise PlanError(str(error)) from error

        serving_mode = self.serving_mode_for_model(spec.serving_mode, spec.sharing_policy)
        model_label = model.model_label
        app_name = f"puu-vllm-{model_label}"
        model_node_label = f"{label_namespace()}/vllm-model-{model_label}"
        gpu_memory_utilization = f"{spec.gpu_memory_utilization:.2f}"
        calibration = model.calibration
        if require_calibration and not calibration.positive:
            raise PlanError(f"calibration required: {calibration.error or 'no positive measured limits'}")
        if require_calibration:
            if calibration.max_context is not None:
                spec = dataclasses.replace(spec, max_context=calibration.max_context)
            if calibration.max_sequences is not None:
                spec = dataclasses.replace(spec, max_concurrent_sequences=calibration.max_sequences)
        resource_gpus = 0
        replicas = 1
        assigned_gpu_devices = ""
        interconnect_selector = "none"

        try:
            cached_root = spec.cached_model_root
            local_path = Path(spec.path).resolve()
            local_root = models_dir.resolve()
        except (OSError, ValueError, RuntimeError) as err:
            raise PlanError(f"invalid model path {spec.path}: {err}") from err
        if cached_root is None and (local_path == local_root or not local_path.is_relative_to(local_root)):
            raise PlanError(f"model path {spec.path} is not under {models_dir} or a validated HF snapshot")
        if model.path_error is not None:
            raise model.path_error
        if not model.path_exists:
            raise PlanError(f"model path {spec.path} is missing")
        if spec.same_path_required and cached_root is None and spec.path != str(models_dir / spec.name):
            raise PlanError("model path must be identical on participants")
        if readiness_error := backend.readiness_error(node):
            raise PlanError(readiness_error)
        if spec.requires_unified_memory and not node.unified_memory:
            raise PlanError("unified memory is required but not available")
        if spec.min_gpu_memory_mib > 0 and node.gpu_memory_max_mib < spec.min_gpu_memory_mib:
            raise PlanError(f"max GPU memory {node.gpu_memory_max_mib} MiB is below required {spec.min_gpu_memory_mib} MiB")
        if spec.gpu_memory_budget_mib <= 0:
            raise PlanError("no positive GPU memory budget was discovered")
        if spec.estimated_memory_mib > 0 and spec.gpu_memory_budget_mib < spec.estimated_memory_mib:
            raise PlanError(f"GPU memory budget {spec.gpu_memory_budget_mib} MiB is below estimated model size {spec.estimated_memory_mib} MiB")

        if serving_mode == "shared-gpu":
            if node.single_gpu_capacity_mib <= 0:
                raise PlanError("shared GPU capacity is unknown")
            if spec.gpu_memory_budget_mib > node.single_gpu_capacity_mib:
                raise PlanError(f"shared GPU budget {spec.gpu_memory_budget_mib} MiB exceeds single-GPU limit {node.single_gpu_capacity_mib} MiB")
            reserved_after = shared_reserved_mib + spec.gpu_memory_budget_mib
            if reserved_after > node.single_gpu_capacity_mib:
                raise PlanError(f"shared GPU budget would reserve {reserved_after} MiB above {node.single_gpu_capacity_mib} MiB")
            shared_reserved_mib = reserved_after
            assigned_gpu_devices = "0"
            gpu_memory_utilization = self.utilization_from_budget(spec.gpu_memory_budget_mib, node.gpu_memory_max_mib)
        elif serving_mode in ("exclusive-gpu", "replicated"):
            resource_gpus = 1
            if serving_mode == "replicated":
                replicas = spec.max_replicas
            if spec.gpu_memory_budget_mib > node.single_gpu_capacity_mib and not node.unified_memory:
                mode_prefix = "exclusive" if serving_mode == "exclusive-gpu" else "replicated"
                raise PlanError(f"budget {spec.gpu_memory_budget_mib} MiB exceeds {mode_prefix} GPU guardrail {node.single_gpu_capacity_mib} MiB")
        elif serving_mode == "topology-parallel":
            parallel_gpus = spec.tensor_parallel_size * spec.pipeline_parallel_size
            if parallel_gpus <= 1:
                raise PlanError("topology-parallel requires more than one vLLM parallel GPU")
            if parallel_gpus < spec.topology_min_gpus:
                raise PlanError(f"vLLM parallel size {parallel_gpus} is below required {spec.topology_min_gpus} GPUs")
            if parallel_gpus > node.gpu_count:
                raise PlanError(f"vLLM parallel size {parallel_gpus} exceeds discovered GPU count {node.gpu_count}")
            parallel_capacity_mib = (node.gpu_memory_max_mib * parallel_gpus * 95) // 100
            if spec.gpu_memory_budget_mib > parallel_capacity_mib and not node.unified_memory:
                raise PlanError(f"budget {spec.gpu_memory_budget_mib} MiB exceeds topology guardrail {parallel_capacity_mib} MiB")
            try:
                interconnect_selector = backend.interconnect_selector(spec, node)
            except ValueError as error:
                raise PlanError(str(error)) from error
            resource_gpus = parallel_gpus

        if require_calibration and calibration.gpu_memory_utilization is not None:
            gpu_memory_utilization = f"{calibration.gpu_memory_utilization:.2f}"
        return PlannedWorkload(
            spec=spec,
            serving_mode=serving_mode,
            gpu_memory_utilization=gpu_memory_utilization,
            resource_gpus=resource_gpus,
            replicas=replicas,
            assigned_gpu_devices=assigned_gpu_devices,
            interconnect_selector=interconnect_selector,
            shared_reserved_mib=shared_reserved_mib,
            app_name=app_name,
            model_label=model_label,
            model_node_label=model_node_label,
            accelerator_vendor=backend.VENDOR,
            max_num_batched_tokens=calibration.max_num_batched_tokens if require_calibration else 8192,
            memory_bytes=calibration.memory_bytes if require_calibration else 0,
        )
