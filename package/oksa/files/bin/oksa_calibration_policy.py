# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

from dataclasses import asdict, dataclass, fields
import math
import os
import re
from statistics import median


@dataclass(frozen=True)
class CalibrationPolicy:
    input_tokens: int = 1024
    output_tokens: int = 128
    ttft_ms: float = 2000.0
    tpot_ms: float = 50.0
    repetitions: int = 3

    def __post_init__(self):
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            minimum = 2 if name == "output_tokens" else 1
            if type(value) is not int or not minimum <= value <= 262144:
                raise ValueError(f"Invalid calibration policy {name}: expected {minimum}..262144 tokens")
        if self.input_tokens + self.output_tokens > 262144:
            raise ValueError("Calibration policy exceeds the 262144-token context limit")
        for name in ("ttft_ms", "tpot_ms"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not 0 < value <= 3600000 or not math.isfinite(value):
                raise ValueError(f"Invalid calibration policy {name}: expected finite milliseconds in (0, 3600000]")
        if type(self.repetitions) is not int or self.repetitions != 3:
            raise ValueError("Calibration policy requires exactly three repetitions")

    @property
    def minimum_context(self) -> int:
        return (self.input_tokens + self.output_tokens + 511) // 512 * 512

    @classmethod
    def from_environment(cls):
        values = {}
        for name in ("input_tokens", "output_tokens", "ttft_ms", "tpot_ms"):
            variable = f"PUU_CALIBRATION_{name.upper()}"
            if variable not in os.environ:
                continue
            raw = os.environ[variable]
            if not raw or len(raw) > 64:
                raise ValueError(f"Invalid {variable}")
            if name.endswith("tokens"):
                if not re.fullmatch(r"[0-9]+", raw):
                    raise ValueError(f"Invalid {variable}: expected an integer")
                values[name] = int(raw)
            else:
                if not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", raw):
                    raise ValueError(f"Invalid {variable}: expected finite milliseconds")
                values[name] = float(raw)
        return cls(**values)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
            raise ValueError("Invalid calibration policy fields")
        return cls(**value)

    def validate_metrics(self, metrics: dict, status: str) -> None:
        if not isinstance(metrics, dict):
            raise ValueError("Invalid probe metrics")
        estimated = metrics.get("estimated_max_model_len")
        if estimated is not None and (type(estimated) is not int or not 0 < estimated < 1e18):
            raise ValueError("Invalid probe estimated_max_model_len")
        if status not in ("passed", "latency", "capacity", "timeout"):
            raise ValueError("Invalid probe status")
        if status not in ("passed", "latency"):
            return
        if CalibrationPolicy.from_dict(metrics.get("policy")) != self:
            raise ValueError("Calibration probe returned a mismatched policy")
        for key in ("tokens_per_second", "peak_gpu_memory_bytes", "peak_host_memory_bytes",
                    "minimum_host_available_bytes", "kv_cache_tokens"):
            number = metrics.get(key)
            if type(number) not in (int, float) or not 0 < number < 1e18:
                raise ValueError(f"Invalid probe metric: {key}")
        cache = metrics.get("cache")
        if (not isinstance(cache, dict) or set(cache) != {"enabled", "corpus_requests", "residency_guaranteed"}
                or type(cache["enabled"]) is not bool
                or type(cache["corpus_requests"]) is not int or cache["corpus_requests"] != 32
                or cache["residency_guaranteed"] is not False):
            raise ValueError("Invalid probe cache metadata")
        throughput_samples = metrics.get("tokens_per_second_samples")
        if (not isinstance(throughput_samples, list) or len(throughput_samples) != self.repetitions
                or any(type(sample) not in (int, float) or not 0 < sample < 1e18 for sample in throughput_samples)):
            raise ValueError("Invalid probe throughput samples")
        compliant = True
        phase_goodputs = []
        for phase in ("cold", "warm"):
            measured = metrics.get(phase)
            if not isinstance(measured, dict):
                raise ValueError(f"Invalid probe {phase} metrics")
            for key in ("goodput", "tokens_per_second", "p99_ttft_ms", "p99_tpot_ms", "compliance"):
                number = measured.get(key)
                if type(number) not in (int, float) or not 0 <= number < 1e18:
                    raise ValueError(f"Invalid probe {phase} metric: {key}")
            if not 0 <= measured["compliance"] <= 1 or measured["tokens_per_second"] <= 0:
                raise ValueError(f"Invalid probe {phase} compliance or throughput")
            samples = measured.get("samples")
            if not isinstance(samples, list) or len(samples) != self.repetitions:
                raise ValueError(f"Invalid probe {phase} samples")
            goodputs, throughputs = [], []
            for sample in samples:
                if (not isinstance(sample, dict)
                        or set(sample) != {"duration_seconds", "completed", "good_completed", "output_tokens",
                                           "p99_ttft_ms", "p99_tpot_ms"}):
                    raise ValueError(f"Invalid probe {phase} sample fields")
                duration = sample["duration_seconds"]
                if type(duration) is not float or not math.isfinite(duration) or duration <= 0:
                    raise ValueError(f"Invalid probe {phase} sample duration")
                if (type(sample["completed"]) is not int or sample["completed"] != 32
                        or type(sample["good_completed"]) is not int or not 0 <= sample["good_completed"] <= 32
                        or type(sample["output_tokens"]) is not int or sample["output_tokens"] != 32 * self.output_tokens):
                    raise ValueError(f"Invalid probe {phase} sample counts")
                for key in ("p99_ttft_ms", "p99_tpot_ms"):
                    number = sample[key]
                    if type(number) not in (int, float) or not 0 <= number < 1e18:
                        raise ValueError(f"Invalid probe {phase} sample metric: {key}")
                goodput = sample["good_completed"] / duration
                throughput = sample["output_tokens"] / duration
                if not 0 <= goodput < 1e18 or not 0 < throughput < 1e18:
                    raise ValueError(f"Invalid probe {phase} sample rates")
                goodputs.append(goodput)
                throughputs.append(throughput)
                compliant &= (sample["good_completed"] == 32
                              and sample["p99_ttft_ms"] <= self.ttft_ms
                              and sample["p99_tpot_ms"] <= self.tpot_ms)
            expected_goodput = median(goodputs)
            expected_throughput = median(throughputs)
            for key, expected in (("goodput", expected_goodput), ("tokens_per_second", expected_throughput)):
                if not math.isclose(measured[key], expected, rel_tol=1e-9, abs_tol=1e-9):
                    raise ValueError(f"Inconsistent probe {phase} metric: {key}")
            for key in ("p99_ttft_ms", "p99_tpot_ms"):
                if measured[key] != max(sample[key] for sample in samples):
                    raise ValueError(f"Inconsistent probe {phase} metric: {key}")
            if measured["compliance"] != min(sample["good_completed"] / sample["completed"] for sample in samples):
                raise ValueError(f"Inconsistent probe {phase} compliance")
            phase_goodputs.append(expected_goodput)
            if phase == "cold":
                if (any(not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9)
                        for actual, expected in zip(throughput_samples, throughputs))
                        or not math.isclose(metrics["tokens_per_second"], expected_throughput, rel_tol=1e-9, abs_tol=1e-9)):
                    raise ValueError("Inconsistent probe throughput samples")
        goodput = metrics.get("goodput")
        if (type(goodput) not in (int, float) or not 0 <= goodput < 1e18
                or not math.isclose(goodput, min(phase_goodputs), rel_tol=1e-9, abs_tol=1e-9)):
            raise ValueError("Invalid probe conservative goodput")
        if status == "passed" and (not compliant or goodput <= 0):
            raise ValueError("Calibration probe passed without meeting the latency policy")
        if status == "latency" and compliant:
            raise ValueError("Calibration probe reported latency failure for a compliant profile")
