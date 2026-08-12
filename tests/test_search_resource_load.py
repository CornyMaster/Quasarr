import threading
import time
import unittest
from collections import Counter
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch
from wsgiref.util import setup_testing_defaults
from xml.etree import ElementTree

from bottle import Bottle

from quasarr.api.arr import setup_arr_routes
from quasarr.constants import SEARCH_CAT_MOVIES, SEARCH_CAT_MOVIES_UHD
from quasarr.search import SearchCache, SearchExecutor
from quasarr.search.cache import MAX_CACHE_ENTRIES, MAX_CACHE_RELEASES
from quasarr.search.runtime import SearchRuntime
from quasarr.search.singleflight import SearchSingleFlight
from quasarr.search.sources.helpers.budget import (
    checkpoint,
    current_budget,
    remaining_seconds,
)


@dataclass(frozen=True)
class _BurstConfig:
    cache_max_entries: int
    cache_max_releases: int
    use_budget_context: bool
    deadline_ignoring_source: bool


_PRODUCTION_CONFIG = _BurstConfig(
    cache_max_entries=MAX_CACHE_ENTRIES,
    cache_max_releases=MAX_CACHE_RELEASES,
    use_budget_context=True,
    deadline_ignoring_source=False,
)
_VIOLATING_CONFIG = _BurstConfig(
    cache_max_entries=(2**63) - 1,
    cache_max_releases=(2**63) - 1,
    use_budget_context=False,
    deadline_ignoring_source=True,
)


class _SyntheticSource:
    supported_categories = [SEARCH_CAT_MOVIES]
    supports_imdb = True
    supports_phrase = False
    supports_absolute_numbering = False
    supports_date_numbering = False

    def __init__(self, initials, search):
        self.initials = initials
        self._search = search

    def search(self, *args, **kwargs):
        return self._search(*args, **kwargs)


class _BurstRuntime(SearchRuntime):
    def __init__(self, expected_coalesced_waiters):
        super().__init__(memory_reader=lambda: {"threads": threading.active_count()})
        self.expected_coalesced_waiters = expected_coalesced_waiters
        self.coalesced_waiters_ready = threading.Event()
        self.overdue_drained = threading.Event()

    def record_coalesced_waiter(self):
        super().record_coalesced_waiter()
        if self.snapshot()["coalesced_waiters"] >= self.expected_coalesced_waiters:
            self.coalesced_waiters_ready.set()

    def resolve_source_overdue(self, token):
        resolved = super().resolve_source_overdue(token)
        if resolved and self.snapshot()["overdue_source_tasks"] == 0:
            self.overdue_drained.set()
        return resolved


class _RecordingCache(SearchCache):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.writes = []
        self._writes_lock = threading.Lock()

    def set(self, key, value, ttl=300):
        with self._writes_lock:
            self.writes.append((key, ttl))
        super().set(key, value, ttl=ttl)


class _ScriptedWorkerClock:
    def __init__(self, fanout_time, worker_times):
        self._fanout_thread = threading.get_ident()
        self._fanout_time = fanout_time
        self._worker_times = list(worker_times)
        self._index = 0
        self._lock = threading.Lock()

    def __call__(self):
        if threading.get_ident() == self._fanout_thread:
            return self._fanout_time
        with self._lock:
            index = min(self._index, len(self._worker_times) - 1)
            self._index += 1
            return self._worker_times[index]


@contextmanager
def _without_budget(_deadline, clock):
    del clock
    yield SimpleNamespace(exhausted=False)


def _bound_violations(snapshot, cache_stats) -> list[str]:
    violations = []
    if cache_stats["max_entries"] != MAX_CACHE_ENTRIES:
        violations.append("cache max_entries is not bounded by the production limit")
    if cache_stats["max_releases"] != MAX_CACHE_RELEASES:
        violations.append("cache max_releases is not bounded by the production limit")
    if cache_stats["entry_count"] > cache_stats["max_entries"]:
        violations.append("cache entry count exceeds its configured limit")
    if cache_stats["release_count"] > cache_stats["max_releases"]:
        violations.append("cache release count exceeds its configured limit")
    if snapshot["budget_context_missing"]:
        violations.append("source work ran without the production budget context")
    if (
        not snapshot["overdue_drained_within_grace"]
        or snapshot["overdue_source_tasks"] != 0
    ):
        violations.append(
            "overdue source gauge did not drain within the five-second grace"
        )
    if snapshot["active_requests"] != 0:
        violations.append("active request gauge did not return to zero")
    if snapshot["active_source_tasks"] != 0:
        violations.append("active source gauge did not return to zero")
    if snapshot["uncacheable_entries_retained"]:
        violations.append("a timed-out or partial result was retained in cache")
    if snapshot["threads"] > snapshot["baseline_threads"] + 1:
        violations.append("thread count did not return near baseline")
    if snapshot["idle_reclaimer_enabled"]:
        violations.append("idle memory reclaimer was enabled by the load harness")
    return violations


def _run_burst(config):
    baseline_thread_ids = {
        thread.ident for thread in threading.enumerate() if thread.ident is not None
    }
    baseline_threads = len(baseline_thread_ids)
    runtime = _BurstRuntime(expected_coalesced_waiters=2)
    cache = _RecordingCache(
        max_entries=config.cache_max_entries,
        max_releases=config.cache_max_releases,
    )
    singleflight = SearchSingleFlight()
    route_release = threading.Event()
    route_sources_started = threading.Event()
    overrun_release = threading.Event()
    overrun_started = threading.Event()
    overrun_grace_elapsed = threading.Event()
    overrun_ignored_grace = threading.Event()
    route_barrier = threading.Barrier(3, timeout=5)
    route_lock = threading.Lock()
    planned_pairs = []
    route_calls = Counter()
    route_pages = []
    partial_pages = []
    budget_context_missing = 0
    responses = [None, None]
    request_failures = []
    request_threads = []

    def record_budget_context():
        nonlocal budget_context_missing
        if current_budget() is None:
            with route_lock:
                budget_context_missing += 1

    def release(title, date, link, hostname):
        return {
            "details": {
                "title": title,
                "date": date,
                "link": link,
                "source": "Synthetic source",
                "hostname": hostname,
                "size": 1,
            }
        }

    fast_releases = [
        release(
            "Synthetic.Fast.2160p",
            "Sun, 04 Jan 2026 00:00:00 +0000",
            "https://downloads.invalid/fast-uhd",
            "FA",
        ),
        release(
            "Synthetic.Fast.1080p",
            "Sat, 03 Jan 2026 00:00:00 +0000",
            "https://downloads.invalid/fast-hd",
            "FA",
        ),
    ]
    page_releases = [
        release(
            "Synthetic.Page.One.2160p",
            "Fri, 02 Jan 2026 00:00:00 +0000",
            "https://downloads.invalid/page-one",
            "PG",
        ),
        release(
            "Synthetic.Page.Two.1080p",
            "Thu, 01 Jan 2026 00:00:00 +0000",
            "https://downloads.invalid/page-two",
            "PG",
        ),
    ]

    def record_route_call(initials):
        with route_lock:
            route_calls[initials] += 1
            if sum(route_calls.values()) >= 2:
                route_sources_started.set()

    def fast_search(*_args, **_kwargs):
        record_budget_context()
        record_route_call("fa")
        if not route_release.wait(5):
            raise RuntimeError("synthetic fast source was not released")
        return fast_releases

    def paginated_search(*_args, **_kwargs):
        record_budget_context()
        record_route_call("pg")
        collected = []
        for page, item in enumerate(page_releases, start=1):
            checkpoint()
            with route_lock:
                route_pages.append(page)
            collected.append(item)
        if not route_release.wait(5):
            raise RuntimeError("synthetic paginated source was not released")
        return collected

    sources = {
        "fa": _SyntheticSource("fa", fast_search),
        "pg": _SyntheticSource("pg", paginated_search),
    }

    class State:
        pass

    state = State()
    state.values = {
        "radarr_client": object(),
        "config": lambda section: (
            {"fa": "fast.invalid", "pg": "pages.invalid"}
            if section == "Hostnames"
            else {}
        ),
    }

    class RecordingSearchExecutor(SearchExecutor):
        def add(self, source, args, kwargs, **options):
            with route_lock:
                planned_pairs.append((source.initials, args[2]))
            return super().add(source, args, kwargs, **options)

    def call_app(app, query_string):
        environ = {}
        setup_testing_defaults(environ)
        environ.update(
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/api",
                "QUERY_STRING": query_string,
                "HTTP_USER_AGENT": "Radarr/5.0",
                "wsgi.input": BytesIO(b""),
            }
        )
        captured = {}

        def start_response(status, headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = headers

        body = b"".join(app(environ, start_response)).decode("utf-8")
        titles = tuple(
            title.text
            for title in ElementTree.fromstring(body).findall(".//item/title")
        )
        return captured["status"], titles

    def run_request(index, app):
        try:
            route_barrier.wait()
            responses[index] = call_app(
                app,
                "t=movie&cat=2045,2000&imdbid=tt9999999999999999&offset=1&limit=2",
            )
        except Exception as error:
            with route_lock:
                request_failures.append(repr(error))

    def join_new_threads(grace_seconds=5):
        deadline = time.monotonic() + grace_seconds
        while True:
            extra_threads = [
                thread
                for thread in threading.enumerate()
                if thread.ident not in baseline_thread_ids
                and thread is not threading.current_thread()
            ]
            if not extra_threads:
                return
            for thread in extra_threads:
                thread.join(max(0.0, deadline - time.monotonic()))
            if time.monotonic() >= deadline:
                return

    app = Bottle()
    setup_arr_routes(app)
    measured_snapshot = None
    overdue_drained_within_grace = False
    partial_key = None
    overrun_key = None
    partial_results = []
    overrun_results = []

    with ExitStack() as patches:
        patches.enter_context(patch("quasarr.api.arr.shared_state", state))
        patches.enter_context(patch("quasarr.api.arr.search_runtime", runtime))
        patches.enter_context(patch("quasarr.search.get_sources", return_value=sources))
        patches.enter_context(
            patch("quasarr.search.get_search_category_sources", return_value=[])
        )
        patches.enter_context(patch("quasarr.search.get_imdb_metadata"))
        patches.enter_context(
            patch("quasarr.search.SearchExecutor", RecordingSearchExecutor)
        )
        patches.enter_context(patch("quasarr.search.search_runtime", runtime))
        patches.enter_context(patch("quasarr.search.cache.search_runtime", runtime))
        patches.enter_context(patch("quasarr.search.search_cache", cache))
        patches.enter_context(patch("quasarr.search.search_singleflight", singleflight))
        if not config.use_budget_context:
            patches.enter_context(
                patch("quasarr.search.use_search_budget", _without_budget)
            )

        try:
            request_threads = [
                threading.Thread(
                    target=run_request,
                    args=(index, app),
                    name=f"resource-load-request-{index}",
                )
                for index in range(2)
            ]
            for thread in request_threads:
                thread.start()
            route_barrier.wait()
            if not route_sources_started.wait(5):
                raise AssertionError("both synthetic route sources did not start")
            if not runtime.coalesced_waiters_ready.wait(5):
                raise AssertionError("identical route requests did not coalesce")
            route_release.set()
            for thread in request_threads:
                thread.join(5)
            if any(thread.is_alive() for thread in request_threads):
                raise AssertionError("a synthetic route request did not finish")

            partial_deadline = 1005.0
            partial_clock = _ScriptedWorkerClock(
                fanout_time=1000.0,
                worker_times=[partial_deadline + 1.0, partial_deadline - 1.0],
            )
            partial_release = release(
                "Synthetic.Partial.2160p",
                "Mon, 05 Jan 2026 00:00:00 +0000",
                "https://downloads.invalid/partial",
                "PP",
            )
            unrequested_release = release(
                "Synthetic.Unrequested.2160p",
                "Tue, 06 Jan 2026 00:00:00 +0000",
                "https://downloads.invalid/unrequested",
                "PP",
            )

            def partial_search(*_args, **_kwargs):
                record_budget_context()
                partial_pages.append(1)
                remaining = remaining_seconds()
                if remaining is None:
                    partial_pages.append(2)
                    return [partial_release, unrequested_release]
                if remaining <= 0:
                    return [partial_release]
                partial_pages.append(2)
                return [partial_release, unrequested_release]

            partial_executor = SearchExecutor(
                deadline=partial_deadline,
                clock=partial_clock,
            )
            partial_executor.add(
                _SyntheticSource("pp", partial_search),
                (state, 0.0, SEARCH_CAT_MOVIES),
                {},
                use_cache=True,
            )
            partial_key = partial_executor.searches[0][0]
            partial_results = partial_executor.run_all()[0]
            join_new_threads()

            def overrun_search(*_args, **_kwargs):
                record_budget_context()
                overrun_started.set()
                if config.deadline_ignoring_source:
                    if not overrun_grace_elapsed.wait(5):
                        raise RuntimeError("synthetic grace boundary was not reached")
                    overrun_ignored_grace.set()
                if not overrun_release.wait(5):
                    raise RuntimeError("synthetic overrun source was not released")
                return [
                    release(
                        "Synthetic.Overrun.2160p",
                        "Wed, 07 Jan 2026 00:00:00 +0000",
                        "https://downloads.invalid/overrun",
                        "OV",
                    )
                ]

            def force_overrun_timeout(_futures, timeout):
                del timeout
                if not overrun_started.wait(5):
                    raise AssertionError("synthetic overrun source did not start")
                raise FutureTimeoutError

            overrun_executor = SearchExecutor(deadline=2001.0, clock=lambda: 2000.0)
            overrun_executor.add(
                _SyntheticSource("ov", overrun_search),
                (state, 0.0, SEARCH_CAT_MOVIES),
                {},
                use_cache=True,
            )
            overrun_key = overrun_executor.searches[0][0]
            with patch("quasarr.search.as_completed", force_overrun_timeout):
                overrun_results = overrun_executor.run_all()[0]

            if config.deadline_ignoring_source:
                overrun_grace_elapsed.set()
                if not overrun_ignored_grace.wait(5):
                    raise AssertionError(
                        "deadline-ignoring source did not cross the grace boundary"
                    )
                measured_snapshot = runtime.snapshot()
            else:
                overrun_release.set()
                overdue_drained_within_grace = runtime.overdue_drained.wait(5)
                join_new_threads()
                measured_snapshot = runtime.snapshot()
        finally:
            route_release.set()
            overrun_release.set()
            for thread in request_threads:
                thread.join(5)
            runtime.overdue_drained.wait(5)
            join_new_threads()

        cleanup_snapshot = runtime.snapshot()
        if measured_snapshot is None:
            measured_snapshot = cleanup_snapshot.copy()
        if config.deadline_ignoring_source:
            overdue_drained_within_grace = False

        cache_stats = cache.stats()
        uncacheable_entries_retained = sum(
            key in cache.cache for key in (partial_key, overrun_key) if key is not None
        )
        measured_snapshot.update(
            {
                "baseline_threads": baseline_threads,
                "budget_context_missing": budget_context_missing,
                "overdue_drained_within_grace": overdue_drained_within_grace,
                "uncacheable_entries_retained": uncacheable_entries_retained,
                "idle_reclaimer_enabled": runtime._idle_reclaimer is not None,
                "planned_pairs": tuple(planned_pairs),
                "route_calls": dict(route_calls),
                "route_pages": tuple(route_pages),
                "partial_pages": tuple(partial_pages),
                "partial_titles": tuple(
                    item["details"]["title"] for item in partial_results
                ),
                "overrun_results": tuple(overrun_results),
                "responses": tuple(responses),
                "request_failures": tuple(request_failures),
                "cache_write_ttls": tuple(ttl for _, ttl in cache.writes),
                "cleanup_active_requests": cleanup_snapshot["active_requests"],
                "cleanup_active_source_tasks": cleanup_snapshot["active_source_tasks"],
                "cleanup_overdue_source_tasks": cleanup_snapshot[
                    "overdue_source_tasks"
                ],
                "cleanup_threads": cleanup_snapshot["threads"],
            }
        )

    return measured_snapshot, cache_stats


class SearchResourceLoadTests(unittest.TestCase):
    def test_production_burst_preserves_work_and_resource_bounds(self):
        snapshot, cache_stats = _run_burst(_PRODUCTION_CONFIG)

        self.assertEqual([], _bound_violations(snapshot, cache_stats))
        expected_pairs = Counter(
            (source, category)
            for _request in range(2)
            for category in (SEARCH_CAT_MOVIES, SEARCH_CAT_MOVIES_UHD)
            for source in ("fa", "pg")
        )
        self.assertEqual(expected_pairs, Counter(snapshot["planned_pairs"]))
        self.assertEqual({"fa": 1, "pg": 1}, snapshot["route_calls"])
        self.assertEqual(2, snapshot["coalesced_waiters"])
        self.assertEqual(2, snapshot["peak_active_source_tasks"])
        self.assertEqual((1, 2), snapshot["route_pages"])
        self.assertEqual((1,), snapshot["partial_pages"])
        self.assertEqual(("Synthetic.Partial.2160p",), snapshot["partial_titles"])
        expected_response = (
            "200 OK",
            (
                "[FA] Synthetic.Fast.1080p",
                "[PG] Synthetic.Page.One.2160p",
            ),
        )
        self.assertEqual((expected_response, expected_response), snapshot["responses"])
        self.assertEqual((), snapshot["request_failures"])
        self.assertEqual(4, snapshot["categories_planned"])
        self.assertEqual(2, snapshot["families_planned"])
        self.assertEqual((300, 300), snapshot["cache_write_ttls"])
        self.assertEqual(2, cache_stats["entry_count"])
        self.assertEqual(4, cache_stats["release_count"])
        self.assertEqual(1, snapshot["source_budget_exhausted"])
        self.assertEqual(1, snapshot["source_dropped"])
        self.assertEqual((), snapshot["overrun_results"])

    def test_violating_configuration_is_rejected_by_production_bounds(self):
        snapshot, cache_stats = _run_burst(_VIOLATING_CONFIG)

        violations = _bound_violations(snapshot, cache_stats)
        self.assertTrue(violations)
        for expected in (
            "cache max_entries",
            "cache max_releases",
            "without the production budget context",
            "overdue source gauge",
        ):
            with self.subTest(expected=expected):
                self.assertTrue(
                    any(expected in violation for violation in violations),
                    violations,
                )
        # The violating snapshot is intentionally taken while the source is
        # held. `_run_burst` must still release it before returning so this
        # proof cannot contaminate another test or the process thread count.
        self.assertEqual(0, snapshot["cleanup_active_requests"])
        self.assertEqual(0, snapshot["cleanup_active_source_tasks"])
        self.assertEqual(0, snapshot["cleanup_overdue_source_tasks"])
        self.assertLessEqual(
            snapshot["cleanup_threads"], snapshot["baseline_threads"] + 1
        )


if __name__ == "__main__":
    unittest.main()
