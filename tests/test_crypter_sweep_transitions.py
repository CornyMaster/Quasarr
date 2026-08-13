# -*- coding: utf-8 -*-

import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from quasarr.providers import shared_state as provider_shared_state
from quasarr.providers.crypter_candidates import (
    FilecryptCandidate,
    FilecryptInventory,
    FilecryptOccurrence,
)
from quasarr.providers.crypter_cooldowns import (
    CRYPTER_EVENT_KEY,
    CRYPTER_EVENT_TABLE,
    CrypterCooldownService,
    decode_package_defer,
    package_defer_covers_fingerprint,
    package_defer_is_active,
)
from quasarr.providers.crypter_sweeps import (
    HEALTHY_SUPPRESSION_SECONDS,
    MAXIMUM_COHORT_SIZE,
    MAXIMUM_GENERATION_OFFER_IDS,
    MAXIMUM_OFFER_ID_ATTEMPTS,
    MINIMUM_CONCLUSIVE_COHORT_SIZE,
    OFFER_LEASE_SECONDS,
    OVERSIZED_COHORT_SENTINEL,
    SWEEP_WINDOW_SECONDS,
    Offer,
    decision_snapshot,
    decode_decision_record,
    encode_decision_record,
    expire_decision,
    lease_next_offer,
    prepare_decision,
    record_access,
    record_blocked,
    record_legacy_report,
)
from quasarr.storage.sqlite_database import DataBase

CRYPTER = "filecrypt"
REASON = "ip_block_suspected"
NOW = 1_700_000_000
DEADLINE = NOW + SWEEP_WINDOW_SECONDS
COOLDOWN_SECONDS = 24 * 60 * 60
SWEEP_ID = "a" * 32
OTHER_SWEEP_ID = "b" * 32
NO_EVENTS = {"observations": 0, "cooldowns": 0, "probes": 0}


def fingerprint(index):
    """A synthetic 64-character lowercase-hex link fingerprint."""
    return f"{index:064d}"


def offer_id(index):
    return f"{index:032d}"


def package(index):
    return f"Quasarr_movies_{index:032d}"


class SequentialIds:
    """A deterministic ID factory; hermetic tests never mint random IDs."""

    def __init__(self, start=900):
        self.start = start
        self.minted = []

    def __call__(self):
        self.start += 1
        value = offer_id(self.start)
        self.minted.append(value)
        return value


def occurrence(package_id, link_fingerprint, link_index=0):
    return FilecryptOccurrence(
        package_id=package_id,
        link_index=link_index,
        link=[f"https://filecrypt.invalid/container/{link_index}", CRYPTER],
        fingerprint=link_fingerprint,
    )


def build_inventory(members, *, oversized=False):
    """`members` maps a fingerprint to the package IDs that still carry it."""
    candidates = tuple(
        FilecryptCandidate(
            fingerprint=link_fingerprint,
            occurrences=tuple(
                occurrence(package_id, link_fingerprint, index)
                for index, package_id in enumerate(package_ids)
            ),
        )
        for link_fingerprint, package_ids in sorted(members.items())
    )
    return FilecryptInventory(candidates=candidates, oversized=oversized)


def cohort_inventory(size, *, start=1, oversized=False):
    return build_inventory(
        {fingerprint(index): (package(index),) for index in range(start, start + size)},
        oversized=oversized,
    )


def member(
    index, result="pending", *, tested=0, offer=None, instruction="", expires=None
):
    return {
        "link_fingerprint": fingerprint(index),
        "result": result,
        "tested_epoch": tested,
        "offer_id": "" if offer is None else offer,
        "offer_expires_epoch": 0
        if offer is None
        else ((tested or NOW) + OFFER_LEASE_SECONDS if expires is None else expires),
        "response_instruction": instruction,
    }


def blocked_member(index, *, tested=NOW, offer=None, instruction="hold"):
    return member(
        index,
        "blocked",
        tested=tested,
        offer=offer if offer is not None else offer_id(index),
        instruction=instruction,
    )


def accepted_offer(index, **overrides):
    entry = {
        "offer_id": offer_id(index),
        "link_fingerprint": fingerprint(index),
        "mode": "sweep",
        "outcome": "blocked",
        "state": "sweeping",
        "instruction": "hold",
        "accepted": "",
        "cleared": False,
        "hold_type": "provisional",
        "evidence_count": index,
        "retry_after_epoch": DEADLINE,
        "sweep_tested": index,
        "sweep_total": 5,
        "sweep_deadline_epoch": DEADLINE,
    }
    entry.update(overrides)
    return entry


def generation_ids(members=(), accepted=(), live_offer=None, extra=()):
    """Every offer ID one generation minted, ascending and unique."""
    identifiers = {entry["offer_id"] for entry in members if entry["offer_id"]}
    identifiers.update(entry["offer_id"] for entry in accepted)
    identifiers.update(extra)
    if live_offer is not None:
        identifiers.add(live_offer["offer_id"])
    return sorted(identifiers)


def accepted_for(members):
    """The replay history a fully reported cohort leaves behind."""
    return [
        accepted_offer(
            index + 1,
            instruction=entry["response_instruction"],
            sweep_tested=index + 1,
            sweep_total=len(members),
            **(
                {
                    "state": "cooldown",
                    "hold_type": "crypter_cooldown",
                    "evidence_count": len(members),
                    "retry_after_epoch": NOW + COOLDOWN_SECONDS,
                }
                if entry["response_instruction"] == "cooldown"
                else {}
            ),
        )
        for index, entry in enumerate(members)
        if entry["result"] == "blocked"
    ]


def sweeping_record(size=5, *, sweep_id=SWEEP_ID, opened=NOW, members=None, **extra):
    members = (
        [member(index) for index in range(1, size + 1)] if members is None else members
    )
    accepted = extra.pop("accepted_offers", [])
    record = {
        "schema_version": 2,
        "state": "sweeping",
        "reason_code": REASON,
        "sweep_id": sweep_id,
        "opened_epoch": opened,
        "deadline_epoch": opened + SWEEP_WINDOW_SECONDS,
        "members": members,
        "accepted_offers": accepted,
        "used_offer_ids": generation_ids(members, accepted),
    }
    record.update(extra)
    return record


def cohort_cooldown_record(
    size=5, *, sweep_id=SWEEP_ID, retry_after=None, opened=NOW, live_offer=None
):
    members = [
        blocked_member(index, instruction="cooldown" if index == size else "hold")
        for index in range(1, size + 1)
    ]
    accepted = accepted_for(members)
    return {
        "schema_version": 2,
        "state": "cooldown",
        "reason_code": REASON,
        "sweep_id": sweep_id,
        "opened_epoch": opened,
        "deadline_epoch": opened + SWEEP_WINDOW_SECONDS,
        "members": members,
        "cohort_size": size,
        "retry_after_epoch": NOW + COOLDOWN_SECONDS
        if retry_after is None
        else retry_after,
        "live_offer": live_offer,
        "accepted_offers": accepted,
        "used_offer_ids": generation_ids(members, accepted, live_offer),
    }


def healthy_record(
    *, sweep_id=SWEEP_ID, until=None, retest=(), live_offer=None, accepted=()
):
    accepted = list(accepted)
    return {
        "schema_version": 2,
        "state": "healthy",
        "sweep_id": sweep_id,
        "until_epoch": NOW + HEALTHY_SUPPRESSION_SECONDS if until is None else until,
        "retest_members": list(retest),
        "live_offer": live_offer,
        "accepted_offers": accepted,
        "used_offer_ids": generation_ids((), accepted, live_offer),
    }


def individual_record(
    reason="sweep_inconclusive",
    *,
    generation=SWEEP_ID,
    until=None,
    live_offer=None,
    holds=(),
    accepted=(),
):
    accepted = list(accepted)
    return {
        "schema_version": 2,
        "state": "individual",
        "reason": reason,
        "generation_id": generation,
        "until_epoch": NOW + SWEEP_WINDOW_SECONDS if until is None else until,
        "live_offer": live_offer,
        "hold_fingerprints": list(holds),
        "accepted_offers": accepted,
        "used_offer_ids": generation_ids((), accepted, live_offer),
    }


def legacy_cooldown_record(*, retry_after=None, evidence=3):
    return {
        "schema_version": 2,
        "state": "cooldown",
        "reason_code": REASON,
        "legacy_cooldown": True,
        "retry_after_epoch": NOW + COOLDOWN_SECONDS
        if retry_after is None
        else retry_after,
        "legacy_evidence_count": evidence,
    }


def live_offer_block(identifier, index, *, mode="probe", expires=None, instruction=""):
    return {
        "offer_id": identifier,
        "offer_fingerprint": fingerprint(index),
        "offer_expires_epoch": NOW + OFFER_LEASE_SECONDS
        if expires is None
        else expires,
        "mode": mode,
        "response_instruction": instruction,
    }


def report(index, *, sweep_id=SWEEP_ID, identifier=None, mode="sweep"):
    return Offer(
        mode=mode,
        sweep_id=sweep_id,
        offer_id=offer_id(index) if identifier is None else identifier,
        fingerprint=fingerprint(index),
        deadline_epoch=DEADLINE,
    )


class PrepareDecisionTests(unittest.TestCase):
    def setUp(self):
        self.ids = SequentialIds()

    def prepare(self, inventory, current=None, now=NOW):
        return prepare_decision(inventory, current, now=now, sweep_id_factory=self.ids)

    def test_cohort_sizes_choose_between_sweeping_and_generation_bound_individual(self):
        # A cohort needs two members to be one at all, and the frozen denominator
        # may never exceed the bounded maximum, so both ends fall back to an
        # individual decision that can never freeze a cooldown cohort.
        cases = (
            (0, None, None),
            (1, "individual", "cohort_too_small"),
            (2, "sweeping", None),
            (4, "sweeping", None),
            (MINIMUM_CONCLUSIVE_COHORT_SIZE, "sweeping", None),
            (MAXIMUM_COHORT_SIZE, "sweeping", None),
            (MAXIMUM_COHORT_SIZE + 1, "individual", "cohort_oversized"),
        )
        for size, state, reason in cases:
            with self.subTest(size=size):
                self.ids = SequentialIds()
                record = self.prepare(cohort_inventory(size))
                if state is None:
                    self.assertIsNone(record)
                    continue
                self.assertEqual(state, record["state"])
                if reason is not None:
                    self.assertEqual(reason, record["reason"])
                    self.assertEqual(NOW + SWEEP_WINDOW_SECONDS, record["until_epoch"])
                else:
                    self.assertEqual(size, len(record["members"]))
                    self.assertEqual(NOW, record["opened_epoch"])
                    self.assertEqual(DEADLINE, record["deadline_epoch"])
                # Every produced record must survive the strict codec unchanged.
                self.assertEqual(
                    record,
                    decode_decision_record(encode_decision_record(record), now=NOW),
                )

    def test_oversized_sentinel_inventory_never_freezes_a_cohort(self):
        record = self.prepare(cohort_inventory(0, oversized=True))

        self.assertEqual("individual", record["state"])
        self.assertEqual("cohort_oversized", record["reason"])

    def test_frozen_members_are_unique_and_ascending_regardless_of_input_order(self):
        inventory = build_inventory(
            {
                fingerprint(9): (package(9), package(3)),
                fingerprint(2): (package(2),),
                fingerprint(5): (package(5),),
                fingerprint(7): (package(7),),
                fingerprint(1): (package(1),),
            }
        )

        record = self.prepare(inventory)

        self.assertEqual(
            [fingerprint(index) for index in (1, 2, 5, 7, 9)],
            [entry["link_fingerprint"] for entry in record["members"]],
        )

    def test_inventory_read_failure_leaves_the_linkcrypter_untouched(self):
        # A read failure proves nothing, so it may neither open a sweep nor
        # replace an existing decision.
        self.assertIsNone(self.prepare(None))
        existing = sweeping_record(3)
        self.assertEqual(existing, self.prepare(None, existing))
        self.assertEqual([], self.ids.minted)

    def test_a_live_decision_is_never_replaced_by_a_fresh_sweep(self):
        for current in (
            sweeping_record(5),
            cohort_cooldown_record(),
            healthy_record(),
            individual_record(),
            legacy_cooldown_record(),
        ):
            with self.subTest(state=current.get("reason") or current["state"]):
                self.assertEqual(current, self.prepare(cohort_inventory(6), current))
        self.assertEqual([], self.ids.minted)

    def test_an_expired_sweep_is_concluded_and_suppresses_a_fresh_sweep(self):
        expired = sweeping_record(6)

        concluded = self.prepare(cohort_inventory(6), expired, now=DEADLINE + 1)

        self.assertEqual("individual", concluded["state"])
        self.assertEqual("sweep_expired", concluded["reason"])
        self.assertEqual(SWEEP_ID, concluded["generation_id"])
        self.assertEqual(DEADLINE + SWEEP_WINDOW_SECONDS, concluded["until_epoch"])
        self.assertEqual([], self.ids.minted)

    def test_a_fully_expired_suppression_window_allows_a_new_sweep(self):
        record = self.prepare(
            cohort_inventory(6),
            individual_record(until=NOW + 10),
            now=NOW + 10,
        )

        self.assertEqual("sweeping", record["state"])
        self.assertEqual([offer_id(901)], self.ids.minted)


class ExpireDecisionTests(unittest.TestCase):
    def test_every_state_is_pinned_one_second_around_its_deadline(self):
        cases = (
            ("sweeping", sweeping_record(3), DEADLINE),
            ("cooldown", cohort_cooldown_record(), NOW + COOLDOWN_SECONDS),
            ("legacy_cooldown", legacy_cooldown_record(), NOW + COOLDOWN_SECONDS),
            ("healthy", healthy_record(), NOW + HEALTHY_SUPPRESSION_SECONDS),
            ("individual", individual_record(), NOW + SWEEP_WINDOW_SECONDS),
        )
        for label, record, deadline in cases:
            with self.subTest(state=label):
                self.assertEqual(record, expire_decision(record, now=deadline - 1))
                if label == "sweeping":
                    # A sweep is still live exactly at its deadline so a report
                    # arriving there is accepted rather than rejected.
                    self.assertEqual(record, expire_decision(record, now=deadline))
                    concluded = expire_decision(record, now=deadline + 1)
                    self.assertEqual("sweep_expired", concluded["reason"])
                else:
                    self.assertIsNone(expire_decision(record, now=deadline))
                    self.assertIsNone(expire_decision(record, now=deadline + 1))

    def test_a_long_expired_sweep_self_heals_to_no_decision(self):
        # The concluded individual window is measured from the sweep deadline, so
        # a sweep nobody read for hours does not resurrect a fresh suppression.
        self.assertIsNone(
            expire_decision(sweeping_record(3), now=DEADLINE + SWEEP_WINDOW_SECONDS)
        )

    def test_conclusion_is_idempotent(self):
        once = expire_decision(sweeping_record(3), now=DEADLINE + 1)
        twice = expire_decision(once, now=DEADLINE + 2)

        self.assertEqual(once, twice)

    def test_a_missing_decision_stays_missing(self):
        self.assertIsNone(expire_decision(None, now=NOW))


class LeaseOfferTests(unittest.TestCase):
    def setUp(self):
        self.ids = SequentialIds()

    def lease(self, record, inventory=None, *, now=NOW, mode=None, preferred=None):
        return lease_next_offer(
            record,
            inventory,
            now=now,
            offer_id_factory=self.ids,
            mode=mode,
            preferred_fingerprint=preferred,
        )

    def test_a_sweep_leases_pending_members_in_frozen_order(self):
        record = sweeping_record(3)

        record, first = self.lease(record)
        record, second = self.lease(record)

        self.assertEqual(
            Offer("sweep", SWEEP_ID, offer_id(901), fingerprint(1), DEADLINE), first
        )
        self.assertEqual(fingerprint(2), second.fingerprint)
        self.assertEqual([offer_id(901), offer_id(902)], self.ids.minted)
        self.assertEqual(
            ["offered", "offered", "pending"],
            [entry["result"] for entry in record["members"]],
        )
        self.assertEqual(
            NOW + OFFER_LEASE_SECONDS, record["members"][0]["offer_expires_epoch"]
        )

    def test_one_live_lease_per_member_and_expired_leases_are_replaced(self):
        record = sweeping_record(1 + 1)
        record, first = self.lease(record)

        # While the lease is live the same member is never handed out twice.
        record, second = self.lease(record)
        self.assertEqual(fingerprint(2), second.fingerprint)
        record, none_left = self.lease(record)
        self.assertIsNone(none_left)

        # Once the two-minute lease expired the member may be re-offered under a
        # fresh ID; the superseded ID can never advance the sweep afterwards.
        record, replaced = self.lease(record, now=NOW + OFFER_LEASE_SECONDS)
        self.assertEqual(first.fingerprint, replaced.fingerprint)
        self.assertNotEqual(first.offer_id, replaced.offer_id)
        self.assertEqual(replaced.offer_id, record["members"][0]["offer_id"])

    def test_no_offer_is_leased_at_or_after_a_deadline(self):
        cases = (
            (sweeping_record(3), DEADLINE, None, None),
            (
                healthy_record(retest=[fingerprint(1)]),
                NOW + HEALTHY_SUPPRESSION_SECONDS,
                None,
                None,
            ),
            (individual_record(), NOW + SWEEP_WINDOW_SECONDS, "individual", None),
            (
                cohort_cooldown_record(),
                NOW + COOLDOWN_SECONDS,
                "probe",
                fingerprint(1),
            ),
        )
        for record, deadline, mode, preferred in cases:
            with self.subTest(state=record["state"]):
                _record, at_deadline = self.lease(
                    record,
                    cohort_inventory(3),
                    now=deadline,
                    mode=mode,
                    preferred=preferred,
                )
                self.assertIsNone(at_deadline)
                _record, before = self.lease(
                    record,
                    cohort_inventory(3),
                    now=deadline - 1,
                    mode=mode,
                    preferred=preferred,
                )
                self.assertIsNotNone(before)

    def test_offer_ids_must_stay_unique_inside_one_generation(self):
        record = sweeping_record(3)
        record, first = self.lease(record)
        colliding = SequentialIds(start=900)

        record, second = lease_next_offer(
            record, None, now=NOW, offer_id_factory=colliding, mode="sweep"
        )

        self.assertNotEqual(first.offer_id, second.offer_id)
        self.assertEqual([first.offer_id, second.offer_id], record["used_offer_ids"])

    def test_healthy_leases_retest_members_in_order(self):
        record = healthy_record(retest=[fingerprint(2), fingerprint(7)])

        record, first = self.lease(record)

        self.assertEqual("retest", first.mode)
        self.assertEqual(fingerprint(2), first.fingerprint)
        self.assertEqual(SWEEP_ID, first.sweep_id)
        self.assertEqual(NOW + HEALTHY_SUPPRESSION_SECONDS, first.deadline_epoch)
        self.assertEqual(first.offer_id, record["live_offer"]["offer_id"])

        _record, second = self.lease(record)
        self.assertIsNone(second)

    def test_healthy_without_a_retest_queue_leases_nothing(self):
        _record, offer = self.lease(healthy_record(), cohort_inventory(3))

        self.assertIsNone(offer)

    def test_individual_leases_from_the_current_inventory(self):
        record = individual_record("cohort_too_small")

        record, offer = self.lease(record, cohort_inventory(2, start=4))

        self.assertEqual("individual", offer.mode)
        self.assertEqual(SWEEP_ID, offer.sweep_id)
        self.assertEqual(fingerprint(4), offer.fingerprint)
        self.assertEqual(record["live_offer"]["offer_id"], offer.offer_id)

    def test_individual_without_a_usable_inventory_leases_nothing(self):
        for inventory in (
            None,
            cohort_inventory(0),
            cohort_inventory(3, oversized=True),
        ):
            with self.subTest(inventory=inventory):
                _record, offer = self.lease(individual_record(), inventory)
                self.assertIsNone(offer)

    def test_cooldown_only_ever_leases_an_explicit_probe(self):
        record = cohort_cooldown_record()

        _unchanged, implicit = self.lease(record, cohort_inventory(5))
        self.assertIsNone(implicit)

        leased, probe = self.lease(
            record, cohort_inventory(5), mode="probe", preferred=fingerprint(1)
        )
        self.assertEqual("probe", probe.mode)
        self.assertEqual(fingerprint(1), probe.fingerprint)
        self.assertEqual(NOW + COOLDOWN_SECONDS, probe.deadline_epoch)
        self.assertEqual(probe.offer_id, leased["live_offer"]["offer_id"])

    def test_a_migrated_legacy_cooldown_never_issues_a_cohort_offer(self):
        record = legacy_cooldown_record()

        unchanged, offer = self.lease(
            record, cohort_inventory(5), mode="probe", preferred=fingerprint(1)
        )

        self.assertIsNone(offer)
        self.assertEqual(record, unchanged)

    def test_no_decision_leases_nothing(self):
        record, offer = self.lease(None, cohort_inventory(5))

        self.assertIsNone(record)
        self.assertIsNone(offer)

    def test_a_leased_sweep_record_still_round_trips_through_the_codec(self):
        record, _offer = self.lease(sweeping_record(3))

        self.assertEqual(
            record, decode_decision_record(encode_decision_record(record), now=NOW)
        )


class PreferredMemberLeaseTests(unittest.TestCase):
    """A manual probe authorizes one package, so its member must be named."""

    def setUp(self):
        self.ids = SequentialIds()

    def lease(self, record, inventory=None, *, now=NOW, mode=None, preferred=None):
        return lease_next_offer(
            record,
            inventory,
            now=now,
            offer_id_factory=self.ids,
            mode=mode,
            preferred_fingerprint=preferred,
        )

    def test_a_cooldown_probe_leases_the_named_member_not_the_first(self):
        record = cohort_cooldown_record()

        leased, probe = self.lease(
            record, cohort_inventory(5), mode="probe", preferred=fingerprint(4)
        )

        self.assertEqual("probe", probe.mode)
        self.assertEqual(fingerprint(4), probe.fingerprint)
        self.assertEqual(probe.offer_id, leased["live_offer"]["offer_id"])
        self.assertEqual(fingerprint(4), leased["live_offer"]["offer_fingerprint"])

    def test_a_probe_without_a_named_member_leases_nothing_and_mints_nothing(self):
        record = cohort_cooldown_record()

        unchanged, offer = self.lease(record, cohort_inventory(5), mode="probe")

        self.assertIsNone(offer)
        self.assertEqual(record, unchanged)
        self.assertEqual([], self.ids.minted)

    def test_a_probe_for_a_fingerprint_outside_the_cohort_changes_nothing(self):
        record = cohort_cooldown_record()

        unchanged, offer = self.lease(
            record, cohort_inventory(9), mode="probe", preferred=fingerprint(9)
        )

        self.assertIsNone(offer)
        self.assertEqual(record, unchanged)
        self.assertEqual([], self.ids.minted)

    def test_a_sweep_leases_exactly_the_named_pending_member(self):
        record = sweeping_record(3)

        leased, offer = self.lease(record, mode="sweep", preferred=fingerprint(3))

        self.assertEqual(fingerprint(3), offer.fingerprint)
        self.assertEqual(
            ["pending", "pending", "offered"],
            [entry["result"] for entry in leased["members"]],
        )

    def test_a_named_member_that_is_not_leasable_changes_nothing(self):
        record = sweeping_record(3, members=[blocked_member(1), member(2), member(3)])

        unchanged, offer = self.lease(record, mode="sweep", preferred=fingerprint(1))

        self.assertIsNone(offer)
        self.assertEqual(record, unchanged)
        self.assertEqual([], self.ids.minted)

    def test_a_retest_queue_only_answers_its_own_head(self):
        record = healthy_record(retest=[fingerprint(2), fingerprint(7)])

        unchanged, refused = self.lease(record, preferred=fingerprint(7))
        self.assertIsNone(refused)
        self.assertEqual(record, unchanged)
        self.assertEqual([], self.ids.minted)

        _leased, offered = self.lease(record, preferred=fingerprint(2))
        self.assertEqual(fingerprint(2), offered.fingerprint)


class TypedReplayTests(unittest.TestCase):
    """An accepted result may only be replayed by the route that produced it."""

    def blocked(self, record, offer, inventory=None, *, now=NOW):
        return record_blocked(
            record, offer, inventory, now=now, cooldown_seconds=COOLDOWN_SECONDS
        )

    def access(self, record, offer, access, *, now=NOW):
        return record_access(record, offer, access, None, now=now)

    def offered(self, size=5, *, index=1):
        members = [
            member(position, "offered", offer=offer_id(position))
            if position == index
            else member(position)
            for position in range(1, size + 1)
        ]
        return sweeping_record(size, members=members)

    def test_the_accepted_history_stores_the_outcome_kind_it_answered(self):
        record = self.offered()

        held, _ = self.blocked(record, report(1), cohort_inventory(5))
        cleared, _ = self.access(record, report(1), "clear")
        unknown, _ = self.access(record, report(1), "unknown")

        self.assertEqual(
            ["blocked"], [entry["outcome"] for entry in held["accepted_offers"]]
        )
        self.assertEqual(
            ["clear"], [entry["outcome"] for entry in cleared["accepted_offers"]]
        )
        self.assertEqual(
            ["unknown"], [entry["outcome"] for entry in unknown["accepted_offers"]]
        )

    def test_an_accepted_access_result_is_never_replayed_as_a_blocked_report(self):
        for access in ("clear", "unknown"):
            with self.subTest(access=access):
                record, answered = self.access(self.offered(), report(1), access)

                unchanged, replay = self.blocked(record, report(1), cohort_inventory(5))

                self.assertEqual("stale", replay["instruction"])
                self.assertEqual("", replay["accepted"])
                self.assertIs(False, replay["cleared"])
                self.assertNotEqual(answered["instruction"], replay["instruction"])
                self.assertEqual(record, unchanged)

    def test_an_accepted_blocked_result_is_never_replayed_as_an_access_report(self):
        record, held = self.blocked(self.offered(), report(1), cohort_inventory(5))
        self.assertEqual("hold", held["instruction"])

        for access in ("clear", "unknown"):
            with self.subTest(access=access):
                unchanged, replay = self.access(record, report(1), access)

                self.assertEqual("stale", replay["instruction"])
                self.assertIs(False, replay["cleared"])
                self.assertEqual("", replay["accepted"])
                self.assertEqual(record, unchanged)

    def test_an_access_report_only_replays_its_own_kind(self):
        record, accepted = self.access(self.offered(), report(1), "unknown")

        _unchanged, mismatched = self.access(record, report(1), "clear")
        self.assertEqual("stale", mismatched["instruction"])
        self.assertIs(False, mismatched["cleared"])

        _unchanged, replay = self.access(record, report(1), "unknown")
        self.assertEqual(accepted["accepted"], replay["accepted"])
        self.assertEqual(accepted["state"], replay["state"])

    def test_a_replay_reports_current_counters_with_its_original_verdict(self):
        record = sweeping_record(
            5,
            members=[
                member(index, "offered", offer=offer_id(index)) for index in range(1, 6)
            ],
        )
        inventory = cohort_inventory(5)
        record, first = self.blocked(record, report(1), inventory)
        self.assertEqual(1, first["sweep_tested"])

        for index in (2, 3):
            record, _answer = self.blocked(record, report(index), inventory)

        unchanged, replay = self.blocked(record, report(1), inventory)

        self.assertEqual(first["instruction"], replay["instruction"])
        self.assertEqual(first["state"], replay["state"])
        self.assertEqual(first["hold_type"], replay["hold_type"])
        self.assertEqual(first["evidence_count"], replay["evidence_count"])
        self.assertEqual(first["retry_after_epoch"], replay["retry_after_epoch"])
        self.assertEqual(3, replay["sweep_tested"])
        self.assertEqual(5, replay["sweep_total"])
        self.assertEqual(SWEEP_ID, replay["sweep_id"])
        self.assertEqual(record, unchanged)

    def test_a_replay_inside_an_oversized_window_still_reports_the_sentinel(self):
        accepted = accepted_offer(
            1,
            state="individual",
            instruction="legacy_failure",
            hold_type="none",
            evidence_count=0,
            retry_after_epoch=0,
            sweep_tested=4,
            sweep_total=4,
        )
        record = individual_record("cohort_oversized", accepted=[accepted])

        unchanged, replay = self.blocked(record, report(1), None)

        self.assertEqual(OVERSIZED_COHORT_SENTINEL, replay["sweep_total"])
        self.assertEqual("legacy_failure", replay["instruction"])
        self.assertEqual(SWEEP_ID, replay["sweep_id"])
        self.assertEqual(record, unchanged)


class RecordBlockedTests(unittest.TestCase):
    def blocked(self, record, offer, inventory=None, *, now=NOW):
        return record_blocked(
            record, offer, inventory, now=now, cooldown_seconds=COOLDOWN_SECONDS
        )

    def offered_cohort(self, size, *, blocked_upto=0, now=NOW, leased_at=None):
        """A sweep whose first `blocked_upto` members already reported blocked."""
        leased_at = now if leased_at is None else leased_at
        members = []
        for index in range(1, size + 1):
            if index <= blocked_upto:
                members.append(blocked_member(index, tested=now))
            elif index == blocked_upto + 1:
                members.append(
                    member(
                        index,
                        "offered",
                        tested=0,
                        offer=offer_id(index),
                        expires=leased_at + OFFER_LEASE_SECONDS,
                    )
                )
            else:
                members.append(member(index))
        return sweeping_record(
            size, members=members, accepted_offers=accepted_for(members)
        )

    def test_a_pending_cohort_answers_hold_and_counts_one_observation(self):
        record = self.offered_cohort(5)

        updated, decision = self.blocked(record, report(1), cohort_inventory(5))

        self.assertEqual("hold", decision["instruction"])
        self.assertEqual("sweeping", decision["state"])
        self.assertEqual("provisional", decision["hold_type"])
        self.assertEqual(DEADLINE, decision["retry_after_epoch"])
        self.assertEqual(1, decision["evidence_count"])
        self.assertEqual(SWEEP_ID, decision["sweep_id"])
        self.assertEqual(5, decision["sweep_total"])
        self.assertEqual(1, decision["sweep_tested"])
        self.assertEqual(DEADLINE, decision["sweep_deadline_epoch"])
        self.assertEqual({**NO_EVENTS, "observations": 1}, decision["events"])
        self.assertEqual("blocked", updated["members"][0]["result"])
        self.assertEqual("hold", updated["members"][0]["response_instruction"])
        self.assertEqual(NOW, updated["members"][0]["tested_epoch"])

    def test_a_complete_all_blocked_cohort_of_five_starts_exactly_one_cooldown(self):
        record = self.offered_cohort(5, blocked_upto=4)

        updated, decision = self.blocked(record, report(5), cohort_inventory(5))

        self.assertEqual("cooldown", decision["instruction"])
        self.assertEqual("cooldown", decision["state"])
        self.assertEqual("crypter_cooldown", decision["hold_type"])
        self.assertEqual(NOW + COOLDOWN_SECONDS, decision["retry_after_epoch"])
        self.assertEqual(5, decision["evidence_count"])
        self.assertEqual(
            {"observations": 1, "cooldowns": 1, "probes": 0}, decision["events"]
        )
        self.assertEqual(5, updated["cohort_size"])
        self.assertEqual(
            ["hold", "hold", "hold", "hold", "cooldown"],
            [entry["response_instruction"] for entry in updated["members"]],
        )
        # The strict cohort codec is what proves the emitted evidence coherent.
        self.assertEqual(
            updated, decode_decision_record(encode_decision_record(updated), now=NOW)
        )

    def test_evidence_outside_the_generation_window_cannot_start_cooldown(self):
        record = self.offered_cohort(5, blocked_upto=4, now=NOW - 1)

        updated, decision = self.blocked(record, report(5), cohort_inventory(5))

        self.assertEqual("individual", updated["state"])
        self.assertEqual("sweep_inconclusive", updated["reason"])
        self.assertEqual("legacy_failure", decision["instruction"])
        self.assertEqual(0, decision["events"]["cooldowns"])

    def test_cohort_sizes_below_the_conclusive_minimum_never_cool_down(self):
        for size in (2, 3, 4):
            with self.subTest(size=size):
                record = self.offered_cohort(size, blocked_upto=size - 1)

                updated, decision = self.blocked(
                    record, report(size), cohort_inventory(size)
                )

                self.assertEqual("legacy_failure", decision["instruction"])
                self.assertEqual("individual", decision["state"])
                self.assertEqual("cohort_too_small", updated["reason"])
                self.assertEqual(SWEEP_ID, updated["generation_id"])
                self.assertEqual(0, decision["retry_after_epoch"])
                self.assertEqual(size, decision["sweep_total"])
                self.assertEqual(size, decision["sweep_tested"])
                self.assertEqual({**NO_EVENTS, "observations": 1}, decision["events"])

    def test_the_full_bounded_cohort_still_concludes_as_one_cooldown(self):
        record = self.offered_cohort(
            MAXIMUM_COHORT_SIZE, blocked_upto=MAXIMUM_COHORT_SIZE - 1
        )

        updated, decision = self.blocked(
            record,
            report(MAXIMUM_COHORT_SIZE),
            cohort_inventory(MAXIMUM_COHORT_SIZE),
        )

        self.assertEqual("cooldown", decision["state"])
        self.assertEqual(MAXIMUM_COHORT_SIZE, updated["cohort_size"])

    def test_one_unknown_member_at_any_position_prevents_cooldown(self):
        # An UNKNOWN never ends the sweep early, but it makes the terminal
        # evaluation inconclusive no matter which member reported it.
        for unknown_index in range(1, 6):
            with self.subTest(position=unknown_index):
                last = 4 if unknown_index == 5 else 5
                members = []
                for index in range(1, 6):
                    if index == unknown_index:
                        entry = blocked_member(index, instruction="legacy_failure")
                        entry["result"] = "unknown"
                        members.append(entry)
                    elif index == last:
                        offered = member(index, "offered", offer=offer_id(index))
                        offered["offer_expires_epoch"] = NOW + OFFER_LEASE_SECONDS
                        members.append(offered)
                    else:
                        members.append(blocked_member(index))

                updated, decision = self.blocked(
                    sweeping_record(5, members=members),
                    report(last),
                    cohort_inventory(5),
                )

                self.assertEqual("individual", updated["state"])
                self.assertEqual("sweep_inconclusive", updated["reason"])
                self.assertEqual("legacy_failure", decision["instruction"])
                self.assertEqual(0, decision["events"]["cooldowns"])

    def test_reports_are_pinned_one_second_around_the_sweep_deadline(self):
        for offset, instruction, state in (
            (-1, "cooldown", "cooldown"),
            (0, "cooldown", "cooldown"),
            (1, "stale", "individual"),
        ):
            with self.subTest(offset=offset):
                record = self.offered_cohort(
                    5, blocked_upto=4, leased_at=DEADLINE + offset
                )

                updated, decision = self.blocked(
                    record, report(5), cohort_inventory(5), now=DEADLINE + offset
                )

                self.assertEqual(instruction, decision["instruction"])
                self.assertEqual(state, updated["state"])
                if state == "individual":
                    self.assertEqual("sweep_expired", updated["reason"])
                    self.assertEqual(0, decision["events"]["observations"])

    def test_a_duplicate_exact_report_replays_without_mutating_or_counting(self):
        record = self.offered_cohort(5)
        held, first = self.blocked(record, report(1), cohort_inventory(5))

        replayed, second = self.blocked(
            held, report(1), cohort_inventory(5), now=NOW + 30
        )

        self.assertEqual(held, replayed)
        self.assertEqual("hold", second["instruction"])
        self.assertEqual(first["retry_after_epoch"], second["retry_after_epoch"])
        self.assertEqual(NO_EVENTS, second["events"])

    def test_a_duplicate_final_report_replays_the_stored_cooldown(self):
        record = self.offered_cohort(5, blocked_upto=4)
        cooled, first = self.blocked(record, report(5), cohort_inventory(5))

        replayed, second = self.blocked(
            cooled, report(5), cohort_inventory(5), now=NOW + 60
        )

        self.assertEqual(cooled, replayed)
        self.assertEqual("cooldown", second["instruction"])
        self.assertEqual(first["retry_after_epoch"], second["retry_after_epoch"])
        self.assertEqual(NO_EVENTS, second["events"])

    def test_a_duplicate_hold_report_during_cooldown_replays_the_accepted_hold(self):
        record = cohort_cooldown_record()

        replayed, decision = self.blocked(record, report(1), cohort_inventory(5))

        self.assertEqual(record, replayed)
        self.assertEqual("hold", decision["instruction"])
        self.assertEqual("sweeping", decision["state"])
        self.assertEqual("provisional", decision["hold_type"])
        self.assertEqual(DEADLINE, decision["retry_after_epoch"])
        self.assertEqual(NO_EVENTS, decision["events"])

    def test_stale_generations_and_superseded_offers_never_mutate(self):
        record = self.offered_cohort(5)
        cases = (
            ("wrong generation", report(1, sweep_id=OTHER_SWEEP_ID)),
            ("superseded offer", report(1, identifier=offer_id(777))),
            ("unknown member", report(60)),
        )
        for label, offer in cases:
            with self.subTest(case=label):
                unchanged, decision = self.blocked(record, offer, cohort_inventory(5))

                self.assertEqual(record, unchanged)
                self.assertEqual("stale", decision["instruction"])
                self.assertEqual("none", decision["hold_type"])
                self.assertEqual(0, decision["retry_after_epoch"])
                self.assertEqual(NO_EVENTS, decision["events"])

    def test_a_report_without_any_decision_is_stale(self):
        record, decision = self.blocked(None, report(1), cohort_inventory(5))

        self.assertIsNone(record)
        self.assertEqual("stale", decision["instruction"])
        self.assertEqual("available", decision["state"])

    def test_a_migrated_legacy_cooldown_is_never_addressed_by_a_cohort_report(self):
        record = legacy_cooldown_record()

        unchanged, decision = self.blocked(record, report(1), cohort_inventory(5))

        self.assertEqual(record, unchanged)
        self.assertEqual("stale", decision["instruction"])

    def test_blocked_during_healthy_or_individual_is_ordinary_legacy_failure(self):
        cases = (
            (
                "healthy",
                healthy_record(
                    retest=[fingerprint(1)],
                    live_offer=live_offer_block(offer_id(1), 1, mode="retest"),
                ),
            ),
            (
                "individual",
                individual_record(
                    "cohort_oversized",
                    live_offer=live_offer_block(offer_id(1), 1, mode="individual"),
                ),
            ),
        )
        for label, record in cases:
            with self.subTest(state=label):
                updated, decision = self.blocked(record, report(1), cohort_inventory(5))

                self.assertEqual("legacy_failure", decision["instruction"])
                self.assertEqual(label, decision["state"])
                self.assertEqual("none", decision["hold_type"])
                self.assertEqual(0, decision["retry_after_epoch"])
                self.assertEqual(NO_EVENTS, decision["events"])
                self.assertIsNone(
                    updated["live_offer"],
                    "an accepted lease is released, not left occupying the slot",
                )
                self.assertEqual(
                    [offer_id(1)],
                    [entry["offer_id"] for entry in updated["accepted_offers"]],
                )

    def test_a_retest_block_drops_that_fingerprint_from_the_queue(self):
        record = healthy_record(
            retest=[fingerprint(1), fingerprint(4)],
            live_offer=live_offer_block(offer_id(1), 1, mode="retest"),
        )

        updated, decision = self.blocked(record, report(1), cohort_inventory(5))

        self.assertEqual([fingerprint(4)], updated["retest_members"])
        self.assertEqual("legacy_failure", decision["instruction"])

    def test_an_unaccepted_member_report_after_conclusion_is_stale(self):
        # The concluded sweep keeps its generation ID for accepted replays, but
        # an offer that was never accepted is no longer a live lease.
        concluded = individual_record(
            "sweep_expired", until=DEADLINE + SWEEP_WINDOW_SECONDS
        )

        unchanged, decision = self.blocked(
            concluded, report(3), cohort_inventory(5), now=DEADLINE + 5
        )

        self.assertEqual(concluded, unchanged)
        self.assertEqual("stale", decision["instruction"])
        self.assertEqual(NO_EVENTS, decision["events"])

    def test_an_untested_member_that_left_the_inventory_ends_the_sweep(self):
        record = self.offered_cohort(5, blocked_upto=1)
        shrunk = build_inventory(
            {fingerprint(index): (package(index),) for index in (1, 2, 3, 5)}
        )

        updated, decision = self.blocked(record, report(2), shrunk)

        self.assertEqual("individual", updated["state"])
        self.assertEqual("sweep_inconclusive", updated["reason"])
        self.assertEqual(5, decision["sweep_total"])
        self.assertEqual("legacy_failure", decision["instruction"])

    def test_a_tested_member_that_left_the_inventory_still_completes_the_cohort(self):
        record = self.offered_cohort(5, blocked_upto=4)
        shrunk = build_inventory(
            {fingerprint(index): (package(index),) for index in (2, 3, 4, 5)}
        )

        updated, decision = self.blocked(record, report(5), shrunk)

        self.assertEqual("cooldown", updated["state"])
        self.assertEqual(5, decision["evidence_count"])

    def test_late_inventory_additions_never_change_the_frozen_denominator(self):
        record = self.offered_cohort(5, blocked_upto=4)

        updated, decision = self.blocked(record, report(5), cohort_inventory(40))

        self.assertEqual("cooldown", updated["state"])
        self.assertEqual(5, updated["cohort_size"])
        self.assertEqual(5, decision["sweep_total"])

    def test_a_frozen_member_that_became_helper_untestable_is_not_removed(self):
        # An unusable inventory never shrinks the denominator; it only makes the
        # conclusion inconclusive.
        record = self.offered_cohort(5, blocked_upto=4)

        updated, decision = self.blocked(record, report(5), None)

        self.assertEqual("individual", updated["state"])
        self.assertEqual("inventory_unavailable", updated["reason"])
        self.assertEqual(5, decision["sweep_total"])
        self.assertEqual(0, decision["events"]["cooldowns"])

    def test_an_oversized_inventory_does_not_disturb_a_frozen_cohort(self):
        record = self.offered_cohort(5, blocked_upto=4)

        updated, _decision = self.blocked(
            record, report(5), cohort_inventory(0, oversized=True)
        )

        self.assertEqual("cooldown", updated["state"])


class InventoryOutageTests(unittest.TestCase):
    """An inventory read failure taints its generation permanently."""

    def blocked(self, record, offer, inventory=None, *, now=NOW):
        return record_blocked(
            record, offer, inventory, now=now, cooldown_seconds=COOLDOWN_SECONDS
        )

    def cohort(self, blocked_upto):
        members = []
        for index in range(1, 6):
            if index <= blocked_upto:
                members.append(blocked_member(index))
            elif index == blocked_upto + 1:
                members.append(member(index, "offered", offer=offer_id(index)))
            else:
                members.append(member(index))
        return sweeping_record(
            5, members=members, accepted_offers=accepted_for(members)
        )

    def test_an_outage_at_any_position_ends_the_generation_fail_closed(self):
        for position in (0, 2, 4):
            with self.subTest(position=position):
                record = self.cohort(position)

                updated, decision = self.blocked(record, report(position + 1), None)

                self.assertEqual("individual", updated["state"])
                self.assertEqual("inventory_unavailable", updated["reason"])
                self.assertEqual(SWEEP_ID, updated["generation_id"])
                self.assertEqual(
                    [fingerprint(position + 1)],
                    updated["hold_fingerprints"],
                    "only the reporting fingerprint keeps a hold",
                )
                self.assertEqual(NOW + SWEEP_WINDOW_SECONDS, updated["until_epoch"])
                self.assertEqual("hold", decision["instruction"])
                self.assertEqual("individual", decision["state"])
                self.assertEqual("provisional", decision["hold_type"])
                self.assertEqual(
                    NOW + SWEEP_WINDOW_SECONDS, decision["retry_after_epoch"]
                )
                self.assertEqual(5, decision["sweep_total"])
                self.assertEqual(0, decision["events"]["cooldowns"])
                self.assertEqual(1, decision["events"]["observations"])

    def test_a_recovered_inventory_can_never_resume_or_cool_the_generation(self):
        record = self.cohort(4)
        tainted, _first = self.blocked(record, report(5), None)

        for index in (1, 5):
            with self.subTest(retry=index):
                unchanged, decision = self.blocked(
                    tainted, report(index), cohort_inventory(5), now=NOW + 60
                )

                self.assertEqual("individual", unchanged["state"])
                self.assertEqual("inventory_unavailable", unchanged["reason"])
                self.assertNotEqual("cooldown", decision["instruction"])
                self.assertEqual(0, decision["events"]["cooldowns"])

    def test_the_tainted_hold_is_replayable_for_the_reporting_fingerprint(self):
        tainted, first = self.blocked(self.cohort(2), report(3), None)

        replayed, second = self.blocked(
            tainted, report(3), cohort_inventory(5), now=NOW + 30
        )

        self.assertEqual(tainted, replayed)
        self.assertEqual(first["instruction"], second["instruction"])
        self.assertEqual(first["retry_after_epoch"], second["retry_after_epoch"])
        self.assertEqual(NO_EVENTS, second["events"])

    def test_only_the_fail_closed_reason_keeps_a_generation_hold_active(self):
        tainted, _decision = self.blocked(self.cohort(2), report(3), None)
        held = {
            "crypter": CRYPTER,
            "reason_code": REASON,
            "since_epoch": NOW,
            "retry_after_epoch": NOW + SWEEP_WINDOW_SECONDS,
            "probe_requested": False,
            "observation_holds": 1,
            "schema_version": 2,
            "sweep_id": SWEEP_ID,
            "link_fingerprints": [fingerprint(3)],
        }
        stale_hold = {**held, "link_fingerprints": [fingerprint(1)]}
        snapshot = decision_snapshot(tainted, now=NOW)

        self.assertTrue(package_defer_is_active(held, snapshot, now=NOW))
        self.assertFalse(
            package_defer_is_active(stale_hold, snapshot, now=NOW),
            "an earlier member of the tainted generation is logically released",
        )
        for reason in (
            "cohort_too_small",
            "cohort_oversized",
            "sweep_expired",
            "sweep_inconclusive",
        ):
            with self.subTest(reason=reason):
                other = decision_snapshot(
                    individual_record(reason, generation=SWEEP_ID), now=NOW
                )
                self.assertFalse(package_defer_is_active(held, other, now=NOW))


class RecordAccessTests(unittest.TestCase):
    def access(self, record, offer, value, inventory=None, *, now=NOW):
        return record_access(record, offer, value, inventory, now=now)

    def sweep_with_blocks(self, blocked_upto=2, size=5, *, offered=None):
        offered = blocked_upto + 1 if offered is None else offered
        members = []
        for index in range(1, size + 1):
            if index <= blocked_upto:
                members.append(blocked_member(index))
            elif index == offered:
                members.append(
                    member(
                        index,
                        "offered",
                        offer=offer_id(index),
                        expires=NOW + OFFER_LEASE_SECONDS,
                    )
                )
            else:
                members.append(member(index))
        return sweeping_record(
            size, members=members, accepted_offers=accepted_for(members)
        )

    def test_clear_at_every_sweep_position_enters_healthy_with_ordered_retests(self):
        for blocked_upto in range(0, 4):
            with self.subTest(blocked=blocked_upto):
                record = self.sweep_with_blocks(blocked_upto)

                updated, decision = self.access(
                    record, report(blocked_upto + 1), "clear", cohort_inventory(5)
                )

                self.assertEqual("healthy", updated["state"])
                self.assertEqual(SWEEP_ID, updated["sweep_id"])
                self.assertEqual(
                    NOW + HEALTHY_SUPPRESSION_SECONDS, updated["until_epoch"]
                )
                self.assertEqual(
                    [fingerprint(index) for index in range(1, blocked_upto + 1)],
                    updated["retest_members"],
                )
                self.assertTrue(decision["cleared"])
                self.assertEqual("healthy", decision["state"])
                self.assertEqual(NO_EVENTS, decision["events"])
                self.assertEqual(
                    updated,
                    decode_decision_record(encode_decision_record(updated), now=NOW),
                )

    def test_clear_wins_over_a_completed_cohort_cooldown(self):
        record = cohort_cooldown_record()
        leased, probe = lease_next_offer(
            record,
            None,
            now=NOW,
            offer_id_factory=SequentialIds(),
            mode="probe",
            preferred_fingerprint=fingerprint(1),
        )

        updated, decision = self.access(leased, probe, "clear", cohort_inventory(5))

        self.assertEqual("healthy", updated["state"])
        self.assertTrue(decision["cleared"])
        # The probed fingerprint just proved itself and is not queued again.
        self.assertEqual(
            [fingerprint(index) for index in range(2, 6)], updated["retest_members"]
        )

    def test_clear_from_an_individual_decision_still_enters_healthy(self):
        record = individual_record(
            "cohort_too_small",
            live_offer=live_offer_block(offer_id(3), 3, mode="individual"),
        )

        updated, decision = self.access(record, report(3, mode="individual"), "clear")

        self.assertEqual("healthy", updated["state"])
        self.assertEqual([], updated["retest_members"])
        self.assertTrue(decision["cleared"])

    def test_a_clear_retry_replays_without_extending_the_health_window(self):
        record = self.sweep_with_blocks(2)
        healthy, first = self.access(record, report(3), "clear", cohort_inventory(5))

        replayed, second = self.access(
            healthy, report(3), "clear", cohort_inventory(5), now=NOW + 120
        )

        self.assertEqual(healthy, replayed)
        self.assertTrue(second["cleared"])
        self.assertEqual(first["sweep_deadline_epoch"], second["sweep_deadline_epoch"])
        self.assertEqual(NO_EVENTS, second["events"])

    def test_clear_invalidates_generation_holds_logically_before_any_cleanup(self):
        record = self.sweep_with_blocks(2)
        deferred = {
            "crypter": CRYPTER,
            "reason_code": REASON,
            "since_epoch": NOW,
            "retry_after_epoch": DEADLINE,
            "probe_requested": False,
            "observation_holds": 1,
            "schema_version": 2,
            "sweep_id": SWEEP_ID,
            "link_fingerprints": [fingerprint(1)],
        }
        self.assertTrue(
            package_defer_is_active(
                deferred, decision_snapshot(record, now=NOW), now=NOW
            )
        )

        healthy, _decision = self.access(
            record, report(3), "clear", cohort_inventory(5)
        )

        self.assertFalse(
            package_defer_is_active(
                deferred, decision_snapshot(healthy, now=NOW), now=NOW
            )
        )

    def test_unknown_marks_a_member_inconclusive_without_ending_the_sweep(self):
        record = self.sweep_with_blocks(0, offered=1)

        updated, decision = self.access(
            record, report(1), "unknown", cohort_inventory(5)
        )

        self.assertEqual("sweeping", updated["state"])
        self.assertEqual("unknown", updated["members"][0]["result"])
        self.assertEqual(NOW, updated["members"][0]["tested_epoch"])
        self.assertEqual("unknown", decision["accepted"])
        self.assertEqual(NO_EVENTS, decision["events"])
        self.assertEqual(5, decision["sweep_total"])
        self.assertEqual(1, decision["sweep_tested"])

    def test_a_final_unknown_concludes_the_sweep_without_cooldown(self):
        record = self.sweep_with_blocks(4, size=5, offered=5)

        updated, decision = self.access(
            record, report(5), "unknown", cohort_inventory(5)
        )

        self.assertEqual("individual", updated["state"])
        self.assertEqual("sweep_inconclusive", updated["reason"])
        self.assertEqual(NO_EVENTS, decision["events"])

    def test_a_probe_unknown_only_consumes_that_offer(self):
        record = cohort_cooldown_record()
        leased, probe = lease_next_offer(
            record,
            None,
            now=NOW,
            offer_id_factory=SequentialIds(),
            mode="probe",
            preferred_fingerprint=fingerprint(1),
        )

        updated, decision = self.access(leased, probe, "unknown", cohort_inventory(5))

        self.assertEqual("cooldown", updated["state"])
        self.assertEqual(NOW + COOLDOWN_SECONDS, updated["retry_after_epoch"])
        self.assertEqual(leased["members"], updated["members"])
        self.assertEqual(5, updated["cohort_size"])
        self.assertIsNone(updated["live_offer"])
        self.assertIn(
            probe.offer_id, [e["offer_id"] for e in updated["accepted_offers"]]
        )
        self.assertEqual("unknown", decision["accepted"])
        self.assertEqual("cooldown", decision["state"])
        self.assertEqual(NO_EVENTS, decision["events"])

        replayed, again = self.access(
            updated, probe, "unknown", cohort_inventory(5), now=NOW + 5
        )
        self.assertEqual(updated, replayed)
        self.assertEqual(decision["accepted"], again["accepted"])

    def test_a_retest_unknown_drops_the_fingerprint_from_the_queue(self):
        record = healthy_record(
            retest=[fingerprint(1), fingerprint(4)],
            live_offer=live_offer_block(offer_id(1), 1, mode="retest"),
        )

        updated, decision = self.access(record, report(1, mode="retest"), "unknown")

        self.assertEqual([fingerprint(4)], updated["retest_members"])
        self.assertEqual("unknown", decision["accepted"])

    def test_stale_access_reports_never_mutate(self):
        record = self.sweep_with_blocks(2)
        cases = (
            ("wrong generation", report(3, sweep_id=OTHER_SWEEP_ID)),
            ("superseded offer", report(3, identifier=offer_id(555))),
            ("unknown member", report(70)),
        )
        for value in ("clear", "unknown"):
            for label, offer in cases:
                with self.subTest(access=value, case=label):
                    unchanged, decision = self.access(record, offer, value)

                    self.assertEqual(record, unchanged)
                    self.assertEqual("stale", decision["instruction"])
                    self.assertFalse(decision["cleared"])
                    self.assertEqual(NO_EVENTS, decision["events"])

    def test_an_unsupported_access_value_is_a_programmer_error(self):
        with self.assertRaises(ValueError):
            self.access(self.sweep_with_blocks(2), report(3), "blocked")


class AcceptedOfferReplayTests(unittest.TestCase):
    """An accepted result is separate state from the lease that produced it."""

    def blocked(self, record, offer, inventory=None, *, now=NOW):
        return record_blocked(
            record, offer, inventory, now=now, cooldown_seconds=COOLDOWN_SECONDS
        )

    def access(self, record, offer, value, inventory=None, *, now=NOW):
        return record_access(record, offer, value, inventory, now=now)

    def offered(self, index, *, size=5, blocked_upto=0):
        members = []
        for position in range(1, size + 1):
            if position <= blocked_upto:
                members.append(blocked_member(position))
            elif position == index:
                members.append(member(position, "offered", offer=offer_id(position)))
            else:
                members.append(member(position))
        return sweeping_record(
            size, members=members, accepted_offers=accepted_for(members)
        )

    def test_an_accepted_result_survives_the_terminal_transition_it_caused(self):
        cooled, first = self.blocked(
            self.offered(5, blocked_upto=4), report(5), cohort_inventory(5)
        )
        self.assertEqual("cooldown", cooled["state"])

        for index in range(1, 6):
            with self.subTest(member=index):
                replayed, decision = self.blocked(
                    cooled, report(index), cohort_inventory(5), now=NOW + 90
                )

                self.assertEqual(cooled, replayed)
                self.assertEqual(NO_EVENTS, decision["events"])
                self.assertEqual(
                    "cooldown" if index == 5 else "hold", decision["instruction"]
                )
                self.assertEqual(
                    "crypter_cooldown" if index == 5 else "provisional",
                    decision["hold_type"],
                )
                self.assertEqual(
                    "cooldown" if index == 5 else "sweeping", decision["state"]
                )
        self.assertEqual("cooldown", first["instruction"])

    def test_prior_accepted_results_survive_every_terminal_transition(self):
        small = self.offered(2, size=2, blocked_upto=1)
        active = self.offered(2, size=5, blocked_upto=1)
        cases = (
            (
                "small all blocked",
                small,
                lambda record: self.blocked(record, report(2), cohort_inventory(2)),
                NOW + 30,
            ),
            (
                "terminal unknown",
                small,
                lambda record: self.access(record, report(2), "unknown"),
                NOW + 30,
            ),
            (
                "inventory unavailable",
                active,
                lambda record: self.blocked(record, report(2), None),
                NOW + 30,
            ),
            (
                "clear",
                active,
                lambda record: self.access(record, report(2), "clear"),
                NOW + 30,
            ),
            (
                "expiry",
                active,
                lambda record: (expire_decision(record, now=DEADLINE + 1), None),
                DEADLINE + 1,
            ),
        )
        replay_fields = (
            "state",
            "instruction",
            "accepted",
            "cleared",
            "hold_type",
            "evidence_count",
            "retry_after_epoch",
        )

        for label, record, transition, replayed_at in cases:
            with self.subTest(transition=label):
                accepted = record["accepted_offers"][0]
                updated, _decision = transition(record)

                self.assertIn(accepted, updated["accepted_offers"])
                expected_ids = (
                    [offer_id(1)] if label == "expiry" else [offer_id(1), offer_id(2)]
                )
                self.assertEqual(
                    expected_ids,
                    [entry["offer_id"] for entry in updated["accepted_offers"]],
                )
                unchanged, replay = self.blocked(
                    updated,
                    report(1),
                    cohort_inventory(5),
                    now=replayed_at,
                )
                self.assertEqual(updated, unchanged)
                self.assertEqual(
                    {field: accepted[field] for field in replay_fields},
                    {field: replay[field] for field in replay_fields},
                )
                # The verdict is replayed; the counters describe the record now.
                current = decision_snapshot(unchanged, now=replayed_at)
                self.assertEqual(current["sweep_tested"], replay["sweep_tested"])
                self.assertEqual(current["sweep_total"], replay["sweep_total"])
                self.assertEqual(NO_EVENTS, replay["events"])

    def test_an_expired_unaccepted_lease_is_stale_and_never_mutates(self):
        record = self.offered(1)

        unchanged, decision = self.blocked(
            record, report(1), cohort_inventory(5), now=NOW + OFFER_LEASE_SECONDS
        )

        self.assertEqual(record, unchanged)
        self.assertEqual("stale", decision["instruction"])
        self.assertEqual(NO_EVENTS, decision["events"])

    def test_an_accepted_offer_replays_even_after_its_lease_expired(self):
        held, first = self.blocked(self.offered(1), report(1), cohort_inventory(5))

        replayed, second = self.blocked(
            held,
            report(1),
            cohort_inventory(5),
            now=NOW + OFFER_LEASE_SECONDS + 60,
        )

        self.assertEqual(held, replayed)
        self.assertEqual(first["instruction"], second["instruction"])
        self.assertEqual(NO_EVENTS, second["events"])

    def test_a_replayed_report_answers_its_verdict_with_current_counters(self):
        held, first = self.blocked(self.offered(1), report(1), cohort_inventory(5))
        self.assertEqual(1, first["sweep_tested"])

        advanced, _second = self.blocked(
            {
                **held,
                "members": [
                    held["members"][0],
                    member(2, "offered", offer=offer_id(2)),
                    *held["members"][2:],
                ],
                "used_offer_ids": sorted(held["used_offer_ids"] + [offer_id(2)]),
            },
            report(2),
            cohort_inventory(5),
            now=NOW + 10,
        )

        _replayed, decision = self.blocked(
            advanced, report(1), cohort_inventory(5), now=NOW + 20
        )

        self.assertEqual(
            2, decision["sweep_tested"], "counters describe the record now"
        )
        self.assertEqual(5, decision["sweep_total"])
        self.assertEqual("sweeping", decision["state"])
        self.assertEqual(DEADLINE, decision["retry_after_epoch"])

    def test_an_oversized_sentinel_and_sweep_identity_survive_a_replay(self):
        record = individual_record(
            "cohort_oversized",
            live_offer=live_offer_block(offer_id(1), 1, mode="individual"),
        )

        answered, first = self.blocked(record, report(1), None)
        self.assertEqual(MAXIMUM_COHORT_SIZE + 1, first["sweep_total"])
        self.assertEqual(SWEEP_ID, first["sweep_id"])

        _replayed, second = self.blocked(answered, report(1), None, now=NOW + 30)

        self.assertEqual(MAXIMUM_COHORT_SIZE + 1, second["sweep_total"])
        self.assertEqual(SWEEP_ID, second["sweep_id"])

    def test_an_accepted_retest_frees_the_slot_for_the_next_retest(self):
        record = healthy_record(
            retest=[fingerprint(1), fingerprint(4)],
            live_offer=live_offer_block(offer_id(1), 1, mode="retest"),
        )

        updated, _decision = self.blocked(record, report(1, mode="retest"), None)

        self.assertIsNone(updated["live_offer"])
        self.assertEqual([fingerprint(4)], updated["retest_members"])

        leased, offer = lease_next_offer(
            updated, None, now=NOW, offer_id_factory=SequentialIds(), mode="retest"
        )
        self.assertIsNotNone(offer)
        self.assertEqual(fingerprint(4), offer.fingerprint)
        self.assertEqual(
            leased, decode_decision_record(encode_decision_record(leased), now=NOW)
        )

    def test_a_fresh_retest_clear_refreshes_health_but_its_retry_never_does(self):
        record = healthy_record(
            until=NOW + 100,
            retest=[fingerprint(1), fingerprint(4)],
            live_offer=live_offer_block(offer_id(1), 1, mode="retest"),
        )

        refreshed, first = self.access(record, report(1, mode="retest"), "clear")

        self.assertEqual(
            NOW + HEALTHY_SUPPRESSION_SECONDS,
            refreshed["until_epoch"],
            "a proven container refreshes the full health window",
        )
        self.assertEqual([fingerprint(4)], refreshed["retest_members"])
        self.assertTrue(first["cleared"])

        replayed, second = self.access(
            refreshed, report(1, mode="retest"), "clear", now=NOW + 60
        )

        self.assertEqual(refreshed, replayed)
        self.assertEqual(
            NOW + HEALTHY_SUPPRESSION_SECONDS,
            replayed["until_epoch"],
            "an acknowledged clear retry can never extend suppression",
        )
        self.assertTrue(second["cleared"])

    def test_a_clear_for_an_unknown_offer_during_health_is_stale(self):
        record = healthy_record(retest=[fingerprint(1)])

        unchanged, decision = self.access(record, report(1, mode="retest"), "clear")

        self.assertEqual(record, unchanged)
        self.assertEqual("stale", decision["instruction"])


class OfferIdentityHistoryTests(unittest.TestCase):
    """Offer identity is generation-wide history, not one mutable slot."""

    def test_every_minted_identifier_is_retained_for_its_generation(self):
        ids = SequentialIds()
        record = sweeping_record(3)

        for _ in range(3):
            record, offer = lease_next_offer(
                record, None, now=NOW, offer_id_factory=ids
            )
            self.assertIsNotNone(offer)

        self.assertEqual(sorted(ids.minted), record["used_offer_ids"])

    def test_a_relet_member_can_never_reuse_an_earlier_identifier(self):
        record = sweeping_record(2)
        record, first = lease_next_offer(
            record, None, now=NOW, offer_id_factory=lambda: offer_id(901)
        )
        record, second = lease_next_offer(
            record, None, now=NOW, offer_id_factory=lambda: offer_id(902)
        )

        # The first member's lease ran out, so member A is offered again while a
        # careless factory replays both identifiers this generation already used.
        minted = iter((offer_id(901), offer_id(902), offer_id(903)))
        record, third = lease_next_offer(
            record,
            None,
            now=NOW + OFFER_LEASE_SECONDS,
            offer_id_factory=lambda: next(minted),
        )

        self.assertEqual(offer_id(901), first.offer_id)
        self.assertEqual(offer_id(902), second.offer_id)
        self.assertEqual(fingerprint(1), third.fingerprint)
        self.assertEqual(offer_id(903), third.offer_id)
        self.assertEqual(
            [offer_id(901), offer_id(902), offer_id(903)], record["used_offer_ids"]
        )

    def test_a_permanently_colliding_factory_fails_after_a_bounded_retry(self):
        record = sweeping_record(2)
        attempts = []

        def always_collide():
            attempts.append(offer_id(901))
            return offer_id(901)

        record, _first = lease_next_offer(
            record, None, now=NOW, offer_id_factory=lambda: offer_id(901)
        )

        with self.assertRaises(ValueError):
            lease_next_offer(record, None, now=NOW, offer_id_factory=always_collide)

        self.assertEqual(MAXIMUM_OFFER_ID_ATTEMPTS, len(attempts))

    def test_the_generation_history_is_bounded_and_fails_closed(self):
        record = sweeping_record(
            2,
            used_offer_ids=[
                offer_id(index) for index in range(MAXIMUM_GENERATION_OFFER_IDS)
            ],
        )

        with self.assertRaises(OverflowError):
            lease_next_offer(
                record, None, now=NOW, offer_id_factory=lambda: offer_id(999_999)
            )

    def test_a_concluded_generation_keeps_the_identifiers_it_minted(self):
        record = sweeping_record(2)
        record, first = lease_next_offer(
            record, None, now=NOW, offer_id_factory=lambda: offer_id(901)
        )

        concluded = expire_decision(record, now=DEADLINE + 1)

        self.assertEqual("individual", concluded["state"])
        self.assertIn(first.offer_id, concluded["used_offer_ids"])


class LegacyReportPrecedenceTests(unittest.TestCase):
    def setUp(self):
        self.ids = SequentialIds()

    def legacy(self, record, *, now=NOW):
        return record_legacy_report(record, now=now, generation_id_factory=self.ids)

    def test_only_an_available_linkcrypter_opens_a_legacy_hold_generation(self):
        record, decision = self.legacy(None)

        self.assertEqual("individual", record["state"])
        self.assertEqual("legacy_v1_hold", record["reason"])
        self.assertEqual(offer_id(901), record["generation_id"])
        self.assertEqual(NOW + SWEEP_WINDOW_SECONDS, record["until_epoch"])
        self.assertEqual("observing", decision["state"])
        self.assertEqual(1, decision["evidence_count"])
        self.assertEqual(
            NOW + SWEEP_WINDOW_SECONDS, decision["package_retry_after_epoch"]
        )
        self.assertTrue(decision["recorded"])
        self.assertFalse(decision["cooldown_started"])

    def test_a_later_report_joins_the_same_generation_without_extending_it(self):
        opened, _first = self.legacy(None)

        joined, decision = self.legacy(opened, now=NOW + 60)

        self.assertEqual(opened, joined)
        self.assertEqual("observing", decision["state"])
        self.assertEqual(
            NOW + SWEEP_WINDOW_SECONDS, decision["package_retry_after_epoch"]
        )
        self.assertFalse(decision["recorded"])

    def test_a_version_one_report_can_never_disturb_a_cohort_state(self):
        cases = (
            ("sweeping", sweeping_record(5), "observing", 0),
            ("cooldown", cohort_cooldown_record(), "cooldown", NOW + COOLDOWN_SECONDS),
            (
                "legacy_cooldown",
                legacy_cooldown_record(),
                "cooldown",
                NOW + COOLDOWN_SECONDS,
            ),
            ("healthy", healthy_record(), "available", 0),
            ("individual", individual_record("cohort_oversized"), "available", 0),
        )
        for label, record, state, retry_after in cases:
            with self.subTest(state=label):
                unchanged, decision = self.legacy(record)

                self.assertEqual(record, unchanged)
                self.assertEqual(state, decision["state"])
                self.assertEqual(retry_after, decision["package_retry_after_epoch"])
                self.assertFalse(decision["recorded"])
                self.assertFalse(decision["cooldown_started"])
        self.assertEqual([], self.ids.minted)

    def test_a_cohort_cooldown_reports_its_own_evidence_count(self):
        _record, decision = self.legacy(cohort_cooldown_record(7))

        self.assertEqual(7, decision["evidence_count"])

    def test_a_migrated_legacy_cooldown_keeps_its_preserved_evidence_count(self):
        _record, decision = self.legacy(legacy_cooldown_record(evidence=2))

        self.assertEqual(2, decision["evidence_count"])

    def test_an_expired_legacy_hold_opens_a_fresh_generation(self):
        opened, _first = self.legacy(None)

        replaced, decision = self.legacy(opened, now=NOW + SWEEP_WINDOW_SECONDS)

        self.assertEqual("legacy_v1_hold", replaced["reason"])
        self.assertEqual(offer_id(902), replaced["generation_id"])
        self.assertTrue(decision["recorded"])


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class FakeDatabase:
    """A minimal storage double that fails the test on any read in a mutation."""

    def __init__(self, table, tables):
        self.table = table
        self.rows = {}
        self.tables = tables
        self.lock = threading.RLock()
        self.mutations = []
        # One-shot hook simulating a competing writer that commits immediately
        # before the next mutation of this table opens its transaction.
        self.before_write = None

    def _peer(self, table):
        if table not in self.tables:
            self.tables[table] = FakeDatabase(table, self.tables)
        return self.tables[table]

    def _reject_reads_from_callback(self):
        if any(peer.in_mutation for peer in self.tables.values()):
            raise AssertionError("storage must never be read inside a mutation")

    in_mutation = False

    def retrieve(self, key):
        self._reject_reads_from_callback()
        return self.rows.get(key)

    def retrieve_all_titles(self):
        self._reject_reads_from_callback()
        items = [[key, value] for key, value in sorted(self.rows.items())]
        return items if items else None

    def update_store(self, key, value):
        self.rows[key] = value
        return True

    def _interleave(self, key):
        hook, self.before_write = self.before_write, None
        if hook is not None:
            hook(key)

    def mutate_value(self, key, mutator):
        self._interleave(key)
        with self.lock:
            self.mutations.append((self.table, key))
            self.in_mutation = True
            try:
                value = mutator(self.rows.get(key))
            finally:
                self.in_mutation = False
            if value is None:
                self.rows.pop(key, None)
            else:
                self.rows[key] = value
            return value

    def mutate_values(self, targets, mutator):
        targets = list(targets)
        if len({tuple(target) for target in targets}) != len(targets):
            raise ValueError("mutate_values targets must be unique")
        self._interleave(targets[0][1])
        with self.lock:
            self.mutations.append(tuple(tuple(target) for target in targets))
            databases = [self._peer(table) for table, _key in targets]
            self.in_mutation = True
            try:
                values = mutator(
                    tuple(
                        database.rows.get(key)
                        for database, (_table, key) in zip(
                            databases, targets, strict=True
                        )
                    )
                )
            finally:
                self.in_mutation = False
            if not isinstance(values, (list, tuple)) or len(values) != len(targets):
                raise TypeError("mutator must return one value per target")
            for database, (_table, key), value in zip(
                databases, targets, values, strict=True
            ):
                if value is None:
                    database.rows.pop(key, None)
                else:
                    database.rows[key] = value
            return tuple(values)


class FakeSharedState:
    def __init__(self, block_mode="defer"):
        self.databases = {}
        self.values = {
            "crypter_cooldown_hours": 24,
            "crypter_block_mode": block_mode,
        }
        for table in ("protected", "crypter_cooldowns", CRYPTER_EVENT_TABLE):
            self.databases[table] = FakeDatabase(table, self.databases)
        self.get_db_calls = []

    def get_db(self, table):
        if any(database.in_mutation for database in self.databases.values()):
            raise AssertionError("a mutation callback must never resolve storage")
        self.get_db_calls.append(table)
        if table not in self.databases:
            self.databases[table] = FakeDatabase(table, self.databases)
        return self.databases[table]


def protected_blob(links, deferred=None):
    blob = {
        "title": "Synthetic.Release.Example",
        "links": links,
        "password": "synthetic",
        "size_mb": 1024,
        "imdb_id": "tt0000000",
    }
    if deferred is not None:
        blob["deferred"] = deferred
    return json.dumps(blob)


class SweepServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.state = FakeSharedState()
        self.clock = FakeClock(NOW)
        self.service = CrypterCooldownService(self.state, clock=self.clock)
        self.ids = SequentialIds()
        self.patcher = mock.patch.object(
            CrypterCooldownService, "_new_identifier", lambda _self: self.ids()
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    # --- fixtures -------------------------------------------------------

    def store_package(self, package_id, indexes, deferred=None):
        self.state.databases["protected"].update_store(
            package_id,
            protected_blob(
                [
                    [f"https://filecrypt.invalid/container/{index}", CRYPTER]
                    for index in indexes
                ],
                deferred,
            ),
        )

    def store_decision(self, record):
        self.state.databases["crypter_cooldowns"].update_store(
            CRYPTER, encode_decision_record(record)
        )

    def stored_decision(self):
        return decode_decision_record(
            self.state.databases["crypter_cooldowns"].rows.get(CRYPTER),
            now=int(self.clock()),
        )

    def stored_defer(self, package_id):
        raw = self.state.databases["protected"].rows.get(package_id)
        return None if raw is None else decode_package_defer(json.loads(raw))

    def ledger(self):
        return self.state.databases[CRYPTER_EVENT_TABLE].rows.get(CRYPTER_EVENT_KEY)


class PrepareOfferTests(SweepServiceTestCase):
    def test_a_fresh_cohort_opens_a_sweep_and_leases_its_first_member(self):
        offer = self.service.prepare_offer(CRYPTER, cohort_inventory(5))

        self.assertEqual(
            {
                "mode": "sweep",
                "sweep_id": offer_id(901),
                "offer_id": offer_id(902),
                "link_fingerprint": fingerprint(1),
                "deadline_epoch": DEADLINE,
            },
            offer,
        )
        record = self.stored_decision()
        self.assertEqual("sweeping", record["state"])
        self.assertEqual("offered", record["members"][0]["result"])

    def test_preparing_an_offer_is_exactly_one_linkcrypter_transaction(self):
        self.service.prepare_offer(CRYPTER, cohort_inventory(5))

        self.assertEqual(
            [("crypter_cooldowns", CRYPTER)],
            self.state.databases["crypter_cooldowns"].mutations,
        )
        self.assertEqual([], self.state.databases["protected"].mutations)

    def test_fail_mode_never_creates_or_advances_cohort_state(self):
        self.state.values["crypter_block_mode"] = "fail"

        self.assertIsNone(self.service.prepare_offer(CRYPTER, cohort_inventory(5)))
        self.assertEqual({}, self.state.databases["crypter_cooldowns"].rows)
        self.assertEqual([], self.state.databases["crypter_cooldowns"].mutations)

    def test_a_persisted_legacy_cooldown_is_migrated_instead_of_overwritten(self):
        legacy = {
            "state": "cooldown",
            "reason_code": REASON,
            "first_seen_epoch": NOW - 60,
            "last_seen_epoch": NOW,
            "retry_after_epoch": NOW + COOLDOWN_SECONDS,
            "observations": [
                {
                    "package_id": package(index),
                    "link_fingerprint": fingerprint(index),
                    "seen_at_epoch": NOW,
                }
                for index in range(1, 4)
            ],
        }
        self.state.databases["crypter_cooldowns"].update_store(
            CRYPTER, json.dumps(legacy)
        )

        self.assertIsNone(self.service.prepare_offer(CRYPTER, cohort_inventory(5)))
        record = self.stored_decision()
        self.assertTrue(record["legacy_cooldown"])
        self.assertEqual(NOW + COOLDOWN_SECONDS, record["retry_after_epoch"])
        self.assertEqual(3, record["legacy_evidence_count"])

    def test_a_stale_version_one_observing_row_never_becomes_a_cohort(self):
        legacy = {
            "state": "observing",
            "reason_code": REASON,
            "first_seen_epoch": NOW,
            "last_seen_epoch": NOW,
            "retry_after_epoch": 0,
            "observations": [
                {
                    "package_id": package(1),
                    "link_fingerprint": fingerprint(1),
                    "seen_at_epoch": NOW,
                }
            ],
        }
        self.state.databases["crypter_cooldowns"].update_store(
            CRYPTER, json.dumps(legacy)
        )

        offer = self.service.prepare_offer(CRYPTER, cohort_inventory(5))

        self.assertEqual("sweep", offer["mode"])
        self.assertEqual("sweeping", self.stored_decision()["state"])


class CohortBlockedServiceTests(SweepServiceTestCase):
    def open_sweep(self, size=5, packages=None):
        packages = (
            {fingerprint(index): (package(index),) for index in range(1, size + 1)}
            if packages is None
            else packages
        )
        for package_ids in packages.values():
            for package_id in package_ids:
                self.store_package(package_id, [1])
        self.inventory = build_inventory(packages)
        return self.service.prepare_offer(CRYPTER, self.inventory)

    def report_blocked(self, offer, package_id):
        return self.service.record_cohort_blocked(
            CRYPTER,
            package_id,
            offer["link_fingerprint"],
            offer["sweep_id"],
            offer["offer_id"],
            REASON,
            self.inventory,
        )

    def test_decision_hold_and_ledger_commit_in_one_transaction(self):
        offer = self.open_sweep()
        self.state.databases["crypter_cooldowns"].mutations.clear()

        decision = self.report_blocked(offer, package(1))

        self.assertEqual("hold", decision["instruction"])
        self.assertEqual(
            [
                (
                    ("crypter_cooldowns", CRYPTER),
                    ("protected", package(1)),
                    (CRYPTER_EVENT_TABLE, CRYPTER_EVENT_KEY),
                )
            ],
            self.state.databases["crypter_cooldowns"].mutations,
        )
        self.assertEqual(
            {"observations": 1, "cooldowns": 0, "probes": 0},
            json.loads(self.ledger()),
        )

    def test_a_hold_binds_every_live_occurrence_of_the_tested_fingerprint(self):
        offer = self.open_sweep(
            packages={
                fingerprint(1): (package(1), package(2)),
                fingerprint(2): (package(3),),
                fingerprint(3): (package(4),),
            }
        )

        self.report_blocked(offer, package(1))

        for package_id in (package(1), package(2)):
            deferred = self.stored_defer(package_id)
            self.assertEqual([fingerprint(1)], deferred["link_fingerprints"])
            self.assertEqual(offer["sweep_id"], deferred["sweep_id"])
            self.assertEqual(DEADLINE, deferred["retry_after_epoch"])
            self.assertEqual(1, deferred["observation_holds"])
        self.assertIsNone(self.stored_defer(package(3)))

    def test_two_filecrypt_links_in_one_package_are_separate_cohort_members(self):
        self.store_package(package(1), [1, 2])
        self.store_package(package(2), [3])
        inventory = build_inventory(
            {
                fingerprint(1): (package(1),),
                fingerprint(2): (package(1),),
                fingerprint(3): (package(2),),
            }
        )
        self.inventory = inventory

        offer = self.service.prepare_offer(CRYPTER, inventory)
        self.assertEqual(fingerprint(1), offer["link_fingerprint"])
        self.report_blocked(offer, package(1))
        record = self.stored_decision()
        self.assertEqual(3, len(record["members"]))
        self.assertEqual(
            [fingerprint(1)], self.stored_defer(package(1))["link_fingerprints"]
        )

        second = self.service.prepare_offer(CRYPTER, inventory)
        self.assertEqual(fingerprint(2), second["link_fingerprint"])

    def test_a_second_blocked_link_joins_the_hold_instead_of_replacing_it(self):
        self.store_package(package(1), [1, 2])
        self.store_package(package(2), [3])
        self.inventory = build_inventory(
            {
                fingerprint(1): (package(1),),
                fingerprint(2): (package(1),),
                fingerprint(3): (package(2),),
            }
        )

        first = self.service.prepare_offer(CRYPTER, self.inventory)
        self.report_blocked(first, package(1))
        second = self.service.prepare_offer(CRYPTER, self.inventory)
        self.report_blocked(second, package(1))

        deferred = self.stored_defer(package(1))
        self.assertEqual(
            [fingerprint(1), fingerprint(2)],
            deferred["link_fingerprints"],
            "both tested links of one package stay held",
        )
        self.assertTrue(
            package_defer_covers_fingerprint(deferred, fingerprint(1))
            and package_defer_covers_fingerprint(deferred, fingerprint(2))
        )
        self.assertFalse(package_defer_covers_fingerprint(deferred, fingerprint(3)))

    def test_a_duplicate_occurrence_of_one_link_shares_a_single_member(self):
        self.store_package(package(1), [1, 1])
        self.store_package(package(2), [2])
        self.inventory = build_inventory(
            {
                fingerprint(1): (package(1), package(1)),
                fingerprint(2): (package(2),),
            }
        )

        offer = self.service.prepare_offer(CRYPTER, self.inventory)
        self.report_blocked(offer, package(1))
        self.report_blocked(offer, package(1))

        self.assertEqual(
            2, len(self.stored_decision()["members"]), "one link is one member"
        )
        self.assertEqual(
            [fingerprint(1)], self.stored_defer(package(1))["link_fingerprints"]
        )

    def test_an_inventory_outage_ends_the_generation_fail_closed(self):
        offer = self.open_sweep()
        self.report_blocked(offer, package(1))
        second = self.service.prepare_offer(CRYPTER, self.inventory)
        # These synthetic fingerprints are not the digests of the stored links,
        # so any readable row would disprove the reporter outright; removing the
        # row is what leaves its ownership genuinely unknown.
        self.state.databases["protected"].rows.pop(package(2))

        decision = self.service.record_cohort_blocked(
            CRYPTER,
            package(2),
            second["link_fingerprint"],
            second["sweep_id"],
            second["offer_id"],
            REASON,
            None,
        )

        self.assertEqual("hold", decision["instruction"])
        self.assertEqual("individual", decision["state"])
        record = self.stored_decision()
        self.assertEqual("inventory_unavailable", record["reason"])
        self.assertEqual([second["link_fingerprint"]], record["hold_fingerprints"])
        current = decision_snapshot(record, now=NOW)
        self.assertFalse(
            package_defer_is_active(self.stored_defer(package(1)), current, now=NOW),
            "the earlier member of the tainted generation is released",
        )
        # Unproven ownership authorizes no package at all, so the reporter does
        # not even get a row of its own back.
        self.assertNotIn(package(2), self.state.databases["protected"].rows)
        follow_up = self.service.prepare_offer(CRYPTER, self.inventory)
        self.assertEqual("individual", follow_up["mode"])
        self.assertEqual("individual", self.stored_decision()["state"])

    def test_an_inventory_outage_still_defers_to_a_readable_row(self):
        offer = self.open_sweep()
        self.report_blocked(offer, package(1))
        second = self.service.prepare_offer(CRYPTER, self.inventory)
        decisions = dict(self.state.databases["crypter_cooldowns"].rows)
        packages = dict(self.state.databases["protected"].rows)
        self.state.databases["crypter_cooldowns"].mutations.clear()

        decision = self.service.record_cohort_blocked(
            CRYPTER,
            package(2),
            second["link_fingerprint"],
            second["sweep_id"],
            second["offer_id"],
            REASON,
            None,
        )

        # The row of package(2) is readable and does not carry the reported
        # link, so the outage never gets as far as tainting the generation.
        self.assertEqual("stale", decision["instruction"])
        self.assertEqual("sweeping", self.stored_decision()["state"])
        self.assertEqual(decisions, self.state.databases["crypter_cooldowns"].rows)
        self.assertEqual(packages, self.state.databases["protected"].rows)
        self.assertEqual([], self.state.databases["crypter_cooldowns"].mutations)

    def test_the_conclusive_cohort_counts_one_cooldown_and_holds_every_member(self):
        offer = self.open_sweep()
        decision = None
        for index in range(1, MINIMUM_CONCLUSIVE_COHORT_SIZE + 1):
            self.assertEqual(fingerprint(index), offer["link_fingerprint"])
            decision = self.report_blocked(offer, package(index))
            offer = self.service.prepare_offer(CRYPTER, self.inventory) or offer

        self.assertEqual("cooldown", decision["instruction"])
        self.assertEqual(
            {"observations": 5, "cooldowns": 1, "probes": 0}, json.loads(self.ledger())
        )
        # Only the reported fingerprint's rows are rewritten, so the earlier
        # provisional holds keep their marker; the cohort cooldown is what keeps
        # every one of them active until its own retry deadline.
        self.assertEqual(
            [1, 1, 1, 1, 0],
            [
                self.stored_defer(package(index))["observation_holds"]
                for index in range(1, MINIMUM_CONCLUSIVE_COHORT_SIZE + 1)
            ],
        )
        cooled = decision_snapshot(self.stored_decision(), now=NOW)
        for index in range(1, MINIMUM_CONCLUSIVE_COHORT_SIZE + 1):
            self.assertTrue(
                package_defer_is_active(
                    self.stored_defer(package(index)), cooled, now=NOW
                )
            )

    def test_a_stale_report_writes_nothing_at_all(self):
        offer = self.open_sweep()
        self.state.databases["crypter_cooldowns"].mutations.clear()
        before = dict(self.state.databases["crypter_cooldowns"].rows)

        decision = self.service.record_cohort_blocked(
            CRYPTER,
            package(1),
            offer["link_fingerprint"],
            OTHER_SWEEP_ID,
            offer["offer_id"],
            REASON,
            self.inventory,
        )

        self.assertEqual("stale", decision["instruction"])
        self.assertEqual(before, self.state.databases["crypter_cooldowns"].rows)
        self.assertIsNone(self.stored_defer(package(1)))
        self.assertIsNone(self.ledger())

    def test_no_decision_row_ledger_or_response_can_carry_a_url_or_package_id(self):
        offer = self.open_sweep()
        decision = self.report_blocked(offer, package(1))
        stored = self.state.databases["crypter_cooldowns"].rows[CRYPTER]

        for label, text in (
            ("decision", json.dumps(decision)),
            ("decision row", stored),
            ("event ledger", self.ledger()),
        ):
            with self.subTest(carrier=label):
                self.assertNotIn("filecrypt.invalid", text)
                self.assertNotIn("http", text)
                self.assertNotIn("Quasarr_movies_", text)
                self.assertNotIn("Synthetic.Release", text)

    def test_fail_mode_answers_without_touching_any_row(self):
        offer = self.open_sweep()
        self.state.values["crypter_block_mode"] = "fail"
        before = dict(self.state.databases["crypter_cooldowns"].rows)
        self.state.databases["crypter_cooldowns"].mutations.clear()

        decision = self.report_blocked(offer, package(1))

        self.assertEqual("legacy_failure", decision["instruction"])
        self.assertEqual("available", decision["state"])
        self.assertEqual("none", decision["hold_type"])
        self.assertEqual(0, decision["retry_after_epoch"])
        self.assertEqual(before, self.state.databases["crypter_cooldowns"].rows)
        self.assertEqual([], self.state.databases["crypter_cooldowns"].mutations)
        self.assertIsNone(self.ledger())

    def test_a_ledger_overflow_rolls_back_the_whole_transition(self):
        offer = self.open_sweep()
        self.state.databases[CRYPTER_EVENT_TABLE].update_store(
            CRYPTER_EVENT_KEY,
            json.dumps({"observations": 10**1000 - 1, "cooldowns": 0, "probes": 0}),
        )
        before_decision = self.state.databases["crypter_cooldowns"].rows[CRYPTER]

        with self.assertRaises(OverflowError):
            self.report_blocked(offer, package(1))

        self.assertEqual(
            before_decision, self.state.databases["crypter_cooldowns"].rows[CRYPTER]
        )
        self.assertIsNone(self.stored_defer(package(1)))

    def test_an_oversized_decision_row_rolls_the_transition_back(self):
        offer = self.open_sweep()
        stored = self.state.databases["crypter_cooldowns"].rows[CRYPTER]
        # Large enough to still decode the frozen sweep, too small for the row
        # the accepted report would produce.
        limit = len(stored.encode("utf-8")) + 4

        with (
            mock.patch(
                "quasarr.providers.crypter_sweeps.MAXIMUM_COHORT_RECORD_BYTES", limit
            ),
            self.assertRaises(OverflowError),
        ):
            self.report_blocked(offer, package(1))

        self.assertEqual(
            stored, self.state.databases["crypter_cooldowns"].rows[CRYPTER]
        )
        self.assertIsNone(self.stored_defer(package(1)))
        self.assertIsNone(self.ledger())

    def test_a_package_fingerprint_overflow_rolls_the_transition_back(self):
        offer = self.open_sweep()
        full_defer = {
            "crypter": CRYPTER,
            "reason_code": REASON,
            "since_epoch": NOW,
            "retry_after_epoch": DEADLINE,
            "probe_requested": False,
            "observation_holds": 1,
            "schema_version": 2,
            "sweep_id": offer["sweep_id"],
            "link_fingerprints": [
                fingerprint(index) for index in range(101, 101 + MAXIMUM_COHORT_SIZE)
            ],
        }
        self.store_package(package(1), [1], deferred=full_defer)
        decision_before = self.state.databases["crypter_cooldowns"].rows[CRYPTER]
        package_before = self.state.databases["protected"].rows[package(1)]

        with self.assertRaises(OverflowError):
            self.report_blocked(offer, package(1))

        self.assertEqual(
            decision_before, self.state.databases["crypter_cooldowns"].rows[CRYPTER]
        )
        self.assertEqual(
            package_before, self.state.databases["protected"].rows[package(1)]
        )
        self.assertIsNone(self.ledger())

    def test_expiry_during_an_accepted_replay_cannot_recreate_a_hold(self):
        offer = self.open_sweep()
        first = self.report_blocked(offer, package(1))
        self.assertEqual("hold", first["instruction"])
        self.state.databases["protected"].update_store(
            package(1), protected_blob([["https://filecrypt.invalid/1", CRYPTER]])
        )
        self.clock.now = DEADLINE + 1

        replay = self.report_blocked(offer, package(1))

        self.assertEqual("hold", replay["instruction"])
        self.assertEqual("individual", self.stored_decision()["state"])
        self.assertIsNone(self.stored_defer(package(1)))


class CohortAccessServiceTests(SweepServiceTestCase):
    def setUp(self):
        super().setUp()
        for index in range(1, 6):
            self.store_package(package(index), [index])
        self.inventory = cohort_inventory(5)

    def report_access(self, offer, package_id, value):
        return self.service.record_cohort_access(
            CRYPTER,
            package_id,
            offer["link_fingerprint"],
            offer["sweep_id"],
            offer["offer_id"],
            value,
            self.inventory,
        )

    def test_clear_releases_every_generation_hold_after_the_decision_commits(self):
        first = self.service.prepare_offer(CRYPTER, self.inventory)
        self.service.record_cohort_blocked(
            CRYPTER,
            package(1),
            first["link_fingerprint"],
            first["sweep_id"],
            first["offer_id"],
            REASON,
            self.inventory,
        )
        second = self.service.prepare_offer(CRYPTER, self.inventory)

        decision = self.report_access(second, package(2), "clear")

        self.assertTrue(decision["cleared"])
        self.assertEqual("healthy", self.stored_decision()["state"])
        self.assertEqual([fingerprint(1)], self.stored_decision()["retest_members"])
        self.assertIsNone(self.stored_defer(package(1)))
        self.assertIsNone(self.stored_defer(package(2)))

    def test_an_accepted_block_replay_after_clear_cannot_recreate_a_hold(self):
        first = self.service.prepare_offer(CRYPTER, self.inventory)
        self.service.record_cohort_blocked(
            CRYPTER,
            package(1),
            first["link_fingerprint"],
            first["sweep_id"],
            first["offer_id"],
            REASON,
            self.inventory,
        )
        second = self.service.prepare_offer(CRYPTER, self.inventory)
        self.report_access(second, package(2), "clear")
        healthy = self.state.databases["crypter_cooldowns"].rows[CRYPTER]

        replay = self.service.record_cohort_blocked(
            CRYPTER,
            package(1),
            first["link_fingerprint"],
            first["sweep_id"],
            first["offer_id"],
            REASON,
            self.inventory,
        )

        self.assertEqual("hold", replay["instruction"])
        self.assertEqual(
            healthy, self.state.databases["crypter_cooldowns"].rows[CRYPTER]
        )
        self.assertIsNone(self.stored_defer(package(1)))

    def test_a_failed_physical_cleanup_never_changes_the_clear_response(self):
        first = self.service.prepare_offer(CRYPTER, self.inventory)
        self.service.record_cohort_blocked(
            CRYPTER,
            package(1),
            first["link_fingerprint"],
            first["sweep_id"],
            first["offer_id"],
            REASON,
            self.inventory,
        )
        second = self.service.prepare_offer(CRYPTER, self.inventory)

        with mock.patch.object(
            CrypterCooldownService,
            "clear_crypter_generation_holds",
            side_effect=RuntimeError("protected storage unavailable"),
        ):
            decision = self.report_access(second, package(2), "clear")

        self.assertTrue(decision["cleared"])
        healthy = self.stored_decision()
        self.assertEqual("healthy", healthy["state"])
        # The surviving metadata is logically dead the moment healthy commits.
        self.assertFalse(
            package_defer_is_active(
                self.stored_defer(package(1)),
                decision_snapshot(healthy, now=NOW),
                now=NOW,
            )
        )

    def test_a_stale_access_report_runs_no_cleanup_at_all(self):
        offer = self.service.prepare_offer(CRYPTER, self.inventory)

        with mock.patch.object(
            CrypterCooldownService, "clear_crypter_generation_holds"
        ) as cleanup:
            decision = self.service.record_cohort_access(
                CRYPTER,
                package(1),
                offer["link_fingerprint"],
                OTHER_SWEEP_ID,
                offer["offer_id"],
                "clear",
                self.inventory,
            )

        self.assertEqual("stale", decision["instruction"])
        cleanup.assert_not_called()

    def test_unknown_writes_no_hold_and_no_statistic(self):
        offer = self.service.prepare_offer(CRYPTER, self.inventory)

        decision = self.report_access(offer, package(1), "unknown")

        self.assertEqual("unknown", decision["accepted"])
        self.assertEqual("sweeping", self.stored_decision()["state"])
        self.assertIsNone(self.stored_defer(package(1)))
        self.assertIsNone(self.ledger())

    def test_fail_mode_acknowledges_nothing_and_writes_nothing(self):
        offer = self.service.prepare_offer(CRYPTER, self.inventory)
        self.state.values["crypter_block_mode"] = "fail"
        before = dict(self.state.databases["crypter_cooldowns"].rows)

        decision = self.report_access(offer, package(1), "clear")

        self.assertFalse(decision["cleared"])
        self.assertEqual("available", decision["state"])
        self.assertEqual(before, self.state.databases["crypter_cooldowns"].rows)

    def test_a_clear_landing_between_read_and_write_beats_the_final_block(self):
        """A competing CLEAR commits first, so the late BLOCKED cannot cool down."""
        offer = self.service.prepare_offer(CRYPTER, self.inventory)
        for index in range(2, 6):
            candidate = self.service.prepare_offer(CRYPTER, self.inventory)
            if candidate is None:
                break
            self.service.record_cohort_blocked(
                CRYPTER,
                package(index),
                candidate["link_fingerprint"],
                candidate["sweep_id"],
                candidate["offer_id"],
                REASON,
                self.inventory,
            )

        healthy = encode_decision_record(
            healthy_record(sweep_id=offer["sweep_id"], retest=[fingerprint(2)])
        )

        def clear_first(_key):
            self.state.databases["crypter_cooldowns"].rows[CRYPTER] = healthy

        self.state.databases["crypter_cooldowns"].before_write = clear_first
        decision = self.service.record_cohort_blocked(
            CRYPTER,
            package(1),
            offer["link_fingerprint"],
            offer["sweep_id"],
            offer["offer_id"],
            REASON,
            self.inventory,
        )

        self.assertEqual("stale", decision["instruction"])
        self.assertEqual("healthy", self.stored_decision()["state"])
        self.assertEqual(0, json.loads(self.ledger() or "{}").get("cooldowns", 0))


class LegacyObserveCollisionTests(SweepServiceTestCase):
    def test_a_version_one_report_never_overwrites_a_version_two_row(self):
        for record in (
            sweeping_record(5),
            cohort_cooldown_record(),
            healthy_record(),
            individual_record("cohort_oversized"),
            legacy_cooldown_record(),
        ):
            with self.subTest(state=record.get("reason") or record["state"]):
                stored = encode_decision_record(record)
                self.state.databases["crypter_cooldowns"].rows[CRYPTER] = stored

                self.service.observe(CRYPTER, package(1), fingerprint(1), REASON)

                self.assertEqual(
                    stored, self.state.databases["crypter_cooldowns"].rows[CRYPTER]
                )

    def test_a_version_one_report_reads_the_version_two_state(self):
        self.state.databases["crypter_cooldowns"].rows[CRYPTER] = (
            encode_decision_record(cohort_cooldown_record(6))
        )

        decision = self.service.observe(CRYPTER, package(1), fingerprint(1), REASON)

        self.assertEqual("cooldown", decision["state"])
        self.assertEqual(6, decision["evidence_count"])
        self.assertEqual(NOW + COOLDOWN_SECONDS, decision["package_retry_after_epoch"])
        self.assertFalse(decision["recorded"])
        self.assertFalse(decision["cooldown_started"])
        self.assertIsNone(self.ledger())

    def test_three_version_one_reports_against_a_sweep_never_start_a_cooldown(self):
        self.state.databases["crypter_cooldowns"].rows[CRYPTER] = (
            encode_decision_record(sweeping_record(5))
        )

        for index in range(1, 4):
            decision = self.service.observe(
                CRYPTER, package(index), fingerprint(index), REASON
            )
            self.assertEqual("observing", decision["state"])

        self.assertEqual("sweeping", self.stored_decision()["state"])
        self.assertIsNone(self.ledger())


class CleanVersionTwoExpiryTests(SweepServiceTestCase):
    def test_an_expired_valid_record_is_removed_without_an_invalid_warning(self):
        for record in (
            cohort_cooldown_record(retry_after=NOW + 10),
            healthy_record(until=NOW + 10),
            individual_record(until=NOW + 10),
        ):
            with self.subTest(state=record["state"]):
                self.state.databases["crypter_cooldowns"].rows[CRYPTER] = (
                    encode_decision_record(record)
                )
                self.clock.now = NOW + 10

                with mock.patch("quasarr.providers.crypter_cooldowns.warn") as warned:
                    snapshot = self.service.snapshot(CRYPTER)

                self.assertEqual("available", snapshot["state"])
                warned.assert_not_called()
                self.assertEqual({}, self.state.databases["crypter_cooldowns"].rows)
                self.clock.now = NOW

    def test_a_past_deadline_sweep_is_never_silently_deleted_on_a_read(self):
        self.state.databases["crypter_cooldowns"].rows[CRYPTER] = (
            encode_decision_record(sweeping_record(5))
        )
        self.clock.now = DEADLINE + 1

        with mock.patch("quasarr.providers.crypter_cooldowns.warn") as warned:
            snapshot = self.service.snapshot(CRYPTER)

        self.assertEqual("observing", snapshot["state"])
        warned.assert_not_called()
        self.assertIn(CRYPTER, self.state.databases["crypter_cooldowns"].rows)

    def test_a_genuinely_malformed_row_is_still_reported(self):
        self.state.databases["crypter_cooldowns"].rows[CRYPTER] = '{"state": 7}'

        with mock.patch("quasarr.providers.crypter_cooldowns.warn") as warned:
            self.service.snapshot(CRYPTER)

        warned.assert_called_once()


class RealDatabaseSweepTests(unittest.TestCase):
    """The atomic wrappers against the real SQLite layer, one file per test."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dbfile = os.path.join(self.tmpdir.name, "Quasarr.db")
        self.original_values = provider_shared_state.values
        self.original_lock = provider_shared_state.lock
        provider_shared_state.values = {"dbfile": self.dbfile}
        provider_shared_state.lock = None
        self.addCleanup(self.restore_shared_state)
        self.databases = {}
        self.clock = FakeClock(NOW)
        self.state = self.RealSharedState(self.databases, self.dbfile)
        self.service = CrypterCooldownService(self.state, clock=self.clock)
        self.ids = SequentialIds()
        patcher = mock.patch.object(
            CrypterCooldownService, "_new_identifier", lambda _self: self.ids()
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    class RealSharedState:
        def __init__(self, databases, dbfile):
            self._databases = databases
            self.values = {
                "dbfile": dbfile,
                "crypter_cooldown_hours": 24,
                "crypter_block_mode": "defer",
            }

        def get_db(self, table):
            if table not in self._databases:
                self._databases[table] = DataBase(table)
            return self._databases[table]

    def restore_shared_state(self):
        for database in self.databases.values():
            database._conn.close()
        self.databases.clear()
        provider_shared_state.values = self.original_values
        provider_shared_state.lock = self.original_lock

    def store_packages(self, count):
        protected = self.state.get_db("protected")
        for index in range(1, count + 1):
            protected.update_store(
                package(index),
                protected_blob(
                    [[f"https://filecrypt.invalid/container/{index}", CRYPTER]]
                ),
            )

    def package_for(self, offer, inventory):
        """Real fingerprints are hash-ordered, so never assume a member's owner."""
        return next(
            candidate.occurrences[0].package_id
            for candidate in inventory.candidates
            if candidate.fingerprint == offer["link_fingerprint"]
        )

    def drive_full_cohort(self, size=5):
        self.store_packages(size)
        inventory = cohort_inventory(size)
        decision = None
        for _ in range(size):
            offer = self.service.prepare_offer(CRYPTER, inventory)
            decision = self.service.record_cohort_blocked(
                CRYPTER,
                self.package_for(offer, inventory),
                offer["link_fingerprint"],
                offer["sweep_id"],
                offer["offer_id"],
                REASON,
                inventory,
            )
        return inventory, decision

    def test_a_complete_cohort_commits_decision_holds_and_ledger_to_disk(self):
        inventory, decision = self.drive_full_cohort()

        self.assertEqual("cooldown", decision["instruction"])
        reopened = DataBase("crypter_cooldowns")
        self.addCleanup(reopened._conn.close)
        record = decode_decision_record(reopened.retrieve(CRYPTER), now=NOW)
        self.assertEqual("cooldown", record["state"])
        self.assertEqual(5, record["cohort_size"])

        events = DataBase(CRYPTER_EVENT_TABLE)
        self.addCleanup(events._conn.close)
        self.assertEqual(
            {"observations": 5, "cooldowns": 1, "probes": 0},
            json.loads(events.retrieve(CRYPTER_EVENT_KEY)),
        )

        protected = DataBase("protected")
        self.addCleanup(protected._conn.close)
        cooled = decision_snapshot(record, now=NOW)
        for index in range(1, 6):
            deferred = decode_package_defer(
                json.loads(protected.retrieve(package(index)))
            )
            self.assertEqual(record["sweep_id"], deferred["sweep_id"])
            self.assertEqual(1, len(deferred["link_fingerprints"]))
            self.assertIn(
                deferred["link_fingerprints"][0],
                [entry["link_fingerprint"] for entry in record["members"]],
            )
            # Every member stays held by the cohort cooldown itself, whatever
            # deadline its own report happened to write.
            self.assertTrue(package_defer_is_active(deferred, cooled, now=NOW))
        del inventory

    def test_a_ledger_overflow_leaves_every_row_on_disk_untouched(self):
        self.store_packages(5)
        inventory = cohort_inventory(5)
        offer = self.service.prepare_offer(CRYPTER, inventory)
        self.state.get_db(CRYPTER_EVENT_TABLE).update_store(
            CRYPTER_EVENT_KEY,
            json.dumps({"observations": 10**1000 - 1, "cooldowns": 0, "probes": 0}),
        )
        before = self.state.get_db("crypter_cooldowns").retrieve(CRYPTER)

        with self.assertRaises(OverflowError):
            self.service.record_cohort_blocked(
                CRYPTER,
                self.package_for(offer, inventory),
                offer["link_fingerprint"],
                offer["sweep_id"],
                offer["offer_id"],
                REASON,
                inventory,
            )

        reopened = DataBase("crypter_cooldowns")
        self.addCleanup(reopened._conn.close)
        self.assertEqual(before, reopened.retrieve(CRYPTER))
        protected = DataBase("protected")
        self.addCleanup(protected._conn.close)
        self.assertNotIn("deferred", json.loads(protected.retrieve(package(1))))

    def test_a_competing_clear_committed_between_read_and_write_wins(self):
        self.store_packages(5)
        inventory = cohort_inventory(5)
        offers = []
        for _ in range(2):
            offers.append(self.service.prepare_offer(CRYPTER, inventory))
        for offer in offers[1:]:
            self.service.record_cohort_blocked(
                CRYPTER,
                self.package_for(offer, inventory),
                offer["link_fingerprint"],
                offer["sweep_id"],
                offer["offer_id"],
                REASON,
                inventory,
            )

        competitor = DataBase("crypter_cooldowns")
        self.addCleanup(competitor._conn.close)
        original_mutate_values = DataBase.mutate_values
        interleaved = {"done": False}

        def clear_first(database, targets, mutator):
            if not interleaved["done"]:
                interleaved["done"] = True
                competitor.update_store(
                    CRYPTER,
                    encode_decision_record(
                        healthy_record(
                            sweep_id=offers[0]["sweep_id"], retest=[fingerprint(2)]
                        )
                    ),
                )
            return original_mutate_values(database, targets, mutator)

        with mock.patch.object(DataBase, "mutate_values", clear_first):
            decision = self.service.record_cohort_blocked(
                CRYPTER,
                self.package_for(offers[0], inventory),
                offers[0]["link_fingerprint"],
                offers[0]["sweep_id"],
                offers[0]["offer_id"],
                REASON,
                inventory,
            )

        self.assertTrue(interleaved["done"])
        self.assertEqual("stale", decision["instruction"])
        reopened = DataBase("crypter_cooldowns")
        self.addCleanup(reopened._conn.close)
        self.assertEqual(
            "healthy",
            decode_decision_record(reopened.retrieve(CRYPTER), now=NOW)["state"],
        )

    def test_the_decision_transaction_reads_no_further_storage(self):
        self.store_packages(5)
        inventory = cohort_inventory(5)
        offer = self.service.prepare_offer(CRYPTER, inventory)
        original_retrieve_all = DataBase.retrieve_all_titles

        def reject(database):
            raise AssertionError("a transition must enumerate before it mutates")

        with mock.patch.object(DataBase, "retrieve_all_titles", reject):
            decision = self.service.record_cohort_blocked(
                CRYPTER,
                self.package_for(offer, inventory),
                offer["link_fingerprint"],
                offer["sweep_id"],
                offer["offer_id"],
                REASON,
                inventory,
            )

        self.assertEqual("hold", decision["instruction"])
        self.assertIs(original_retrieve_all, DataBase.retrieve_all_titles)

    def test_a_clear_after_a_cooldown_reopens_every_held_package(self):
        inventory, _decision = self.drive_full_cohort()
        reopened = DataBase("crypter_cooldowns")
        self.addCleanup(reopened._conn.close)
        cooled = decode_decision_record(reopened.retrieve(CRYPTER), now=NOW)
        member_fingerprint = cooled["members"][2]["link_fingerprint"]
        probe = self.service.prepare_offer(
            CRYPTER,
            inventory,
            mode="probe",
            preferred_fingerprint=member_fingerprint,
        )
        self.assertEqual(member_fingerprint, probe["link_fingerprint"])

        decision = self.service.record_cohort_access(
            CRYPTER,
            self.package_for(probe, inventory),
            probe["link_fingerprint"],
            probe["sweep_id"],
            probe["offer_id"],
            "clear",
            inventory,
        )

        self.assertTrue(decision["cleared"])
        protected = DataBase("protected")
        self.addCleanup(protected._conn.close)
        for index in range(1, 6):
            self.assertNotIn("deferred", json.loads(protected.retrieve(package(index))))

    def test_concurrent_writers_never_lose_a_committed_member_result(self):
        self.store_packages(5)
        inventory = cohort_inventory(5)
        offers = [self.service.prepare_offer(CRYPTER, inventory) for _ in range(2)]
        started = threading.Event()
        errors = []

        def report(offer, package_id):
            try:
                started.wait(5)
                self.service.record_cohort_blocked(
                    CRYPTER,
                    package_id,
                    offer["link_fingerprint"],
                    offer["sweep_id"],
                    offer["offer_id"],
                    REASON,
                    inventory,
                )
            except Exception as error:  # pragma: no cover - surfaced by the test
                errors.append(error)

        threads = [
            threading.Thread(
                target=report, args=(offer, self.package_for(offer, inventory))
            )
            for offer in offers
        ]
        for thread in threads:
            thread.start()
        started.set()
        for thread in threads:
            thread.join(10)

        self.assertEqual([], errors)
        reopened = DataBase("crypter_cooldowns")
        self.addCleanup(reopened._conn.close)
        record = decode_decision_record(reopened.retrieve(CRYPTER), now=NOW)
        self.assertEqual(
            ["blocked", "blocked"],
            [entry["result"] for entry in record["members"][:2]],
        )
        events = DataBase(CRYPTER_EVENT_TABLE)
        self.addCleanup(events._conn.close)
        self.assertEqual(
            2, json.loads(events.retrieve(CRYPTER_EVENT_KEY))["observations"]
        )

    def test_concurrent_leases_never_share_an_offer_identity(self):
        self.store_packages(5)
        inventory = cohort_inventory(5)
        started = threading.Event()
        offers = []
        errors = []

        def lease():
            try:
                started.wait(5)
                offers.append(self.service.prepare_offer(CRYPTER, inventory))
            except Exception as error:  # pragma: no cover - surfaced by the test
                errors.append(error)

        threads = [threading.Thread(target=lease) for _ in range(4)]
        for thread in threads:
            thread.start()
        started.set()
        for thread in threads:
            thread.join(10)

        self.assertEqual([], errors)
        identifiers = [offer["offer_id"] for offer in offers]
        self.assertEqual(4, len(identifiers))
        self.assertEqual(len(set(identifiers)), len(identifiers))
        reopened = DataBase("crypter_cooldowns")
        self.addCleanup(reopened._conn.close)
        record = decode_decision_record(reopened.retrieve(CRYPTER), now=NOW)
        self.assertTrue(set(identifiers).issubset(record["used_offer_ids"]))

    def test_a_committed_cooldown_survives_a_restart_of_every_reader(self):
        inventory, decision = self.drive_full_cohort()
        self.assertEqual("cooldown", decision["instruction"])

        for database in self.databases.values():
            database._conn.close()
        self.databases.clear()

        record = decode_decision_record(
            self.state.get_db("crypter_cooldowns").retrieve(CRYPTER), now=NOW
        )
        self.assertIsNotNone(record, "a coherent cooldown must reload after a restart")
        self.assertEqual(NOW, record["opened_epoch"])
        self.assertEqual(DEADLINE, record["deadline_epoch"])
        for entry in record["members"]:
            self.assertLessEqual(record["opened_epoch"], entry["tested_epoch"])
            self.assertLessEqual(entry["tested_epoch"], record["deadline_epoch"])
            self.assertIn(entry["offer_id"], record["used_offer_ids"])
        self.assertEqual(
            len(record["used_offer_ids"]), len(set(record["used_offer_ids"]))
        )

        replay = self.service.record_cohort_blocked(
            CRYPTER,
            self.package_for(
                {"link_fingerprint": record["members"][0]["link_fingerprint"]},
                inventory,
            ),
            record["members"][0]["link_fingerprint"],
            record["sweep_id"],
            record["members"][0]["offer_id"],
            REASON,
            inventory,
        )
        self.assertEqual("hold", replay["instruction"])
        self.assertEqual("sweeping", replay["state"])
        self.assertEqual("provisional", replay["hold_type"])


if __name__ == "__main__":
    unittest.main()
