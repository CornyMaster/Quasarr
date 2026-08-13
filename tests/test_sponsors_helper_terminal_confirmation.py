# -*- coding: utf-8 -*-

import contextlib
import json
import threading
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
    def __init__(self, rows=None):
        self.rows = dict(rows or {})
        self.writes = 0
        self.deletes = 0
        self.unavailable = False
        # Access order, so a read taken to decide something can be told from
        # one taken to verify a write that already happened.
        self.events = []

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
            self.tables[table] = MemoryTable()
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
    ):
        """Persist the record a crashed request would have left behind.

        `legacy` writes the exact seven-key row shipped before the effect
        phase existed, which is what a Quasarr that is upgraded mid-operation
        finds on disk.
        """
        package_id = package_id or package()
        stored = {
            "state": state,
            "terminal_state": terminal_state,
            "package_id": package_id,
            "created_epoch": created,
            "updated_epoch": created,
            "package_removed": False,
            "package_terminal": False,
        }
        if not legacy:
            stored["effect_state"] = effect_state
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
        operations = self.state.get_db(TERMINAL_OPERATION_TABLE)
        original = operations.mutate_value
        calls = {"count": 0}

        # begin, the durable attempt, then the confirmation this one loses.
        def fail_the_third_transaction(key, mutator):
            calls["count"] += 1
            if calls["count"] == 3:
                raise RuntimeError("terminal operation storage unavailable")
            return original(key, mutator)

        with mock.patch.object(operations, "mutate_value", fail_the_third_transaction):
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
        operations = self.state.get_db(TERMINAL_OPERATION_TABLE)
        original = operations.mutate_value
        calls = {"count": 0}

        # begin, the durable attempt, then the confirmation this one loses.
        def fail_the_third_transaction(key, mutator):
            calls["count"] += 1
            if calls["count"] == 3:
                raise RuntimeError("terminal operation storage unavailable")
            return original(key, mutator)

        with mock.patch.object(operations, "mutate_value", fail_the_third_transaction):
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
            "quasarr.api.sponsors_helper.fail",
            gate.wrap(sponsors_helper_api.fail),
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


if __name__ == "__main__":
    unittest.main()
