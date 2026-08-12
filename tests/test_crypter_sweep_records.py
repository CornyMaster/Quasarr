# -*- coding: utf-8 -*-

import json
import unittest
from collections.abc import Callable
from unittest import mock

import quasarr.providers.crypter_cooldowns as cooldown_module
import quasarr.providers.crypter_sweeps as sweep_module
from quasarr.providers.crypter_cooldowns import CrypterCooldownService
from quasarr.providers.crypter_sweeps import (
    HEALTHY_SUPPRESSION_SECONDS,
    MAXIMUM_ACCEPTED_OFFERS,
    MAXIMUM_COHORT_OCCURRENCES,
    MAXIMUM_COHORT_RECORD_BYTES,
    MAXIMUM_COHORT_SIZE,
    MAXIMUM_GENERATION_OFFER_IDS,
    MAXIMUM_OFFER_ID_ATTEMPTS,
    MINIMUM_CONCLUSIVE_COHORT_SIZE,
    OFFER_LEASE_SECONDS,
    OVERSIZED_COHORT_SENTINEL,
    SWEEP_SCHEMA_VERSION,
    SWEEP_WINDOW_SECONDS,
    decision_snapshot,
    decode_decision_record,
    encode_decision_record,
    helper_package_is_candidate,
    migrate_legacy_record,
)

NOW = 1_700_000_000
SWEEP_ID = "a" * 32
OFFER_ID = "b" * 32
GENERATION_ID = "c" * 32
REASON = "ip_block_suspected"
COOLDOWN_RETRY = NOW + 24 * 60 * 60
UNTIL = NOW + HEALTHY_SUPPRESSION_SECONDS
MEMBER_KEYS = (
    "link_fingerprint",
    "result",
    "tested_epoch",
    "offer_id",
    "offer_expires_epoch",
    "response_instruction",
)
OFFER_KEYS = (
    "offer_id",
    "offer_fingerprint",
    "offer_expires_epoch",
    "mode",
    "response_instruction",
)
ACCEPTED_OFFER_KEYS = (
    "offer_id",
    "link_fingerprint",
    "mode",
    "state",
    "instruction",
    "accepted",
    "cleared",
    "hold_type",
    "evidence_count",
    "retry_after_epoch",
    "sweep_tested",
    "sweep_total",
    "sweep_deadline_epoch",
)


def fingerprint(index):
    return f"{index:064x}"


def member(index, **overrides):
    data = {
        "link_fingerprint": fingerprint(index),
        "result": "pending",
        "tested_epoch": 0,
        "offer_id": "",
        "offer_expires_epoch": 0,
        "response_instruction": "",
    }
    data.update(overrides)
    return data


def blocked_members(count=MINIMUM_CONCLUSIVE_COHORT_SIZE):
    return [
        member(
            index,
            result="blocked",
            tested_epoch=NOW,
            offer_id=f"{index:032x}",
            offer_expires_epoch=NOW + OFFER_LEASE_SECONDS,
            response_instruction="cooldown" if index == count else "hold",
        )
        for index in range(1, count + 1)
    ]


def accepted_offer(index, **overrides):
    entry = {
        "offer_id": f"{index:032x}",
        "link_fingerprint": fingerprint(index),
        "mode": "sweep",
        "state": "sweeping",
        "instruction": "hold",
        "accepted": "",
        "cleared": False,
        "hold_type": "provisional",
        "evidence_count": index,
        "retry_after_epoch": NOW + SWEEP_WINDOW_SECONDS,
        "sweep_tested": index,
        "sweep_total": MINIMUM_CONCLUSIVE_COHORT_SIZE,
        "sweep_deadline_epoch": NOW + SWEEP_WINDOW_SECONDS,
    }
    entry.update(overrides)
    return entry


def used_ids(*groups):
    """Every offer ID one generation ever minted, ascending and unique."""
    identifiers = set()
    for group in groups:
        identifiers.update(group)
    return sorted(identifiers)


def live_offer(**overrides):
    offer = {
        "offer_id": OFFER_ID,
        "offer_fingerprint": fingerprint(1),
        "offer_expires_epoch": NOW + OFFER_LEASE_SECONDS,
        "mode": "sweep",
        "response_instruction": "hold",
    }
    offer.update(overrides)
    return offer


def sweeping_record(**overrides):
    record = {
        "schema_version": SWEEP_SCHEMA_VERSION,
        "state": "sweeping",
        "reason_code": REASON,
        "sweep_id": SWEEP_ID,
        "opened_epoch": NOW,
        "deadline_epoch": NOW + SWEEP_WINDOW_SECONDS,
        "members": [member(1), member(2)],
        "accepted_offers": [],
        "used_offer_ids": [],
    }
    record.update(overrides)
    return record


def cohort_cooldown_record(**overrides):
    members = overrides.pop("members", blocked_members())
    accepted = overrides.pop(
        "accepted_offers",
        [
            accepted_offer(
                index,
                instruction="cooldown" if index == len(members) else "hold",
                **(
                    {
                        "state": "cooldown",
                        "hold_type": "crypter_cooldown",
                        "evidence_count": len(members),
                        "retry_after_epoch": COOLDOWN_RETRY,
                    }
                    if index == len(members)
                    else {}
                ),
            )
            for index in range(1, len(members) + 1)
        ],
    )
    record = {
        "schema_version": SWEEP_SCHEMA_VERSION,
        "state": "cooldown",
        "reason_code": REASON,
        "sweep_id": SWEEP_ID,
        "opened_epoch": NOW,
        "deadline_epoch": NOW + SWEEP_WINDOW_SECONDS,
        "members": members,
        "cohort_size": len(members),
        "retry_after_epoch": COOLDOWN_RETRY,
        "live_offer": live_offer(mode="probe"),
        "accepted_offers": accepted,
        "used_offer_ids": used_ids(
            (entry["offer_id"] for entry in members if entry["offer_id"]),
            (entry["offer_id"] for entry in accepted),
            (OFFER_ID,),
        ),
    }
    record.update(overrides)
    return record


def legacy_cooldown_record(**overrides):
    record = {
        "schema_version": SWEEP_SCHEMA_VERSION,
        "state": "cooldown",
        "reason_code": REASON,
        "legacy_cooldown": True,
        "retry_after_epoch": COOLDOWN_RETRY,
        "legacy_evidence_count": 3,
    }
    record.update(overrides)
    return record


def healthy_record(**overrides):
    record = {
        "schema_version": SWEEP_SCHEMA_VERSION,
        "state": "healthy",
        "sweep_id": SWEEP_ID,
        "until_epoch": UNTIL,
        "retest_members": [fingerprint(1), fingerprint(2)],
        "live_offer": live_offer(mode="retest", response_instruction=""),
        "accepted_offers": [],
        "used_offer_ids": [OFFER_ID],
    }
    record.update(overrides)
    return record


def individual_record(**overrides):
    record = {
        "schema_version": SWEEP_SCHEMA_VERSION,
        "state": "individual",
        "reason": "cohort_too_small",
        "generation_id": GENERATION_ID,
        "until_epoch": UNTIL,
        "live_offer": live_offer(
            mode="individual", response_instruction="legacy_failure"
        ),
        "hold_fingerprints": [],
        "accepted_offers": [],
        "used_offer_ids": [OFFER_ID],
    }
    record.update(overrides)
    return record


def every_record():
    return (
        ("sweeping", sweeping_record()),
        ("cooldown", cohort_cooldown_record()),
        ("legacy_cooldown", legacy_cooldown_record()),
        ("healthy", healthy_record()),
        ("individual", individual_record()),
    )


class DecisionRecordCodecTests(unittest.TestCase):
    def test_constants_pin_the_approved_cohort_bounds(self):
        self.assertEqual(2, SWEEP_SCHEMA_VERSION)
        self.assertEqual(15 * 60, SWEEP_WINDOW_SECONDS)
        self.assertEqual(5, MINIMUM_CONCLUSIVE_COHORT_SIZE)
        self.assertEqual(100, MAXIMUM_COHORT_SIZE)
        self.assertEqual(1000, MAXIMUM_COHORT_OCCURRENCES)
        self.assertEqual(15 * 60, HEALTHY_SUPPRESSION_SECONDS)
        self.assertEqual(2 * 60, OFFER_LEASE_SECONDS)
        self.assertEqual(256 * 1024, MAXIMUM_COHORT_RECORD_BYTES)
        self.assertEqual(101, OVERSIZED_COHORT_SENTINEL)
        self.assertEqual(MAXIMUM_COHORT_SIZE, MAXIMUM_ACCEPTED_OFFERS)
        self.assertEqual(1000, MAXIMUM_GENERATION_OFFER_IDS)
        self.assertEqual(8, MAXIMUM_OFFER_ID_ATTEMPTS)

    def test_round_trips_every_state_deterministically_and_url_free(self):
        for name, record in every_record():
            with self.subTest(state=name):
                encoded = encode_decision_record(record)

                self.assertEqual(
                    json.dumps(
                        json.loads(encoded), separators=(",", ":"), sort_keys=True
                    ),
                    encoded,
                )
                self.assertEqual(encoded, encode_decision_record(json.loads(encoded)))
                self.assertEqual(record, decode_decision_record(encoded, now=NOW))
                self.assertNotIn("http", encoded)
                self.assertNotIn("://", encoded)

    def test_each_state_pins_its_exact_key_set(self):
        expected = {
            "sweeping": {
                "schema_version",
                "state",
                "reason_code",
                "sweep_id",
                "opened_epoch",
                "deadline_epoch",
                "members",
                "accepted_offers",
                "used_offer_ids",
            },
            "cooldown": {
                "schema_version",
                "state",
                "reason_code",
                "sweep_id",
                "opened_epoch",
                "deadline_epoch",
                "members",
                "cohort_size",
                "retry_after_epoch",
                "live_offer",
                "accepted_offers",
                "used_offer_ids",
            },
            "legacy_cooldown": {
                "schema_version",
                "state",
                "reason_code",
                "legacy_cooldown",
                "retry_after_epoch",
                "legacy_evidence_count",
            },
            "healthy": {
                "schema_version",
                "state",
                "sweep_id",
                "until_epoch",
                "retest_members",
                "live_offer",
                "accepted_offers",
                "used_offer_ids",
            },
            "individual": {
                "schema_version",
                "state",
                "reason",
                "generation_id",
                "until_epoch",
                "live_offer",
                "hold_fingerprints",
                "accepted_offers",
                "used_offer_ids",
            },
        }

        for name, record in every_record():
            with self.subTest(state=name):
                self.assertEqual(expected[name], set(record))

                for key in expected[name]:
                    missing = {k: v for k, v in record.items() if k != key}
                    self.assertIsNone(
                        decode_decision_record(json.dumps(missing), now=NOW),
                        f"decoded a {name} record without {key}",
                    )
                    with self.assertRaises(ValueError):
                        encode_decision_record(missing)

                extra = dict(record, unexpected_field=1)
                self.assertIsNone(decode_decision_record(json.dumps(extra), now=NOW))
                with self.assertRaises(ValueError):
                    encode_decision_record(extra)

    def test_a_member_missing_or_gaining_a_key_is_rejected(self):
        for key in MEMBER_KEYS:
            with self.subTest(member_key=key):
                broken = {k: v for k, v in member(1).items() if k != key}
                record = sweeping_record(members=[broken, member(2)])

                self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))
                with self.assertRaises(ValueError):
                    encode_decision_record(record)

        extra = sweeping_record(
            members=[dict(member(1), tested_by="helper"), member(2)]
        )
        self.assertIsNone(decode_decision_record(json.dumps(extra), now=NOW))
        with self.assertRaises(ValueError):
            encode_decision_record(extra)

    def test_a_live_offer_missing_or_gaining_a_key_is_rejected(self):
        for key in OFFER_KEYS:
            with self.subTest(offer_key=key):
                broken = {k: v for k, v in live_offer().items() if k != key}
                record = healthy_record(live_offer=broken)

                self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))
                with self.assertRaises(ValueError):
                    encode_decision_record(record)

        extra = healthy_record(live_offer=dict(live_offer(), url="x"))
        self.assertIsNone(decode_decision_record(json.dumps(extra), now=NOW))
        self.assertEqual(
            healthy_record(live_offer=None),
            decode_decision_record(
                json.dumps(healthy_record(live_offer=None)), now=NOW
            ),
        )

    def test_identifiers_must_be_exactly_thirty_two_lowercase_hex(self):
        rejected = (
            "A" * 32,
            "a" * 31,
            "a" * 33,
            "g" * 32,
            "a" * 64,
            " " + "a" * 31,
            "",
            None,
            0,
            ["a" * 32],
        )

        for value in rejected:
            with self.subTest(identifier=repr(value)):
                for record in (
                    sweeping_record(sweep_id=value),
                    individual_record(generation_id=value),
                    healthy_record(live_offer=live_offer(offer_id=value)),
                ):
                    self.assertIsNone(
                        decode_decision_record(json.dumps(record), now=NOW)
                    )
                    with self.assertRaises(ValueError):
                        encode_decision_record(record)

                # A member offer slot is empty or an exact identifier, never else.
                if value != "":
                    record = sweeping_record(
                        members=[member(1, offer_id=value), member(2)]
                    )
                    self.assertIsNone(
                        decode_decision_record(json.dumps(record), now=NOW)
                    )

        empty_slot = sweeping_record(members=[member(1, offer_id=""), member(2)])
        self.assertIsNotNone(decode_decision_record(json.dumps(empty_slot), now=NOW))

    def test_fingerprints_must_be_exactly_sixty_four_lowercase_hex(self):
        rejected = (
            "A" * 64,
            "a" * 63,
            "a" * 65,
            "z" * 64,
            "a" * 32,
            "",
            None,
            0,
        )

        for value in rejected:
            with self.subTest(fingerprint=repr(value)):
                for record in (
                    sweeping_record(
                        members=[member(1), member(2, link_fingerprint=value)]
                    ),
                    healthy_record(retest_members=[value]),
                    healthy_record(live_offer=live_offer(offer_fingerprint=value)),
                ):
                    self.assertIsNone(
                        decode_decision_record(json.dumps(record), now=NOW)
                    )
                    with self.assertRaises(ValueError):
                        encode_decision_record(record)

    def test_members_and_retest_members_are_unique_and_strictly_ordered(self):
        rejected = (
            [member(1), member(1)],
            [member(2), member(1)],
            [member(1), member(3), member(2)],
        )

        for members in rejected:
            with self.subTest(members=[m["link_fingerprint"][-2:] for m in members]):
                record = sweeping_record(members=members)
                self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))
                with self.assertRaises(ValueError):
                    encode_decision_record(record)

        for retest in (
            [fingerprint(1), fingerprint(1)],
            [fingerprint(2), fingerprint(1)],
        ):
            with self.subTest(retest=retest[-1][-2:]):
                record = healthy_record(retest_members=retest)
                self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))

        self.assertIsNotNone(
            decode_decision_record(
                json.dumps(healthy_record(retest_members=[])), now=NOW
            )
        )

    def test_cohort_bounds_are_enforced_on_both_ends(self):
        ordered = [member(index) for index in range(1, MAXIMUM_COHORT_SIZE + 2)]

        self.assertIsNone(
            decode_decision_record(
                json.dumps(sweeping_record(members=[member(1)])), now=NOW
            ),
            "a one-member inventory is individual, never a sweep",
        )
        self.assertIsNone(
            decode_decision_record(json.dumps(sweeping_record(members=[])), now=NOW)
        )
        self.assertIsNotNone(
            decode_decision_record(
                json.dumps(sweeping_record(members=ordered[:MAXIMUM_COHORT_SIZE])),
                now=NOW,
            )
        )
        self.assertIsNone(
            decode_decision_record(
                json.dumps(sweeping_record(members=ordered)), now=NOW
            )
        )

        too_small = blocked_members(MINIMUM_CONCLUSIVE_COHORT_SIZE - 1)
        self.assertIsNone(
            decode_decision_record(
                json.dumps(cohort_cooldown_record(members=too_small)), now=NOW
            ),
            "a cohort cooldown always carries at least the conclusive minimum",
        )
        self.assertIsNone(
            decode_decision_record(
                json.dumps(cohort_cooldown_record(cohort_size=4)), now=NOW
            ),
            "cohort size can never disagree with the frozen member list",
        )

    def test_cohort_cooldown_requires_complete_coherent_blocked_members(self):
        offered = [
            member(
                index,
                result="offered",
                offer_id=f"{index:032x}",
                offer_expires_epoch=NOW + OFFER_LEASE_SECONDS,
            )
            for index in range(1, MINIMUM_CONCLUSIVE_COHORT_SIZE + 1)
        ]
        unknown = [
            member(
                index,
                result="unknown",
                tested_epoch=NOW,
                offer_id=f"{index:032x}",
                offer_expires_epoch=NOW + OFFER_LEASE_SECONDS,
            )
            for index in range(1, MINIMUM_CONCLUSIVE_COHORT_SIZE + 1)
        ]
        invalid_members = {
            "pending results": [
                member(index) for index in range(1, MINIMUM_CONCLUSIVE_COHORT_SIZE + 1)
            ],
            "offered results": offered,
            "unknown results": unknown,
            "untested blocked result": [
                member(1, **{**blocked_members()[0], "tested_epoch": 0}),
                *blocked_members()[1:],
            ],
            "blocked result without offer id": [
                member(1, **{**blocked_members()[0], "offer_id": ""}),
                *blocked_members()[1:],
            ],
            "blocked result without offer expiry": [
                member(1, **{**blocked_members()[0], "offer_expires_epoch": 0}),
                *blocked_members()[1:],
            ],
            "blocked result after offer expiry": [
                member(
                    1,
                    **{
                        **blocked_members()[0],
                        "offer_expires_epoch": NOW - 1,
                    },
                ),
                *blocked_members()[1:],
            ],
            "blocked result without replay instruction": [
                member(1, **{**blocked_members()[0], "response_instruction": ""}),
                *blocked_members()[1:],
            ],
            "blocked result with legacy replay instruction": [
                member(
                    1,
                    **{
                        **blocked_members()[0],
                        "response_instruction": "legacy_failure",
                    },
                ),
                *blocked_members()[1:],
            ],
            "no cooldown replay instruction": [
                dict(entry, response_instruction="hold") for entry in blocked_members()
            ],
            "multiple cooldown replay instructions": [
                dict(entry, response_instruction="cooldown")
                for entry in blocked_members()
            ],
        }

        self.assertEqual(
            cohort_cooldown_record(),
            decode_decision_record(
                encode_decision_record(cohort_cooldown_record()), now=NOW
            ),
        )
        for name, members in invalid_members.items():
            with self.subTest(case=name):
                record = cohort_cooldown_record(members=members)
                self.assertIsNone(
                    decode_decision_record(json.dumps(record), now=NOW),
                    "malformed terminal members cannot become cohort evidence",
                )
                with self.assertRaises(ValueError):
                    encode_decision_record(record)

    def test_result_instruction_mode_and_reason_enums_are_closed(self):
        for value in ("tested", "clear", "", None, True):
            with self.subTest(result=repr(value)):
                record = sweeping_record(members=[member(1, result=value), member(2)])
                self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))

        for value in ("stale", "fail", None, 0):
            with self.subTest(instruction=repr(value)):
                record = sweeping_record(
                    members=[member(1, response_instruction=value), member(2)]
                )
                self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))

        for value in ("cohort", "", None):
            with self.subTest(mode=repr(value)):
                record = healthy_record(live_offer=live_offer(mode=value))
                self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))

        for value in ("ip_block_suspected", "", None):
            with self.subTest(reason=repr(value)):
                record = individual_record(reason=value)
                self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))

        for value in ("blocked_container", "", None):
            with self.subTest(reason_code=repr(value)):
                record = sweeping_record(reason_code=value)
                self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))

    def test_epoch_fields_reject_negatives_floats_and_booleans(self):
        for value in (-1, 1.0, True, "0", None):
            with self.subTest(epoch=repr(value)):
                self.assertIsNone(
                    decode_decision_record(
                        json.dumps(cohort_cooldown_record(retry_after_epoch=value)),
                        now=NOW,
                    )
                )
                self.assertIsNone(
                    decode_decision_record(
                        json.dumps(
                            sweeping_record(
                                members=[member(1, tested_epoch=value), member(2)]
                            )
                        ),
                        now=NOW,
                    )
                )

    def test_a_sweep_deadline_is_exactly_one_observation_window(self):
        for deadline in (
            NOW + SWEEP_WINDOW_SECONDS - 1,
            NOW + SWEEP_WINDOW_SECONDS + 1,
            NOW,
        ):
            with self.subTest(deadline=deadline):
                record = sweeping_record(deadline_epoch=deadline)
                self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))
                with self.assertRaises(ValueError):
                    encode_decision_record(record)

    def test_rejects_records_larger_than_the_record_byte_limit(self):
        record = sweeping_record()
        encoded = encode_decision_record(record)

        with mock.patch.object(
            sweep_module, "MAXIMUM_COHORT_RECORD_BYTES", len(encoded)
        ):
            self.assertEqual(encoded, encode_decision_record(record))
            self.assertEqual(record, decode_decision_record(encoded, now=NOW))

        with mock.patch.object(
            sweep_module, "MAXIMUM_COHORT_RECORD_BYTES", len(encoded) - 1
        ):
            with self.assertRaises(OverflowError):
                encode_decision_record(record)
            self.assertIsNone(decode_decision_record(encoded, now=NOW))

        oversized = json.dumps({"padding": "x" * MAXIMUM_COHORT_RECORD_BYTES})
        self.assertGreater(len(oversized.encode("utf-8")), MAXIMUM_COHORT_RECORD_BYTES)
        self.assertIsNone(decode_decision_record(oversized, now=NOW))

    def test_unreadable_deep_or_future_rows_never_brick_a_read(self):
        # Built by repetition so the fixture itself never recurses.
        deeply_nested = "[" * 100_000 + "]" * 100_000
        with self.assertRaises(RecursionError):
            json.loads(deeply_nested)

        oversized_integer = '{"schema_version": ' + "9" * 5000 + "}"
        with self.assertRaises(ValueError) as caught:
            json.loads(oversized_integer)
        self.assertNotIsInstance(caught.exception, json.JSONDecodeError)

        unreadable = (
            None,
            "",
            "not json",
            "[]",
            '"text"',
            "null",
            42,
            b'{"schema_version":2}',
            deeply_nested,
            oversized_integer,
            json.dumps(sweeping_record(schema_version=1)),
            json.dumps(sweeping_record(schema_version=3)),
            json.dumps(sweeping_record(schema_version=2.0)),
            json.dumps(sweeping_record(schema_version=True)),
            json.dumps(sweeping_record(state="observing")),
            json.dumps(sweeping_record(state=["sweeping"])),
        )

        for value in unreadable:
            with self.subTest(value=repr(value)[:40]):
                self.assertIsNone(decode_decision_record(value, now=NOW))

    def test_lone_surrogates_cannot_escape_codec_byte_sizing(self):
        self.assertIsNone(decode_decision_record("\ud800", now=NOW))

        with mock.patch.object(sweep_module.json, "dumps", return_value="\ud800"):
            with self.assertRaises((ValueError, OverflowError)) as caught:
                encode_decision_record(sweeping_record())

        self.assertNotIsInstance(caught.exception, UnicodeEncodeError)

    def test_expiry_boundaries_are_exact(self):
        sweeping = sweeping_record()
        encoded = encode_decision_record(sweeping)
        deadline = NOW + SWEEP_WINDOW_SECONDS

        for now, expired in (
            (deadline - 1, False),
            (deadline, False),
            (deadline + 1, True),
        ):
            with self.subTest(now=now):
                decoded = decode_decision_record(encoded, now=now)
                # An expired sweep still owns its evidence: only a transition may
                # end it, so a read may never shrink it back to "available".
                self.assertEqual(sweeping, decoded)
                self.assertIs(expired, decision_snapshot(decoded, now=now)["expired"])

        cooldown = encode_decision_record(cohort_cooldown_record())
        self.assertIsNotNone(decode_decision_record(cooldown, now=COOLDOWN_RETRY - 1))
        self.assertIsNone(decode_decision_record(cooldown, now=COOLDOWN_RETRY))
        self.assertIsNone(decode_decision_record(cooldown, now=COOLDOWN_RETRY + 1))

        legacy = encode_decision_record(legacy_cooldown_record())
        self.assertIsNotNone(decode_decision_record(legacy, now=COOLDOWN_RETRY - 1))
        self.assertIsNone(decode_decision_record(legacy, now=COOLDOWN_RETRY))

        for suppressed in (healthy_record(), individual_record()):
            with self.subTest(state=suppressed["state"]):
                encoded = encode_decision_record(suppressed)
                self.assertIsNotNone(decode_decision_record(encoded, now=UNTIL - 1))
                self.assertIsNone(decode_decision_record(encoded, now=UNTIL))
                self.assertIsNone(decode_decision_record(encoded, now=UNTIL + 1))


class AcceptedOfferCodecTests(unittest.TestCase):
    """The replay history is separate persisted state, so it is validated too."""

    def cohort_states(self):
        return (
            ("sweeping", sweeping_record),
            ("cooldown", cohort_cooldown_record),
            ("healthy", healthy_record),
            ("individual", individual_record),
        )

    def test_every_cohort_state_round_trips_an_accepted_offer(self):
        entry = accepted_offer(1)
        for name, build in self.cohort_states():
            with self.subTest(state=name):
                record = build(accepted_offers=[entry])
                record["used_offer_ids"] = used_ids(
                    record["used_offer_ids"], (entry["offer_id"],)
                )
                encoded = encode_decision_record(record)

                self.assertEqual(record, decode_decision_record(encoded, now=NOW))
                self.assertNotIn("://", encoded)

    def test_an_accepted_offer_retains_the_complete_accepted_response(self):
        entry = accepted_offer(
            1,
            state="sweeping",
            hold_type="provisional",
            evidence_count=1,
            retry_after_epoch=NOW + SWEEP_WINDOW_SECONDS,
        )
        record = sweeping_record(
            accepted_offers=[entry], used_offer_ids=[entry["offer_id"]]
        )

        encoded = encode_decision_record(record)

        self.assertEqual(record, decode_decision_record(encoded, now=NOW))

    def test_an_accepted_offer_pins_its_exact_key_set(self):
        for key in ACCEPTED_OFFER_KEYS:
            with self.subTest(accepted_key=key):
                broken = {k: v for k, v in accepted_offer(1).items() if k != key}
                record = sweeping_record(
                    accepted_offers=[broken], used_offer_ids=[f"{1:032x}"]
                )

                self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))
                with self.assertRaises(ValueError):
                    encode_decision_record(record)

        extra = sweeping_record(
            accepted_offers=[dict(accepted_offer(1), url="https://x.invalid")],
            used_offer_ids=[f"{1:032x}"],
        )
        self.assertIsNone(decode_decision_record(json.dumps(extra), now=NOW))
        with self.assertRaises(ValueError):
            encode_decision_record(extra)

    def test_accepted_offers_are_unique_and_ascending_by_fingerprint(self):
        rejected = (
            [accepted_offer(1), accepted_offer(1, offer_id=f"{9:032x}")],
            [accepted_offer(2), accepted_offer(1)],
            [accepted_offer(1), accepted_offer(3), accepted_offer(2)],
        )

        for entries in rejected:
            with self.subTest(order=[e["link_fingerprint"][-2:] for e in entries]):
                record = sweeping_record(
                    accepted_offers=entries,
                    used_offer_ids=used_ids(e["offer_id"] for e in entries),
                )
                self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))
                with self.assertRaises(ValueError):
                    encode_decision_record(record)

    def test_one_offer_identity_can_never_be_both_accepted_and_still_leased(self):
        accepted = accepted_offer(1, offer_id=OFFER_ID)
        records = (
            healthy_record(accepted_offers=[accepted], used_offer_ids=[OFFER_ID]),
            sweeping_record(
                members=[
                    member(
                        1,
                        result="offered",
                        offer_id=OFFER_ID,
                        offer_expires_epoch=NOW + OFFER_LEASE_SECONDS,
                    ),
                    member(2),
                ],
                accepted_offers=[accepted],
                used_offer_ids=[OFFER_ID],
            ),
        )

        for record in records:
            with self.subTest(state=record["state"]):
                self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))
                with self.assertRaises(ValueError):
                    encode_decision_record(record)

    def test_one_offer_identity_cannot_be_rebound_to_another_fingerprint(self):
        accepted = accepted_offer(1, offer_id=OFFER_ID)
        record = sweeping_record(
            members=[
                member(1),
                member(
                    2,
                    result="offered",
                    offer_id=OFFER_ID,
                    offer_expires_epoch=NOW + OFFER_LEASE_SECONDS,
                ),
            ],
            accepted_offers=[accepted],
            used_offer_ids=[OFFER_ID],
        )

        self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))
        with self.assertRaises(ValueError):
            encode_decision_record(record)

    def test_a_member_shared_identity_must_be_its_accepted_sweep_result(self):
        invalid = (
            (
                "probe identity reused by a member",
                member(
                    1,
                    result="blocked",
                    tested_epoch=NOW,
                    offer_id=OFFER_ID,
                    offer_expires_epoch=NOW + OFFER_LEASE_SECONDS,
                    response_instruction="hold",
                ),
                accepted_offer(1, offer_id=OFFER_ID, mode="probe"),
            ),
            (
                "accepted identity retained by a pending member",
                member(1, offer_id=OFFER_ID),
                accepted_offer(1, offer_id=OFFER_ID),
            ),
        )

        for label, first, accepted in invalid:
            with self.subTest(case=label):
                record = sweeping_record(
                    members=[first, member(2)],
                    accepted_offers=[accepted],
                    used_offer_ids=[OFFER_ID],
                )
                self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))
                with self.assertRaises(ValueError):
                    encode_decision_record(record)

    def test_a_record_level_live_offer_cannot_reuse_a_member_identity(self):
        members = blocked_members()
        accepted = [accepted_offer(index) for index in range(2, len(members) + 1)]
        record = cohort_cooldown_record(
            members=members,
            accepted_offers=accepted,
            live_offer=live_offer(offer_id=members[0]["offer_id"], mode="probe"),
        )

        self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))
        with self.assertRaises(ValueError):
            encode_decision_record(record)

    def test_accepted_offer_enums_and_epochs_are_closed(self):
        rejected = {
            "mode": ("cohort", "", None),
            "state": ("available", "observing", "", None),
            "instruction": ("stale", "fail", None),
            "accepted": ("clear", "blocked", None),
            "cleared": ("true", 1, None),
            "hold_type": ("hold", "cooldown", "", None),
            "evidence_count": (-1, 1.0, True, "1", None),
            "retry_after_epoch": (-1, 1.0, True, "1", None),
            "sweep_tested": (-1, 1.0, True, "1", None),
            "sweep_total": (-1, 1.0, True, None),
            "sweep_deadline_epoch": (-1, 1.0, True, None),
        }

        for field, values in rejected.items():
            for value in values:
                with self.subTest(field=field, value=repr(value)):
                    record = sweeping_record(
                        accepted_offers=[accepted_offer(1, **{field: value})],
                        used_offer_ids=[f"{1:032x}"],
                    )
                    self.assertIsNone(
                        decode_decision_record(json.dumps(record), now=NOW)
                    )

    def test_accepted_response_fields_must_describe_one_coherent_outcome(self):
        rejected = (
            accepted_offer(1, cleared=True),
            accepted_offer(1, accepted="unknown", instruction="hold"),
            accepted_offer(1, instruction="hold", hold_type="none"),
            accepted_offer(1, instruction="hold", retry_after_epoch=0),
            accepted_offer(
                1,
                state="sweeping",
                instruction="cooldown",
                hold_type="crypter_cooldown",
            ),
            accepted_offer(1, instruction="legacy_failure", hold_type="provisional"),
            accepted_offer(1, instruction="legacy_failure", retry_after_epoch=NOW),
        )

        for entry in rejected:
            with self.subTest(entry=entry):
                record = sweeping_record(
                    accepted_offers=[entry], used_offer_ids=[entry["offer_id"]]
                )
                self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))
                with self.assertRaises(ValueError):
                    encode_decision_record(record)

    def test_accepted_offers_are_bounded_by_the_cohort_maximum(self):
        entries = [
            accepted_offer(index) for index in range(1, MAXIMUM_ACCEPTED_OFFERS + 2)
        ]
        identifiers = used_ids(entry["offer_id"] for entry in entries)

        self.assertIsNotNone(
            decode_decision_record(
                json.dumps(
                    sweeping_record(
                        accepted_offers=entries[:MAXIMUM_ACCEPTED_OFFERS],
                        used_offer_ids=identifiers[:MAXIMUM_ACCEPTED_OFFERS],
                    )
                ),
                now=NOW,
            )
        )
        self.assertIsNone(
            decode_decision_record(
                json.dumps(
                    sweeping_record(accepted_offers=entries, used_offer_ids=identifiers)
                ),
                now=NOW,
            )
        )

    def test_used_offer_ids_are_unique_ascending_and_bounded(self):
        rejected = (
            [f"{2:032x}", f"{1:032x}"],
            [f"{1:032x}", f"{1:032x}"],
            ["A" * 32],
            ["a" * 31],
            [None],
            "not-a-list",
        )

        for identifiers in rejected:
            with self.subTest(used=repr(identifiers)[:40]):
                record = sweeping_record(used_offer_ids=identifiers)
                self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))
                with self.assertRaises(ValueError):
                    encode_decision_record(record)

        full = [f"{index:032x}" for index in range(1, MAXIMUM_GENERATION_OFFER_IDS + 2)]
        self.assertIsNotNone(
            decode_decision_record(
                json.dumps(
                    sweeping_record(used_offer_ids=full[:MAXIMUM_GENERATION_OFFER_IDS])
                ),
                now=NOW,
            )
        )
        self.assertIsNone(
            decode_decision_record(
                json.dumps(sweeping_record(used_offer_ids=full)), now=NOW
            )
        )

    def test_a_cohort_cooldown_retains_the_sweep_window_it_was_won_in(self):
        for opened, deadline in (
            (NOW, NOW + SWEEP_WINDOW_SECONDS - 1),
            (NOW, NOW + SWEEP_WINDOW_SECONDS + 1),
            (NOW, NOW),
        ):
            with self.subTest(deadline=deadline):
                record = cohort_cooldown_record(
                    opened_epoch=opened, deadline_epoch=deadline
                )
                self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))
                with self.assertRaises(ValueError):
                    encode_decision_record(record)

    def test_a_cohort_cooldown_rejects_members_tested_outside_its_window(self):
        week = 7 * 24 * 60 * 60
        outside = {
            "weeks apart": [
                dict(entry, tested_epoch=NOW + index * week)
                for index, entry in enumerate(blocked_members())
            ],
            "tested before the sweep opened": [
                dict(blocked_members()[0], tested_epoch=NOW - 1),
                *blocked_members()[1:],
            ],
            "tested after the deadline": [
                dict(
                    blocked_members()[0],
                    tested_epoch=NOW + SWEEP_WINDOW_SECONDS + 1,
                    offer_expires_epoch=NOW + SWEEP_WINDOW_SECONDS + 1,
                ),
                *blocked_members()[1:],
            ],
        }

        for name, members in outside.items():
            with self.subTest(case=name):
                record = cohort_cooldown_record(members=members)
                self.assertIsNone(
                    decode_decision_record(json.dumps(record), now=NOW),
                    "a late or ancient member result can never cool a linkcrypter",
                )
                with self.assertRaises(ValueError):
                    encode_decision_record(record)

        self.assertIsNotNone(
            decode_decision_record(
                json.dumps(
                    cohort_cooldown_record(
                        members=[
                            dict(
                                entry,
                                tested_epoch=NOW + SWEEP_WINDOW_SECONDS,
                                offer_expires_epoch=NOW + SWEEP_WINDOW_SECONDS,
                            )
                            for entry in blocked_members()
                        ]
                    )
                ),
                now=NOW,
            ),
            "a member tested exactly at the deadline is still coherent evidence",
        )

    def test_a_cohort_cooldown_requires_unique_offer_ids_across_its_history(self):
        record = cohort_cooldown_record()
        record["used_offer_ids"] = [
            entry
            for entry in record["used_offer_ids"]
            if entry != record["members"][0]["offer_id"]
        ]
        self.assertIsNone(
            decode_decision_record(json.dumps(record), now=NOW),
            "a member result with no leased identity in the history is incoherent",
        )
        with self.assertRaises(ValueError):
            encode_decision_record(record)

        reused = cohort_cooldown_record(
            members=[
                dict(entry, offer_id=f"{1:032x}") for entry in blocked_members()[:1]
            ]
            + [dict(entry, offer_id=f"{1:032x}") for entry in blocked_members()[1:2]]
            + blocked_members()[2:]
        )
        self.assertIsNone(
            decode_decision_record(json.dumps(reused), now=NOW),
            "two members can never share one offer identity",
        )

    def test_hold_fingerprints_are_only_retained_by_the_fail_closed_reason(self):
        allowed = individual_record(
            reason="inventory_unavailable", hold_fingerprints=[fingerprint(1)]
        )
        self.assertEqual(
            allowed, decode_decision_record(encode_decision_record(allowed), now=NOW)
        )

        for reason in (
            "cohort_too_small",
            "cohort_oversized",
            "sweep_expired",
            "sweep_inconclusive",
            "legacy_v1_hold",
        ):
            with self.subTest(reason=reason):
                record = individual_record(
                    reason=reason, hold_fingerprints=[fingerprint(1)]
                )
                self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))
                with self.assertRaises(ValueError):
                    encode_decision_record(record)

    def test_hold_fingerprints_are_unique_ascending_and_bounded(self):
        rejected = (
            [fingerprint(2), fingerprint(1)],
            [fingerprint(1), fingerprint(1)],
            ["a" * 63],
            "not-a-list",
            [fingerprint(index) for index in range(1, MAXIMUM_COHORT_SIZE + 2)],
        )

        for value in rejected:
            with self.subTest(hold=repr(value)[:40]):
                record = individual_record(
                    reason="inventory_unavailable", hold_fingerprints=value
                )
                self.assertIsNone(decode_decision_record(json.dumps(record), now=NOW))
                with self.assertRaises(ValueError):
                    encode_decision_record(record)


class DecisionSnapshotTests(unittest.TestCase):
    def base_snapshot(self, **overrides):
        snapshot = {
            "state": "available",
            "reason_code": "",
            "legacy_cooldown": False,
            "sweep_id": "",
            "generation_id": "",
            "opened_epoch": 0,
            "sweep_deadline_epoch": 0,
            "sweep_total": 0,
            "sweep_tested": 0,
            "retry_after_epoch": 0,
            "until_epoch": 0,
            "evidence_count": 0,
            "expired": False,
            "retest_members": (),
            "hold_fingerprints": (),
            "live_offer": None,
        }
        snapshot.update(overrides)
        return snapshot

    def test_missing_and_unusable_records_project_as_available(self):
        for record in (None, {}, "sweeping", [], {"state": "unknown"}):
            with self.subTest(record=repr(record)):
                self.assertEqual(
                    self.base_snapshot(), decision_snapshot(record, now=NOW)
                )

    def test_projects_each_state_into_one_fixed_shape(self):
        members = [
            member(1, result="blocked", tested_epoch=NOW),
            member(2, result="unknown", tested_epoch=NOW),
            member(3, result="offered"),
            member(4),
        ]

        self.assertEqual(
            self.base_snapshot(
                state="sweeping",
                reason_code=REASON,
                sweep_id=SWEEP_ID,
                opened_epoch=NOW,
                sweep_deadline_epoch=NOW + SWEEP_WINDOW_SECONDS,
                sweep_total=4,
                sweep_tested=2,
                evidence_count=1,
            ),
            decision_snapshot(sweeping_record(members=members), now=NOW),
        )
        self.assertEqual(
            self.base_snapshot(
                state="cooldown",
                reason_code=REASON,
                sweep_id=SWEEP_ID,
                sweep_total=MINIMUM_CONCLUSIVE_COHORT_SIZE,
                sweep_tested=MINIMUM_CONCLUSIVE_COHORT_SIZE,
                retry_after_epoch=COOLDOWN_RETRY,
                evidence_count=MINIMUM_CONCLUSIVE_COHORT_SIZE,
                live_offer=live_offer(mode="probe"),
            ),
            decision_snapshot(cohort_cooldown_record(), now=NOW),
        )
        self.assertEqual(
            self.base_snapshot(
                state="cooldown",
                reason_code=REASON,
                legacy_cooldown=True,
                retry_after_epoch=COOLDOWN_RETRY,
                evidence_count=3,
            ),
            decision_snapshot(legacy_cooldown_record(), now=NOW),
        )
        self.assertEqual(
            self.base_snapshot(
                state="healthy",
                sweep_id=SWEEP_ID,
                until_epoch=UNTIL,
                retest_members=(fingerprint(1), fingerprint(2)),
                live_offer=live_offer(mode="retest", response_instruction=""),
            ),
            decision_snapshot(healthy_record(), now=NOW),
        )
        self.assertEqual(
            self.base_snapshot(
                state="individual",
                reason_code="cohort_too_small",
                generation_id=GENERATION_ID,
                until_epoch=UNTIL,
                live_offer=live_offer(
                    mode="individual", response_instruction="legacy_failure"
                ),
            ),
            decision_snapshot(individual_record(), now=NOW),
        )

    def test_a_projection_never_aliases_the_record(self):
        record = healthy_record()
        snapshot = decision_snapshot(record, now=NOW)

        snapshot["live_offer"]["offer_id"] = "0" * 32
        self.assertEqual(OFFER_ID, record["live_offer"]["offer_id"])
        self.assertIsInstance(snapshot["retest_members"], tuple)


class LegacyMigrationTests(unittest.TestCase):
    def legacy_record(self, state, observation_epochs, retry_after_epoch=0):
        observations = [
            {
                "package_id": f"Quasarr_movies_{index:032x}",
                "link_fingerprint": fingerprint(index),
                "seen_at_epoch": seen_at,
            }
            for index, seen_at in enumerate(observation_epochs, start=1)
        ]
        return json.dumps(
            {
                "state": state,
                "reason_code": REASON,
                "first_seen_epoch": min(observation_epochs, default=0),
                "last_seen_epoch": max(observation_epochs, default=0),
                "retry_after_epoch": retry_after_epoch,
                "observations": observations,
            }
        )

    def test_an_old_observing_row_is_dropped_and_never_promoted_to_cooldown(self):
        # The stored state decides, never the retry deadline: a row carrying a
        # live deadline while still observing is evidence of nothing.
        for count in (1, 2, 3, 4):
            for retry_after_epoch in (0, COOLDOWN_RETRY):
                with self.subTest(observations=count, retry=retry_after_epoch):
                    value = self.legacy_record(
                        "observing",
                        [NOW - 60] * count,
                        retry_after_epoch=retry_after_epoch,
                    )

                    self.assertIsNone(migrate_legacy_record(value, now=NOW))

    def test_an_old_cooldown_row_becomes_a_marked_legacy_cooldown(self):
        value = self.legacy_record(
            "cooldown",
            [NOW - 300, NOW - 200, NOW - 100],
            retry_after_epoch=COOLDOWN_RETRY,
        )

        migrated = migrate_legacy_record(value, now=NOW)

        self.assertEqual(
            {
                "schema_version": SWEEP_SCHEMA_VERSION,
                "state": "cooldown",
                "reason_code": REASON,
                "legacy_cooldown": True,
                "retry_after_epoch": COOLDOWN_RETRY,
                "legacy_evidence_count": 3,
            },
            migrated,
        )
        self.assertEqual(
            migrated,
            decode_decision_record(encode_decision_record(migrated), now=NOW),
        )

    def test_only_genuinely_unversioned_rows_use_legacy_migration(self):
        legacy = json.loads(
            self.legacy_record("cooldown", [NOW - 60], retry_after_epoch=COOLDOWN_RETRY)
        )
        self.assertIsNotNone(
            migrate_legacy_record(
                json.dumps(dict(legacy, additive_legacy_field="preserved")), now=NOW
            ),
            "unversioned legacy rows retain their additive-field compatibility",
        )

        for version in (2, 3, 99, None, True, "3"):
            with self.subTest(schema_version=repr(version)):
                hybrid = dict(legacy, schema_version=version)
                self.assertIsNone(
                    migrate_legacy_record(json.dumps(hybrid), now=NOW),
                    "a versioned row must never fall back to the legacy decoder",
                )

    def test_migration_counts_surviving_evidence_and_floors_it_at_one(self):
        value = self.legacy_record(
            "cooldown",
            [NOW - 20 * 60, NOW - 19 * 60, NOW - 60],
            retry_after_epoch=COOLDOWN_RETRY,
        )

        self.assertEqual(
            1, migrate_legacy_record(value, now=NOW)["legacy_evidence_count"]
        )

        stale = self.legacy_record(
            "cooldown", [NOW - 48 * 60 * 60], retry_after_epoch=COOLDOWN_RETRY
        )
        self.assertEqual(
            1, migrate_legacy_record(stale, now=NOW)["legacy_evidence_count"]
        )

    def test_expired_and_unreadable_legacy_rows_migrate_to_nothing(self):
        expired = self.legacy_record("cooldown", [NOW - 60], retry_after_epoch=NOW)
        deeply_nested = "[" * 100_000 + "]" * 100_000
        unreadable = (
            None,
            "",
            "not json",
            "[]",
            deeply_nested,
            '{"schema_version": ' + "9" * 5000 + "}",
            json.dumps({"state": "cooldown"}),
            json.dumps(legacy_cooldown_record()),
            json.dumps(sweeping_record()),
            expired,
        )

        for value in unreadable:
            with self.subTest(value=repr(value)[:40]):
                self.assertIsNone(migrate_legacy_record(value, now=NOW))

        self.assertIsNotNone(
            migrate_legacy_record(
                self.legacy_record("cooldown", [NOW - 60], retry_after_epoch=NOW + 1),
                now=NOW,
            )
        )


class HelperPackagePredicateTests(unittest.TestCase):
    def candidate_package(self, **overrides):
        package = {
            "title": "Synthetic.Release.2026.German.1080p",
            "password": "",
            "links": [["https://filecrypt.invalid/Container/1", "filecrypt"]],
        }
        package.update(overrides)
        return package

    def test_accepts_only_packages_the_handout_can_actually_offer(self):
        package = self.candidate_package()

        self.assertIs(True, helper_package_is_candidate(package))
        self.assertIs(
            True,
            helper_package_is_candidate(
                dict(package, deferred={"crypter": "filecrypt"}, size_mb=1)
            ),
        )

        rejected = {
            "missing title": {k: v for k, v in package.items() if k != "title"},
            "missing password": {k: v for k, v in package.items() if k != "password"},
            "missing links": {k: v for k, v in package.items() if k != "links"},
            "empty links": dict(package, links=[]),
            "non-list links": dict(package, links="https://filecrypt.invalid/1"),
            "mapping links": dict(package, links={"0": "link"}),
            "disabled": dict(package, disabled=True),
            "disabled false": dict(package, disabled=False),
            "not a mapping": ["title", "password", "links"],
            "encoded json": json.dumps(package),
            "none": None,
            "number": 42,
        }

        for name, value in rejected.items():
            with self.subTest(package=name):
                self.assertIs(False, helper_package_is_candidate(value))


class ServiceSnapshotTests(unittest.TestCase):
    class FakeDatabase:
        def __init__(self):
            self.rows = {}
            self.mutation_count = 0
            self.before_mutation: Callable[[], None] | None = None

        def retrieve(self, key):
            return self.rows.get(key)

        def mutate_value(self, key, mutator):
            self.mutation_count += 1
            if self.before_mutation is not None:
                before_mutation = self.before_mutation
                self.before_mutation = None
                before_mutation()
            value = mutator(self.rows.get(key))
            if value is None:
                self.rows.pop(key, None)
            else:
                self.rows[key] = value
            return value

    class FakeSharedState:
        def __init__(self, databases):
            self.values = {"crypter_cooldown_hours": 24}
            self.databases = databases

        def get_db(self, table):
            return self.databases[table]

    def service(self, stored=None, now=NOW):
        self.database = self.FakeDatabase()
        if stored is not None:
            self.database.rows["filecrypt"] = stored
        shared_state = self.FakeSharedState({"crypter_cooldowns": self.database})
        return CrypterCooldownService(shared_state, clock=lambda: now)

    def legacy_snapshot(self, **overrides):
        snapshot = {
            "state": "available",
            "reason_code": None,
            "first_seen_epoch": 0,
            "last_seen_epoch": 0,
            "retry_after_epoch": 0,
            "observations": [],
            "evidence_count": 0,
        }
        snapshot.update(overrides)
        return snapshot

    def legacy_cooldown_value(self, **overrides):
        record = {
            "state": "cooldown",
            "reason_code": REASON,
            "first_seen_epoch": NOW - 60,
            "last_seen_epoch": NOW - 60,
            "retry_after_epoch": COOLDOWN_RETRY,
            "observations": [
                {
                    "package_id": "Quasarr_movies_00000000000000000000000000000001",
                    "link_fingerprint": fingerprint(1),
                    "seen_at_epoch": NOW - 60,
                }
            ],
        }
        record.update(overrides)
        return json.dumps(record)

    def test_version_two_rows_project_into_the_existing_snapshot_keys(self):
        cases = (
            (
                cohort_cooldown_record(),
                self.legacy_snapshot(
                    state="cooldown",
                    reason_code=REASON,
                    retry_after_epoch=COOLDOWN_RETRY,
                    evidence_count=MINIMUM_CONCLUSIVE_COHORT_SIZE,
                ),
            ),
            (
                legacy_cooldown_record(),
                self.legacy_snapshot(
                    state="cooldown",
                    reason_code=REASON,
                    retry_after_epoch=COOLDOWN_RETRY,
                    evidence_count=3,
                ),
            ),
            (
                sweeping_record(members=[member(1, result="blocked"), member(2)]),
                self.legacy_snapshot(
                    state="observing",
                    reason_code=REASON,
                    first_seen_epoch=NOW,
                    last_seen_epoch=NOW,
                    evidence_count=1,
                ),
            ),
            (healthy_record(), self.legacy_snapshot()),
            (individual_record(), self.legacy_snapshot()),
        )

        for record, expected in cases:
            with self.subTest(state=record["state"]):
                encoded = encode_decision_record(record)
                service = self.service(encoded)

                self.assertEqual(expected, service.snapshot("filecrypt"))
                self.assertIs(
                    expected["state"] == "cooldown", service.is_cooling("filecrypt")
                )
                self.assertEqual(
                    expected["retry_after_epoch"], service.retry_after("filecrypt")
                )
                # A valid current row is a pure read: no self-heal, no rewrite.
                self.assertEqual(0, self.database.mutation_count)
                self.assertEqual(encoded, self.database.rows["filecrypt"])

    def test_legacy_rows_keep_their_current_snapshot_and_are_never_promoted(self):
        observations = [
            {
                "package_id": f"Quasarr_movies_{index:032x}",
                "link_fingerprint": fingerprint(index),
                "seen_at_epoch": NOW - 60,
            }
            for index in (1, 2)
        ]
        stored = json.dumps(
            {
                "state": "observing",
                "reason_code": REASON,
                "first_seen_epoch": NOW - 60,
                "last_seen_epoch": NOW - 60,
                "retry_after_epoch": 0,
                "observations": observations,
            }
        )
        service = self.service(stored)

        self.assertEqual(
            self.legacy_snapshot(
                state="observing",
                reason_code=REASON,
                first_seen_epoch=NOW - 60,
                last_seen_epoch=NOW - 60,
                observations=observations,
                evidence_count=2,
            ),
            service.snapshot("filecrypt"),
        )
        self.assertFalse(service.is_cooling("filecrypt"))
        self.assertEqual(0, self.database.mutation_count)
        self.assertEqual(stored, self.database.rows["filecrypt"])

    def test_snapshot_never_falls_back_from_versioned_to_legacy_fields(self):
        legacy = json.loads(self.legacy_cooldown_value())

        additive = self.service(
            json.dumps(dict(legacy, additive_legacy_field="allowed"))
        ).snapshot("filecrypt")
        self.assertEqual("cooldown", additive["state"])
        self.assertEqual(1, additive["evidence_count"])
        self.assertEqual(0, self.database.mutation_count)

        for version in (2, 3, 99, None, True, "3"):
            with self.subTest(schema_version=repr(version)):
                service = self.service(json.dumps(dict(legacy, schema_version=version)))
                with mock.patch.object(cooldown_module, "warn") as warn:
                    self.assertEqual(
                        self.legacy_snapshot(), service.snapshot("filecrypt")
                    )

                self.assertEqual(1, self.database.mutation_count)
                self.assertNotIn("filecrypt", self.database.rows)
                warn.assert_called_once()

    def test_cleanup_preserves_a_concurrently_installed_version_two_row(self):
        concurrent_value = encode_decision_record(cohort_cooldown_record())
        future_hybrid = json.loads(self.legacy_cooldown_value())
        future_hybrid["schema_version"] = 3
        initial_rows = {
            "invalid": "malformed-sensitive-marker",
            "expired legacy": self.legacy_cooldown_value(retry_after_epoch=NOW),
            "future hybrid": json.dumps(future_hybrid),
        }

        for name, stored in initial_rows.items():
            with self.subTest(initial=name):
                service = self.service(stored)
                self.database.before_mutation = lambda: self.database.rows.__setitem__(
                    "filecrypt", concurrent_value
                )

                with mock.patch.object(cooldown_module, "warn") as warn:
                    snapshot = service.snapshot("filecrypt")

                self.assertEqual(
                    self.legacy_snapshot(
                        state="cooldown",
                        reason_code=REASON,
                        retry_after_epoch=COOLDOWN_RETRY,
                        evidence_count=MINIMUM_CONCLUSIVE_COHORT_SIZE,
                    ),
                    snapshot,
                )
                self.assertEqual(1, self.database.mutation_count)
                self.assertEqual(concurrent_value, self.database.rows["filecrypt"])
                warn.assert_not_called()

    def test_unreadable_and_expired_version_two_rows_self_heal_on_read(self):
        deeply_nested = "[" * 100_000 + "]" * 100_000
        broken_member = sweeping_record(
            members=[{k: v for k, v in member(1).items() if k != "result"}, member(2)]
        )
        unreadable = (
            deeply_nested,
            '{"schema_version": ' + "9" * 5000 + "}",
            json.dumps(sweeping_record(schema_version=3)),
            json.dumps(broken_member),
        )
        # A structurally valid record whose own window merely ended is a clean
        # expiry, not a discarded row, so it self-heals without the warning the
        # legacy reader emits for genuinely unreadable state.
        expired = encode_decision_record(cohort_cooldown_record())

        for row, expected_warnings in [(row, 1) for row in unreadable] + [(expired, 0)]:
            with self.subTest(row=repr(row)[:40]):
                service = self.service(row, now=COOLDOWN_RETRY)
                with mock.patch.object(cooldown_module, "warn") as warn:
                    self.assertEqual(
                        self.legacy_snapshot(), service.snapshot("filecrypt")
                    )

                self.assertEqual(1, self.database.mutation_count)
                self.assertNotIn("filecrypt", self.database.rows)
                self.assertEqual(expected_warnings, warn.call_count)


if __name__ == "__main__":
    unittest.main()
