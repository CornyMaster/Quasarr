# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_active_budget: ContextVar["SearchBudget | None"] = ContextVar(
    "quasarr_search_budget", default=None
)


class SearchBudgetExhausted(Exception):
    """Raised at a checkpoint once the request's deadline has passed."""


class SearchBudget:
    """One worker's share of the request deadline, plus whether it ran out.

    The deadline is the absolute wall-clock instant the fan-out answers by, not
    a per-call allowance: a source that starts late inherits the time that is
    actually left. `exhausted` is only ever set from inside, by a source that
    asked for time and found none - so it means "this answer may be cut short",
    which is what the fan-out must not cache.
    """

    __slots__ = ("_clock", "_exhausted", "deadline")

    def __init__(self, deadline: float, clock: Callable[[], float] = time.time) -> None:
        self.deadline = float(deadline)
        self._clock = clock
        self._exhausted = False

    @property
    def exhausted(self) -> bool:
        return self._exhausted

    def remaining(self) -> float:
        """Seconds left, never negative; zero also marks the budget spent."""
        remaining = self.deadline - self._clock()
        if remaining <= 0:
            # Standing exactly on the deadline is spent: the next round trip
            # could only finish after the instant the response is due.
            self._exhausted = True
            return 0.0
        return remaining

    def clamp(self, default_seconds: float, minimum_seconds: float = 0.1) -> float:
        return max(minimum_seconds, min(default_seconds, self.remaining()))

    def checkpoint(self) -> None:
        if self.remaining() <= 0:
            raise SearchBudgetExhausted("Search budget spent")


@contextmanager
def use_search_budget(
    deadline: float, clock: Callable[[], float] = time.time
) -> Iterator[SearchBudget]:
    """Run a block under its own budget and hand the budget back.

    The clock is injectable because `SearchExecutor` already owns the wall
    clock its deadline is expressed in. The `ContextVar` is reset in `finally`,
    so a pool thread that is handed another task - or one whose source raised -
    starts without an inherited deadline.
    """
    budget = SearchBudget(deadline, clock=clock)
    token = _active_budget.set(budget)
    try:
        yield budget
    finally:
        _active_budget.reset(token)


def current_budget() -> SearchBudget | None:
    """The budget this worker runs under, or None outside a search worker."""
    return _active_budget.get()


def remaining_seconds() -> float | None:
    """Seconds left in this worker's budget, or None when there is none."""
    budget = _active_budget.get()
    if budget is None:
        return None
    return budget.remaining()


def clamp_timeout(default_seconds: float, minimum_seconds: float = 0.1) -> float:
    """Shorten a call site's own timeout to what the request has left.

    `default_seconds` is passed in rather than imported: slow mode rebinds the
    timeout constants at runtime, so a value captured at import time would keep
    serving the standard timeout after a user turned slow mode on. With no
    active budget the call site's value is returned unchanged.
    """
    budget = _active_budget.get()
    if budget is None:
        return default_seconds
    return budget.clamp(default_seconds, minimum_seconds)


def checkpoint() -> None:
    """Stop a loop that has no time left; a no-op outside a search worker."""
    budget = _active_budget.get()
    if budget is not None:
        budget.checkpoint()
