# -*- coding: utf-8 -*-
"""Tests for FilecryptLifecycleService.confirm_blacklist (Task 3B2B)."""

import json
import unittest

from quasarr.providers.filecrypt_lifecycle import (
    FILECRYPT_LINK_STATES_TABLE,
    FILECRYPT_OFFER_RECEIPTS_TABLE,
    decode_link_state,
    decode_offer_receipt,
    encode_link_state,
    encode_offer_receipt,
)
from quasarr.providers.filecrypt_lifecycle_decisions import (
    BLACKLIST_RESPONSE_KEYS,
    RECEIPT_RETENTION_SECONDS,
    build_blacklist_decision,
    validate_blacklist_response,
)
from quasarr.providers.filecrypt_lifecycle_service import (
    FilecryptLifecycleService,
)
from quasarr.providers.terminal_operations import terminal_operation_id
from tests.test_filecrypt_lifecycle_service import (
    NOW,
    AtomicSharedState,
    FakeClock,
    SequentialIds,
    fp,
    pkg,
)

WINDOW = 15 * 60

FP0 = fp(0)
PKG0 = pkg(0)
TOP0 = terminal_operation_id(PKG0)
SWEEP_ID = "a" * 32
OFFER_ID = "b" * 32
FIRST_BLOCKED = NOW - 86400


def _make_blacklisting(
    *,
    fp_val=FP0,
    offer_id=OFFER_ID,
    pkg_id=PKG0,
    sweep_id=SWEEP_ID,
    top_id=TOP0,
    first_blocked=FIRST_BLOCKED,
):
    return {
        "schema_version": 1,
        "state": "blacklisting",
        "first_blocked_epoch": first_blocked,
        "recheck_offer_id": offer_id,
        "recheck_package_id": pkg_id,
        "recheck_sweep_id": sweep_id,
        "terminal_operation_id": top_id,
    }


class BlacklistConfirmationTestCase(unittest.TestCase):
    """Base with clock, ids, shared state, and service helpers."""

    def setUp(self):
        self.clock = FakeClock(NOW)
        self.ids = SequentialIds()
        self.state = AtomicSharedState()

    def service(self):
        return FilecryptLifecycleService(
            self.state, clock=self.clock, identifier_factory=self.ids
        )

    def ls_db(self):
        return self.state.get_db(FILECRYPT_LINK_STATES_TABLE)

    def receipts_db(self):
        return self.state.get_db(FILECRYPT_OFFER_RECEIPTS_TABLE)

    def link_state(self, fp_val=FP0):
        raw = self.ls_db().retrieve(fp_val)
        return None if raw is None else decode_link_state(raw)

    def receipt(self, offer_id=OFFER_ID):
        raw = self.receipts_db().retrieve(offer_id)
        return None if raw is None else decode_offer_receipt(raw)

    def install_blacklisting(self, record=None):
        record = record or _make_blacklisting()
        self.ls_db().update_store(FP0, encode_link_state(record))

    def confirm(self, fp_val=FP0, offer_id=OFFER_ID, top_id=TOP0):
        return self.service().confirm_blacklist(fp_val, offer_id, top_id)


# ── fresh confirmation ────────────────────────────────────────────────────────


class TestFreshConfirmation(BlacklistConfirmationTestCase):
    """Fresh exact blacklisting row produces blacklisted + receipt + wrapper."""

    def test_fresh_blacklisting_returns_wrapper(self):
        self.install_blacklisting()
        result = self.confirm()
        self.assertIsNotNone(result)
        self.assertEqual(result["instruction"], "blacklist")
        self.assertEqual(result["terminal_required"], False)
        self.assertEqual(result["fingerprint"], FP0)
        self.assertEqual(result["package_id"], PKG0)
        self.assertEqual(result["terminal_operation_id"], TOP0)

    def test_fresh_confirmation_transitions_link_to_blacklisted(self):
        self.install_blacklisting()
        self.confirm()
        ls = self.link_state()
        self.assertIsNotNone(ls)
        self.assertEqual(ls["state"], "blacklisted")

    def test_preserves_first_blocked_epoch(self):
        self.install_blacklisting()
        self.confirm()
        ls = self.link_state()
        self.assertEqual(ls["first_blocked_epoch"], FIRST_BLOCKED)

    def test_sets_blacklisted_epoch_to_confirmation_clock(self):
        self.clock.now = NOW + 500
        self.install_blacklisting()
        self.confirm()
        ls = self.link_state()
        self.assertEqual(ls["blacklisted_epoch"], NOW + 500)

    def test_writes_receipt(self):
        self.install_blacklisting()
        self.confirm()
        rcpt = self.receipt()
        self.assertIsNotNone(rcpt)

    def test_receipt_carries_stored_package_id(self):
        self.install_blacklisting()
        self.confirm()
        self.assertEqual(self.receipt()["package_id"], PKG0)

    def test_receipt_carries_stored_sweep_id_as_generation(self):
        self.install_blacklisting()
        self.confirm()
        self.assertEqual(self.receipt()["generation_id"], SWEEP_ID)

    def test_receipt_carries_fingerprint(self):
        self.install_blacklisting()
        self.confirm()
        self.assertEqual(self.receipt()["fingerprint"], FP0)

    def test_receipt_mode_is_retest(self):
        self.install_blacklisting()
        self.confirm()
        self.assertEqual(self.receipt()["mode"], "retest")

    def test_receipt_outcome_is_blocked(self):
        self.install_blacklisting()
        self.confirm()
        self.assertEqual(self.receipt()["outcome"], "blocked")

    def test_receipt_response_passes_blacklist_validator(self):
        self.install_blacklisting()
        self.confirm()
        resp = self.receipt()["response"]
        validate_blacklist_response(resp)  # must not raise

    def test_receipt_response_sweep_id_matches_recheck_sweep_id(self):
        self.install_blacklisting()
        self.confirm()
        self.assertEqual(self.receipt()["response"]["sweep_id"], SWEEP_ID)

    def test_receipt_response_deadline_is_now_plus_window(self):
        self.install_blacklisting()
        self.confirm()
        self.assertEqual(
            self.receipt()["response"]["sweep_deadline_epoch"], NOW + WINDOW
        )

    def test_wrapper_response_fields_match_exact_blacklist_shape(self):
        self.install_blacklisting()
        result = self.confirm()
        response_keys = BLACKLIST_RESPONSE_KEYS | {
            "terminal_required",
            "fingerprint",
            "package_id",
            "terminal_operation_id",
        }
        self.assertEqual(set(result.keys()), response_keys)

    def test_wrapper_blacklist_fields_equal_receipt_response(self):
        self.install_blacklisting()
        result = self.confirm()
        rcpt_resp = self.receipt()["response"]
        for k in BLACKLIST_RESPONSE_KEYS:
            self.assertEqual(result[k], rcpt_resp[k])


# ── replay ────────────────────────────────────────────────────────────────────


class TestExactReplay(BlacklistConfirmationTestCase):
    """Exact replay after link-state is blacklisted returns identical wrapper."""

    def _first_result(self):
        self.install_blacklisting()
        return self.confirm()

    def test_replay_after_blacklisted_returns_nonnull(self):
        self._first_result()
        result = self.confirm()
        self.assertIsNotNone(result)

    def test_replay_equals_first_result(self):
        first = self._first_result()
        second = self.confirm()
        self.assertEqual(first, second)

    def test_replay_does_not_change_link_state(self):
        self._first_result()
        ls_after_first = decode_link_state(self.ls_db().retrieve(FP0))
        self.confirm()
        ls_after_second = decode_link_state(self.ls_db().retrieve(FP0))
        self.assertEqual(
            json.dumps(ls_after_first, sort_keys=True),
            json.dumps(ls_after_second, sort_keys=True),
        )

    def test_replay_does_not_rewrite_receipt(self):
        self._first_result()
        raw_after_first = self.receipts_db().retrieve(OFFER_ID)
        self.confirm()
        raw_after_second = self.receipts_db().retrieve(OFFER_ID)
        self.assertEqual(raw_after_first, raw_after_second)

    def test_replay_mutation_count_is_one(self):
        self._first_result()
        count_before = self.ls_db().mutation_count
        self.confirm()
        # One mutate_values call during replay (reads only, no writes)
        self.assertEqual(self.ls_db().mutation_count, count_before + 1)


# ── wrong offer / operation ───────────────────────────────────────────────────


class TestWrongIdentity(BlacklistConfirmationTestCase):
    """Wrong offer_id or terminal_operation_id returns None and writes nothing."""

    def test_wrong_offer_returns_none(self):
        self.install_blacklisting()
        result = self.service().confirm_blacklist(FP0, "c" * 32, TOP0)
        self.assertIsNone(result)

    def test_wrong_offer_no_link_state_written(self):
        self.install_blacklisting()
        raw_before = self.ls_db().retrieve(FP0)
        self.service().confirm_blacklist(FP0, "c" * 32, TOP0)
        self.assertEqual(raw_before, self.ls_db().retrieve(FP0))

    def test_wrong_offer_no_receipt_written(self):
        self.install_blacklisting()
        self.service().confirm_blacklist(FP0, "c" * 32, TOP0)
        self.assertIsNone(self.receipts_db().retrieve("c" * 32))

    def test_wrong_operation_returns_none(self):
        self.install_blacklisting()
        other_top = terminal_operation_id(pkg(1))
        result = self.service().confirm_blacklist(FP0, OFFER_ID, other_top)
        self.assertIsNone(result)

    def test_wrong_operation_no_link_state_written(self):
        self.install_blacklisting()
        other_top = terminal_operation_id(pkg(1))
        raw_before = self.ls_db().retrieve(FP0)
        self.service().confirm_blacklist(FP0, OFFER_ID, other_top)
        self.assertEqual(raw_before, self.ls_db().retrieve(FP0))


# ── invalid argument syntax ───────────────────────────────────────────────────


class TestInvalidArguments(BlacklistConfirmationTestCase):
    """Invalid arguments raise ValueError before any storage call."""

    def test_bad_fingerprint_raises(self):
        with self.assertRaises(ValueError):
            self.service().confirm_blacklist("ABCD" * 16, OFFER_ID, TOP0)

    def test_short_fingerprint_raises(self):
        with self.assertRaises(ValueError):
            self.service().confirm_blacklist("a" * 63, OFFER_ID, TOP0)

    def test_bad_offer_id_raises(self):
        with self.assertRaises(ValueError):
            self.service().confirm_blacklist(FP0, "XXXX" * 8, TOP0)

    def test_short_offer_id_raises(self):
        with self.assertRaises(ValueError):
            self.service().confirm_blacklist(FP0, "a" * 31, TOP0)

    def test_bad_terminal_operation_id_raises(self):
        with self.assertRaises(ValueError):
            self.service().confirm_blacklist(FP0, OFFER_ID, "Z" * 64)

    def test_raises_before_storage_call(self):
        # No mutation must have occurred
        count_before = self.ls_db().mutation_count
        try:
            self.service().confirm_blacklist("bad", OFFER_ID, TOP0)
        except ValueError:
            pass
        self.assertEqual(self.ls_db().mutation_count, count_before)


# ── fail-closed ───────────────────────────────────────────────────────────────


class TestFailClosed(BlacklistConfirmationTestCase):
    """Malformed/absent/wrong-state input returns None without writes."""

    def test_absent_link_state_returns_none(self):
        result = self.confirm()
        self.assertIsNone(result)

    def test_absent_link_state_no_receipt_written(self):
        self.confirm()
        self.assertIsNone(self.receipts_db().retrieve(OFFER_ID))

    def test_held_state_not_blacklisting_returns_none(self):
        held = {
            "schema_version": 1,
            "state": "held",
            "first_blocked_epoch": FIRST_BLOCKED,
            "retry_after_epoch": NOW + WINDOW,
            "lease": None,
        }
        self.ls_db().update_store(FP0, encode_link_state(held))
        result = self.confirm()
        self.assertIsNone(result)

    def test_already_blacklisted_without_receipt_returns_none(self):
        blacklisted = {
            "schema_version": 1,
            "state": "blacklisted",
            "first_blocked_epoch": FIRST_BLOCKED,
            "blacklisted_epoch": NOW,
        }
        self.ls_db().update_store(FP0, encode_link_state(blacklisted))
        result = self.confirm()
        self.assertIsNone(result)

    def test_malformed_link_state_returns_none(self):
        self.ls_db().update_store(FP0, '{"bad": true}')
        result = self.confirm()
        self.assertIsNone(result)

    def test_malformed_link_state_no_receipt_written(self):
        self.ls_db().update_store(FP0, '{"bad": true}')
        self.confirm()
        self.assertIsNone(self.receipts_db().retrieve(OFFER_ID))

    def test_malformed_receipt_fails_closed(self):
        self.install_blacklisting()
        self.receipts_db().update_store(OFFER_ID, '{"bad": true}')
        result = self.confirm()
        self.assertIsNone(result)

    def test_malformed_receipt_leaves_link_state_unchanged(self):
        self.install_blacklisting()
        raw_before = self.ls_db().retrieve(FP0)
        self.receipts_db().update_store(OFFER_ID, '{"bad": true}')
        self.confirm()
        self.assertEqual(raw_before, self.ls_db().retrieve(FP0))

    def test_conflicting_receipt_wrong_outcome_fails_closed(self):
        self.install_blacklisting()
        # Write a receipt with outcome="clear" at the offer_id key
        bad_receipt = encode_offer_receipt(
            {
                "schema_version": 1,
                "generation_id": SWEEP_ID,
                "fingerprint": FP0,
                "package_id": PKG0,
                "mode": "retest",
                "outcome": "clear",
                "response": {
                    "state": "healthy",
                    "cleared": True,
                    "accepted": "",
                    "sweep_id": SWEEP_ID,
                    "sweep_tested": 0,
                    "sweep_total": 0,
                    "sweep_deadline_epoch": NOW + WINDOW,
                },
                "accepted_epoch": NOW,
                "expires_epoch": NOW + RECEIPT_RETENTION_SECONDS,
            }
        )
        self.receipts_db().update_store(OFFER_ID, bad_receipt)
        result = self.confirm()
        self.assertIsNone(result)

    def test_conflicting_receipt_wrong_mode_fails_closed(self):
        self.install_blacklisting()
        bad_receipt = encode_offer_receipt(
            {
                "schema_version": 1,
                "generation_id": SWEEP_ID,
                "fingerprint": FP0,
                "package_id": PKG0,
                "mode": "sweep",
                "outcome": "blocked",
                "response": {
                    "instruction": "hold",
                    "state": "sweeping",
                    "hold_type": "provisional",
                    "evidence_count": 1,
                    "retry_after_epoch": NOW + WINDOW,
                    "sweep_id": SWEEP_ID,
                    "sweep_tested": 1,
                    "sweep_total": 2,
                    "sweep_deadline_epoch": NOW + WINDOW,
                },
                "accepted_epoch": NOW,
                "expires_epoch": NOW + RECEIPT_RETENTION_SECONDS,
            }
        )
        self.receipts_db().update_store(OFFER_ID, bad_receipt)
        result = self.confirm()
        self.assertIsNone(result)

    def test_conflicting_receipt_wrong_fingerprint_fails_closed(self):
        self.install_blacklisting()
        bad_fp = fp(99)
        bad_receipt = encode_offer_receipt(
            {
                "schema_version": 1,
                "generation_id": SWEEP_ID,
                "fingerprint": bad_fp,
                "package_id": PKG0,
                "mode": "retest",
                "outcome": "blocked",
                "response": build_blacklist_decision(
                    sweep_id=SWEEP_ID,
                    sweep_deadline_epoch=NOW + WINDOW,
                ),
                "accepted_epoch": NOW,
                "expires_epoch": NOW + RECEIPT_RETENTION_SECONDS,
            }
        )
        self.receipts_db().update_store(OFFER_ID, bad_receipt)
        result = self.confirm()
        self.assertIsNone(result)

    def test_conflicting_receipt_even_with_valid_blacklisting_fails_closed(self):
        # Blacklisting row is valid, but receipt is conflicting → fails closed
        self.install_blacklisting()
        bad_receipt = encode_offer_receipt(
            {
                "schema_version": 1,
                "generation_id": SWEEP_ID,
                "fingerprint": FP0,
                "package_id": PKG0,
                "mode": "retest",
                "outcome": "clear",
                "response": {
                    "state": "healthy",
                    "cleared": True,
                    "accepted": "",
                    "sweep_id": SWEEP_ID,
                    "sweep_tested": 0,
                    "sweep_total": 0,
                    "sweep_deadline_epoch": NOW + WINDOW,
                },
                "accepted_epoch": NOW,
                "expires_epoch": NOW + RECEIPT_RETENTION_SECONDS,
            }
        )
        self.receipts_db().update_store(OFFER_ID, bad_receipt)
        result = self.confirm()
        self.assertIsNone(result)
        # Link state must be unchanged (still blacklisting)
        ls = self.link_state()
        self.assertEqual(ls["state"], "blacklisting")


# ── mutation targets ──────────────────────────────────────────────────────────


class TestMutationTargets(BlacklistConfirmationTestCase):
    """Only two mutation targets: link state and receipt."""

    def test_only_link_states_and_receipts_mutated(self):
        # Track all mutation calls and their targets
        mutations = []
        ls_db = self.ls_db()
        original_mutate = ls_db.mutate_values

        def recording_mutate(targets, mutator):
            mutations.append([(t, k) for t, k in targets])
            return original_mutate(targets, mutator)

        ls_db.mutate_values = recording_mutate
        self.install_blacklisting()
        self.confirm()

        self.assertEqual(len(mutations), 1)
        target_tables = {t for t, _k in mutations[0]}
        self.assertIn(FILECRYPT_LINK_STATES_TABLE, target_tables)
        self.assertIn(FILECRYPT_OFFER_RECEIPTS_TABLE, target_tables)
        # Exactly two targets
        self.assertEqual(len(mutations[0]), 2)

    def test_no_header_member_protected_outbox_touched(self):
        from quasarr.providers.crypter_cooldowns import CRYPTER_EVENT_TABLE
        from quasarr.providers.filecrypt_lifecycle import (
            FILECRYPT_SWEEP_MEMBERS_TABLE,
            FILECRYPT_SWEEP_STATE_TABLE,
        )

        self.install_blacklisting()
        self.confirm()

        for forbidden_table in (
            FILECRYPT_SWEEP_STATE_TABLE,
            FILECRYPT_SWEEP_MEMBERS_TABLE,
            CRYPTER_EVENT_TABLE,
            "protected",
        ):
            db = self.state.get_db(forbidden_table)
            self.assertEqual(db.mutation_count, 0, f"{forbidden_table} was mutated")


# ── rollback ──────────────────────────────────────────────────────────────────


class TestRollback(BlacklistConfirmationTestCase):
    """Injected second-target write failure rolls both link and receipt back."""

    def test_write_failure_rolls_back_link_state(self):
        self.install_blacklisting()
        raw_before = self.ls_db().retrieve(FP0)

        ls_db = self.ls_db()
        original_mutate = ls_db.mutate_values

        class FailingRows(dict):
            def __setitem__(inner_self, key, value):
                raise RuntimeError("injected receipt write failure")

        def failing_mutate(targets, mutator):
            # Let the mutator run, then fail during the write phase by replacing rows
            receipts_db = self.state.get_db(FILECRYPT_OFFER_RECEIPTS_TABLE)
            receipts_db.rows = FailingRows(receipts_db.rows)
            try:
                return original_mutate(targets, mutator)
            except RuntimeError:
                raise

        ls_db.mutate_values = failing_mutate

        with self.assertRaises(RuntimeError):
            self.confirm()

        # Link state must be restored (not blacklisted)
        raw_after = self.ls_db().retrieve(FP0)
        self.assertEqual(raw_before, raw_after)

    def test_write_failure_leaves_no_receipt(self):
        self.install_blacklisting()

        ls_db = self.ls_db()
        original_mutate = ls_db.mutate_values

        class FailingRows(dict):
            def __setitem__(inner_self, key, value):
                raise RuntimeError("injected receipt write failure")

        def failing_mutate(targets, mutator):
            receipts_db = self.state.get_db(FILECRYPT_OFFER_RECEIPTS_TABLE)
            receipts_db.rows = FailingRows(receipts_db.rows)
            return original_mutate(targets, mutator)

        ls_db.mutate_values = failing_mutate

        with self.assertRaises(RuntimeError):
            self.confirm()

        self.assertIsNone(self.receipts_db().retrieve(OFFER_ID))


# ── concurrency ───────────────────────────────────────────────────────────────


class TestConcurrency(BlacklistConfirmationTestCase):
    """Concurrent exact calls: one commits, the other replays identically."""

    def test_concurrent_calls_produce_one_receipt_and_identical_results(self):
        self.install_blacklisting()
        svc = self.service()
        results = []

        def first_confirm():
            r = svc.confirm_blacklist(FP0, OFFER_ID, TOP0)
            results.append(r)

        # Fire first_confirm inside the lock via the before_mutation hook
        self.ls_db().before_mutation = first_confirm

        second_result = svc.confirm_blacklist(FP0, OFFER_ID, TOP0)

        self.assertEqual(len(results), 1)
        self.assertIsNotNone(results[0])
        self.assertIsNotNone(second_result)
        self.assertEqual(results[0], second_result)

    def test_concurrent_calls_write_exactly_one_receipt(self):
        self.install_blacklisting()
        svc = self.service()

        def first_confirm():
            svc.confirm_blacklist(FP0, OFFER_ID, TOP0)

        self.ls_db().before_mutation = first_confirm
        svc.confirm_blacklist(FP0, OFFER_ID, TOP0)

        # Exactly one receipt at the offer_id key
        raw = self.receipts_db().retrieve(OFFER_ID)
        self.assertIsNotNone(raw)
        rcpt = decode_offer_receipt(raw)
        self.assertEqual(rcpt["outcome"], "blocked")


# ── privacy ───────────────────────────────────────────────────────────────────


class TestPrivacy(BlacklistConfirmationTestCase):
    """Raw link-state and receipt JSON must contain no URL/title/reason/error."""

    def test_link_state_raw_no_url_title_reason_error(self):
        self.install_blacklisting()
        self.confirm()
        raw = self.ls_db().retrieve(FP0)
        for keyword in ("http", "title", "reason", "error"):
            self.assertNotIn(keyword, raw)

    def test_receipt_raw_no_url_title_reason_error(self):
        self.install_blacklisting()
        self.confirm()
        raw = self.receipts_db().retrieve(OFFER_ID)
        for keyword in ("http", "title", "reason", "error"):
            self.assertNotIn(keyword, raw)


if __name__ == "__main__":
    unittest.main()
