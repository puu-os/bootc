# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oksa_model_serving import ModelSpec, NodeCapacity


from oksa_model_serving import label_namespace


@dataclasses.dataclass(frozen=True)
class ModelSnapshot:
    spec: ModelSpec
    path_exists: bool
    model_label: str
    public_name: str
    path_error: OSError | ValueError | None = None


@dataclasses.dataclass(frozen=True)
class CalibrationSnapshot:
    applied_max_sequences: int | None = None
    replicas: int = 1
    error: str = ""
    gpu_memory_utilization: float | None = None
    max_context: int | None = None
    max_sequences: int | None = None

    @property
    def pending(self) -> bool:
        return self.applied_max_sequences is None

    @property
    def positive(self) -> bool:
        return self.applied_max_sequences is not None and self.applied_max_sequences > 0


@dataclasses.dataclass(frozen=True)
class WorkloadSnapshot:
    models: tuple[ModelSnapshot, ...]
    node: NodeCapacity
    models_dir: Path
    calibration_model: str
    calibration: CalibrationSnapshot


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


@dataclasses.dataclass(frozen=True)
class WorkloadEvaluation:
    plan: PlannedWorkload | None = None
    calibrated_active: bool = False
    reasons: tuple[str, ...] = ()

    @property
    def capable(self) -> bool:
        return self.plan is not None or self.calibrated_active


class PlanError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class WorkloadEvaluator:
    snapshot: WorkloadSnapshot

    def evaluate(self, selected_model: str = "", disabled: bool = False) -> WorkloadEvaluation:
        calibration = self.snapshot.calibration
        reasons = []
        if calibration.pending and selected_model == self.snapshot.calibration_model:
            reasons.append(f"cannot start calibrated Bonsai: {calibration.error}")
        if disabled:
            return WorkloadEvaluation(reasons=tuple(reasons))
        if selected_model == self.snapshot.calibration_model:
            if not calibration.positive:
                reasons.append("Bonsai requires a positive calibration applied during this boot")
            return WorkloadEvaluation(calibrated_active=calibration.positive, reasons=tuple(reasons))
        if not selected_model and (calibration.pending or calibration.positive):
            return WorkloadEvaluation(calibrated_active=calibration.positive and calibration.replicas > 0)
        if calibration.pending:
            return WorkloadEvaluation(reasons=("waiting for a valid calibration before switching workloads",))
        if selected_model:
            matching = [model for model in self.snapshot.models if model.spec.name == selected_model]
            if not matching:
                return WorkloadEvaluation(reasons=(f"selected model {selected_model} was not discovered",))
            candidates = matching[:1]
        else:
            candidates = sorted(
                (model for model in self.snapshot.models
                 if model.spec.name != self.snapshot.calibration_model and model.spec.autostart),
                key=lambda model: -model.spec.autostart_priority,
            )
        for model in candidates:
            try:
                plan = self.plan_workload(model, manual_selection=bool(selected_model))
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
    ) -> PlannedWorkload:
        spec = dataclasses.replace(model.spec)
        node = self.snapshot.node
        models_dir = self.snapshot.models_dir
        if not manual_selection and not spec.autostart:
            raise PlanError("autostart is disabled")

        serving_mode = self.serving_mode_for_model(spec.serving_mode, spec.sharing_policy)
        model_label = model.model_label
        app_name = f"puu-vllm-{model_label}"
        model_node_label = f"{label_namespace()}/vllm-model-{model_label}"
        gpu_memory_utilization = f"{spec.gpu_memory_utilization:.2f}"
        calibration = self.snapshot.calibration
        if calibration.gpu_memory_utilization is not None:
            gpu_memory_utilization = f"{calibration.gpu_memory_utilization:.2f}"
        if calibration.max_context is not None and calibration.max_context < spec.max_context:
            spec = dataclasses.replace(spec, max_context=calibration.max_context)
        if calibration.max_sequences is not None and calibration.max_sequences < spec.max_concurrent_sequences:
            spec = dataclasses.replace(spec, max_concurrent_sequences=calibration.max_sequences)
        resource_gpus = 0
        replicas = 1
        assigned_gpu_devices = ""
        interconnect_selector = "none"

        if not spec.path.startswith(f"{models_dir}/"):
            raise PlanError(f"model path {spec.path} is not under {models_dir}")
        if model.path_error is not None:
            raise model.path_error
        if not model.path_exists:
            raise PlanError(f"model path {spec.path} is missing")
        if spec.same_path_required and spec.path != str(models_dir / spec.name):
            raise PlanError("model path must be identical on participants")
        if node.gpu_count <= 0 or not node.cuda_ready or not node.cdi_ready:
            raise PlanError("CUDA/CDI GPU capacity is not ready")
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
            if spec.requires_nvlink:
                if not node.topology_parallel_capable:
                    raise PlanError("NVLink topology is required but not available")
                interconnect_selector = "topology"
            elif node.topology_parallel_capable:
                interconnect_selector = "topology"
            elif (spec.requires_fast_nic or spec.allow_fast_nic_fallback) and node.fast_nic_present:
                interconnect_selector = "fast-nic"
            else:
                raise PlanError("neither topology NVLink nor fast NIC is available for topology-parallel serving")
            resource_gpus = parallel_gpus

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
        )
