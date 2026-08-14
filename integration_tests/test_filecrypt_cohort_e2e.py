# -*- coding: utf-8 -*-

"""Combined SponsorsHelper/Quasarr Filecrypt cohort end-to-end harness.

This module is deliberately outside `tests/`: it is the only suite that needs
both repositories importable at once and is therefore never picked up by the
ordinary `unittest discover -s tests` gate. It runs inside the SponsorsHelper
image with `PYTHONPATH=/quasarr:/sponsorhelper`.

Both sides are the shipped ones. The real `rix.service.check_quasarr_process`
loop runs unmodified, and the `requests` double it is given does not answer
anything itself: it dispatches straight into the real Quasarr Bottle callbacks
in this process, which read and write a real SQLite database. Nothing is
compared against a recorded fixture and neither state machine is reimplemented
here - the only doubles are the clock, the CAPTCHA-handler status, the durable
state root, the JDownloader device, and a scripted Filecrypt adapter that
produces its verdicts through Quasarr-independent production code
(`rix.crypter_outcomes`). No socket is opened; the harness proves that by
making one an error for the duration of every loop.

The resulting trace is finally handed to the SponsorsHelper contract oracle,
which was written in the other repository and derives its expectations from the
trace alone.
"""

import importlib.util
import json
import os
import pathlib
import socket
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit

from bottle import Bottle, HTTPResponse
from loguru import logger
from rix import pending_results as pending_store
from rix import service
from rix.crypter_outcomes import (
    CrypterAccess,
    CrypterEvidence,
    classify_filecrypt_access,
    clear_metadata,
    defer_payload,
)


def _import_quasarr():
    """Import Quasarr into an interpreter SponsorsHelper already configured.

    Both repositories set up the one global `loguru` logger while they are
    imported: each removes the default sink by its id and registers the same
    custom `WARN` and `CRIT` levels. In production they are separate processes,
    so whichever runs does it once; in this harness the second importer would
    raise on both. Only that logger setup is bridged, and only for the duration
    of the import - nothing else of either side is touched, and every module
    below is the shipped one.
    """
    logger_class = type(logger)
    original_level, original_remove = logger_class.level, logger_class.remove

    def level(self, name, no=None, color=None, icon=None):
        try:
            return original_level(self, name, no, color, icon)
        except ValueError:
            return original_level(self, name)

    def remove(self, handler_id=None):
        try:
            return original_remove(self, handler_id)
        except ValueError:
            return None

    with (
        mock.patch.object(logger_class, "level", level),
        mock.patch.object(logger_class, "remove", remove),
    ):
        api = importlib.import_module("quasarr.api.sponsors_helper")
        return SimpleNamespace(
            api=api,
            setup_sponsors_helper_routes=api.setup_sponsors_helper_routes,
            shared_state=importlib.import_module("quasarr.providers.shared_state"),
            enumerate_filecrypt_candidates=importlib.import_module(
                "quasarr.providers.crypter_candidates"
            ).enumerate_filecrypt_candidates,
            CrypterCooldownService=importlib.import_module(
                "quasarr.providers.crypter_cooldowns"
            ).CrypterCooldownService,
            DataBase=importlib.import_module(
                "quasarr.storage.sqlite_database"
            ).DataBase,
        )


_quasarr = _import_quasarr()
sponsors_helper_api = _quasarr.api
setup_sponsors_helper_routes = _quasarr.setup_sponsors_helper_routes
provider_shared_state = _quasarr.shared_state
enumerate_filecrypt_candidates = _quasarr.enumerate_filecrypt_candidates
CrypterCooldownService = _quasarr.CrypterCooldownService
DataBase = _quasarr.DataBase

NOW = 1_700_000_000
CRYPTER = "filecrypt"
QUASARR_URL = "https://quasarr.invalid"
API_KEY = "synthetic-api-key"
HOSTER_URL = "https://hoster.invalid/file/1"
TITLE = "Synthetic.Release.2024.1080p"

DECRYPT_RULE = "/sponsors_helper/api/to_decrypt/"
DEFER_RULE = "/sponsors_helper/api/defer/"
ACCESS_RULE = "/sponsors_helper/api/crypter-access/"
DOWNLOAD_RULE = "/sponsors_helper/api/download/"
DISABLE_RULE = "/sponsors_helper/api/disable/"
FAIL_RULE = "/sponsors_helper/api/fail/"
MIRRORS_RULE = "/sponsors_helper/api/mirrors/<package_id>/"
CREDENTIALS_RULE = "/sponsors_helper/api/credentials/<hostname>/"

# The literal the other repository's oracle reads as "this exchange ran out",
# restated here so the harness owes the oracle nothing but the trace itself.
COMPLETION_EVENT = "complete"


def package(index):
    return f"Quasarr_movies_{index:032x}"


def filecrypt_url(index):
    return f"https://filecrypt.invalid/container/{index}"


def protected_blob(index):
    return json.dumps(
        {
            "title": f"{TITLE}.{index}",
            "password": "",
            "links": [[filecrypt_url(index), "he"]],
        }
    )


def contract_oracle():
    """The SponsorsHelper contract oracle, loaded from its own repository.

    Imported by path so this harness never depends on how the two `tests`
    directories happen to merge on `PYTHONPATH`.
    """
    import rix

    path = (
        pathlib.Path(rix.__file__).resolve().parent.parent
        / "tests"
        / "test_quasarr_cohort_contract.py"
    )
    if not path.is_file():
        raise unittest.SkipTest(f"SponsorsHelper contract module missing at {path}")
    name = "sponsorshelper_contract_oracle"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


class FakeClock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class SequentialIds:
    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1
        return f"{self.count:032x}"


class Response:
    """Exactly as much of a `requests` response as the helper reads."""

    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class FakeLinkgrabber:
    def __init__(self, device):
        self.device = device

    def add_links(self, params=None):
        self.device.add_links_calls.append(params)
        self.device.packages.append(
            {"uuid": len(self.device.packages) + 1, "comment": params[0]["comment"]}
        )
        return True

    def query_packages(self, params=None):
        return list(self.device.packages)

    def query_links(self, params=None):
        return []


class FakeDownloads:
    def __init__(self, device):
        self.device = device

    def query_packages(self, params=None):
        return []

    def query_links(self, params=None):
        return []


class FakeDevice:
    def __init__(self):
        self.packages = []
        self.add_links_calls = []
        self.linkgrabber = FakeLinkgrabber(self)
        self.downloads = FakeDownloads(self)


class QuasarrServer:
    """The shipped Quasarr routes, reachable exactly like an HTTP server."""

    def __init__(self, dbfile, clock, packages=5):
        self.clock = clock
        self.ids = SequentialIds()
        self.device = FakeDevice()
        self.trace = []
        self.calls = []
        # Answers the harness loses on the way back, by route path suffix.
        self.drop_next = {}
        self.values = {
            "dbfile": dbfile,
            "database": self.get_db,
            "crypter_block_mode": "defer",
            "crypter_cooldown_hours": 24,
            "helper_active": True,
            "helper_last_seen": 0,
            "external_address": "http://quasarr.invalid",
        }
        self._databases = {}
        app = Bottle()
        setup_sponsors_helper_routes(app)
        self._routes = {route.rule: route for route in app.routes}
        protected = self.get_db("protected")
        for index in range(1, packages + 1):
            protected.update_store(package(index), protected_blob(index))

    # --- storage -----------------------------------------------------------
    def get_db(self, table):
        if table not in self._databases:
            self._databases[table] = DataBase(table)
        return self._databases[table]

    def update(self, key, value):
        self.values[key] = value

    def run_device_request(self, request_name, request_fn, default=None):
        return request_fn(self.device)

    def close(self):
        for database in self._databases.values():
            database._conn.close()
        self._databases.clear()

    # --- inspection --------------------------------------------------------
    def decision_row(self):
        raw = self.get_db("crypter_cooldowns").retrieve(CRYPTER)
        return None if raw is None else json.loads(raw)

    def protected_ids(self):
        rows = self.get_db("protected").retrieve_all_titles() or ()
        return sorted(row[0] for row in rows)

    def inventory_size(self):
        rows = self.get_db("protected").retrieve_all_titles()
        return len(enumerate_filecrypt_candidates(rows).candidates)

    def events(self, name):
        return [entry for entry in self.trace if entry["event"] == name]

    # --- dispatch ----------------------------------------------------------
    def _clocked_service(self, state):
        return CrypterCooldownService(state, clock=self.clock)

    def _invoke(self, rule, payload, *args):
        route = self._routes[rule]
        with (
            mock.patch.object(sponsors_helper_api, "shared_state", self),
            mock.patch.object(sponsors_helper_api, "request", mock.Mock(json=payload)),
            mock.patch.object(
                sponsors_helper_api,
                "CrypterCooldownService",
                self._clocked_service,
            ),
            mock.patch.object(
                CrypterCooldownService, "_new_identifier", lambda _self: self.ids()
            ),
        ):
            try:
                body = route.callback(*args)
            except HTTPResponse as response:
                return self._response(response.status_code, response.body)
        if isinstance(body, HTTPResponse):
            return self._response(body.status_code, body.body)
        if isinstance(body, dict):
            return Response(200, body)
        return Response(200, None, text=str(body))

    @staticmethod
    def _response(status, raw):
        try:
            return Response(status, json.loads(raw))
        except (TypeError, ValueError):
            return Response(status, None, text="" if raw is None else str(raw))

    def _route(self, method, url, payload):
        path = urlsplit(url).path
        self.calls.append((method, path))
        lost = bool(self.drop_next.get(path))
        if lost:
            self.drop_next[path] -= 1
        response = self._dispatch(method, path, payload)
        if lost:
            # The server ran, committed and answered; only the answer is lost.
            # Anything the helper does next has to survive work already done.
            raise ConnectionError("synthetic lost response")
        return response

    def _dispatch(self, method, path, payload):
        if path.endswith("/sponsors_helper/api/to_decrypt/"):
            return self._to_decrypt(payload)
        if path.endswith("/sponsors_helper/api/defer/"):
            return self._report(DEFER_RULE, "defer", payload)
        if path.endswith("/sponsors_helper/api/crypter-access/"):
            return self._report(ACCESS_RULE, "access", payload)
        if path.endswith("/sponsors_helper/api/download/"):
            return self._terminal(DOWNLOAD_RULE, "download", payload)
        if path.endswith("/sponsors_helper/api/disable/"):
            return self._terminal(DISABLE_RULE, "disable", payload)
        if path.endswith("/sponsors_helper/api/fail/"):
            return self._terminal(FAIL_RULE, "fail", payload)
        if "/sponsors_helper/api/mirrors/" in path:
            return self._invoke(MIRRORS_RULE, None, path.split("/")[-2])
        if "/sponsors_helper/api/credentials/" in path:
            return self._invoke(CREDENTIALS_RULE, None, path.split("/")[-2])
        raise AssertionError(f"unexpected request {method} {path}")

    def _to_decrypt(self, payload):
        response = self._invoke(DECRYPT_RULE, payload)
        handout = (
            (response._payload or {}).get("to_decrypt") if response._payload else None
        )
        offer = (handout or {}).get("crypter_offer")
        if offer is not None:
            self.trace.append(
                {
                    "event": "offer",
                    "mode": offer["mode"],
                    "sweep_id": offer["sweep_id"],
                    "offer_id": offer["offer_id"],
                    "fingerprint": offer["link_fingerprint"],
                }
            )
        if handout is None:
            # Quasarr has nothing left to hand out, so whatever this exchange
            # still owed is owed no longer: the trace may be judged in full.
            self._note_completion()
        return response

    def _note_completion(self):
        if not self.trace or self.trace[-1]["event"] != COMPLETION_EVENT:
            self.trace.append({"event": COMPLETION_EVENT})

    def _report(self, rule, route, payload):
        access = "blocked" if route == "defer" else payload.get("access")
        self.trace.append(
            {
                "event": "report",
                "route": route,
                "access": access,
                "sweep_id": payload.get("sweep_id", ""),
                "offer_id": payload.get("offer_id", ""),
                "fingerprint": payload.get("link_fingerprint", ""),
            }
        )
        response = self._invoke(rule, payload)
        self.trace.append(
            {
                "event": "answer",
                "route": route,
                "status": response.status_code,
                "body": response._payload,
            }
        )
        return response

    def _terminal(self, rule, endpoint, payload):
        submissions = len(self.device.add_links_calls)
        response = self._invoke(rule, payload)
        self.trace.append(
            {
                "event": "terminal",
                "endpoint": endpoint,
                "package_id": payload.get("package_id"),
                "operation_id": payload.get("terminal_operation_id", ""),
                "status": response.status_code,
                "body": response._payload,
            }
        )
        for _ in range(len(self.device.add_links_calls) - submissions):
            self.trace.append(
                {"event": "submission", "package_id": payload.get("package_id")}
            )
        return response

    # --- the `requests` surface the helper uses ----------------------------
    def post(self, url, json=None, **kwargs):
        return self._route("POST", url, json)

    def get(self, url, **kwargs):
        return self._route("GET", url, None)

    def delete(self, url, json=None, **kwargs):
        return self._route("DELETE", url, json)


class ScriptedAdapter:
    """One Filecrypt verdict per offered container, in the scripted order.

    The verdicts themselves are produced by the shipped classifier from
    synthetic evidence, so what the loop receives is the exact result shape a
    real round emits rather than a body written by this test.
    """

    def __init__(self, verdicts, server=None):
        self.verdicts = list(verdicts)
        self.payloads = []
        self.server = server

    def __call__(self, payload):
        self.payloads.append(dict(payload))
        if self.server is not None:
            # One timeline for requests and rounds, so "before any new work"
            # is an ordering that can be asserted rather than a count.
            self.server.calls.append(("DECRYPT", "/decrypt"))
        if not self.verdicts:
            raise KeyboardInterrupt
        return self.result(payload["url"], self.verdicts.pop(0))

    @staticmethod
    def result(url, verdict):
        blocked = verdict == "blocked"
        evidence = CrypterEvidence(
            crypter=CRYPTER,
            url=url,
            final_url="https://filecrypt.invalid/404.html" if blocked else url,
            status_code=200,
            container_content_seen=verdict == "clear",
        )
        outcome = classify_filecrypt_access(evidence)
        if outcome.access is CrypterAccess.BLOCKED:
            return defer_payload(outcome)
        result = {
            "status": 200,
            "urls": [HOSTER_URL] if outcome.access is CrypterAccess.CLEAR else [],
        }
        result.update(clear_metadata(outcome))
        return result


class CombinedCohortTestCase(unittest.TestCase):
    """One real helper loop against one real Quasarr, in one process."""

    packages = 5

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_dir = os.path.join(directory.name, "state")
        os.makedirs(self.state_dir)
        patched = mock.patch.dict(
            os.environ, {pending_store.STATE_DIR_ENVIRONMENT_VARIABLE: self.state_dir}
        )
        patched.start()
        self.addCleanup(patched.stop)
        service._UNFROZEN_CYCLE_WARNINGS.clear()
        self.addCleanup(service._UNFROZEN_CYCLE_WARNINGS.clear)

        self.original_values = provider_shared_state.values
        self.original_lock = provider_shared_state.lock
        provider_shared_state.values = {
            "dbfile": os.path.join(directory.name, "Quasarr.db")
        }
        provider_shared_state.lock = None
        self.clock = FakeClock()
        self.server = QuasarrServer(
            provider_shared_state.values["dbfile"], self.clock, packages=self.packages
        )
        self.addCleanup(self.restore_shared_state)
        self.logs = []
        self.sleeps = []

    def restore_shared_state(self):
        self.server.close()
        provider_shared_state.values = self.original_values
        provider_shared_state.lock = self.original_lock

    def log(self, message):
        self.logs.append(str(message))

    def run_loop(self, adapter, stop_after_sleeps=1):
        """Run the real loop until its scripted work or sleep budget ends."""
        sleeps = self.sleeps

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= stop_after_sleeps:
                raise KeyboardInterrupt

        fake_requests = SimpleNamespace(
            post=self.server.post, get=self.server.get, delete=self.server.delete
        )
        fake_time = SimpleNamespace(time=self.clock, sleep=sleep)

        def no_socket(*args, **kwargs):
            raise AssertionError("the combined harness must open no socket")

        with (
            mock.patch.object(socket, "socket", no_socket),
            mock.patch.object(service, "requests", fake_requests),
            mock.patch.object(service, "time", fake_time),
            mock.patch.object(service, "decrypt_payload_with_captcha_handler", adapter),
            mock.patch.object(
                service, "get_captcha_handler_status", return_value=False
            ),
            mock.patch.object(service, "debug", self.log),
            mock.patch.object(service, "info", self.log),
            mock.patch.object(service, "warn", self.log),
            mock.patch.object(service, "error", self.log),
        ):
            service.check_quasarr_process(QUASARR_URL, API_KEY)

    def sweep(self, verdicts, **kwargs):
        adapter = ScriptedAdapter(verdicts, server=self.server)
        self.run_loop(adapter, stop_after_sleeps=kwargs.pop("stop_after_sleeps", 1))
        return adapter

    def offers(self):
        return self.server.events("offer")

    def answers(self, route=None):
        return [
            entry
            for entry in self.server.events("answer")
            if route is None or entry["route"] == route
        ]

    def assert_no_violations(self):
        oracle = contract_oracle()
        violations = oracle._contract_violations(self.server.trace)
        self.assertEqual([], violations, f"combined trace violated: {violations}")


class AllBlockedCohortTests(CombinedCohortTestCase):
    def test_five_blocked_members_drive_the_real_cohort_into_cooldown(self):
        adapter = self.sweep(["blocked"] * 5, stop_after_sleeps=5)

        self.assertEqual(5, len(adapter.payloads))
        self.assertEqual(5, len({payload["url"] for payload in adapter.payloads}))
        self.assertTrue(
            all(payload["fresh_crypter_state"] for payload in adapter.payloads),
            "every cohort container is opened in fresh browser state",
        )
        offers = self.offers()
        self.assertEqual(["sweep"] * 5, [offer["mode"] for offer in offers])
        self.assertEqual(1, len({offer["sweep_id"] for offer in offers}))
        self.assertEqual(5, len({offer["fingerprint"] for offer in offers}))

        answers = [entry["body"]["instruction"] for entry in self.answers("defer")]
        self.assertEqual(["hold", "hold", "hold", "hold", "cooldown"], answers)
        self.assertEqual("cooldown", self.server.decision_row()["state"])
        self.assertEqual([], self.server.events("submission"))
        self.assertEqual([], self.server.events("terminal"))
        self.assert_no_violations()

    def test_the_cooled_cohort_stops_offering_work_to_the_same_helper(self):
        self.sweep(["blocked"] * 5, stop_after_sleeps=5)
        before = len(self.server.trace)

        self.sweep(["blocked"], stop_after_sleeps=1)

        self.assertEqual(
            [],
            self.server.trace[before:],
            "a cooling linkcrypter hands out no further offer",
        )


class ClearEndsTheSweepTests(CombinedCohortTestCase):
    def test_a_clear_in_third_place_ends_the_sweep_and_queues_the_retests(self):
        self.sweep(["blocked", "blocked", "clear"], stop_after_sleeps=6)

        reports = self.server.events("report")
        self.assertEqual(
            ["blocked", "blocked", "clear"],
            [report["access"] for report in reports[:3]],
        )
        blocked = [entry for entry in self.answers("defer")]
        self.assertEqual(
            ["hold", "hold"], [entry["body"]["instruction"] for entry in blocked]
        )
        cleared = self.answers("access")[-1]
        self.assertEqual(200, cleared["status"])
        self.assertIs(True, cleared["body"]["cleared"])
        self.assertEqual("healthy", cleared["body"]["state"])

        record = self.server.decision_row()
        self.assertEqual("healthy", record["state"])
        offered = [offer["fingerprint"] for offer in self.offers()[:2]]
        self.assertEqual(sorted(offered), record["retest_members"])
        self.assertNotEqual("cooldown", record["state"])
        self.assert_no_violations()

    def test_the_helper_is_offered_exactly_the_retests_after_the_clear(self):
        self.sweep(["blocked", "blocked", "clear"], stop_after_sleeps=6)
        blocked = sorted(offer["fingerprint"] for offer in self.offers()[:2])
        before = len(self.offers())
        # The round that was interrupted left one lease behind; it expires two
        # minutes later and only then may the same member be offered again.
        self.clock.now += 300

        self.sweep(["blocked", "blocked"], stop_after_sleeps=4)

        retests = self.offers()[before:]
        self.assertEqual(["retest", "retest"], [offer["mode"] for offer in retests])
        self.assertEqual(
            blocked,
            [offer["fingerprint"] for offer in retests],
            "the retest queue is exactly the invalidated members",
        )


class LostAcknowledgementTests(CombinedCohortTestCase):
    def test_a_lost_clear_acknowledgement_is_retried_before_any_new_work(self):
        adapter = ScriptedAdapter(["clear"], server=self.server)
        self.server.drop_next["/sponsors_helper/api/crypter-access/"] = 1

        self.run_loop(adapter, stop_after_sleeps=4)

        kinds = [call[1] for call in self.server.calls]
        first = kinds.index("/sponsors_helper/api/crypter-access/")
        second = kinds.index("/sponsors_helper/api/crypter-access/", first + 1)
        self.assertEqual(
            2,
            kinds.count("/sponsors_helper/api/crypter-access/"),
            "the owed result is retried once",
        )
        self.assertNotIn(
            "/decrypt",
            kinds[first:second],
            "an owed acknowledgement is settled before new work",
        )
        # The first report reached the routes and committed; the answer to it
        # is what was lost, so the retry is a replay of a decided generation.
        reported = [
            entry
            for entry in self.server.events("report")
            if entry["route"] == "access"
        ]
        self.assertEqual(2, len(reported))
        self.assertEqual(1, len({entry["offer_id"] for entry in reported}))
        answered = self.answers("access")
        self.assertEqual([200, 200], [entry["status"] for entry in answered])
        self.assertEqual([True, True], [entry["body"]["cleared"] for entry in answered])
        self.assertEqual(answered[0]["body"], answered[1]["body"])
        self.assertEqual("healthy", self.server.decision_row()["state"])
        self.assertEqual([], self.server.events("submission"))
        self.assertEqual([], self.server.device.add_links_calls)
        self.assert_no_violations()

    def test_the_owed_result_survives_a_restart_of_the_loop(self):
        adapter = ScriptedAdapter(["clear"], server=self.server)
        self.server.drop_next["/sponsors_helper/api/crypter-access/"] = 5
        self.run_loop(adapter, stop_after_sleeps=3)

        stored = json.loads(
            (
                pathlib.Path(self.state_dir) / pending_store.PENDING_RESULTS_FILENAME
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(1, len(stored["pending"]))

        self.server.drop_next.clear()
        self.sleeps.clear()
        self.run_loop(ScriptedAdapter([], server=self.server), stop_after_sleeps=2)

        self.assertIs(True, self.answers("access")[-1]["body"]["cleared"])
        self.assertEqual("healthy", self.server.decision_row()["state"])


class TerminalCleanupTests(CombinedCohortTestCase):
    def test_one_successful_submission_finishes_the_package_exactly_once(self):
        self.sweep(["clear"], stop_after_sleeps=3)

        submissions = self.server.events("submission")
        self.assertEqual(1, len(submissions), "exactly one JDownloader submission")
        terminal = self.server.events("terminal")
        self.assertEqual(1, len(terminal))
        self.assertEqual("download", terminal[0]["endpoint"])
        self.assertEqual(200, terminal[0]["status"])
        self.assertEqual(
            {
                "success": True,
                "terminal_state": "downloaded",
                "package_removed": True,
                "package_terminal": True,
                "package_id": terminal[0]["package_id"],
            },
            terminal[0]["body"],
        )
        self.assertNotIn(terminal[0]["package_id"], self.server.protected_ids())
        self.assertEqual(self.packages - 1, len(self.server.protected_ids()))
        comment = self.server.device.add_links_calls[0][0]["comment"]
        self.assertTrue(comment.startswith(f"{terminal[0]['package_id']} op:"))
        self.assert_no_violations()

    def test_a_lost_terminal_answer_never_submits_the_package_twice(self):
        self.server.drop_next["/sponsors_helper/api/download/"] = 1

        self.sweep(["clear"], stop_after_sleeps=4)

        self.assertEqual(
            1,
            len(self.server.events("submission")),
            "the retried operation replays instead of submitting again",
        )
        self.assertEqual(1, len(self.server.device.add_links_calls))
        terminal = self.server.events("terminal")
        self.assertEqual(2, len(terminal), "the lost answer cost one extra request")
        self.assertEqual([200, 200], [entry["status"] for entry in terminal])
        self.assertEqual(
            terminal[0]["body"],
            terminal[1]["body"],
            "the operation answers the retry with the verdict it already reached",
        )
        kinds = [call[1] for call in self.server.calls]
        first = kinds.index("/sponsors_helper/api/download/")
        second = kinds.index("/sponsors_helper/api/download/", first + 1)
        self.assertNotIn(
            "/decrypt",
            kinds[first:second],
            "the retry replays the operation instead of solving again",
        )
        confirmations = [entry for entry in terminal if entry["status"] == 200]
        self.assertTrue(confirmations)
        self.assertIs(True, confirmations[-1]["body"]["success"])
        self.assertNotIn(confirmations[-1]["package_id"], self.server.protected_ids())
        self.assert_no_violations()

    def test_the_terminal_cycle_is_cleared_after_the_confirmation(self):
        self.sweep(["clear"], stop_after_sleeps=3)

        cycles = pathlib.Path(self.state_dir) / pending_store.TERMINAL_CYCLES_FILENAME
        stored = (
            json.loads(cycles.read_text(encoding="utf-8")) if cycles.exists() else {}
        )
        self.assertEqual(
            [],
            stored.get("cycles", []),
            "a confirmed terminal operation leaves no frozen cycle",
        )


class ContractOracleTests(CombinedCohortTestCase):
    """The other repository's oracle really discriminates this harness."""

    def test_the_oracle_rejects_the_legacy_control_and_accepts_the_real_trace(self):
        oracle = contract_oracle()
        self.sweep(["blocked"] * 5, stop_after_sleeps=5)

        self.assertEqual([], oracle._contract_violations(self.server.trace))
        control = oracle._contract_violations(oracle.legacy_three_404_trace())
        self.assertNotEqual([], control)
        self.assertLessEqual(
            {"premature_cooldown", "incomplete_coverage", "missing_retest"},
            {violation["code"] for violation in control},
        )


if __name__ == "__main__":
    unittest.main()
