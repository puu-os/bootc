# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

from __future__ import annotations

import dataclasses
import enum
import json
from pathlib import Path
import re
import subprocess


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
    index: int
    name: str
    vendor: str
    memory_used_mib: int = 0
    memory_total_mib: int = 0
    gpu_util_pct: int = 0
    temperature_c: int = 0
    power_w: int = 0

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


class AcceleratorDiscovery:
    DRM_CLASS_PATH = Path("/sys/class/drm")
    PCI_BUS_PATH = Path("/sys/bus/pci/devices")

    @classmethod
    def scan_devices(cls) -> list[AcceleratorDevice]:
        devices: list[AcceleratorDevice] = []
        seen_pci: set[str] = set()

        if cls.DRM_CLASS_PATH.is_dir():
            card_dirs = sorted(cls.DRM_CLASS_PATH.glob("card[0-9]*"))
            for card_dir in card_dirs:
                if "-" in card_dir.name:
                    continue
                device_link = card_dir / "device"
                if not device_link.is_symlink() and not device_link.is_dir():
                    continue

                pci_id = device_link.resolve().name
                if pci_id in seen_pci:
                    continue

                vendor_file = device_link / "vendor"
                if not vendor_file.is_file():
                    continue

                try:
                    vendor_hex = vendor_file.read_text(encoding="ascii").strip()
                except OSError:
                    continue

                vendor = AcceleratorVendor.from_pci_vendor_id(vendor_hex)
                if vendor == AcceleratorVendor.UNKNOWN:
                    continue

                driver_link = device_link / "driver"
                driver_name = driver_link.resolve().name if driver_link.exists() else ""

                seen_pci.add(pci_id)
                devices.append(
                    AcceleratorDevice(
                        index=len(devices),
                        vendor=vendor,
                        pci_id=pci_id,
                        driver=driver_name,
                    )
                )

        if not devices:
            devices = cls._fallback_device_scan()

        return devices

    @classmethod
    def _fallback_device_scan(cls) -> list[AcceleratorDevice]:
        devices: list[AcceleratorDevice] = []
        nvidia_devs = list(Path("/dev").glob("nvidia[0-9]*"))
        if nvidia_devs:
            for idx, dev in enumerate(nvidia_devs):
                devices.append(
                    AcceleratorDevice(
                        index=idx,
                        vendor=AcceleratorVendor.NVIDIA,
                        pci_id=f"nvidia-{idx}",
                        driver="nvidia",
                    )
                )
        elif Path("/dev/kfd").exists():
            devices.append(
                AcceleratorDevice(
                    index=0,
                    vendor=AcceleratorVendor.AMD,
                    pci_id="kfd-0",
                    driver="amdgpu",
                )
            )
        elif list(Path("/dev/dri").glob("renderD*")):
            devices.append(
                AcceleratorDevice(
                    index=0,
                    vendor=AcceleratorVendor.UNKNOWN,
                    pci_id="renderD-0",
                    driver="drm",
                )
            )
        return devices

    @classmethod
    def has_accelerators(cls) -> bool:
        return len(cls.scan_devices()) > 0

    @classmethod
    def primary_vendor(cls) -> AcceleratorVendor:
        devices = cls.scan_devices()
        if not devices:
            return AcceleratorVendor.UNKNOWN
        return devices[0].vendor


class NvidiaBackend:
    SMI_PATH = Path("/usr/bin/nvidia-smi")

    @classmethod
    def is_available(cls) -> bool:
        return cls.SMI_PATH.is_file() and os_access_executable(cls.SMI_PATH)

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

            parsed_telemetry = AcceleratorTelemetry(
                index=parse_int_safe(parts[0], 0),
                name=parts[1],
                vendor=AcceleratorVendor.NVIDIA.value,
                gpu_util_pct=parse_int_safe(parts[2], 0),
                memory_used_mib=parse_int_safe(parts[3], 0),
                memory_total_mib=parse_int_safe(parts[4], 0),
                temperature_c=parse_int_safe(parts[5], 0),
                power_w=parse_int_safe(parts[6], 0),
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


class AcceleratorManager:
    @classmethod
    def collect_telemetry(cls) -> list[dict] | None:
        vendor = AcceleratorDiscovery.primary_vendor()
        if vendor == AcceleratorVendor.NVIDIA:
            return NvidiaBackend.collect_telemetry()
        return None

    @classmethod
    def query_device_summary(cls) -> dict:
        devices = AcceleratorDiscovery.scan_devices()
        vendor = AcceleratorDiscovery.primary_vendor()
        telemetry = cls.collect_telemetry() or []

        return {
            "vendor": vendor.value,
            "count": len(devices),
            "devices": [
                {
                    "index": dev.index,
                    "vendor": dev.vendor.value,
                    "pci_id": dev.pci_id,
                    "driver": dev.driver,
                }
                for dev in devices
            ],
            "telemetry": telemetry,
        }


def os_access_executable(path: Path) -> bool:
    import os

    return os.access(path, os.X_OK)


def parse_int_safe(val: str, default: int) -> int:
    try:
        return int(float(val.strip()))
    except (ValueError, TypeError):
        return default
