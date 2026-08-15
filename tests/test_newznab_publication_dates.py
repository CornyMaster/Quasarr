import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from wsgiref.util import setup_testing_defaults
from xml.etree import ElementTree

from bottle import Bottle

from quasarr.api.arr import _stable_publication_dates, setup_arr_routes
from quasarr.providers import shared_state
from quasarr.search.runtime import SearchRuntime
from quasarr.storage.sqlite_database import DataBase


class NewznabPublicationDateTests(unittest.TestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self._previous_values = shared_state.values
        shared_state.values = {
            "dbfile": str(Path(self._temporary_directory.name) / "Quasarr.db")
        }
        self.addCleanup(self._restore_shared_state)

    def _restore_shared_state(self):
        shared_state.values = self._previous_values

    def test_same_release_keeps_its_first_publication_date_across_app_restarts(self):
        dates = iter(
            (
                "Sat, 15 Aug 2026 12:00:00 +0000",
                "Sat, 15 Aug 2026 12:05:00 +0000",
            )
        )

        def search_results(*_args, **_kwargs):
            return [
                {
                    "details": {
                        "title": "Synthetic.Movie.2026.1080p-GROUP",
                        "date": next(dates),
                        "link": "https://downloads.invalid/release-one",
                        "source": "https://source.invalid/post/1",
                        "hostname": "AA",
                        "size": 1024,
                    }
                }
            ]

        runtime = SearchRuntime(memory_reader=lambda: {})
        with (
            patch("quasarr.api.arr.get_search_results", side_effect=search_results),
            patch("quasarr.api.arr.search_runtime", runtime),
        ):
            first = self._publication_date(self._new_app())
            second = self._publication_date(self._new_app())

        self.assertEqual("Sat, 15 Aug 2026 12:00:00 +0000", first)
        self.assertEqual(first, second)

    def test_every_release_crossing_a_storage_batch_keeps_a_private_identity(self):
        releases = [
            {
                "details": {
                    "title": f"Synthetic.Movie.{index:04d}.1080p-GROUP",
                    "date": "Sat, 15 Aug 2026 12:00:00 +0000",
                    "link": f"https://downloads.invalid/release-{index}",
                    "source": f"https://source.invalid/post/{index}",
                    "hostname": "AA",
                    "size": 1024,
                }
            }
            for index in range(501)
        ]
        runtime = SearchRuntime(memory_reader=lambda: {})

        with (
            patch("quasarr.api.arr.get_search_results", return_value=releases),
            patch("quasarr.api.arr.search_runtime", runtime),
        ):
            publication_dates = self._publication_dates(self._new_app())

        self.assertEqual(501, len(publication_dates))
        stored = DataBase("newznab_publication_dates").retrieve_all_titles()
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(501, len(stored))
        serialized = repr(stored)
        self.assertNotIn("downloads.invalid", serialized)
        self.assertNotIn("Synthetic.Movie", serialized)

    def test_a_fresh_python_process_reuses_the_date_from_sqlite(self):
        link = "https://downloads.invalid/persisted-release"
        first = _stable_publication_dates(
            [self._release(link, "Sat, 15 Aug 2026 12:00:00 +0000")],
            "Sat, 15 Aug 2026 12:00:00 +0000",
        )[link]
        script = """
import json
import sys
from quasarr.api.arr import _stable_publication_dates
from quasarr.providers import shared_state

shared_state.values = {"dbfile": sys.argv[1]}
link = "https://downloads.invalid/persisted-release"
release = {"details": {"link": link, "date": "Sat, 15 Aug 2026 12:05:00 +0000"}}
print(json.dumps(_stable_publication_dates([release], "fallback")[link]))
"""

        child = subprocess.run(
            [sys.executable, "-c", script, shared_state.values["dbfile"]],
            cwd=Path(__file__).parents[1],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual("Sat, 15 Aug 2026 12:00:00 +0000", first)
        self.assertEqual(first, json.loads(child.stdout))

    def test_concurrent_first_sightings_return_one_committed_date(self):
        link = "https://downloads.invalid/concurrent-release"
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def resolve(date):
            try:
                barrier.wait()
                results.append(
                    _stable_publication_dates(
                        [self._release(link, date)],
                        "fallback",
                    )[link]
                )
            except Exception as error:
                errors.append(error)

        threads = [
            threading.Thread(
                target=resolve,
                args=(f"Sat, 15 Aug 2026 12:0{minute}:00 +0000",),
            )
            for minute in (0, 5)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([], errors)
        self.assertEqual(1, len(set(results)))
        stored = DataBase("newznab_publication_dates").retrieve_all_titles()
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(results[0], stored[0][1])

    def test_iso_source_date_is_canonicalized_to_rfc822(self):
        link = "https://downloads.invalid/iso-release"

        resolved = _stable_publication_dates(
            [self._release(link, "2026-08-15T12:00:00+00:00")],
            "Sat, 15 Aug 2026 13:00:00 +0000",
        )

        self.assertEqual("Sat, 15 Aug 2026 12:00:00 +0000", resolved[link])
        stored = DataBase("newznab_publication_dates").retrieve_all_titles()
        self.assertEqual(
            [[hashlib.sha256(link.encode("utf-8")).hexdigest(), resolved[link]]],
            stored,
        )

    def test_malformed_source_and_stored_values_are_replaced_by_safe_fallback(self):
        link = "https://downloads.invalid/malformed-release"
        key = hashlib.sha256(link.encode("utf-8")).hexdigest()
        database = DataBase("newznab_publication_dates")
        database.update_store(key, "https://attacker.invalid/persisted-secret")
        fallback = "Sat, 15 Aug 2026 13:00:00 +0000"

        resolved = _stable_publication_dates(
            [self._release(link, "https://source.invalid/not-a-date")],
            fallback,
        )

        self.assertEqual(fallback, resolved[link])
        self.assertEqual(fallback, database.retrieve(key))
        self.assertNotIn("invalid", repr(database.retrieve_all_titles()))

    def test_none_source_date_reuses_the_persisted_date_in_the_xml(self):
        link = "https://downloads.invalid/none-date-release"
        persisted = "Sat, 15 Aug 2026 12:00:00 +0000"
        _stable_publication_dates([self._release(link, persisted)], persisted)
        release = self._complete_release(link, None)
        runtime = SearchRuntime(memory_reader=lambda: {})

        with (
            patch("quasarr.api.arr.get_search_results", return_value=[release]),
            patch("quasarr.api.arr.search_runtime", runtime),
        ):
            publication_date = self._publication_date(self._new_app())

        self.assertEqual(persisted, publication_date)

    def test_missing_source_date_uses_the_utc_request_time(self):
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is timezone.utc:
                    return cls(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
                return cls(2026, 8, 15, 14, 0)

        link = "https://downloads.invalid/utc-fallback-release"
        release = self._complete_release(link, None)
        runtime = SearchRuntime(memory_reader=lambda: {})

        with (
            patch("quasarr.api.arr.get_search_results", return_value=[release]),
            patch("quasarr.api.arr.search_runtime", runtime),
            patch("quasarr.api.arr.datetime", FixedDateTime),
        ):
            publication_date = self._publication_date(self._new_app())

        self.assertEqual("Sat, 15 Aug 2026 12:00:00 +0000", publication_date)

    def test_source_date_overflow_during_utc_conversion_uses_safe_fallback(self):
        link = "https://downloads.invalid/source-overflow-release"
        fallback = "Sat, 15 Aug 2026 13:00:00 +0000"

        resolved = _stable_publication_dates(
            [self._release(link, "9999-12-31T23:59:59-23:59")],
            fallback,
        )

        self.assertEqual(fallback, resolved[link])

    def test_stored_date_overflow_during_utc_conversion_self_heals(self):
        link = "https://downloads.invalid/stored-overflow-release"
        key = hashlib.sha256(link.encode("utf-8")).hexdigest()
        database = DataBase("newznab_publication_dates")
        database.update_store(key, "9999-12-31T23:59:59-23:59")
        fallback = "Sat, 15 Aug 2026 13:00:00 +0000"

        resolved = _stable_publication_dates(
            [self._release(link, fallback)],
            fallback,
        )

        self.assertEqual(fallback, resolved[link])
        self.assertEqual(fallback, database.retrieve(key))

    @staticmethod
    def _release(link, date):
        return {"details": {"link": link, "date": date}}

    @staticmethod
    def _complete_release(link, date):
        return {
            "details": {
                "title": "Synthetic.Movie.2026.1080p-GROUP",
                "date": date,
                "link": link,
                "source": "https://source.invalid/post/1",
                "hostname": "AA",
                "size": 1024,
            }
        }

    def _new_app(self):
        app = Bottle()
        setup_arr_routes(app)
        return app

    def _publication_date(self, app):
        return self._publication_dates(app)[0]

    def _publication_dates(self, app):
        environ = {}
        setup_testing_defaults(environ)
        environ.update(
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/api",
                "QUERY_STRING": "t=movie&cat=2000&imdbid=tt0000013",
                "HTTP_USER_AGENT": "Radarr/5.0",
                "wsgi.input": BytesIO(b""),
            }
        )
        captured = {}

        def start_response(status, headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = headers

        body = b"".join(app(environ, start_response)).decode("utf-8")
        self.assertEqual("200 OK", captured["status"])
        return [
            element.text
            for element in ElementTree.fromstring(body).findall(".//item/pubDate")
        ]


if __name__ == "__main__":
    unittest.main()
