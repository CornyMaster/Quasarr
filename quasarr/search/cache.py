# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from threading import RLock
from typing import NamedTuple

from quasarr.search.runtime import search_runtime

# Entries are cheap to count but say nothing about size: one source can answer a
# broad query with thousands of releases, so the release total is its own limit.
MAX_CACHE_ENTRIES = 2048
MAX_CACHE_RELEASES = 50000


class _Entry(NamedTuple):
    value: list
    expires_at: float
    # Counted once at write time: accounting stays consistent even if the caller
    # later mutates the list it handed over.
    releases: int


class SearchCache:
    """Bounded TTL/LRU cache for per-source search results.

    Both the entry count and the total number of retained releases are capped,
    so a long-lived process cannot grow with however many category, source and
    query combinations arrive before their TTLs run out.
    """

    def __init__(
        self,
        max_entries: int = MAX_CACHE_ENTRIES,
        max_releases: int = MAX_CACHE_RELEASES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        # Wall clock, not monotonic: `get` hands the absolute expiry back to the
        # fan-out, which subtracts time.time() from it to report the TTL left.
        self._clock = clock
        self._max_entries = max_entries
        self._max_releases = max_releases
        self._lock = RLock()
        self.cache: OrderedDict[Hashable, _Entry] = OrderedDict()
        self._release_count = 0

    def get(self, key: Hashable) -> tuple[list | None, float]:
        """Return `(value, expires_at)` for a live entry, else `(None, 0)`."""
        now = self._clock()
        with self._lock:
            entry = self.cache.get(key)
            if entry is not None and now < entry.expires_at:
                self.cache.move_to_end(key)
                hit = (entry.value, entry.expires_at)
            else:
                hit = None
                if entry is not None:
                    self._discard_locked(key)
        # Counters are recorded outside the cache lock so instrumentation can
        # never make a reader wait on another subsystem's lock.
        if hit is None:
            search_runtime.record_cache_miss()
            return (None, 0)
        search_runtime.record_cache_hit()
        return hit

    def set(self, key: Hashable, value: list, ttl: float = 300) -> None:
        now = self._clock()
        with self._lock:
            # Expired entries go first, so no live entry is evicted while stale
            # ones still hold the room.
            self._sweep_locked(now)
            self._discard_locked(key)
            releases = len(value)
            self.cache[key] = _Entry(value, now + ttl, releases)
            self._release_count += releases
            evicted = self._evict_locked()
        for _ in range(evicted):
            search_runtime.record_cache_eviction()

    def sweep(self, now: float | None = None) -> int:
        """Drop every expired entry and return how many were removed."""
        with self._lock:
            return self._sweep_locked(self._clock() if now is None else now)

    def clear(self) -> int:
        """Drop every entry, expired or not, and return how many were removed."""
        with self._lock:
            removed = len(self.cache)
            self.cache.clear()
            self._release_count = 0
            return removed

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entry_count": len(self.cache),
                "release_count": self._release_count,
                "max_entries": self._max_entries,
                "max_releases": self._max_releases,
            }

    def _sweep_locked(self, now: float) -> int:
        expired = [key for key, entry in self.cache.items() if now >= entry.expires_at]
        for key in expired:
            self._discard_locked(key)
        return len(expired)

    def _evict_locked(self) -> int:
        """Evict from the LRU front until both limits hold; report how many."""
        evicted = 0
        while self.cache and (
            len(self.cache) > self._max_entries
            or self._release_count > self._max_releases
        ):
            self._discard_locked(next(iter(self.cache)))
            evicted += 1
        return evicted

    def _discard_locked(self, key: Hashable) -> None:
        entry = self.cache.pop(key, None)
        if entry is not None:
            self._release_count -= entry.releases


search_cache = SearchCache()
