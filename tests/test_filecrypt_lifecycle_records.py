# -*- coding: utf-8 -*-

import json
import unittest

from quasarr.providers.filecrypt_lifecycle import (
    FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE,
    FILECRYPT_LINK_STATES_TABLE,
    FILECRYPT_MIGRATION_KEY,
    FILECRYPT_OFFER_RECEIPTS_TABLE,
    FILECRYPT_SWEEP_KEY,
    FILECRYPT_SWEEP_MEMBERS_TABLE,
    FILECRYPT_SWEEP_STATE_TABLE,
    MINIMUM_GLOBAL_COOLDOWN_SIZE,
    MINIMUM_SWEEP_SIZE,
    decode_link_state,
    decode_migration_marker,
    decode_offer_receipt,
    decode_sweep_header,
    decode_sweep_member,
    encode_link_state,
    encode_migration_marker,
    encode_offer_receipt,
    encode_sweep_header,
    encode_sweep_member,
    link_state_is_held,
    link_state_is_recheck_eligible,
)

# ── golden records ────────────────────────────────────────────────────────────

_ID = "a" * 32
_ID2 = "b" * 32
_ID3 = "c" * 32
_FP = "d" * 64
_PKG = "Quasarr_movies_" + "e" * 32
_PKG_NUMERIC = "Quasarr_movies4k_" + "a" * 32
_TOP_ID = "f" * 64  # terminal operation ID (64-hex)

_HELD = {
    "schema_version": 1,
    "state": "held",
    "first_blocked_epoch": 1000,
    "retry_after_epoch": 2000,
    "lease": None,
}
_HELD_WITH_LEASE = {
    "schema_version": 1,
    "state": "held",
    "first_blocked_epoch": 1000,
    "retry_after_epoch": 2000,
    "lease": {
        "sweep_id": _ID,
        "offer_id": _ID2,
        "package_id": _PKG,
        "offer_expires_epoch": 1500,
    },
}
_BLACKLISTING = {
    "schema_version": 1,
    "state": "blacklisting",
    "first_blocked_epoch": 1000,
    "recheck_offer_id": _ID3,
    "recheck_package_id": _PKG,
    "recheck_sweep_id": _ID,
    "terminal_operation_id": _TOP_ID,
}
_BLACKLISTED = {
    "schema_version": 1,
    "state": "blacklisted",
    "first_blocked_epoch": 1000,
    "blacklisted_epoch": 3000,
}
_SWEEPING = {
    "schema_version": 1,
    "state": "sweeping",
    "generation_id": _ID,
    "opened_epoch": 1000,
    "deadline_epoch": 1900,
    "window_seconds": 900,
    "total": 5,
    "tested": 3,
    "blocked": 2,
    "global_possible": True,
}
_HEALTHY = {
    "schema_version": 1,
    "state": "healthy",
    "generation_id": _ID,
    "until_epoch": 2000,
}
_SWEEP_COOLDOWN = {
    "schema_version": 1,
    "state": "cooldown",
    "generation_id": _ID,
    "sweep_deadline_epoch": 1900,
    "retry_after_epoch": 3000,
}
_MEMBER_PENDING = {
    "schema_version": 1,
    "generation_id": _ID,
    "fingerprint": _FP,
    "state": "pending",
    "lease": None,
    "outcome": None,
}
_MEMBER_OFFERED = {
    "schema_version": 1,
    "generation_id": _ID,
    "fingerprint": _FP,
    "state": "offered",
    "lease": {
        "offer_id": _ID2,
        "package_id": _PKG,
        "offer_expires_epoch": 1500,
    },
    "outcome": None,
}
_MEMBER_BLOCKED = {
    "schema_version": 1,
    "generation_id": _ID,
    "fingerprint": _FP,
    "state": "blocked",
    "lease": None,
    "outcome": {
        "offer_id": _ID2,
        "package_id": _PKG,
        "accepted_epoch": 1600,
    },
}
_MEMBER_CLEAR = dict(_MEMBER_BLOCKED, state="clear")
_MEMBER_UNKNOWN = dict(_MEMBER_BLOCKED, state="unknown")
_RECEIPT = {
    "schema_version": 1,
    "generation_id": _ID,
    "fingerprint": _FP,
    "package_id": _PKG,
    "mode": "sweep",
    "outcome": "blocked",
    "response": {"instruction": "hold"},
    "accepted_epoch": 1600,
    "expires_epoch": 90000,
}
_MIGRATION = {
    "schema_version": 1,
    "completed_epoch": 12345,
}


class ConstantsTests(unittest.TestCase):
    def test_table_names_are_distinct_strings(self):
        tables = [
            FILECRYPT_LINK_STATES_TABLE,
            FILECRYPT_SWEEP_STATE_TABLE,
            FILECRYPT_SWEEP_MEMBERS_TABLE,
            FILECRYPT_OFFER_RECEIPTS_TABLE,
            FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE,
        ]
        self.assertEqual(len(tables), len(set(tables)))
        for t in tables:
            self.assertIsInstance(t, str)
            self.assertTrue(t)

    def test_key_constants(self):
        self.assertIsInstance(FILECRYPT_SWEEP_KEY, str)
        self.assertIsInstance(FILECRYPT_MIGRATION_KEY, str)

    def test_minimums(self):
        self.assertEqual(2, MINIMUM_SWEEP_SIZE)
        self.assertEqual(5, MINIMUM_GLOBAL_COOLDOWN_SIZE)


# ── link-state codecs ─────────────────────────────────────────────────────────


class LinkStateHeldCodecTests(unittest.TestCase):
    def test_round_trip_null_lease(self):
        encoded = encode_link_state(_HELD)
        decoded = decode_link_state(encoded)
        self.assertEqual(_HELD, decoded)

    def test_round_trip_with_lease(self):
        encoded = encode_link_state(_HELD_WITH_LEASE)
        decoded = decode_link_state(encoded)
        self.assertEqual(_HELD_WITH_LEASE, decoded)

    def test_decode_rejects_missing_key(self):
        for key in _HELD:
            with self.subTest(missing=key):
                bad = {k: v for k, v in _HELD.items() if k != key}
                self.assertIsNone(decode_link_state(json.dumps(bad)))

    def test_decode_rejects_extra_key(self):
        bad = dict(_HELD, extra_field="x")
        self.assertIsNone(decode_link_state(json.dumps(bad)))

    def test_decode_rejects_wrong_schema_version(self):
        for ver in (0, 2, "1", None, True):
            with self.subTest(version=ver):
                bad = dict(_HELD, schema_version=ver)
                self.assertIsNone(decode_link_state(json.dumps(bad)))

    def test_decode_rejects_bool_as_epoch(self):
        for field in ("first_blocked_epoch", "retry_after_epoch"):
            with self.subTest(field=field):
                bad = dict(_HELD, **{field: True})
                self.assertIsNone(decode_link_state(json.dumps(bad)))

    def test_decode_rejects_negative_epoch(self):
        bad = dict(_HELD, retry_after_epoch=-1)
        self.assertIsNone(decode_link_state(json.dumps(bad)))

    def test_decode_rejects_lease_missing_key(self):
        for key in _HELD_WITH_LEASE["lease"]:
            with self.subTest(missing=key):
                lease = {k: v for k, v in _HELD_WITH_LEASE["lease"].items() if k != key}
                bad = dict(_HELD_WITH_LEASE, lease=lease)
                self.assertIsNone(decode_link_state(json.dumps(bad)))

    def test_decode_rejects_lease_extra_key(self):
        lease = dict(_HELD_WITH_LEASE["lease"], extra="x")
        bad = dict(_HELD_WITH_LEASE, lease=lease)
        self.assertIsNone(decode_link_state(json.dumps(bad)))

    def test_decode_rejects_lease_bad_sweep_id(self):
        for bad_id in ("", "A" * 32, "z" * 32, _ID[:-1]):
            with self.subTest(id=bad_id):
                lease = dict(_HELD_WITH_LEASE["lease"], sweep_id=bad_id)
                bad = dict(_HELD_WITH_LEASE, lease=lease)
                self.assertIsNone(decode_link_state(json.dumps(bad)))

    def test_decode_rejects_lease_bad_package_id(self):
        lease = dict(_HELD_WITH_LEASE["lease"], package_id="bad_pkg")
        bad = dict(_HELD_WITH_LEASE, lease=lease)
        self.assertIsNone(decode_link_state(json.dumps(bad)))

    def test_encode_raises_type_error_for_non_dict(self):
        with self.assertRaises(TypeError):
            encode_link_state("not-a-dict")

    def test_encode_raises_value_error_for_wrong_schema_version(self):
        with self.assertRaises(ValueError):
            encode_link_state(dict(_HELD, schema_version=2))

    def test_encode_raises_value_error_for_bool_epoch(self):
        with self.assertRaises(ValueError):
            encode_link_state(dict(_HELD, first_blocked_epoch=True))

    def test_encode_raises_value_error_for_extra_key(self):
        with self.assertRaises(ValueError):
            encode_link_state(dict(_HELD, extra="x"))

    def test_encode_raises_value_error_for_bad_lease_package_id(self):
        lease = dict(_HELD_WITH_LEASE["lease"], package_id="bad")
        with self.assertRaises(ValueError):
            encode_link_state(dict(_HELD_WITH_LEASE, lease=lease))

    def test_decode_output_is_canonical_sorted_json(self):
        raw = json.dumps(_HELD, sort_keys=False)
        decoded = decode_link_state(raw)
        self.assertIsNotNone(decoded)
        re_encoded = encode_link_state(decoded)
        self.assertEqual(json.loads(re_encoded), _HELD)


class LinkStateBlacklistingCodecTests(unittest.TestCase):
    def test_round_trip(self):
        encoded = encode_link_state(_BLACKLISTING)
        decoded = decode_link_state(encoded)
        self.assertEqual(_BLACKLISTING, decoded)

    def test_decode_rejects_missing_key(self):
        for key in _BLACKLISTING:
            with self.subTest(missing=key):
                bad = {k: v for k, v in _BLACKLISTING.items() if k != key}
                self.assertIsNone(decode_link_state(json.dumps(bad)))

    def test_decode_rejects_bad_terminal_operation_id(self):
        # terminal_operation_id must be 64-hex, not 32-hex
        bad = dict(_BLACKLISTING, terminal_operation_id=_ID)
        self.assertIsNone(decode_link_state(json.dumps(bad)))

    def test_decode_rejects_bad_recheck_offer_id(self):
        bad = dict(_BLACKLISTING, recheck_offer_id=_FP)
        self.assertIsNone(decode_link_state(json.dumps(bad)))

    def test_decode_rejects_non_canonical_package_id(self):
        bad = dict(_BLACKLISTING, recheck_package_id="Quasarr_MOVIES_" + "a" * 32)
        self.assertIsNone(decode_link_state(json.dumps(bad)))

    def test_decode_rejects_missing_recheck_sweep_id(self):
        bad = {k: v for k, v in _BLACKLISTING.items() if k != "recheck_sweep_id"}
        self.assertIsNone(decode_link_state(json.dumps(bad)))

    def test_decode_rejects_bad_recheck_sweep_id(self):
        for bad_id in ("", "Z" * 32, "G" * 32, _ID[:-1]):
            with self.subTest(id=bad_id):
                bad = dict(_BLACKLISTING, recheck_sweep_id=bad_id)
                self.assertIsNone(decode_link_state(json.dumps(bad)))

    def test_decode_rejects_recheck_sweep_id_as_bool(self):
        bad = dict(_BLACKLISTING, recheck_sweep_id=True)
        self.assertIsNone(decode_link_state(json.dumps(bad)))

    def test_decode_rejects_recheck_sweep_id_as_integer(self):
        bad = dict(_BLACKLISTING, recheck_sweep_id=12345)
        self.assertIsNone(decode_link_state(json.dumps(bad)))

    def test_round_trip_different_sweep_and_offer_ids(self):
        # Verify round trip preserves both different recheck_sweep_id and recheck_offer_id
        encoded = encode_link_state(_BLACKLISTING)
        decoded = decode_link_state(encoded)
        self.assertEqual(_BLACKLISTING, decoded)
        # Ensure sweep_id is different from offer_id (not confused)
        self.assertNotEqual(decoded["recheck_sweep_id"], decoded["recheck_offer_id"])
        self.assertEqual(decoded["recheck_sweep_id"], _ID)
        self.assertEqual(decoded["recheck_offer_id"], _ID3)

    def test_encode_raises_for_missing_recheck_sweep_id(self):
        bad = {k: v for k, v in _BLACKLISTING.items() if k != "recheck_sweep_id"}
        with self.assertRaises(ValueError):
            encode_link_state(bad)

    def test_encode_raises_for_bad_recheck_sweep_id(self):
        for bad_id in ("", "Z" * 32, "G" * 32, _ID[:-1]):
            with self.subTest(id=bad_id):
                bad = dict(_BLACKLISTING, recheck_sweep_id=bad_id)
                with self.assertRaises(ValueError):
                    encode_link_state(bad)

    def test_encode_raises_for_recheck_sweep_id_as_bool(self):
        bad = dict(_BLACKLISTING, recheck_sweep_id=False)
        with self.assertRaises(ValueError):
            encode_link_state(bad)

    def test_encode_raises_for_extra_key(self):
        bad = dict(_BLACKLISTING, extra="x")
        with self.assertRaises(ValueError):
            encode_link_state(bad)


class LinkStateBlacklistedCodecTests(unittest.TestCase):
    def test_round_trip(self):
        encoded = encode_link_state(_BLACKLISTED)
        decoded = decode_link_state(encoded)
        self.assertEqual(_BLACKLISTED, decoded)

    def test_decode_rejects_missing_key(self):
        for key in _BLACKLISTED:
            with self.subTest(missing=key):
                bad = {k: v for k, v in _BLACKLISTED.items() if k != key}
                self.assertIsNone(decode_link_state(json.dumps(bad)))

    def test_decode_rejects_bool_as_blacklisted_epoch(self):
        bad = dict(_BLACKLISTED, blacklisted_epoch=False)
        self.assertIsNone(decode_link_state(json.dumps(bad)))

    def test_decode_rejects_unknown_state(self):
        bad = dict(_BLACKLISTED, state="unknown_state")
        self.assertIsNone(decode_link_state(json.dumps(bad)))


# ── sweep-header codecs ───────────────────────────────────────────────────────


class SweepHeaderSweepingCodecTests(unittest.TestCase):
    def test_round_trip(self):
        encoded = encode_sweep_header(_SWEEPING)
        decoded = decode_sweep_header(encoded)
        self.assertEqual(_SWEEPING, decoded)

    def test_round_trip_global_possible_false(self):
        record = dict(_SWEEPING, global_possible=False)
        self.assertEqual(record, decode_sweep_header(encode_sweep_header(record)))

    def test_decode_rejects_missing_key(self):
        for key in _SWEEPING:
            with self.subTest(missing=key):
                bad = {k: v for k, v in _SWEEPING.items() if k != key}
                self.assertIsNone(decode_sweep_header(json.dumps(bad)))

    def test_decode_rejects_extra_key(self):
        bad = dict(_SWEEPING, extra="x")
        self.assertIsNone(decode_sweep_header(json.dumps(bad)))

    def test_decode_rejects_bool_as_epoch(self):
        for field in (
            "opened_epoch",
            "deadline_epoch",
            "window_seconds",
            "total",
            "tested",
            "blocked",
        ):
            with self.subTest(field=field):
                bad = dict(_SWEEPING, **{field: True})
                self.assertIsNone(decode_sweep_header(json.dumps(bad)))

    def test_decode_rejects_integer_as_global_possible(self):
        for v in (0, 1):
            with self.subTest(value=v):
                bad = dict(_SWEEPING, global_possible=v)
                self.assertIsNone(decode_sweep_header(json.dumps(bad)))

    def test_decode_rejects_tested_greater_than_total(self):
        bad = dict(_SWEEPING, tested=6, blocked=2)
        self.assertIsNone(decode_sweep_header(json.dumps(bad)))

    def test_decode_rejects_blocked_greater_than_tested(self):
        bad = dict(_SWEEPING, blocked=4)
        self.assertIsNone(decode_sweep_header(json.dumps(bad)))

    def test_decode_accepts_tested_equal_total(self):
        record = dict(_SWEEPING, tested=5, blocked=3)
        self.assertIsNotNone(decode_sweep_header(json.dumps(record)))

    def test_decode_accepts_all_zeros(self):
        record = dict(_SWEEPING, total=0, tested=0, blocked=0)
        self.assertIsNotNone(decode_sweep_header(json.dumps(record)))

    def test_encode_raises_for_integer_global_possible(self):
        with self.assertRaises(ValueError):
            encode_sweep_header(dict(_SWEEPING, global_possible=1))

    def test_encode_raises_for_tested_gt_total(self):
        with self.assertRaises(ValueError):
            encode_sweep_header(dict(_SWEEPING, tested=10))

    def test_decode_rejects_bad_generation_id(self):
        bad = dict(_SWEEPING, generation_id="Z" * 32)
        self.assertIsNone(decode_sweep_header(json.dumps(bad)))


class SweepHeaderHealthyCodecTests(unittest.TestCase):
    def test_round_trip(self):
        encoded = encode_sweep_header(_HEALTHY)
        decoded = decode_sweep_header(encoded)
        self.assertEqual(_HEALTHY, decoded)

    def test_decode_rejects_missing_key(self):
        for key in _HEALTHY:
            with self.subTest(missing=key):
                bad = {k: v for k, v in _HEALTHY.items() if k != key}
                self.assertIsNone(decode_sweep_header(json.dumps(bad)))

    def test_decode_rejects_bool_as_until_epoch(self):
        bad = dict(_HEALTHY, until_epoch=True)
        self.assertIsNone(decode_sweep_header(json.dumps(bad)))


class SweepHeaderCooldownCodecTests(unittest.TestCase):
    def test_round_trip(self):
        encoded = encode_sweep_header(_SWEEP_COOLDOWN)
        decoded = decode_sweep_header(encoded)
        self.assertEqual(_SWEEP_COOLDOWN, decoded)

    def test_decode_rejects_missing_key(self):
        for key in _SWEEP_COOLDOWN:
            with self.subTest(missing=key):
                bad = {k: v for k, v in _SWEEP_COOLDOWN.items() if k != key}
                self.assertIsNone(decode_sweep_header(json.dumps(bad)))

    def test_decode_rejects_bool_as_retry_epoch(self):
        bad = dict(_SWEEP_COOLDOWN, retry_after_epoch=False)
        self.assertIsNone(decode_sweep_header(json.dumps(bad)))

    def test_decode_rejects_unknown_sweep_state(self):
        bad = dict(_SWEEP_COOLDOWN, state="observing")
        self.assertIsNone(decode_sweep_header(json.dumps(bad)))

    def test_decode_rejects_zero_sweep_deadline_epoch(self):
        # Epoch 0 is not a valid deadline; cooldown must have a strictly positive deadline.
        bad = dict(_SWEEP_COOLDOWN, sweep_deadline_epoch=0)
        self.assertIsNone(decode_sweep_header(json.dumps(bad)))

    def test_encode_raises_for_zero_sweep_deadline_epoch(self):
        bad = dict(_SWEEP_COOLDOWN, sweep_deadline_epoch=0)
        with self.assertRaises(ValueError):
            encode_sweep_header(bad)

    def test_positive_sweep_deadline_epoch_round_trips(self):
        record = dict(_SWEEP_COOLDOWN, sweep_deadline_epoch=1)
        self.assertEqual(record, decode_sweep_header(encode_sweep_header(record)))


# ── sweep-member codecs ───────────────────────────────────────────────────────


class SweepMemberCodecTests(unittest.TestCase):
    def test_round_trip_pending(self):
        encoded = encode_sweep_member(_MEMBER_PENDING)
        decoded = decode_sweep_member(encoded)
        self.assertEqual(_MEMBER_PENDING, decoded)

    def test_round_trip_offered(self):
        encoded = encode_sweep_member(_MEMBER_OFFERED)
        decoded = decode_sweep_member(encoded)
        self.assertEqual(_MEMBER_OFFERED, decoded)

    def test_round_trip_blocked(self):
        encoded = encode_sweep_member(_MEMBER_BLOCKED)
        decoded = decode_sweep_member(encoded)
        self.assertEqual(_MEMBER_BLOCKED, decoded)

    def test_round_trip_clear(self):
        encoded = encode_sweep_member(_MEMBER_CLEAR)
        decoded = decode_sweep_member(encoded)
        self.assertEqual(_MEMBER_CLEAR, decoded)

    def test_round_trip_unknown(self):
        encoded = encode_sweep_member(_MEMBER_UNKNOWN)
        decoded = decode_sweep_member(encoded)
        self.assertEqual(_MEMBER_UNKNOWN, decoded)

    def test_decode_rejects_terminal_state_without_outcome(self):
        for state in ("blocked", "clear", "unknown"):
            with self.subTest(state=state):
                bad = dict(_MEMBER_PENDING, state=state)
                self.assertIsNone(decode_sweep_member(json.dumps(bad)))

    def test_decode_rejects_non_terminal_state_with_outcome(self):
        for state in ("pending", "offered"):
            with self.subTest(state=state):
                bad = dict(_MEMBER_BLOCKED, state=state)
                self.assertIsNone(decode_sweep_member(json.dumps(bad)))

    def test_decode_rejects_unknown_state(self):
        bad = dict(_MEMBER_PENDING, state="stale")
        self.assertIsNone(decode_sweep_member(json.dumps(bad)))

    def test_decode_rejects_missing_key(self):
        for key in _MEMBER_PENDING:
            with self.subTest(missing=key):
                bad = {k: v for k, v in _MEMBER_PENDING.items() if k != key}
                self.assertIsNone(decode_sweep_member(json.dumps(bad)))

    def test_decode_rejects_extra_key(self):
        bad = dict(_MEMBER_PENDING, extra="x")
        self.assertIsNone(decode_sweep_member(json.dumps(bad)))

    def test_decode_rejects_bad_fingerprint(self):
        # fingerprint must be 64-hex, not 32-hex
        bad = dict(_MEMBER_PENDING, fingerprint=_ID)
        self.assertIsNone(decode_sweep_member(json.dumps(bad)))

    def test_decode_rejects_outcome_missing_key(self):
        for key in _MEMBER_BLOCKED["outcome"]:
            with self.subTest(missing=key):
                outcome = {
                    k: v for k, v in _MEMBER_BLOCKED["outcome"].items() if k != key
                }
                bad = dict(_MEMBER_BLOCKED, outcome=outcome)
                self.assertIsNone(decode_sweep_member(json.dumps(bad)))

    def test_decode_rejects_outcome_extra_key(self):
        outcome = dict(_MEMBER_BLOCKED["outcome"], extra="x")
        bad = dict(_MEMBER_BLOCKED, outcome=outcome)
        self.assertIsNone(decode_sweep_member(json.dumps(bad)))

    def test_encode_raises_for_terminal_without_outcome(self):
        bad = dict(_MEMBER_PENDING, state="blocked")
        with self.assertRaises(ValueError):
            encode_sweep_member(bad)

    def test_encode_raises_for_non_terminal_with_outcome(self):
        bad = dict(_MEMBER_BLOCKED, state="pending")
        with self.assertRaises(ValueError):
            encode_sweep_member(bad)


# ── offer-receipt codecs ──────────────────────────────────────────────────────


class OfferReceiptCodecTests(unittest.TestCase):
    def test_round_trip(self):
        encoded = encode_offer_receipt(_RECEIPT)
        decoded = decode_offer_receipt(encoded)
        self.assertEqual(_RECEIPT, decoded)

    def test_round_trip_all_modes(self):
        for mode in ("sweep", "individual", "retest", "probe"):
            with self.subTest(mode=mode):
                record = dict(_RECEIPT, mode=mode)
                self.assertEqual(
                    record, decode_offer_receipt(encode_offer_receipt(record))
                )

    def test_round_trip_all_outcomes(self):
        for outcome in ("blocked", "clear", "unknown"):
            with self.subTest(outcome=outcome):
                record = dict(_RECEIPT, outcome=outcome)
                self.assertEqual(
                    record, decode_offer_receipt(encode_offer_receipt(record))
                )

    def test_probe_mode_round_trip(self):
        """Verify probe mode encodes and decodes correctly."""
        record = dict(_RECEIPT, mode="probe")
        encoded = encode_offer_receipt(record)
        decoded = decode_offer_receipt(encoded)
        self.assertEqual(record, decoded)

    def test_probe_mode_with_all_outcomes(self):
        """Verify probe mode works with all outcome values."""
        for outcome in ("blocked", "clear", "unknown"):
            with self.subTest(outcome=outcome):
                record = dict(_RECEIPT, mode="probe", outcome=outcome)
                self.assertEqual(
                    record, decode_offer_receipt(encode_offer_receipt(record))
                )

    def test_decode_rejects_missing_key(self):
        for key in _RECEIPT:
            with self.subTest(missing=key):
                bad = {k: v for k, v in _RECEIPT.items() if k != key}
                self.assertIsNone(decode_offer_receipt(json.dumps(bad)))

    def test_decode_rejects_extra_key(self):
        bad = dict(_RECEIPT, extra="x")
        self.assertIsNone(decode_offer_receipt(json.dumps(bad)))

    def test_decode_rejects_invalid_mode(self):
        bad = dict(_RECEIPT, mode="invalid")
        self.assertIsNone(decode_offer_receipt(json.dumps(bad)))

    def test_decode_rejects_invalid_outcome(self):
        bad = dict(_RECEIPT, outcome="stale")
        self.assertIsNone(decode_offer_receipt(json.dumps(bad)))

    def test_decode_rejects_non_dict_response(self):
        bad = dict(_RECEIPT, response=["not", "a", "dict"])
        self.assertIsNone(decode_offer_receipt(json.dumps(bad)))

    def test_decode_rejects_bool_as_epoch(self):
        for field in ("accepted_epoch", "expires_epoch"):
            with self.subTest(field=field):
                bad = dict(_RECEIPT, **{field: True})
                self.assertIsNone(decode_offer_receipt(json.dumps(bad)))

    def test_encode_raises_for_invalid_mode(self):
        bad = dict(_RECEIPT, mode="invalid")
        with self.assertRaises(ValueError):
            encode_offer_receipt(bad)

    def test_encode_raises_for_list_response(self):
        bad = dict(_RECEIPT, response=[1, 2])
        with self.assertRaises(ValueError):
            encode_offer_receipt(bad)


# ── migration-marker codecs ───────────────────────────────────────────────────


class MigrationMarkerCodecTests(unittest.TestCase):
    def test_round_trip(self):
        encoded = encode_migration_marker(_MIGRATION)
        decoded = decode_migration_marker(encoded)
        self.assertEqual(_MIGRATION, decoded)

    def test_decode_rejects_missing_key(self):
        for key in _MIGRATION:
            with self.subTest(missing=key):
                bad = {k: v for k, v in _MIGRATION.items() if k != key}
                self.assertIsNone(decode_migration_marker(json.dumps(bad)))

    def test_decode_rejects_extra_key(self):
        bad = dict(_MIGRATION, extra="x")
        self.assertIsNone(decode_migration_marker(json.dumps(bad)))

    def test_decode_rejects_bool_as_completed_epoch(self):
        bad = dict(_MIGRATION, completed_epoch=True)
        self.assertIsNone(decode_migration_marker(json.dumps(bad)))

    def test_decode_rejects_zero_schema_version(self):
        bad = dict(_MIGRATION, schema_version=0)
        self.assertIsNone(decode_migration_marker(json.dumps(bad)))

    def test_encode_raises_type_error_for_non_dict(self):
        with self.assertRaises(TypeError):
            encode_migration_marker(42)

    def test_encode_raises_value_error_for_wrong_schema_version(self):
        with self.assertRaises(ValueError):
            encode_migration_marker(dict(_MIGRATION, schema_version=2))


# ── robustness ────────────────────────────────────────────────────────────────

_ALL_DECODERS = [
    decode_link_state,
    decode_sweep_header,
    decode_sweep_member,
    decode_offer_receipt,
    decode_migration_marker,
]


class RecordRobustnessTests(unittest.TestCase):
    def _assert_all_none(self, value):
        for decoder in _ALL_DECODERS:
            with self.subTest(decoder=decoder.__name__):
                self.assertIsNone(decoder(value))

    def test_bad_json_returns_none(self):
        self._assert_all_none("{not json}")

    def test_none_input_returns_none(self):
        self._assert_all_none(None)

    def test_empty_string_returns_none(self):
        self._assert_all_none("")

    def test_json_array_returns_none(self):
        self._assert_all_none("[]")

    def test_json_null_returns_none(self):
        self._assert_all_none("null")

    def test_wrong_schema_version_returns_none(self):
        for ver in (0, 2, "1", None, True, 2.0):
            with self.subTest(version=ver):
                self._assert_all_none(
                    json.dumps({"schema_version": ver, "state": "held"})
                )

    def test_future_schema_version_returns_none(self):
        self._assert_all_none(json.dumps({"schema_version": 99, "state": "held"}))

    def test_recursion_error_returns_none(self):
        # A 100 000-deep nesting raises RecursionError in json.loads
        deep = "{" * 100_000 + "}" * 100_000
        self._assert_all_none(deep)

    def test_5000_digit_integer_literal_returns_none(self):
        # A very large integer literal raises ValueError in json.loads
        payload = '{"schema_version": 1, "completed_epoch": ' + "1" * 5000 + "}"
        self._assert_all_none(payload)

    def test_oversized_record_returns_none(self):
        # Build a record that exceeds the per-row byte limit
        oversized = json.dumps({"schema_version": 1, "data": "x" * (16 * 1024 + 1)})
        self._assert_all_none(oversized)

    def test_lone_surrogate_returns_none(self):
        # A lone surrogate is invalid JSON and should return None
        self._assert_all_none('{"schema_version": 1, "state": "\ud800"}')


# ── deadline predicates ───────────────────────────────────────────────────────


class LinkStatePredicateTests(unittest.TestCase):
    def test_is_held_true_before_retry_epoch(self):
        record = decode_link_state(encode_link_state(_HELD))
        self.assertTrue(link_state_is_held(record, now=1999))

    def test_is_held_false_at_retry_epoch(self):
        record = decode_link_state(encode_link_state(_HELD))
        self.assertFalse(link_state_is_held(record, now=2000))

    def test_is_held_false_after_retry_epoch(self):
        record = decode_link_state(encode_link_state(_HELD))
        self.assertFalse(link_state_is_held(record, now=9999))

    def test_is_held_false_for_blacklisted(self):
        record = decode_link_state(encode_link_state(_BLACKLISTED))
        self.assertFalse(link_state_is_held(record, now=0))

    def test_is_held_false_for_blacklisting(self):
        record = decode_link_state(encode_link_state(_BLACKLISTING))
        self.assertFalse(link_state_is_held(record, now=0))

    def test_is_held_false_for_none(self):
        self.assertFalse(link_state_is_held(None, now=0))

    def test_recheck_eligible_true_at_retry_epoch(self):
        record = decode_link_state(encode_link_state(_HELD))
        self.assertTrue(link_state_is_recheck_eligible(record, now=2000))

    def test_recheck_eligible_true_after_retry_epoch(self):
        record = decode_link_state(encode_link_state(_HELD))
        self.assertTrue(link_state_is_recheck_eligible(record, now=9999))

    def test_recheck_eligible_false_before_retry_epoch(self):
        record = decode_link_state(encode_link_state(_HELD))
        self.assertFalse(link_state_is_recheck_eligible(record, now=1999))

    def test_recheck_eligible_false_for_blacklisted(self):
        record = decode_link_state(encode_link_state(_BLACKLISTED))
        self.assertFalse(link_state_is_recheck_eligible(record, now=9999))

    def test_recheck_eligible_false_for_none(self):
        self.assertFalse(link_state_is_recheck_eligible(None, now=0))

    def test_held_and_recheck_eligible_are_mutually_exclusive(self):
        record = decode_link_state(encode_link_state(_HELD))
        for now in range(1000, 3001, 100):
            with self.subTest(now=now):
                held = link_state_is_held(record, now=now)
                recheck = link_state_is_recheck_eligible(record, now=now)
                self.assertFalse(held and recheck)

    def test_decode_link_state_is_clock_free(self):
        # decode_link_state must accept no `now` argument
        import inspect

        sig = inspect.signature(decode_link_state)
        self.assertNotIn("now", sig.parameters)


# ── numeric-category package-id tests ─────────────────────────────────────────


class PackageIdNumericCategoryTests(unittest.TestCase):
    """Every record family containing a package_id must accept an alphanumeric
    category segment such as 'movies4k'."""

    def _cases(self):
        return [
            (
                "held_lease",
                encode_link_state,
                decode_link_state,
                dict(
                    _HELD_WITH_LEASE,
                    lease=dict(_HELD_WITH_LEASE["lease"], package_id=_PKG_NUMERIC),
                ),
            ),
            (
                "blacklisting_recheck_pkg",
                encode_link_state,
                decode_link_state,
                dict(_BLACKLISTING, recheck_package_id=_PKG_NUMERIC),
            ),
            (
                "member_offered_lease",
                encode_sweep_member,
                decode_sweep_member,
                dict(
                    _MEMBER_OFFERED,
                    lease=dict(_MEMBER_OFFERED["lease"], package_id=_PKG_NUMERIC),
                ),
            ),
            (
                "member_outcome",
                encode_sweep_member,
                decode_sweep_member,
                dict(
                    _MEMBER_BLOCKED,
                    outcome=dict(_MEMBER_BLOCKED["outcome"], package_id=_PKG_NUMERIC),
                ),
            ),
            (
                "offer_receipt",
                encode_offer_receipt,
                decode_offer_receipt,
                dict(_RECEIPT, package_id=_PKG_NUMERIC),
            ),
        ]

    def test_encode_accepts_numeric_category(self):
        for name, encoder, _decoder, record in self._cases():
            with self.subTest(family=name):
                encoded = encoder(record)
                self.assertIsNotNone(encoded)

    def test_decode_accepts_numeric_category(self):
        for name, encoder, decoder, record in self._cases():
            with self.subTest(family=name):
                decoded = decoder(encoder(record))
                self.assertEqual(record, decoded)


# ── bool schema_version encoder rejection ─────────────────────────────────────


class BoolSchemaVersionEncoderTests(unittest.TestCase):
    """All five encoders must reject schema_version=True (bool is not int)."""

    _CASES = [
        ("encode_link_state", encode_link_state, _HELD),
        ("encode_sweep_header", encode_sweep_header, _SWEEPING),
        ("encode_sweep_member", encode_sweep_member, _MEMBER_PENDING),
        ("encode_offer_receipt", encode_offer_receipt, _RECEIPT),
        ("encode_migration_marker", encode_migration_marker, _MIGRATION),
    ]

    def test_bool_schema_version_raises_value_error(self):
        for name, encoder, base in self._CASES:
            with self.subTest(encoder=name):
                with self.assertRaises(ValueError):
                    encoder(dict(base, schema_version=True))


if __name__ == "__main__":
    unittest.main()
