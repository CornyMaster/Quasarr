# -*- coding: utf-8 -*-
"""Tests for FilecryptLifecycleService retest and probe outcomes."""

import json
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
    encode_link_state,
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
    fp,
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


class RetestTestCase(unittest.TestCase):
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
        db = self.state.get_db("protected")
        for pkg_id, raw in rows:
            db.store(pkg_id, raw)
        return rows

    def prepare_first_time_blocked(self, fp_index=0, total=5):
        """Prepare and record a first-time BLOCKED, returning the offer."""
        svc = self.service()
        rows = self.protected_rows(range(total))
        offer = svc.prepare_offer(rows)
        self.assertIsNotNone(offer)
        report = _report_blocked(offer)
        result = svc.record_blocked(report, rows)
        self.assertIsNotNone(result)
        return offer, rows

    def advance_to_retest(self, offer, rows):
        """Advance clock past cooldown, prepare a retest offer."""
        self.clock.now = NOW + COOLDOWN_SECONDS + 1
        svc = self.service()
        retest_offer = svc.prepare_offer(rows)
        self.assertIsNotNone(retest_offer)
        self.assertEqual(retest_offer["mode"], "retest")
        self.assertEqual(retest_offer["link_fingerprint"], offer["link_fingerprint"])
        return retest_offer


# ── retest CLEAR ──────────────────────────────────────────────────────────────


class TestRetestClear(RetestTestCase):
    """RED: retest CLEAR deletes held, writes healthy header, receipt, wrapper."""

    def test_retest_clear_deletes_held_state(self):
        offer, rows = self.prepare_first_time_blocked()
        retest_offer = self.advance_to_retest(offer, rows)

        svc = self.service()
        report = _report_access(retest_offer, access="clear")
        result = svc.record_access(report, rows)

        self.assertIsNotNone(result)
        self.assertFalse(result["terminal_required"])
        self.assertTrue(result["cleared"])
        self.assertEqual(result["state"], "healthy")
        self.assertEqual(result["accepted"], "")
        self.assertEqual(result["sweep_tested"], 0)
        self.assertEqual(result["sweep_total"], 0)
        self.assertGreater(result["sweep_deadline_epoch"], 0)

        # Held state deleted
        ls = self.link_state(offer["link_fingerprint"])
        self.assertIsNone(ls)

    def test_retest_clear_writes_healthy_header(self):
        offer, rows = self.prepare_first_time_blocked()
        retest_offer = self.advance_to_retest(offer, rows)

        svc = self.service()
        report = _report_access(retest_offer, access="clear")
        svc.record_access(report, rows)

        hdr = self.header()
        self.assertIsNotNone(hdr)
        self.assertEqual(hdr["state"], "healthy")
        retest_now = NOW + COOLDOWN_SECONDS + 1
        self.assertEqual(hdr["until_epoch"], retest_now + WINDOW_SECONDS)

    def test_retest_clear_writes_receipt(self):
        offer, rows = self.prepare_first_time_blocked()
        retest_offer = self.advance_to_retest(offer, rows)

        svc = self.service()
        report = _report_access(retest_offer, access="clear")
        svc.record_access(report, rows)

        rcpt = self.receipt(retest_offer["offer_id"])
        self.assertIsNotNone(rcpt)
        self.assertEqual(rcpt["mode"], "retest")
        self.assertEqual(rcpt["outcome"], "clear")
        self.assertEqual(rcpt["fingerprint"], offer["link_fingerprint"])

    def test_retest_clear_no_counters(self):
        offer, rows = self.prepare_first_time_blocked()
        retest_offer = self.advance_to_retest(offer, rows)

        svc = self.service()
        report = _report_access(retest_offer, access="clear")
        svc.record_access(report, rows)

        events = self.pending_events()
        # Only the original first-time BLOCKED observation; retest adds nothing
        self.assertEqual(events["observations"], 1)
        self.assertEqual(events["cooldowns"], 0)
        self.assertEqual(events["probes"], 0)

    def test_retest_clear_replay_returns_stored_response(self):
        offer, rows = self.prepare_first_time_blocked()
        retest_offer = self.advance_to_retest(offer, rows)

        svc = self.service()
        report = _report_access(retest_offer, access="clear")
        result1 = svc.record_access(report, rows)

        # Delete held state and protected row to prove replay works from receipt
        self.ls_db().rows.pop(offer["link_fingerprint"], None)
        self.state.get_db("protected").rows.clear()

        result2 = svc.record_access(report, [])
        self.assertEqual(result1, result2)


# ── retest UNKNOWN ────────────────────────────────────────────────────────────


class TestRetestUnknown(RetestTestCase):
    """RED: retest UNKNOWN clears lease, preserves held, no header/counters."""

    def test_retest_unknown_clears_lease_preserves_held(self):
        offer, rows = self.prepare_first_time_blocked()
        retest_offer = self.advance_to_retest(offer, rows)

        svc = self.service()
        report = _report_access(retest_offer, access="unknown")
        result = svc.record_access(report, rows)

        self.assertIsNotNone(result)
        self.assertFalse(result["terminal_required"])
        self.assertFalse(result["cleared"])
        self.assertEqual(result["accepted"], "unknown")
        self.assertEqual(result["state"], "individual")
        self.assertEqual(result["sweep_tested"], 0)
        self.assertEqual(result["sweep_total"], 0)
        self.assertGreater(result["sweep_deadline_epoch"], 0)

        # Held state preserved with lease cleared
        ls = self.link_state(offer["link_fingerprint"])
        self.assertIsNotNone(ls)
        self.assertEqual(ls["state"], "held")
        self.assertIsNone(ls["lease"])
        # Original epochs preserved
        self.assertEqual(ls["first_blocked_epoch"], NOW)
        self.assertEqual(ls["retry_after_epoch"], NOW + COOLDOWN_SECONDS)

    def test_retest_unknown_no_header_change(self):
        offer, rows = self.prepare_first_time_blocked()
        header_before = self.header()
        retest_offer = self.advance_to_retest(offer, rows)

        svc = self.service()
        report = _report_access(retest_offer, access="unknown")
        svc.record_access(report, rows)

        header_after = self.header()
        self.assertEqual(header_before, header_after)

    def test_retest_unknown_writes_receipt(self):
        offer, rows = self.prepare_first_time_blocked()
        retest_offer = self.advance_to_retest(offer, rows)

        svc = self.service()
        report = _report_access(retest_offer, access="unknown")
        svc.record_access(report, rows)

        rcpt = self.receipt(retest_offer["offer_id"])
        self.assertIsNotNone(rcpt)
        self.assertEqual(rcpt["mode"], "retest")
        self.assertEqual(rcpt["outcome"], "unknown")

    def test_retest_unknown_no_counters(self):
        offer, rows = self.prepare_first_time_blocked()
        retest_offer = self.advance_to_retest(offer, rows)

        svc = self.service()
        report = _report_access(retest_offer, access="unknown")
        svc.record_access(report, rows)

        events = self.pending_events()
        self.assertEqual(events["observations"], 1)
        self.assertEqual(events["cooldowns"], 0)
        self.assertEqual(events["probes"], 0)

    def test_retest_unknown_then_next_prepare_gets_retest(self):
        offer, rows = self.prepare_first_time_blocked()
        retest_offer = self.advance_to_retest(offer, rows)

        svc = self.service()
        report = _report_access(retest_offer, access="unknown")
        svc.record_access(report, rows)

        # retry_after is in the past (NOW + COOLDOWN), clock is NOW + COOLDOWN + 1
        # Lease was cleared, so next prepare should issue a new retest
        next_offer = svc.prepare_offer(rows)
        self.assertIsNotNone(next_offer)
        self.assertEqual(next_offer["mode"], "retest")
        self.assertEqual(next_offer["link_fingerprint"], offer["link_fingerprint"])

    def test_retest_unknown_replay_returns_stored_response(self):
        offer, rows = self.prepare_first_time_blocked()
        retest_offer = self.advance_to_retest(offer, rows)

        svc = self.service()
        report = _report_access(retest_offer, access="unknown")
        result1 = svc.record_access(report, rows)

        # Delete protected to prove replay
        self.state.get_db("protected").rows.clear()
        result2 = svc.record_access(report, [])
        self.assertEqual(result1, result2)


# ── retest BLOCKED ────────────────────────────────────────────────────────────


class TestRetestBlocked(RetestTestCase):
    """RED: retest BLOCKED creates blacklisting, returns terminal transition."""

    def test_retest_blocked_creates_blacklisting(self):
        offer, rows = self.prepare_first_time_blocked()
        retest_offer = self.advance_to_retest(offer, rows)

        svc = self.service()
        report = _report_blocked(retest_offer)
        result = svc.record_blocked(report, rows)

        self.assertIsNotNone(result)
        self.assertTrue(result["terminal_required"])
        self.assertEqual(result["fingerprint"], offer["link_fingerprint"])
        self.assertEqual(result["package_id"], retest_offer["occurrence"].package_id)
        self.assertEqual(
            result["terminal_operation_id"],
            terminal_operation_id(retest_offer["occurrence"].package_id),
        )
        self.assertEqual(result["offer_id"], retest_offer["offer_id"])
        self.assertEqual(result["sweep_id"], retest_offer["sweep_id"])

        # Link state transitioned to blacklisting
        ls = self.link_state(offer["link_fingerprint"])
        self.assertIsNotNone(ls)
        self.assertEqual(ls["state"], "blacklisting")
        self.assertEqual(ls["first_blocked_epoch"], NOW)
        self.assertEqual(ls["recheck_sweep_id"], retest_offer["sweep_id"])
        self.assertEqual(ls["recheck_offer_id"], retest_offer["offer_id"])
        self.assertEqual(
            ls["recheck_package_id"], retest_offer["occurrence"].package_id
        )
        self.assertEqual(
            ls["terminal_operation_id"],
            terminal_operation_id(retest_offer["occurrence"].package_id),
        )

    def test_retest_blocked_no_receipt(self):
        offer, rows = self.prepare_first_time_blocked()
        retest_offer = self.advance_to_retest(offer, rows)

        svc = self.service()
        report = _report_blocked(retest_offer)
        svc.record_blocked(report, rows)

        rcpt = self.receipt(retest_offer["offer_id"])
        self.assertIsNone(rcpt)

    def test_retest_blocked_no_counters(self):
        offer, rows = self.prepare_first_time_blocked()
        retest_offer = self.advance_to_retest(offer, rows)

        svc = self.service()
        report = _report_blocked(retest_offer)
        svc.record_blocked(report, rows)

        events = self.pending_events()
        # Only the original first-time observation
        self.assertEqual(events["observations"], 1)
        self.assertEqual(events["cooldowns"], 0)
        self.assertEqual(events["probes"], 0)

    def test_retest_blocked_header_unchanged(self):
        offer, rows = self.prepare_first_time_blocked()
        header_before = self.header()
        retest_offer = self.advance_to_retest(offer, rows)

        svc = self.service()
        report = _report_blocked(retest_offer)
        svc.record_blocked(report, rows)

        header_after = self.header()
        self.assertEqual(header_before, header_after)

    def test_retest_blocked_member_unchanged(self):
        offer, rows = self.prepare_first_time_blocked()
        member_before = self.member(offer["link_fingerprint"])
        retest_offer = self.advance_to_retest(offer, rows)

        svc = self.service()
        report = _report_blocked(retest_offer)
        svc.record_blocked(report, rows)

        member_after = self.member(offer["link_fingerprint"])
        self.assertEqual(member_before, member_after)

    def test_retest_blocked_replay_after_protected_deletion(self):
        offer, rows = self.prepare_first_time_blocked()
        retest_offer = self.advance_to_retest(offer, rows)

        svc = self.service()
        report = _report_blocked(retest_offer)
        result1 = svc.record_blocked(report, rows)

        # Delete protected row
        self.state.get_db("protected").rows.clear()
        result2 = svc.record_blocked(report, [])
        self.assertEqual(result1, result2)

    def test_conflicting_identity_against_blacklisting_stale(self):
        offer, rows = self.prepare_first_time_blocked()
        retest_offer = self.advance_to_retest(offer, rows)

        svc = self.service()
        report = _report_blocked(retest_offer)
        svc.record_blocked(report, rows)

        # Now try a different sweep/offer against the same blacklisting state
        fake_report = dict(report)
        fake_report["sweep_id"] = "f" * 32
        fake_report["offer_id"] = "e" * 32
        result = svc.record_blocked(fake_report, rows)
        self.assertIsNone(result)


# ── retest stale conditions ───────────────────────────────────────────────────


class TestRetestStale(RetestTestCase):
    """RED: stale conditions for retest outcomes."""

    def test_expired_lease_stale(self):
        offer, rows = self.prepare_first_time_blocked()
        retest_offer = self.advance_to_retest(offer, rows)

        # Advance past the lease expiry
        self.clock.now = NOW + COOLDOWN_SECONDS + 1 + OFFER_LEASE_SECONDS + 1

        svc = self.service()
        report = _report_blocked(retest_offer)
        result = svc.record_blocked(report, rows)
        self.assertIsNone(result)

    def test_active_hold_without_cooldown_stale_for_probe(self):
        """Hold not yet expired + no cooldown → not probe, not retest → stale."""
        offer, rows = self.prepare_first_time_blocked()
        # Clock is still within hold period, no cooldown header present
        # Try to report using the held state's lease (would need a probe offer)
        # Actually: only valid leases from prepare_offer are reportable
        # A fabricated report against the held state without proper mode → stale
        svc = self.service()
        # Advance clock just past first-time but still within hold
        self.clock.now = NOW + 100
        # Fabricate a report with wrong sweep/offer that doesn't match
        report = {
            "package_id": offer["occurrence"].package_id,
            "crypter": "filecrypt",
            "reason_code": "ip_block_suspected",
            "link_fingerprint": offer["link_fingerprint"],
            "sweep_id": "a" * 32,
            "offer_id": "b" * 32,
            "protocol_version": 2,
            "terminal_operation_id": terminal_operation_id(
                offer["occurrence"].package_id
            ),
        }
        result = svc.record_blocked(report, rows)
        self.assertIsNone(result)

    def test_changed_owner_stale(self):
        offer, rows = self.prepare_first_time_blocked()
        retest_offer = self.advance_to_retest(offer, rows)

        # Remove ownership: clear the protected DB for this package
        pkg_id = retest_offer["occurrence"].package_id
        self.state.get_db("protected").rows.pop(pkg_id, None)

        svc = self.service()
        report = _report_blocked(retest_offer)
        result = svc.record_blocked(report, [])
        self.assertIsNone(result)


# ── probe BLOCKED ─────────────────────────────────────────────────────────────


class TestProbeBlocked(RetestTestCase):
    """RED: probe BLOCKED keeps held, cooldown response, probes+1."""

    def _setup_probe(self):
        """Set up a probe scenario: 5 all blocked → cooldown, then probe."""
        svc = self.service()
        rows = self.protected_rows(range(5))
        offers = []
        for i in range(5):
            offer = svc.prepare_offer(rows)
            self.assertIsNotNone(offer, f"offer {i}")
            offers.append(offer)
            report = _report_blocked(offer)
            svc.record_blocked(report, rows)

        # Now in cooldown state; prepare a probe
        probe_offer = svc.prepare_offer(
            rows, preferred_fingerprint=offers[0]["link_fingerprint"]
        )
        self.assertIsNotNone(probe_offer)
        self.assertEqual(probe_offer["mode"], "probe")
        return offers, rows, probe_offer

    def test_probe_blocked_keeps_held_no_terminal(self):
        offers, rows, probe_offer = self._setup_probe()

        svc = self.service()
        report = _report_blocked(probe_offer)
        result = svc.record_blocked(report, rows)

        self.assertIsNotNone(result)
        self.assertFalse(result["terminal_required"])
        validate_defer_response(_response_of(result))
        self.assertEqual(result["instruction"], "cooldown")
        self.assertEqual(result["state"], "cooldown")
        self.assertEqual(result["hold_type"], "crypter_cooldown")
        self.assertEqual(result["evidence_count"], 1)
        self.assertEqual(result["sweep_tested"], 0)
        self.assertEqual(result["sweep_total"], 0)
        self.assertGreater(result["retry_after_epoch"], 0)
        self.assertGreater(result["sweep_deadline_epoch"], 0)

        # Held state preserved with lease cleared
        ls = self.link_state(probe_offer["link_fingerprint"])
        self.assertIsNotNone(ls)
        self.assertEqual(ls["state"], "held")
        self.assertIsNone(ls["lease"])
        # Original epochs preserved
        self.assertEqual(ls["first_blocked_epoch"], NOW)

    def test_probe_blocked_probes_incremented(self):
        offers, rows, probe_offer = self._setup_probe()

        svc = self.service()
        report = _report_blocked(probe_offer)
        svc.record_blocked(report, rows)

        events = self.pending_events()
        self.assertEqual(events["probes"], 1)
        # Observations from the 5 first-time BLOCKEDs
        self.assertEqual(events["observations"], 5)

    def test_probe_blocked_no_blacklisting(self):
        offers, rows, probe_offer = self._setup_probe()

        svc = self.service()
        report = _report_blocked(probe_offer)
        svc.record_blocked(report, rows)

        ls = self.link_state(probe_offer["link_fingerprint"])
        self.assertNotEqual(ls["state"], "blacklisting")

    def test_probe_blocked_header_unchanged(self):
        offers, rows, probe_offer = self._setup_probe()
        header_before = self.header()

        svc = self.service()
        report = _report_blocked(probe_offer)
        svc.record_blocked(report, rows)

        header_after = self.header()
        self.assertEqual(header_before, header_after)

    def test_probe_blocked_writes_receipt(self):
        offers, rows, probe_offer = self._setup_probe()

        svc = self.service()
        report = _report_blocked(probe_offer)
        svc.record_blocked(report, rows)

        rcpt = self.receipt(probe_offer["offer_id"])
        self.assertIsNotNone(rcpt)
        self.assertEqual(rcpt["mode"], "probe")
        self.assertEqual(rcpt["outcome"], "blocked")

    def test_probe_blocked_replay_no_second_increment(self):
        offers, rows, probe_offer = self._setup_probe()

        svc = self.service()
        report = _report_blocked(probe_offer)
        result1 = svc.record_blocked(report, rows)
        result2 = svc.record_blocked(report, rows)
        self.assertEqual(result1, result2)

        events = self.pending_events()
        self.assertEqual(events["probes"], 1)

    def test_probe_blocked_no_observations_cooldowns(self):
        offers, rows, probe_offer = self._setup_probe()
        events_before = self.pending_events()

        svc = self.service()
        report = _report_blocked(probe_offer)
        svc.record_blocked(report, rows)

        events_after = self.pending_events()
        # Only probes changed
        self.assertEqual(events_after["observations"], events_before["observations"])
        self.assertEqual(events_after["cooldowns"], events_before["cooldowns"])


# ── probe CLEAR ───────────────────────────────────────────────────────────────


class TestProbeClear(RetestTestCase):
    """RED: probe CLEAR deletes probed hold, header healthy, probes+1."""

    def _setup_probe(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offers = []
        for i in range(5):
            offer = svc.prepare_offer(rows)
            self.assertIsNotNone(offer, f"offer {i}")
            offers.append(offer)
            report = _report_blocked(offer)
            svc.record_blocked(report, rows)

        probe_offer = svc.prepare_offer(
            rows, preferred_fingerprint=offers[0]["link_fingerprint"]
        )
        self.assertIsNotNone(probe_offer)
        self.assertEqual(probe_offer["mode"], "probe")
        return offers, rows, probe_offer

    def test_probe_clear_deletes_probed_hold(self):
        offers, rows, probe_offer = self._setup_probe()

        svc = self.service()
        report = _report_access(probe_offer, access="clear")
        result = svc.record_access(report, rows)

        self.assertIsNotNone(result)
        self.assertFalse(result["terminal_required"])
        self.assertTrue(result["cleared"])
        self.assertEqual(result["state"], "healthy")
        self.assertEqual(result["sweep_tested"], 0)
        self.assertEqual(result["sweep_total"], 0)
        self.assertGreater(result["sweep_deadline_epoch"], 0)

        # Probed hold deleted
        ls = self.link_state(probe_offer["link_fingerprint"])
        self.assertIsNone(ls)

    def test_probe_clear_unrelated_hold_remains(self):
        offers, rows, probe_offer = self._setup_probe()

        svc = self.service()
        report = _report_access(probe_offer, access="clear")
        svc.record_access(report, rows)

        # Other held states remain
        for offer in offers[1:]:
            ls = self.link_state(offer["link_fingerprint"])
            self.assertIsNotNone(ls)
            self.assertEqual(ls["state"], "held")

    def test_probe_clear_header_becomes_healthy(self):
        offers, rows, probe_offer = self._setup_probe()

        svc = self.service()
        report = _report_access(probe_offer, access="clear")
        svc.record_access(report, rows)

        hdr = self.header()
        self.assertIsNotNone(hdr)
        self.assertEqual(hdr["state"], "healthy")
        self.assertEqual(hdr["until_epoch"], NOW + WINDOW_SECONDS)

    def test_probe_clear_probes_incremented(self):
        offers, rows, probe_offer = self._setup_probe()

        svc = self.service()
        report = _report_access(probe_offer, access="clear")
        svc.record_access(report, rows)

        events = self.pending_events()
        self.assertEqual(events["probes"], 1)

    def test_probe_clear_writes_receipt(self):
        offers, rows, probe_offer = self._setup_probe()

        svc = self.service()
        report = _report_access(probe_offer, access="clear")
        svc.record_access(report, rows)

        rcpt = self.receipt(probe_offer["offer_id"])
        self.assertIsNotNone(rcpt)
        self.assertEqual(rcpt["mode"], "probe")
        self.assertEqual(rcpt["outcome"], "clear")

    def test_probe_clear_replay_no_second_increment(self):
        offers, rows, probe_offer = self._setup_probe()

        svc = self.service()
        report = _report_access(probe_offer, access="clear")
        result1 = svc.record_access(report, rows)
        result2 = svc.record_access(report, rows)
        self.assertEqual(result1, result2)

        events = self.pending_events()
        self.assertEqual(events["probes"], 1)


# ── probe UNKNOWN ─────────────────────────────────────────────────────────────


class TestProbeUnknown(RetestTestCase):
    """RED: probe UNKNOWN clears lease, cooldown/unknown response, probes+1."""

    def _setup_probe(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offers = []
        for i in range(5):
            offer = svc.prepare_offer(rows)
            self.assertIsNotNone(offer, f"offer {i}")
            offers.append(offer)
            report = _report_blocked(offer)
            svc.record_blocked(report, rows)

        probe_offer = svc.prepare_offer(
            rows, preferred_fingerprint=offers[0]["link_fingerprint"]
        )
        self.assertIsNotNone(probe_offer)
        self.assertEqual(probe_offer["mode"], "probe")
        return offers, rows, probe_offer

    def test_probe_unknown_clears_lease_keeps_held(self):
        offers, rows, probe_offer = self._setup_probe()

        svc = self.service()
        report = _report_access(probe_offer, access="unknown")
        result = svc.record_access(report, rows)

        self.assertIsNotNone(result)
        self.assertFalse(result["terminal_required"])
        self.assertFalse(result["cleared"])
        self.assertEqual(result["accepted"], "unknown")
        self.assertEqual(result["state"], "cooldown")
        self.assertEqual(result["sweep_tested"], 0)
        self.assertEqual(result["sweep_total"], 0)
        self.assertGreater(result["sweep_deadline_epoch"], 0)

        # Held state preserved with lease cleared
        ls = self.link_state(probe_offer["link_fingerprint"])
        self.assertIsNotNone(ls)
        self.assertEqual(ls["state"], "held")
        self.assertIsNone(ls["lease"])

    def test_probe_unknown_header_cooldown_unchanged(self):
        offers, rows, probe_offer = self._setup_probe()
        header_before = self.header()

        svc = self.service()
        report = _report_access(probe_offer, access="unknown")
        svc.record_access(report, rows)

        header_after = self.header()
        self.assertEqual(header_before, header_after)

    def test_probe_unknown_probes_incremented(self):
        offers, rows, probe_offer = self._setup_probe()

        svc = self.service()
        report = _report_access(probe_offer, access="unknown")
        svc.record_access(report, rows)

        events = self.pending_events()
        self.assertEqual(events["probes"], 1)

    def test_probe_unknown_writes_receipt(self):
        offers, rows, probe_offer = self._setup_probe()

        svc = self.service()
        report = _report_access(probe_offer, access="unknown")
        svc.record_access(report, rows)

        rcpt = self.receipt(probe_offer["offer_id"])
        self.assertIsNotNone(rcpt)
        self.assertEqual(rcpt["mode"], "probe")
        self.assertEqual(rcpt["outcome"], "unknown")

    def test_probe_unknown_replay_no_second_increment(self):
        offers, rows, probe_offer = self._setup_probe()

        svc = self.service()
        report = _report_access(probe_offer, access="unknown")
        result1 = svc.record_access(report, rows)
        result2 = svc.record_access(report, rows)
        self.assertEqual(result1, result2)

        events = self.pending_events()
        self.assertEqual(events["probes"], 1)

    def test_probe_unknown_no_observations_cooldowns(self):
        offers, rows, probe_offer = self._setup_probe()
        events_before = self.pending_events()

        svc = self.service()
        report = _report_access(probe_offer, access="unknown")
        svc.record_access(report, rows)

        events_after = self.pending_events()
        self.assertEqual(events_after["observations"], events_before["observations"])
        self.assertEqual(events_after["cooldowns"], events_before["cooldowns"])


# ── probe stale conditions ────────────────────────────────────────────────────


class TestProbeStale(RetestTestCase):
    """RED: stale conditions for probe outcomes."""

    def test_expired_cooldown_stale(self):
        """Once cooldown expires, probe offer is stale."""
        svc = self.service()
        rows = self.protected_rows(range(5))
        offers = []
        for _i in range(5):
            offer = svc.prepare_offer(rows)
            offers.append(offer)
            report = _report_blocked(offer)
            svc.record_blocked(report, rows)

        probe_offer = svc.prepare_offer(
            rows, preferred_fingerprint=offers[0]["link_fingerprint"]
        )
        self.assertIsNotNone(probe_offer)

        # Advance past cooldown
        self.clock.now = NOW + COOLDOWN_SECONDS + 1
        report = _report_blocked(probe_offer)
        result = svc.record_blocked(report, rows)
        # Should be stale because cooldown expired and hold expired → mode is retest not probe
        # But lease was for probe... actual mode derivation would see expired retry
        # and no cooldown → retest mode, but the lease was from probe time
        # The lease matches but mode derivation gives retest not probe
        # Actually, the held retry_after_epoch is NOW + COOLDOWN, clock is NOW + COOLDOWN + 1
        # so now >= retry_after_epoch. And cooldown expired (no live cooldown).
        # So it would try retest. But the offer lease still matches the report.
        # Retest BLOCKED should work here. Let me verify the semantics...
        # Actually the hold's retry_after is past, so it qualifies as retest.
        # The probe's offer_expires_epoch is NOW + OFFER_LEASE_SECONDS which is past too.
        # So the lease is expired → stale.
        self.assertIsNone(result)

    def test_wrong_preferred_lease_stale(self):
        """Report with mismatched offer against held state → stale."""
        svc = self.service()
        rows = self.protected_rows(range(5))
        offers = []
        for _i in range(5):
            offer = svc.prepare_offer(rows)
            offers.append(offer)
            report = _report_blocked(offer)
            svc.record_blocked(report, rows)

        probe_offer = svc.prepare_offer(
            rows, preferred_fingerprint=offers[0]["link_fingerprint"]
        )
        self.assertIsNotNone(probe_offer)

        # Fabricate a report with wrong offer_id
        report = _report_blocked(probe_offer)
        report["offer_id"] = "f" * 32
        result = svc.record_blocked(report, rows)
        self.assertIsNone(result)

    def test_probe_never_creates_blacklisting(self):
        """Probe BLOCKED never transitions to blacklisting."""
        svc = self.service()
        rows = self.protected_rows(range(5))
        offers = []
        for _i in range(5):
            offer = svc.prepare_offer(rows)
            offers.append(offer)
            report = _report_blocked(offer)
            svc.record_blocked(report, rows)

        probe_offer = svc.prepare_offer(
            rows, preferred_fingerprint=offers[0]["link_fingerprint"]
        )
        svc = self.service()
        report = _report_blocked(probe_offer)
        svc.record_blocked(report, rows)

        # Verify no blacklisting state was created for any fingerprint
        for offer in offers:
            ls = self.link_state(offer["link_fingerprint"])
            if ls is not None:
                self.assertNotEqual(ls["state"], "blacklisting")


# ── callback six targets ──────────────────────────────────────────────────────


class TestRetestCallbackTargets(RetestTestCase):
    """RED: retest/probe callback still uses six targets."""

    def test_retest_blocked_six_targets(self):
        offer, rows = self.prepare_first_time_blocked()
        retest_offer = self.advance_to_retest(offer, rows)

        calls = []
        original_mutate = self.sweep_db().mutate_values

        def tracking_mutate(targets, mutator):
            calls.append(targets)
            return original_mutate(targets, mutator)

        self.sweep_db().mutate_values = tracking_mutate

        svc = self.service()
        report = _report_blocked(retest_offer)
        svc.record_blocked(report, rows)

        outcome_calls = [c for c in calls if len(c) == 6]
        self.assertEqual(len(outcome_calls), 1)

    def test_probe_clear_six_targets(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offers = []
        for _i in range(5):
            offer = svc.prepare_offer(rows)
            offers.append(offer)
            report = _report_blocked(offer)
            svc.record_blocked(report, rows)

        probe_offer = svc.prepare_offer(
            rows, preferred_fingerprint=offers[0]["link_fingerprint"]
        )

        calls = []
        original_mutate = self.sweep_db().mutate_values

        def tracking_mutate(targets, mutator):
            calls.append(targets)
            return original_mutate(targets, mutator)

        self.sweep_db().mutate_values = tracking_mutate

        svc = self.service()
        report = _report_access(probe_offer, access="clear")
        svc.record_access(report, rows)

        outcome_calls = [c for c in calls if len(c) == 6]
        self.assertEqual(len(outcome_calls), 1)


# ── privacy ───────────────────────────────────────────────────────────────────


class TestRetestPrivacy(RetestTestCase):
    """RED: receipts/blacklisting contain no URL/title/reason/error text."""

    def test_retest_blocked_no_url_in_link_state(self):
        offer, rows = self.prepare_first_time_blocked()
        retest_offer = self.advance_to_retest(offer, rows)

        svc = self.service()
        report = _report_blocked(retest_offer)
        svc.record_blocked(report, rows)

        raw = self.ls_db().retrieve(offer["link_fingerprint"])
        self.assertNotIn("http", raw)
        self.assertNotIn("title", raw)
        self.assertNotIn("reason", raw)
        self.assertNotIn("error", raw)

    def test_probe_receipt_no_url(self):
        svc = self.service()
        rows = self.protected_rows(range(5))
        offers = []
        for _i in range(5):
            offer = svc.prepare_offer(rows)
            offers.append(offer)
            report = _report_blocked(offer)
            svc.record_blocked(report, rows)

        probe_offer = svc.prepare_offer(
            rows, preferred_fingerprint=offers[0]["link_fingerprint"]
        )
        svc = self.service()
        report = _report_blocked(probe_offer)
        svc.record_blocked(report, rows)

        raw = self.receipts_db().retrieve(probe_offer["offer_id"])
        self.assertNotIn("http", raw)
        self.assertNotIn("title", raw)
        self.assertNotIn("reason", raw)
        self.assertNotIn("error", raw)


# ── access response validator extension ───────────────────────────────────────


class TestAccessResponseCooldownState(RetestTestCase):
    """RED: validate_access_response allows cooldown for cleared=False."""

    def test_cooldown_state_valid_for_unknown(self):
        resp = {
            "state": "cooldown",
            "cleared": False,
            "accepted": "unknown",
            "sweep_id": "a" * 32,
            "sweep_tested": 0,
            "sweep_total": 0,
            "sweep_deadline_epoch": 1000,
        }
        validate_access_response(resp)

    def test_cooldown_state_invalid_for_cleared(self):
        resp = {
            "state": "cooldown",
            "cleared": True,
            "accepted": "",
            "sweep_id": "a" * 32,
            "sweep_tested": 0,
            "sweep_total": 0,
            "sweep_deadline_epoch": 1000,
        }
        with self.assertRaises(ValueError):
            validate_access_response(resp)


# ── malformed cooldown deadline regression ────────────────────────────────────


class TestMalformedCooldownDeadlineProbe(RetestTestCase):
    """Regression: cooldown header with sweep_deadline_epoch=0 must be rejected.

    After the lifecycle codec fix, decode_sweep_header returns None for any
    cooldown header carrying a zero epoch, so is_live_cooldown is False and
    both probe paths fall to the stale branch: None, no exception, no writes.
    """

    def _setup_malformed_probe(self):
        """5 normal blocked to enter cooldown, then corrupt sweep_deadline_epoch=0."""
        svc = self.service()
        rows = self.protected_rows(range(5))
        for i in range(5):
            offer = svc.prepare_offer(rows)
            self.assertIsNotNone(offer, f"offer {i}")
            svc.record_blocked(_report_blocked(offer), rows)

        hdr_raw = self.sweep_db().retrieve(FILECRYPT_SWEEP_KEY)
        hdr = decode_sweep_header(hdr_raw)
        self.assertIsNotNone(hdr, "cooldown header expected after 5 blocked")
        self.assertEqual(hdr["state"], "cooldown")

        generation_id = hdr["generation_id"]
        fingerprint = fp(0)
        pkg_id = rows[0][0]

        # Bypass encoder to install a cooldown header with sweep_deadline_epoch=0.
        malformed_raw = json.dumps(
            dict(hdr, sweep_deadline_epoch=0),
            separators=(",", ":"),
            sort_keys=True,
        )
        self.sweep_db().store(FILECRYPT_SWEEP_KEY, malformed_raw)

        # Install an active probe lease directly on the held link state.
        offer_id = "f" * 32
        ls_raw = self.ls_db().retrieve(fingerprint)
        ls = decode_link_state(ls_raw)
        self.assertIsNotNone(ls)
        self.assertEqual(ls["state"], "held")
        probe_ls = dict(
            ls,
            lease={
                "sweep_id": generation_id,
                "offer_id": offer_id,
                "package_id": pkg_id,
                "offer_expires_epoch": self.clock.now + OFFER_LEASE_SECONDS,
            },
        )
        self.ls_db().store(fingerprint, encode_link_state(probe_ls))

        blocked_report = {
            "package_id": pkg_id,
            "crypter": "filecrypt",
            "reason_code": "ip_block_suspected",
            "link_fingerprint": fingerprint,
            "sweep_id": generation_id,
            "offer_id": offer_id,
            "protocol_version": 2,
            "terminal_operation_id": terminal_operation_id(pkg_id),
        }
        access_report = {
            "package_id": pkg_id,
            "crypter": "filecrypt",
            "access": "unknown",
            "link_fingerprint": fingerprint,
            "sweep_id": generation_id,
            "offer_id": offer_id,
            "protocol_version": 2,
            "terminal_operation_id": terminal_operation_id(pkg_id),
        }
        return rows, fingerprint, offer_id, blocked_report, access_report

    def test_probe_blocked_zero_deadline_returns_none(self):
        rows, fingerprint, offer_id, blocked_report, _ = self._setup_malformed_probe()
        pkg_id = rows[0][0]

        # Snapshot all six before the call
        hdr_before = self.sweep_db().retrieve(FILECRYPT_SWEEP_KEY)
        member_before = self.members_db().retrieve(fingerprint)
        ls_before = self.ls_db().retrieve(fingerprint)
        receipt_before = self.receipts_db().retrieve(offer_id)
        protected_before = self.state.get_db("protected").retrieve(pkg_id)
        events_before = self.events_db().retrieve(CRYPTER_EVENT_KEY)

        result = self.service().record_blocked(blocked_report, rows)

        self.assertIsNone(result)
        # Verify all six are byte-identical after failed probe
        self.assertEqual(self.sweep_db().retrieve(FILECRYPT_SWEEP_KEY), hdr_before)
        self.assertEqual(self.members_db().retrieve(fingerprint), member_before)
        self.assertEqual(self.ls_db().retrieve(fingerprint), ls_before)
        self.assertEqual(self.receipts_db().retrieve(offer_id), receipt_before)
        self.assertEqual(
            self.state.get_db("protected").retrieve(pkg_id), protected_before
        )
        self.assertEqual(self.events_db().retrieve(CRYPTER_EVENT_KEY), events_before)

    def test_probe_unknown_zero_deadline_returns_none(self):
        rows, fingerprint, offer_id, _, access_report = self._setup_malformed_probe()
        pkg_id = rows[0][0]

        # Snapshot all six before the call
        hdr_before = self.sweep_db().retrieve(FILECRYPT_SWEEP_KEY)
        member_before = self.members_db().retrieve(fingerprint)
        ls_before = self.ls_db().retrieve(fingerprint)
        receipt_before = self.receipts_db().retrieve(offer_id)
        protected_before = self.state.get_db("protected").retrieve(pkg_id)
        events_before = self.events_db().retrieve(CRYPTER_EVENT_KEY)

        result = self.service().record_access(access_report, rows)

        self.assertIsNone(result)
        # Verify all six are byte-identical after failed probe
        self.assertEqual(self.sweep_db().retrieve(FILECRYPT_SWEEP_KEY), hdr_before)
        self.assertEqual(self.members_db().retrieve(fingerprint), member_before)
        self.assertEqual(self.ls_db().retrieve(fingerprint), ls_before)
        self.assertEqual(self.receipts_db().retrieve(offer_id), receipt_before)
        self.assertEqual(
            self.state.get_db("protected").retrieve(pkg_id), protected_before
        )
        self.assertEqual(self.events_db().retrieve(CRYPTER_EVENT_KEY), events_before)


if __name__ == "__main__":
    unittest.main()
