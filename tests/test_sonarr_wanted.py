import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from quasarr.providers import sonarr_api

PAST = "2000-01-01T00:00:00Z"
FUTURE = "2999-01-01T00:00:00Z"


def ep(imdb, season, episode, air):
    return {
        "series": {"imdbId": imdb},
        "seasonNumber": season,
        "episodeNumber": episode,
        "airDateUtc": air,
    }


class FakeClient:
    """Single-page client: records on page 1, empty thereafter."""

    def __init__(self, missing, cutoff):
        self._records = {"missing": missing, "cutoff": cutoff}

    def wanted(self, kind, page=1, page_size=50, timeout=None):
        return {"records": self._records.get(kind, []) if page == 1 else []}


class PagedClient:
    def __init__(self, pages):
        self._pages = pages

    def wanted(self, kind, page=1, page_size=50, timeout=None):
        return {"records": self._pages.get((kind, page), [])}


def shared_state_with(client):
    return SimpleNamespace(values={"sonarr_client": client})


class SonarrWantedTests(unittest.TestCase):
    def test_skips_unaired_and_undated(self):
        missing = [
            ep("tt1", 1, 1, PAST),
            ep("tt2", 1, 2, FUTURE),  # not aired yet -> skip
            ep("tt3", 1, 3, None),  # no air date -> skip
            ep("tt4", 2, 1, PAST),
        ]
        got = sonarr_api.get_wanted_episodes(shared_state_with(FakeClient(missing, [])))
        self.assertEqual(
            got,
            [
                {"imdb_id": "tt1", "season": 1, "episode": 1},
                {"imdb_id": "tt4", "season": 2, "episode": 1},
            ],
        )

    def test_pages_past_unaired(self):
        pages = {
            ("missing", 1): [ep("tt1", 1, 1, FUTURE), ep("tt2", 1, 2, None)],
            ("missing", 2): [ep("tt9", 3, 3, PAST)],
        }
        got = sonarr_api.get_wanted_episodes(
            shared_state_with(PagedClient(pages)), limit=5
        )
        self.assertEqual(got, [{"imdb_id": "tt9", "season": 3, "episode": 3}])

    def test_paging_stops_at_the_caller_deadline(self):
        # Every page is its own Sonarr request, so a caller that is itself under
        # a wall-clock budget must not have it spent on paging alone.
        pages = {
            ("missing", 1): [ep("tt1", 1, 1, FUTURE)],
            ("missing", 2): [ep("tt9", 3, 3, PAST)],
        }
        got = sonarr_api.get_wanted_episodes(
            shared_state_with(PagedClient(pages)),
            limit=5,
            deadline=time.time() - 1,
        )
        self.assertEqual(got, [])

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
        sonarr_api.get_wanted_episodes(
            shared_state_with(client), limit=5, deadline=time.time() + 2
        )
        self.assertTrue(client.timeouts)
        self.assertLessEqual(client.timeouts[0], 2)
        self.assertGreater(client.timeouts[0], 0)

    def test_a_caller_timeout_only_tightens_the_client_timeout(self):
        # The deadline says how much of the caller's budget is left, never that
        # this request may take longer than the client allows.
        client = sonarr_api.SonarrAPIClient("http://arr.invalid", "key")
        seen = []

        def fake_get(url, headers=None, params=None, timeout=None):
            seen.append(timeout)
            raise RuntimeError("no network in tests")

        with patch("quasarr.providers.sonarr_api.requests.get", fake_get):
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
                    return {"records": [ep("tt1", 1, 1, PAST)]}
                return None  # request failed

        status = {}
        sonarr_api.get_wanted_episodes(
            shared_state_with(FailingSecondPage()), limit=50, status=status
        )
        self.assertFalse(status["complete"])

    def test_a_complete_walk_reports_complete(self):
        status = {}
        sonarr_api.get_wanted_episodes(
            shared_state_with(FakeClient([ep("tt1", 1, 1, PAST)], [])),
            limit=50,
            status=status,
        )
        self.assertTrue(status["complete"])

    def test_empty_without_client(self):
        self.assertEqual(sonarr_api.get_wanted_episodes(SimpleNamespace(values={})), [])


if __name__ == "__main__":
    unittest.main()
