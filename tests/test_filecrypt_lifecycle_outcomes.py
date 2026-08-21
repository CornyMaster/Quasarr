# -*- coding: utf-8 -*-
"""Tests for FilecryptLifecycleService record_blocked / record_access."""

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
    MINIMUM_GLOBAL_COOLDOWN_SIZE,
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
    DEFAULT_SWEEP_BLOCK_THRESHOLD,
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
WINDOW_SECONDS = 15 * 60


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


def _response_of(result):
    """Strip wrapper fields, returning only the decision response dict."""
    excluded = {
        "terminal_required",
        "fingerprint",
        "package_id",
        "terminal_operation_id",
    }
    return {k: v for k, v in result.items() if k not in excluded}


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
        validate_defer_response(_response_of(result))
        self.assertEqual(result["instruction"], "hold")
        self.assertEqual(result["state"], "sweeping")
        self.assertEqual(result["hold_type"], "provisional")
        self.assertFalse(result["terminal_required"])
        self.assertEqual(result["fingerprint"], offer["link_fingerprint"])
        self.assertEqual(result["package_id"], offer["occurrence"].package_id)
        self.assertEqual(
            result["terminal_operation_id"],
            terminal_operation_id(offer["occurrence"].package_id),
        )

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
        self.assertEqual(rcpt["response"], _response_of(result))

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
        validate_defer_response(_response_of(result))
        self.assertEqual(result["instruction"], "hold")
        self.assertEqual(result["state"], "individual")
        self.assertEqual(result["hold_type"], "provisional")
        self.assertEqual(result["sweep_tested"], 0)
        self.assertEqual(result["sweep_total"], 0)
        self.assertFalse(result["terminal_required"])
        self.assertEqual(result["fingerprint"], offer["link_fingerprint"])
        self.assertEqual(result["package_id"], offer["occurrence"].package_id)

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
        validate_access_response(_response_of(result))
        self.assertTrue(result["cleared"])
        self.assertEqual(result["state"], "healthy")
        self.assertEqual(result["accepted"], "")
        self.assertEqual(result["sweep_tested"], 0)
        self.assertEqual(result["sweep_total"], 0)
        self.assertFalse(result["terminal_required"])
        self.assertEqual(result["fingerprint"], offer["link_fingerprint"])
        self.assertEqual(result["package_id"], offer["occurrence"].package_id)

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
        validate_access_response(_response_of(result))
        self.assertEqual(result["accepted"], "unknown")
        self.assertFalse(result["terminal_required"])
        self.assertEqual(result["fingerprint"], offer["link_fingerprint"])

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

        # Last result should be cooldown (replay)
        last_report = _report_blocked(offers[-1])
        last_result = svc.record_blocked(last_report, rows)
        validate_defer_response(_response_of(last_result))
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
        # This case is about the 100% trigger refusing a cohort with one CLEAR,
        # so the block-count trigger is lifted above the cohort size; at the
        # default threshold the sweep would pause long before member 499.
        self.state.values["filecrypt_sweep_block_threshold"] = 500
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

        validate_access_response(_response_of(result))
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
        self.assertFalse(result2["terminal_required"])
        self.assertEqual(result2["fingerprint"], offer["link_fingerprint"])

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
        self.assertFalse(result2["terminal_required"])

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
        # This case is about the absence of a cohort cap, so the block-count
        # trigger is lifted above the cohort size; at the default threshold the
        # sweep would pause after the fifth block and stop handing out members.
        self.state.values["filecrypt_sweep_block_threshold"] = 101
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


# ── service wrapper exact keys (fix 1) ───────────────────────────────────────


class TestServiceWrapperKeys(OutcomeTestCase):
    """Fix 1: record_blocked/access returns full wrapper with internal fields."""

    def test_blocked_fresh_wrapper_keys(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_blocked(offer)
        result = svc.record_blocked(report, rows)
        self.assertFalse(result["terminal_required"])
        self.assertEqual(result["fingerprint"], offer["link_fingerprint"])
        self.assertEqual(result["package_id"], offer["occurrence"].package_id)
        self.assertEqual(
            result["terminal_operation_id"],
            terminal_operation_id(offer["occurrence"].package_id),
        )

    def test_blocked_replay_wrapper_keys(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_blocked(offer)
        svc.record_blocked(report, rows)
        result = svc.record_blocked(report, rows)
        self.assertFalse(result["terminal_required"])
        self.assertEqual(result["fingerprint"], offer["link_fingerprint"])
        self.assertEqual(result["package_id"], offer["occurrence"].package_id)
        self.assertEqual(
            result["terminal_operation_id"],
            terminal_operation_id(offer["occurrence"].package_id),
        )

    def test_access_fresh_wrapper_keys(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_access(offer, access="clear")
        result = svc.record_access(report, rows)
        self.assertFalse(result["terminal_required"])
        self.assertEqual(result["fingerprint"], offer["link_fingerprint"])
        self.assertEqual(result["package_id"], offer["occurrence"].package_id)
        self.assertEqual(
            result["terminal_operation_id"],
            terminal_operation_id(offer["occurrence"].package_id),
        )

    def test_access_replay_wrapper_keys(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_access(offer, access="unknown")
        svc.record_access(report, rows)
        result = svc.record_access(report, rows)
        self.assertFalse(result["terminal_required"])
        self.assertEqual(result["fingerprint"], offer["link_fingerprint"])


# ── CLEAR counters (fix 2) ───────────────────────────────────────────────────


class TestClearCounters(OutcomeTestCase):
    """Fix 2: every CLEAR response has counters 0/0 and positive deadline."""

    def test_sweep_clear_response_counters_zero(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_access(offer, access="clear")
        result = svc.record_access(report, rows)
        self.assertEqual(result["sweep_tested"], 0)
        self.assertEqual(result["sweep_total"], 0)
        self.assertTrue(result["cleared"])
        self.assertEqual(result["accepted"], "")
        self.assertEqual(result["state"], "healthy")
        self.assertGreater(result["sweep_deadline_epoch"], 0)

    def test_individual_clear_response_counters_zero(self):
        svc = self.service()
        rows = self.protected_rows([0])
        offer = svc.prepare_offer(rows)
        report = _report_access(offer, access="clear")
        result = svc.record_access(report, rows)
        self.assertEqual(result["sweep_tested"], 0)
        self.assertEqual(result["sweep_total"], 0)
        self.assertTrue(result["cleared"])
        self.assertGreater(result["sweep_deadline_epoch"], 0)

    def test_persisted_header_still_sweeping_after_clear(self):
        """CLEAR response says healthy but persisted header remains sweeping."""
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_access(offer, access="clear")
        result = svc.record_access(report, rows)
        # Response says healthy
        self.assertEqual(result["state"], "healthy")
        # But persisted header is still sweeping with incremented tested
        hdr = self.header()
        self.assertEqual(hdr["state"], "sweeping")
        self.assertEqual(hdr["tested"], 1)
        self.assertFalse(hdr["global_possible"])


# ── header derivation (fix 3) ────────────────────────────────────────────────


class TestHeaderDerivation(OutcomeTestCase):
    """Fix 3: header classification for blocked/access both paths."""

    def test_no_header_permits_individual(self):
        svc = self.service()
        rows = self.protected_rows([0])
        offer = svc.prepare_offer(rows)
        self.assertEqual(offer["mode"], "individual")
        report = _report_blocked(offer)
        result = svc.record_blocked(report, rows)
        self.assertIsNotNone(result)
        self.assertEqual(result["state"], "individual")

    def test_live_healthy_header_stale_blocked(self):
        """A live healthy header makes a blocked report stale."""
        svc = self.service()
        rows = self.protected_rows([0])
        offer = svc.prepare_offer(rows)
        # offer writes a healthy header (individual writes healthy on success)
        # Instead, manually seed a healthy header and individual member
        from quasarr.providers.filecrypt_lifecycle import encode_sweep_header

        gen_id = offer["sweep_id"]
        healthy_hdr = {
            "schema_version": 1,
            "state": "healthy",
            "generation_id": gen_id,
            "until_epoch": NOW + WINDOW_SECONDS,
        }
        self.sweep_db().rows[FILECRYPT_SWEEP_KEY] = encode_sweep_header(healthy_hdr)
        report = _report_blocked(offer)
        result = svc.record_blocked(report, rows)
        self.assertIsNone(result)

    def test_live_cooldown_header_stale_blocked(self):
        """A live cooldown header makes a blocked report stale."""
        svc = self.service()
        rows = self.protected_rows([0])
        offer = svc.prepare_offer(rows)
        from quasarr.providers.filecrypt_lifecycle import encode_sweep_header

        gen_id = offer["sweep_id"]
        cooldown_hdr = {
            "schema_version": 1,
            "state": "cooldown",
            "generation_id": gen_id,
            "sweep_deadline_epoch": NOW + 100,
            "retry_after_epoch": NOW + COOLDOWN_SECONDS,
        }
        self.sweep_db().rows[FILECRYPT_SWEEP_KEY] = encode_sweep_header(cooldown_hdr)
        report = _report_blocked(offer)
        result = svc.record_blocked(report, rows)
        self.assertIsNone(result)

    def test_expired_header_permits_individual(self):
        """An expired valid header permits individual mode."""
        svc = self.service()
        rows = self.protected_rows([0])
        offer = svc.prepare_offer(rows)
        from quasarr.providers.filecrypt_lifecycle import encode_sweep_header

        gen_id = offer["sweep_id"]
        expired_hdr = {
            "schema_version": 1,
            "state": "healthy",
            "generation_id": gen_id,
            "until_epoch": NOW - 1,
        }
        self.sweep_db().rows[FILECRYPT_SWEEP_KEY] = encode_sweep_header(expired_hdr)
        report = _report_blocked(offer)
        result = svc.record_blocked(report, rows)
        self.assertIsNotNone(result)
        self.assertEqual(result["state"], "individual")

    def test_malformed_header_stale(self):
        """A malformed non-None header causes stale."""
        svc = self.service()
        rows = self.protected_rows([0])
        offer = svc.prepare_offer(rows)
        self.sweep_db().rows[FILECRYPT_SWEEP_KEY] = "not valid json {"
        report = _report_blocked(offer)
        result = svc.record_blocked(report, rows)
        self.assertIsNone(result)

    def test_live_sweeping_mismatched_generation_stale(self):
        """A live sweeping header with different generation is stale."""
        svc = self.service()
        rows = self.protected_rows([0])
        offer = svc.prepare_offer(rows)
        from quasarr.providers.filecrypt_lifecycle import encode_sweep_header

        sweeping_hdr = {
            "schema_version": 1,
            "state": "sweeping",
            "generation_id": "f" * 32,
            "opened_epoch": NOW,
            "deadline_epoch": NOW + WINDOW_SECONDS,
            "window_seconds": WINDOW_SECONDS,
            "total": 5,
            "tested": 0,
            "blocked": 0,
            "global_possible": True,
        }
        self.sweep_db().rows[FILECRYPT_SWEEP_KEY] = encode_sweep_header(sweeping_hdr)
        report = _report_blocked(offer)
        result = svc.record_blocked(report, rows)
        self.assertIsNone(result)

    def test_live_healthy_stale_access(self):
        """Live healthy also stales access reports."""
        svc = self.service()
        rows = self.protected_rows([0])
        offer = svc.prepare_offer(rows)
        from quasarr.providers.filecrypt_lifecycle import encode_sweep_header

        gen_id = offer["sweep_id"]
        healthy_hdr = {
            "schema_version": 1,
            "state": "healthy",
            "generation_id": gen_id,
            "until_epoch": NOW + WINDOW_SECONDS,
        }
        self.sweep_db().rows[FILECRYPT_SWEEP_KEY] = encode_sweep_header(healthy_hdr)
        report = _report_access(offer, access="clear")
        result = svc.record_access(report, rows)
        self.assertIsNone(result)


# ── generation binding (fix 4) ───────────────────────────────────────────────


class TestGenerationBinding(OutcomeTestCase):
    """Fix 4: sweep_id must equal member.generation_id."""

    def test_mismatched_generation_blocked_stale(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_blocked(offer)
        # Tamper with sweep_id in report
        report["sweep_id"] = "f" * 32
        result = svc.record_blocked(report, rows)
        self.assertIsNone(result)

    def test_mismatched_generation_access_stale(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_access(offer, access="clear")
        report["sweep_id"] = "f" * 32
        result = svc.record_access(report, rows)
        self.assertIsNone(result)

    def test_receipt_replay_requires_matching_generation(self):
        """Receipt with mismatched generation_id is not replayed."""
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_blocked(offer)
        svc.record_blocked(report, rows)

        # Craft a report with different sweep_id (but same offer_id)
        bad_report = dict(report)
        bad_report["sweep_id"] = "e" * 32
        result = svc.record_blocked(bad_report, rows)
        self.assertIsNone(result)

    def test_receipt_replay_requires_matching_generation_access(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_access(offer, access="clear")
        svc.record_access(report, rows)

        bad_report = dict(report)
        bad_report["sweep_id"] = "e" * 32
        result = svc.record_access(bad_report, rows)
        self.assertIsNone(result)


# ── response matrix (fix 5) ──────────────────────────────────────────────────


class TestResponseMatrix(OutcomeTestCase):
    """Fix 5: exact response field validation for all outcome scenarios."""

    def test_sweep_blocked_incomplete_response(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_blocked(offer)
        result = svc.record_blocked(report, rows)
        resp = _response_of(result)
        self.assertEqual(resp["instruction"], "hold")
        self.assertEqual(resp["state"], "sweeping")
        self.assertEqual(resp["hold_type"], "provisional")
        self.assertGreater(resp["retry_after_epoch"], 0)
        self.assertGreater(resp["sweep_deadline_epoch"], 0)
        self.assertLessEqual(resp["sweep_tested"], resp["sweep_total"])

    def test_individual_blocked_response(self):
        svc = self.service()
        rows = self.protected_rows([0])
        offer = svc.prepare_offer(rows)
        report = _report_blocked(offer)
        result = svc.record_blocked(report, rows)
        resp = _response_of(result)
        self.assertEqual(resp["instruction"], "hold")
        self.assertEqual(resp["state"], "individual")
        self.assertEqual(resp["hold_type"], "provisional")
        self.assertGreater(resp["sweep_deadline_epoch"], 0)
        self.assertGreater(resp["retry_after_epoch"], 0)
        self.assertEqual(resp["sweep_tested"], 0)
        self.assertEqual(resp["sweep_total"], 0)

    def test_cooldown_response_exact(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        for _i in range(5):
            offer = svc.prepare_offer(rows)
            report = _report_blocked(offer)
            result = svc.record_blocked(report, rows)
        resp = _response_of(result)
        self.assertEqual(resp["instruction"], "cooldown")
        self.assertEqual(resp["state"], "cooldown")
        self.assertEqual(resp["hold_type"], "crypter_cooldown")
        self.assertGreater(resp["evidence_count"], 0)
        self.assertGreater(resp["retry_after_epoch"], 0)
        self.assertGreater(resp["sweep_deadline_epoch"], 0)
        self.assertLessEqual(resp["sweep_tested"], resp["sweep_total"])

    def test_clear_response_exact_shape(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_access(offer, access="clear")
        result = svc.record_access(report, rows)
        resp = _response_of(result)
        self.assertEqual(resp["state"], "healthy")
        self.assertTrue(resp["cleared"])
        self.assertEqual(resp["accepted"], "")
        self.assertEqual(resp["sweep_tested"], 0)
        self.assertEqual(resp["sweep_total"], 0)
        self.assertGreater(resp["sweep_deadline_epoch"], 0)

    def test_unknown_incomplete_sweep_response(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offer = svc.prepare_offer(rows)
        report = _report_access(offer, access="unknown")
        result = svc.record_access(report, rows)
        resp = _response_of(result)
        self.assertEqual(resp["state"], "sweeping")
        self.assertFalse(resp["cleared"])
        self.assertEqual(resp["accepted"], "unknown")
        self.assertLessEqual(resp["sweep_tested"], resp["sweep_total"])
        self.assertGreater(resp["sweep_deadline_epoch"], 0)

    def test_unknown_individual_response(self):
        svc = self.service()
        rows = self.protected_rows([0])
        offer = svc.prepare_offer(rows)
        report = _report_access(offer, access="unknown")
        result = svc.record_access(report, rows)
        resp = _response_of(result)
        self.assertEqual(resp["state"], "individual")
        self.assertFalse(resp["cleared"])
        self.assertEqual(resp["accepted"], "unknown")
        self.assertEqual(resp["sweep_tested"], 0)
        self.assertEqual(resp["sweep_total"], 0)
        self.assertGreater(resp["sweep_deadline_epoch"], 0)


# -- threshold-based site-wide pause ------------------------------------------


class TestBlockThresholdPausesTheCrypter(OutcomeTestCase):
    """Enough blocks inside a live sweep window pause Filecrypt on their own.

    The complete-cohort trigger next to this one needs a complete, fully
    blocked, still-globally-possible cohort, which a real IP block never
    produces: it answers a mix of BLOCKED and UNKNOWN, every UNKNOWN clears
    `global_possible`, and the cohort is far too large to walk inside one
    window.  This trigger exists so the pause is reachable in exactly that
    situation, which is why none of those three conditions may be required.
    """

    def test_threshold_pauses_despite_unknown_and_incomplete_sweep(self):
        svc = self.service()
        rows = self.protected_rows(range(10))

        # One UNKNOWN member first - a FlareSolverr timeout looks like this -
        # which permanently clears `global_possible` for the whole sweep.
        unknown_offer = svc.prepare_offer(rows)
        svc.record_access(_report_access(unknown_offer, access="unknown"), rows)
        self.assertFalse(self.header()["global_possible"])

        result = None
        for i in range(DEFAULT_SWEEP_BLOCK_THRESHOLD):
            offer = svc.prepare_offer(rows)
            self.assertIsNotNone(offer, f"offer {i} was None")
            result = svc.record_blocked(_report_blocked(offer), rows)
            self.assertIsNotNone(result, f"report {i} was stale")

        # The sweep is deliberately still incomplete when it concludes.
        self.assertLess(result["sweep_tested"], result["sweep_total"])

        validate_defer_response(_response_of(result))
        self.assertEqual(result["instruction"], "cooldown")
        self.assertEqual(result["state"], "cooldown")
        self.assertEqual(result["hold_type"], "crypter_cooldown")

        hdr = self.header()
        self.assertEqual(hdr["state"], "cooldown")
        self.assertEqual(hdr["retry_after_epoch"], NOW + COOLDOWN_SECONDS)
        self.assertEqual(svc.decision()["state"], "cooldown")

        events = self.pending_events()
        self.assertEqual(events["observations"], DEFAULT_SWEEP_BLOCK_THRESHOLD)
        self.assertEqual(events["cooldowns"], 1)

    def test_paused_crypter_hands_out_no_ordinary_work(self):
        svc = self.service()
        rows = self.protected_rows(range(10))
        for _ in range(DEFAULT_SWEEP_BLOCK_THRESHOLD):
            offer = svc.prepare_offer(rows)
            svc.record_blocked(_report_blocked(offer), rows)

        self.assertEqual(self.header()["state"], "cooldown")
        # Nothing but a probe may be handed out while the crypter is paused,
        # and no probe was requested, so there is no work at all.
        self.assertIsNone(svc.prepare_offer(rows))

    def test_handouts_resume_after_retry_after_epoch(self):
        svc = self.service()
        rows = self.protected_rows(range(10))
        for _ in range(DEFAULT_SWEEP_BLOCK_THRESHOLD):
            offer = svc.prepare_offer(rows)
            svc.record_blocked(_report_blocked(offer), rows)

        retry_after = self.header()["retry_after_epoch"]
        self.clock.now = retry_after - 1
        self.assertIsNone(svc.prepare_offer(rows))

        # The pause is a deadline, not a dead end: once it passes the service
        # hands out ordinary work again, exactly as after the complete-cohort
        # cooldown.
        self.clock.now = retry_after
        resumed = svc.prepare_offer(rows)
        self.assertIsNotNone(resumed)
        self.assertNotEqual(resumed["mode"], "probe")

    def test_one_block_below_the_threshold_pauses_nothing(self):
        svc = self.service()
        rows = self.protected_rows(range(10))
        result = None
        for _ in range(DEFAULT_SWEEP_BLOCK_THRESHOLD - 1):
            offer = svc.prepare_offer(rows)
            result = svc.record_blocked(_report_blocked(offer), rows)

        self.assertEqual(result["instruction"], "hold")
        self.assertEqual(result["state"], "sweeping")
        self.assertEqual(self.header()["state"], "sweeping")
        self.assertEqual(self.pending_events()["cooldowns"], 0)
        # Ordinary sweep work continues.
        self.assertEqual(svc.prepare_offer(rows)["mode"], "sweep")

    def test_expired_window_pauses_nothing(self):
        svc = self.service()
        # A one-minute window expires well inside the 300 s offer lease, so the
        # last report below still arrives on a live lease but into a dead sweep.
        self.state.values["filecrypt_sweep_window_minutes"] = 1
        rows = self.protected_rows(range(10))

        offers = [svc.prepare_offer(rows) for _ in range(DEFAULT_SWEEP_BLOCK_THRESHOLD)]
        for offer in offers[:-1]:
            self.assertIsNotNone(svc.record_blocked(_report_blocked(offer), rows))

        deadline = self.header()["deadline_epoch"]
        self.clock.now = deadline
        result = svc.record_blocked(_report_blocked(offers[-1]), rows)

        self.assertIsNotNone(result)
        self.assertNotEqual(result["instruction"], "cooldown")
        self.assertNotEqual(self.header()["state"], "cooldown")
        self.assertEqual(self.pending_events()["cooldowns"], 0)

    def test_configured_threshold_is_read_from_settings(self):
        svc = self.service()
        self.state.values["filecrypt_sweep_block_threshold"] = 8
        rows = self.protected_rows(range(10))

        result = None
        for i in range(8):
            offer = svc.prepare_offer(rows)
            self.assertIsNotNone(offer, f"offer {i} was None")
            result = svc.record_blocked(_report_blocked(offer), rows)
            if i < 7:
                self.assertEqual(result["instruction"], "hold", f"paused at {i}")

        self.assertEqual(result["instruction"], "cooldown")
        self.assertEqual(self.header()["state"], "cooldown")

    def test_threshold_below_the_floor_is_clamped_up(self):
        svc = self.service()
        # A configured 2 would pause the crypter on evidence too thin for a
        # site-wide conclusion, so the established floor still applies.
        self.state.values["filecrypt_sweep_block_threshold"] = 2
        rows = self.protected_rows(range(10))

        result = None
        for _ in range(MINIMUM_GLOBAL_COOLDOWN_SIZE - 1):
            offer = svc.prepare_offer(rows)
            result = svc.record_blocked(_report_blocked(offer), rows)
        self.assertEqual(result["instruction"], "hold")

        offer = svc.prepare_offer(rows)
        result = svc.record_blocked(_report_blocked(offer), rows)
        self.assertEqual(result["instruction"], "cooldown")

    def test_unreadable_threshold_falls_back_to_the_default(self):
        svc = self.service()
        self.state.values["filecrypt_sweep_block_threshold"] = "not a number"
        rows = self.protected_rows(range(10))

        result = None
        for _ in range(DEFAULT_SWEEP_BLOCK_THRESHOLD):
            offer = svc.prepare_offer(rows)
            result = svc.record_blocked(_report_blocked(offer), rows)

        self.assertEqual(result["instruction"], "cooldown")


class TestCompleteCohortTriggerSurvives(OutcomeTestCase):
    """The original complete-cohort trigger still concludes a sweep by itself."""

    def test_complete_all_blocked_cohort_cools_below_the_threshold(self):
        svc = self.service()
        # Lifted far above the cohort size, so only the complete-cohort trigger
        # can possibly fire here.
        self.state.values["filecrypt_sweep_block_threshold"] = 1000
        rows = self.protected_rows(range(MINIMUM_GLOBAL_COOLDOWN_SIZE))

        result = None
        for _ in range(MINIMUM_GLOBAL_COOLDOWN_SIZE):
            offer = svc.prepare_offer(rows)
            result = svc.record_blocked(_report_blocked(offer), rows)

        validate_defer_response(_response_of(result))
        self.assertEqual(result["instruction"], "cooldown")
        self.assertEqual(result["state"], "cooldown")
        self.assertEqual(result["hold_type"], "crypter_cooldown")
        self.assertEqual(self.header()["state"], "cooldown")
        self.assertEqual(self.pending_events()["cooldowns"], 1)


if __name__ == "__main__":
    unittest.main()
