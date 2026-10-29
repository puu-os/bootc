# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import math
import threading
import time
from collections.abc import Callable, Iterator


@dataclass(frozen=True)
class MonitoringSnapshot:
    """A point-in-time view with a caller-owned status payload."""

    status: dict | None
    sampled_at: str | None
    age_seconds: float | None
    stale: bool
    error: str | None
    max_age_seconds: float

    def to_json(self) -> dict:
        return {
            "ok": True,
            "status": deepcopy(self.status),
            "snapshot": {
                "sampled_at": self.sampled_at,
                "age_seconds": self.age_seconds,
                "stale": self.stale,
                "error": self.error,
                "max_age_seconds": self.max_age_seconds,
            },
        }


class StatusSampler:
    def __init__(self, collector: Callable[[], dict], interval: float = 2.5,
                 max_age: float = 10.0) -> None:
        if not callable(collector):
            raise TypeError("collector must be callable")
        try:
            interval = float(interval)
            max_age = float(max_age)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("interval and max_age must be positive finite numbers") from error
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("interval must be positive and finite")
        if not math.isfinite(max_age) or max_age <= 0:
            raise ValueError("max_age must be positive and finite")
        self._collector = collector
        self._interval = interval
        self._max_age = max_age
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._generation = 0
        self._published_generation: int | None = None
        self._mutations = 0
        self._pending = True
        self._status: dict | None = None
        self._sampled_at: str | None = None
        self._sample_started: float | None = None
        self._error: str | None = None

    def start(self) -> None:
        """Start once; a closed sampler cannot be restarted."""
        with self._condition:
            if self._closed or self._thread is not None:
                return
            try:
                thread = threading.Thread(target=self._run, name="oksa-monitoring", daemon=True)
                self._thread = thread
                thread.start()
            except Exception as error:
                self._thread = None
                self._error = f"{type(error).__name__}: {error}"
                logging.getLogger(__name__).exception("Unable to start monitoring sampler")
                raise

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
            if thread.is_alive():
                logging.getLogger(__name__).warning("Monitoring probe outlived sampler shutdown")

    def snapshot(self) -> MonitoringSnapshot:
        with self._condition:
            status = self._status
            sampled_at = self._sampled_at
            age = None if self._sample_started is None else time.monotonic() - self._sample_started
            stale = (self._closed or self._mutations > 0 or status is None
                     or self._published_generation != self._generation
                     or self._error is not None or age is None or age > self._max_age)
            error = self._error
        return MonitoringSnapshot(deepcopy(status), sampled_at, age, stale, error, self._max_age)

    def invalidate(self) -> None:
        with self._condition:
            self._generation += 1
            self._pending = True
            self._condition.notify_all()

    @contextmanager
    def changing(self) -> Iterator[None]:
        with self._condition:
            self._mutations += 1
            self.invalidate()
        try:
            yield
        finally:
            with self._condition:
                self._mutations -= 1
                self.invalidate()

    def _run(self) -> None:
        next_sweep = 0.0
        while True:
            with self._condition:
                while not self._closed:
                    if self._mutations:
                        self._condition.wait()
                    elif self._pending or time.monotonic() >= next_sweep:
                        break
                    else:
                        self._condition.wait(min(next_sweep - time.monotonic(),
                                                 threading.TIMEOUT_MAX))
                if self._closed:
                    return
                generation = self._generation
                self._pending = False
                started = time.monotonic()
                sampled_at = datetime.now(timezone.utc).isoformat()
            error = None
            try:
                status = self._collector()
                if not isinstance(status, dict):
                    raise TypeError("monitoring collector must return a dict")
                status = deepcopy(status)
            except Exception as exception:
                error = f"{type(exception).__name__}: {exception}"
                logging.getLogger(__name__).exception("Monitoring collection failed")
            with self._condition:
                if self._closed:
                    return
                if generation == self._generation and not self._mutations:
                    self._error = error
                    if error is None:
                        self._status = status
                        self._sampled_at = sampled_at
                        self._sample_started = started
                        self._published_generation = generation
                next_sweep = time.monotonic() + self._interval
