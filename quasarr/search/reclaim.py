# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import ctypes
import gc
import sys
import threading
import time
from collections.abc import Callable, Mapping
from typing import cast

from quasarr.providers.log import debug
from quasarr.search.runtime import read_process_memory

_UNPROBED = object()


class _NativeHeapTrimmer:
    """One process-local, permanently cached glibc malloc_trim probe."""

    def __init__(self, platform: str, libc_loader: Callable) -> None:
        self._platform = platform
        self._libc_loader = libc_loader
        self._lock = threading.Lock()
        self._malloc_trim = _UNPROBED

    def __call__(self) -> bool:
        malloc_trim = self._get_malloc_trim()
        if malloc_trim is None:
            return False
        try:
            return bool(malloc_trim(0))
        except Exception:
            with self._lock:
                self._malloc_trim = None
            return False

    def _get_malloc_trim(self) -> Callable[[int], int] | None:
        with self._lock:
            if self._malloc_trim is not _UNPROBED:
                return cast(Callable[[int], int] | None, self._malloc_trim)
            self._malloc_trim = self._probe()
            return self._malloc_trim

    def _probe(self) -> Callable[[int], int] | None:
        if not self._platform.startswith("linux"):
            return None
        try:
            libc = self._libc_loader("libc.so.6", use_errno=True)
            malloc_trim = libc.malloc_trim
            malloc_trim.argtypes = [ctypes.c_size_t]
            malloc_trim.restype = ctypes.c_int
        except Exception:
            return None
        return cast(Callable[[int], int], malloc_trim)


_native_heap_trimmer = _NativeHeapTrimmer(
    platform=sys.platform,
    libc_loader=ctypes.CDLL,
)


def trim_native_heap() -> bool:
    """Return whether glibc released native allocator pages to the OS."""
    return _native_heap_trimmer()


def _empty_summary() -> dict[str, bool | int | None]:
    return {
        "performed": False,
        "failed": False,
        "expired_cache_entries": 0,
        "gc_collected": 0,
        "native_heap_trimmed": False,
        "pss_before_kib": None,
        "pss_after_kib": None,
    }


class IdleMemoryReclaimer:
    """Reclaim expired search memory after a sustained quiet period."""

    def __init__(
        self,
        cache,
        runtime,
        quiet_seconds: float = 30,
        minimum_interval_seconds: float = 300,
        clock: Callable[[], float] = time.monotonic,
        timer_factory: Callable = threading.Timer,
        *,
        collector: Callable[[], int] = gc.collect,
        native_trimmer: Callable[[], bool] = trim_native_heap,
        memory_reader: Callable[[], Mapping[str, int | None]] = read_process_memory,
        logger: Callable[[str], None] = debug,
    ) -> None:
        self._cache = cache
        self._runtime = runtime
        self._quiet_seconds = quiet_seconds
        self._minimum_interval_seconds = minimum_interval_seconds
        self._clock = clock
        self._timer_factory = timer_factory
        self._collector = collector
        self._native_trimmer = native_trimmer
        self._memory_reader = memory_reader
        self._logger = logger
        self._lock = threading.Lock()
        self._timer = None
        self._timer_token = None
        self._last_collection_at = None
        self._collecting = False
        self._activity_generation = 0
        self._schedule_after_collection = False
        runtime.set_idle_reclaimer(self)

    def schedule_if_quiet(self) -> None:
        try:
            if not self._runtime.is_idle():
                return
            with self._lock:
                if self._timer is not None:
                    return
                if self._collecting:
                    self._schedule_after_collection = True
                    return

            now = self._clock()
            with self._lock:
                if self._timer is not None:
                    return
                if self._collecting:
                    self._schedule_after_collection = True
                    return
                delay = self._quiet_seconds
                if self._last_collection_at is not None:
                    delay = max(
                        delay,
                        self._minimum_interval_seconds
                        - (now - self._last_collection_at),
                    )
                token = object()
                activity_generation = self._activity_generation
                timer = self._timer_factory(
                    max(0, delay),
                    lambda: self._timer_fired(token, activity_generation),
                )
                timer.daemon = True
                self._timer = timer
                self._timer_token = token
        except Exception:
            self._log_message("Search idle memory reclaim scheduling failed")
            return

        try:
            if not self._runtime.is_idle():
                self._discard_timer(token, timer)
                return
            with self._lock:
                should_start = (
                    token is self._timer_token
                    and activity_generation == self._activity_generation
                )
            if not should_start:
                timer.cancel()
                return
            timer.start()
        except Exception:
            self._discard_timer(token, timer)
            self._log_message("Search idle memory reclaim timer start failed")

    def cancel_for_activity(self) -> None:
        with self._lock:
            self._activity_generation += 1
            timer = self._timer
            self._timer = None
            self._timer_token = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                self._log_message("Search idle memory reclaim timer cancel failed")

    def collect_and_trim(self) -> dict[str, bool | int | None]:
        summary = _empty_summary()
        try:
            if not self._runtime.is_idle():
                return summary
            now = self._clock()
            with self._lock:
                if self._collecting:
                    return summary
                if (
                    self._last_collection_at is not None
                    and now - self._last_collection_at < self._minimum_interval_seconds
                ):
                    return summary
                self._collecting = True
                activity_generation = self._activity_generation
        except Exception:
            self._log_message("Search idle memory reclaim setup failed")
            return summary

        return self._collect_claimed(summary, now, activity_generation)

    def _collect_claimed(
        self,
        summary: dict[str, bool | int | None],
        now: float,
        activity_generation: int,
    ) -> dict[str, bool | int | None]:
        try:
            if not self._quiet_is_unchanged(activity_generation):
                return summary
            summary["pss_before_kib"] = self._read_pss()
            if not self._quiet_is_unchanged(activity_generation):
                return summary

            summary["expired_cache_entries"] = self._cache.sweep()
            if not self._quiet_is_unchanged(activity_generation):
                return summary

            with self._lock:
                self._last_collection_at = now
            summary["performed"] = True
            summary["gc_collected"] = self._collector()
            summary["native_heap_trimmed"] = bool(self._native_trimmer())
        except Exception:
            with self._lock:
                self._last_collection_at = now
            summary["failed"] = True
        finally:
            if summary["performed"] or summary["failed"]:
                summary["pss_after_kib"] = self._read_pss()
                self._log(summary)
            with self._lock:
                self._collecting = False
                schedule_after_collection = self._schedule_after_collection
                self._schedule_after_collection = False
            if schedule_after_collection:
                self.schedule_if_quiet()
        return summary

    def _timer_fired(self, token: object, activity_generation: int) -> None:
        collection_claimed = False
        try:
            now = self._clock()
            with self._lock:
                if token is not self._timer_token:
                    return
                if activity_generation != self._activity_generation:
                    self._timer = None
                    self._timer_token = None
                    return
                self._timer = None
                self._timer_token = None
                if self._collecting:
                    return
                if (
                    self._last_collection_at is not None
                    and now - self._last_collection_at < self._minimum_interval_seconds
                ):
                    return
                self._collecting = True
                collection_claimed = True
            self._collect_claimed(_empty_summary(), now, activity_generation)
        except Exception:
            with self._lock:
                if token is self._timer_token:
                    self._timer = None
                    self._timer_token = None
                if collection_claimed and self._collecting:
                    self._collecting = False
                schedule_after_collection = self._schedule_after_collection
                self._schedule_after_collection = False
            self._log_message("Search idle memory reclaim timer callback failed")
            if schedule_after_collection:
                self.schedule_if_quiet()

    def _discard_timer(self, token: object, timer) -> None:
        with self._lock:
            if token is self._timer_token:
                self._timer = None
                self._timer_token = None
        try:
            timer.cancel()
        except Exception:
            self._log_message("Search idle memory reclaim timer cancel failed")

    def _quiet_is_unchanged(self, activity_generation: int) -> bool:
        if not self._runtime.is_idle():
            return False
        with self._lock:
            return activity_generation == self._activity_generation

    def _read_pss(self) -> int | None:
        try:
            readings = self._memory_reader()
        except Exception:
            return None
        if not isinstance(readings, Mapping):
            return None
        pss_kib = readings.get("pss_kib")
        if isinstance(pss_kib, int) and not isinstance(pss_kib, bool):
            return pss_kib
        return None

    def _log(self, summary: Mapping[str, bool | int | None]) -> None:
        self._log_message(f"Search idle memory reclaim summary: {dict(summary)}")

    def _log_message(self, message: str) -> None:
        try:
            self._logger(message)
        except Exception:
            pass
