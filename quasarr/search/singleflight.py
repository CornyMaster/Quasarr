# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from threading import RLock
from typing import Protocol


class _Submitter(Protocol):
    def submit(self, func: Callable[[], object]) -> Future[object]: ...


@dataclass
class _Flight:
    future: Future[object]
    waiters: int = 0
    cache_claimed: bool = False


class SharedWork:
    """One caller's handle to shared in-flight source work."""

    def __init__(
        self,
        registry: SearchSingleFlight,
        flight: _Flight,
        is_leader: bool,
        deadline: float,
    ) -> None:
        self.future = flight.future
        self.is_leader = is_leader
        self.deadline = deadline
        self._registry = registry
        self._flight = flight
        self._waiter_pending = not is_leader

    def waiter_done(self) -> None:
        """Release only this follower's accounting slot."""
        if not self._waiter_pending:
            return
        self._waiter_pending = False
        self._registry._waiter_done(self._flight)

    def _claim_cache(self) -> bool:
        """Claim the shared completion's single cache write."""
        return self._registry._claim_cache(self._flight)


class SearchSingleFlight:
    """Coalesce source work by the search cache's exact integer key."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._flights: dict[int, _Flight] = {}

    def submit(
        self,
        key: int,
        executor: _Submitter,
        func: Callable[[], object],
        deadline: float,
    ) -> SharedWork:
        with self._lock:
            flight = self._flights.get(key)
            if flight is not None and not flight.future.done():
                flight.waiters += 1
                return SharedWork(self, flight, False, deadline)

            future = executor.submit(func)
            flight = _Flight(future)
            self._flights[key] = flight
            future.add_done_callback(
                lambda done, flight_key=key, owner=flight: self._leader_done(
                    flight_key, owner, done
                )
            )
            return SharedWork(self, flight, True, deadline)

    def _leader_done(self, key: int, flight: _Flight, future: Future[object]) -> None:
        with self._lock:
            current = self._flights.get(key)
            if current is not None and current is flight and current.future is future:
                self._flights.pop(key)

    def _waiter_done(self, flight: _Flight) -> None:
        with self._lock:
            if flight.waiters > 0:
                flight.waiters -= 1

    def _claim_cache(self, flight: _Flight) -> bool:
        with self._lock:
            if flight.cache_claimed:
                return False
            flight.cache_claimed = True
            return True


search_singleflight = SearchSingleFlight()
