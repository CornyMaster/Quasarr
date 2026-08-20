import unittest
from threading import Barrier, Thread
from unittest.mock import patch

from quasarr.search.cache import SearchCache
from quasarr.search.runtime import SearchRuntime


class FakeClock:
    """Stand-in for time.time() so TTL tests are deterministic and never sleep."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def releases(count):
    """Synthetic release list of the shape the search fan-out caches."""
    return [{"details": {"title": f"Release.{index}"}} for index in range(count)]


def counts(cache):
    stats = cache.stats()
    return {
        "entry_count": stats["entry_count"],
        "release_count": stats["release_count"],
    }


class SearchCacheTests(unittest.TestCase):
    def setUp(self):
        # The cache reports through the module singleton, so every test injects
        # its own runtime: counter assertions stay exact and the real process
        # singleton is never mutated by the suite.
        self.runtime = SearchRuntime(memory_reader=lambda: {})
        patcher = patch("quasarr.search.cache.search_runtime", self.runtime)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = FakeClock()

    def counters(self):
        snapshot = self.runtime.snapshot()
        return (
            snapshot["cache_hits"],
            snapshot["cache_misses"],
            snapshot["cache_evictions"],
        )

    def test_a_value_is_returned_with_its_absolute_expiry_until_the_ttl_runs_out(self):
        # run_all() turns the second element into the remaining TTL it reports,
        # so it has to stay an absolute point in time, not a duration.
        cache = SearchCache(clock=self.clock)
        value = releases(2)

        cache.set("a", value, ttl=300)

        self.assertEqual((value, 1300.0), cache.get("a"))
        self.clock.advance(299)
        self.assertEqual((value, 1300.0), cache.get("a"))
        # Expiry is exclusive: an entry is stale the instant its deadline is hit.
        self.clock.advance(1)
        self.assertEqual((None, 0), cache.get("a"))

    def test_the_default_ttl_is_still_five_minutes(self):
        cache = SearchCache(clock=self.clock)

        cache.set("a", releases(1))

        self.assertEqual(1300.0, cache.get("a")[1])

    def test_a_feed_ttl_is_preserved_independently_of_the_search_ttl(self):
        # Feeds cache for 60s and searches for 300s; one shared cache must keep
        # both, so a feed entry may never inherit the search TTL.
        cache = SearchCache(clock=self.clock)

        cache.set("feed", releases(1), ttl=60)
        cache.set("search", releases(1), ttl=300)

        self.assertEqual(1060.0, cache.get("feed")[1])
        self.assertEqual(1300.0, cache.get("search")[1])

    def test_a_missing_key_reports_a_miss_without_storing_anything(self):
        cache = SearchCache(clock=self.clock)

        self.assertEqual((None, 0), cache.get("absent"))

        self.assertEqual(0, cache.stats()["entry_count"])
        self.assertEqual((0, 1, 0), self.counters())

    def test_an_expired_entry_is_dropped_on_read_and_frees_its_releases(self):
        cache = SearchCache(clock=self.clock)
        cache.set("a", releases(3), ttl=300)

        self.clock.advance(301)

        self.assertEqual((None, 0), cache.get("a"))
        self.assertEqual({"entry_count": 0, "release_count": 0}, counts(cache))
        # Expiry is not an eviction: only a capacity limit evicts a live entry.
        self.assertEqual((0, 1, 0), self.counters())

    def test_mutating_the_written_list_afterwards_cannot_change_what_is_retained(self):
        # The caller keeps its own list after handing it over. If the cache kept
        # that same object, appending to it would grow a retained entry behind
        # the accounting and past the release bound.
        cache = SearchCache(clock=self.clock)
        value = releases(1)
        cache.set("a", value, ttl=300)

        value.append({"details": {"title": "Smuggled.In"}})

        self.assertEqual(1, len(cache.get("a")[0]))
        self.assertEqual({"entry_count": 1, "release_count": 1}, counts(cache))

    def test_mutating_a_returned_list_cannot_change_what_is_retained(self):
        cache = SearchCache(clock=self.clock)
        cache.set("a", releases(1), ttl=300)

        handed_out, _ = cache.get("a")
        handed_out.append({"details": {"title": "Smuggled.In"}})

        self.assertEqual(1, len(cache.get("a")[0]))
        self.assertEqual({"entry_count": 1, "release_count": 1}, counts(cache))

    def test_appending_after_a_write_cannot_push_the_cache_past_its_bound(self):
        # The bound has to hold against what is actually reachable from the
        # cache, not only against what was counted at write time.
        cache = SearchCache(max_entries=4, max_releases=3, clock=self.clock)
        value = releases(2)
        cache.set("a", value, ttl=300)

        value.extend(releases(50))
        cache.set("b", releases(1), ttl=300)

        retained = sum(len(entry.value) for entry in cache.cache.values())
        self.assertLessEqual(retained, 3)
        self.assertEqual(retained, cache.stats()["release_count"])

    def test_each_read_hands_out_its_own_list_of_the_same_release_objects(self):
        cache = SearchCache(clock=self.clock)
        value = releases(1)
        cache.set("a", value, ttl=300)

        first, _ = cache.get("a")
        second, _ = cache.get("a")

        self.assertIsInstance(first, list)
        self.assertIsNot(first, second)
        # Only the list is owned by the cache: the releases inside it are never
        # mutated by the fan-out, and copying every dict on each read would cost
        # more than the cache saves.
        self.assertIs(value[0], first[0])

    def test_reading_an_entry_makes_it_the_most_recent_one(self):
        # "a" survives the third insert only because reading it moved it
        # behind "b" in LRU order.
        cache = SearchCache(max_entries=2, max_releases=3, clock=self.clock)
        cache.set("a", [{"id": 1}, {"id": 2}], ttl=300)
        cache.set("b", [{"id": 3}], ttl=300)

        cache.get("a")
        cache.set("c", [{"id": 4}], ttl=300)

        self.assertEqual((None, 0), cache.get("b"))
        self.assertIsNotNone(cache.get("a")[0])
        self.assertLessEqual(cache.stats()["release_count"], 3)
        self.assertEqual(3, cache.stats()["release_count"])
        self.assertEqual(2, cache.stats()["entry_count"])

    def test_the_entry_limit_evicts_the_least_recently_used_entries(self):
        cache = SearchCache(max_entries=2, max_releases=1000, clock=self.clock)

        cache.set("a", releases(1), ttl=300)
        cache.set("b", releases(1), ttl=300)
        cache.set("c", releases(1), ttl=300)

        self.assertEqual((None, 0), cache.get("a"))
        self.assertEqual(2, cache.stats()["entry_count"])
        self.assertEqual(1, self.runtime.snapshot()["cache_evictions"])

    def test_the_release_limit_evicts_even_while_entry_room_is_left(self):
        # Entry count alone does not bound memory: one source can answer with
        # thousands of releases, so the release total is its own limit.
        cache = SearchCache(max_entries=10, max_releases=4, clock=self.clock)

        cache.set("a", releases(3), ttl=300)
        cache.set("b", releases(3), ttl=300)

        self.assertEqual((None, 0), cache.get("a"))
        self.assertEqual(3, cache.stats()["release_count"])
        self.assertEqual(1, cache.stats()["entry_count"])
        self.assertEqual(1, self.runtime.snapshot()["cache_evictions"])

    def test_a_value_bigger_than_the_release_limit_leaves_the_cache_empty(self):
        # Documented bound: the limits always hold afterwards, even when that
        # means the oversized write itself is dropped again.
        cache = SearchCache(max_entries=10, max_releases=2, clock=self.clock)
        cache.set("a", releases(1), ttl=300)

        cache.set("huge", releases(5), ttl=300)

        self.assertEqual((None, 0), cache.get("huge"))
        self.assertEqual({"entry_count": 0, "release_count": 0}, counts(cache))

    def test_replacing_a_key_replaces_its_release_accounting(self):
        # A re-run source overwrites its own key. Adding the new count without
        # releasing the old one would starve the cache after a few refreshes.
        cache = SearchCache(max_entries=4, max_releases=10, clock=self.clock)

        cache.set("a", releases(4), ttl=300)
        cache.set("a", releases(1), ttl=300)

        self.assertEqual({"entry_count": 1, "release_count": 1}, counts(cache))
        self.assertEqual(0, self.runtime.snapshot()["cache_evictions"])

    def test_an_empty_result_occupies_one_entry_and_no_releases(self):
        # An empty answer is a real cached result: it keeps the source from
        # being asked again, while costing nothing against the release budget.
        cache = SearchCache(max_entries=4, max_releases=2, clock=self.clock)

        cache.set("a", [], ttl=300)
        cache.set("b", releases(2), ttl=300)

        self.assertEqual(([], 1300.0), cache.get("a"))
        self.assertEqual({"entry_count": 2, "release_count": 2}, counts(cache))
        self.assertEqual(0, self.runtime.snapshot()["cache_evictions"])

    def test_expired_entries_are_removed_before_a_live_entry_is_evicted(self):
        # Nothing useful is thrown away while stale entries still hold the room.
        cache = SearchCache(max_entries=2, max_releases=1000, clock=self.clock)
        cache.set("stale", releases(1), ttl=60)
        cache.set("fresh", releases(1), ttl=300)

        self.clock.advance(61)
        cache.set("new", releases(1), ttl=300)

        self.assertIsNotNone(cache.get("fresh")[0])
        self.assertIsNotNone(cache.get("new")[0])
        self.assertEqual(2, cache.stats()["entry_count"])
        self.assertEqual(0, self.runtime.snapshot()["cache_evictions"])

    def test_sweep_removes_only_expired_entries_and_reports_the_count(self):
        cache = SearchCache(clock=self.clock)
        cache.set("stale", releases(2), ttl=60)
        cache.set("fresh", releases(1), ttl=300)

        self.clock.advance(61)

        self.assertEqual(1, cache.sweep())
        self.assertEqual({"entry_count": 1, "release_count": 1}, counts(cache))
        self.assertEqual(0, cache.sweep())

    def test_sweep_accepts_an_explicit_time(self):
        cache = SearchCache(clock=self.clock)
        cache.set("a", releases(1), ttl=300)

        self.assertEqual(0, cache.sweep(now=1299.0))
        self.assertEqual(1, cache.sweep(now=1300.0))

    def test_clear_drops_every_entry_and_reports_how_many(self):
        cache = SearchCache(clock=self.clock)
        cache.set("a", releases(2), ttl=300)
        cache.set("b", releases(1), ttl=300)

        self.assertEqual(2, cache.clear())
        self.assertEqual({"entry_count": 0, "release_count": 0}, counts(cache))
        self.assertEqual(0, cache.clear())

    def test_hits_and_misses_are_reported_to_the_runtime(self):
        cache = SearchCache(clock=self.clock)
        cache.set("a", releases(1), ttl=300)

        cache.get("a")
        cache.get("a")
        cache.get("b")

        self.assertEqual((2, 1, 0), self.counters())

    def test_stats_expose_the_configured_limits(self):
        cache = SearchCache(max_entries=7, max_releases=11, clock=self.clock)

        stats = cache.stats()

        self.assertEqual(7, stats["max_entries"])
        self.assertEqual(11, stats["max_releases"])

    def test_the_shipped_limits_are_the_documented_ones(self):
        stats = SearchCache().stats()

        self.assertEqual(2048, stats["max_entries"])
        self.assertEqual(50000, stats["max_releases"])

    def test_concurrent_writers_keep_the_release_total_and_the_limits_honest(self):
        # The accounting is incremental, so a lost update would drift forever:
        # the total must always describe exactly what is still retained.
        cache = SearchCache(max_entries=8, max_releases=24, clock=self.clock)
        barrier = Barrier(4)
        failures = []

        def worker(worker_id):
            try:
                barrier.wait(10)
                for index in range(30):
                    key = f"{worker_id}-{index % 5}"
                    cache.set(key, releases(index % 4), ttl=300)
                    cache.get(key)
            except Exception as exc:
                failures.append(exc)

        threads = [Thread(target=worker, args=(worker_id,)) for worker_id in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)

        # A worker that raised or never finished would otherwise leave the
        # invariants below asserted against a cache nobody raced.
        self.assertEqual([], [repr(exc) for exc in failures])
        self.assertEqual([], [thread.name for thread in threads if thread.is_alive()])

        stats = cache.stats()
        retained = list(cache.cache.values())
        self.assertLessEqual(stats["entry_count"], 8)
        self.assertLessEqual(stats["release_count"], 24)
        self.assertEqual(len(retained), stats["entry_count"])
        self.assertEqual(
            sum(len(entry.value) for entry in retained), stats["release_count"]
        )


if __name__ == "__main__":
    unittest.main()
