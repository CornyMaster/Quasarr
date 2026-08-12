import io
import unittest
from threading import Barrier, Thread
from unittest.mock import patch

from quasarr.search.runtime import (
    SearchRuntime,
    _parse_proc_fields,
    _read_proc_fields,
    read_process_memory,
    search_runtime,
)

SNAPSHOT_KEYS = {
    "active_requests",
    "active_source_tasks",
    "overdue_source_tasks",
    "peak_active_source_tasks",
    "requests_started",
    "requests_completed",
    "categories_planned",
    "families_planned",
    "source_started",
    "source_completed",
    "source_dropped",
    "source_skipped",
    "source_errored",
    "source_budget_exhausted",
    "cache_hits",
    "cache_misses",
    "cache_evictions",
    "coalesced_waiters",
    "rss_kib",
    "pss_kib",
    "threads",
}


PROC_FILES = {
    "/proc/self/status": "Name:\tpython3\nVmRSS:\t  123456 kB\nThreads:\t17\n",
    "/proc/self/smaps_rollup": "Rss:\t  123456 kB\nPss:\t   65432 kB\n",
}


def make_runtime(memory=None):
    return SearchRuntime(clock=lambda: 100.0, memory_reader=lambda: memory or {})


def fake_proc_opener(files):
    """Stand-in for open() so the /proc reader never touches a real file."""

    def fake_open(path, *_args, **_kwargs):
        try:
            return io.StringIO(files[path])
        except KeyError:
            raise OSError(f"no such file: {path}") from None

    return fake_open


class SearchRuntimeTests(unittest.TestCase):
    def test_request_and_source_counts_return_to_zero(self):
        runtime = SearchRuntime(clock=lambda: 100.0, memory_reader=lambda: {})
        with runtime.request(category_count=3, family_count=1):
            with runtime.source_task():
                snapshot = runtime.snapshot()
                self.assertEqual(1, snapshot["active_requests"])
                self.assertEqual(1, snapshot["active_source_tasks"])
        snapshot = runtime.snapshot()
        self.assertEqual(0, snapshot["active_requests"])
        self.assertEqual(0, snapshot["active_source_tasks"])

    def test_fixed_outcome_counters_are_recorded(self):
        runtime = SearchRuntime(clock=lambda: 100.0, memory_reader=lambda: {})
        runtime.record_source_outcome("completed")
        runtime.record_source_outcome("dropped")
        runtime.record_source_outcome("budget_exhausted")
        self.assertEqual(1, runtime.snapshot()["source_completed"])
        self.assertEqual(1, runtime.snapshot()["source_dropped"])
        self.assertEqual(1, runtime.snapshot()["source_budget_exhausted"])

    def test_unknown_outcome_is_rejected(self):
        runtime = SearchRuntime(clock=lambda: 100.0, memory_reader=lambda: {})
        with self.assertRaises(ValueError):
            runtime.record_source_outcome("source-name-must-not-be-a-label")

    def test_skipped_and_errored_outcomes_are_recorded(self):
        runtime = make_runtime()

        runtime.record_source_outcome("skipped")
        runtime.record_source_outcome("errored")
        runtime.record_source_outcome("errored")

        snapshot = runtime.snapshot()
        self.assertEqual(1, snapshot["source_skipped"])
        self.assertEqual(2, snapshot["source_errored"])

    def test_snapshot_exposes_only_fixed_cardinality_keys(self):
        # Every key is a fixed counter name. A source initial, query, URL or
        # category ID must never reach the snapshot, because these counters end
        # up in logs and unbounded label values would leak user configuration.
        runtime = make_runtime()

        with runtime.request(category_count=2, family_count=1):
            with runtime.source_task():
                pass

        self.assertEqual(SNAPSHOT_KEYS, set(runtime.snapshot()))

    def test_request_totals_accumulate_across_requests(self):
        runtime = make_runtime()

        with runtime.request(category_count=3, family_count=2):
            pass
        with runtime.request(category_count=1, family_count=1):
            pass

        snapshot = runtime.snapshot()
        self.assertEqual(2, snapshot["requests_started"])
        self.assertEqual(2, snapshot["requests_completed"])
        self.assertEqual(4, snapshot["categories_planned"])
        self.assertEqual(3, snapshot["families_planned"])
        self.assertEqual(0, snapshot["active_requests"])

    def test_gauges_are_restored_when_the_body_raises(self):
        # The fan-out raises on source errors; a gauge that only decrements on
        # the success path would report phantom in-flight work forever.
        runtime = make_runtime()

        with self.assertRaises(RuntimeError):
            with runtime.request(category_count=1, family_count=1):
                with runtime.source_task():
                    raise RuntimeError("source failed")

        snapshot = runtime.snapshot()
        self.assertEqual(0, snapshot["active_requests"])
        self.assertEqual(0, snapshot["active_source_tasks"])
        self.assertEqual(1, snapshot["requests_completed"])
        self.assertEqual(1, snapshot["source_started"])

    def test_peak_active_source_tasks_keeps_the_high_water_mark(self):
        runtime = make_runtime()

        with runtime.source_task():
            with runtime.source_task():
                with runtime.source_task():
                    pass

        snapshot = runtime.snapshot()
        self.assertEqual(0, snapshot["active_source_tasks"])
        self.assertEqual(3, snapshot["peak_active_source_tasks"])
        self.assertEqual(3, snapshot["source_started"])

    def test_overdue_tokens_are_resolved_exactly_once(self):
        # A source dropped at the deadline keeps running; the gauge has to fall
        # again once its future finishes, otherwise idle reclamation would never
        # see a quiet process. Several sources can be overdue at once and their
        # futures finish in any order, so a resolve must release the caller's
        # own slot only - a duplicate or replayed call must not cancel another
        # task's mark.
        runtime = make_runtime()

        first = runtime.mark_source_overdue()
        second = runtime.mark_source_overdue()
        self.assertIsNot(first, second)
        self.assertEqual(2, runtime.snapshot()["overdue_source_tasks"])

        self.assertTrue(runtime.resolve_source_overdue(first))
        self.assertEqual(1, runtime.snapshot()["overdue_source_tasks"])

        self.assertFalse(runtime.resolve_source_overdue(first))
        self.assertEqual(1, runtime.snapshot()["overdue_source_tasks"])

        self.assertTrue(runtime.resolve_source_overdue(second))
        self.assertEqual(0, runtime.snapshot()["overdue_source_tasks"])

    def test_foreign_overdue_tokens_never_move_the_gauge(self):
        # A stale token from an earlier request - or from another runtime - must
        # be inert, so no gauge can be driven below zero.
        runtime = make_runtime()
        other = make_runtime()
        foreign = other.mark_source_overdue()

        for token in (None, "token", 0, object(), foreign):
            with self.subTest(token=token):
                self.assertFalse(runtime.resolve_source_overdue(token))

        self.assertEqual(0, runtime.snapshot()["overdue_source_tasks"])
        self.assertEqual(1, other.snapshot()["overdue_source_tasks"])

    def test_cache_counters_are_recorded(self):
        runtime = make_runtime()

        runtime.record_cache_hit()
        runtime.record_cache_hit()
        runtime.record_cache_miss()
        runtime.record_cache_eviction()
        runtime.record_coalesced_waiter()

        snapshot = runtime.snapshot()
        self.assertEqual(2, snapshot["cache_hits"])
        self.assertEqual(1, snapshot["cache_misses"])
        self.assertEqual(1, snapshot["cache_evictions"])
        self.assertEqual(1, snapshot["coalesced_waiters"])

    def test_injected_memory_readings_are_merged_into_the_snapshot(self):
        runtime = make_runtime({"rss_kib": 4096, "pss_kib": 2048, "threads": 12})

        snapshot = runtime.snapshot()

        self.assertEqual(4096, snapshot["rss_kib"])
        self.assertEqual(2048, snapshot["pss_kib"])
        self.assertEqual(12, snapshot["threads"])

    def test_missing_memory_readings_stay_none(self):
        runtime = make_runtime({"rss_kib": 4096})

        snapshot = runtime.snapshot()

        self.assertEqual(4096, snapshot["rss_kib"])
        self.assertIsNone(snapshot["pss_kib"])
        self.assertIsNone(snapshot["threads"])

    def test_unknown_memory_reader_keys_are_ignored(self):
        runtime = make_runtime({"rss_kib": 4096, "process_command_line": "secret"})

        self.assertEqual(SNAPSHOT_KEYS, set(runtime.snapshot()))

    def test_snapshot_survives_a_raising_memory_reader(self):
        # snapshot() is called from inside the fan-out; a /proc read that blows
        # up has to degrade to "unknown" instead of aborting a search request.
        def explode():
            raise OSError("smaps_rollup vanished")

        runtime = SearchRuntime(clock=lambda: 100.0, memory_reader=explode)

        with runtime.request(category_count=1, family_count=1):
            snapshot = runtime.snapshot()

        self.assertEqual(SNAPSHOT_KEYS, set(snapshot))
        self.assertIsNone(snapshot["rss_kib"])
        self.assertIsNone(snapshot["pss_kib"])
        self.assertIsNone(snapshot["threads"])
        self.assertEqual(1, snapshot["active_requests"])

    def test_non_mapping_memory_readings_are_ignored(self):
        for readings in (None, [("rss_kib", 4096)], "rss_kib=4096", 42):
            with self.subTest(readings=readings):
                runtime = SearchRuntime(
                    clock=lambda: 100.0, memory_reader=lambda value=readings: value
                )

                snapshot = runtime.snapshot()

                self.assertEqual(SNAPSHOT_KEYS, set(snapshot))
                self.assertIsNone(snapshot["rss_kib"])
                self.assertIsNone(snapshot["pss_kib"])
                self.assertIsNone(snapshot["threads"])

    def test_non_integer_memory_readings_are_dropped(self):
        # Only int-or-None may reach the fixed memory fields: a string or float
        # reading would put unbounded, reader-controlled text into the logs, and
        # bool is an int subclass that is never a KiB or thread count.
        runtime = make_runtime(
            {"rss_kib": "4096 kB", "pss_kib": 2048.5, "threads": True}
        )

        snapshot = runtime.snapshot()

        self.assertIsNone(snapshot["rss_kib"])
        self.assertIsNone(snapshot["pss_kib"])
        self.assertIsNone(snapshot["threads"])

    def test_runtime_has_no_global_reset_hook(self):
        # A public reset() lets one caller zero the counters while another
        # thread sits inside request()/source_task(), whose finally block then
        # decrements a fresh zero into a negative gauge. Tests build their own
        # SearchRuntime (or patch the singleton) instead.
        self.assertFalse(hasattr(make_runtime(), "reset"))

    def test_concurrent_source_tasks_overlap_and_are_counted_exactly(self):
        # The fan-out runs one worker per source, so every counter update has to
        # survive concurrent threads without losing increments. Holding all
        # workers inside source_task() at the same time also proves the peak
        # gauge records the real high-water mark instead of a serialized 1.
        runtime = make_runtime()
        workers = 8
        entered = Barrier(workers, timeout=15)
        failures = []

        def work():
            try:
                with runtime.source_task():
                    entered.wait()
                    runtime.record_source_outcome("completed")
            except Exception as error:
                failures.append(error)

        threads = [Thread(target=work) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)

        self.assertEqual([], failures)
        self.assertEqual([], [thread for thread in threads if thread.is_alive()])
        snapshot = runtime.snapshot()
        self.assertEqual(workers, snapshot["source_started"])
        self.assertEqual(workers, snapshot["source_completed"])
        self.assertEqual(workers, snapshot["peak_active_source_tasks"])
        self.assertEqual(0, snapshot["active_source_tasks"])

    def test_module_singleton_is_a_search_runtime(self):
        self.assertIsInstance(search_runtime, SearchRuntime)


class ProcessMemoryTests(unittest.TestCase):
    def test_non_linux_platforms_report_no_readings(self):
        with patch("quasarr.search.runtime.sys.platform", "win32"):
            self.assertEqual(
                {"rss_kib": None, "pss_kib": None, "threads": None},
                read_process_memory(),
            )

    def test_proc_fields_are_parsed_by_name(self):
        lines = [
            "Name:\tpython3\n",
            "VmRSS:\t  123456 kB\n",
            "Threads:\t17\n",
        ]

        self.assertEqual(
            {"rss_kib": 123456, "threads": 17},
            _parse_proc_fields(lines, {"VmRSS": "rss_kib", "Threads": "threads"}),
        )

    def test_unparsable_proc_values_are_skipped(self):
        lines = ["VmRSS:\tunknown kB\n", "Threads:\n", "Pss:\t42 kB\n"]

        self.assertEqual(
            {"pss_kib": 42},
            _parse_proc_fields(
                lines,
                {"VmRSS": "rss_kib", "Threads": "threads", "Pss": "pss_kib"},
            ),
        )

    def test_unreadable_proc_file_yields_no_fields(self):
        # smaps_rollup is absent on some kernels and containers; a missing file
        # must degrade to "unknown", never raise into a search request. The
        # module's own open() is patched so the suite never touches disk.
        with patch("quasarr.search.runtime.open", side_effect=OSError("denied")):
            self.assertEqual(
                {},
                _read_proc_fields("/proc/self/smaps_rollup", {"Pss": "pss_kib"}),
            )

    def test_linux_readings_are_collected_from_both_proc_files(self):
        with (
            patch("quasarr.search.runtime.sys.platform", "linux"),
            patch("quasarr.search.runtime.open", fake_proc_opener(PROC_FILES)),
        ):
            self.assertEqual(
                {"rss_kib": 123456, "pss_kib": 65432, "threads": 17},
                read_process_memory(),
            )

    def test_partial_proc_readings_leave_the_rest_unknown(self):
        available = {"/proc/self/status": PROC_FILES["/proc/self/status"]}

        with (
            patch("quasarr.search.runtime.sys.platform", "linux"),
            patch("quasarr.search.runtime.open", fake_proc_opener(available)),
        ):
            self.assertEqual(
                {"rss_kib": 123456, "pss_kib": None, "threads": 17},
                read_process_memory(),
            )


if __name__ == "__main__":
    unittest.main()
