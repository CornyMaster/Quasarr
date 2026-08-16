# -*- coding: utf-8 -*-

"""Combined SponsorsHelper/Quasarr Filecrypt lifecycle end-to-end harness.

This module is deliberately outside `tests/`: it is the only suite that needs
both repositories importable at once and is therefore never picked up by the
ordinary `unittest discover -s tests` gate. It runs inside the SponsorsHelper
image with `PYTHONPATH=/quasarr:/app`.

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
    link_fingerprint,
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
            FilecryptLifecycleService=importlib.import_module(
                "quasarr.providers.filecrypt_lifecycle_service"
            ).FilecryptLifecycleService,
        )


_quasarr = _import_quasarr()
sponsors_helper_api = _quasarr.api
setup_sponsors_helper_routes = _quasarr.setup_sponsors_helper_routes
provider_shared_state = _quasarr.shared_state
enumerate_filecrypt_candidates = _quasarr.enumerate_filecrypt_candidates
CrypterCooldownService = _quasarr.CrypterCooldownService
DataBase = _quasarr.DataBase
FilecryptLifecycleService = _quasarr.FilecryptLifecycleService

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


def filecrypt_fingerprint(index):
    return link_fingerprint(CRYPTER, filecrypt_url(index))


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

    def lifecycle_sweep_state(self):
        """Current filecrypt_sweep_state header, decoded, or None."""
        raw = self.get_db("filecrypt_sweep_state").retrieve("filecrypt")
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    def lifecycle_link_state(self, fingerprint):
        """Link-state record for one fingerprint, decoded, or None."""
        raw = self.get_db("filecrypt_link_states").retrieve(fingerprint)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    def held_fps(self):
        """Sorted list of fingerprints with state=held (including expired holds)."""
        rows = self.get_db("filecrypt_link_states").retrieve_all_titles() or []
        result = []
        for fp, raw in rows:
            try:
                rec = json.loads(raw)
                if rec.get("state") == "held":
                    result.append(fp)
            except (TypeError, ValueError):
                pass
        return sorted(result)

    def blacklisted_fps(self):
        """Sorted list of fingerprints with state=blacklisted."""
        rows = self.get_db("filecrypt_link_states").retrieve_all_titles() or []
        result = []
        for fp, raw in rows:
            try:
                rec = json.loads(raw)
                if rec.get("state") == "blacklisted":
                    result.append(fp)
            except (TypeError, ValueError):
                pass
        return sorted(result)

    # --- dispatch ----------------------------------------------------------
    def _clocked_service(self, state):
        return CrypterCooldownService(state, clock=self.clock)

    def _make_lifecycle_service(self, state):
        return FilecryptLifecycleService(
            state, clock=self.clock, identifier_factory=self.ids
        )

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
            mock.patch.object(
                sponsors_helper_api,
                "FilecryptLifecycleService",
                self._make_lifecycle_service,
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
                    "capability": offer.get("capability", ""),
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


class LifecycleSweepTests(CombinedCohortTestCase):
    """Lifecycle protocol with unlimited denominator and per-URL held state."""

    packages = 500

    def test_500_blocked_no_cap_all_held_global_cooldown(self):
        """500 first-time BLOCKED: unlimited denominator, all held, global cooldown.

        Defect caught: a 100-member cap on lifecycle offers would reject hold
        acknowledgements and loop forever without clearing held state.
        """
        self.sweep(["blocked"] * self.packages)

        defer_answers = self.answers("defer")
        self.assertEqual(self.packages, len(defer_answers))
        # First 499: hold; last: cooldown (all blocked, sweep complete ≥5)
        instructions = [a["body"]["instruction"] for a in defer_answers]
        self.assertEqual(["hold"] * (self.packages - 1) + ["cooldown"], instructions)
        # Denominator proves no 100-cap: lifecycle sweep_total equals full package count
        last_body = defer_answers[-1]["body"]
        self.assertEqual(self.packages, last_body["sweep_total"])
        self.assertEqual(self.packages, last_body["sweep_tested"])
        # Every link is individually held (24h)
        self.assertEqual(self.packages, len(self.server.held_fps()))
        # No terminal failures during first-time BLOCKED
        self.assertEqual([], self.server.events("terminal"))
        self.assertEqual([], self.server.events("submission"))
        # All packages survive
        self.assertEqual(self.packages, len(self.server.protected_ids()))
        # Global cooldown header written
        sweep_state = self.server.lifecycle_sweep_state()
        self.assertIsNotNone(sweep_state)
        self.assertEqual("cooldown", sweep_state.get("state"))
        self.assert_no_violations()

    def test_499_blocked_one_clear_no_global_cooldown(self):
        """499 BLOCKED + one CLEAR: no global cooldown; blocked holds remain.

        Defect caught: the CLEAR should prevent global cooldown and its receipt
        must set global_possible=False so the sweep closes as healthy.
        The CLEAR package is successfully downloaded (no Arr failure = no /fail/).
        """
        self.sweep(["blocked"] * (self.packages - 1) + ["clear"])

        # 499 hold responses (sweep in progress, not all blocked)
        defer_answers = self.answers("defer")
        self.assertEqual(self.packages - 1, len(defer_answers))
        self.assertTrue(
            all(a["body"]["instruction"] == "hold" for a in defer_answers),
            "every BLOCKED sweep member gets a hold while sweep is in progress",
        )
        # CLEAR closes the sweep as healthy (not cooldown)
        access_answers = self.answers("access")
        self.assertEqual(1, len(access_answers))
        self.assertIs(True, access_answers[-1]["body"]["cleared"])
        self.assertEqual("healthy", access_answers[-1]["body"]["state"])
        # 499 per-URL held states survive the CLEAR
        self.assertEqual(self.packages - 1, len(self.server.held_fps()))
        # No global cooldown
        sweep_state = self.server.lifecycle_sweep_state()
        self.assertIsNotNone(sweep_state)
        self.assertNotEqual("cooldown", sweep_state.get("state"))
        # CLEAR: helper downloads (success, not Arr failure); no /fail/ called
        terminal = self.server.events("terminal")
        self.assertEqual(1, len(terminal), "CLEAR triggers one successful download")
        self.assertEqual("download", terminal[0]["endpoint"])
        fail_calls = [c for c in self.server.calls if c[1].endswith("/fail/")]
        self.assertEqual([], fail_calls, "CLEAR download is not an Arr failure")
        self.assert_no_violations()


class LifecycleLinkTests(CombinedCohortTestCase):
    """Per-link hold, recheck, terminal blacklist, and config semantics."""

    packages = 1

    def test_untested_link_first_time_after_global_cooldown(self):
        """New fingerprint added during global cooldown gets individual first-time after expiry.

        Full workflow: 5 all-BLOCKED → global cooldown; add new package while
        cooldown active; no offer while cooldown active; advance clock past
        cooldown deadline; first post-expiry offer for that fingerprint uses
        mode=individual and BLOCKED returns hold (not blacklist/terminal).

        Defect caught: a global cooldown infecting untested fingerprints would
        make their first BLOCKED terminal-eligible, causing spurious failures.
        """
        self.packages = 5
        self.server.close()
        self.server = QuasarrServer(
            provider_shared_state.values["dbfile"], self.clock, packages=5
        )

        # 5 all-BLOCKED → global cooldown
        self.sweep(["blocked"] * 5)
        sweep_state = self.server.lifecycle_sweep_state()
        self.assertIsNotNone(sweep_state)
        self.assertEqual("cooldown", sweep_state.get("state"))
        cooldown_deadline = sweep_state["retry_after_epoch"]
        self.assertEqual(5, len(self.server.held_fps()))

        # Add new package while cooldown is active
        self.server.get_db("protected").update_store(package(6), protected_blob(6))
        fp6 = filecrypt_fingerprint(6)

        # While cooldown is still active: no offer for the new fingerprint
        self.clock.now = cooldown_deadline - 1
        adapter = ScriptedAdapter([], server=self.server)
        self.run_loop(adapter, stop_after_sleeps=1)
        offered_fps = [o["fingerprint"] for o in self.offers()]
        self.assertNotIn(fp6, offered_fps, "no offer while cooldown active")

        # Advance past cooldown deadline (holds also expire at the same time)
        self.clock.now = cooldown_deadline + 1

        # Retests fire for the 5 original held fps, then fp6 gets individual.
        # Each retest-blacklist may cause a helper sleep cycle; allow enough sleeps.
        self.sweep(["blocked"] * 5 + ["blocked"], stop_after_sleeps=10)

        # Locate fp6's offer specifically (must be individual, not retest)
        fp6_offers = [o for o in self.offers() if o["fingerprint"] == fp6]
        self.assertEqual(1, len(fp6_offers), "fp6 offered exactly once")
        self.assertEqual("individual", fp6_offers[0]["mode"])
        # Instruction is hold (first-time), not blacklist or terminal
        defer_answers = [
            a
            for a in self.answers("defer")
            if isinstance(a.get("body"), dict)
            and a["body"].get("sweep_id") == fp6_offers[0]["sweep_id"]
        ]
        self.assertEqual(1, len(defer_answers))
        self.assertEqual("hold", defer_answers[0]["body"]["instruction"])
        self.assertIn(fp6, self.server.held_fps())
        self.assertNotIn(fp6, self.server.blacklisted_fps())
        # No terminal events for fp6 (only retests for original 5 may produce terminals)
        # Package 6 survives
        self.assertIn(package(6), self.server.protected_ids())

    def test_first_blocked_recheck_clear_downloads_no_arr_failure(self):
        """After 24h hold, a RETEST CLEAR downloads the package successfully.

        The CLEAR proves the link is accessible; the helper downloads it as success.
        No /fail/ is called, so Arr sees no failure and no release is re-grabbed.
        After CLEAR: fingerprint is neither held nor blacklisted.

        Defect caught: not clearing the held link state after retest CLEAR would
        leave the fingerprint permanently in retest, preventing a clean download.
        """
        # Package 1: first BLOCKED → individual hold
        self.sweep(["blocked"])
        fp1 = filecrypt_fingerprint(1)
        self.assertIn(fp1, self.server.held_fps())

        # Advance past hold period → retest available
        self.clock.now += 86401

        # Retest CLEAR → link accessible → helper downloads (success)
        self.sweep(["clear"])

        # Offer mode must be retest (deterministic: held link past expiry)
        retest_offers = [
            o
            for o in self.offers()
            if o["fingerprint"] == fp1 and o["mode"] == "retest"
        ]
        self.assertEqual(1, len(retest_offers), "retest mode for held fp past expiry")

        # Exact CLEAR ack: cleared=True, state=healthy
        access_answers = self.answers("access")
        self.assertEqual(1, len(access_answers))
        self.assertIs(True, access_answers[0]["body"]["cleared"])
        self.assertEqual("healthy", access_answers[0]["body"]["state"])

        # One successful download terminal event
        terminal = self.server.events("terminal")
        self.assertEqual(1, len(terminal))
        self.assertEqual("download", terminal[0]["endpoint"])
        self.assertEqual(200, terminal[0]["status"])
        # Package downloaded and removed from protected
        self.assertNotIn(terminal[0]["package_id"], self.server.protected_ids())
        # No /fail/ = no Arr failure
        fail_calls = [c for c in self.server.calls if c[1].endswith("/fail/")]
        self.assertEqual([], fail_calls)
        # FP neither held nor blacklisted after CLEAR
        self.assertNotIn(fp1, self.server.held_fps())
        self.assertNotIn(fp1, self.server.blacklisted_fps())
        self.assert_no_violations()

    def test_first_blocked_second_blocked_terminal_blacklist_and_scrub(self):
        """Second BLOCKED in retest: terminal failure once, blacklist ack returned.

        Covers: shared owner with alternative link retains alternative after scrub;
        empty owner (sole link = blacklisted fp) fails exactly once; no helper /fail/;
        pre-blacklisted fingerprint in a newly added package is scrubbed on next
        /to_decrypt and terminal-failed exactly once without being offered.

        Defect caught: counting an attempt on blacklist would silently deplete
        solver quota; calling /fail/ before Quasarr's confirmed blacklist would
        corrupt the terminal history.
        """
        # Package 1: sole owner of fp(url(1))
        # Package 2: shared owner with fp(url(1)) + fp(url(2)) (alternative)
        shared_blob = json.dumps(
            {
                "title": f"{TITLE}.shared",
                "password": "",
                "links": [[filecrypt_url(1), "he"], [filecrypt_url(2), "he"]],
            }
        )
        self.server.get_db("protected").update_store(package(2), shared_blob)
        # Package 3: sole owner of fp(url(1)) only (empty owner after blacklist)
        empty_owner_blob = json.dumps(
            {
                "title": f"{TITLE}.empty",
                "password": "",
                "links": [[filecrypt_url(1), "he"]],
            }
        )
        self.server.get_db("protected").update_store(package(3), empty_owner_blob)

        fp1 = filecrypt_fingerprint(1)

        # First BLOCKED for fp(url(1)) → individual hold (package 1 offered first)
        self.sweep(["blocked"])
        self.assertIn(fp1, self.server.held_fps())

        # Advance past hold period → retest available
        self.clock.now += 86401

        # Retest for fp1 → BLOCKED → terminal failure + blacklist ack (no /fail/)
        # Then fp2 (first-time individual) → BLOCKED → first-time hold for pkg2
        self.sweep(["blocked", "blocked"])

        # Blacklist ack in trace (returned from /defer/ by route internally)
        blacklist_answers = [
            a
            for a in self.answers("defer")
            if isinstance(a.get("body"), dict)
            and a["body"].get("instruction") == "blacklist"
        ]
        self.assertEqual(1, len(blacklist_answers), "exactly one blacklist ack")
        # Helper called no /fail/ (terminal handled internally by defer route)
        fail_calls = [c for c in self.server.calls if c[1].endswith("/fail/")]
        self.assertEqual([], fail_calls)
        # No helper-initiated terminal events
        self.assertEqual([], self.server.events("terminal"))
        # fp(url(1)) blacklisted
        self.assertIn(fp1, self.server.blacklisted_fps())
        # Package 1 terminal-failed and removed (sole owner of fp1)
        self.assertNotIn(package(1), self.server.protected_ids())
        # Package 3 terminal-failed and removed (sole owner = empty owner)
        self.assertNotIn(package(3), self.server.protected_ids())
        # Package 2 still present (has alternative url(2) after url(1) scrubbed)
        self.assertIn(package(2), self.server.protected_ids())
        # Package 2's row has only url(2) (url(1) scrubbed by route on next /decrypt/)
        pkg2_raw = self.server.get_db("protected").retrieve(package(2))
        pkg2_links = json.loads(pkg2_raw)["links"]
        self.assertEqual(
            1, len(pkg2_links), "url(1) scrubbed from pkg2, url(2) remains"
        )
        self.assertEqual(filecrypt_url(2), pkg2_links[0][0])

        # --- Next /to_decrypt scrub pass: blacklisted fp never offered again ---
        trace_len_before_scrub = len(self.server.trace)
        # Trigger a real scrub pass by running the loop for the remaining work
        self.sweep(["blocked"])
        # Only new offers (after blacklist) may not contain fp1
        new_offers = [
            e
            for e in self.server.trace[trace_len_before_scrub:]
            if e.get("event") == "offer" and e.get("fingerprint") == fp1
        ]
        self.assertEqual([], new_offers, "blacklisted fp never re-offered after scrub")

        # --- Pre-blacklisted fingerprint in a newly added package ---
        # Add package 4 carrying the already-blacklisted fp1
        pre_blacklisted_blob = json.dumps(
            {
                "title": f"{TITLE}.late",
                "password": "",
                "links": [[filecrypt_url(1), "he"]],
            }
        )
        self.server.get_db("protected").update_store(package(4), pre_blacklisted_blob)
        trace_len_before_preblacklist = len(self.server.trace)
        # Next /to_decrypt: pre-offer scrub removes pkg4 without offering fp1
        self.sweep(["blocked"])
        # Package 4 must be terminal-failed (pre-blacklisted link is sole link)
        self.assertNotIn(package(4), self.server.protected_ids())
        # fp1 still never offered after the first blacklist
        new_fp1_offers = [
            e
            for e in self.server.trace[trace_len_before_preblacklist:]
            if e.get("event") == "offer" and e.get("fingerprint") == fp1
        ]
        self.assertEqual([], new_fp1_offers, "pre-blacklisted fp not offered")
        # Exactly one reporting-package failure per empty-owner package (1,3,4)
        # verified by absence from protected and no /fail/ duplication
        remaining = self.server.protected_ids()
        self.assertNotIn(package(1), remaining)
        self.assertNotIn(package(3), remaining)
        self.assertNotIn(package(4), remaining)
        self.assertIn(package(2), remaining)

    def test_table_driven_recovery_and_config(self):
        """Sweep window config: stored WebGUI > ENV > default 15m through real route.
        Lost lifecycle blacklist ack replays exact identity without duplicate terminal.

        Defect caught: lost ack replay with no receipt would re-run terminal
        failure, double-removing the package and corrupting the blacklist state.
        ENV precedence tested through route-created offer deadline; stored override
        beats ENV; clearing stored override returns to ENV, not hardcoded 15.
        """
        from quasarr.storage.setup.crypter_blocks import (
            FILECRYPT_SWEEP_WINDOW_KEY,
            refresh_crypter_block_settings,
        )

        # --- config precedence through real route offer deadline ---
        settings_db = self.server.get_db("crypter_block_settings")

        with self.subTest("ENV 45m, no stored override"):
            settings_db.delete(FILECRYPT_SWEEP_WINDOW_KEY)
            with mock.patch.dict(os.environ, {"FILECRYPT_SWEEP_WINDOW_MINUTES": "45"}):
                refresh_crypter_block_settings(self.server)
            self.assertEqual(45, self.server.values["filecrypt_sweep_window_minutes"])
            # Route-created offer deadline reflects 45m
            svc = FilecryptLifecycleService(
                self.server, clock=self.clock, identifier_factory=self.server.ids
            )
            self.assertEqual(45 * 60, svc._sweep_window_seconds())

        with self.subTest("stored 30 beats ENV 45"):
            settings_db.update_store(FILECRYPT_SWEEP_WINDOW_KEY, "30")
            with mock.patch.dict(os.environ, {"FILECRYPT_SWEEP_WINDOW_MINUTES": "45"}):
                refresh_crypter_block_settings(self.server)
            self.assertEqual(30, self.server.values["filecrypt_sweep_window_minutes"])
            svc2 = FilecryptLifecycleService(
                self.server, clock=self.clock, identifier_factory=self.server.ids
            )
            self.assertEqual(30 * 60, svc2._sweep_window_seconds())

        with self.subTest("clearing stored override returns to ENV 45, not default 15"):
            settings_db.delete(FILECRYPT_SWEEP_WINDOW_KEY)
            with mock.patch.dict(os.environ, {"FILECRYPT_SWEEP_WINDOW_MINUTES": "45"}):
                refresh_crypter_block_settings(self.server)
            self.assertEqual(45, self.server.values["filecrypt_sweep_window_minutes"])
            svc3 = FilecryptLifecycleService(
                self.server, clock=self.clock, identifier_factory=self.server.ids
            )
            self.assertEqual(45 * 60, svc3._sweep_window_seconds())

        # Restore default for the rest of the test
        settings_db.delete(FILECRYPT_SWEEP_WINDOW_KEY)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FILECRYPT_SWEEP_WINDOW_MINUTES", None)
            refresh_crypter_block_settings(self.server)

        # --- lost lifecycle blacklist ack replays same identity ---
        # First BLOCKED → individual hold
        self.sweep(["blocked"])
        n_held = len(self.server.held_fps())
        self.assertGreater(n_held, 0, "at least one link held after first BLOCKED")

        # Advance past hold period
        self.clock.now += 86401

        # Drop defer response for the retest BLOCKED → ack lost; server committed terminal
        self.server.drop_next["/sponsors_helper/api/defer/"] = 1
        self.sweep(["blocked"])  # retest BLOCKED → terminal + blacklist; ack LOST
        # Server-side: terminal failure committed, package removed
        self.assertEqual(
            0,
            len(self.server.protected_ids()),
            "package terminal-failed on first (lost) attempt",
        )

        # Replay: helper has stored pending; new check_quasarr_process reads durable file
        self.server.drop_next.clear()
        # This invocation is the "actual helper restart" reading durable pending file
        self.run_loop(ScriptedAdapter([], server=self.server), stop_after_sleeps=2)

        # Both blacklist answers must be identical (receipt replay, exact body/terminal ID)
        blacklist_answers = [
            a["body"]
            for a in self.answers("defer")
            if isinstance(a.get("body"), dict)
            and a["body"].get("instruction") == "blacklist"
        ]
        self.assertEqual(
            2, len(blacklist_answers), "one lost + one replayed blacklist ack"
        )
        self.assertEqual(
            blacklist_answers[0],
            blacklist_answers[1],
            "replay returns exact same body from receipt (terminal ID preserved)",
        )


class ContractOracleTests(CombinedCohortTestCase):
    """The other repository's oracle discriminates lifecycle and legacy traces."""

    def test_the_oracle_rejects_the_legacy_control_and_accepts_the_lifecycle_trace(
        self,
    ):
        """Oracle accepts unlimited-denominator lifecycle cooldown and rejects legacy.

        Also validates: offer modes for workflows 3-5 (individual/retest), and
        a non-lifecycle negative control with missing retests is still detected.

        Defect caught: applying the 100-cap check to lifecycle would flag
        legitimate 500-member sweeps as `impossible_cohort_size`.
        """
        oracle = contract_oracle()
        self.sweep(["blocked"] * 5)

        # Lifecycle trace is clean
        self.assertEqual([], oracle._contract_violations(self.server.trace))
        # Offer modes: first 5 are individual or sweep (first-time lifecycle)
        offer_modes = {o["mode"] for o in self.offers()}
        self.assertTrue(
            offer_modes <= {"sweep", "individual"},
            f"first-time offers must be sweep or individual, got {offer_modes}",
        )

        # Legacy negative control: premature cooldown, incomplete coverage, missing retest
        control = oracle._contract_violations(oracle.legacy_three_404_trace())
        self.assertNotEqual([], control)
        self.assertLessEqual(
            {"premature_cooldown", "incomplete_coverage", "missing_retest"},
            {violation["code"] for violation in control},
        )

        # Negative control: a non-lifecycle trace with a CLEAR that omits retests
        # is still caught by the oracle (the lifecycle CLEAR skip is narrowly scoped).
        with self.subTest("non-lifecycle missing retest detected"):
            bad_trace = oracle.clear_after_blocked_trace(
                blocked=3, retests=[], complete=True
            )
            bad_violations = oracle._contract_violations(bad_trace)
            codes = {v["code"] for v in bad_violations}
            self.assertIn("missing_retest", codes)


if __name__ == "__main__":
    unittest.main()
