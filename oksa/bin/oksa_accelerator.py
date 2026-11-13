# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

from __future__ import annotations

import dataclasses
import enum
import math
import os
from pathlib import Path
import re
import subprocess
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oksa_model_serving import ModelSpec, NodeCapacity


def normalize_pci_id(pci_id: str) -> str:
    match = re.fullmatch(r"([0-9a-fA-F]+):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])", pci_id.strip())
    if not match:
        return pci_id.strip().lower()
    domain, bus, slot, func = match.groups()
    return f"{int(domain, 16):04x}:{bus.lower()}:{slot.lower()}.{func}"


class AcceleratorVendor(enum.Enum):
    NVIDIA = "nvidia"
    AMD = "amd"
    INTEL = "intel"
    UNKNOWN = "unknown"

    @classmethod
    def from_pci_vendor_id(cls, vendor_id: str) -> AcceleratorVendor:
        normalized = vendor_id.lower().strip()
        if normalized in ("0x10de", "10de"):
            return cls.NVIDIA
        if normalized in ("0x1002", "1002"):
            return cls.AMD
        if normalized in ("0x8086", "8086"):
            return cls.INTEL
        return cls.UNKNOWN


@dataclasses.dataclass(frozen=True)
class AcceleratorDevice:
    index: int
    vendor: AcceleratorVendor
    pci_id: str
    name: str = ""
    memory_total_bytes: int = 0
    driver: str = ""


@dataclasses.dataclass
class AcceleratorTelemetry:
    index: int | None
    name: str | None
    vendor: str
    memory_used_mib: int | float | None = None
    memory_total_mib: int | float | None = None
    gpu_util_pct: int | float | None = None
    temperature_c: int | float | None = None
    power_w: int | float | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


@dataclasses.dataclass(frozen=True)
class AcceleratorMemory:
    index: int
    free_bytes: int
    total_bytes: int

    @property
    def used_bytes(self) -> int:
        return self.total_bytes - self.free_bytes


@dataclasses.dataclass(frozen=True)
class AcceleratorRuntimeManifest:
    environment: dict[str, str]
    device_resources: dict[str, str]
    runtime_class_name: str
    volumes: tuple[dict[str, object], ...]
    volume_mounts: tuple[dict[str, object], ...]
    init_containers: tuple[dict[str, object], ...]


class AcceleratorDiscovery:
    DRM_CLASS_PATH = Path("/sys/class/drm")
    PCI_BUS_PATH = Path("/sys/bus/pci/devices")

    @staticmethod
    def _read_sysfs_int(path: Path) -> int:
        try:
            raw = path.read_text(encoding="ascii").strip()
            value = int(raw, 0)
            return value if value > 0 else 0
        except (OSError, ValueError):
            return 0

    @classmethod
    def _probe_pci_dir(
        cls,
        device_dir: Path,
        nvidia_info: dict[str, tuple[str, int]],
    ) -> tuple[AcceleratorVendor, str, str, int, str] | None:
        vendor_file = device_dir / "vendor"
        if not vendor_file.is_file():
            return None
        try:
            vendor_hex = vendor_file.read_text(encoding="ascii").strip()
            resolved_dir = device_dir.resolve()
        except OSError:
            return None

        vendor = AcceleratorVendor.from_pci_vendor_id(vendor_hex)
        if vendor == AcceleratorVendor.UNKNOWN:
            return None

        pci_id = normalize_pci_id(resolved_dir.name)
        driver_link = device_dir / "driver"
        try:
            driver_name = driver_link.resolve().name if driver_link.exists() else ""
        except OSError:
            driver_name = ""

        memory_bytes = 0
        for mem_name in ("mem_info_vram_total", "lmem_total_bytes"):
            memory_bytes = cls._read_sysfs_int(device_dir / mem_name)
            if memory_bytes > 0:
                break

        name = ""
        product_file = device_dir / "product_name"
        if product_file.is_file():
            try:
                name = product_file.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                name = ""

        if vendor == AcceleratorVendor.NVIDIA and pci_id in nvidia_info:
            nv_name, nv_memory_bytes = nvidia_info[pci_id]
            if nv_name:
                name = nv_name
            if nv_memory_bytes > 0:
                memory_bytes = nv_memory_bytes

        return vendor, pci_id, name, memory_bytes, driver_name

    @classmethod
    def scan_devices(cls) -> list[AcceleratorDevice]:
        devices: list[AcceleratorDevice] = []
        seen_pci: set[str] = set()
        nvidia_info = NvidiaBackend.query_pci_devices()

        if cls.DRM_CLASS_PATH.is_dir():
            for card_dir in sorted(cls.DRM_CLASS_PATH.glob("card[0-9]*")):
                if "-" in card_dir.name:
                    continue
                device_link = card_dir / "device"
                if not device_link.is_symlink() and not device_link.is_dir():
                    continue
                probed = cls._probe_pci_dir(device_link, nvidia_info)
                if probed is None or probed[1] in seen_pci:
                    continue
                vendor, pci_id, name, memory_bytes, driver_name = probed
                seen_pci.add(pci_id)
                devices.append(
                    AcceleratorDevice(
                        index=len(devices),
                        vendor=vendor,
                        pci_id=pci_id,
                        name=name,
                        memory_total_bytes=memory_bytes,
                        driver=driver_name,
                    )
                )

        if cls.PCI_BUS_PATH.is_dir():
            for pci_dir in sorted(cls.PCI_BUS_PATH.glob("*")):
                pci_id = normalize_pci_id(pci_dir.name)
                if pci_id in seen_pci:
                    continue
                class_file = pci_dir / "class"
                try:
                    pci_class = class_file.read_text(encoding="ascii").strip().lower()
                except OSError:
                    continue
                if not pci_class.startswith(("0x0300", "0x0302", "0x0380")):
                    continue
                probed = cls._probe_pci_dir(pci_dir, nvidia_info)
                if probed is None or probed[1] in seen_pci:
                    continue
                vendor, pci_id, name, memory_bytes, driver_name = probed
                seen_pci.add(pci_id)
                devices.append(
                    AcceleratorDevice(
                        index=len(devices),
                        vendor=vendor,
                        pci_id=pci_id,
                        name=name,
                        memory_total_bytes=memory_bytes,
                        driver=driver_name,
                    )
                )

        return devices
    @classmethod
    def has_accelerators(cls) -> bool:
        return len(cls.scan_devices()) > 0

    @staticmethod
    def _device_usable(dev: AcceleratorDevice) -> bool:
        if not dev.driver:
            return False
        if dev.vendor == AcceleratorVendor.NVIDIA:
            return bool(dev.name) or dev.memory_total_bytes > 0
        return True

    @classmethod
    def primary_vendor(cls, devices: list[AcceleratorDevice] | None = None) -> AcceleratorVendor:
        scanned = cls.scan_devices() if devices is None else devices
        groups: dict[AcceleratorVendor, list[AcceleratorDevice]] = {}
        for dev in scanned:
            if dev.vendor != AcceleratorVendor.UNKNOWN:
                groups.setdefault(dev.vendor, []).append(dev)
        if not groups:
            return AcceleratorVendor.UNKNOWN

        def rank(item: tuple[AcceleratorVendor, list[AcceleratorDevice]]) -> tuple[int, int, int, int]:
            _, group = item
            usable = int(any(cls._device_usable(d) for d in group))
            max_mem = max(d.memory_total_bytes for d in group)
            total_mem = sum(d.memory_total_bytes for d in group)
            return usable, max_mem, total_mem, len(group)

        return max(groups.items(), key=rank)[0]

    @classmethod
    def primary_devices(cls, devices: list[AcceleratorDevice] | None = None) -> list[AcceleratorDevice]:
        scanned = cls.scan_devices() if devices is None else devices
        vendor = cls.primary_vendor(scanned)
        if vendor == AcceleratorVendor.UNKNOWN:
            return []
        group = [dev for dev in scanned if dev.vendor == vendor]
        if any(dev.memory_total_bytes >= 4 * 1024**3 for dev in group):
            discrete = [dev for dev in group if dev.memory_total_bytes >= 2 * 1024**3]
            if discrete:
                group = discrete
        return group


class NvidiaBackend:
    SMI_PATH = Path("/usr/bin/nvidia-smi")
    VENDOR = AcceleratorVendor.NVIDIA
    REQUIRED_NODE_LABELS = ("cuda-ready", "cdi-ready")
    PREFERRED_NODE_LABELS = ((100, "nvlink-present"), (75, "fast-gpu-interconnect"))
    _MEMORY_ERRORS = re.compile(
        r"CUDA out of memory|torch(?:\.cuda)?\.OutOfMemoryError|OutOfMemoryError|"
        r"No available memory for the cache blocks|"
        r"max seq len[^\n]*larger than[^\n]*KV cache|"
        r"Free memory on device[^\n]*less than desired GPU memory utilization", re.IGNORECASE,
    )

    @staticmethod
    def is_memory_error(text: str) -> bool:
        return NvidiaBackend._MEMORY_ERRORS.search(text) is not None

    @classmethod
    def hardware_identity(cls) -> list[str]:
        result = subprocess.run(
            [str(cls.SMI_PATH), "--query-gpu=name,uuid,memory.total,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True, timeout=15,
        )
        gpus = sorted(line.strip() for line in result.stdout.splitlines() if line.strip())
        if not gpus:
            raise ValueError("NVIDIA hardware present but no usable GPU reported")
        identities = [line.rsplit(",", 3) for line in gpus]
        if any(len(parts) != 4 or any(not part.strip() or part.strip() in ("N/A", "[N/A]")
                                     for part in parts) for parts in identities):
            raise ValueError("Invalid NVIDIA GPU identity")
        if len({(parts[0].strip(), parts[2].strip()) for parts in identities}) > 1:
            raise ValueError("Calibration requires matching local GPUs")
        return gpus

    @staticmethod
    def query_memory(torch_module: ModuleType) -> tuple[AcceleratorMemory, ...]:
        if torch_module.version.hip is not None:
            raise ValueError("Unsupported accelerator runtime: HIP")
        if not torch_module.cuda.is_available():
            raise ValueError("NVIDIA CUDA runtime is not available")
        count = torch_module.cuda.device_count()
        if count <= 0:
            raise ValueError("No visible NVIDIA CUDA devices")
        devices: list[AcceleratorMemory] = []
        for index in range(count):
            free, total = torch_module.cuda.mem_get_info(index)
            if not 0 <= free <= total or total <= 0:
                raise ValueError(f"Invalid CUDA memory capacity for device {index}")
            devices.append(AcceleratorMemory(index, free, total))
        return tuple(devices)

    @staticmethod
    def readiness_error(node: NodeCapacity) -> str:
        if node.gpu_count <= 0 or not node.cuda_ready or not node.cdi_ready:
            return "CUDA/CDI GPU capacity is not ready"
        return ""

    @staticmethod
    def interconnect_selector(spec: ModelSpec, node: NodeCapacity) -> str:
        if spec.requires_nvlink:
            if not node.topology_parallel_capable:
                raise ValueError("NVLink topology is required but not available")
            return "topology"
        if node.topology_parallel_capable:
            return "topology"
        if (spec.requires_fast_nic or spec.allow_fast_nic_fallback) and node.fast_nic_present:
            return "fast-nic"
        raise ValueError("neither topology NVLink nor fast NIC is available for topology-parallel serving")

    @staticmethod
    def runtime_manifest(*, assigned_devices: str, resource_gpus: int) -> AcceleratorRuntimeManifest:
        if resource_gpus < 0:
            raise ValueError("GPU resource count cannot be negative")
        environment = {
            "NVIDIA_DRIVER_CAPABILITIES": "compute,utility",
            "FLASHINFER_WORKSPACE_BASE": "/var/lib/vllm/cache/flashinfer",
        }
        if assigned_devices:
            environment.update(NVIDIA_VISIBLE_DEVICES=assigned_devices, CUDA_VISIBLE_DEVICES=assigned_devices)
        mounts = ({"name": "nvidia-cdi", "mountPath": "/var/run/cdi", "readOnly": True},)
        return AcceleratorRuntimeManifest(
            environment=environment,
            device_resources={"nvidia.com/gpu": str(resource_gpus)} if resource_gpus else {},
            runtime_class_name="nvidia-cdi",
            volumes=({"name": "nvidia-cdi", "hostPath": {"path": "/var/run/cdi", "type": "DirectoryOrCreate"}},),
            volume_mounts=mounts,
            init_containers=({
                "name": "wait-for-nvidia-cdi",
                "image": "busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0",
                "imagePullPolicy": "IfNotPresent",
                "command": ["/bin/sh", "-ec", "until [ -s /var/run/cdi/nvidia.yaml ]; do sleep 10; done"],
                "volumeMounts": list(mounts),
            },),
        )

    @classmethod
    def is_available(cls) -> bool:
        return cls.SMI_PATH.is_file() and os_access_executable(cls.SMI_PATH)

    @classmethod
    def query_pci_devices(cls) -> dict[str, tuple[str, int]]:
        if not cls.is_available():
            return {}
        command = [
            str(cls.SMI_PATH),
            "--query-gpu=pci.bus_id,name,memory.total",
            "--format=csv,noheader,nounits",
        ]
        try:
            res = subprocess.run(command, capture_output=True, text=True, check=True, timeout=10)
        except (subprocess.SubprocessError, OSError):
            return {}
        info: dict[str, tuple[str, int]] = {}
        for line in res.stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 3 or not parts[0]:
                continue
            pci_id = normalize_pci_id(parts[0])
            raw_name = ", ".join(parts[1:-1]).strip()
            name = "" if raw_name in ("N/A", "[N/A]") else raw_name
            mem_raw = parts[-1]
            mem_bytes = int(mem_raw) * 1024 * 1024 if mem_raw.isdigit() else 0
            info[pci_id] = (name, mem_bytes)
        return info

    @classmethod
    def collect_telemetry(cls) -> list[dict] | None:
        if not cls.is_available():
            return None

        command = [
            str(cls.SMI_PATH),
            "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
            "--format=csv,noheader,nounits",
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=10)
        except (subprocess.SubprocessError, OSError):
            return None

        items: list[dict] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 7:
                continue

            raw_name = ", ".join(parts[1:-5]).strip()
            parsed_telemetry = AcceleratorTelemetry(
                index=parse_int_optional(parts[0]),
                name=None if not raw_name or raw_name in ("N/A", "[N/A]") else raw_name,
                vendor=AcceleratorVendor.NVIDIA.value,
                gpu_util_pct=parse_number_optional(parts[-5]),
                memory_used_mib=parse_number_optional(parts[-4]),
                memory_total_mib=parse_number_optional(parts[-3]),
                temperature_c=parse_number_optional(parts[-2]),
                power_w=parse_number_optional(parts[-1]),
            )
            items.append(parsed_telemetry.as_dict())

        return items if items else None

    @classmethod
    def query_gpu_names(cls) -> list[str]:
        if not cls.is_available():
            return []
        try:
            res = subprocess.run(
                [str(cls.SMI_PATH), "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            return [line.strip() for line in res.stdout.splitlines() if line.strip()]
        except (subprocess.SubprocessError, OSError):
            return []

    @classmethod
    def query_gpu_memory_mib(cls) -> list[int]:
        if not cls.is_available():
            return []
        try:
            res = subprocess.run(
                [str(cls.SMI_PATH), "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            values: list[int] = []
            for line in res.stdout.splitlines():
                stripped = line.strip()
                if stripped.isdigit():
                    values.append(int(stripped))
            return values
        except (subprocess.SubprocessError, OSError):
            return []


class AmdSysfsBackend:
    @classmethod
    def collect_telemetry(cls, devices: list[AcceleratorDevice]) -> list[dict] | None:
        items: list[dict] = []
        for idx, dev in enumerate(devices):
            pci_dir = AcceleratorDiscovery.PCI_BUS_PATH / dev.pci_id
            if not pci_dir.is_dir():
                continue
            used = AcceleratorDiscovery._read_sysfs_int(pci_dir / "mem_info_vram_used")
            total = dev.memory_total_bytes or AcceleratorDiscovery._read_sysfs_int(pci_dir / "mem_info_vram_total")
            util_raw = None
            try:
                util_raw = int((pci_dir / "gpu_busy_percent").read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                pass
            temp_c = None
            power_w = None
            for hwmon in sorted((pci_dir / "hwmon").glob("hwmon*")) if (pci_dir / "hwmon").is_dir() else ():
                if temp_c is None:
                    milli_c = AcceleratorDiscovery._read_sysfs_int(hwmon / "temp1_input")
                    if milli_c > 0:
                        temp_c = round(milli_c / 1000.0, 1)
                if power_w is None:
                    for pfile in ("power1_average", "power1_input"):
                        microwatt = AcceleratorDiscovery._read_sysfs_int(hwmon / pfile)
                        if microwatt > 0:
                            power_w = round(microwatt / 1_000_000.0, 1)
                            break
            telemetry = AcceleratorTelemetry(
                index=idx,
                name=dev.name or dev.driver or AcceleratorVendor.AMD.value,
                vendor=AcceleratorVendor.AMD.value,
                memory_used_mib=used // (1024 * 1024) if total > 0 else None,
                memory_total_mib=total // (1024 * 1024) if total > 0 else None,
                gpu_util_pct=util_raw,
                temperature_c=temp_c,
                power_w=power_w,
            )
            items.append(telemetry.as_dict())
        return items if items else None


class AcceleratorManager:
    @staticmethod
    def execution_backend(vendor: AcceleratorVendor | None = None) -> type[NvidiaBackend]:
        if vendor is None:
            vendor = AcceleratorDiscovery.primary_vendor()
        if vendor == NvidiaBackend.VENDOR:
            return NvidiaBackend
        raise ValueError(f"Unsupported accelerator vendor for serving/calibration: {vendor.value}")
    @classmethod
    def collect_telemetry(cls) -> list[dict] | None:
        devices = AcceleratorDiscovery.scan_devices()
        vendor = AcceleratorDiscovery.primary_vendor(devices)
        if vendor == AcceleratorVendor.NVIDIA:
            return NvidiaBackend.collect_telemetry()
        if vendor == AcceleratorVendor.AMD:
            return AmdSysfsBackend.collect_telemetry(AcceleratorDiscovery.primary_devices(devices))
        return None

    @classmethod
    def query_device_summary(cls) -> dict:
        devices = AcceleratorDiscovery.scan_devices()
        vendor = AcceleratorDiscovery.primary_vendor(devices)
        primary = AcceleratorDiscovery.primary_devices(devices)
        telemetry = cls.collect_telemetry() or []

        return {
            "vendor": vendor.value,
            "count": len(primary),
            "devices": [
                {
                    "index": dev.index,
                    "vendor": dev.vendor.value,
                    "pci_id": dev.pci_id,
                    "name": dev.name,
                    "memory_total_bytes": dev.memory_total_bytes,
                    "driver": dev.driver,
                }
                for dev in devices
            ],
            "telemetry": telemetry,
        }


def os_access_executable(path: Path) -> bool:
    return os.access(path, os.X_OK)


def parse_int_optional(val: str) -> int | None:
    try:
        return int(val.strip())
    except (ValueError, TypeError):
        return None


def parse_number_optional(val: str) -> int | float | None:
    try:
        parsed = float(val.strip())
    except (ValueError, TypeError):
        return None
    if not math.isfinite(parsed):
        return None
    return int(parsed) if parsed.is_integer() else round(parsed, 1)


def parse_int_safe(val: str, default: int) -> int:
    try:
        return int(float(val.strip()))
    except (ValueError, TypeError):
        return default
