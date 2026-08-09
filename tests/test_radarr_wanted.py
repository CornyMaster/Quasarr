import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from quasarr.providers import radarr_api


class FakeClient:
    """Single-page client: all records on page 1, empty thereafter."""

    def __init__(self, missing, cutoff):
        self._records = {"missing": missing, "cutoff": cutoff}

    def wanted(self, kind, page=1, page_size=50, timeout=None):
        return {"records": self._records.get(kind, []) if page == 1 else []}


class PagedClient:
    """Client keyed by (kind, page) to exercise pagination."""

    def __init__(self, pages):
        self._pages = pages

    def wanted(self, kind, page=1, page_size=50, timeout=None):
        return {"records": self._pages.get((kind, page), [])}


def shared_state_with(client):
    return SimpleNamespace(values={"radarr_client": client})


class RadarrWantedTests(unittest.TestCase):
    def test_skips_unreleased_and_keeps_missing_first(self):
        missing = [
            {"imdbId": "tt1", "status": "announced"},  # not in cinemas yet -> skip
            {"imdbId": "tt2", "status": "inCinemas"},
            {"imdbId": "tt3", "status": "released"},
            {"imdbId": "tt4", "status": "tba"},  # skip
        ]
        cutoff = [
            {"imdbId": "tt3", "status": "released"},  # dupe of missing -> drop
            {"imdbId": "tt5", "status": "released"},
        ]
        ids = radarr_api.get_wanted_imdb_ids(
            shared_state_with(FakeClient(missing, cutoff))
        )
        self.assertEqual(ids, ["tt2", "tt3", "tt5"])

    def test_limit_caps_results(self):
        missing = [{"imdbId": f"tt{i}", "status": "released"} for i in range(10)]
        ids = radarr_api.get_wanted_imdb_ids(
            shared_state_with(FakeClient(missing, [])), limit=3
        )
        self.assertEqual(ids, ["tt0", "tt1", "tt2"])

    def test_pages_past_an_all_unreleased_page(self):
        # Page 1 is entirely announced; the released entry on page 2 must still
        # be picked up rather than yielding an empty seed.
        pages = {
            ("missing", 1): [
                {"imdbId": "tt1", "status": "announced"},
                {"imdbId": "tt2", "status": "tba"},
            ],
            ("missing", 2): [{"imdbId": "tt3", "status": "released"}],
        }
        ids = radarr_api.get_wanted_imdb_ids(
            shared_state_with(PagedClient(pages)), limit=5
        )
        self.assertEqual(ids, ["tt3"])

    def test_paging_stops_at_the_caller_deadline(self):
        # Every page is its own Radarr request, so a caller that is itself under
        # a wall-clock budget must not have it spent on paging alone.
        pages = {
            ("missing", 1): [{"imdbId": "tt1", "status": "announced"}],
            ("missing", 2): [{"imdbId": "tt2", "status": "released"}],
        }
        ids = radarr_api.get_wanted_imdb_ids(
            shared_state_with(PagedClient(pages)),
            limit=5,
            deadline=time.time() - 1,
        )
        self.assertEqual(ids, [])

    def test_page_request_timeout_is_clamped_to_the_deadline(self):
        # The deadline check passes just before the last page, so that request
        # must carry the time that is left rather than the client's full timeout.
        class TimeoutRecordingClient:
            def __init__(self):
                self.timeouts = []

            def wanted(self, kind, page=1, page_size=50, timeout=None):
                self.timeouts.append(timeout)
                return {"records": []}

        client = TimeoutRecordingClient()
        radarr_api.get_wanted_imdb_ids(
            shared_state_with(client), limit=5, deadline=time.time() + 2
        )
        self.assertTrue(client.timeouts)
        self.assertLessEqual(client.timeouts[0], 2)
        self.assertGreater(client.timeouts[0], 0)

    def test_a_caller_timeout_only_tightens_the_client_timeout(self):
        # The deadline says how much of the caller's budget is left, never that
        # this request may take longer than the client allows.
        client = radarr_api.RadarrAPIClient("http://arr.invalid", "key")
        seen = []

        def fake_get(url, headers=None, params=None, timeout=None):
            seen.append(timeout)
            raise RuntimeError("no network in tests")

        with patch("quasarr.providers.radarr_api.requests.get", fake_get):
            client.wanted("missing", timeout=600)
            client.wanted("missing", timeout=2)
            client.wanted("missing")

        self.assertEqual([10, 2, 10], seen)

    def test_a_failed_page_reports_partial_paging(self):
        # A failed page request is not the end of the list. Callers that persist
        # progress must be able to tell "that was everything" from "that was as
        # far as I got".
        class FailingSecondPage:
            def wanted(self, kind, page=1, page_size=50, timeout=None):
                if page == 1:
                    return {"records": [{"imdbId": "tt1", "status": "released"}]}
                return None  # request failed

        status = {}
        radarr_api.get_wanted_imdb_ids(
            shared_state_with(FailingSecondPage()), limit=50, status=status
        )
        self.assertFalse(status["complete"])

    def test_a_complete_walk_reports_complete(self):
        status = {}
        radarr_api.get_wanted_imdb_ids(
            shared_state_with(
                FakeClient([{"imdbId": "tt1", "status": "released"}], [])
            ),
            limit=50,
            status=status,
        )
        self.assertTrue(status["complete"])

    def test_empty_without_client(self):
        self.assertEqual(radarr_api.get_wanted_imdb_ids(SimpleNamespace(values={})), [])


if __name__ == "__main__":
    unittest.main()
