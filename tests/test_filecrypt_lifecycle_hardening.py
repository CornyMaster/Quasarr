# -*- coding: utf-8 -*-
"""Hardening tests: prune_receipts and lifecycle storage resilience."""

import os
import tempfile
import threading
import unittest
from unittest import mock

from quasarr.providers import shared_state as provider_shared_state
from quasarr.providers.crypter_cooldowns import (
    CRYPTER_EVENT_KEY,
    CRYPTER_EVENT_TABLE,
    decode_pending_crypter_events,
)
from quasarr.providers.filecrypt_lifecycle import (
    FILECRYPT_LINK_STATES_TABLE,
    FILECRYPT_OFFER_RECEIPTS_TABLE,
    FILECRYPT_SWEEP_KEY,
    FILECRYPT_SWEEP_MEMBERS_TABLE,
    FILECRYPT_SWEEP_STATE_TABLE,
    decode_link_state,
    decode_offer_receipt,
    decode_sweep_header,
    decode_sweep_member,
    encode_offer_receipt,
)
from quasarr.providers.filecrypt_lifecycle_decisions import (
    build_lifecycle_access_decision,
    build_lifecycle_defer_decision,
)
from quasarr.providers.filecrypt_lifecycle_service import (
    RECEIPT_ADVISORY_THRESHOLD,
    FilecryptLifecycleService,
)
from quasarr.providers.terminal_operations import terminal_operation_id
from quasarr.storage.sqlite_database import DataBase
from tests.test_filecrypt_lifecycle_service import (
    NOW,
    AtomicSharedState,
    FakeClock,
    SequentialIds,
    rows_for,
)

_COOLDOWN_HOURS = 24
_COOLDOWN_SECONDS = _COOLDOWN_HOURS * 3600
_WINDOW_SECONDS = 15 * 60


# ── helpers ──────────────────────────────────────────────────────────────────


def _receipt_fp(n):
    """64-lowercase-hex fingerprint for testing (no real URL needed)."""
    return f"{n:064x}"


def _receipt_pkg(n):
    return f"Quasarr_movies_{n:032x}"


def _receipt_id(n):
    return f"{n:032x}"


def _make_raw_receipt(offer_id, fingerprint, package_id, expires_epoch, *, now=NOW):
    """Encode a valid blocked/individual receipt with the given expiry."""
    response = build_lifecycle_defer_decision(
        instruction="hold",
        state="individual",
        hold_type="provisional",
        evidence_count=0,
        retry_after_epoch=now + _COOLDOWN_SECONDS,
        sweep_id=offer_id,
        sweep_tested=0,
        sweep_total=0,
        sweep_deadline_epoch=now + _COOLDOWN_SECONDS,
    )
    return encode_offer_receipt(
        {
            "schema_version": 1,
            "generation_id": offer_id,
            "fingerprint": fingerprint,
            "package_id": package_id,
            "mode": "individual",
            "outcome": "blocked",
            "response": response,
            "accepted_epoch": now,
            "expires_epoch": expires_epoch,
        }
    )


def _make_clear_receipt(offer_id, fingerprint, package_id, expires_epoch, *, now=NOW):
    """Encode a valid clear/individual receipt."""
    response = build_lifecycle_access_decision(
        state="healthy",
        cleared=True,
        accepted="",
        sweep_id=offer_id,
        sweep_tested=0,
        sweep_total=0,
        sweep_deadline_epoch=now + _WINDOW_SECONDS,
    )
    return encode_offer_receipt(
        {
            "schema_version": 1,
            "generation_id": offer_id,
            "fingerprint": fingerprint,
            "package_id": package_id,
            "mode": "individual",
            "outcome": "clear",
            "response": response,
            "accepted_epoch": now,
            "expires_epoch": expires_epoch,
        }
    )


def _make_blocked_report(offer):
    pkg_id = offer["occurrence"].package_id
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


def _make_access_report(offer, access="clear"):
    pkg_id = offer["occurrence"].package_id
    return {
        "package_id": pkg_id,
        "crypter": "filecrypt",
        "access": access,
        "link_fingerprint": offer["link_fingerprint"],
        "sweep_id": offer["sweep_id"],
        "offer_id": offer["offer_id"],
        "protocol_version": 2,
        "terminal_operation_id": terminal_operation_id(pkg_id),
    }


class _RealSharedState:
    """Slim shared state backed by one real SQLite file for lifecycle tests."""

    def __init__(self, dbfile):
        self._databases = {}
        self.values = {
            "dbfile": dbfile,
            "crypter_cooldown_hours": _COOLDOWN_HOURS,
        }

    def get_db(self, table):
        if table not in self._databases:
            self._databases[table] = DataBase(table)
        return self._databases[table]

    def update(self, key, value):
        self.values[key] = value

    def close(self):
        for db in self._databases.values():
            db._conn.close()
        self._databases.clear()


# ── pruning tests ─────────────────────────────────────────────────────────────


class PruningTests(unittest.TestCase):
    """FilecryptLifecycleService.prune_receipts contract."""

    def setUp(self):
        self.clock = FakeClock(NOW)
        self.ids = SequentialIds()
        self.state = AtomicSharedState()
        self.state.values["crypter_cooldown_hours"] = _COOLDOWN_HOURS

    def service(self):
        return FilecryptLifecycleService(
            self.state, clock=self.clock, identifier_factory=self.ids
        )

    def receipts_db(self):
        return self.state.get_db(FILECRYPT_OFFER_RECEIPTS_TABLE)

    def test_empty_table_returns_zero_no_mutation(self):
        svc = self.service()
        result = svc.prune_receipts()

        self.assertEqual(result, 0)
        # mutate_values must never be called for an empty table
        self.assertEqual(self.receipts_db().mutation_count, 0)

    def test_expired_at_now_removed_future_retained(self):
        # expires_epoch == NOW is expired; expires_epoch == NOW+1 is live
        oid_exp = _receipt_id(1)
        oid_live = _receipt_id(2)
        raw_exp = _make_raw_receipt(
            oid_exp, _receipt_fp(1), _receipt_pkg(1), expires_epoch=NOW
        )
        raw_live = _make_raw_receipt(
            oid_live, _receipt_fp(2), _receipt_pkg(2), expires_epoch=NOW + 1
        )
        db = self.receipts_db()
        db.store(oid_exp, raw_exp)
        db.store(oid_live, raw_live)

        result = self.service().prune_receipts()

        self.assertEqual(result, 1)
        self.assertIsNone(db.retrieve(oid_exp))
        self.assertEqual(db.retrieve(oid_live), raw_live)

    def test_malformed_preserved_expired_valid_removed_warning_no_identifier(self):
        oid_malformed = _receipt_id(10)
        oid_expired = _receipt_id(11)
        raw_malformed = "not valid json at all"
        raw_expired = _make_raw_receipt(
            oid_expired, _receipt_fp(11), _receipt_pkg(11), expires_epoch=NOW - 1
        )
        db = self.receipts_db()
        db.store(oid_malformed, raw_malformed)
        db.store(oid_expired, raw_expired)

        warned = []
        with mock.patch("quasarr.providers.log.warn", side_effect=warned.append):
            result = self.service().prune_receipts()

        self.assertEqual(result, 1)
        self.assertEqual(db.retrieve(oid_malformed), raw_malformed)
        self.assertIsNone(db.retrieve(oid_expired))

        # At least one warning contains the malformed count
        warning_text = " ".join(warned)
        self.assertIn("1", warning_text)
        # No warning must contain the malformed row's key or fingerprint
        self.assertNotIn(oid_malformed, warning_text)
        self.assertNotIn(_receipt_fp(10), warning_text)
        self.assertNotIn(_receipt_pkg(10), warning_text)

    def test_advisory_threshold_below_no_threshold_warning(self):
        # RECEIPT_ADVISORY_THRESHOLD - 1 expired rows → no threshold warning
        n = RECEIPT_ADVISORY_THRESHOLD - 1
        db = self.receipts_db()
        for i in range(n):
            oid = _receipt_id(i)
            db.store(
                oid, _make_raw_receipt(oid, _receipt_fp(i), _receipt_pkg(i), NOW - 1)
            )

        threshold_warned = []

        def capture(msg):
            if str(RECEIPT_ADVISORY_THRESHOLD) in msg and "rows" in msg:
                threshold_warned.append(msg)

        with mock.patch("quasarr.providers.log.warn", side_effect=capture):
            result = self.service().prune_receipts()

        self.assertEqual(result, n)
        self.assertEqual(len(threshold_warned), 0, "must not warn below threshold")

    def test_advisory_threshold_at_boundary_warning_and_continues(self):
        # RECEIPT_ADVISORY_THRESHOLD expired rows → threshold warning, all pruned
        n = RECEIPT_ADVISORY_THRESHOLD
        db = self.receipts_db()
        for i in range(n):
            oid = _receipt_id(i)
            db.store(
                oid, _make_raw_receipt(oid, _receipt_fp(i), _receipt_pkg(i), NOW - 1)
            )

        threshold_warned = []

        def capture(msg):
            if str(n) in msg and "rows" in msg:
                threshold_warned.append(msg)

        with mock.patch("quasarr.providers.log.warn", side_effect=capture):
            result = self.service().prune_receipts()

        self.assertEqual(result, n)
        self.assertGreater(len(threshold_warned), 0, "must warn at threshold")

    def test_5000_expired_all_pruned_in_one_mutation_no_cap(self):
        n = 5000
        db = self.receipts_db()
        for i in range(n):
            oid = _receipt_id(i)
            db.store(
                oid, _make_raw_receipt(oid, _receipt_fp(i), _receipt_pkg(i), NOW - 1)
            )

        with mock.patch("quasarr.providers.log.warn"):
            result = self.service().prune_receipts()

        self.assertEqual(result, n)
        self.assertEqual(db.mutation_count, 1, "exactly one mutate_values call")
        self.assertIsNone(db.retrieve_all_titles(), "table must be empty")

    def test_5000_live_returns_zero_no_mutation(self):
        n = 5000
        db = self.receipts_db()
        for i in range(n):
            oid = _receipt_id(i)
            db.store(
                oid, _make_raw_receipt(oid, _receipt_fp(i), _receipt_pkg(i), NOW + 1)
            )

        result = self.service().prune_receipts()

        self.assertEqual(result, 0)
        self.assertEqual(db.mutation_count, 0, "must not call mutate_values")

    def test_concurrent_replacement_survives_other_expired_rows_pruned(self):
        # Two expired receipts: A and B.
        # Before the pruning mutation fires, A is replaced with a live receipt.
        # A must survive; B must be deleted.
        oid_a = _receipt_id(1)
        oid_b = _receipt_id(2)
        raw_a_expired = _make_raw_receipt(
            oid_a, _receipt_fp(1), _receipt_pkg(1), expires_epoch=NOW - 1
        )
        raw_a_live = _make_raw_receipt(
            oid_a, _receipt_fp(1), _receipt_pkg(1), expires_epoch=NOW + 9999
        )
        raw_b_expired = _make_raw_receipt(
            oid_b, _receipt_fp(2), _receipt_pkg(2), expires_epoch=NOW - 1
        )
        db = self.receipts_db()
        db.store(oid_a, raw_a_expired)
        db.store(oid_b, raw_b_expired)

        # Replace A before the pruning callback reads current values
        def replace_a():
            db.rows[oid_a] = raw_a_live

        db.before_mutation = replace_a

        result = self.service().prune_receipts()

        # Only B was actually changed from matching raw → None
        self.assertEqual(result, 1)
        self.assertEqual(
            db.retrieve(oid_a), raw_a_live, "replaced receipt must survive"
        )
        self.assertIsNone(db.retrieve(oid_b), "expired receipt must be pruned")

    def test_write_failure_propagates_rows_unchanged(self):
        oid = _receipt_id(1)
        raw = _make_raw_receipt(oid, _receipt_fp(1), _receipt_pkg(1), NOW - 1)
        db = self.receipts_db()
        db.store(oid, raw)

        # Override mutate_values to raise before any write
        def failing_mutate(targets, mutator):
            raise RuntimeError("injected write failure")

        db.mutate_values = failing_mutate

        svc = self.service()
        with self.assertRaises(RuntimeError):
            svc.prune_receipts()

        # Row still present (write never happened)
        self.assertEqual(db.retrieve(oid), raw)

    def test_clock_not_called_inside_callback(self):
        # Clock raises on the second call; callback must not call it.
        call_count = [0]

        def one_shot_clock():
            call_count[0] += 1
            if call_count[0] > 1:
                raise RuntimeError("clock must not be called inside callback")
            return NOW

        oid = _receipt_id(1)
        raw = _make_raw_receipt(oid, _receipt_fp(1), _receipt_pkg(1), NOW - 1)
        db = self.receipts_db()
        db.store(oid, raw)

        self.state.values["crypter_cooldown_hours"] = _COOLDOWN_HOURS
        svc = FilecryptLifecycleService(
            self.state, clock=one_shot_clock, identifier_factory=self.ids
        )
        # Must not raise; clock was called exactly once (before callback)
        result = svc.prune_receipts()
        self.assertEqual(result, 1)
        self.assertEqual(call_count[0], 1)

    def test_double_mutator_invocation_counts_correctly(self):
        """Mutator double invocation before commit does not double-count deletions.

        Requirement: if prune_receipts' callback is hypothetically called twice
        before mutate_values commits, the returned deletion count must reflect
        only what was actually deleted (not accumulated double counts).

        Test: Store one expired receipt. Mock mutate_values to invoke the
        callback twice, then return committed_values with the receipt as None
        only on the second call. Assert prune_receipts returns count=1, not 2.
        """
        oid = _receipt_id(1)
        raw = _make_raw_receipt(oid, _receipt_fp(1), _receipt_pkg(1), NOW - 1)
        db = self.receipts_db()
        db.store(oid, raw)

        # Capture the callback to invoke it twice
        captured_callback = [None]
        original_mutate = db.mutate_values

        def double_invoke_mutate(targets, mutator):
            captured_callback[0] = mutator
            # Invoke the callback twice before the real mutate_values
            current_values = tuple(db.retrieve(key) for _table, key in targets)
            first_result = mutator(current_values)
            second_result = mutator(current_values)
            # The second invocation should return the same (deletion marked as None)
            self.assertEqual(
                first_result, second_result, "callback must be deterministic"
            )
            # Now call the real mutate_values
            return original_mutate(targets, mutator)

        db.mutate_values = double_invoke_mutate

        result = self.service().prune_receipts()

        # Must return 1, not 2 (no double-counting)
        self.assertEqual(result, 1)
        self.assertIsNone(db.retrieve(oid), "receipt must be deleted")

    def test_concurrent_deletion_counts_only_local_prune(self):
        """Concurrent external deletion of A must not inflate the returned count.

        Two expired receipts A and B are enumerated.  Before the pruning
        callback reads current values, a concurrent actor deletes A entirely.
        The callback sees current_raw=None for A (mismatch against enumerated),
        so it preserves None without marking it as locally deleted.  Only B is
        deleted by this call.  Returned count must be 1, not 2.
        """
        oid_a = _receipt_id(1)
        oid_b = _receipt_id(2)
        raw_a = _make_raw_receipt(
            oid_a, _receipt_fp(1), _receipt_pkg(1), expires_epoch=NOW - 1
        )
        raw_b = _make_raw_receipt(
            oid_b, _receipt_fp(2), _receipt_pkg(2), expires_epoch=NOW - 1
        )
        db = self.receipts_db()
        db.store(oid_a, raw_a)
        db.store(oid_b, raw_b)

        # Concurrent actor deletes A before the callback reads current values.
        db.before_mutation = lambda: db.rows.pop(oid_a, None)

        result = self.service().prune_receipts()

        # Only B was deleted by this call; A was already gone externally.
        self.assertEqual(result, 1)
        self.assertIsNone(db.retrieve(oid_a), "A absent (deleted externally)")
        self.assertIsNone(db.retrieve(oid_b), "B absent (pruned locally)")


# ── hardening tests: fake store ───────────────────────────────────────────────


class LifecycleHardeningFakeTests(unittest.TestCase):
    """Lifecycle storage hardening using the in-memory fake store."""

    def setUp(self):
        self.clock = FakeClock(NOW)
        self.ids = SequentialIds()
        self.state = AtomicSharedState()
        self.state.values["crypter_cooldown_hours"] = _COOLDOWN_HOURS

    def service(self):
        return FilecryptLifecycleService(
            self.state, clock=self.clock, identifier_factory=self.ids
        )

    def _store_packages(self, rows):
        pdb = self.state.get_db("protected")
        for pkg_id, blob in rows:
            pdb.update_store(pkg_id, blob)

    def test_clear_before_blocked_serialized_interleaving(self):
        """CLEAR committed inside BLOCKED's mutex: BLOCKED returns None, no cooldown.

        Deterministic serialized interleaving, not true concurrency.  The
        `before_mutation` hook fires inside record_blocked's mutate_values, before
        current_values is read, so CLEAR's committed receipt is visible to the
        BLOCKED callback which treats it as conflicting → None.
        """
        n = 5
        rows = rows_for(range(1, n + 1))
        self._store_packages(rows)
        svc = self.service()

        # Open the sweep by getting + recording the first 4 members as BLOCKED.
        for _ in range(4):
            offer = svc.prepare_offer(rows)
            self.assertIsNotNone(offer)
            result = svc.record_blocked(_make_blocked_report(offer), rows)
            self.assertIsNotNone(result)

        # Lease the 5th member.
        offer_5 = svc.prepare_offer(rows)
        self.assertIsNotNone(offer_5)

        clear_report = _make_access_report(offer_5, access="clear")
        blocked_report = _make_blocked_report(offer_5)
        fp_5 = offer_5["link_fingerprint"]

        # Install hook: submit CLEAR for member 5 inside record_blocked's mutex.
        sweep_state_db = self.state.get_db(FILECRYPT_SWEEP_STATE_TABLE)

        def clear_in_hook():
            r = svc.record_access(clear_report, rows)
            self.assertIsNotNone(r, "CLEAR inside hook must succeed")

        sweep_state_db.before_mutation = clear_in_hook

        blocked_result = svc.record_blocked(blocked_report, rows)

        # BLOCKED must return None: CLEAR receipt is already there.
        self.assertIsNone(blocked_result)

        # Header must be healthy (sweep completed with one CLEAR member).
        hdr_raw = self.state.get_db(FILECRYPT_SWEEP_STATE_TABLE).retrieve(
            FILECRYPT_SWEEP_KEY
        )
        hdr = decode_sweep_header(hdr_raw)
        self.assertIsNotNone(hdr)
        self.assertEqual(hdr["state"], "healthy", "sweep ended healthy via CLEAR")

        # Member 5 must be clear, not blocked.
        member_5_raw = self.state.get_db(FILECRYPT_SWEEP_MEMBERS_TABLE).retrieve(fp_5)
        m5 = decode_sweep_member(member_5_raw)
        self.assertIsNotNone(m5)
        self.assertEqual(m5["state"], "clear")

        # No held link state for fp_5 (CLEAR never creates one).
        self.assertIsNone(self.state.get_db(FILECRYPT_LINK_STATES_TABLE).retrieve(fp_5))

        # Outbox: 4 observations from the 4 BLOCKEDs, 0 cooldowns.
        events_raw = self.state.get_db(CRYPTER_EVENT_TABLE).retrieve(CRYPTER_EVENT_KEY)
        events, readable = decode_pending_crypter_events(events_raw)
        self.assertTrue(readable)
        self.assertEqual(events["observations"], 4)
        self.assertEqual(events["cooldowns"], 0)

    def test_1000_member_all_blocked_scale(self):
        """1,000 members all BLOCKED before deadline: cooldown, exact counters."""
        n = 1000
        rows = rows_for(range(1, n + 1))
        self._store_packages(rows)
        svc = self.service()

        last_result = None

        # First prepare_offer opens the 1000-member sweep and leases the first.
        offer = svc.prepare_offer(rows)
        self.assertIsNotNone(offer)
        self.assertEqual(offer["mode"], "sweep")

        for i in range(n):
            if i > 0:
                offer = svc.prepare_offer(rows)
                self.assertIsNotNone(offer, f"no offer at iteration {i}")
                self.assertEqual(offer["mode"], "sweep")
            result = svc.record_blocked(_make_blocked_report(offer), rows)
            self.assertIsNotNone(
                result, f"record_blocked returned None at iteration {i}"
            )
            last_result = result

        # Final response must be a cooldown with exact counters.
        self.assertEqual(last_result["state"], "cooldown")
        self.assertEqual(last_result["sweep_tested"], n)
        self.assertEqual(last_result["sweep_total"], n)

        # 1,000 held link states.
        all_ls = self.state.get_db(FILECRYPT_LINK_STATES_TABLE).retrieve_all_titles()
        self.assertIsNotNone(all_ls)
        self.assertEqual(len(all_ls), n)
        for _fp_key, ls_raw in all_ls:
            ls = decode_link_state(ls_raw)
            self.assertIsNotNone(ls)
            self.assertEqual(ls["state"], "held")

        # 1,000 receipts.
        all_receipts = self.state.get_db(
            FILECRYPT_OFFER_RECEIPTS_TABLE
        ).retrieve_all_titles()
        self.assertIsNotNone(all_receipts)
        self.assertEqual(len(all_receipts), n)

        # Outbox: 1,000 observations, 1 cooldown.
        events_raw = self.state.get_db(CRYPTER_EVENT_TABLE).retrieve(CRYPTER_EVENT_KEY)
        events, readable = decode_pending_crypter_events(events_raw)
        self.assertTrue(readable)
        self.assertEqual(events["observations"], n)
        self.assertEqual(events["cooldowns"], 1)


# ── hardening tests: real SQLite ─────────────────────────────────────────────


class RealSQLiteHardeningTests(unittest.TestCase):
    """Hardening tests using a real temporary SQLite database."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.dbfile = os.path.join(self._tmpdir.name, "Quasarr.db")
        self._orig_values = provider_shared_state.values
        self._orig_lock = provider_shared_state.lock
        provider_shared_state.values = {"dbfile": self.dbfile}
        provider_shared_state.lock = None
        self.clock = FakeClock(NOW)
        self.ids = SequentialIds()
        self.state = _RealSharedState(self.dbfile)
        self.addCleanup(self._restore)

    def _restore(self):
        self.state.close()
        provider_shared_state.values = self._orig_values
        provider_shared_state.lock = self._orig_lock

    def service(self):
        return FilecryptLifecycleService(
            self.state, clock=self.clock, identifier_factory=self.ids
        )

    def _store_packages(self, rows):
        pdb = self.state.get_db("protected")
        for pkg_id, blob in rows:
            pdb.update_store(pkg_id, blob)

    def test_real_sqlite_duplicate_blocked_serialization(self):
        """Two threads share service state/connection serialization on same offer.

        Both threads submit the same BLOCKED offer concurrently.  They share a
        single SQLite database file (not independent connections per thread).
        SQLite serializes via IMMEDIATE transaction locks. Both threads finish,
        but only one's write commits; the other sees the committed receipt and
        returns None (conflict detected).
        """
        n = 5
        rows = rows_for(range(1, n + 1))
        self._store_packages(rows)

        svc = self.service()
        offer = svc.prepare_offer(rows)
        self.assertIsNotNone(offer)
        self.assertEqual(offer["mode"], "sweep")

        report = _make_blocked_report(offer)
        fp_offered = offer["link_fingerprint"]
        offer_id = offer["offer_id"]

        barrier = threading.Barrier(2)
        results = [None, None]
        errors = [None, None]

        def submit(idx):
            try:
                # Each thread gets its own service but shares the same state.
                svc_t = FilecryptLifecycleService(
                    self.state,
                    clock=FakeClock(NOW),
                    identifier_factory=SequentialIds(),
                )
                try:
                    barrier.wait(timeout=5)
                except threading.BrokenBarrierError as e:
                    errors[idx] = e
                    return
                results[idx] = svc_t.record_blocked(report, rows)
            except Exception as exc:  # noqa: BLE001
                errors[idx] = exc

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        for i, t in enumerate(threads):
            self.assertFalse(t.is_alive(), f"thread {i} timed out")
        for i, e in enumerate(errors):
            self.assertIsNone(e, f"thread {i} raised: {e}")

        # Both threads returned non-None equal wrappers.
        self.assertIsNotNone(results[0])
        self.assertIsNotNone(results[1])
        self.assertEqual(results[0], results[1])

        # Exactly one immutable receipt.
        rcpt_raw = self.state.get_db(FILECRYPT_OFFER_RECEIPTS_TABLE).retrieve(offer_id)
        self.assertIsNotNone(rcpt_raw)
        rcpt = decode_offer_receipt(rcpt_raw)
        self.assertIsNotNone(rcpt)
        self.assertEqual(rcpt["outcome"], "blocked")

        # Header: tested=1, blocked=1, state=sweeping.
        hdr_raw = self.state.get_db(FILECRYPT_SWEEP_STATE_TABLE).retrieve(
            FILECRYPT_SWEEP_KEY
        )
        hdr = decode_sweep_header(hdr_raw)
        self.assertIsNotNone(hdr)
        self.assertEqual(hdr["state"], "sweeping")
        self.assertEqual(hdr["tested"], 1)
        self.assertEqual(hdr["blocked"], 1)

        # One held link state for the offered fingerprint.
        ls_raw = self.state.get_db(FILECRYPT_LINK_STATES_TABLE).retrieve(fp_offered)
        ls = decode_link_state(ls_raw)
        self.assertIsNotNone(ls)
        self.assertEqual(ls["state"], "held")

        # Outbox: exactly 1 observation.
        events_raw = self.state.get_db(CRYPTER_EVENT_TABLE).retrieve(CRYPTER_EVENT_KEY)
        events, readable = decode_pending_crypter_events(events_raw)
        self.assertTrue(readable)
        self.assertEqual(events["observations"], 1)
        self.assertEqual(events["cooldowns"], 0)

    def test_real_sqlite_six_target_rollback(self):
        """Injected _upsert_value failure after ≥3 writes rolls back all 6 targets."""
        n = 5
        rows = rows_for(range(1, n + 1))
        self._store_packages(rows)

        svc = self.service()
        offer = svc.prepare_offer(rows)
        self.assertIsNotNone(offer)

        report = _make_blocked_report(offer)
        fp_offered = offer["link_fingerprint"]
        offer_id = offer["offer_id"]
        pkg_id = offer["occurrence"].package_id

        # Snapshot the six target values before the failing call.
        # Use independent reader connections (separate DataBase instances).
        reader_sweep = DataBase(FILECRYPT_SWEEP_STATE_TABLE)
        reader_member = DataBase(FILECRYPT_SWEEP_MEMBERS_TABLE)
        reader_ls = DataBase(FILECRYPT_LINK_STATES_TABLE)
        reader_receipt = DataBase(FILECRYPT_OFFER_RECEIPTS_TABLE)
        reader_protected = DataBase("protected")
        reader_events = DataBase(CRYPTER_EVENT_TABLE)
        readers = [
            reader_sweep,
            reader_member,
            reader_ls,
            reader_receipt,
            reader_protected,
            reader_events,
        ]
        try:
            snap_hdr = reader_sweep.retrieve(FILECRYPT_SWEEP_KEY)
            snap_member = reader_member.retrieve(fp_offered)
            snap_ls = reader_ls.retrieve(fp_offered)
            snap_receipt = reader_receipt.retrieve(offer_id)
            snap_protected = reader_protected.retrieve(pkg_id)
            snap_events = reader_events.retrieve(CRYPTER_EVENT_KEY)

            call_count = [0]
            original_impl = DataBase._upsert_value

            def failing_upsert(self_db, table, key, value):
                call_count[0] += 1
                if call_count[0] >= 4:
                    raise RuntimeError("injected upsert failure for rollback test")
                original_impl(self_db, table, key, value)

            with mock.patch.object(DataBase, "_upsert_value", failing_upsert):
                with self.assertRaises(RuntimeError):
                    svc.record_blocked(report, rows)

            # All 6 values must be byte-identical to the pre-call snapshot.
            self.assertEqual(reader_sweep.retrieve(FILECRYPT_SWEEP_KEY), snap_hdr)
            self.assertEqual(reader_member.retrieve(fp_offered), snap_member)
            self.assertEqual(reader_ls.retrieve(fp_offered), snap_ls)
            self.assertEqual(reader_receipt.retrieve(offer_id), snap_receipt)
            self.assertEqual(reader_protected.retrieve(pkg_id), snap_protected)
            self.assertEqual(reader_events.retrieve(CRYPTER_EVENT_KEY), snap_events)
        finally:
            for r in readers:
                r._conn.close()


if __name__ == "__main__":
    unittest.main()
