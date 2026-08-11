# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import sys
import threading
import time
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


def read_process_memory():
    """Process-wide RSS/PSS/thread readings, or None values off Linux."""
    readings = dict.fromkeys(_MEMORY_KEYS, None)
    if not sys.platform.startswith("linux"):
        return readings
    readings.update(_read_proc_fields("/proc/self/status", _STATUS_FIELDS))
    readings.update(
        _read_proc_fields("/proc/self/smaps_rollup", _SMAPS_ROLLUP_FIELDS)
    )
    return readings


class SearchRuntime:
    """Process-local, fixed-cardinality counters for the search fan-out.

    Never record a source initial, query, URL or category ID here: the snapshot
    is meant for logs, so every value has to stay bounded.
    """

    def __init__(self, clock=time.monotonic, memory_reader=read_process_memory):
        self._clock = clock
        self._memory_reader = memory_reader
        self._lock = threading.RLock()
        self._counters = dict.fromkeys(_COUNTER_KEYS, 0)
        self._active_requests = 0
        self._active_source_tasks = 0
        self._overdue_source_tasks = 0
        self._peak_active_source_tasks = 0
        self._last_activity_at = clock()

    @property
    def last_activity_at(self):
        with self._lock:
            return self._last_activity_at

    def reset(self):
        with self._lock:
            self._counters = dict.fromkeys(_COUNTER_KEYS, 0)
            self._active_requests = 0
            self._active_source_tasks = 0
            self._overdue_source_tasks = 0
            self._peak_active_source_tasks = 0
            self._last_activity_at = self._clock()

    @contextmanager
    def request(self, category_count, family_count):
        with self._lock:
            self._counters["requests_started"] += 1
            self._counters["categories_planned"] += int(category_count)
            self._counters["families_planned"] += int(family_count)
            self._active_requests += 1
            self._last_activity_at = self._clock()
        try:
            yield self
        finally:
            with self._lock:
                self._counters["requests_completed"] += 1
                self._active_requests -= 1
                self._last_activity_at = self._clock()

    @contextmanager
    def source_task(self):
        with self._lock:
            self._counters["source_started"] += 1
            self._active_source_tasks += 1
            self._peak_active_source_tasks = max(
                self._peak_active_source_tasks, self._active_source_tasks
            )
            self._last_activity_at = self._clock()
        try:
            yield self
        finally:
            with self._lock:
                self._active_source_tasks -= 1
                self._last_activity_at = self._clock()

    def record_source_outcome(self, outcome):
        if outcome not in SOURCE_OUTCOMES:
            raise ValueError(f"Unknown search source outcome: {outcome}")
        self._increment(f"source_{outcome}")

    def record_cache_hit(self):
        self._increment("cache_hits")

    def record_cache_miss(self):
        self._increment("cache_misses")

    def record_cache_eviction(self):
        self._increment("cache_evictions")

    def record_coalesced_waiter(self):
        self._increment("coalesced_waiters")

    def mark_source_overdue(self):
        with self._lock:
            self._overdue_source_tasks += 1
            self._last_activity_at = self._clock()

    def resolve_source_overdue(self):
        with self._lock:
            self._overdue_source_tasks = max(0, self._overdue_source_tasks - 1)
            self._last_activity_at = self._clock()

    def snapshot(self):
        readings = self._memory_reader() or {}
        with self._lock:
            snapshot = dict(self._counters)
            snapshot["active_requests"] = self._active_requests
            snapshot["active_source_tasks"] = self._active_source_tasks
            snapshot["overdue_source_tasks"] = self._overdue_source_tasks
            snapshot["peak_active_source_tasks"] = self._peak_active_source_tasks
        for key in _MEMORY_KEYS:
            snapshot[key] = readings.get(key)
        return snapshot

    def _increment(self, key):
        with self._lock:
            self._counters[key] += 1


search_runtime = SearchRuntime()
