# -*- coding: utf-8 -*-
"""Tests for FilecryptLifecycleService record_blocked / record_access (Task 3B1)."""

import unittest

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
)
from quasarr.providers.filecrypt_lifecycle_decisions import (
    validate_access_response,
    validate_defer_response,
)
from quasarr.providers.filecrypt_lifecycle_service import (
    OFFER_LEASE_SECONDS,
    FilecryptLifecycleService,
)
from quasarr.providers.terminal_operations import terminal_operation_id
from tests.test_filecrypt_lifecycle_service import (
    NOW,
    AtomicSharedState,
    FakeClock,
    SequentialIds,
    rows_for,
)

COOLDOWN_HOURS = 24
COOLDOWN_SECONDS = COOLDOWN_HOURS * 3600


# ── helpers ──────────────────────────────────────────────────────────────────


def _report_blocked(offer, *, reason_code="ip_block_suspected"):
    return {
        "package_id": offer["occurrence"].package_id,
        "crypter": "filecrypt",
        "reason_code": reason_code,
        "link_fingerprint": offer["link_fingerprint"],
        "sweep_id": offer["sweep_id"],
        "offer_id": offer["offer_id"],
        "protocol_version": 2,
        "terminal_operation_id": terminal_operation_id(offer["occurrence"].package_id),
    }


def _report_access(offer, *, access="clear"):
    return {
        "package_id": offer["occurrence"].package_id,
        "crypter": "filecrypt",
        "access": access,
        "link_fingerprint": offer["link_fingerprint"],
        "sweep_id": offer["sweep_id"],
        "offer_id": offer["offer_id"],
        "protocol_version": 2,
        "terminal_operation_id": terminal_operation_id(offer["occurrence"].package_id),
    }


class OutcomeTestCase(unittest.TestCase):
    """Base with clock, ids, shared state, and service factory."""

    def setUp(self):
        self.clock = FakeClock(NOW)
        self.ids = SequentialIds()
        self.state = AtomicSharedState()
        self.state.values["crypter_cooldown_hours"] = COOLDOWN_HOURS

    def service(self):
        return FilecryptLifecycleService(
            self.state, clock=self.clock, identifier_factory=self.ids
        )

    def sweep_db(self):
        return self.state.get_db(FILECRYPT_SWEEP_STATE_TABLE)

    def members_db(self):
        return self.state.get_db(FILECRYPT_SWEEP_MEMBERS_TABLE)

    def ls_db(self):
        return self.state.get_db(FILECRYPT_LINK_STATES_TABLE)

    def receipts_db(self):
        return self.state.get_db(FILECRYPT_OFFER_RECEIPTS_TABLE)

    def events_db(self):
        return self.state.get_db(CRYPTER_EVENT_TABLE)

    def header(self):
        raw = self.sweep_db().retrieve(FILECRYPT_SWEEP_KEY)
        return None if raw is None else decode_sweep_header(raw)

    def member(self, fingerprint):
        raw = self.members_db().retrieve(fingerprint)
        return None if raw is None else decode_sweep_member(raw)

    def link_state(self, fingerprint):
        raw = self.ls_db().retrieve(fingerprint)
        return None if raw is None else decode_link_state(raw)

    def receipt(self, offer_id):
        raw = self.receipts_db().retrieve(offer_id)
        return None if raw is None else decode_offer_receipt(raw)

    def pending_events(self):
        raw = self.events_db().retrieve(CRYPTER_EVENT_KEY)
        counts, _ = decode_pending_crypter_events(raw)
        return counts

    def protected_rows(self, indices):
        rows = rows_for(indices)
        # Also store in the "protected" DB table so callback can read them
        db = self.state.get_db("protected")
        for pkg_id, raw in rows:
            db.store(pkg_id, raw)
        return rows


# ── first-time sweep BLOCKED ──────────────────────────────────────────────────


class TestFirstSweepBlocked(OutcomeTestCase):
    """RED: first sweep BLOCKED creates held at report_now + 24h."""

    def test_hold_at_report_time_plus_cooldown(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        self.assertIsNotNone(offer)
        self.assertEqual(offer["mode"], "sweep")

        report = _report_blocked(offer)
        result = svc.record_blocked(report, rows)

        self.assertIsNotNone(result)
        validate_defer_response(result)
        self.assertEqual(result["instruction"], "hold")
        self.assertEqual(result["state"], "sweeping")
        self.assertEqual(result["hold_type"], "provisional")

        ls = self.link_state(offer["link_fingerprint"])
        self.assertIsNotNone(ls)
        self.assertEqual(ls["state"], "held")
        self.assertEqual(ls["first_blocked_epoch"], NOW)
        self.assertEqual(ls["retry_after_epoch"], NOW + COOLDOWN_SECONDS)

    def test_hold_not_at_offer_or_sweep_deadline(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)

        # Advance clock past offer time but before lease expiry
        self.clock.now = NOW + 30
        report = _report_blocked(offer)
        svc.record_blocked(report, rows)

        ls = self.link_state(offer["link_fingerprint"])
        # Hold starts at report time (NOW+30), not offer time or sweep deadline
        self.assertEqual(ls["retry_after_epoch"], NOW + 30 + COOLDOWN_SECONDS)

    def test_member_transitions_to_blocked(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_blocked(offer)
        svc.record_blocked(report, rows)

        m = self.member(offer["link_fingerprint"])
        self.assertEqual(m["state"], "blocked")
        self.assertIsNone(m["lease"])
        self.assertEqual(m["outcome"]["offer_id"], offer["offer_id"])
        self.assertEqual(m["outcome"]["accepted_epoch"], NOW)

    def test_outbox_observations_incremented(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_blocked(offer)
        svc.record_blocked(report, rows)

        events = self.pending_events()
        self.assertEqual(events["observations"], 1)
        self.assertEqual(events["cooldowns"], 0)

    def test_receipt_stored(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_blocked(offer)
        result = svc.record_blocked(report, rows)

        rcpt = self.receipt(offer["offer_id"])
        self.assertIsNotNone(rcpt)
        self.assertEqual(rcpt["outcome"], "blocked")
        self.assertEqual(rcpt["mode"], "sweep")
        self.assertEqual(rcpt["response"], result)

    def test_blocked_does_not_prevent_next_member_offer(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer1 = svc.prepare_offer(rows)
        report = _report_blocked(offer1)
        svc.record_blocked(report, rows)

        offer2 = svc.prepare_offer(rows)
        self.assertIsNotNone(offer2)
        self.assertNotEqual(offer2["link_fingerprint"], offer1["link_fingerprint"])


# ── first-time individual BLOCKED ─────────────────────────────────────────────


class TestFirstIndividualBlocked(OutcomeTestCase):
    """RED: individual BLOCKED, no header dependency."""

    def test_individual_blocked_creates_held(self):
        svc = self.service()
        # Only 1 fp => individual mode (below MINIMUM_SWEEP_SIZE)
        rows = self.protected_rows([0])
        offer = svc.prepare_offer(rows)
        self.assertIsNotNone(offer)
        self.assertEqual(offer["mode"], "individual")

        report = _report_blocked(offer)
        result = svc.record_blocked(report, rows)

        self.assertIsNotNone(result)
        validate_defer_response(result)
        self.assertEqual(result["instruction"], "hold")
        self.assertEqual(result["state"], "individual")
        self.assertEqual(result["hold_type"], "provisional")
        self.assertEqual(result["sweep_tested"], 0)
        self.assertEqual(result["sweep_total"], 0)

        ls = self.link_state(offer["link_fingerprint"])
        self.assertEqual(ls["state"], "held")
        self.assertEqual(ls["retry_after_epoch"], NOW + COOLDOWN_SECONDS)


# ── CLEAR / UNKNOWN ──────────────────────────────────────────────────────────


class TestClearUnknown(OutcomeTestCase):
    """RED: CLEAR/UNKNOWN mark member, no link state, global impossible."""

    def test_clear_marks_member_no_link_state(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_access(offer, access="clear")
        result = svc.record_access(report, rows)

        self.assertIsNotNone(result)
        validate_access_response(result)
        self.assertTrue(result["cleared"])
        self.assertEqual(result["state"], "healthy")

        m = self.member(offer["link_fingerprint"])
        self.assertEqual(m["state"], "clear")
        ls = self.link_state(offer["link_fingerprint"])
        self.assertIsNone(ls)

    def test_clear_makes_global_impossible(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_access(offer, access="clear")
        svc.record_access(report, rows)

        hdr = self.header()
        self.assertFalse(hdr["global_possible"])

    def test_unknown_marks_member_no_link_state(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_access(offer, access="unknown")
        result = svc.record_access(report, rows)

        self.assertIsNotNone(result)
        validate_access_response(result)
        self.assertEqual(result["accepted"], "unknown")

        m = self.member(offer["link_fingerprint"])
        self.assertEqual(m["state"], "unknown")
        ls = self.link_state(offer["link_fingerprint"])
        self.assertIsNone(ls)

    def test_clear_no_outbox_delta(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_access(offer, access="clear")
        svc.record_access(report, rows)

        events = self.pending_events()
        self.assertEqual(events["observations"], 0)
        self.assertEqual(events["cooldowns"], 0)

    def test_remaining_member_continues_after_clear(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer1 = svc.prepare_offer(rows)
        report = _report_access(offer1, access="clear")
        svc.record_access(report, rows)

        offer2 = svc.prepare_offer(rows)
        self.assertIsNotNone(offer2)
        self.assertNotEqual(offer2["link_fingerprint"], offer1["link_fingerprint"])


# ── complete sweep: 5 all BLOCKED → cooldown ──────────────────────────────────


class TestFiveAllBlockedCooldown(OutcomeTestCase):
    """RED: 5 all BLOCKED before deadline → cooldown + five holds."""

    def test_five_blocked_triggers_cooldown(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offers = []
        for i in range(5):
            offer = svc.prepare_offer(rows)
            self.assertIsNotNone(offer, f"offer {i} was None")
            offers.append(offer)
            report = _report_blocked(offer)
            svc.record_blocked(report, rows)

        # Last result should be cooldown
        last_report = _report_blocked(offers[-1])
        last_result = svc.record_blocked(last_report, rows)
        # Replay
        validate_defer_response(last_result)
        self.assertEqual(last_result["instruction"], "cooldown")
        self.assertEqual(last_result["state"], "cooldown")
        self.assertEqual(last_result["hold_type"], "crypter_cooldown")

        # Five held link states
        for offer in offers:
            ls = self.link_state(offer["link_fingerprint"])
            self.assertIsNotNone(ls)
            self.assertEqual(ls["state"], "held")

        events = self.pending_events()
        self.assertEqual(events["observations"], 5)
        self.assertEqual(events["cooldowns"], 1)


# ── 4 all BLOCKED → no cooldown ──────────────────────────────────────────────


class TestFourBlockedNoCooldown(OutcomeTestCase):
    """RED: 4 all BLOCKED → no cooldown, four holds."""

    def test_four_blocked_no_cooldown(self):
        svc = self.service()
        rows = self.protected_rows(range(4))
        offers = []
        for i in range(4):
            offer = svc.prepare_offer(rows)
            self.assertIsNotNone(offer, f"offer {i} was None")
            offers.append(offer)
            report = _report_blocked(offer)
            result = svc.record_blocked(report, rows)

        # No cooldown instruction at any point
        self.assertNotEqual(result["instruction"], "cooldown")

        events = self.pending_events()
        self.assertEqual(events["observations"], 4)
        self.assertEqual(events["cooldowns"], 0)


# ── 499 BLOCKED + 1 CLEAR → no cooldown, complete healthy ─────────────────────


class TestMixedSweepNoCooldown(OutcomeTestCase):
    """RED: 499 BLOCKED + 1 CLEAR → no cooldown, 499 holds, complete healthy."""

    def test_mixed_no_cooldown(self):
        svc = self.service()
        # Need at least 500 fps: use range(500)
        rows = self.protected_rows(range(500))
        # Open sweep and accept all blocked except last
        offers = []
        for i in range(499):
            offer = svc.prepare_offer(rows)
            self.assertIsNotNone(offer, f"offer {i}")
            offers.append(offer)
            report = _report_blocked(offer)
            svc.record_blocked(report, rows)

        # Last one: CLEAR
        offer_clear = svc.prepare_offer(rows)
        self.assertIsNotNone(offer_clear)
        report = _report_access(offer_clear, access="clear")
        result = svc.record_access(report, rows)

        validate_access_response(result)
        self.assertTrue(result["cleared"])

        events = self.pending_events()
        self.assertEqual(events["observations"], 499)
        self.assertEqual(events["cooldowns"], 0)


# ── stale reports ─────────────────────────────────────────────────────────────


class TestStaleReports(OutcomeTestCase):
    """RED: expired lease, wrong generation/offer, deleted protected → stale."""

    def test_expired_lease_rejected(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)

        self.clock.now = NOW + OFFER_LEASE_SECONDS + 1
        report = _report_blocked(offer)
        result = svc.record_blocked(report, rows)
        self.assertIsNone(result)

    def test_wrong_offer_id_rejected(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_blocked(offer)
        report["offer_id"] = "f" * 32
        result = svc.record_blocked(report, rows)
        self.assertIsNone(result)

    def test_wrong_fingerprint_rejected(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_blocked(offer)
        report["link_fingerprint"] = "f" * 64
        # Fix terminal_operation_id to pass syntax
        report["terminal_operation_id"] = terminal_operation_id(report["package_id"])
        result = svc.record_blocked(report, rows)
        self.assertIsNone(result)

    def test_deleted_protected_row_rejected(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        # Remove the protected row from storage
        report = _report_blocked(offer)
        self.state.get_db("protected").rows.pop(offer["occurrence"].package_id, None)
        result = svc.record_blocked(report, rows)
        self.assertIsNone(result)


# ── replay ────────────────────────────────────────────────────────────────────


class TestReplay(OutcomeTestCase):
    """RED: exact replay returns same response, no second counters."""

    def test_blocked_replay_same_response(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_blocked(offer)
        result1 = svc.record_blocked(report, rows)
        result2 = svc.record_blocked(report, rows)
        self.assertEqual(result1, result2)

        events = self.pending_events()
        self.assertEqual(events["observations"], 1)

    def test_replay_after_member_and_protected_deletion(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_blocked(offer)
        result1 = svc.record_blocked(report, rows)

        # Delete member and protected row
        self.members_db().rows.pop(offer["link_fingerprint"], None)
        empty_rows = []
        result2 = svc.record_blocked(report, empty_rows)
        self.assertEqual(result1, result2)

    def test_clear_replay_same_response(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_access(offer, access="clear")
        result1 = svc.record_access(report, rows)
        result2 = svc.record_access(report, rows)
        self.assertEqual(result1, result2)

    def test_conflicting_receipt_not_replayed(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report_blocked = _report_blocked(offer)
        svc.record_blocked(report_blocked, rows)

        # Try to record access with the same offer → stale
        report_access = _report_access(offer, access="clear")
        result = svc.record_access(report_access, rows)
        self.assertIsNone(result)


# ── callback six-target validation ────────────────────────────────────────────


class TestCallbackTargets(OutcomeTestCase):
    """RED: callback target list contains all six targets."""

    def test_six_targets_in_blocked(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_blocked(offer)

        # Track mutate_values calls
        calls = []
        original_mutate = self.sweep_db().mutate_values

        def tracking_mutate(targets, mutator):
            calls.append(targets)
            return original_mutate(targets, mutator)

        self.sweep_db().mutate_values = tracking_mutate
        svc.record_blocked(report, rows)

        # Find the outcome call (six targets)
        outcome_calls = [c for c in calls if len(c) == 6]
        self.assertEqual(len(outcome_calls), 1)
        tables = [t[0] for t in outcome_calls[0]]
        self.assertIn(FILECRYPT_SWEEP_STATE_TABLE, tables)
        self.assertIn(FILECRYPT_SWEEP_MEMBERS_TABLE, tables)
        self.assertIn(FILECRYPT_LINK_STATES_TABLE, tables)
        self.assertIn(FILECRYPT_OFFER_RECEIPTS_TABLE, tables)
        self.assertIn(CRYPTER_EVENT_TABLE, tables)
        # Protected is "protected" (from rows)
        self.assertTrue(any("protected" in t[0] for t in outcome_calls[0]))


# ── mutation failure atomicity ────────────────────────────────────────────────


class TestAtomicFailure(OutcomeTestCase):
    """RED: callback write failure leaves no partial state."""

    def test_failure_leaves_no_partial(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_blocked(offer)

        # Inject an error in the mutation
        original_mutate = self.sweep_db().mutate_values

        def failing_mutate(targets, mutator):
            if len(targets) == 6:
                raise RuntimeError("injected failure")
            return original_mutate(targets, mutator)

        self.sweep_db().mutate_values = failing_mutate

        with self.assertRaises(RuntimeError):
            svc.record_blocked(report, rows)

        # No link state, no receipt, no outbox change
        ls = self.link_state(offer["link_fingerprint"])
        self.assertIsNone(ls)
        rcpt = self.receipt(offer["offer_id"])
        self.assertIsNone(rcpt)
        events = self.pending_events()
        self.assertEqual(events["observations"], 0)


# ── receipt privacy ───────────────────────────────────────────────────────────


class TestReceiptRawPrivacy(OutcomeTestCase):
    """RED: receipt raw JSON contains no URL/title/reason beyond IDs."""

    def test_receipt_no_url_title_reason(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_blocked(offer)
        svc.record_blocked(report, rows)

        raw = self.receipts_db().retrieve(offer["offer_id"])
        self.assertNotIn("http", raw)
        self.assertNotIn("title", raw)
        self.assertNotIn("reason", raw)
        self.assertNotIn("error", raw)


# ── 101-member sequence ──────────────────────────────────────────────────────


class TestLargeSequence(OutcomeTestCase):
    """RED: no cap/sentinel; 101-member sequence must work."""

    def test_101_members_all_blocked(self):
        svc = self.service()
        rows = self.protected_rows(range(101))
        offers = []
        for i in range(101):
            offer = svc.prepare_offer(rows)
            self.assertIsNotNone(offer, f"offer {i} was None")
            offers.append(offer)
            report = _report_blocked(offer)
            svc.record_blocked(report, rows)

        events = self.pending_events()
        self.assertEqual(events["observations"], 101)
        self.assertEqual(events["cooldowns"], 1)

        # All 101 link states held
        for offer in offers:
            ls = self.link_state(offer["link_fingerprint"])
            self.assertIsNotNone(ls)
            self.assertEqual(ls["state"], "held")


# ── protected row unchanged ──────────────────────────────────────────────────


class TestProtectedUnchanged(OutcomeTestCase):
    """RED: protected value returned unchanged from callback."""

    def test_protected_value_unchanged(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_blocked(offer)

        # Snapshot protected row
        pkg_id = offer["occurrence"].package_id
        original_raw = next(r[1] for r in rows if r[0] == pkg_id)

        svc.record_blocked(report, rows)

        # Protected row in DB unchanged
        protected_db = self.state.get_db("protected")
        current_raw = protected_db.retrieve(pkg_id)
        self.assertEqual(current_raw, original_raw)


if __name__ == "__main__":
    unittest.main()
