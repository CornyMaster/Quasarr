# -*- coding: utf-8 -*-

import contextlib
import json
import threading
import time
import unittest
from unittest import mock

from bottle import Bottle, HTTPError

import quasarr.api.sponsors_helper as sponsors_helper_api
from quasarr.api.sponsors_helper import setup_sponsors_helper_routes
from quasarr.providers.terminal_operations import (
    TERMINAL_OPERATION_TABLE,
    operation_evidence,
    submission_comment,
    terminal_operation_id,
)

DOWNLOAD_RULE = "/sponsors_helper/api/download/"
DISABLE_RULE = "/sponsors_helper/api/disable/"
FAIL_RULE = "/sponsors_helper/api/fail/"

TITLE = "Synthetic.Release.2024.1080p"
FINAL_URL = "https://hoster.invalid/file/1"
TERMINAL_BODY_KEYS = frozenset(
    {
        "success",
        "terminal_state",
        "package_removed",
        "package_terminal",
        "package_id",
    }
)


def package(index=1):
    return f"Quasarr_movies_{index:032x}"


class MemoryTable:
    def __init__(self, rows=None, tables=None, name="protected"):
        self.rows = dict(rows or {})
        self.writes = 0
        self.deletes = 0
        self.unavailable = False
        # Access order, so a read taken to decide something can be told from
        # one taken to verify a write that already happened.
        self.events = []
        # Every table of a Quasarr database lives in one file, so the fake
        # reaches its peers the same way one connection does.
        self.tables = {} if tables is None else tables
        self.tables.setdefault(name, self)
        self.name = name
        self.transactions = []

    def _check(self):
        if self.unavailable:
            raise RuntimeError("table unavailable")

    def retrieve(self, key):
        self.events.append("read")
        self._check()
        return self.rows.get(key)

    def retrieve_all_titles(self):
        self._check()
        items = [[key, value] for key, value in sorted(self.rows.items())]
        return items or None

    def store(self, key, value):
        self.events.append("write")
        self._check()
        self.writes += 1
        self.rows[key] = value
        return True

    def update_store(self, key, value):
        return self.store(key, value)

    def mutate_value(self, key, mutator):
        self._check()
        value = mutator(self.rows.get(key))
        self.writes += 1
        if value is None:
            self.rows.pop(key, None)
        else:
            self.rows[key] = value
        return value

    def _peer(self, table):
        if table not in self.tables:
            self.tables[table] = MemoryTable(tables=self.tables, name=table)
        return self.tables[table]

    def mutate_values(self, targets, mutator):
        """One transaction over several keys of the same database file."""
        resolved = list(targets)
        if len(set(resolved)) != len(resolved) or not resolved:
            raise ValueError("mutate_values targets must be unique and non-empty")
        self.transactions.append(tuple(resolved))
        tables = [self._peer(table) for table, _key in resolved]
        for table in tables:
            table._check()
        current = tuple(
            table.rows.get(key)
            for table, (_name, key) in zip(tables, resolved, strict=True)
        )
        new_values = mutator(current)
        if not isinstance(new_values, (list, tuple)) or len(new_values) != len(
            resolved
        ):
            raise TypeError("mutator must return one value per target")
        for table, (_name, key), value in zip(
            tables, resolved, new_values, strict=True
        ):
            if value == table.rows.get(key):
                continue
            table.events.append("write")
            table.writes += 1
            if value is None:
                table.rows.pop(key, None)
            else:
                table.rows[key] = value
        return tuple(new_values)

    def delete_exact(self, key, value):
        self._check()
        if self.rows.get(key) != value:
            return False
        self.rows.pop(key, None)
        self.deletes += 1
        return True

    def delete(self, key):
        self._check()
        self.rows.pop(key, None)
        self.deletes += 1
        return True


class FakeLinkgrabber:
    def __init__(self, device):
        self.device = device

    def add_links(self, params=None):
        self.device.add_links_calls.append(params)
        if self.device.before_add_links is not None:
            self.device.before_add_links()
        if not self.device.add_links_succeeds:
            return False
        entry = params[0]
        self.device.packages.append(
            {"uuid": len(self.device.packages) + 1, "comment": entry["comment"]}
        )
        return True

    def query_packages(self, params=None):
        self.device.queries += 1
        return list(self.device.packages)

    def query_links(self, params=None):
        self.device.queries += 1
        return list(self.device.links)


class FakeDownloads:
    def __init__(self, device):
        self.device = device

    def query_packages(self, params=None):
        self.device.queries += 1
        return list(self.device.downloader_packages)

    def query_links(self, params=None):
        self.device.queries += 1
        return list(self.device.downloader_links)


class FakeDevice:
    def __init__(self):
        self.packages = []
        self.links = []
        self.downloader_packages = []
        self.downloader_links = []
        self.add_links_calls = []
        self.add_links_succeeds = True
        self.before_add_links = None
        self.queries = 0
        self.linkgrabber = FakeLinkgrabber(self)
        self.downloads = FakeDownloads(self)


class MemorySharedState:
    def __init__(self):
        self.tables = {}
        self.device = FakeDevice()
        self.device_available = True
        self.device_requests = []
        self.values = {
            "database": self.get_db,
            "helper_active": True,
            "helper_last_seen": 0,
            "external_address": "http://quasarr.invalid",
            "crypter_block_mode": "defer",
            "crypter_cooldown_hours": 24,
        }

    def get_db(self, table):
        if table not in self.tables:
            self.tables[table] = MemoryTable(tables=self.tables, name=table)
        return self.tables[table]

    def update(self, key, value):
        self.values[key] = value

    def run_device_request(self, request_name, request_fn, default=None):
        self.device_requests.append(request_name)
        if not self.device_available:
            return default
        return request_fn(self.device)


def route_for(rule):
    app = Bottle()
    setup_sponsors_helper_routes(app)
    return next(route for route in app.routes if route.rule == rule)


class SideEffectGate:
    """Parks callers inside one side effect until the test releases them.

    The first arrival is the request under observation. A second arrival can
    only happen if the route let two requests into the same side effect, so the
    event it sets is the direct evidence of a duplicated transition.
    """

    def __init__(self):
        self.first = threading.Event()
        self.second = threading.Event()
        self.release = threading.Event()
        self.arrivals = 0
        self._guard = threading.Lock()

    def arrive(self):
        with self._guard:
            self.arrivals += 1
            first = self.arrivals == 1
        (self.first if first else self.second).set()
        self.release.wait(timeout=10)

    def wrap(self, function):
        def gated(*args, **kwargs):
            self.arrive()
            return function(*args, **kwargs)

        return gated


class TerminalConfirmationTestCase(unittest.TestCase):
    def setUp(self):
        self.state = MemorySharedState()
        self.notifications = mock.Mock()
        self.store_protected()

    # --- fixtures -------------------------------------------------------

    def store_protected(self, package_id=None, **extra):
        package_id = package_id or package()
        blob = {
            "title": TITLE,
            "password": "",
            "links": [["https://filecrypt.invalid/container/1", "filecrypt"]],
            "notifications": {"discord": {"message_id": "1"}},
        }
        blob.update(extra)
        self.state.get_db("protected").update_store(package_id, json.dumps(blob))

    def protected_row(self, package_id=None):
        raw = self.state.get_db("protected").retrieve(package_id or package())
        return None if raw is None else json.loads(raw)

    def operation_row(self, package_id=None):
        raw = self.state.get_db(TERMINAL_OPERATION_TABLE).retrieve(
            terminal_operation_id(package_id or package())
        )
        return None if raw is None else json.loads(raw)

    def failed_row(self, package_id=None):
        return self.state.get_db("failed").retrieve(package_id or package())

    def failed_blob(self, package_id=None):
        raw = self.failed_row(package_id)
        if raw is None:
            return None
        blob = json.loads(raw)
        return json.loads(blob) if isinstance(blob, str) else blob

    def store_failed(self, package_id=None, **extra):
        """History of an earlier lifecycle of this very release."""
        blob = {"title": TITLE, "error": "last time"}
        blob.update(extra)
        self.state.get_db("failed").update_store(
            package_id or package(), json.dumps(json.dumps(blob))
        )

    def evidence(self, package_id=None, created=1):
        return operation_evidence(
            {"package_id": package_id or package(), "created_epoch": created}
        )

    def current_evidence(self, package_id=None):
        """The evidence of the operation record this request actually opened."""
        return operation_evidence(self.operation_row(package_id))

    def seed_operation(
        self,
        terminal_state,
        package_id=None,
        *,
        state="prepared",
        effect_state="not_started",
        created=1,
        legacy=False,
        shape=None,
        failure_persisted=False,
        notification_state="not_started",
        package_removed=False,
        package_terminal=False,
    ):
        """Persist the record a crashed request would have left behind.

        `shape` picks which deployed row a Quasarr that is upgraded
        mid-operation finds on disk: the seven-key row shipped before the
        effect phase existed, the eight-key row shipped before the failure
        bookkeeping existed, or the current one.
        """
        package_id = package_id or package()
        if shape is None:
            shape = "legacy" if legacy else "current"
        stored = {
            "state": state,
            "terminal_state": terminal_state,
            "package_id": package_id,
            "created_epoch": created,
            "updated_epoch": created,
            "package_removed": package_removed,
            "package_terminal": package_terminal,
        }
        if shape != "legacy":
            stored["effect_state"] = effect_state
        if shape == "current":
            stored["failure_persisted"] = failure_persisted
            stored["notification_state"] = notification_state
        self.state.get_db(TERMINAL_OPERATION_TABLE).update_store(
            terminal_operation_id(package_id),
            json.dumps(stored, sort_keys=True, separators=(",", ":")),
        )
        return self.evidence(package_id, created)

    def submitted_comments(self):
        return [call[0]["comment"] for call in self.state.device.add_links_calls]

    def statistic(self, key):
        raw = self.state.get_db("statistics").retrieve(key)
        return 0 if raw is None else int(raw)

    def call(self, rule, payload):
        route = route_for(rule)
        with self.patched(payload):
            return route.callback()

    @contextlib.contextmanager
    def patched(self, payload):
        with (
            mock.patch("quasarr.api.sponsors_helper.shared_state", self.state),
            mock.patch("quasarr.api.sponsors_helper.request", mock.Mock(json=payload)),
            mock.patch(
                "quasarr.api.sponsors_helper.update_release_notification",
                self.notifications,
            ),
            mock.patch(
                "quasarr.downloads.update_release_notification", self.notifications
            ),
            mock.patch(
                "quasarr.downloads.get_download_category_from_package_id",
                return_value="movies",
            ),
            mock.patch(
                "quasarr.downloads.get_download_category_mirrors", return_value=[]
            ),
        ):
            yield

    def concurrently(self, rule, payload, gate):
        """Two identical requests, the second arriving while the first works."""
        callbacks = [route_for(rule).callback, route_for(rule).callback]
        answers = {}
        failures = {}

        def send(name, callback):
            try:
                answers[name] = callback()
            except BaseException as e:  # noqa: BLE001 - reported as the failure
                failures[name] = e

        with self.patched(payload):
            first = threading.Thread(
                target=send, args=("first", callbacks[0]), name="first"
            )
            first.start()
            self.assertTrue(
                gate.first.wait(timeout=10),
                msg="the first request never reached its side effect",
            )
            second = threading.Thread(
                target=send, args=("second", callbacks[1]), name="second"
            )
            second.start()
            # A serialized request can never reach the parked side effect; one
            # that races into it does, and this window is what exposes it.
            gate.second.wait(timeout=0.5)
            gate.release.set()
            for thread in (first, second):
                thread.join(timeout=15)

        self.assertEqual(
            [], [thread.name for thread in (first, second) if thread.is_alive()]
        )
        self.assertEqual({}, failures)
        return answers["first"], answers["second"]

    def download_payload(self, package_id=None, urls=None, **extra):
        package_id = package_id or package()
        payload = {
            "name": TITLE,
            "package_id": package_id,
            "urls": [FINAL_URL] if urls is None else urls,
            "password": "",
            "notification": {"solvers": []},
        }
        payload.update(extra)
        return payload

    def version_two(self, payload, package_id=None):
        payload = dict(payload)
        payload["protocol_version"] = 2
        payload["terminal_operation_id"] = terminal_operation_id(
            package_id or payload.get("package_id") or package()
        )
        return payload

    def notification_cases(self):
        return [call.args[2].value for call in self.notifications.call_args_list]

    def expect_status(self, rule, payload, status):
        with self.assertRaises(HTTPError) as context:
            self.call(rule, payload)
        self.assertEqual(status, context.exception.status_code)

    @contextlib.contextmanager
    def crash_after(self, ordinal, method="mutate_value"):
        """Lose the n-th operation-table transaction of the next request.

        Every durable phase of a terminal request is one such transaction, so
        naming its ordinal is how a crash is placed in an exact window between
        two side effects.
        """
        operations = self.state.get_db(TERMINAL_OPERATION_TABLE)
        original = getattr(operations, method)
        calls = {"count": 0}

        def crashing(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == ordinal:
                raise RuntimeError("terminal operation storage unavailable")
            return original(*args, **kwargs)

        with mock.patch.object(operations, method, crashing):
            yield calls


class LegacyTerminalRequestTests(TerminalConfirmationTestCase):
    """A request that names no protocol version keeps its exact old behavior."""

    def test_legacy_download_returns_the_unchanged_plain_text_body(self):
        result = self.call(DOWNLOAD_RULE, self.download_payload())

        self.assertEqual(f"Downloaded 1 download links for {TITLE}", result)
        self.assertIsNone(self.protected_row())
        self.assertIsNone(self.operation_row())
        self.assertEqual(1, len(self.state.device.add_links_calls))

    def test_legacy_disable_returns_the_unchanged_plain_text_body(self):
        result = self.call(DISABLE_RULE, {"package_id": package()})

        self.assertEqual(f"Package <y>{TITLE}</y> disabled", result)
        self.assertTrue(self.protected_row()["disabled"])
        self.assertIsNone(self.operation_row())

    def test_legacy_fail_returns_the_unchanged_plain_text_body(self):
        result = self.call(FAIL_RULE, {"package_id": package(), "name": TITLE})

        self.assertEqual(
            f'Package <y>{TITLE}</y> with ID <y>{package()}</y> marked as failed!"',
            result,
        )
        self.assertIsNone(self.protected_row())
        self.assertIsNotNone(self.failed_row())
        self.assertIsNone(self.operation_row())

    def test_a_legacy_download_never_queries_jdownloader_for_reconciliation(self):
        self.call(DOWNLOAD_RULE, self.download_payload())

        self.assertEqual(0, self.state.device.queries)


class TerminalProtocolValidationTests(TerminalConfirmationTestCase):
    def test_a_malformed_protocol_version_is_refused_before_any_operation(self):
        for version in (1, 3, "2", 2.0, True, None, [2]):
            with self.subTest(version=version):
                payload = self.download_payload()
                payload["protocol_version"] = version
                payload["terminal_operation_id"] = terminal_operation_id(package())
                self.expect_status(DOWNLOAD_RULE, payload, 400)

        self.assertIsNone(self.operation_row())
        self.assertIsNotNone(self.protected_row())
        self.assertEqual(0, len(self.state.device.add_links_calls))

    def test_a_malformed_operation_id_is_refused_before_any_operation(self):
        valid = terminal_operation_id(package())
        cases = (
            None,
            "",
            valid.upper(),
            valid[:63],
            valid + "0",
            terminal_operation_id(package(2)),
            7,
        )

        for operation_id in cases:
            with self.subTest(operation_id=str(operation_id)[:16]):
                payload = self.download_payload()
                payload["protocol_version"] = 2
                if operation_id is not None:
                    payload["terminal_operation_id"] = operation_id
                self.expect_status(DOWNLOAD_RULE, payload, 400)

        self.assertIsNone(self.operation_row())
        self.assertEqual(0, len(self.state.device.add_links_calls))

    def test_all_three_routes_validate_the_same_envelope(self):
        for rule, payload in (
            (DOWNLOAD_RULE, self.download_payload()),
            (DISABLE_RULE, {"package_id": package()}),
            (FAIL_RULE, {"package_id": package()}),
        ):
            with self.subTest(rule=rule):
                broken = dict(payload)
                broken["protocol_version"] = 2
                broken["terminal_operation_id"] = "not-a-digest"
                self.expect_status(rule, broken, 400)

        self.assertIsNone(self.operation_row())
        self.assertIsNotNone(self.protected_row())

    def test_reusing_one_operation_for_another_terminal_state_conflicts(self):
        self.call(DOWNLOAD_RULE, self.version_two(self.download_payload()))
        before = self.operation_row()

        self.expect_status(FAIL_RULE, self.version_two({"package_id": package()}), 409)

        self.assertEqual(before, self.operation_row())

    def test_no_capacity_refuses_the_operation_before_any_side_effect(self):
        with mock.patch(
            "quasarr.providers.terminal_operations.MAXIMUM_TERMINAL_OPERATIONS", 0
        ):
            self.expect_status(
                DOWNLOAD_RULE, self.version_two(self.download_payload()), 503
            )

        self.assertIsNone(self.operation_row())
        self.assertIsNotNone(self.protected_row())
        self.assertEqual(0, len(self.state.device.add_links_calls))


class UnreadableTerminalRecordTests(TerminalConfirmationTestCase):
    """A row this Quasarr cannot read may describe an effect that happened."""

    corrupt = {
        "post effect corruption": '{"state": "complete", "package_id"',
        "a future schema": json.dumps(
            {
                "schema_version": 3,
                "state": "complete",
                "terminal_state": "downloaded",
                "package_id": package(),
            },
            sort_keys=True,
        ),
        "a future phase": json.dumps(
            {
                "state": "reconciled",
                "terminal_state": "downloaded",
                "package_id": package(),
                "created_epoch": 1,
                "updated_epoch": 1,
                "package_removed": True,
                "package_terminal": True,
                "effect_state": "applied",
                "failure_persisted": True,
                "notification_state": "recorded",
            },
            sort_keys=True,
        ),
    }

    def seed_corrupt(self, raw, package_id=None):
        self.state.get_db(TERMINAL_OPERATION_TABLE).update_store(
            terminal_operation_id(package_id or package()), raw
        )
        return raw

    def raw_operation(self, package_id=None):
        return self.state.get_db(TERMINAL_OPERATION_TABLE).retrieve(
            terminal_operation_id(package_id or package())
        )

    def test_every_terminal_route_answers_service_unavailable(self):
        routes = (
            (DOWNLOAD_RULE, self.download_payload()),
            (DISABLE_RULE, {"package_id": package()}),
            (FAIL_RULE, {"package_id": package(), "name": TITLE}),
        )

        for label, raw in self.corrupt.items():
            for rule, payload in routes:
                with self.subTest(row=label, rule=rule):
                    self.seed_corrupt(raw)

                    self.expect_status(rule, self.version_two(payload), 503)

    def test_a_refused_request_authorizes_no_side_effect_at_all(self):
        before = self.protected_row()

        for label, raw in self.corrupt.items():
            for rule, payload in (
                (DOWNLOAD_RULE, self.download_payload()),
                (DISABLE_RULE, {"package_id": package()}),
                (FAIL_RULE, {"package_id": package(), "name": TITLE}),
            ):
                with self.subTest(row=label, rule=rule):
                    self.seed_corrupt(raw)

                    self.expect_status(rule, self.version_two(payload), 503)

                    self.assertEqual(raw, self.raw_operation())
                    self.assertEqual(before, self.protected_row())
                    self.assertIsNone(self.failed_row())
                    self.assertEqual([], self.state.device.add_links_calls)
                    self.assertEqual([], self.notifications.call_args_list)
                    self.assertEqual(0, self.statistic("packages_failed"))

    def test_a_readable_conflicting_row_still_answers_conflict(self):
        self.seed_operation("downloaded")

        self.expect_status(FAIL_RULE, self.version_two({"package_id": package()}), 409)

        self.assertEqual("downloaded", self.operation_row()["terminal_state"])


class DownloadTerminalConfirmationTests(TerminalConfirmationTestCase):
    def test_a_confirmed_download_is_structured_complete_and_submitted_once(self):
        result = self.call(DOWNLOAD_RULE, self.version_two(self.download_payload()))

        self.assertEqual(TERMINAL_BODY_KEYS, set(result))
        self.assertEqual(
            {
                "success": True,
                "terminal_state": "downloaded",
                "package_removed": True,
                "package_terminal": True,
                "package_id": package(),
            },
            result,
        )
        self.assertEqual(1, len(self.state.device.add_links_calls))
        self.assertIsNone(self.protected_row())
        self.assertEqual("complete", self.operation_row()["state"])
        self.assertEqual(["solved"], self.notification_cases())
        self.assertEqual(1, self.statistic("packages_downloaded"))
        self.assertEqual(1, self.statistic("captcha_decryptions_automatic"))

    def test_a_fresh_operation_never_queries_jdownloader_for_reconciliation(self):
        self.call(DOWNLOAD_RULE, self.version_two(self.download_payload()))

        self.assertEqual(0, self.state.device.queries)

    def test_a_complete_operation_replays_without_any_side_effect(self):
        payload = self.version_two(self.download_payload())
        first = self.call(DOWNLOAD_RULE, payload)
        self.notifications.reset_mock()
        calls = len(self.state.device.add_links_calls)
        downloaded = self.statistic("packages_downloaded")

        replay = self.call(DOWNLOAD_RULE, payload)

        self.assertEqual(first, replay)
        self.assertEqual(calls, len(self.state.device.add_links_calls))
        self.assertEqual(downloaded, self.statistic("packages_downloaded"))
        self.assertEqual([], self.notification_cases())

    def test_a_failed_submission_stays_prepared_and_reports_an_unconfirmed_failure(
        self,
    ):
        self.state.device.add_links_succeeds = False

        result = self.call(DOWNLOAD_RULE, self.version_two(self.download_payload()))

        self.assertEqual(
            {
                "success": False,
                "terminal_state": "downloaded",
                "package_removed": False,
                "package_terminal": False,
                "package_id": package(),
            },
            result,
        )
        self.assertEqual("prepared", self.operation_row()["state"])
        self.assertIsNotNone(self.protected_row())

    def test_a_retry_after_a_failed_submission_may_submit_again(self):
        payload = self.version_two(self.download_payload())
        self.state.device.add_links_succeeds = False
        self.call(DOWNLOAD_RULE, payload)
        self.state.device.add_links_succeeds = True

        result = self.call(DOWNLOAD_RULE, payload)

        self.assertTrue(result["success"])
        self.assertEqual(2, len(self.state.device.add_links_calls))
        self.assertGreater(self.state.device.queries, 0)

    def test_a_crash_after_submission_never_submits_a_second_time(self):
        payload = self.version_two(self.download_payload())
        operations = self.state.get_db(TERMINAL_OPERATION_TABLE)
        original = operations.mutate_value
        calls = {"count": 0}

        # begin, the durable attempt, then the submission this one loses.
        def fail_the_third_transaction(key, mutator):
            calls["count"] += 1
            if calls["count"] == 3:
                raise RuntimeError("terminal operation storage unavailable")
            return original(key, mutator)

        with mock.patch.object(operations, "mutate_value", fail_the_third_transaction):
            with self.assertRaises(HTTPError):
                self.call(DOWNLOAD_RULE, payload)

        self.assertEqual(1, len(self.state.device.add_links_calls))
        self.assertEqual("prepared", self.operation_row()["state"])
        self.assertEqual("attempting", self.operation_row()["effect_state"])

        result = self.call(DOWNLOAD_RULE, payload)

        self.assertTrue(result["success"])
        self.assertEqual(1, len(self.state.device.add_links_calls))
        self.assertGreater(self.state.device.queries, 0)
        self.assertEqual("complete", self.operation_row()["state"])
        self.assertEqual(1, self.statistic("packages_downloaded"))

    def test_a_completion_write_failure_never_duplicates_finalization(self):
        payload = self.version_two(self.download_payload())
        operations = self.state.get_db(TERMINAL_OPERATION_TABLE)
        original = operations.mutate_value
        calls = {"count": 0}

        def fail_the_fourth_transaction(key, mutator):
            calls["count"] += 1
            if calls["count"] == 4:
                raise RuntimeError("terminal completion unavailable")
            return original(key, mutator)

        with mock.patch.object(operations, "mutate_value", fail_the_fourth_transaction):
            with self.assertRaises(HTTPError):
                self.call(DOWNLOAD_RULE, payload)

        self.assertEqual("submitted", self.operation_row()["state"])
        self.assertIsNone(self.protected_row())
        self.assertEqual(1, len(self.state.device.add_links_calls))
        self.assertEqual(["solved"], self.notification_cases())
        self.assertEqual(1, self.statistic("packages_downloaded"))
        self.notifications.reset_mock()

        replay = self.call(DOWNLOAD_RULE, payload)

        self.assertTrue(replay["success"])
        self.assertEqual("complete", self.operation_row()["state"])
        self.assertEqual(1, len(self.state.device.add_links_calls))
        self.assertEqual([], self.notification_cases())
        self.assertEqual(1, self.statistic("packages_downloaded"))

    def test_an_unprovable_linkgrabber_never_authorizes_a_second_submission(self):
        payload = self.version_two(self.download_payload())
        self.seed_operation("downloaded", effect_state="attempting")
        self.state.device_available = False

        result = self.call(DOWNLOAD_RULE, payload)

        self.assertFalse(result["success"])
        self.assertEqual(0, len(self.state.device.add_links_calls))
        self.assertEqual("prepared", self.operation_row()["state"])
        self.assertIsNotNone(self.protected_row())

    def test_a_package_already_moved_to_the_download_list_counts_as_submitted(self):
        payload = self.version_two(self.download_payload())
        evidence = self.seed_operation("downloaded", effect_state="attempting")
        self.state.device.downloader_packages.append(
            {"uuid": 5, "comment": submission_comment(package(), evidence)}
        )

        result = self.call(DOWNLOAD_RULE, payload)

        self.assertTrue(result["success"])
        self.assertEqual(0, len(self.state.device.add_links_calls))
        self.assertEqual("complete", self.operation_row()["state"])

    def test_a_package_that_cannot_be_removed_keeps_the_operation_submitted(self):
        payload = self.version_two(self.download_payload())
        protected = self.state.get_db("protected")

        with mock.patch.object(protected, "delete", lambda key: True):
            result = self.call(DOWNLOAD_RULE, payload)

        self.assertEqual(
            {
                "success": False,
                "terminal_state": "downloaded",
                "package_removed": False,
                "package_terminal": False,
                "package_id": package(),
            },
            result,
        )
        self.assertEqual("submitted", self.operation_row()["state"])
        self.assertEqual([], self.notification_cases())

        retry = self.call(DOWNLOAD_RULE, payload)

        self.assertTrue(retry["success"])
        self.assertEqual(1, len(self.state.device.add_links_calls))
        self.assertEqual(["solved"], self.notification_cases())
        self.assertEqual(1, self.statistic("packages_downloaded"))

    def test_an_already_absent_package_counts_as_removed(self):
        payload = self.version_two(self.download_payload())
        self.state.get_db("protected").delete(package())

        result = self.call(DOWNLOAD_RULE, payload)

        self.assertTrue(result["success"])
        self.assertTrue(result["package_removed"])
        self.assertEqual([], self.notification_cases())

    def test_an_unusable_payload_fails_the_package_exactly_once(self):
        payload = self.version_two(self.download_payload(urls=[]))

        result = self.call(DOWNLOAD_RULE, payload)

        self.assertEqual(
            {
                "success": True,
                "terminal_state": "downloaded",
                "package_removed": True,
                "package_terminal": True,
                "package_id": package(),
            },
            result,
        )
        self.assertIsNotNone(self.failed_row())
        self.assertEqual(["failed"], self.notification_cases())
        self.assertEqual(1, self.statistic("failed_decryptions_automatic"))
        self.assertEqual(0, len(self.state.device.add_links_calls))

        replay = self.call(DOWNLOAD_RULE, payload)

        self.assertEqual(result, replay)
        self.assertEqual(["failed"], self.notification_cases())
        self.assertEqual(1, self.statistic("failed_decryptions_automatic"))

    def test_a_rejected_mirror_whitelist_fails_the_package_without_submitting(self):
        payload = self.version_two(self.download_payload())
        route = route_for(DOWNLOAD_RULE)

        with (
            mock.patch("quasarr.api.sponsors_helper.shared_state", self.state),
            mock.patch("quasarr.api.sponsors_helper.request", mock.Mock(json=payload)),
            mock.patch(
                "quasarr.api.sponsors_helper.update_release_notification",
                self.notifications,
            ),
            mock.patch(
                "quasarr.downloads.update_release_notification", self.notifications
            ),
            mock.patch(
                "quasarr.downloads.get_download_category_from_package_id",
                return_value="movies",
            ),
            mock.patch(
                "quasarr.downloads.get_download_category_mirrors",
                return_value=["ddownload"],
            ),
        ):
            result = route.callback()

        self.assertTrue(result["success"])
        self.assertTrue(result["package_removed"])
        self.assertEqual(0, len(self.state.device.add_links_calls))
        self.assertIsNotNone(self.failed_row())
        self.assertEqual(["failed"], self.notification_cases())


class FailTerminalConfirmationTests(TerminalConfirmationTestCase):
    def test_a_confirmed_failure_is_structured_complete_and_recorded_once(self):
        result = self.call(FAIL_RULE, self.version_two({"package_id": package()}))

        self.assertEqual(
            {
                "success": True,
                "terminal_state": "failed",
                "package_removed": True,
                "package_terminal": True,
                "package_id": package(),
            },
            result,
        )
        self.assertIsNone(self.protected_row())
        self.assertIsNotNone(self.failed_row())
        self.assertEqual(["failed"], self.notification_cases())
        self.assertEqual(1, self.statistic("failed_decryptions_automatic"))
        self.assertEqual(1, self.statistic("failed_downloads"))
        self.assertEqual("complete", self.operation_row()["state"])

    def test_a_replayed_failure_duplicates_no_history_notification_or_counter(self):
        payload = self.version_two({"package_id": package()})
        first = self.call(FAIL_RULE, payload)
        self.notifications.reset_mock()

        replay = self.call(FAIL_RULE, payload)

        self.assertEqual(first, replay)
        self.assertEqual([], self.notification_cases())
        self.assertEqual(1, self.statistic("failed_decryptions_automatic"))
        self.assertEqual(1, self.statistic("failed_downloads"))

    def test_a_cleanup_failure_keeps_the_operation_submitted_and_resumes(self):
        payload = self.version_two({"package_id": package()})
        protected = self.state.get_db("protected")

        with mock.patch.object(protected, "delete", lambda key: True):
            result = self.call(FAIL_RULE, payload)

        self.assertEqual(
            {
                "success": False,
                "terminal_state": "failed",
                "package_removed": False,
                "package_terminal": False,
                "package_id": package(),
            },
            result,
        )
        self.assertEqual("submitted", self.operation_row()["state"])
        self.assertEqual(["failed"], self.notification_cases())

        retry = self.call(FAIL_RULE, payload)

        self.assertTrue(retry["success"])
        self.assertEqual(["failed"], self.notification_cases())
        self.assertEqual(1, self.statistic("failed_decryptions_automatic"))
        self.assertEqual(1, self.statistic("failed_downloads"))

    def test_an_existing_failed_row_is_never_written_a_second_time(self):
        payload = self.version_two({"package_id": package()})

        # begin, the durable attempt, the two notification phases, then the
        # confirmation this request loses.
        with self.crash_after(5):
            with self.assertRaises(HTTPError):
                self.call(FAIL_RULE, payload)

        self.assertEqual(1, self.statistic("failed_downloads"))
        self.assertEqual(["failed"], self.notification_cases())

        retry = self.call(FAIL_RULE, payload)

        self.assertTrue(retry["success"])
        self.assertEqual(1, self.statistic("failed_downloads"))
        self.assertEqual(1, self.statistic("failed_decryptions_automatic"))
        self.assertEqual(["failed"], self.notification_cases())

    def test_a_version_two_failure_needs_the_package_the_operation_names(self):
        self.expect_status(
            FAIL_RULE,
            {
                "protocol_version": 2,
                "terminal_operation_id": terminal_operation_id(package()),
                "name": TITLE,
            },
            400,
        )
        self.assertIsNone(self.operation_row())
        self.assertIsNotNone(self.protected_row())


class DisableTerminalConfirmationTests(TerminalConfirmationTestCase):
    def test_a_confirmed_disable_keeps_the_package_and_marks_it_terminal(self):
        result = self.call(DISABLE_RULE, self.version_two({"package_id": package()}))

        self.assertEqual(
            {
                "success": True,
                "terminal_state": "disabled",
                "package_removed": False,
                "package_terminal": True,
                "package_id": package(),
            },
            result,
        )
        self.assertTrue(self.protected_row()["disabled"])
        self.assertEqual(["disabled"], self.notification_cases())
        self.assertEqual(1, self.statistic("failed_decryptions_automatic"))
        self.assertEqual(1, self.statistic("captcha_decryptions_automatic"))
        self.assertEqual("complete", self.operation_row()["state"])

    def test_a_replayed_disable_never_notifies_or_counts_twice(self):
        payload = self.version_two({"package_id": package()})
        first = self.call(DISABLE_RULE, payload)
        self.notifications.reset_mock()

        replay = self.call(DISABLE_RULE, payload)

        self.assertEqual(first, replay)
        self.assertEqual([], self.notification_cases())
        self.assertEqual(1, self.statistic("failed_decryptions_automatic"))
        self.assertEqual(1, self.statistic("captcha_decryptions_automatic"))

    def test_a_crash_before_confirmation_never_disables_twice(self):
        payload = self.version_two({"package_id": package()})

        # begin, the durable attempt, then the confirmation this one loses.
        with self.crash_after(3):
            with self.assertRaises(HTTPError):
                self.call(DISABLE_RULE, payload)

        self.assertTrue(self.protected_row()["disabled"])
        self.assertEqual(["disabled"], self.notification_cases())

        retry = self.call(DISABLE_RULE, payload)

        self.assertTrue(retry["success"])
        self.assertEqual(["disabled"], self.notification_cases())
        self.assertEqual(1, self.statistic("captcha_decryptions_automatic"))

    def test_a_disable_that_did_not_persist_is_reported_as_unconfirmed(self):
        payload = self.version_two({"package_id": package()})
        protected = self.state.get_db("protected")

        with mock.patch.object(protected, "update_store", lambda key, value: True):
            result = self.call(DISABLE_RULE, payload)

        self.assertEqual(
            {
                "success": False,
                "terminal_state": "disabled",
                "package_removed": False,
                "package_terminal": False,
                "package_id": package(),
            },
            result,
        )
        self.assertEqual("submitted", self.operation_row()["state"])

    def test_a_missing_package_is_already_terminal(self):
        self.state.get_db("protected").delete(package())

        result = self.call(DISABLE_RULE, self.version_two({"package_id": package()}))

        self.assertEqual(
            {
                "success": True,
                "terminal_state": "disabled",
                "package_removed": True,
                "package_terminal": True,
                "package_id": package(),
            },
            result,
        )
        self.assertEqual([], self.notification_cases())


class ConcurrentTerminalRequestTests(TerminalConfirmationTestCase):
    """Two helper retries of one operation may never both apply it.

    Each case parks the first request inside the exact irreversible step and
    lets a second identical request in behind it, so anything that decides to
    submit, fail or disable from a stale read is recorded twice.
    """

    def test_two_concurrent_downloads_submit_the_package_exactly_once(self):
        gate = SideEffectGate()
        self.state.device.before_add_links = gate.arrive

        first, second = self.concurrently(
            DOWNLOAD_RULE, self.version_two(self.download_payload()), gate
        )

        self.assertFalse(gate.second.is_set())
        self.assertEqual(1, len(self.state.device.add_links_calls))
        self.assertEqual(first, second)
        self.assertTrue(first["success"])
        self.assertTrue(first["package_removed"])
        self.assertEqual("complete", self.operation_row()["state"])
        self.assertIsNone(self.protected_row())
        self.assertEqual(["solved"], self.notification_cases())
        self.assertEqual(1, self.statistic("packages_downloaded"))
        self.assertEqual(1, self.statistic("captcha_decryptions_automatic"))

    def test_two_concurrent_failures_record_one_history_row_and_counter(self):
        gate = SideEffectGate()

        with mock.patch(
            "quasarr.api.sponsors_helper.commit_terminal_failure",
            gate.wrap(sponsors_helper_api.commit_terminal_failure),
        ):
            first, second = self.concurrently(
                FAIL_RULE, self.version_two({"package_id": package()}), gate
            )

        self.assertFalse(gate.second.is_set())
        self.assertEqual(1, gate.arrivals)
        self.assertEqual(first, second)
        self.assertTrue(first["success"])
        self.assertIsNotNone(self.failed_row())
        self.assertIsNone(self.protected_row())
        self.assertEqual(["failed"], self.notification_cases())
        self.assertEqual(1, self.statistic("failed_downloads"))
        self.assertEqual(1, self.statistic("failed_decryptions_automatic"))
        self.assertEqual("complete", self.operation_row()["state"])

    def test_two_concurrent_disables_transition_the_package_exactly_once(self):
        gate = SideEffectGate()
        protected = self.state.get_db("protected")

        with mock.patch.object(
            protected, "update_store", gate.wrap(protected.update_store)
        ):
            first, second = self.concurrently(
                DISABLE_RULE, self.version_two({"package_id": package()}), gate
            )

        self.assertFalse(gate.second.is_set())
        self.assertEqual(1, gate.arrivals)
        self.assertEqual(first, second)
        self.assertTrue(first["success"])
        self.assertTrue(first["package_terminal"])
        self.assertFalse(first["package_removed"])
        self.assertTrue(self.protected_row()["disabled"])
        self.assertEqual(["disabled"], self.notification_cases())
        self.assertEqual(1, self.statistic("captcha_decryptions_automatic"))
        self.assertEqual(1, self.statistic("failed_decryptions_automatic"))
        self.assertEqual("complete", self.operation_row()["state"])


class StaleFailedHistoryTests(TerminalConfirmationTestCase):
    """A failed row of an older lifecycle is not this operation's outcome.

    Package IDs are derived from the release, so a package that once failed and
    was added again carries that history while it is protected. Only the
    operation's own state may dedupe a terminal side effect.
    """

    def setUp(self):
        super().setUp()
        self.store_failed()

    def test_a_stale_failed_row_never_reports_success_without_submitting(self):
        result = self.call(DOWNLOAD_RULE, self.version_two(self.download_payload()))

        self.assertEqual(1, len(self.state.device.add_links_calls))
        self.assertTrue(result["success"])
        self.assertTrue(result["package_removed"])
        self.assertIsNone(self.protected_row())
        self.assertEqual(["solved"], self.notification_cases())
        self.assertEqual(1, self.statistic("packages_downloaded"))
        self.assertEqual(1, self.statistic("captcha_decryptions_automatic"))

    def test_a_stale_failed_row_never_skips_the_current_failure(self):
        result = self.call(FAIL_RULE, self.version_two({"package_id": package()}))

        self.assertTrue(result["success"])
        self.assertIsNone(self.protected_row())
        self.assertEqual(["failed"], self.notification_cases())
        self.assertEqual(1, self.statistic("failed_downloads"))
        self.assertEqual(1, self.statistic("failed_decryptions_automatic"))
        self.assertEqual("complete", self.operation_row()["state"])


class TerminalSubmissionProvenanceTests(TerminalConfirmationTestCase):
    """What a version-two submission leaves behind for a later retry to read."""

    def test_a_terminal_submission_names_the_operation_behind_it(self):
        self.call(DOWNLOAD_RULE, self.version_two(self.download_payload()))

        self.assertEqual(
            [submission_comment(package(), self.current_evidence())],
            self.submitted_comments(),
        )

    def test_a_legacy_submission_still_travels_the_bare_package_id(self):
        self.call(DOWNLOAD_RULE, self.download_payload())

        self.assertEqual([package()], self.submitted_comments())

    def test_a_terminal_failure_names_the_operation_behind_it(self):
        self.call(FAIL_RULE, self.version_two({"package_id": package()}))

        self.assertEqual(
            self.current_evidence(), self.failed_blob()["terminal_operation"]
        )

    def test_a_legacy_failure_records_no_operation_at_all(self):
        self.call(FAIL_RULE, {"package_id": package(), "name": TITLE})

        self.assertNotIn("terminal_operation", self.failed_blob())

    def test_a_terminal_disable_names_the_operation_behind_it(self):
        self.call(DISABLE_RULE, self.version_two({"package_id": package()}))

        self.assertEqual(
            self.current_evidence(), self.protected_row()["terminal_operation"]
        )

    def test_a_legacy_disable_records_no_operation_at_all(self):
        self.call(DISABLE_RULE, {"package_id": package()})

        self.assertTrue(self.protected_row()["disabled"])
        self.assertNotIn("terminal_operation", self.protected_row())


class ResumedDownloadProvenanceTests(TerminalConfirmationTestCase):
    """A resumed `prepared` download may only reconcile from its own evidence.

    Everything the crash window can leave behind - failed history, a package in
    a JDownloader list - is also produced by earlier lifecycles of the same
    release, by the automatic download path and by hand. Only the record proves
    whether this operation ever reached its side effect, and only the marker it
    submits with proves that the artifact found is the one it made.
    """

    def stale_artifacts(self):
        self.store_failed()
        self.state.device.downloader_packages.append({"uuid": 5, "comment": package()})

    def assert_submitted_once(self, result, evidence):
        self.assertTrue(result["success"])
        self.assertTrue(result["package_removed"])
        self.assertEqual(
            [submission_comment(package(), evidence)], self.submitted_comments()
        )
        self.assertIsNone(self.protected_row())
        self.assertEqual(["solved"], self.notification_cases())
        self.assertEqual(1, self.statistic("packages_downloaded"))
        self.assertEqual(1, self.statistic("captcha_decryptions_automatic"))
        self.assertEqual("complete", self.operation_row()["state"])

    def test_an_operation_that_never_started_submits_despite_stale_artifacts(self):
        evidence = self.seed_operation("downloaded")
        self.stale_artifacts()

        result = self.call(DOWNLOAD_RULE, self.version_two(self.download_payload()))

        self.assert_submitted_once(result, evidence)
        # Nothing about the world was read: the record already answered.
        self.assertEqual(0, self.state.device.queries)

    def test_a_legacy_prepared_row_is_resumed_as_never_started(self):
        evidence = self.seed_operation("downloaded", legacy=True)
        self.stale_artifacts()

        result = self.call(DOWNLOAD_RULE, self.version_two(self.download_payload()))

        self.assert_submitted_once(result, evidence)
        self.assertEqual(0, self.state.device.queries)

    def test_a_crash_after_submission_is_proven_by_this_operations_package(self):
        evidence = self.seed_operation("downloaded", effect_state="attempting")
        self.state.device.downloader_packages.append(
            {"uuid": 5, "comment": submission_comment(package(), evidence)}
        )

        result = self.call(DOWNLOAD_RULE, self.version_two(self.download_payload()))

        self.assertTrue(result["success"])
        self.assertEqual([], self.submitted_comments())
        self.assertIsNone(self.protected_row())
        self.assertEqual(["solved"], self.notification_cases())
        self.assertEqual(1, self.statistic("packages_downloaded"))
        self.assertEqual("complete", self.operation_row()["state"])

    def test_a_package_of_an_earlier_lifecycle_never_counts_as_submitted(self):
        evidence = self.seed_operation("downloaded", effect_state="attempting")
        self.state.device.downloader_packages.append({"uuid": 5, "comment": package()})

        result = self.call(DOWNLOAD_RULE, self.version_two(self.download_payload()))

        self.assert_submitted_once(result, evidence)
        self.assertGreater(self.state.device.queries, 0)

    def test_a_failed_row_of_an_earlier_lifecycle_never_ends_this_operation(self):
        evidence = self.seed_operation("downloaded", effect_state="attempting")
        self.store_failed()

        result = self.call(DOWNLOAD_RULE, self.version_two(self.download_payload()))

        self.assert_submitted_once(result, evidence)

    def test_history_this_operation_wrote_ends_it_without_submitting(self):
        evidence = self.seed_operation("downloaded", effect_state="attempting")
        self.store_failed(terminal_operation=evidence)

        result = self.call(DOWNLOAD_RULE, self.version_two(self.download_payload()))

        self.assertTrue(result["success"])
        self.assertTrue(result["package_removed"])
        self.assertEqual([], self.submitted_comments())
        self.assertEqual(0, self.state.device.queries)
        self.assertIsNone(self.protected_row())
        self.assertEqual([], self.notification_cases())
        self.assertEqual(0, self.statistic("packages_downloaded"))
        self.assertEqual("complete", self.operation_row()["state"])


class ResumedFailureProvenanceTests(TerminalConfirmationTestCase):
    """A resumed `prepared` failure records its own history exactly once."""

    def assert_failed_once(self, result, evidence):
        self.assertTrue(result["success"])
        self.assertIsNone(self.protected_row())
        self.assertEqual(evidence, self.failed_blob()["terminal_operation"])
        self.assertEqual(["failed"], self.notification_cases())
        self.assertEqual(1, self.statistic("failed_downloads"))
        self.assertEqual(1, self.statistic("failed_decryptions_automatic"))
        self.assertEqual("complete", self.operation_row()["state"])

    def test_an_operation_that_never_started_records_its_own_failure(self):
        evidence = self.seed_operation("failed")
        self.store_failed()

        result = self.call(FAIL_RULE, self.version_two({"package_id": package()}))

        self.assert_failed_once(result, evidence)

    def test_a_legacy_prepared_row_records_its_own_failure(self):
        evidence = self.seed_operation("failed", legacy=True)
        self.store_failed()

        result = self.call(FAIL_RULE, self.version_two({"package_id": package()}))

        self.assert_failed_once(result, evidence)

    def test_an_attempt_that_never_reached_history_records_it(self):
        evidence = self.seed_operation("failed", effect_state="attempting")
        self.store_failed()

        result = self.call(FAIL_RULE, self.version_two({"package_id": package()}))

        self.assert_failed_once(result, evidence)

    def test_history_this_operation_wrote_is_never_recorded_twice(self):
        evidence = self.seed_operation("failed", effect_state="attempting")
        self.store_failed(terminal_operation=evidence)

        result = self.call(FAIL_RULE, self.version_two({"package_id": package()}))

        self.assertTrue(result["success"])
        self.assertIsNone(self.protected_row())
        self.assertEqual("last time", self.failed_blob()["error"])
        self.assertEqual([], self.notification_cases())
        self.assertEqual(0, self.statistic("failed_downloads"))
        self.assertEqual(0, self.statistic("failed_decryptions_automatic"))
        self.assertEqual("complete", self.operation_row()["state"])

    def test_an_unstarted_failure_writes_before_it_ever_reads_history(self):
        # Nothing in history can answer for an operation that provably ran
        # nothing, so the first thing it does to history is record its own row.
        self.seed_operation("failed")
        self.store_failed()
        history = self.state.get_db("failed")
        history.events.clear()

        self.call(FAIL_RULE, self.version_two({"package_id": package()}))

        self.assertEqual("write", history.events[0])

    def test_an_interrupted_failure_reads_history_before_it_writes(self):
        self.seed_operation("failed", effect_state="attempting")
        self.store_failed()
        history = self.state.get_db("failed")
        history.events.clear()

        self.call(FAIL_RULE, self.version_two({"package_id": package()}))

        self.assertEqual("read", history.events[0])

    def test_unreadable_history_never_confirms_a_failure_it_cannot_prove(self):
        self.seed_operation("failed", effect_state="attempting")
        self.state.get_db("failed").unavailable = True

        result = self.call(FAIL_RULE, self.version_two({"package_id": package()}))

        self.assertFalse(result["success"])
        self.assertEqual("prepared", self.operation_row()["state"])
        self.assertIsNotNone(self.protected_row())
        self.assertEqual([], self.notification_cases())


class ResumedDisableProvenanceTests(TerminalConfirmationTestCase):
    """A resumed `prepared` disable applies its own transition exactly once."""

    def assert_disabled_once(self, result, evidence):
        self.assertTrue(result["success"])
        self.assertTrue(result["package_terminal"])
        self.assertFalse(result["package_removed"])
        self.assertTrue(self.protected_row()["disabled"])
        self.assertEqual(evidence, self.protected_row()["terminal_operation"])
        self.assertEqual(["disabled"], self.notification_cases())
        self.assertEqual(1, self.statistic("failed_decryptions_automatic"))
        self.assertEqual(1, self.statistic("captcha_decryptions_automatic"))
        self.assertEqual("complete", self.operation_row()["state"])

    def test_an_operation_that_never_started_disables_despite_a_stale_flag(self):
        self.store_protected(disabled=True)
        evidence = self.seed_operation("disabled")

        result = self.call(DISABLE_RULE, self.version_two({"package_id": package()}))

        self.assert_disabled_once(result, evidence)

    def test_a_legacy_prepared_row_applies_the_disable(self):
        self.store_protected(disabled=True)
        evidence = self.seed_operation("disabled", legacy=True)

        result = self.call(DISABLE_RULE, self.version_two({"package_id": package()}))

        self.assert_disabled_once(result, evidence)

    def test_an_attempt_that_never_reached_the_package_applies_it(self):
        self.store_protected(disabled=True)
        evidence = self.seed_operation("disabled", effect_state="attempting")

        result = self.call(DISABLE_RULE, self.version_two({"package_id": package()}))

        self.assert_disabled_once(result, evidence)

    def test_a_flag_this_operation_wrote_is_never_applied_twice(self):
        evidence = self.seed_operation("disabled", effect_state="attempting")
        self.store_protected(disabled=True, terminal_operation=evidence)

        result = self.call(DISABLE_RULE, self.version_two({"package_id": package()}))

        self.assertTrue(result["success"])
        self.assertTrue(result["package_terminal"])
        self.assertTrue(self.protected_row()["disabled"])
        self.assertEqual([], self.notification_cases())
        self.assertEqual(0, self.statistic("failed_decryptions_automatic"))
        self.assertEqual(0, self.statistic("captcha_decryptions_automatic"))
        self.assertEqual("complete", self.operation_row()["state"])


class TerminalFailurePersistenceTests(TerminalConfirmationTestCase):
    """Nothing a helper can see happens before the failure is durably this one's.

    Counting a failure, telling the operator about it and dropping the
    protected package are the three things a retry cannot take back, and none
    of them may answer for a history row that was never written.
    """

    def payload(self):
        return self.version_two({"package_id": package()})

    def assert_nothing_happened(self, result):
        self.state.get_db("failed").unavailable = False
        self.assertEqual(
            {
                "success": False,
                "terminal_state": "failed",
                "package_removed": False,
                "package_terminal": False,
                "package_id": package(),
            },
            result,
        )
        self.assertIsNone(self.failed_row())
        self.assertIsNotNone(self.protected_row())
        self.assertEqual([], self.notification_cases())
        self.assertEqual(0, self.statistic("failed_downloads"))
        self.assertEqual(0, self.statistic("failed_decryptions_automatic"))

    def test_a_history_write_that_never_lands_confirms_nothing(self):
        self.state.get_db("failed").unavailable = True

        result = self.call(FAIL_RULE, self.payload())

        self.assert_nothing_happened(result)
        self.assertEqual("prepared", self.operation_row()["state"])
        self.assertFalse(self.operation_row()["failure_persisted"])

    def test_a_history_write_that_never_lands_still_resumes_afterwards(self):
        history = self.state.get_db("failed")
        history.unavailable = True
        self.call(FAIL_RULE, self.payload())
        history.unavailable = False

        retry = self.call(FAIL_RULE, self.payload())

        self.assertTrue(retry["success"])
        self.assertEqual(
            self.current_evidence(), self.failed_blob()["terminal_operation"]
        )
        self.assertEqual(["failed"], self.notification_cases())
        self.assertEqual(1, self.statistic("failed_downloads"))
        self.assertEqual(1, self.statistic("failed_decryptions_automatic"))

    def test_the_row_and_both_counters_are_one_commit(self):
        self.call(FAIL_RULE, self.payload())

        self.assertEqual(
            [
                (
                    (TERMINAL_OPERATION_TABLE, terminal_operation_id(package())),
                    ("failed", package()),
                    ("statistics", "failed_downloads"),
                    ("statistics", "failed_decryptions_automatic"),
                )
            ],
            self.state.get_db(TERMINAL_OPERATION_TABLE).transactions,
        )

    def test_history_is_written_before_the_notification_and_the_removal(self):
        order = []
        operations = self.state.get_db(TERMINAL_OPERATION_TABLE)
        protected = self.state.get_db("protected")
        original = operations.mutate_values
        removal = protected.delete
        self.notifications.side_effect = lambda *a, **k: order.append("notify")

        def committed(targets, mutator):
            order.append("history")
            return original(targets, mutator)

        def removed(key):
            order.append("remove")
            return removal(key)

        with (
            mock.patch.object(operations, "mutate_values", committed),
            mock.patch.object(protected, "delete", removed),
        ):
            self.call(FAIL_RULE, self.payload())

        self.assertEqual(["history", "notify", "remove"], order)

    def test_a_crash_after_the_row_notifies_exactly_once_on_the_retry(self):
        payload = self.payload()

        # begin, the durable attempt, then the notification phase this one loses.
        with self.crash_after(3):
            with self.assertRaises(HTTPError):
                self.call(FAIL_RULE, payload)

        self.assertIsNotNone(self.failed_row())
        self.assertEqual(1, self.statistic("failed_downloads"))
        self.assertEqual([], self.notification_cases())
        self.assertIsNotNone(self.protected_row())

        retry = self.call(FAIL_RULE, payload)

        self.assertTrue(retry["success"])
        self.assertEqual(["failed"], self.notification_cases())
        self.assertEqual(1, self.statistic("failed_downloads"))
        self.assertEqual(1, self.statistic("failed_decryptions_automatic"))

    def test_a_dispatched_notification_is_never_sent_a_second_time(self):
        """The one step no store can undo is fenced before it is taken.

        A crash between dispatching the notification and recording it leaves a
        phase that cannot prove whether the operator saw it, and repeating it
        would be a visible duplicate, so it is never repeated.
        """
        payload = self.payload()

        # begin, the durable attempt, the pending notification phase, then the
        # phase that records it, which this request loses.
        with self.crash_after(4):
            with self.assertRaises(HTTPError):
                self.call(FAIL_RULE, payload)

        self.assertEqual(["failed"], self.notification_cases())
        self.assertEqual("attempting", self.operation_row()["notification_state"])

        retry = self.call(FAIL_RULE, payload)

        self.assertTrue(retry["success"])
        self.assertEqual(["failed"], self.notification_cases())
        self.assertEqual(1, self.statistic("failed_downloads"))
        self.assertEqual(1, self.statistic("failed_decryptions_automatic"))

    def test_the_pending_phase_is_durable_before_the_notification_is_sent(self):
        order = []
        self.notifications.side_effect = lambda *a, **k: order.append("notify")
        operations = self.state.get_db(TERMINAL_OPERATION_TABLE)
        original = operations.mutate_value

        def observed(key, mutator):
            value = original(key, mutator)
            order.append(json.loads(value)["notification_state"])
            return value

        with mock.patch.object(operations, "mutate_value", observed):
            self.call(FAIL_RULE, self.payload())

        self.assertLess(order.index("attempting"), order.index("notify"))
        self.assertLess(order.index("notify"), order.index("recorded"))

    def test_an_unusable_download_payload_persists_before_it_counts(self):
        self.state.get_db("failed").unavailable = True
        payload = self.version_two(self.download_payload(urls=[]))

        result = self.call(DOWNLOAD_RULE, payload)

        self.assertFalse(result["success"])
        self.assertIsNotNone(self.protected_row())
        self.assertEqual([], self.notification_cases())
        self.assertEqual(0, self.statistic("failed_decryptions_automatic"))
        self.assertEqual(0, self.statistic("failed_downloads"))

    def test_an_interrupted_download_failure_finishes_telling_the_operator(self):
        """The reason it wrote down is what the resumed notification carries.

        A download failure whose answer was lost still owes the operator the
        message it never sent, and the only place that reason survives is the
        history row - the operation record itself stays bounded.
        """
        payload = self.version_two(self.download_payload(urls=[]))

        # begin, the durable attempt, then the notification phase this one loses.
        with self.crash_after(3):
            with self.assertRaises(HTTPError):
                self.call(DOWNLOAD_RULE, payload)

        self.assertIsNotNone(self.failed_row())
        self.assertEqual([], self.notification_cases())

        retry = self.call(DOWNLOAD_RULE, payload)

        self.assertTrue(retry["success"])
        self.assertEqual(["failed"], self.notification_cases())
        self.assertEqual(
            "SponsorsHelper returned no final download links.",
            self.notifications.call_args.kwargs["details"]["reason"],
        )
        self.assertEqual(1, self.statistic("failed_downloads"))
        self.assertEqual(1, self.statistic("failed_decryptions_automatic"))
        self.assertEqual(0, len(self.state.device.add_links_calls))
        self.assertIsNone(self.protected_row())

    def test_a_rejected_mirror_whitelist_persists_before_it_counts(self):
        self.state.get_db("failed").unavailable = True
        payload = self.version_two(self.download_payload())
        route = route_for(DOWNLOAD_RULE)

        with (
            self.patched(payload),
            mock.patch(
                "quasarr.downloads.get_download_category_mirrors",
                return_value=["ddownload"],
            ),
        ):
            result = route.callback()

        self.assertFalse(result["success"])
        self.assertIsNotNone(self.protected_row())
        self.assertEqual([], self.notification_cases())
        self.assertEqual(0, self.statistic("failed_decryptions_automatic"))
        self.assertEqual(0, self.statistic("failed_downloads"))
        self.assertEqual(0, len(self.state.device.add_links_calls))


class MigratedTerminalRecordTests(TerminalConfirmationTestCase):
    """A seven-key row past `prepared` can never show what it applied.

    Its failed rows, disabled packages and JDownloader packages name no
    operation, so the absence of a marker is not evidence that this operation
    failed, and it is certainly not evidence that it solved anything.
    """

    RULES = {
        "downloaded": DOWNLOAD_RULE,
        "failed": FAIL_RULE,
        "disabled": DISABLE_RULE,
    }

    def request(self, terminal_state):
        if terminal_state == "downloaded":
            return DOWNLOAD_RULE, self.version_two(self.download_payload())
        return self.RULES[terminal_state], self.version_two({"package_id": package()})

    def assert_untouched(self, result, terminal_state):
        self.assertEqual(
            {
                "success": False,
                "terminal_state": terminal_state,
                "package_removed": False,
                "package_terminal": False,
                "package_id": package(),
            },
            result,
        )
        self.assertIsNotNone(self.protected_row())
        self.assertEqual([], self.notification_cases())
        self.assertEqual([], self.submitted_comments())
        self.assertEqual(0, self.state.device.queries)
        self.assertEqual(0, self.statistic("failed_downloads"))
        self.assertEqual(0, self.statistic("failed_decryptions_automatic"))
        self.assertEqual(0, self.statistic("captcha_decryptions_automatic"))
        self.assertEqual(0, self.statistic("packages_downloaded"))

    def test_a_protected_package_blocks_every_migrated_submitted_report(self):
        for terminal_state in ("downloaded", "failed", "disabled"):
            with self.subTest(terminal_state=terminal_state):
                self.setUp()
                self.seed_operation(terminal_state, state="submitted", legacy=True)
                rule, payload = self.request(terminal_state)

                result = self.call(rule, payload)

                self.assert_untouched(result, terminal_state)
                self.assertNotIn("disabled", self.protected_row())
                self.assertIsNone(self.failed_row())

    def test_an_absent_package_completes_a_migrated_report_without_effects(self):
        for terminal_state in ("downloaded", "failed", "disabled"):
            with self.subTest(terminal_state=terminal_state):
                self.setUp()
                self.seed_operation(terminal_state, state="submitted", legacy=True)
                self.state.get_db("protected").delete(package())
                rule, payload = self.request(terminal_state)

                result = self.call(rule, payload)

                self.assertEqual(
                    {
                        "success": True,
                        "terminal_state": terminal_state,
                        "package_removed": True,
                        "package_terminal": True,
                        "package_id": package(),
                    },
                    result,
                )
                self.assertEqual([], self.notification_cases())
                self.assertEqual([], self.submitted_comments())
                self.assertEqual(0, self.state.device.queries)
                self.assertIsNone(self.failed_row())
                self.assertEqual("complete", self.operation_row()["state"])

    def test_an_unmarked_failed_row_never_solves_a_migrated_download(self):
        self.seed_operation("downloaded", state="submitted", legacy=True)
        self.store_failed()

        result = self.call(DOWNLOAD_RULE, self.version_two(self.download_payload()))

        self.assert_untouched(result, "downloaded")
        self.assertEqual("last time", self.failed_blob()["error"])

    def test_an_unmarked_jdownloader_package_never_ends_a_migrated_download(self):
        self.seed_operation("downloaded", state="submitted", legacy=True)
        self.state.device.downloader_packages.append({"uuid": 5, "comment": package()})

        result = self.call(DOWNLOAD_RULE, self.version_two(self.download_payload()))

        self.assert_untouched(result, "downloaded")

    def test_an_unmarked_disabled_flag_never_confirms_a_migrated_disable(self):
        self.store_protected(disabled=True)
        self.seed_operation("disabled", state="submitted", legacy=True)

        result = self.call(DISABLE_RULE, self.version_two({"package_id": package()}))

        self.assertFalse(result["success"])
        self.assertFalse(result["package_terminal"])
        self.assertTrue(self.protected_row()["disabled"])
        self.assertNotIn("terminal_operation", self.protected_row())
        self.assertEqual([], self.notification_cases())
        self.assertEqual(0, self.statistic("captcha_decryptions_automatic"))

    def test_a_migrated_complete_row_replays_its_stored_outcome_only(self):
        for terminal_state in ("downloaded", "failed", "disabled"):
            with self.subTest(terminal_state=terminal_state):
                self.setUp()
                self.seed_operation(
                    terminal_state,
                    state="complete",
                    legacy=True,
                    # Inside retention, or admission would prune it and open a
                    # fresh operation instead of replaying this one.
                    created=int(time.time()),
                    package_removed=True,
                    package_terminal=True,
                )
                rule, payload = self.request(terminal_state)

                first = self.call(rule, payload)
                replay = self.call(rule, payload)

                self.assertEqual(first, replay)
                self.assertTrue(first["success"])
                self.assertTrue(first["package_removed"])
                self.assertEqual([], self.notification_cases())
                self.assertEqual([], self.submitted_comments())
                self.assertEqual(0, self.state.device.queries)
                # The protected row is the one thing a replay must not touch.
                self.assertIsNotNone(self.protected_row())
                self.assertIsNone(self.failed_row())

    def test_a_migrated_prepared_row_still_performs_its_own_transition(self):
        evidence = self.seed_operation("failed", legacy=True)

        result = self.call(FAIL_RULE, self.version_two({"package_id": package()}))

        self.assertTrue(result["success"])
        self.assertEqual(evidence, self.failed_blob()["terminal_operation"])
        self.assertEqual(["failed"], self.notification_cases())
        self.assertEqual(1, self.statistic("failed_downloads"))

    def test_an_eight_key_submitted_row_is_still_proven_by_its_marker(self):
        """The shape that already marked its artifacts is not blocked."""
        evidence = self.seed_operation(
            "downloaded", state="submitted", shape="effect", effect_state="applied"
        )
        self.state.device.downloader_packages.append(
            {"uuid": 5, "comment": submission_comment(package(), evidence)}
        )

        result = self.call(DOWNLOAD_RULE, self.version_two(self.download_payload()))

        self.assertTrue(result["success"])
        self.assertTrue(result["package_removed"])
        self.assertIsNone(self.protected_row())
        self.assertEqual(["solved"], self.notification_cases())


DEFER_RULE = "/sponsors_helper/api/defer/"
LIFECYCLE_CAPABILITIES = [
    "crypter_defer_v1",
    "filecrypt_cohort_sweep_v1",
    "filecrypt_link_lifecycle_v1",
]
DECRYPT_RULE = "/sponsors_helper/api/to_decrypt/"


class LifecycleTerminalBlacklistTests(TerminalConfirmationTestCase):
    """Terminal blacklist through the lifecycle /defer/ route."""

    def lifecycle_handout(self):
        payload = {
            "supported_urls": ["filecrypt.invalid"],
            "capabilities": list(LIFECYCLE_CAPABILITIES),
        }
        route = route_for(DECRYPT_RULE)
        with self.patched(payload):
            return route.callback()["to_decrypt"]

    def lifecycle_blocked(self, offer, pkg_id):
        return {
            "package_id": pkg_id,
            "crypter": "filecrypt",
            "reason_code": "ip_block_suspected",
            "link_fingerprint": offer["link_fingerprint"],
            "sweep_id": offer["sweep_id"],
            "offer_id": offer["offer_id"],
            "protocol_version": 2,
            "terminal_operation_id": terminal_operation_id(pkg_id),
        }

    def test_lifecycle_second_404_terminal_blacklist_workflow(self):
        """Retest BLOCKED → terminal failure → blacklist acknowledgement."""
        from quasarr.providers.crypter_candidates import link_fingerprint
        from quasarr.providers.filecrypt_lifecycle import (
            FILECRYPT_LINK_STATES_TABLE,
            encode_link_state,
        )
        from quasarr.providers.filecrypt_lifecycle_service import (
            OFFER_LEASE_SECONDS,
            FilecryptLifecycleService,
        )

        pkg_id = package()
        fp = link_fingerprint("filecrypt", "https://filecrypt.invalid/container/1")
        sweep_id = "a" * 32
        offer_id = "b" * 32
        top_id = terminal_operation_id(pkg_id)
        now = int(time.time())

        # Seed held link state with an active retest lease (hold expired)
        self.state.get_db(FILECRYPT_LINK_STATES_TABLE).update_store(
            fp,
            encode_link_state(
                {
                    "schema_version": 1,
                    "state": "held",
                    "first_blocked_epoch": now - 86400,
                    "retry_after_epoch": now - 1,
                    "lease": {
                        "sweep_id": sweep_id,
                        "offer_id": offer_id,
                        "package_id": pkg_id,
                        "offer_expires_epoch": now + OFFER_LEASE_SECONDS,
                    },
                }
            ),
        )

        # Call record_blocked directly (retest path)
        lifecycle = FilecryptLifecycleService(self.state)
        report = {
            "package_id": pkg_id,
            "crypter": "filecrypt",
            "reason_code": "ip_block_suspected",
            "link_fingerprint": fp,
            "sweep_id": sweep_id,
            "offer_id": offer_id,
            "protocol_version": 2,
            "terminal_operation_id": top_id,
        }
        protected_rows = self.state.get_db("protected").retrieve_all_titles()
        result = lifecycle.record_blocked(report, protected_rows)

        # Retest BLOCKED → terminal_required
        self.assertIsNotNone(result)
        self.assertTrue(result.get("terminal_required"))
        self.assertEqual(fp, result["fingerprint"])
        self.assertEqual(pkg_id, result["package_id"])

        # Now confirm the blacklist via the route's terminal workflow
        blacklist = lifecycle.confirm_blacklist(fp, offer_id, top_id)
        self.assertIsNotNone(blacklist)
        self.assertEqual("blacklist", blacklist["instruction"])
        self.assertEqual("individual", blacklist["state"])
        self.assertFalse(blacklist["terminal_required"])
        # No legacy fail() involved
        self.assertIsNone(self.state.get_db("failed").retrieve(pkg_id))

    def test_lifecycle_blacklist_lost_response_replays_once(self):
        """Lost blacklist response replays via receipt without duplicate effects."""
        from quasarr.providers.filecrypt_lifecycle import (
            FILECRYPT_LINK_STATES_TABLE,
            FILECRYPT_OFFER_RECEIPTS_TABLE,
            encode_link_state,
            encode_offer_receipt,
        )
        from quasarr.providers.filecrypt_lifecycle_decisions import (
            build_blacklist_decision,
        )
        from quasarr.providers.filecrypt_lifecycle_service import (
            FilecryptLifecycleService,
        )

        pkg_id = package()
        fp = terminal_operation_id(pkg_id)  # just need a 64-hex fingerprint
        # Use a real fingerprint from the stored links
        from quasarr.providers.crypter_candidates import link_fingerprint

        fp = link_fingerprint("filecrypt", "https://filecrypt.invalid/container/1")
        sweep_id = "a" * 32
        offer_id = "b" * 32
        top_id = terminal_operation_id(pkg_id)

        # Seed the receipt as if blacklist already completed
        blacklist_resp = build_blacklist_decision(
            sweep_id=sweep_id, sweep_deadline_epoch=int(time.time()) + 900
        )
        receipt = {
            "schema_version": 1,
            "generation_id": sweep_id,
            "fingerprint": fp,
            "package_id": pkg_id,
            "mode": "retest",
            "outcome": "blocked",
            "response": blacklist_resp,
            "accepted_epoch": int(time.time()),
            "expires_epoch": int(time.time()) + 30 * 24 * 3600,
        }
        self.state.get_db(FILECRYPT_OFFER_RECEIPTS_TABLE).update_store(
            offer_id, encode_offer_receipt(receipt)
        )
        # Seed link state as blacklisted (post-confirm)
        self.state.get_db(FILECRYPT_LINK_STATES_TABLE).update_store(
            fp,
            encode_link_state(
                {
                    "schema_version": 1,
                    "state": "blacklisted",
                    "first_blocked_epoch": int(time.time()) - 86400,
                    "blacklisted_epoch": int(time.time()),
                }
            ),
        )

        # Call record_blocked which should replay the blacklist receipt
        lifecycle = FilecryptLifecycleService(self.state)
        report = {
            "package_id": pkg_id,
            "crypter": "filecrypt",
            "reason_code": "ip_block_suspected",
            "link_fingerprint": fp,
            "sweep_id": sweep_id,
            "offer_id": offer_id,
            "protocol_version": 2,
            "terminal_operation_id": top_id,
        }
        protected_rows = self.state.get_db("protected").retrieve_all_titles()
        result = lifecycle.record_blocked(report, protected_rows)

        # Receipt replay returns blacklist without terminal_required
        self.assertIsNotNone(result)
        self.assertFalse(result.get("terminal_required", True))
        self.assertEqual("blacklist", result["instruction"])
        self.assertEqual(fp, result["fingerprint"])
        self.assertEqual(pkg_id, result["package_id"])
        # No duplicate effects: failed row untouched, no notification
        self.assertIsNone(self.state.get_db("failed").retrieve(pkg_id))


if __name__ == "__main__":
    unittest.main()
