# -*- coding: utf-8 -*-

"""Carbon Downloads data contract.

Covers:
  - Device isolation: get_packages_for_device() never touches
    shared_state.get_device() and routes every query/auto-start operation
    through the supplied device.
  - Schema conformance: PackageListResponse/QueueRow/HistoryRow/DeferredRow
    carry exactly the documented fields, enum values, and no forbidden
    values (URLs, hostnames, fingerprints, sweep/offer/operation IDs,
    credentials).
  - Route behavior: GET /api/packages/list reads shared_state.values once,
    returns the empty connected:false shape when absent, and otherwise
    delegates only to get_packages_for_device().
  - Behavioral coverage: fail mode, inactive defer metadata, malformed rows,
    unknown ETA, raw (unescaped) name passthrough, and the latest-commit
    Filecrypt terminal-blacklist outcomes (continuing with alternatives /
    appearing once in failed history / disappearing entirely).
  - HistoryRow.error passes through _scrub_protected_links() so
    a raw exception message (downloads/__init__.py's generic "Unexpected
    error: {e}" reason) can never leak an embedded protected URL or bare
    hostname.
  - DeferredRow.state's "retest" value stays a valid/accepted
    schema enum value (see the validator test) but carbon._deferred_state()
    never emits it - an earlier attempt derived it from a second,
    independently-timed crypter_projection() read, which could observe a
    live decision transition read #1 (inside get_packages_for_device()) had
    not yet seen, producing an internally contradictory, flickering row.
    Removed; a non-zero cohort_retest_depth (the crypter-wide count, never a
    per-package fact) alone must never produce "retest".
"""

import json
import re
import unittest
from types import SimpleNamespace
from unittest import mock

from bottle import Bottle

import quasarr.api.packages as packages_api
import quasarr.api.packages.carbon as carbon
from quasarr.api.packages.carbon import (
    DEFERRED_STATE_VALUES,
    HISTORY_STATUS_VALUES,
    QUEUE_STATUS_VALUES,
    build_package_list_response,
    empty_package_list_response,
)
from quasarr.downloads.packages import get_packages_for_device
from quasarr.providers.auth import audit_route_auth_modes
from quasarr.providers.crypter_cooldowns import CrypterCooldownService

PACKAGE_A = "Quasarr_movies_" + "a" * 32
PACKAGE_B = "Quasarr_movies_" + "b" * 32
PACKAGE_C = "Quasarr_movies_" + "c" * 32
NOW = 1_700_000_000


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


# ---------------------------------------------------------------------------
# Shared schema definitions (mirrors the PackageListResponse contract in
# quasarr/api/packages/carbon.py verbatim)
# ---------------------------------------------------------------------------

PACKAGE_LIST_KEYS = {
    "connected",
    "linkgrabber",
    "deferred",
    "queue",
    "history",
    "other_queue",
    "other_history",
}
LINKGRABBER_KEYS = {"is_collecting", "is_stopped"}
# The four fields every row type carries from the persisted package origin.
ORIGIN_ROW_KEYS = {"crypter", "crypter_label", "mirror", "added_epoch"}
QUEUE_ROW_KEYS = {
    "package_id",
    "name",
    "category",
    "size_label",
    "size_bytes",
    "eta",
    "eta_unknown",
    "percentage",
    "status",
    "can_solve_captcha",
    "is_archive",
    "extraction_status",
    "storage",
} | ORIGIN_ROW_KEYS
HISTORY_ROW_KEYS = {
    "package_id",
    "name",
    "category",
    "size_label",
    "size_bytes",
    "status",
    "error",
} | ORIGIN_ROW_KEYS
DEFERRED_ROW_KEYS = {
    "package_id",
    "name",
    "state",
    "reason_label",
    "evidence_count",
    "retry_after_epoch",
    "probe_requested",
    "cohort_tested",
    "cohort_total",
    "cohort_deadline_epoch",
    "cohort_retest_depth",
    "can_solve_captcha",
} | ORIGIN_ROW_KEYS

# Any of these appearing verbatim in the serialized response is a leak.
_FORBIDDEN_PATTERNS = (
    re.compile(r"https?://"),
    re.compile(r"\b[0-9a-f]{64}\b"),  # link fingerprint / operation ID
    re.compile(r"\b[0-9a-f]{32}\b"),  # sweep ID / offer ID
)
_FORBIDDEN_KEY_NAMES = {
    "url",
    "urls",
    "password",
    "hostname",
    "sweep_id",
    "offer_id",
    "link_fingerprint",
    "link_fingerprints",
    "operation_id",
    "terminal_operation_id",
    "original_url",
}


# The row builders take the origin mapping as a REQUIRED argument so a caller
# cannot silently skip the join - an empty origin block is indistinguishable
# from a forgotten one in the response, and no test would catch it. These
# wrappers default it to "nothing stored" for the many cases that exercise
# something other than the origin.
def _deferred_row(item, origins=None):
    return carbon._build_deferred_row(item, origins or {})


def _queue_row(item, origins=None):
    return carbon._build_queue_row(item, origins or {})


def _history_row(item, origins=None):
    return carbon._build_history_row(item, origins or {})


def _assert_origin_fields_valid(test, row):
    """The origin block, identical in all three row types.

    `mirror` is the one deliberate exception to the no-hostname rule, so it is
    checked here rather than trusted: it must be a bare host, never anything
    carrying a scheme, path, credential, or query.
    """
    test.assertIsInstance(row["crypter"], str)
    test.assertIsInstance(row["crypter_label"], str)
    test.assertIsInstance(row["mirror"], str)
    for forbidden in ("://", "/", "@", "?", "#", " "):
        test.assertNotIn(forbidden, row["mirror"])
    test.assertIs(type(row["added_epoch"]), int)
    test.assertNotIsInstance(row["added_epoch"], bool)
    test.assertGreaterEqual(row["added_epoch"], 0)


def _assert_size_bytes_valid(test, row):
    test.assertIs(type(row["size_bytes"]), int)
    test.assertNotIsInstance(row["size_bytes"], bool)
    test.assertGreaterEqual(row["size_bytes"], 0)


def _assert_queue_row_valid(test, row):
    test.assertEqual(QUEUE_ROW_KEYS, set(row))
    _assert_origin_fields_valid(test, row)
    _assert_size_bytes_valid(test, row)
    test.assertIsInstance(row["package_id"], str)
    test.assertIsInstance(row["name"], str)
    test.assertIsInstance(row["category"], str)
    test.assertIsInstance(row["size_label"], str)
    test.assertIsInstance(row["eta"], str)
    test.assertIs(type(row["eta_unknown"]), bool)
    test.assertIs(type(row["percentage"]), int)
    test.assertNotIsInstance(row["percentage"], bool)
    test.assertGreaterEqual(row["percentage"], 0)
    test.assertLessEqual(row["percentage"], 100)
    test.assertIn(row["status"], QUEUE_STATUS_VALUES)
    test.assertIs(type(row["can_solve_captcha"]), bool)
    test.assertIs(type(row["is_archive"]), bool)
    test.assertIsInstance(row["extraction_status"], str)
    test.assertIsInstance(row["storage"], str)


def _assert_history_row_valid(test, row):
    test.assertEqual(HISTORY_ROW_KEYS, set(row))
    _assert_origin_fields_valid(test, row)
    _assert_size_bytes_valid(test, row)
    test.assertIsInstance(row["package_id"], str)
    test.assertIsInstance(row["name"], str)
    test.assertIsInstance(row["category"], str)
    test.assertIsInstance(row["size_label"], str)
    test.assertIn(row["status"], HISTORY_STATUS_VALUES)
    test.assertIsInstance(row["error"], str)


def _assert_deferred_row_valid(test, row):
    test.assertEqual(DEFERRED_ROW_KEYS, set(row))
    _assert_origin_fields_valid(test, row)
    test.assertIsInstance(row["package_id"], str)
    test.assertIsInstance(row["name"], str)
    test.assertIn(row["state"], DEFERRED_STATE_VALUES)
    test.assertIsInstance(row["crypter_label"], str)
    test.assertIsInstance(row["reason_label"], str)
    test.assertIs(type(row["evidence_count"]), int)
    test.assertGreaterEqual(row["evidence_count"], 0)
    test.assertIs(type(row["retry_after_epoch"]), int)
    test.assertGreaterEqual(row["retry_after_epoch"], 0)
    test.assertIs(type(row["probe_requested"]), bool)
    for field in (
        "cohort_tested",
        "cohort_total",
        "cohort_deadline_epoch",
        "cohort_retest_depth",
    ):
        test.assertIs(type(row[field]), int)
        test.assertGreaterEqual(row[field], 0)
    test.assertIs(type(row["can_solve_captcha"]), bool)


def assert_forbidden_value_free(test, response):
    """No URL, hostname-in-a-URL, fingerprint, sweep/offer/operation ID, or
    credential anywhere in the serialized response.
    """
    serialized = json.dumps(response)
    for pattern in _FORBIDDEN_PATTERNS:
        test.assertIsNone(
            pattern.search(serialized),
            f"forbidden pattern {pattern.pattern!r} found in response",
        )

    def _walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                test.assertNotIn(str(key).lower(), _FORBIDDEN_KEY_NAMES)
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(response)


def assert_valid_package_list_response(test, response):
    test.assertEqual(PACKAGE_LIST_KEYS, set(response))
    test.assertIs(type(response["connected"]), bool)
    test.assertEqual(LINKGRABBER_KEYS, set(response["linkgrabber"]))
    test.assertIs(type(response["linkgrabber"]["is_collecting"]), bool)
    test.assertIs(type(response["linkgrabber"]["is_stopped"]), bool)

    for row in response["queue"] + response["other_queue"]:
        _assert_queue_row_valid(test, row)
    for row in response["history"] + response["other_history"]:
        _assert_history_row_valid(test, row)
    for row in response["deferred"]:
        _assert_deferred_row_valid(test, row)

    assert_forbidden_value_free(test, response)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class MemoryDatabase:
    """A minimal in-memory DataBase double. `tables` is shared across every
    table of one shared_state so `mutate_values()` (used by e.g.
    CrypterCooldownService.observe()) can atomically touch several tables at
    once, exactly like the real sqlite-backed primitive.
    """

    def __init__(self, tables=None):
        self.rows = {}
        self.tables = {} if tables is None else tables

    def _peer(self, table):
        if table not in self.tables:
            self.tables[table] = MemoryDatabase(tables=self.tables)
        return self.tables[table]

    def retrieve(self, key):
        return self.rows.get(key)

    def retrieve_all_titles(self):
        items = [[key, value] for key, value in sorted(self.rows.items())]
        return items if items else None

    def mutate_value(self, key, mutator):
        value = mutator(self.rows.get(key))
        if value is None:
            self.rows.pop(key, None)
        else:
            self.rows[key] = value
        return value

    def mutate_values(self, targets, mutator):
        """One transaction over several tables, like the sqlite primitive."""
        databases = [self._peer(table) for table, _key in targets]
        values = mutator(
            tuple(
                database.rows.get(key)
                for database, (_table, key) in zip(databases, targets, strict=True)
            )
        )
        for database, (_table, key), value in zip(
            databases, targets, values, strict=True
        ):
            if value is None:
                database.rows.pop(key, None)
            else:
                database.rows[key] = value
        return tuple(values)

    def delete(self, key):
        self.rows.pop(key, None)
        return True

    def delete_exact(self, key, value):
        if self.rows.get(key) != value:
            return False
        self.rows.pop(key)
        return True


class RecordingFakeDevice:
    """A minimal JDownloader device double that records every call made to
    it, so a test can prove no *other* device (e.g. one obtained through
    shared_state.get_device()) was ever used.
    """

    def __init__(
        self,
        linkgrabber_packages=(),
        linkgrabber_links=(),
        downloader_packages=(),
        downloader_links=(),
        is_collecting=False,
    ):
        self.calls = []
        self._linkgrabber_packages = list(linkgrabber_packages)
        self._linkgrabber_links = list(linkgrabber_links)
        self._downloader_packages = list(downloader_packages)
        self._downloader_links = list(downloader_links)
        self._is_collecting = is_collecting

        self.linkgrabber = SimpleNamespace(
            query_packages=lambda: self._linkgrabber_packages,
            query_links=lambda: self._linkgrabber_links,
            is_collecting=lambda: self._is_collecting,
            cleanup=self._recorder("linkgrabber.cleanup"),
            remove_links=self._recorder("linkgrabber.remove_links"),
            move_to_downloadlist=self._recorder("linkgrabber.move_to_downloadlist"),
        )
        self.downloads = SimpleNamespace(
            query_packages=lambda: self._downloader_packages,
            query_links=lambda: self._downloader_links,
            remove_links=self._recorder("downloads.remove_links"),
            cleanup=self._recorder("downloads.cleanup"),
        )
        self.extraction = SimpleNamespace(
            get_archive_info=self._recorder("extraction.get_archive_info", result=[]),
        )

    def _recorder(self, name, result=None):
        def _fn(*args, **kwargs):
            self.calls.append(name)
            return result

        return _fn


class RaisingSharedState:
    """A shared_state double whose get_device() always raises, so any code
    path that still tries the legacy device-retry resolver fails the test
    immediately instead of silently succeeding with the "wrong" device.
    """

    def __init__(self, protected_rows=(), failed_rows=(), block_mode="defer"):
        self.values = {"crypter_block_mode": block_mode}
        # All tables share one `tables` dict so mutate_values() transactions
        # (e.g. CrypterCooldownService.observe()) can reach any peer table.
        self.databases = {}
        self.databases["protected"] = MemoryDatabase(tables=self.databases)
        self.databases["failed"] = MemoryDatabase(tables=self.databases)
        for package_id, blob in protected_rows:
            self.databases["protected"].rows[package_id] = blob
        for package_id, blob in failed_rows:
            self.databases["failed"].rows[package_id] = blob
        self.get_device_calls = 0

    def get_db(self, table):
        if table not in self.databases:
            self.databases[table] = MemoryDatabase(tables=self.databases)
        return self.databases[table]

    def get_device(self):
        self.get_device_calls += 1
        raise AssertionError(
            "get_packages_for_device() must never call shared_state.get_device()"
        )


def linkgrabber_package(uuid, name, comment, bytes_total=1024 * 1024 * 100):
    return {
        "uuid": uuid,
        "name": name,
        "comment": comment,
        "bytesTotal": bytes_total,
        "saveTo": "/downloads",
    }


def linkgrabber_link(
    uuid,
    package_uuid,
    name,
    url,
    availability="online",
    status="",
    status_icon="",
    comment=None,
):
    return {
        "uuid": uuid,
        "packageUUID": package_uuid,
        "name": name,
        "url": url,
        "availability": availability,
        "status": status,
        "statusIconKey": status_icon,
        "finished": False,
        "comment": comment,
    }


def downloader_package(
    uuid, name, comment, bytes_total=1024 * 1024 * 50, bytes_loaded=0, eta=120
):
    return {
        "uuid": uuid,
        "name": name,
        "comment": comment,
        "bytesTotal": bytes_total,
        "bytesLoaded": bytes_loaded,
        "eta": eta,
        "saveTo": "/downloads",
    }


def downloader_link(
    uuid,
    package_uuid,
    name,
    url,
    availability="",
    status="",
    status_icon="",
    finished=False,
):
    return {
        "uuid": uuid,
        "packageUUID": package_uuid,
        "name": name,
        "url": url,
        "availability": availability,
        "status": status,
        "statusIconKey": status_icon,
        "finished": finished,
    }


def protected_blob(
    title="Synthetic Release", links=None, deferred=None, password="secret-pw"
):
    blob = {
        "title": title,
        "links": links or [["https://filecrypt.invalid/c/1", "filecrypt"]],
        "password": password,
        "size_mb": 700,
        "original_url": "https://source.invalid/release/synthetic",
    }
    if deferred is not None:
        blob["deferred"] = deferred
    return json.dumps(blob)


def failed_blob(title="Synthetic Failure", reason="Synthetic download error"):
    return json.dumps({"title": title, "error": reason})


def deferred_block(
    *,
    hold_type="provisional",
    state="available",
    active=True,
    evidence_count=1,
    retry_after_epoch=4_000_000_000,
    probe_requested=False,
    cohort_tested=0,
    cohort_total=0,
    cohort_deadline_epoch=0,
    cohort_retest_depth=0,
    crypter="filecrypt",
    reason_code="ip_block_suspected",
):
    return {
        "crypter": crypter,
        "reason_code": reason_code,
        "since_epoch": 1_700_000_000,
        "retry_after_epoch": retry_after_epoch,
        "probe_requested": probe_requested,
        "observation_holds": 1,
        "state": state,
        "evidence_count": evidence_count,
        "hold_type": hold_type,
        "active": active,
        "cohort_tested": cohort_tested,
        "cohort_total": cohort_total,
        "cohort_deadline_epoch": cohort_deadline_epoch,
        "cohort_retest_depth": cohort_retest_depth,
    }


def _patch_category(return_value="movies"):
    return mock.patch(
        "quasarr.downloads.packages.get_download_category_from_package_id",
        return_value=return_value,
    )


# ---------------------------------------------------------------------------
# 1. Device isolation (service test, written and proven RED first)
# ---------------------------------------------------------------------------


class DeviceIsolationTests(unittest.TestCase):
    """get_packages_for_device() must use ONLY the supplied device: never
    shared_state.get_device(), for cache construction, cleanup calls, archive
    detection, or auto-start.
    """

    def test_never_touches_shared_state_get_device_and_uses_only_the_supplied_device(
        self,
    ):
        lg_package = linkgrabber_package("lg-uuid-1", "Synthetic.Release.LG", PACKAGE_A)
        # Three links across two domains: one offline (cleaned via
        # linkgrabber.cleanup), one not-downloadable (removed via
        # linkgrabber.remove_links), one healthy (keeps the mirror online so
        # both cleanups fire instead of failing the whole package).
        # Auto-start resolves its comment from the LINKS, not the package, so
        # every link carries the package's comment too.
        lg_links = [
            linkgrabber_link(
                "lg-link-offline",
                "lg-uuid-1",
                "file1.mkv",
                "https://mirror-a.invalid/1",
                availability="offline",
                comment=PACKAGE_A,
            ),
            linkgrabber_link(
                "lg-link-notdownloadable",
                "lg-uuid-1",
                "file2.mkv",
                "https://mirror-b.invalid/2",
                status="Not downloadable!",
                comment=PACKAGE_A,
            ),
            linkgrabber_link(
                "lg-link-online",
                "lg-uuid-1",
                "file3.mkv",
                "https://mirror-c.invalid/3",
                availability="online",
                comment=PACKAGE_A,
            ),
        ]

        dl_package = downloader_package(
            "dl-uuid-1", "Synthetic.Release.DL", PACKAGE_B, eta=60
        )
        dl_links = [
            downloader_link(
                "dl-link-error",
                "dl-uuid-1",
                "file1.mkv",
                "https://mirror-d.invalid/1",
                status_icon="false",
            ),
            downloader_link(
                "dl-link-online",
                "dl-uuid-1",
                "file2.mkv",
                "https://mirror-e.invalid/2",
                availability="online",
                finished=True,
            ),
        ]

        device = RecordingFakeDevice(
            linkgrabber_packages=[lg_package],
            linkgrabber_links=lg_links,
            downloader_packages=[dl_package],
            downloader_links=dl_links,
            is_collecting=False,
        )
        shared_state = RaisingSharedState()

        with _patch_category():
            downloads = get_packages_for_device(shared_state, device, auto_start=True)

        # No exception means shared_state.get_device() (which raises) was
        # never invoked by any query, cleanup, archive-detection, or
        # auto-start call inside get_packages_for_device().
        self.assertEqual(0, shared_state.get_device_calls)

        # And the supplied device really was exercised for every operation
        # this scenario is designed to trigger.
        self.assertIn("linkgrabber.cleanup", device.calls)
        self.assertIn("linkgrabber.remove_links", device.calls)
        self.assertIn("downloads.remove_links", device.calls)
        self.assertIn("extraction.get_archive_info", device.calls)
        self.assertIn("linkgrabber.move_to_downloadlist", device.calls)

        self.assertTrue(downloads["queue"] or downloads["history"])

    def test_legacy_get_packages_still_resolves_its_device_through_get_device(self):
        from quasarr.downloads.packages import get_packages

        device = RecordingFakeDevice()

        class LegacySharedState(RaisingSharedState):
            def get_device(self):
                self.get_device_calls += 1
                return device

        shared_state = LegacySharedState()

        with _patch_category():
            get_packages(shared_state, auto_start=False)

        self.assertEqual(1, shared_state.get_device_calls)


# ---------------------------------------------------------------------------
# 2. Schema conformance (written and proven RED first)
# ---------------------------------------------------------------------------


class SchemaValidatorSelfTests(unittest.TestCase):
    """Proves the validator actually rejects what it claims to reject."""

    def _valid_response(self):
        return empty_package_list_response()

    def test_accepts_a_well_formed_empty_response(self):
        assert_valid_package_list_response(self, self._valid_response())

    def test_rejects_an_unknown_top_level_field(self):
        response = self._valid_response()
        response["extra_field"] = "unexpected"
        with self.assertRaises(AssertionError):
            assert_valid_package_list_response(self, response)

    def test_rejects_a_missing_top_level_field(self):
        response = self._valid_response()
        del response["deferred"]
        with self.assertRaises(AssertionError):
            assert_valid_package_list_response(self, response)

    def test_rejects_an_unknown_queue_row_field(self):
        response = self._valid_response()
        response["queue"] = [
            {
                "package_id": PACKAGE_A,
                "name": "x",
                "category": "movies",
                "size_label": "1 GB",
                "eta": "",
                "eta_unknown": True,
                "percentage": 0,
                "status": "queued",
                "can_solve_captcha": False,
                "is_archive": False,
                "extraction_status": "",
                "storage": "",
                "source_url": "https://leak.invalid",
            }
        ]
        with self.assertRaises(AssertionError):
            assert_valid_package_list_response(self, response)

    def test_rejects_an_invalid_queue_status_value(self):
        response = self._valid_response()
        row = {
            "package_id": PACKAGE_A,
            "name": "x",
            "category": "movies",
            "size_label": "1 GB",
            "eta": "",
            "eta_unknown": True,
            "percentage": 0,
            "status": "paused",  # not one of the four allowed values
            "can_solve_captcha": False,
            "is_archive": False,
            "extraction_status": "",
            "storage": "",
        }
        response["queue"] = [row]
        with self.assertRaises(AssertionError):
            assert_valid_package_list_response(self, response)

    def test_rejects_an_invalid_deferred_state_value(self):
        response = self._valid_response()
        response["deferred"] = [
            {
                "package_id": PACKAGE_A,
                "name": "x",
                "state": "sweeping",  # not one of the four allowed values
                "crypter_label": "Filecrypt",
                "reason_label": "IP access block suspected",
                "evidence_count": 0,
                "retry_after_epoch": 0,
                "probe_requested": False,
                "cohort_tested": 0,
                "cohort_total": 0,
                "cohort_deadline_epoch": 0,
                "cohort_retest_depth": 0,
                "can_solve_captcha": True,
            }
        ]
        with self.assertRaises(AssertionError):
            assert_valid_package_list_response(self, response)

    def test_retest_stays_an_accepted_deferred_state_value(self):
        # "retest" is reserved by the schema contract even though the
        # current backend (carbon._deferred_state()) never emits it - see
        # DeferredRowStateMappingTests. The validator - and therefore the
        # Downloads page and any future emitter - must still accept it as
        # valid.
        response = self._valid_response()
        response["deferred"] = [
            {
                "package_id": PACKAGE_A,
                "name": "x",
                "state": "retest",
                "crypter": "filecrypt",
                "crypter_label": "FileCrypt",
                "mirror": "filecrypt.invalid",
                "added_epoch": NOW,
                "reason_label": "IP access block suspected",
                "evidence_count": 0,
                "retry_after_epoch": 0,
                "probe_requested": False,
                "cohort_tested": 3,
                "cohort_total": 5,
                "cohort_deadline_epoch": 0,
                "cohort_retest_depth": 2,
                "can_solve_captcha": True,
            }
        ]
        assert_valid_package_list_response(self, response)

    def test_rejects_a_protected_url_anywhere_in_the_response(self):
        response = self._valid_response()
        response["history"] = [
            {
                "package_id": PACKAGE_A,
                "name": "x",
                "category": "movies",
                "size_label": "1 GB",
                "status": "failed",
                "error": "Failed: https://filecrypt.invalid/c/1 unreachable",
            }
        ]
        with self.assertRaises(AssertionError):
            assert_valid_package_list_response(self, response)

    def test_rejects_a_64_hex_fingerprint_anywhere_in_the_response(self):
        response = self._valid_response()
        response["history"] = [
            {
                "package_id": PACKAGE_A,
                "name": "x",
                "category": "movies",
                "size_label": "1 GB",
                "status": "failed",
                "error": "fp=" + "a" * 64,
            }
        ]
        with self.assertRaises(AssertionError):
            assert_valid_package_list_response(self, response)

    def test_rejects_a_32_hex_sweep_id_anywhere_in_the_response(self):
        response = self._valid_response()
        response["history"] = [
            {
                "package_id": PACKAGE_A,
                "name": "x",
                "category": "movies",
                "size_label": "1 GB",
                "status": "failed",
                "error": "sweep=" + "b" * 32,
            }
        ]
        with self.assertRaises(AssertionError):
            assert_valid_package_list_response(self, response)

    def test_rejects_a_forbidden_key_name_anywhere_in_the_response(self):
        response = self._valid_response()
        response["queue"] = [
            {
                "package_id": PACKAGE_A,
                "name": "x",
                "category": "movies",
                "size_label": "1 GB",
                "eta": "",
                "eta_unknown": True,
                "percentage": 0,
                "status": "queued",
                "can_solve_captcha": False,
                "is_archive": False,
                "extraction_status": "",
                "storage": "",
            }
        ]
        response["deferred"] = [{"sweep_id": "irrelevant"}]
        with self.assertRaises(AssertionError):
            assert_valid_package_list_response(self, response)


# ---------------------------------------------------------------------------
# 3. Projection behavior against real data shapes
# ---------------------------------------------------------------------------


class PackageListProjectionTests(unittest.TestCase):
    def _build(self, shared_state, device, **kwargs):
        with _patch_category():
            return build_package_list_response(shared_state, device, **kwargs)

    def test_empty_response_shape_when_nothing_is_connected(self):
        response = empty_package_list_response()
        self.assertFalse(response["connected"])
        assert_valid_package_list_response(self, response)

    def test_ordinary_protected_package_appears_in_queue_as_waiting_captcha(self):
        shared_state = RaisingSharedState(
            protected_rows=[(PACKAGE_A, protected_blob())]
        )
        device = RecordingFakeDevice()

        response = self._build(shared_state, device, auto_start=False)

        assert_valid_package_list_response(self, response)
        self.assertEqual(1, len(response["queue"]))
        self.assertEqual(0, len(response["deferred"]))
        row = response["queue"][0]
        self.assertEqual(PACKAGE_A, row["package_id"])
        self.assertEqual("waiting_captcha", row["status"])
        self.assertTrue(row["can_solve_captcha"])
        self.assertEqual("Synthetic Release", row["name"])

    def _build_with_clock(self, shared_state, device, clock, **kwargs):
        # CrypterCooldownService(shared_state) has no clock parameter on the
        # production call path, so a deterministic clock is injected the same
        # way test_deferred_protected_packages.py does it. carbon.py itself
        # holds no reference to CrypterCooldownService (the second,
        # independently-timed retest read was removed), so only the
        # downloads.packages construction site needs patching.
        def build_service(inner_shared_state):
            return CrypterCooldownService(inner_shared_state, clock=clock)

        with (
            _patch_category(),
            mock.patch(
                "quasarr.downloads.packages.CrypterCooldownService", build_service
            ),
        ):
            return build_package_list_response(shared_state, device, **kwargs)

    def test_active_deferred_package_appears_only_in_deferred_not_queue(self):
        # A real provisional hold, written through CrypterCooldownService
        # exactly like the production defer flow does - not a hand-built
        # projection - so this proves the real decode+project pipeline.
        clock = FakeClock(NOW)
        shared_state = RaisingSharedState(
            protected_rows=[(PACKAGE_A, protected_blob())]
        )
        CrypterCooldownService(shared_state, clock=clock).defer_package(
            PACKAGE_A, "filecrypt", "ip_block_suspected", NOW + 900, 1
        )

        response = self._build_with_clock(
            shared_state, RecordingFakeDevice(), clock, auto_start=False
        )

        assert_valid_package_list_response(self, response)
        self.assertEqual(0, len(response["queue"]))
        self.assertEqual(1, len(response["deferred"]))
        row = response["deferred"][0]
        self.assertEqual(PACKAGE_A, row["package_id"])
        self.assertEqual("observing", row["state"])
        # One spelling across the whole UI: the CAPTCHA page's provider map
        # has always rendered this as "FileCrypt".
        self.assertEqual("filecrypt", row["crypter"])
        self.assertEqual("FileCrypt", row["crypter_label"])
        self.assertEqual("IP access block suspected", row["reason_label"])
        self.assertTrue(row["can_solve_captcha"])

    def test_inactive_defer_metadata_keeps_the_package_in_the_ordinary_queue(self):
        # Behavioral coverage: once the provisional hold's own deadline has
        # passed, the live projection is inactive and must NOT create a
        # DeferredRow - the package renders as a normal protected queue item.
        clock = FakeClock(NOW)
        shared_state = RaisingSharedState(
            protected_rows=[(PACKAGE_A, protected_blob())]
        )
        CrypterCooldownService(shared_state, clock=clock).defer_package(
            PACKAGE_A, "filecrypt", "ip_block_suspected", NOW + 900, 1
        )
        clock.now = NOW + 901  # past retry_after_epoch: hold now inactive

        response = self._build_with_clock(
            shared_state, RecordingFakeDevice(), clock, auto_start=False
        )

        assert_valid_package_list_response(self, response)
        self.assertEqual(0, len(response["deferred"]))
        self.assertEqual(1, len(response["queue"]))
        self.assertEqual("waiting_captcha", response["queue"][0]["status"])

    def test_real_crypter_cooldown_end_to_end_maps_to_cooldown_state(self):
        # A genuine crypter-wide cooldown (three real observations, exactly
        # the production threshold) supersedes the package's own expired
        # provisional hold - proving the full pipeline end to end, not just
        # this module's own mapping function.
        clock = FakeClock(NOW)
        shared_state = RaisingSharedState(
            protected_rows=[(PACKAGE_A, protected_blob())]
        )
        service = CrypterCooldownService(shared_state, clock=clock)
        service.defer_package(
            PACKAGE_A, "filecrypt", "ip_block_suspected", NOW + 900, 1
        )
        clock.now = NOW + 600
        for package_id, fingerprint in (
            (PACKAGE_B, "b"),
            (PACKAGE_C, "c"),
            ("Quasarr_movies_" + "d" * 32, "d"),
        ):
            service.observe(
                "filecrypt", package_id, fingerprint * 64, "ip_block_suspected"
            )
        clock.now = NOW + 901  # past the package's own (now-superseded) hold

        response = self._build_with_clock(
            shared_state, RecordingFakeDevice(), clock, auto_start=False
        )

        assert_valid_package_list_response(self, response)
        self.assertEqual(1, len(response["deferred"]))
        row = response["deferred"][0]
        self.assertEqual("cooldown", row["state"])
        self.assertFalse(row["probe_requested"])
        self.assertEqual(0, row["cohort_retest_depth"])

    def test_failed_package_appears_once_in_history_with_its_reason(self):
        shared_state = RaisingSharedState(
            failed_rows=[(PACKAGE_A, failed_blob(reason="Synthetic download error"))]
        )
        response = self._build(shared_state, RecordingFakeDevice(), auto_start=False)

        assert_valid_package_list_response(self, response)
        self.assertEqual(1, len(response["history"]))
        row = response["history"][0]
        self.assertEqual("failed", row["status"])
        self.assertEqual("Synthetic download error", row["error"])

    def test_malformed_protected_row_is_skipped_without_crashing(self):
        shared_state = RaisingSharedState(
            protected_rows=[(PACKAGE_A, "{not valid json")]
        )
        response = self._build(shared_state, RecordingFakeDevice(), auto_start=False)

        assert_valid_package_list_response(self, response)
        self.assertEqual([], response["queue"])
        self.assertEqual([], response["deferred"])

    def test_malformed_failed_row_is_skipped_without_crashing(self):
        shared_state = RaisingSharedState(failed_rows=[(PACKAGE_A, "{not valid json")])
        response = self._build(shared_state, RecordingFakeDevice(), auto_start=False)

        assert_valid_package_list_response(self, response)
        self.assertEqual([], response["history"])

    def test_fail_block_mode_never_produces_deferred_rows(self):
        # "fail" mode: get_packages_for_device() never builds a
        # CrypterCooldownService and never projects `deferred` at all - even
        # a package carrying stored defer metadata renders as an ordinary
        # protected queue item.
        shared_state = RaisingSharedState(
            protected_rows=[
                (PACKAGE_A, protected_blob(deferred=deferred_block(active=True)))
            ],
            block_mode="fail",
        )
        with mock.patch(
            "quasarr.downloads.packages.crypter_blocks_deferred", return_value=False
        ):
            response = self._build(
                shared_state, RecordingFakeDevice(), auto_start=False
            )

        assert_valid_package_list_response(self, response)
        self.assertEqual(0, len(response["deferred"]))
        self.assertEqual(1, len(response["queue"]))
        self.assertEqual("waiting_captcha", response["queue"][0]["status"])

    def test_unknown_eta_sentinel_is_reported_as_eta_unknown(self):
        lg_package = linkgrabber_package("lg-1", "Synthetic.Queued", PACKAGE_A)
        device = RecordingFakeDevice(
            linkgrabber_packages=[lg_package],
            linkgrabber_links=[
                linkgrabber_link(
                    "lg-link-1", "lg-1", "file.mkv", "https://mirror-a.invalid/1"
                )
            ],
            is_collecting=True,  # keep auto-start from moving it mid-assertion
        )
        shared_state = RaisingSharedState()

        response = self._build(shared_state, device, auto_start=False)

        assert_valid_package_list_response(self, response)
        self.assertEqual(1, len(response["queue"]))
        row = response["queue"][0]
        self.assertTrue(row["eta_unknown"])
        self.assertEqual("", row["eta"])
        self.assertEqual("queued", row["status"])

    def test_known_eta_is_surfaced_as_a_formatted_string(self):
        dl_package = downloader_package(
            "dl-1", "Synthetic.Downloading", PACKAGE_A, eta=3723
        )
        device = RecordingFakeDevice(
            downloader_packages=[dl_package],
            downloader_links=[
                downloader_link(
                    "dl-link-1",
                    "dl-1",
                    "file.mkv",
                    "https://mirror-a.invalid/1",
                    availability="online",
                )
            ],
        )
        shared_state = RaisingSharedState()

        response = self._build(shared_state, device, auto_start=False)

        assert_valid_package_list_response(self, response)
        row = response["queue"][0]
        self.assertFalse(row["eta_unknown"])
        self.assertEqual("01:02:03", row["eta"])
        self.assertEqual("downloading", row["status"])

    def test_hostile_title_passes_through_raw_unescaped_for_the_renderer(self):
        # The projection layer must not HTML-escape (that is the renderer's
        # job); it must also not corrupt or drop special characters.
        hostile_title = '<b>Röck & Röll<\\/script> "quoted"'
        shared_state = RaisingSharedState(
            protected_rows=[(PACKAGE_A, protected_blob(title=hostile_title))]
        )
        response = self._build(shared_state, RecordingFakeDevice(), auto_start=False)

        assert_valid_package_list_response(self, response)
        self.assertEqual(hostile_title, response["queue"][0]["name"])

    def test_history_error_scrubs_an_embedded_protected_url_and_hostname(self):
        # HistoryRow.error is not guaranteed URL-free: downloads/__init__.py
        # persists a generic "Unexpected error: {e}" reason on an
        # unclassified failure, and an exception message can carry a URL.
        hostile_reason = (
            "Unexpected error: connection to "
            "https://mirror-leak.invalid/container/1 failed"
        )
        shared_state = RaisingSharedState(
            failed_rows=[(PACKAGE_A, failed_blob(reason=hostile_reason))]
        )
        response = self._build(shared_state, RecordingFakeDevice(), auto_start=False)

        assert_valid_package_list_response(self, response)
        row = response["history"][0]
        self.assertNotIn("https://", row["error"])
        self.assertNotIn("mirror-leak.invalid", row["error"])
        self.assertIn("[link removed]", row["error"])
        # The non-URL parts of the message are preserved.
        self.assertIn("Unexpected error", row["error"])
        self.assertIn("failed", row["error"])

    def test_history_error_scrubs_a_bare_hostname_without_a_scheme(self):
        shared_state = RaisingSharedState(
            failed_rows=[
                (PACKAGE_A, failed_blob(reason="Timed out reaching bare-host.invalid"))
            ]
        )
        response = self._build(shared_state, RecordingFakeDevice(), auto_start=False)

        assert_valid_package_list_response(self, response)
        row = response["history"][0]
        self.assertNotIn("bare-host.invalid", row["error"])
        self.assertIn("[link removed]", row["error"])

    def test_history_error_scrubbing_does_not_corrupt_ordinary_release_text(self):
        shared_state = RaisingSharedState(
            failed_rows=[
                (
                    PACKAGE_A,
                    failed_blob(reason="Synthetic.Release.2024.1080p.WEB-DL failed"),
                )
            ]
        )
        response = self._build(shared_state, RecordingFakeDevice(), auto_start=False)

        assert_valid_package_list_response(self, response)
        self.assertEqual(
            "Synthetic.Release.2024.1080p.WEB-DL failed",
            response["history"][0]["error"],
        )

    def test_password_and_original_url_never_leak_into_the_response(self):
        shared_state = RaisingSharedState(
            protected_rows=[
                (
                    PACKAGE_A,
                    protected_blob(
                        password="super-secret-credential",
                        links=[["https://filecrypt.invalid/c/leak", "filecrypt"]],
                    ),
                )
            ]
        )
        response = self._build(shared_state, RecordingFakeDevice(), auto_start=False)

        assert_valid_package_list_response(self, response)
        serialized = json.dumps(response)
        self.assertNotIn("super-secret-credential", serialized)
        # The crypter's bare host is the one allowed exception; the link
        # itself - scheme, path, container token - is not.
        self.assertEqual("filecrypt.invalid", response["queue"][0]["mirror"])
        self.assertNotIn("://", serialized)
        self.assertNotIn("/c/leak", serialized)

    # -- Latest-commit Filecrypt terminal-blacklist outcomes (c294f65) -----

    def test_blacklist_scrub_continuing_with_alternatives_stays_a_queue_row(self):
        # A package whose blacklisted link was scrubbed but that still has an
        # alternative link stays an ordinary protected queue row - the
        # fingerprint that was removed is never visible.
        shared_state = RaisingSharedState(
            protected_rows=[
                (
                    PACKAGE_A,
                    protected_blob(
                        links=[["https://mirror-alt.invalid/c/2", "filecrypt"]],
                    ),
                )
            ]
        )
        response = self._build(shared_state, RecordingFakeDevice(), auto_start=False)

        assert_valid_package_list_response(self, response)
        self.assertEqual(1, len(response["queue"]))
        self.assertEqual(0, len(response["history"]))
        self.assertEqual("waiting_captcha", response["queue"][0]["status"])

    def test_blacklist_scrub_terminal_failure_appears_once_in_history(self):
        # A package whose only link was blacklisted is terminally failed with
        # the exact fixed, fingerprint-free reason text.
        reason = "Filecrypt URL permanently blacklisted; no remaining links available."
        shared_state = RaisingSharedState(
            failed_rows=[(PACKAGE_A, failed_blob(reason=reason))]
        )
        response = self._build(shared_state, RecordingFakeDevice(), auto_start=False)

        assert_valid_package_list_response(self, response)
        self.assertEqual(1, len(response["history"]))
        row = response["history"][0]
        self.assertEqual("failed", row["status"])
        self.assertEqual(reason, row["error"])
        self.assertEqual(0, len(response["queue"]))
        self.assertEqual(0, len(response["deferred"]))

    def test_blacklist_scrub_package_absence_produces_no_row_at_all(self):
        # A package removed entirely by a concurrent scrub (package_absent)
        # before this read must not be fabricated into any row.
        shared_state = RaisingSharedState()
        response = self._build(shared_state, RecordingFakeDevice(), auto_start=False)

        assert_valid_package_list_response(self, response)
        self.assertEqual([], response["queue"])
        self.assertEqual([], response["history"])
        self.assertEqual([], response["deferred"])


# ---------------------------------------------------------------------------
# 3b. DeferredRow.state unit coverage (probe precedence; "retest" reserved)
# ---------------------------------------------------------------------------


class DeferredRowStateMappingTests(unittest.TestCase):
    """Exercises _deferred_row() directly against the exact
    post-projection shape get_packages_for_device() attaches to a queue item
    (project_package_defer()'s output - see
    test_deferred_protected_packages.py for that shape pinned end to end).

    "retest" is a reserved schema value (see SchemaValidatorSelfTests for the
    validator still accepting it) that the current backend never emits: an
    earlier attempt derived it from a *second*, independently-timed
    crypter_projection() read against the crypter's live retest_members, but
    that queue is only ever non-empty for a healthy/individual decision,
    which _legacy_shaped_snapshot() maps to legacy state "available" - never
    "cooldown". Since hold_type == "crypter_cooldown" requires legacy state
    "cooldown", a package can never simultaneously be cooling (by this read)
    and a real retest-queue member (by that other read) within one
    consistent row - the two conditions are mutually exclusive on a single
    read, and only ever appeared to coexist because the two reads could
    observe different moments of live state (a cooldown -> healthy
    transition landing between them), which produced an internally
    contradictory, flickering row. See carbon._deferred_state()'s docstring
    for the full trace. cohort_retest_depth (a crypter-wide count, never a
    per-package fact) remains on the row regardless of state - the two
    "non-empty retest queue but state stays cooldown" cases below pin that a
    non-zero count alone can never produce "retest".
    """

    def _item(self, **deferred_overrides):
        deferred = {
            "crypter": "filecrypt",
            "reason_code": "ip_block_suspected",
            "since_epoch": NOW,
            "retry_after_epoch": NOW + 900,
            "probe_requested": False,
            "observation_holds": 1,
            "state": "available",
            "evidence_count": 1,
            "hold_type": "provisional",
            "active": True,
            "cohort_tested": 0,
            "cohort_total": 0,
            "cohort_deadline_epoch": 0,
            "cohort_retest_depth": 0,
        }
        deferred.update(deferred_overrides)
        return {
            "nzo_id": PACKAGE_A,
            "filename": "[Waiting for linkcrypter retry] Synthetic Release",
            "deferred": deferred,
        }

    def test_provisional_hold_maps_to_observing(self):
        row = _deferred_row(self._item(hold_type="provisional"))
        _assert_deferred_row_valid(self, row)
        self.assertEqual("observing", row["state"])

    def test_plain_cooldown_maps_to_cooldown(self):
        row = _deferred_row(
            self._item(
                hold_type="crypter_cooldown",
                state="cooldown",
                probe_requested=False,
            )
        )
        _assert_deferred_row_valid(self, row)
        self.assertEqual("cooldown", row["state"])

    def test_queued_probe_takes_precedence_and_maps_to_probe_queued(self):
        row = _deferred_row(
            self._item(
                hold_type="crypter_cooldown",
                state="cooldown",
                probe_requested=True,
                cohort_retest_depth=3,
            )
        )
        _assert_deferred_row_valid(self, row)
        self.assertEqual("probe_queued", row["state"])
        self.assertTrue(row["probe_requested"])

    def test_a_non_zero_cohort_retest_depth_alone_never_produces_retest_state(self):
        # Pins the review finding: cohort_retest_depth is a crypter-wide
        # count copied identically onto every held package of that crypter,
        # never a per-package fact, so it must never drive DeferredRow.state
        # by itself. The count is still surfaced on the row unchanged.
        row = _deferred_row(
            self._item(
                hold_type="crypter_cooldown",
                state="cooldown",
                probe_requested=False,
                cohort_tested=3,
                cohort_total=5,
                cohort_deadline_epoch=4_100_000_000,
                cohort_retest_depth=2,
            )
        )
        _assert_deferred_row_valid(self, row)
        self.assertEqual("cooldown", row["state"])
        self.assertEqual(2, row["cohort_retest_depth"])
        self.assertEqual(3, row["cohort_tested"])
        self.assertEqual(5, row["cohort_total"])

    def test_generation_bound_hold_with_retest_depth_still_reports_cooldown(self):
        # Same pin as above, but on a v2/generation-bound hold (the shape a
        # real cohort transition would leave) rather than a bare count -
        # link_fingerprints alone must not resurrect the removed retest path.
        row = _deferred_row(
            self._item(
                hold_type="crypter_cooldown",
                state="cooldown",
                probe_requested=False,
                cohort_retest_depth=1,
                schema_version=2,
                sweep_id="5e" * 16,
                link_fingerprints=["a1" * 32],
            )
        )
        _assert_deferred_row_valid(self, row)
        self.assertEqual("cooldown", row["state"])
        self.assertEqual(1, row["cohort_retest_depth"])

    def test_legacy_hold_under_cooldown_reports_cooldown(self):
        # A legacy (non-generation) cooldown hold carries no link_fingerprints
        # at all and must report the plain cooldown state.
        row = _deferred_row(
            self._item(
                hold_type="crypter_cooldown",
                state="cooldown",
                probe_requested=False,
            )
        )
        _assert_deferred_row_valid(self, row)
        self.assertEqual("cooldown", row["state"])

    def test_malformed_deferred_fields_default_safely(self):
        row = _deferred_row(
            self._item(
                evidence_count="not-a-number",
                retry_after_epoch=None,
                cohort_tested=-5,
                cohort_retest_depth=True,  # bool must not pass through as int
            )
        )
        _assert_deferred_row_valid(self, row)
        self.assertEqual(0, row["evidence_count"])
        self.assertEqual(0, row["retry_after_epoch"])
        self.assertEqual(0, row["cohort_tested"])
        self.assertEqual(1, row["cohort_retest_depth"])  # bool True -> int 1, still >=0

    def test_missing_deferred_block_still_builds_a_row_defensively(self):
        row = _deferred_row({"nzo_id": PACKAGE_A, "filename": "x"})
        _assert_deferred_row_valid(self, row)
        self.assertEqual("observing", row["state"])
        # A package with neither a live hold nor a stored origin reports no
        # crypter at all. It used to read "Unknown", an invented word that
        # looked like a real answer; an empty value lets the row render the
        # same "not known" dash every other origin-less row shows.
        self.assertEqual("", row["crypter"])
        self.assertEqual("", row["crypter_label"])


class ScrubProtectedLinksTests(unittest.TestCase):
    """Unit coverage for carbon._scrub_protected_links(), the HistoryRow.error
    sanitizer added for the review finding that a raw exception message
    (downloads/__init__.py's generic "Unexpected error: {e}" reason) is not
    guaranteed URL-free.
    """

    def test_empty_and_none_pass_through_unchanged(self):
        self.assertEqual("", carbon._scrub_protected_links(""))
        self.assertIsNone(carbon._scrub_protected_links(None))

    def test_a_full_url_is_replaced_with_the_fixed_marker(self):
        text = "Failed: https://mirror-x.invalid/container/1 unreachable"
        scrubbed = carbon._scrub_protected_links(text)
        self.assertNotIn("https://", scrubbed)
        self.assertNotIn("mirror-x.invalid", scrubbed)
        self.assertIn("[link removed]", scrubbed)
        self.assertIn("Failed:", scrubbed)
        self.assertIn("unreachable", scrubbed)

    def test_a_bare_hostname_with_a_known_tld_is_redacted(self):
        scrubbed = carbon._scrub_protected_links("host bare-host.invalid down")
        self.assertNotIn("bare-host.invalid", scrubbed)
        self.assertIn("[link removed]", scrubbed)

    def test_ordinary_release_title_text_is_never_redacted(self):
        text = "Synthetic.Release.2024.1080p.WEB-DL failed"
        self.assertEqual(text, carbon._scrub_protected_links(text))

    def test_a_media_file_extension_is_never_mistaken_for_a_hostname(self):
        text = "corrupt file movie.mkv in package"
        self.assertEqual(text, carbon._scrub_protected_links(text))

    def test_a_plain_reason_with_no_link_is_unchanged(self):
        text = "Too many failed attempts by SponsorsHelper"
        self.assertEqual(text, carbon._scrub_protected_links(text))


# ---------------------------------------------------------------------------
# 4. Route behavior
# ---------------------------------------------------------------------------


class PackagesListRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Bottle()
        packages_api.setup_packages_routes(self.app)

    def _route(self):
        return next(
            route.callback
            for route in self.app.routes
            if route.method == "GET" and route.rule == "/api/packages/list"
        )

    def test_route_is_registered_and_api_key_guarded(self):
        methods = {
            route.rule: route.method
            for route in self.app.routes
            if route.rule == "/api/packages/list"
        }
        self.assertEqual({"/api/packages/list": "GET"}, methods)

        audit_route_auth_modes(
            self.app,
            api_key_prefixes=("/api",),
            public_whitelist=(),
        )

    def test_returns_empty_shape_when_device_is_absent_without_building_projection(
        self,
    ):
        # The route imports carbon.py lazily inside its own callback (like
        # every other Carbon import in this module), so the patch target is
        # the source function, not a module-scope attribute on packages_api.
        with (
            mock.patch.dict(packages_api.shared_state.values, {}, clear=True),
            mock.patch.object(carbon, "build_package_list_response") as build_response,
        ):
            result = self._route()()

        assert_valid_package_list_response(self, result)
        self.assertFalse(result["connected"])
        build_response.assert_not_called()

    def test_delegates_only_to_build_package_list_response_when_device_present(self):
        sentinel_device = object()
        sentinel_response = empty_package_list_response()
        sentinel_response["connected"] = True

        with (
            mock.patch.dict(
                packages_api.shared_state.values, {"device": sentinel_device}
            ),
            mock.patch.object(
                carbon,
                "build_package_list_response",
                return_value=sentinel_response,
            ) as build_response,
        ):
            result = self._route()()

        self.assertIs(sentinel_response, result)
        build_response.assert_called_once_with(
            packages_api.shared_state, sentinel_device
        )


class OriginProjectionTests(unittest.TestCase):
    """The persisted package origin joined onto every row type.

    `mirror` is the single field that could carry more than a bare host, so
    the sanitizing is pinned on the projection itself and not only on the
    writer - a row written by an older build must not be able to widen the
    contract.
    """

    def test_origin_fields_reach_a_queue_row(self):
        origins = {
            PACKAGE_A: {
                "crypter": "filecrypt",
                "mirror": "filecrypt.invalid",
                "added_epoch": NOW,
            }
        }
        row = _queue_row(
            {"nzo_id": PACKAGE_A, "filename": "Synthetic.Release", "bytes": 2048},
            origins,
        )

        self.assertEqual("filecrypt", row["crypter"])
        self.assertEqual("FileCrypt", row["crypter_label"])
        self.assertEqual("filecrypt.invalid", row["mirror"])
        self.assertEqual(NOW, row["added_epoch"])
        self.assertEqual(2048, row["size_bytes"])

    def test_a_protected_row_reports_its_size_in_bytes_from_megabytes(self):
        # Protected packages carry no reliable byte count, only size_mb - the
        # sortable column still needs a number, not a formatted label.
        row = _queue_row(
            {"nzo_id": PACKAGE_A, "filename": "Synthetic.Release", "mb": 100}, {}
        )

        self.assertEqual(100 * 1024 * 1024, row["size_bytes"])

    def test_rows_without_an_origin_stay_empty_instead_of_guessing(self):
        row = _queue_row({"nzo_id": PACKAGE_B, "filename": "Synthetic.Release"}, {})

        self.assertEqual("", row["crypter"])
        self.assertEqual("", row["crypter_label"])
        self.assertEqual("", row["mirror"])
        self.assertEqual(0, row["added_epoch"])

    def test_a_stored_url_can_never_reach_the_mirror_field(self):
        origins = {
            PACKAGE_A: {
                "crypter": "filecrypt",
                "mirror": "https://filecrypt.invalid/Container/ABC123",
                "added_epoch": NOW,
            }
        }
        row = _history_row({"nzo_id": PACKAGE_A, "name": "Synthetic.Release"}, origins)

        self.assertEqual("", row["mirror"])

    def test_an_unrecognized_link_publishes_no_host_at_all(self):
        # The contract's exception is the host of a LINKCRYPTER. A link that
        # resolves to no known crypter could be a hoster or a source, so its
        # host must not leave the projection - publishing it would widen the
        # exception into exactly what the contract forbids. Caught by the
        # blacklist-scrub simulation before this rule existed.
        from quasarr.downloads.packages import _protected_origin

        for links in (
            [["https://unknown-host.invalid/c/2", "filecrypt"]],
            [["https://hoster.invalid/file/1", "rapidgator"]],
            [["not-a-url", "filecrypt"]],
            [],
            None,
        ):
            with self.subTest(links=links):
                self.assertEqual(("", ""), _protected_origin(links))

    def test_a_recognized_crypter_link_yields_its_key_and_bare_host(self):
        from quasarr.downloads.packages import _protected_origin

        self.assertEqual(
            ("filecrypt", "filecrypt.invalid"),
            _protected_origin([["https://filecrypt.invalid/Container/ABC123", "x"]]),
        )

    def test_an_auto_decrypt_crypter_is_named_too(self):
        # A hide link that failed auto-decryption falls back to the protected
        # queue and waits for a manual CAPTCHA, where /captcha names it
        # "Crypter: Hide". The table has to agree: resolve_protected_crypter_key()
        # is the COOLDOWN-eligibility allowlist (filecrypt/tolink/keeplinks/
        # junkies) and deliberately rejects hide, which is not the same
        # question as "can we name this crypter".
        from quasarr.downloads.packages import _protected_origin

        self.assertEqual(
            ("hide", "hide.invalid"),
            _protected_origin([["https://hide.invalid/state/abc", "hide"]]),
        )

    def test_the_junkies_mirror_tag_still_resolves(self):
        from quasarr.downloads.packages import _protected_origin

        self.assertEqual(
            ("junkies", "container.invalid"),
            _protected_origin([["https://container.invalid/item", "junkies"]]),
        )

    def test_a_still_protected_row_names_its_crypter_without_a_stored_origin(self):
        # The links of a package waiting for a CAPTCHA are right there, which
        # is how /captcha names the crypter and mirror. Packages older than
        # the package_origin table have no stored row at all, so this live
        # value is the only thing that can fill the column for them.
        row = carbon._build_queue_row(
            {
                "nzo_id": PACKAGE_A,
                "filename": "[CAPTCHA not solved!] Synthetic.Release",
                "crypter": "filecrypt",
                "mirror": "filecrypt.invalid",
            },
            {},
        )

        self.assertEqual("filecrypt", row["crypter"])
        self.assertEqual("FileCrypt", row["crypter_label"])
        self.assertEqual("filecrypt.invalid", row["mirror"])
        # Nothing ever recorded when these were accepted, so this one stays 0.
        self.assertEqual(0, row["added_epoch"])

    def test_the_live_link_wins_over_a_stale_stored_origin(self):
        origins = {
            PACKAGE_A: {
                "crypter": "junkies",
                "mirror": "old.invalid",
                "added_epoch": NOW,
            }
        }
        row = carbon._build_queue_row(
            {
                "nzo_id": PACKAGE_A,
                "filename": "x",
                "crypter": "filecrypt",
                "mirror": "filecrypt.invalid",
            },
            origins,
        )

        self.assertEqual("filecrypt", row["crypter"])
        self.assertEqual("filecrypt.invalid", row["mirror"])
        self.assertEqual(NOW, row["added_epoch"])

    def test_a_live_mirror_carrying_a_url_is_still_rejected(self):
        row = carbon._build_queue_row(
            {
                "nzo_id": PACKAGE_A,
                "filename": "x",
                "crypter": "filecrypt",
                "mirror": "https://filecrypt.invalid/Container/ABC123",
            },
            {},
        )

        self.assertEqual("", row["mirror"])

    def test_deferred_prefers_its_live_crypter_over_the_stored_one(self):
        # The live hold is authoritative; the stored origin only fills the
        # host, which the hold does not carry.
        origins = {
            PACKAGE_A: {
                "crypter": "junkies",
                "mirror": "filecrypt.invalid",
                "added_epoch": NOW,
            }
        }
        row = _deferred_row(
            {
                "nzo_id": PACKAGE_A,
                "filename": "Synthetic.Release",
                "deferred": {
                    "active": True,
                    "crypter": "filecrypt",
                    "reason_code": "ip_block_suspected",
                    "hold_type": "crypter_cooldown",
                },
            },
            origins,
        )

        self.assertEqual("filecrypt", row["crypter"])
        self.assertEqual("FileCrypt", row["crypter_label"])
        self.assertEqual("filecrypt.invalid", row["mirror"])
        self.assertEqual(NOW, row["added_epoch"])

    def test_deferred_falls_back_to_the_stored_crypter_when_the_hold_has_none(self):
        origins = {
            PACKAGE_A: {
                "crypter": "junkies",
                "mirror": "container.invalid",
                "added_epoch": NOW,
            }
        }
        row = _deferred_row(
            {
                "nzo_id": PACKAGE_A,
                "filename": "Synthetic.Release",
                "deferred": {"active": True, "reason_code": "ip_block_suspected"},
            },
            origins,
        )

        self.assertEqual("junkies", row["crypter"])
        self.assertEqual("Junkies", row["crypter_label"])


if __name__ == "__main__":
    unittest.main()
