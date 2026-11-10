# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

from collections.abc import Callable
import json
import math
from pathlib import Path
from statistics import median
import subprocess
import tempfile
import time

from oksa_calibration_policy import CalibrationPolicy


class VllmBenchmark:
    RESULT_LIMIT = 2 * 1024 * 1024
    LOG_LIMIT = 16 * 1024 * 1024

    def __init__(self, policy: CalibrationPolicy, model: str, tokenizer: str, sequences: int,
                 monitor: Callable, stop_process: Callable, reset_cache: Callable,
                 *, prefix_caching: bool, trust_remote_code: bool) -> None:
        self.policy = policy
        self.model = model
        self.tokenizer = tokenizer
        self.sequences = sequences
        self.monitor = monitor
        self.stop_process = stop_process
        self.reset_cache = reset_cache
        self.prefix_caching = prefix_caching
        self.trust_remote_code = trust_remote_code

    @staticmethod
    def _number(value, *, positive: bool = False) -> bool:
        return (type(value) in (int, float) and math.isfinite(value)
                and (0 < value < 1e18 if positive else 0 <= value < 1e18))

    def _round(self, directory: Path, *, input_tokens: int, output_tokens: int,
               requests: int, seed: int) -> dict:
        result_path = directory / "result.json"
        result_path.unlink(missing_ok=True)
        command = [
            "vllm", "bench", "serve", "--backend", "vllm",
            "--base-url", "http://127.0.0.1:8000", "--endpoint", "/v1/completions",
            "--model", self.model, "--tokenizer", self.tokenizer,
            "--dataset-name", "random", "--random-input-len", str(input_tokens),
            "--random-output-len", str(output_tokens), "--random-range-ratio", "0",
            "--num-prompts", str(requests), "--max-concurrency", str(self.sequences),
            "--request-rate", "inf", "--ignore-eos", "--seed", str(seed),
            "--goodput", f"ttft:{self.policy.ttft_ms}", f"tpot:{self.policy.tpot_ms}",
            "--percentile-metrics", "ttft,tpot", "--metric-percentiles", "99",
            "--ready-check-timeout-sec", "0", "--num-warmups", "0",
            "--save-result", "--save-detailed", "--result-dir", str(directory),
            "--result-filename", result_path.name, "--disable-tqdm",
        ]
        if self.trust_remote_code:
            command.append("--trust-remote-code")
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                while True:
                    self.monitor()
                    if output.tell() > self.LOG_LIMIT:
                        raise RuntimeError("vLLM benchmark output exceeded 16 MiB")
                    if result_path.exists() and result_path.stat().st_size > self.RESULT_LIMIT:
                        raise RuntimeError("vLLM benchmark result exceeded 2 MiB")
                    if process.poll() is not None:
                        break
                    time.sleep(0.2)
                if process.returncode:
                    output.seek(max(0, output.tell() - 2048))
                    detail = output.read(2048).decode(errors="replace")
                    raise RuntimeError(f"vLLM benchmark failed (exit {process.returncode}): {detail}")
            finally:
                self.stop_process(process)
        with result_path.open("rb") as stream:
            raw = stream.read(self.RESULT_LIMIT + 1)
        if len(raw) > self.RESULT_LIMIT:
            raise ValueError("vLLM benchmark result exceeded 2 MiB")
        value = json.loads(raw)
        if not isinstance(value, dict) or type(value.get("completed")) is not int or value["completed"] != requests:
            raise ValueError("vLLM benchmark did not complete every request")
        for key in ("duration", "request_throughput", "output_throughput"):
            if not self._number(value.get(key), positive=True):
                raise ValueError(f"Invalid vLLM benchmark metric: {key}")
        for key in ("request_goodput", "p99_ttft_ms", "p99_tpot_ms"):
            if not self._number(value.get(key)):
                raise ValueError(f"Invalid vLLM benchmark metric: {key}")
        for key, expected in (("input_lens", input_tokens), ("output_lens", output_tokens)):
            lengths = value.get(key)
            if (not isinstance(lengths, list) or len(lengths) != requests
                    or any(type(length) is not int or length != expected for length in lengths)):
                raise ValueError(f"vLLM benchmark returned mismatched {key}")
        duration = value["duration"]
        good_completed = round(value["request_goodput"] * duration)
        if (not 0 <= good_completed <= requests
                or not math.isclose(value["request_goodput"], good_completed / duration, rel_tol=1e-9, abs_tol=1e-9)
                or not math.isclose(value["request_throughput"], requests / duration, rel_tol=1e-9)
                or not math.isclose(value["output_throughput"], requests * output_tokens / duration, rel_tol=1e-9)
                or type(value.get("total_output_tokens")) is not int
                or value["total_output_tokens"] != requests * output_tokens):
            raise ValueError("Inconsistent vLLM benchmark request, token or goodput counts")
        ttfts, itls = value.get("ttfts"), value.get("itls")
        if (not isinstance(ttfts, list) or len(ttfts) != requests
                or any(not self._number(latency) for latency in ttfts)
                or not isinstance(itls, list) or len(itls) != requests
                or any(not isinstance(intervals, list) or any(not self._number(interval) for interval in intervals)
                       for intervals in itls)):
            raise ValueError("Invalid vLLM benchmark per-request latency measurements")
        if good_completed == requests and any(
                ttft * 1000 > self.policy.ttft_ms
                or sum(intervals) * 1000 / (output_tokens - 1) > self.policy.tpot_ms + 0.001
                for ttft, intervals in zip(ttfts, itls)):
            raise ValueError("vLLM goodput contradicts per-request latency measurements")
        evidence = dict(duration_seconds=duration, completed=requests, good_completed=good_completed,
                        output_tokens=requests * output_tokens,
                        p99_ttft_ms=value["p99_ttft_ms"], p99_tpot_ms=value["p99_tpot_ms"])
        return dict(goodput=good_completed / duration, tokens_per_second=requests * output_tokens / duration,
                    p99_ttft_ms=value["p99_ttft_ms"], p99_tpot_ms=value["p99_tpot_ms"],
                    compliance=good_completed / requests, evidence=evidence)

    @staticmethod
    def _aggregate(rounds: list[dict]) -> dict:
        return dict(goodput=median(value["goodput"] for value in rounds),
                    tokens_per_second=median(value["tokens_per_second"] for value in rounds),
                    p99_ttft_ms=max(value["p99_ttft_ms"] for value in rounds),
                    p99_tpot_ms=max(value["p99_tpot_ms"] for value in rounds),
                    compliance=min(value["compliance"] for value in rounds),
                    samples=[value["evidence"] for value in rounds])

    def measure(self, context: int) -> dict:
        cold, warm = [], []
        with tempfile.TemporaryDirectory(prefix="puu-calibration-bench-") as temporary:
            directory = Path(temporary)
            if self.prefix_caching:
                self.reset_cache()
            self._round(directory, input_tokens=context - self.policy.output_tokens,
                        output_tokens=self.policy.output_tokens, requests=self.sequences, seed=1728)
            self._round(directory, input_tokens=self.policy.input_tokens, output_tokens=self.policy.output_tokens,
                        requests=32, seed=1727)
            for index in range(self.policy.repetitions):
                if self.prefix_caching:
                    self.reset_cache()
                arguments = dict(input_tokens=self.policy.input_tokens, output_tokens=self.policy.output_tokens,
                                 requests=32, seed=1729 + index)
                cold.append(self._round(directory, **arguments))
                warm.append(self._round(directory, **arguments))
        cold_metrics, warm_metrics = self._aggregate(cold), self._aggregate(warm)
        return dict(cold=cold_metrics, warm=warm_metrics,
                    goodput=min(cold_metrics["goodput"], warm_metrics["goodput"]),
                    tokens_per_second=cold_metrics["tokens_per_second"],
                    tokens_per_second_samples=[value["tokens_per_second"] for value in cold],
                    cache=dict(enabled=self.prefix_caching, corpus_requests=32, residency_guaranteed=False),
                    policy=self.policy.to_dict())

    def within_budget(self, metrics: dict) -> bool:
        return all(value["p99_ttft_ms"] <= self.policy.ttft_ms
                   and value["p99_tpot_ms"] <= self.policy.tpot_ms
                   and value["compliance"] >= 1 - 1e-9
                   for value in (metrics["cold"], metrics["warm"]))
