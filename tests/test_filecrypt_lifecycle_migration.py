# -*- coding: utf-8 -*-
"""Task 6A: Filecrypt lifecycle migration — proven state, atomicity, route gate."""

import json
import unittest
from unittest import mock

from bottle import Bottle, HTTPResponse

from quasarr.api.sponsors_helper import setup_sponsors_helper_routes
from quasarr.providers.crypter_cooldowns import (
    MINIMUM_COOLDOWN_HOURS,
    _encode_record,
)
from quasarr.providers.crypter_sweeps import (
    SWEEP_SCHEMA_VERSION,
    SWEEP_WINDOW_SECONDS,
    encode_decision_record,
)
from quasarr.providers.filecrypt_lifecycle import (
    FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE,
    FILECRYPT_LINK_STATES_TABLE,
    FILECRYPT_MIGRATION_KEY,
    FILECRYPT_SWEEP_KEY,
    FILECRYPT_SWEEP_STATE_TABLE,
    decode_link_state,
    decode_migration_marker,
    decode_sweep_header,
)
from quasarr.providers.filecrypt_lifecycle_service import (
    FILECRYPT_CRYPTER,
    FilecryptLifecycleService,
)
from tests.test_crypter_sweep_records import cohort_cooldown_record
from tests.test_filecrypt_lifecycle_service import (
    AtomicSharedState,
    FakeClock,
    SequentialIds,
    fp,
    pkg,
    url,
)

NOW = 1_700_000_000
COOLDOWN_SECS = MINIMUM_COOLDOWN_HOURS * 3600
SID = "a" * 32
DECRYPT_RULE = "/sponsors_helper/api/to_decrypt/"


# ── fixture helpers ───────────────────────────────────────────────────────────
# fmt: off


def _obs(n):
    return {"package_id": pkg(n), "link_fingerprint": fp(n), "seen_at_epoch": NOW - 5}


def legacy_cooldown(obs, retry=NOW + COOLDOWN_SECS):
    return _encode_record({"state": "cooldown", "reason_code": "ip_block_suspected",
        "first_seen_epoch": NOW - 100, "last_seen_epoch": NOW - 10,
        "retry_after_epoch": retry, "observations": obs})


def v2_cooldown(sweep_id, retry=NOW + COOLDOWN_SECS):
    return encode_decision_record(cohort_cooldown_record(
        sweep_id=sweep_id, opened_epoch=NOW - SWEEP_WINDOW_SECONDS,
        deadline_epoch=NOW, retry_after_epoch=retry))


def marked_legacy(retry=NOW + COOLDOWN_SECS):
    return encode_decision_record({"schema_version": SWEEP_SCHEMA_VERSION,
        "state": "cooldown", "reason_code": "ip_block_suspected",
        "legacy_cooldown": True, "retry_after_epoch": retry, "legacy_evidence_count": 2})


def v2_defer(sid, fps, since=NOW - 50, retry=NOW + COOLDOWN_SECS, probe=False):
    return {"schema_version": SWEEP_SCHEMA_VERSION, "crypter": "filecrypt",
        "reason_code": "ip_block_suspected", "since_epoch": since,
        "retry_after_epoch": retry, "probe_requested": probe,
        "observation_holds": 0, "sweep_id": sid, "link_fingerprints": sorted(fps)}


def legacy_defer(since=NOW - 50, retry=NOW + COOLDOWN_SECS):
    return {"crypter": "filecrypt", "reason_code": "ip_block_suspected",
        "since_epoch": since, "retry_after_epoch": retry,
        "probe_requested": False, "observation_holds": 0}


def pkg_raw(n, defer_dict):
    links = [[url(n), FILECRYPT_CRYPTER]]
    return json.dumps({"title": "T", "password": "", "links": links, "deferred": defer_dict},
        separators=(",", ":"), sort_keys=True)

def pkg_no_defer(n):
    links = [[url(n), FILECRYPT_CRYPTER]]
    return json.dumps({"title": "T", "password": "", "links": links},
        separators=(",", ":"), sort_keys=True)


_OBSERVING_RAW = _encode_record({"state": "observing", "reason_code": "ip_block_suspected",
    "first_seen_epoch": NOW - 100, "last_seen_epoch": NOW - 10,
    "retry_after_epoch": 0, "observations": [_obs(1)]})
_BAD_PKG = json.dumps({"title": "T", "password": "", "links": [[url(33), "filecrypt"]],
    "deferred": "not_a_dict"}, separators=(",", ":"), sort_keys=True)


# fmt: on


# ── base ──────────────────────────────────────────────────────────────────────


class MigrationTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(NOW)
        self.ids = SequentialIds()
        self.state = AtomicSharedState()

    def svc(self):
        return FilecryptLifecycleService(
            self.state, clock=self.clock, identifier_factory=self.ids
        )

    def seed(self, old_raw, protected):
        if old_raw is not None:
            self.state.get_db("crypter_cooldowns").store("filecrypt", old_raw)
        for pid, raw in protected:
            self.state.get_db("protected").store(pid, raw)

    def old_raw(self):
        return self.state.get_db("crypter_cooldowns").retrieve("filecrypt")

    def marker(self):
        return self.state.get_db(FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE).retrieve(
            FILECRYPT_MIGRATION_KEY
        )

    def hdr(self):
        raw = self.state.get_db(FILECRYPT_SWEEP_STATE_TABLE).retrieve(
            FILECRYPT_SWEEP_KEY
        )
        return None if raw is None else decode_sweep_header(raw)

    def ls(self, fingerprint):
        raw = self.state.get_db(FILECRYPT_LINK_STATES_TABLE).retrieve(fingerprint)
        return None if raw is None else decode_link_state(raw)

    def prot(self, pid):
        return self.state.get_db("protected").retrieve(pid)

    def prot_defer(self, pid):
        raw = self.prot(pid)
        return None if raw is None else json.loads(raw).get("deferred")

    def snap(self):
        return {table: dict(db.rows) for table, db in self.state.databases.items()}

    def assert_unchanged(self, before):
        for table, rows in before.items():
            db = self.state.databases.get(table)
            current = dict(db.rows) if db else {}
            self.assertEqual(rows, current, f"table {table!r} changed")


# ── method 1: state migration ────────────────────────────────────────────────


class TestMigrateLegacyStateMatrix(MigrationTestCase):
    """Task 6A method 1: proven state migration and v2-defer-without-old-row."""

    def test_migrate_legacy_state_matrix(self):
        # fmt: off
        CASES = [
            ("no_state", None, [], {"held": 0, "global_cooldown": False}, None),
            ("observing", _OBSERVING_RAW, [], {"held": 0, "global_cooldown": False}, None),
            ("v1_cooldown_legacy_defer", legacy_cooldown([_obs(1)], NOW + 5000),
             [[pkg(1), pkg_raw(1, legacy_defer(NOW - 80, NOW + 5000))]],
             {"held": 1, "global_cooldown": True},
             lambda s: (s.assertEqual(s.ls(fp(1))["state"], "held"),
                        s.assertEqual(s.ls(fp(1))["first_blocked_epoch"], NOW - 80),
                        s.assertIsNone(s.prot_defer(pkg(1))))),
            ("v2_cooldown_v2_defer", v2_cooldown(SID, NOW + 6000),
             [[pkg(2), pkg_raw(2, v2_defer(SID, [fp(2)], NOW - 30, NOW + 6000))]],
             {"held": 1, "global_cooldown": True},
             lambda s: (s.assertEqual(s.hdr()["generation_id"], SID),
                        s.assertEqual(s.ls(fp(2))["first_blocked_epoch"], NOW - 30))),
            ("expired_cooldown", legacy_cooldown([_obs(3)], NOW - 1),
             [[pkg(3), pkg_raw(3, legacy_defer(NOW - 80, NOW - 1))]],
             {"held": 0, "global_cooldown": False},
             lambda s: (s.assertIsNone(s.hdr()), s.assertIsNone(s.ls(fp(3))),
                        s.assertIsNone(s.prot_defer(pkg(3))))),
            ("v2_defer_multiple_fps", v2_cooldown(SID, NOW + 7000),
             [[pkg(4), pkg_raw(4, v2_defer(SID, [fp(4), fp(5)], NOW - 20, NOW + 7000))]],
             {"held": 2, "global_cooldown": True},
             lambda s: (s.assertIsNotNone(s.ls(fp(4))), s.assertIsNotNone(s.ls(fp(5))))),
            ("unmatched_legacy_defer", legacy_cooldown([_obs(6)], NOW + 3000),
             [[pkg(7), pkg_raw(7, legacy_defer(NOW - 50, NOW + 3000))]],
             {"held": 0, "global_cooldown": True},
             lambda s: s.assertIsNotNone(s.prot_defer(pkg(7)))),
            ("matched_legacy_defer", legacy_cooldown([_obs(8)], NOW + 4000),
             [[pkg(8), pkg_raw(8, legacy_defer(NOW - 60, NOW + 4000))]],
             {"held": 1, "global_cooldown": True},
             lambda s: (s.assertEqual(s.ls(fp(8))["first_blocked_epoch"], NOW - 60),
                        s.assertIsNone(s.prot_defer(pkg(8))))),
            ("marked_legacy", marked_legacy(NOW + 9000), [],
             {"held": 0, "global_cooldown": True},
             lambda s: (s.assertEqual(s.hdr()["retry_after_epoch"], NOW + 9000),
                        s.assertIsNone(s.old_raw()))),
            # Req 2: v2 defer migrates even when old decision row absent
            ("v2_defer_no_old_active", None,
             [[pkg(30), pkg_raw(30, v2_defer(SID, [fp(30)], NOW - 40, NOW + 8000))]],
             {"held": 1, "global_cooldown": False},
             lambda s: (s.assertEqual(s.ls(fp(30))["state"], "held"),
                        s.assertIsNone(s.prot_defer(pkg(30))))),
            ("v2_defer_no_old_expired", None,
             [[pkg(31), pkg_raw(31, v2_defer(SID, [fp(31)], NOW - 40, NOW - 1))]],
             {"held": 0, "global_cooldown": False},
             lambda s: (s.assertIsNone(s.ls(fp(31))), s.assertIsNone(s.prot_defer(pkg(31))))),
            # Req 3: probe_requested=True -> hold created, deferred preserved
            ("probe_preserves_defer", v2_cooldown(SID, NOW + 8000),
             [[pkg(32), pkg_raw(32, v2_defer(SID, [fp(32)], NOW - 20, NOW + 8000, probe=True))]],
             {"held": 1, "global_cooldown": True},
             lambda s: (s.assertEqual(s.ls(fp(32))["state"], "held"),
                        s.assertIsNotNone(s.prot_defer(pkg(32))))),
            # Req 4: malformed package defer -> byte-identical, no invented hold
            ("malformed_pkg_defer", v2_cooldown(SID, NOW + 5000), [[pkg(33), _BAD_PKG]],
             {"held": 0, "global_cooldown": True},
             lambda s: s.assertEqual(s.prot(pkg(33)), _BAD_PKG)),
        ]
        # fmt: on
        for label, old, prot, expect, verify in CASES:
            with self.subTest(label):
                self.setUp()
                self.seed(old, prot)
                result = self.svc().migrate_legacy(protected_rows=prot or None)
                self.assertEqual(result["status"], "complete", label)
                for k, v in expect.items():
                    self.assertEqual(result[k], v, f"{label}.{k}")
                self.assertIsNotNone(decode_migration_marker(self.marker()))
                if verify:
                    verify(self)


# ── method 2: fail-closed and atomicity ───────────────────────────────────────


class TestMigrationFailClosedAndAtomic(MigrationTestCase):
    """Task 6A method 2: fail-closed, malformed old decision, write failure."""

    def test_migration_fail_closed_and_atomic(self):
        sid = "b" * 32

        with self.subTest("malformed_old_decision"):
            self.setUp()
            self.state.get_db("crypter_cooldowns").store("filecrypt", "garbage{")
            prot = [[pkg(40), pkg_raw(40, v2_defer(sid, [fp(40)], retry=NOW + 5000))]]
            self.seed(None, prot)
            before = self.snap()
            result = self.svc().migrate_legacy(protected_rows=prot)
            self.assertEqual(result["status"], "unavailable")
            self.assertIsNone(decode_migration_marker(self.marker()))
            self.assert_unchanged(before)

        with self.subTest("malformed_marker"):
            self.setUp()
            self.state.get_db(FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE).store(
                FILECRYPT_MIGRATION_KEY, "not valid json{"
            )
            before = self.snap()
            result = self.svc().migrate_legacy(protected_rows=None)
            self.assertEqual(result["status"], "unavailable")
            self.assert_unchanged(before)

        with self.subTest("malformed_lifecycle_header"):
            self.setUp()
            self.state.get_db(FILECRYPT_SWEEP_STATE_TABLE).store(
                FILECRYPT_SWEEP_KEY, "corrupt"
            )
            self.seed(legacy_cooldown([], NOW + 5000), [])
            before = self.snap()
            result = self.svc().migrate_legacy(protected_rows=None)
            self.assertEqual(result["status"], "unavailable")
            self.assert_unchanged(before)

        with self.subTest("malformed_link_state"):
            self.setUp()
            self.seed(
                v2_cooldown(sid, NOW + 5000),
                [[pkg(10), pkg_raw(10, v2_defer(sid, [fp(10)], retry=NOW + 5000))]],
            )
            self.state.get_db(FILECRYPT_LINK_STATES_TABLE).store(fp(10), "bad")
            before = self.snap()
            result = self.svc().migrate_legacy(
                protected_rows=[
                    [pkg(10), pkg_raw(10, v2_defer(sid, [fp(10)], retry=NOW + 5000))]
                ]
            )
            self.assertEqual(result["status"], "unavailable")
            self.assert_unchanged(before)

        with self.subTest("concurrent_conflict"):
            self.setUp()
            self.seed(
                v2_cooldown(sid, NOW + 5000),
                [[pkg(11), pkg_raw(11, v2_defer(sid, [fp(11)], retry=NOW + 5000))]],
            )
            cd_db = self.state.get_db("crypter_cooldowns")
            sweep_db = self.state.get_db(FILECRYPT_SWEEP_STATE_TABLE)
            sweep_db.before_mutation = lambda: cd_db.store("filecrypt", "raced")
            result = self.svc().migrate_legacy(
                protected_rows=[
                    [pkg(11), pkg_raw(11, v2_defer(sid, [fp(11)], retry=NOW + 5000))]
                ]
            )
            self.assertEqual(result["status"], "conflict")
            self.assertIsNone(decode_migration_marker(self.marker()))

        with self.subTest("write_failure_propagates"):
            self.setUp()
            self.seed(
                v2_cooldown(sid, NOW + 5000),
                [[pkg(12), pkg_raw(12, v2_defer(sid, [fp(12)], retry=NOW + 5000))]],
            )
            before = self.snap()
            sweep_db = self.state.get_db(FILECRYPT_SWEEP_STATE_TABLE)
            sweep_db.mutate_values = lambda t, m: (_ for _ in ()).throw(
                OSError("disk full")
            )
            with self.assertRaises(OSError):
                self.svc().migrate_legacy(
                    protected_rows=[
                        [
                            pkg(12),
                            pkg_raw(12, v2_defer(sid, [fp(12)], retry=NOW + 5000)),
                        ]
                    ]
                )
            self.assertIsNone(decode_migration_marker(self.marker()))
            self.assert_unchanged(before)


# ── method 3: idempotence and route gate ──────────────────────────────────────


class TestMigrationIdempotenceAndRouteGate(MigrationTestCase):
    """Task 6A method 3: idempotence, route 503 gate."""

    def test_migration_idempotence_and_route_gate(self):
        sid = "c" * 32

        with self.subTest("idempotent_second_call"):
            self.setUp()
            prot = [[pkg(20), pkg_raw(20, v2_defer(sid, [fp(20)], retry=NOW + 5000))]]
            self.seed(v2_cooldown(sid, NOW + 5000), prot)
            svc = self.svc()
            r1 = svc.migrate_legacy(protected_rows=prot)
            self.assertEqual(r1["status"], "complete")
            self.assertEqual(r1["held"], 1)
            mc = self.state.get_db(FILECRYPT_SWEEP_STATE_TABLE).mutation_count
            r2 = svc.migrate_legacy(protected_rows=prot)
            self.assertEqual(r2["status"], "already_migrated")
            self.assertEqual(r2["held"], 0)
            self.assertEqual(
                self.state.get_db(FILECRYPT_SWEEP_STATE_TABLE).mutation_count, mc
            )

        with self.subTest("route_gate_unavailable"):
            self._assert_route_503("unavailable")

        with self.subTest("route_gate_conflict"):
            self._assert_route_503("conflict")

    def _assert_route_503(self, status):
        from quasarr.api.sponsors_helper.cohort_protocol import (
            CRYPTER_DEFER_CAPABILITY,
            FILECRYPT_COHORT_CAPABILITY,
        )

        fake_result = {
            "status": status,
            "held": 0,
            "packages_cleaned": 0,
            "global_cooldown": False,
        }
        app = Bottle()
        setup_sponsors_helper_routes(app)
        route = next(r for r in app.routes if r.rule == DECRYPT_RULE)
        state = AtomicSharedState()
        state.get_db("protected").store(pkg(50), pkg_no_defer(50))
        payload = {
            "supported_urls": ["filecrypt.invalid"],
            "capabilities": [
                CRYPTER_DEFER_CAPABILITY,
                FILECRYPT_COHORT_CAPABILITY,
                "filecrypt_link_lifecycle_v1",
            ],
        }
        with (
            mock.patch("quasarr.api.sponsors_helper.shared_state", state),
            mock.patch("quasarr.api.sponsors_helper.request", mock.Mock(json=payload)),
            mock.patch(
                "quasarr.api.sponsors_helper.CrypterCooldownService",
                lambda s: mock.Mock(),
            ),
            mock.patch(
                "quasarr.api.sponsors_helper.FilecryptLifecycleService"
            ) as mock_cls,
        ):
            instance = mock.Mock()
            instance.migrate_legacy.return_value = fake_result
            mock_cls.return_value = instance
            result = route.callback()
            self.assertIsInstance(result, HTTPResponse)
            self.assertEqual(result.status_code, 503)
            instance.prepare_offer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
