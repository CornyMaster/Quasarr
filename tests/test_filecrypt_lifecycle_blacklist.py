# -*- coding: utf-8 -*-
"""Filecrypt owner scrub and terminal orchestration."""

import json
import time
import unittest
from unittest import mock

from bottle import Bottle

import quasarr.api.sponsors_helper as sponsors_helper_api
from quasarr.api.sponsors_helper import setup_sponsors_helper_routes
from quasarr.api.sponsors_helper.cohort_protocol import (
    CRYPTER_DEFER_CAPABILITY,
    FILECRYPT_COHORT_CAPABILITY,
)
from quasarr.providers.filecrypt_lifecycle import (
    FILECRYPT_LINK_STATES_TABLE,
    encode_link_state,
)
from quasarr.providers.filecrypt_lifecycle_service import (
    FILECRYPT_CRYPTER,
    FILECRYPT_LINK_LIFECYCLE_CAPABILITY,
    FilecryptLifecycleService,
)
from quasarr.providers.terminal_operations import (
    CAPACITY,
    CONFLICT,
    EFFECT_NOT_STARTED,
    NOTIFICATION_NOT_STARTED,
    OPENED,
    RESUMED,
    TERMINAL_OPERATION_TABLE,
    UNREADABLE,
    terminal_operation_id,
)
from tests.test_filecrypt_lifecycle_service import (
    AtomicSharedState,
    FakeClock,
    SequentialIds,
    fp,
    pkg,
    url,
)

NOW = 1_700_000_000
COOLDOWN_SECS = 86400
DECRYPT_RULE = "/sponsors_helper/api/to_decrypt/"
_LIFECYCLE_CAPS = [
    CRYPTER_DEFER_CAPABILITY,
    FILECRYPT_COHORT_CAPABILITY,
    FILECRYPT_LINK_LIFECYCLE_CAPABILITY,
]

# ── fixtures ──────────────────────────────────────────────────────────────────


def _blacklisted_ls(first_blocked=NOW - COOLDOWN_SECS * 2):
    return encode_link_state(
        {
            "schema_version": 1,
            "state": "blacklisted",
            "first_blocked_epoch": first_blocked,
            "blacklisted_epoch": first_blocked + COOLDOWN_SECS,
        }
    )


def _pkg_with_links(urls_and_crypters):
    """Protected package JSON with one or more [url, crypter] links."""
    links = [[u, c] for u, c in urls_and_crypters]
    return json.dumps(
        {"title": "T", "password": "", "links": links},
        separators=(",", ":"),
        sort_keys=True,
    )


def _filecrypt_pkg(n):
    """Single-Filecrypt-link protected package."""
    return _pkg_with_links([[url(n), FILECRYPT_CRYPTER]])


def _multi_link_pkg(n, extra_url="https://rapidgator.invalid/f/abc"):
    """Filecrypt link plus one alternative (non-Filecrypt)."""
    return _pkg_with_links([[url(n), FILECRYPT_CRYPTER], [extra_url, "rapidgator"]])


# ── base ──────────────────────────────────────────────────────────────────────


class BlacklistTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(NOW)
        self.ids = SequentialIds()
        self.state = AtomicSharedState()

    def svc(self):
        return FilecryptLifecycleService(
            self.state, clock=self.clock, identifier_factory=self.ids
        )

    def seed_blacklisted(self, fingerprint):
        self.state.get_db(FILECRYPT_LINK_STATES_TABLE).store(
            fingerprint, _blacklisted_ls()
        )

    def seed_protected(self, package_id, raw):
        self.state.get_db("protected").store(package_id, raw)

    def prot_links(self, package_id):
        raw = self.state.get_db("protected").retrieve(package_id)
        if raw is None:
            return None
        return json.loads(raw)["links"]


# ── method 1: blacklisted_owners and remove_blacklisted_link ─────────────────


class TestOwnersAndRemoveLink(BlacklistTestCase):
    """blacklisted_owners and remove_blacklisted_link service methods."""

    def test_owners_and_remove_link_table(self):
        # fmt: off
        CASES = [
            # (label, protected_rows, fingerprint, expect_owners, expect_removal)
            # expect_removal: (link_removed, usable_links_remaining, package_absent)
            ("no_owners",
             [],
             fp(1),
             (),
             None),
            ("single_owner",
             [[pkg(1), _filecrypt_pkg(1)]],
             fp(1),
             (pkg(1),),
             None),
            ("two_owners_same_fp",
             [[pkg(1), _filecrypt_pkg(1)], [pkg(2), _pkg_with_links([[url(1), FILECRYPT_CRYPTER]])]],
             fp(1),
             (pkg(1), pkg(2)),
             None),
            ("unrelated_fp_not_an_owner",
             [[pkg(3), _filecrypt_pkg(3)]],
             fp(99),
             (),
             None),
            ("non_filecrypt_link_ignored",
             [[pkg(4), _pkg_with_links([["https://rapidgator.invalid/f/x", "rapidgator"]])]],
             fp(4),
             (),
             None),
            ("remove_with_alternative",
             [[pkg(5), _multi_link_pkg(5)]],
             fp(5),
             (pkg(5),),
             (True, 1, False)),
            ("remove_only_link_not_removed",
             [[pkg(6), _filecrypt_pkg(6)]],
             fp(6),
             (pkg(6),),
             (False, 0, False)),
            ("fp_not_in_package_no_change",
             [[pkg(7), _filecrypt_pkg(7)]],
             fp(99),
             (),
             (False, 1, False)),
            ("package_absent",
             [],
             fp(8),
             (),
             (False, 0, True)),
        ]
        # fmt: on
        svc = self.svc()
        for label, prot_rows, fingerprint, exp_owners, exp_removal in CASES:
            with self.subTest(label):
                self.setUp()
                svc = self.svc()
                for pid, raw in prot_rows:
                    self.seed_protected(pid, raw)

                owners = svc.blacklisted_owners(prot_rows, fingerprint)
                self.assertEqual(owners, exp_owners, f"{label}: owners")

                if exp_removal is not None:
                    # Pick the first owner if there is one, else use pkg(8) for absent
                    target_pkg = prot_rows[0][0] if prot_rows else pkg(8)
                    result = svc.remove_blacklisted_link(target_pkg, fingerprint)
                    exp_removed, exp_remaining, exp_absent = exp_removal
                    self.assertEqual(
                        result["link_removed"], exp_removed, f"{label}: link_removed"
                    )
                    self.assertEqual(
                        result["usable_links_remaining"],
                        exp_remaining,
                        f"{label}: remaining",
                    )
                    self.assertEqual(
                        result["package_absent"], exp_absent, f"{label}: absent"
                    )


# ── method 2: scrub link with alternatives vs. terminal failure ───────────────


class TestScrubWithAndWithoutAlternatives(BlacklistTestCase):
    """Packages with alternatives keep their row; without alternatives are terminally failed."""

    def test_scrub_with_and_without_alternatives(self):
        # pkg(1): single Filecrypt link -> must be terminally failed (exactly once)
        # pkg(2): Filecrypt link + rapidgator -> Filecrypt link scrubbed, package kept
        self.seed_blacklisted(fp(1))
        self.seed_protected(pkg(1), _filecrypt_pkg(1))
        self.seed_protected(
            pkg(2), _multi_link_pkg(2, "https://rapidgator.invalid/f/y")
        )
        self.seed_blacklisted(fp(2))

        app = Bottle()
        setup_sponsors_helper_routes(app)
        route = next(r for r in app.routes if r.rule == DECRYPT_RULE)
        state = self.state

        # Non-Filecrypt package so the route has a handable package after scrub.
        state.get_db("protected").store(
            pkg(99),
            json.dumps(
                {
                    "title": "Other",
                    "password": "",
                    "links": [["https://other.invalid/x", "other"]],
                },
                separators=(",", ":"),
            ),
        )
        from quasarr.providers.filecrypt_lifecycle import (
            FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE,
            FILECRYPT_MIGRATION_KEY,
            encode_migration_marker,
        )

        state.get_db(FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE).store(
            FILECRYPT_MIGRATION_KEY,
            encode_migration_marker(
                {"schema_version": 1, "completed_epoch": NOW - 100}
            ),
        )

        payload = {
            "supported_urls": ["other.invalid"],
            "capabilities": _LIFECYCLE_CAPS,
        }

        # Capture every protected_packages argument reaching select_helper_package.
        select_received: list = []
        real_select = sponsors_helper_api.select_helper_package

        def spy_select(protected_packages, *args, **kwargs):
            select_received.append(list(protected_packages or []))
            return real_select(protected_packages, *args, **kwargs)

        with (
            mock.patch("quasarr.api.sponsors_helper.shared_state", state),
            mock.patch("quasarr.api.sponsors_helper.request", mock.Mock(json=payload)),
            mock.patch(
                "quasarr.api.sponsors_helper.CrypterCooldownService",
                lambda s: mock.Mock(),
            ),
            mock.patch(
                "quasarr.api.sponsors_helper.crypter_blocks_deferred", return_value=True
            ),
            mock.patch("quasarr.api.sponsors_helper.update_release_notification"),
            mock.patch("quasarr.api.sponsors_helper.select_helper_package", spy_select),
        ):
            route.callback()

        # ── requirement 1: no Mock TypeError – real service drives terminal failure ──
        # pkg(1): protected row must be gone, failed row and counter must exist
        self.assertIsNone(
            state.get_db("protected").retrieve(pkg(1)),
            "pkg(1) must be removed from protected after terminal failure",
        )
        self.assertIsNotNone(
            state.get_db("failed").retrieve(pkg(1)),
            "pkg(1) must have a failed history row",
        )
        self.assertEqual(
            "1",
            state.get_db("statistics").retrieve("failed_downloads"),
            "failed_downloads counter must be exactly 1",
        )

        # ── requirement 3: no stale pre-scrub row reaches select_helper_package ──
        pkg1_in_any_call = any(
            any(r[0] == pkg(1) for r in call_rows) for call_rows in select_received
        )
        self.assertFalse(
            pkg1_in_any_call,
            "scrubbed pkg(1) must not appear in protected_packages arg of "
            "select_helper_package (re-read required after scrub)",
        )

        # ── pkg(2): Filecrypt link removed, rapidgator link preserved ──
        links_2 = self.prot_links(pkg(2))
        self.assertIsNotNone(links_2, "pkg(2) should still exist")
        fp2_urls = [lnk[0] for lnk in links_2 if isinstance(lnk, list) and lnk]
        self.assertNotIn(
            url(2), fp2_urls, "blacklisted Filecrypt URL removed from pkg(2)"
        )
        rapidgator_kept = any(
            "rapidgator" in lnk[0] for lnk in links_2 if isinstance(lnk, list)
        )
        self.assertTrue(rapidgator_kept, "rapidgator link preserved in pkg(2)")


# ── method 3: terminal outcome table preserves or removes ────────────────────


class TestScrubTerminalOutcomeTable(BlacklistTestCase):
    """Outcomes OPENED/RESUMED complete; CAPACITY/CONFLICT/UNREADABLE preserve the link; HTTP 409 is a per-owner skip; rerun causes no duplicate."""

    def test_scrub_terminal_outcome_table(self):
        from contextlib import ExitStack

        from quasarr.providers.filecrypt_lifecycle import (
            FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE,
            FILECRYPT_MIGRATION_KEY,
            encode_migration_marker,
        )

        def _seed(pre_seed_op=False):
            """Fresh shared state seeded for one scrub pass."""
            s = AtomicSharedState()
            s.get_db(FILECRYPT_LINK_STATES_TABLE).store(fp(1), _blacklisted_ls())
            s.get_db("protected").store(pkg(1), _filecrypt_pkg(1))
            s.get_db("protected").store(
                pkg(99),
                json.dumps(
                    {
                        "title": "O",
                        "password": "",
                        "links": [["https://other.invalid/z", "other"]],
                    },
                    separators=(",", ":"),
                ),
            )
            s.get_db(FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE).store(
                FILECRYPT_MIGRATION_KEY,
                encode_migration_marker(
                    {"schema_version": 1, "completed_epoch": NOW - 1}
                ),
            )
            if pre_seed_op:
                # Pre-existing operation record so exclusive() returns RESUMED
                s.get_db(TERMINAL_OPERATION_TABLE).store(
                    terminal_operation_id(pkg(1)),
                    json.dumps(
                        {
                            "state": "prepared",
                            "terminal_state": "failed",
                            "package_id": pkg(1),
                            "created_epoch": int(time.time()),
                            "updated_epoch": int(time.time()),
                            "package_removed": False,
                            "package_terminal": False,
                            "effect_state": EFFECT_NOT_STARTED,
                            "failure_persisted": False,
                            "notification_state": NOTIFICATION_NOT_STARTED,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            return s

        def _route_patches(state, terminal_outcome):
            """Base patches plus outcome-specific terminal-service patch."""
            payload = {
                "supported_urls": ["other.invalid"],
                "capabilities": _LIFECYCLE_CAPS,
            }
            base = [
                mock.patch("quasarr.api.sponsors_helper.shared_state", state),
                mock.patch(
                    "quasarr.api.sponsors_helper.request", mock.Mock(json=payload)
                ),
                mock.patch(
                    "quasarr.api.sponsors_helper.CrypterCooldownService",
                    lambda s: mock.Mock(),
                ),
                mock.patch(
                    "quasarr.api.sponsors_helper.crypter_blocks_deferred",
                    return_value=True,
                ),
                mock.patch("quasarr.api.sponsors_helper.update_release_notification"),
            ]
            if terminal_outcome in (CAPACITY, CONFLICT, UNREADABLE):
                calls = [0]
                ctx = mock.MagicMock(
                    __enter__=mock.Mock(
                        return_value={"outcome": terminal_outcome, "record": None}
                    ),
                    __exit__=mock.Mock(return_value=False),
                )

                def fake_exc(op_id, pid, s_str):
                    calls[0] += 1
                    return ctx

                base.append(
                    mock.patch(
                        "quasarr.api.sponsors_helper.TerminalOperationService",
                        lambda s: mock.Mock(exclusive=fake_exc),
                    )
                )
                return base, calls
            if terminal_outcome == "http_409":
                calls = [0]
                record = {
                    "state": "prepared",
                    "terminal_state": "failed",
                    "package_id": pkg(1),
                    "created_epoch": int(time.time()),
                    "updated_epoch": int(time.time()),
                    "package_removed": False,
                    "package_terminal": False,
                    "effect_state": EFFECT_NOT_STARTED,
                    "failure_persisted": False,
                    "notification_state": NOTIFICATION_NOT_STARTED,
                    "legacy_unproven": False,  # always present on decoded records
                }

                def fake_409_exc(op_id, pid, s_str):
                    calls[0] += 1
                    return mock.MagicMock(
                        __enter__=mock.Mock(
                            return_value={"outcome": OPENED, "record": record}
                        ),
                        __exit__=mock.Mock(return_value=False),
                    )

                # mark_effect_attempting CONFLICT causes abort(409) inside begin_terminal_effect
                mock_svc = mock.Mock()
                mock_svc.exclusive = fake_409_exc
                mock_svc.mark_effect_attempting.return_value = {
                    "outcome": CONFLICT,
                    "record": None,
                }
                base.append(
                    mock.patch(
                        "quasarr.api.sponsors_helper.TerminalOperationService",
                        lambda s: mock_svc,
                    )
                )
                return base, calls
            # OPENED / RESUMED: use real TerminalOperationService (no mock)
            return base, None

        def _call(state, patches):
            app = Bottle()
            setup_sponsors_helper_routes(app)
            route = next(r for r in app.routes if r.rule == DECRYPT_RULE)
            with ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                route.callback()

        # (label, outcome, pre_seed_op, expect_link_preserved)
        # expect_link_preserved: True = row must remain; None = don't check (OPENED/RESUMED remove it)
        CASES = [
            ("opened", OPENED, False, None),
            ("resumed", RESUMED, True, None),
            ("capacity_preserves_link", CAPACITY, False, True),
            ("conflict_preserves_link", CONFLICT, False, True),
            ("unreadable_preserves_link", UNREADABLE, False, True),
            ("http_409_preserves_link", "http_409", False, True),
        ]
        for label, outcome, pre_seed_op, expect_link_preserved in CASES:
            with self.subTest(label):
                state = _seed(pre_seed_op=pre_seed_op)
                patches, calls = _route_patches(state, outcome)
                _call(state, patches)

                pkg1_row = state.get_db("protected").retrieve(pkg(1))
                if expect_link_preserved is True:
                    self.assertIsNotNone(pkg1_row, f"{label}: link must be preserved")
                    if calls is not None:
                        self.assertGreater(
                            calls[0], 0, f"{label}: exclusive() must be called"
                        )
                elif outcome in (OPENED, RESUMED):
                    # Terminal failure must complete: package removed, history/counter written once
                    self.assertIsNone(pkg1_row, f"{label}: package must be removed")
                    self.assertIsNotNone(
                        state.get_db("failed").retrieve(pkg(1)),
                        f"{label}: failed row must exist",
                    )
                    self.assertEqual(
                        "1",
                        state.get_db("statistics").retrieve("failed_downloads"),
                        f"{label}: counter must be exactly 1",
                    )
                    # ── requirement 2: duplicate/rerun must not add a second effect ──
                    patches2, _ = _route_patches(state, outcome)
                    _call(state, patches2)
                    self.assertEqual(
                        "1",
                        state.get_db("statistics").retrieve("failed_downloads"),
                        f"{label}: counter must still be 1 after rerun",
                    )
                    # The failed row persists; the unchanged counter proves no
                    # second terminal effect was applied.


# ── method 4: reporting package absent is idempotent ─────────────────────────


class TestScrubReportingPackageAbsent(BlacklistTestCase):
    """Reporting package already absent/terminal -> idempotent scrub."""

    def test_scrub_reporting_package_absent_idempotent(self):
        from quasarr.providers.filecrypt_lifecycle import (
            FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE,
            FILECRYPT_MIGRATION_KEY,
            encode_migration_marker,
        )

        # Seed blacklisted fingerprint but NO protected package
        self.seed_blacklisted(fp(1))
        self.state.get_db("protected").store(
            pkg(99),
            json.dumps(
                {
                    "title": "O",
                    "password": "",
                    "links": [["https://other.invalid/a", "other"]],
                },
                separators=(",", ":"),
            ),
        )
        self.state.get_db(FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE).store(
            FILECRYPT_MIGRATION_KEY,
            encode_migration_marker({"schema_version": 1, "completed_epoch": NOW - 1}),
        )

        terminal_service_mock = mock.Mock()
        terminal_service_mock.exclusive.side_effect = AssertionError(
            "exclusive() must not be called when package is absent"
        )

        app = Bottle()
        setup_sponsors_helper_routes(app)
        route = next(r for r in app.routes if r.rule == DECRYPT_RULE)
        payload = {"supported_urls": ["other.invalid"], "capabilities": _LIFECYCLE_CAPS}
        with (
            mock.patch("quasarr.api.sponsors_helper.shared_state", self.state),
            mock.patch("quasarr.api.sponsors_helper.request", mock.Mock(json=payload)),
            mock.patch(
                "quasarr.api.sponsors_helper.CrypterCooldownService",
                lambda s: mock.Mock(),
            ),
            mock.patch(
                "quasarr.api.sponsors_helper.TerminalOperationService",
                lambda s: terminal_service_mock,
            ),
            mock.patch(
                "quasarr.api.sponsors_helper.crypter_blocks_deferred", return_value=True
            ),
        ):
            # Should not raise - no exclusive() call for absent package
            result = route.callback()

        # Route must return a result (the other non-Filecrypt package is handed out)
        self.assertIsNotNone(result)


# ── method 5: partial failure resumes on next scrub ──────────────────────────


class TestScrubPartialFailureResumes(BlacklistTestCase):
    """One owner preserved (CONFLICT), another terminated (OPENED); rerun leaves counter unchanged."""

    def test_scrub_partial_failure_resumes(self):
        from quasarr.providers.filecrypt_lifecycle import (
            FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE,
            FILECRYPT_MIGRATION_KEY,
            encode_migration_marker,
        )

        # Two packages both own fp(1) with no alternatives.
        # pkg(1): pre-seeded CONFLICT record -> exclusive() returns CONFLICT -> link preserved
        # pkg(2): no pre-seed -> exclusive() returns OPENED -> real terminal failure
        self.seed_blacklisted(fp(1))
        self.state.get_db("protected").store(
            pkg(1), _pkg_with_links([[url(1), FILECRYPT_CRYPTER]])
        )
        self.state.get_db("protected").store(
            pkg(2), _pkg_with_links([[url(1), FILECRYPT_CRYPTER]])
        )
        self.state.get_db("protected").store(
            pkg(99),
            json.dumps(
                {
                    "title": "O",
                    "password": "",
                    "links": [["https://other.invalid/b", "other"]],
                },
                separators=(",", ":"),
            ),
        )
        self.state.get_db(FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE).store(
            FILECRYPT_MIGRATION_KEY,
            encode_migration_marker({"schema_version": 1, "completed_epoch": NOW - 1}),
        )

        # Pre-seed a conflicting operation record for pkg(1) so exclusive() returns CONFLICT.
        # The record claims a different package_id, triggering the mismatch branch in begin().
        self.state.get_db(TERMINAL_OPERATION_TABLE).store(
            terminal_operation_id(pkg(1)),
            json.dumps(
                {
                    "state": "prepared",
                    "terminal_state": "failed",
                    "package_id": pkg(99),  # mismatch → CONFLICT
                    "created_epoch": int(time.time()),
                    "updated_epoch": int(time.time()),
                    "package_removed": False,
                    "package_terminal": False,
                    "effect_state": EFFECT_NOT_STARTED,
                    "failure_persisted": False,
                    "notification_state": NOTIFICATION_NOT_STARTED,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

        app = Bottle()
        setup_sponsors_helper_routes(app)
        route = next(r for r in app.routes if r.rule == DECRYPT_RULE)
        payload = {"supported_urls": ["other.invalid"], "capabilities": _LIFECYCLE_CAPS}
        with (
            mock.patch("quasarr.api.sponsors_helper.shared_state", self.state),
            mock.patch("quasarr.api.sponsors_helper.request", mock.Mock(json=payload)),
            mock.patch(
                "quasarr.api.sponsors_helper.CrypterCooldownService",
                lambda s: mock.Mock(),
            ),
            mock.patch(
                "quasarr.api.sponsors_helper.crypter_blocks_deferred", return_value=True
            ),
            mock.patch("quasarr.api.sponsors_helper.update_release_notification"),
        ):
            route.callback()

        # pkg(1): CONFLICT -> link preserved for retry
        self.assertIsNotNone(
            self.state.get_db("protected").retrieve(pkg(1)),
            "pkg(1) must be preserved when terminal op returns CONFLICT",
        )
        # pkg(2): OPENED -> real terminal failure completed
        self.assertIsNone(
            self.state.get_db("protected").retrieve(pkg(2)),
            "pkg(2) must be removed after terminal failure",
        )
        self.assertIsNotNone(
            self.state.get_db("failed").retrieve(pkg(2)),
            "pkg(2) must have a failed history row",
        )
        # Counter must be exactly once (requirement 2: no duplicate effects)
        self.assertEqual(
            "1",
            self.state.get_db("statistics").retrieve("failed_downloads"),
            "failed_downloads counter must be exactly 1",
        )

        # ── requirement 2: rerun must not add a second effect ──
        with (
            mock.patch("quasarr.api.sponsors_helper.shared_state", self.state),
            mock.patch("quasarr.api.sponsors_helper.request", mock.Mock(json=payload)),
            mock.patch(
                "quasarr.api.sponsors_helper.CrypterCooldownService",
                lambda s: mock.Mock(),
            ),
            mock.patch(
                "quasarr.api.sponsors_helper.crypter_blocks_deferred", return_value=True
            ),
            mock.patch("quasarr.api.sponsors_helper.update_release_notification"),
        ):
            route.callback()

        self.assertEqual(
            "1",
            self.state.get_db("statistics").retrieve("failed_downloads"),
            "counter must still be 1 after rerun",
        )


if __name__ == "__main__":
    unittest.main()
