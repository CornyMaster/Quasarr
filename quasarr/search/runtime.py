# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager

SOURCE_OUTCOMES = (
    "completed",
    "dropped",
    "skipped",
    "errored",
    "budget_exhausted",
)

_COUNTER_KEYS = (
    "requests_started",
    "requests_completed",
    "categories_planned",
    "families_planned",
    "source_started",
    "cache_hits",
    "cache_misses",
    "cache_evictions",
    "coalesced_waiters",
) + tuple(f"source_{outcome}" for outcome in SOURCE_OUTCOMES)

_MEMORY_KEYS = ("rss_kib", "pss_kib", "threads")

_STATUS_FIELDS = {"VmRSS": "rss_kib", "Threads": "threads"}
_SMAPS_ROLLUP_FIELDS = {"Pss": "pss_kib"}


def _parse_proc_fields(lines, wanted):
    values = {}
    for line in lines:
        name, separator, rest = line.partition(":")
        if not separator:
            continue
        key = wanted.get(name.strip())
        if key is None:
            continue
        parts = rest.split()
        if not parts:
            continue
        try:
            values[key] = int(parts[0])
        except ValueError:
            continue
    return values


def _read_proc_fields(path, wanted):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return _parse_proc_fields(handle, wanted)
    except OSError:
        return {}


def read_process_memory() -> dict[str, int | None]:
    """Process-wide RSS/PSS/thread readings, or None values off Linux."""
    readings: dict[str, int | None] = dict.fromkeys(_MEMORY_KEYS, None)
    if not sys.platform.startswith("linux"):
        return readings
    readings.update(_read_proc_fields("/proc/self/status", _STATUS_FIELDS))
    readings.update(_read_proc_fields("/proc/self/smaps_rollup", _SMAPS_ROLLUP_FIELDS))
    return readings


class _OverdueToken:
    """Opaque handle for one overdue source task, compared by identity."""

    __slots__ = ()


class SearchRuntime:
    """Process-local, fixed-cardinality counters for the search fan-out.

    Never record a source initial, query, URL or category ID here: the snapshot
    is meant for logs, so every value has to stay bounded.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        memory_reader: Callable[[], Mapping[str, int | None]] = read_process_memory,
    ) -> None:
        # The clock is injected so later time-based work stays deterministic in
        # tests; none of the counters below are time-based.
        self._clock = clock
        self._memory_reader = memory_reader
        self._lock = threading.RLock()
        self._counters = dict.fromkeys(_COUNTER_KEYS, 0)
        self._active_requests = 0
        self._active_source_tasks = 0
        self._overdue_tokens: set[_OverdueToken] = set()
        self._peak_active_source_tasks = 0
        self._idle_reclaimer = None

    @contextmanager
    def request(
        self, category_count: int, family_count: int
    ) -> Iterator["SearchRuntime"]:
        with self._lock:
            self._counters["requests_started"] += 1
            self._counters["categories_planned"] += int(category_count)
            self._counters["families_planned"] += int(family_count)
            self._active_requests += 1
            self._cancel_idle_reclaim_locked()
        try:
            yield self
        finally:
            with self._lock:
                self._counters["requests_completed"] += 1
                self._active_requests -= 1
                self._schedule_idle_reclaim_locked()

    @contextmanager
    def source_task(self) -> Iterator["SearchRuntime"]:
        with self._lock:
            self._counters["source_started"] += 1
            self._active_source_tasks += 1
            self._peak_active_source_tasks = max(
                self._peak_active_source_tasks, self._active_source_tasks
            )
            self._cancel_idle_reclaim_locked()
        try:
            yield self
        finally:
            with self._lock:
                self._active_source_tasks -= 1
                self._schedule_idle_reclaim_locked()

    def record_source_outcome(self, outcome: str) -> None:
        if outcome not in SOURCE_OUTCOMES:
            raise ValueError(f"Unknown search source outcome: {outcome}")
        self._increment(f"source_{outcome}")

    def record_cache_hit(self) -> None:
        self._increment("cache_hits")

    def record_cache_miss(self) -> None:
        self._increment("cache_misses")

    def record_cache_eviction(self) -> None:
        self._increment("cache_evictions")

    def record_coalesced_waiter(self) -> None:
        self._increment("coalesced_waiters")

    def mark_source_overdue(self) -> _OverdueToken:
        """Claim one overdue slot and return the token that releases it."""
        token = _OverdueToken()
        with self._lock:
            self._overdue_tokens.add(token)
            self._cancel_idle_reclaim_locked()
        return token

    def resolve_source_overdue(self, token: object) -> bool:
        """Release only the slot claimed by `token`; replays return False."""
        with self._lock:
            if not isinstance(token, _OverdueToken):
                return False
            if token not in self._overdue_tokens:
                return False
            self._overdue_tokens.remove(token)
            self._schedule_idle_reclaim_locked()
            return True

    def is_idle(self) -> bool:
        with self._lock:
            return self._is_idle_locked()

    def set_idle_reclaimer(self, reclaimer) -> None:
        with self._lock:
            self._idle_reclaimer = reclaimer

    def snapshot(self) -> dict[str, int | None]:
        readings = self._memory_readings()
        with self._lock:
            snapshot: dict[str, int | None] = dict(self._counters)
            snapshot["active_requests"] = self._active_requests
            snapshot["active_source_tasks"] = self._active_source_tasks
            snapshot["overdue_source_tasks"] = len(self._overdue_tokens)
            snapshot["peak_active_source_tasks"] = self._peak_active_source_tasks
        snapshot.update(readings)
        return snapshot

    def _memory_readings(self) -> dict[str, int | None]:
        """Readings the snapshot can trust; anything else degrades to None."""
        readings: dict[str, int | None] = dict.fromkeys(_MEMORY_KEYS, None)
        try:
            raw = self._memory_reader()
        except Exception:
            return readings
        if not isinstance(raw, Mapping):
            return readings
        for key in _MEMORY_KEYS:
            value = raw.get(key)
            # bool is an int subclass, but a flag is never a KiB or thread count.
            if isinstance(value, int) and not isinstance(value, bool):
                readings[key] = value
        return readings

    def _increment(self, key: str) -> None:
        with self._lock:
            self._counters[key] += 1

    def _is_idle_locked(self) -> bool:
        return (
            self._active_requests == 0
            and self._active_source_tasks == 0
            and not self._overdue_tokens
        )

    def _cancel_idle_reclaim_locked(self) -> None:
        if self._idle_reclaimer is None:
            return
        try:
            self._idle_reclaimer.cancel_for_activity()
        except Exception:
            pass

    def _schedule_idle_reclaim_locked(self) -> None:
        if self._idle_reclaimer is None or not self._is_idle_locked():
            return
        try:
            self._idle_reclaimer.schedule_if_quiet()
        except Exception:
            pass


search_runtime = SearchRuntime()
