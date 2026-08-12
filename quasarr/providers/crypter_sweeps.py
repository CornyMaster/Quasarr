# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import dataclasses
import json
import re

SWEEP_SCHEMA_VERSION = 2
SWEEP_WINDOW_SECONDS = 15 * 60
MINIMUM_CONCLUSIVE_COHORT_SIZE = 5
MAXIMUM_COHORT_SIZE = 100
MAXIMUM_COHORT_OCCURRENCES = 1000
HEALTHY_SUPPRESSION_SECONDS = 15 * 60
OFFER_LEASE_SECONDS = 2 * 60
MAXIMUM_COHORT_RECORD_BYTES = 256 * 1024

SWEEP_REASON_CODES = frozenset({"ip_block_suspected"})
DEFAULT_SWEEP_REASON_CODE = "ip_block_suspected"
INDIVIDUAL_REASONS = frozenset(
    {
        "cohort_too_small",
        "cohort_oversized",
        "sweep_expired",
        "sweep_inconclusive",
        "legacy_v1_hold",
    }
)
MEMBER_RESULTS = frozenset({"pending", "offered", "blocked", "unknown"})
RESPONSE_INSTRUCTIONS = frozenset({"", "hold", "cooldown", "legacy_failure"})
OFFER_MODES = frozenset({"sweep", "retest", "individual", "probe"})

# A sweep needs two members to be a cohort at all; one candidate is handled
# individually and can never freeze a cooldown denominator.
MINIMUM_SWEEP_COHORT_SIZE = 2

_IDENTIFIER_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MEMBER_KEYS = frozenset(
    {
        "link_fingerprint",
        "result",
        "tested_epoch",
        "offer_id",
        "offer_expires_epoch",
        "response_instruction",
    }
)
_OFFER_KEYS = frozenset(
    {
        "offer_id",
        "offer_fingerprint",
        "offer_expires_epoch",
        "mode",
        "response_instruction",
    }
)
_RECORD_KEYS = {
    "sweeping": frozenset(
        {
            "schema_version",
            "state",
            "reason_code",
            "sweep_id",
            "opened_epoch",
            "deadline_epoch",
            "members",
        }
    ),
    "cooldown": frozenset(
        {
            "schema_version",
            "state",
            "reason_code",
            "sweep_id",
            "members",
            "cohort_size",
            "retry_after_epoch",
            "live_offer",
        }
    ),
    "legacy_cooldown": frozenset(
        {
            "schema_version",
            "state",
            "reason_code",
            "legacy_cooldown",
            "retry_after_epoch",
            "legacy_evidence_count",
        }
    ),
    "healthy": frozenset(
        {
            "schema_version",
            "state",
            "sweep_id",
            "until_epoch",
            "retest_members",
            "live_offer",
        }
    ),
    "individual": frozenset(
        {
            "schema_version",
            "state",
            "reason",
            "generation_id",
            "until_epoch",
            "live_offer",
        }
    ),
}
_PACKAGE_CANDIDATE_KEYS = frozenset({"title", "password"})


def _identifier(value, field_name):
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f'Invalid linkcrypter decision field "{field_name}"')
    return value


def _optional_identifier(value, field_name):
    if value == "":
        return value
    return _identifier(value, field_name)


def _fingerprint(value, field_name):
    if not isinstance(value, str) or not _FINGERPRINT_PATTERN.fullmatch(value):
        raise ValueError(f'Invalid linkcrypter decision field "{field_name}"')
    return value


def _epoch(value, field_name):
    if type(value) is not int or value < 0:
        raise ValueError(f'Invalid linkcrypter decision field "{field_name}"')
    return value


def _choice(value, allowed, field_name):
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f'Invalid linkcrypter decision field "{field_name}"')
    return value


def _ascending_fingerprints(value, field_name, minimum=0):
    """Fingerprint order is ascending, which also proves cohort uniqueness."""
    if not isinstance(value, list) or not minimum <= len(value) <= MAXIMUM_COHORT_SIZE:
        raise ValueError(f'Invalid linkcrypter decision field "{field_name}"')
    fingerprints = []
    for entry in value:
        fingerprint = _fingerprint(entry, field_name)
        if fingerprints and fingerprint <= fingerprints[-1]:
            raise ValueError(f'Unordered linkcrypter decision field "{field_name}"')
        fingerprints.append(fingerprint)
    return fingerprints


def _members(value, minimum):
    if not isinstance(value, list) or not minimum <= len(value) <= MAXIMUM_COHORT_SIZE:
        raise ValueError('Invalid linkcrypter decision field "members"')
    members = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != _MEMBER_KEYS:
            raise ValueError("Invalid linkcrypter cohort member")
        fingerprint = _fingerprint(entry["link_fingerprint"], "link_fingerprint")
        if members and fingerprint <= members[-1]["link_fingerprint"]:
            raise ValueError("Cohort members must be unique and ordered")
        members.append(
            {
                "link_fingerprint": fingerprint,
                "result": _choice(entry["result"], MEMBER_RESULTS, "result"),
                "tested_epoch": _epoch(entry["tested_epoch"], "tested_epoch"),
                "offer_id": _optional_identifier(entry["offer_id"], "offer_id"),
                "offer_expires_epoch": _epoch(
                    entry["offer_expires_epoch"], "offer_expires_epoch"
                ),
                "response_instruction": _choice(
                    entry["response_instruction"],
                    RESPONSE_INSTRUCTIONS,
                    "response_instruction",
                ),
            }
        )
    return members


def _cohort_cooldown_members(value):
    members = _members(value, MINIMUM_CONCLUSIVE_COHORT_SIZE)
    cooldown_responses = 0
    for entry in members:
        if entry["result"] != "blocked":
            raise ValueError("Cohort cooldown members must all be blocked")
        if (
            entry["tested_epoch"] == 0
            or entry["offer_id"] == ""
            or entry["offer_expires_epoch"] == 0
            or entry["tested_epoch"] > entry["offer_expires_epoch"]
            or entry["response_instruction"] not in ("hold", "cooldown")
        ):
            raise ValueError("Invalid blocked cohort member")
        cooldown_responses += entry["response_instruction"] == "cooldown"
    if cooldown_responses != 1:
        raise ValueError("Cohort cooldown must retain one cooldown response")
    return members


def _live_offer(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _OFFER_KEYS:
        raise ValueError("Invalid linkcrypter live offer")
    return {
        "offer_id": _identifier(value["offer_id"], "offer_id"),
        "offer_fingerprint": _fingerprint(
            value["offer_fingerprint"], "offer_fingerprint"
        ),
        "offer_expires_epoch": _epoch(
            value["offer_expires_epoch"], "offer_expires_epoch"
        ),
        "mode": _choice(value["mode"], OFFER_MODES, "mode"),
        "response_instruction": _choice(
            value["response_instruction"], RESPONSE_INSTRUCTIONS, "response_instruction"
        ),
    }


def _build_sweeping(raw):
    opened_epoch = _epoch(raw["opened_epoch"], "opened_epoch")
    deadline_epoch = _epoch(raw["deadline_epoch"], "deadline_epoch")
    if deadline_epoch != opened_epoch + SWEEP_WINDOW_SECONDS:
        raise ValueError('Invalid linkcrypter decision field "deadline_epoch"')
    return {
        "schema_version": SWEEP_SCHEMA_VERSION,
        "state": "sweeping",
        "reason_code": _choice(raw["reason_code"], SWEEP_REASON_CODES, "reason_code"),
        "sweep_id": _identifier(raw["sweep_id"], "sweep_id"),
        "opened_epoch": opened_epoch,
        "deadline_epoch": deadline_epoch,
        "members": _members(raw["members"], MINIMUM_SWEEP_COHORT_SIZE),
    }


def _build_cohort_cooldown(raw):
    members = _cohort_cooldown_members(raw["members"])
    cohort_size = raw["cohort_size"]
    if type(cohort_size) is not int or cohort_size != len(members):
        raise ValueError('Invalid linkcrypter decision field "cohort_size"')
    return {
        "schema_version": SWEEP_SCHEMA_VERSION,
        "state": "cooldown",
        "reason_code": _choice(raw["reason_code"], SWEEP_REASON_CODES, "reason_code"),
        "sweep_id": _identifier(raw["sweep_id"], "sweep_id"),
        "members": members,
        "cohort_size": cohort_size,
        "retry_after_epoch": _epoch(raw["retry_after_epoch"], "retry_after_epoch"),
        "live_offer": _live_offer(raw["live_offer"]),
    }


def _build_legacy_cooldown(raw):
    evidence_count = raw["legacy_evidence_count"]
    if raw["legacy_cooldown"] is not True or type(evidence_count) is not int:
        raise ValueError('Invalid linkcrypter decision field "legacy_cooldown"')
    if evidence_count < 1:
        raise ValueError('Invalid linkcrypter decision field "legacy_evidence_count"')
    return {
        "schema_version": SWEEP_SCHEMA_VERSION,
        "state": "cooldown",
        "reason_code": _choice(raw["reason_code"], SWEEP_REASON_CODES, "reason_code"),
        "legacy_cooldown": True,
        "retry_after_epoch": _epoch(raw["retry_after_epoch"], "retry_after_epoch"),
        "legacy_evidence_count": evidence_count,
    }


def _build_healthy(raw):
    return {
        "schema_version": SWEEP_SCHEMA_VERSION,
        "state": "healthy",
        "sweep_id": _identifier(raw["sweep_id"], "sweep_id"),
        "until_epoch": _epoch(raw["until_epoch"], "until_epoch"),
        "retest_members": _ascending_fingerprints(
            raw["retest_members"], "retest_members"
        ),
        "live_offer": _live_offer(raw["live_offer"]),
    }


def _build_individual(raw):
    return {
        "schema_version": SWEEP_SCHEMA_VERSION,
        "state": "individual",
        "reason": _choice(raw["reason"], INDIVIDUAL_REASONS, "reason"),
        "generation_id": _identifier(raw["generation_id"], "generation_id"),
        "until_epoch": _epoch(raw["until_epoch"], "until_epoch"),
        "live_offer": _live_offer(raw["live_offer"]),
    }


_RECORD_BUILDERS = {
    "sweeping": _build_sweeping,
    "cooldown": _build_cohort_cooldown,
    "legacy_cooldown": _build_legacy_cooldown,
    "healthy": _build_healthy,
    "individual": _build_individual,
}


def _validate_record(raw):
    if not isinstance(raw, dict):
        raise ValueError("Invalid linkcrypter decision record")
    version = raw.get("schema_version")
    if type(version) is not int or version != SWEEP_SCHEMA_VERSION:
        raise ValueError("Unsupported linkcrypter decision schema version")
    state = raw.get("state")
    if not isinstance(state, str):
        raise ValueError("Unsupported linkcrypter decision state")
    variant = (
        "legacy_cooldown" if state == "cooldown" and "legacy_cooldown" in raw else state
    )
    if variant not in _RECORD_KEYS:
        raise ValueError("Unsupported linkcrypter decision state")
    if set(raw) != _RECORD_KEYS[variant]:
        raise ValueError("Invalid linkcrypter decision record")
    return _RECORD_BUILDERS[variant](raw)


def _prune_expired(record, now):
    """Drop a decision whose own suppression or cooldown window already ended.

    A `sweeping` record is never pruned here: it still owns frozen member
    evidence, and only a transition may conclude it. Shrinking it away on a read
    would let a fresh sweep start inside the window it was supposed to close.
    """
    state = record["state"]
    if state == "cooldown" and record["retry_after_epoch"] <= now:
        return None
    if state in ("healthy", "individual") and record["until_epoch"] <= now:
        return None
    return record


def decode_decision_record(value, *, now):
    """Decode one persisted version-2 decision row, or None when it carries none.

    Total by contract: a malformed, foreign, oversized, deeply nested, or
    future-schema row can never raise and therefore can never brick a read. A
    legacy version-one row also decodes as None; `migrate_legacy_record` is the
    only reader that understands it.
    """
    record = _decode_valid_record(value)
    return None if record is None else _prune_expired(record, now)


def _decode_valid_record(value):
    if not isinstance(value, str):
        return None
    try:
        encoded_size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if encoded_size > MAXIMUM_COHORT_RECORD_BYTES:
        return None
    try:
        raw = json.loads(value)
    except (TypeError, ValueError, RecursionError):
        # An integer literal with more digits than Python converts raises a plain
        # ValueError and a value nested past the recursion limit a RecursionError;
        # neither is a JSONDecodeError.
        return None
    try:
        return _validate_record(raw)
    except (TypeError, ValueError):
        return None


def is_decision_record(value):
    """Whether a stored row is a structurally valid version-two decision.

    Deliberately expiry-blind: a valid row whose own window merely ended is a
    clean self-heal, not the malformed row a legacy reader would warn about.
    """
    return _decode_valid_record(value) is not None


def encode_decision_record(record):
    """Encode one validated decision record deterministically.

    Raises `ValueError` for an invalid record and `OverflowError` once the
    encoded row exceeds `MAXIMUM_COHORT_RECORD_BYTES`, so an oversized cohort
    rolls the whole transition back instead of committing dropped members.
    """
    try:
        encoded = json.dumps(
            _validate_record(record), separators=(",", ":"), sort_keys=True
        )
        encoded_size = len(encoded.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError("Linkcrypter decision record is not valid UTF-8") from error
    if encoded_size > MAXIMUM_COHORT_RECORD_BYTES:
        raise OverflowError("Linkcrypter decision record is too large to store")
    return encoded


def migrate_legacy_record(value, *, now):
    """Project a legacy version-one row onto its version-two equivalent.

    An old `observing` row is not a cohort and never becomes one: it migrates to
    nothing. An old `cooldown` row keeps its exact retry deadline and surviving
    evidence count as a marked legacy cooldown, and gains no synthetic sweep,
    member, or offer field it never had.
    """
    from quasarr.providers.crypter_cooldowns import (
        OBSERVATION_WINDOW_SECONDS,
        _decode_record,
    )

    try:
        legacy = _decode_record(value)
    except (TypeError, ValueError, RecursionError):
        return None
    if legacy is None or legacy["state"] != "cooldown":
        return None
    retry_after_epoch = legacy["retry_after_epoch"]
    if retry_after_epoch <= now:
        return None

    cutoff = now - OBSERVATION_WINDOW_SECONDS
    surviving = sum(
        1
        for observation in legacy["observations"]
        if observation["seen_at_epoch"] >= cutoff
    )
    return {
        "schema_version": SWEEP_SCHEMA_VERSION,
        "state": "cooldown",
        "reason_code": legacy["reason_code"],
        "legacy_cooldown": True,
        "retry_after_epoch": retry_after_epoch,
        "legacy_evidence_count": max(1, surviving),
    }


def _text(value):
    return value if isinstance(value, str) else ""


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def decision_snapshot(record, *, now):
    """Project a decoded decision onto one fixed read-only shape.

    Total for any input, so a caller can project a record it did not decode
    itself without a shape check of its own.
    """
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
        "live_offer": None,
    }
    if not isinstance(record, dict):
        return snapshot
    state = record.get("state")
    if state not in ("sweeping", "cooldown", "healthy", "individual"):
        return snapshot

    members = record.get("members")
    members = (
        [entry for entry in members if isinstance(entry, dict)]
        if isinstance(members, list)
        else []
    )
    results = [entry.get("result") for entry in members]
    offer = record.get("live_offer")
    snapshot.update(
        {
            "state": state,
            "sweep_total": len(members),
            "sweep_tested": sum(
                1 for result in results if result in ("blocked", "unknown")
            ),
            "live_offer": dict(offer) if isinstance(offer, dict) else None,
        }
    )

    if state == "sweeping":
        deadline_epoch = _count(record.get("deadline_epoch"))
        snapshot.update(
            {
                "reason_code": _text(record.get("reason_code")),
                "sweep_id": _text(record.get("sweep_id")),
                "opened_epoch": _count(record.get("opened_epoch")),
                "sweep_deadline_epoch": deadline_epoch,
                "evidence_count": sum(1 for result in results if result == "blocked"),
                "expired": now > deadline_epoch,
            }
        )
    elif state == "cooldown":
        legacy_cooldown = record.get("legacy_cooldown") is True
        retry_after_epoch = _count(record.get("retry_after_epoch"))
        snapshot.update(
            {
                "reason_code": _text(record.get("reason_code")),
                "legacy_cooldown": legacy_cooldown,
                "sweep_id": _text(record.get("sweep_id")),
                "retry_after_epoch": retry_after_epoch,
                "evidence_count": _count(
                    record.get("legacy_evidence_count")
                    if legacy_cooldown
                    else record.get("cohort_size")
                ),
                "expired": now >= retry_after_epoch,
            }
        )
    else:
        until_epoch = _count(record.get("until_epoch"))
        retest_members = record.get("retest_members")
        snapshot.update(
            {
                "reason_code": _text(record.get("reason")),
                "sweep_id": _text(record.get("sweep_id")),
                "generation_id": _text(record.get("generation_id")),
                "until_epoch": until_epoch,
                "retest_members": tuple(retest_members)
                if isinstance(retest_members, list)
                else (),
                "expired": now >= until_epoch,
            }
        )
    return snapshot


def helper_package_is_candidate(package_data):
    """Whether a decoded protected package can ever be handed to the helper.

    The single eligibility predicate shared by the Filecrypt candidate inventory
    and the helper handout, so a frozen cohort can never contain a member the
    selector would refuse to offer.
    """
    if not isinstance(package_data, dict):
        return False
    if not _PACKAGE_CANDIDATE_KEYS.issubset(package_data):
        return False
    if "disabled" in package_data:
        return False
    links = package_data.get("links")
    return isinstance(links, list) and bool(links)


@dataclasses.dataclass(frozen=True)
class Offer:
    """One leased handout. A report only has to name the identity it carries."""

    mode: str
    sweep_id: str
    offer_id: str
    fingerprint: str
    deadline_epoch: int


_TESTED_RESULTS = ("blocked", "unknown")
_UNTESTED_RESULTS = ("pending", "offered")
_TRANSITION_EVENTS = ("observations", "cooldowns", "probes")


def _decision(**overrides):
    """The one fixed-cardinality shape every transition answers with."""
    events = overrides.pop("events", None)
    decision = {
        "instruction": "",
        "state": "available",
        "hold_type": "none",
        "accepted": "",
        "cleared": False,
        "evidence_count": 0,
        "retry_after_epoch": 0,
        "sweep_id": "",
        "sweep_tested": 0,
        "sweep_total": 0,
        "sweep_deadline_epoch": 0,
        "events": dict.fromkeys(_TRANSITION_EVENTS, 0),
    }
    decision.update(overrides)
    if events:
        decision["events"] = {**decision["events"], **events}
    return decision


def _generation_id(record):
    """The generation a report must name, or "" when the record has none."""
    if not isinstance(record, dict):
        return ""
    if record["state"] == "individual":
        return record["generation_id"]
    return record.get("sweep_id", "")


def _window_deadline(record):
    """The instant the current decision stops being authoritative."""
    state = record["state"]
    if state == "sweeping":
        return record["deadline_epoch"]
    if state == "cooldown":
        return record["retry_after_epoch"]
    return record["until_epoch"]


def _inventory_fingerprints(inventory):
    """Ascending unique fingerprints, or None when the inventory proves nothing."""
    if inventory is None or inventory.oversized:
        return None
    return sorted({candidate.fingerprint for candidate in inventory.candidates})


def _new_member(link_fingerprint):
    return {
        "link_fingerprint": link_fingerprint,
        "result": "pending",
        "tested_epoch": 0,
        "offer_id": "",
        "offer_expires_epoch": 0,
        "response_instruction": "",
    }


def _individual(reason, generation_id, until_epoch, live_offer=None):
    return {
        "schema_version": SWEEP_SCHEMA_VERSION,
        "state": "individual",
        "reason": reason,
        "generation_id": generation_id,
        "until_epoch": until_epoch,
        "live_offer": live_offer,
    }


def _member_index(record, link_fingerprint):
    for position, entry in enumerate(record.get("members", ())):
        if entry["link_fingerprint"] == link_fingerprint:
            return position
    return None


def _located_offer(record, offer):
    """The record-level slot or frozen member the report names, or None.

    The record-level slot is matched first: a manual probe leases its own offer
    for a fingerprint that is also a frozen member, and the report belongs to
    the lease it was actually handed, not to that member's older evidence.
    """
    live = record.get("live_offer")
    if (
        isinstance(live, dict)
        and live["offer_id"] == offer.offer_id
        and live["offer_fingerprint"] == offer.fingerprint
    ):
        return "slot", None, live
    index = _member_index(record, offer.fingerprint)
    if index is not None and record["members"][index]["offer_id"] == offer.offer_id:
        return "member", index, record["members"][index]
    return None


def _situation(
    record,
    now,
    *,
    instruction="",
    accepted="",
    cleared=False,
    events=None,
    hold=False,
    total=None,
    tested=None,
):
    """Project the record a report ended on into the fixed decision shape."""
    snapshot = decision_snapshot(record, now=now)
    state = snapshot["state"]
    if state == "sweeping":
        deadline = snapshot["sweep_deadline_epoch"]
    elif state == "cooldown":
        deadline = snapshot["retry_after_epoch"]
    elif state == "available":
        deadline = 0
    else:
        deadline = snapshot["until_epoch"]

    # Only a sweep or a cohort cooldown ever asks for a hold, so the resulting
    # hold type follows from the state the report ended on.
    if not hold:
        hold_type, retry_after_epoch = "none", 0
    elif state == "cooldown":
        hold_type, retry_after_epoch = "crypter_cooldown", deadline
    else:
        hold_type, retry_after_epoch = "provisional", deadline

    return _decision(
        instruction=instruction,
        state=state,
        hold_type=hold_type,
        accepted=accepted,
        cleared=cleared,
        evidence_count=snapshot["evidence_count"],
        retry_after_epoch=retry_after_epoch,
        sweep_id=snapshot["sweep_id"] or snapshot["generation_id"],
        sweep_tested=snapshot["sweep_tested"] if tested is None else tested,
        sweep_total=snapshot["sweep_total"] if total is None else total,
        sweep_deadline_epoch=deadline,
        events=events,
    )


def _stale(record, now):
    return _situation(record, now, instruction="stale")


def bypass_decision():
    """The pure `fail`-mode answer: no hold, no evidence, no state change."""
    return _decision(instruction="legacy_failure")


def expire_decision(record, *, now):
    """Conclude or drop a decision whose own window ended.

    A sweep concludes into a generation-bound suppression window measured from
    its own deadline, never from the reading clock, so repeating this call can
    never extend it and a long-forgotten sweep self-heals to no decision at all.
    """
    if not isinstance(record, dict):
        return None
    if record["state"] == "sweeping":
        if now <= record["deadline_epoch"]:
            return record
        record = _individual(
            "sweep_expired",
            record["sweep_id"],
            record["deadline_epoch"] + SWEEP_WINDOW_SECONDS,
        )
    return None if _window_deadline(record) <= now else record


def prepare_decision(inventory, current_record, *, now, sweep_id_factory):
    """The decision that must be current before any offer is leased.

    An existing live decision always wins, so only an available linkcrypter may
    freeze a cohort and no read can restart a window that is already suppressing
    work. An inventory that proves nothing changes nothing.
    """
    record = expire_decision(current_record, now=now)
    if record is not None or inventory is None:
        return record
    fingerprints = _inventory_fingerprints(inventory)
    if fingerprints is None or len(fingerprints) > MAXIMUM_COHORT_SIZE:
        return _individual(
            "cohort_oversized", sweep_id_factory(), now + SWEEP_WINDOW_SECONDS
        )
    if not fingerprints:
        return None
    if len(fingerprints) < MINIMUM_SWEEP_COHORT_SIZE:
        return _individual(
            "cohort_too_small", sweep_id_factory(), now + SWEEP_WINDOW_SECONDS
        )
    return {
        "schema_version": SWEEP_SCHEMA_VERSION,
        "state": "sweeping",
        "reason_code": DEFAULT_SWEEP_REASON_CODE,
        "sweep_id": sweep_id_factory(),
        "opened_epoch": now,
        "deadline_epoch": now + SWEEP_WINDOW_SECONDS,
        "members": [_new_member(entry) for entry in fingerprints],
    }


def _generation_offer_ids(record):
    identifiers = {
        entry["offer_id"] for entry in record.get("members", ()) if entry["offer_id"]
    }
    live = record.get("live_offer")
    if isinstance(live, dict):
        identifiers.add(live["offer_id"])
    return identifiers


def _mint_offer_id(record, offer_id_factory):
    """Offer IDs identify evidence, so a collision fails the whole transition."""
    offer_id = _identifier(offer_id_factory(), "offer_id")
    if offer_id in _generation_offer_ids(record):
        raise ValueError("Linkcrypter offer ID must be unique inside its generation")
    return offer_id


def _slot_offer(offer_id, link_fingerprint, offer_expires_epoch, mode, instruction=""):
    return {
        "offer_id": offer_id,
        "offer_fingerprint": link_fingerprint,
        "offer_expires_epoch": offer_expires_epoch,
        "mode": mode,
        "response_instruction": instruction,
    }


def _slot_is_leasable(record, now):
    live = record["live_offer"]
    return live is None or live["offer_expires_epoch"] <= now


def lease_next_offer(record, inventory, *, now, offer_id_factory, mode=None):
    """Lease at most one offer, or answer that there is no work to hand out.

    A member or record-level slot with a live lease is never handed out twice,
    an expired unaccepted lease is replaced under a fresh ID, and nothing is
    ever leased at or after the current window's deadline.
    """
    if not isinstance(record, dict):
        return record, None
    state = record["state"]
    if now >= _window_deadline(record):
        return record, None

    if state == "sweeping":
        return _lease_sweep_offer(record, now, offer_id_factory, mode)
    if state == "cooldown":
        # A migrated legacy cooldown owns no cohort and never issues one.
        if mode != "probe" or "members" not in record:
            return record, None
        link_fingerprint = record["members"][0]["link_fingerprint"]
    elif state == "healthy":
        if mode not in (None, "retest") or not record["retest_members"]:
            return record, None
        link_fingerprint = record["retest_members"][0]
        mode = "retest"
    else:
        if mode not in (None, "individual"):
            return record, None
        fingerprints = _inventory_fingerprints(inventory)
        if not fingerprints:
            return record, None
        link_fingerprint = fingerprints[0]
        mode = "individual"

    if not _slot_is_leasable(record, now):
        return record, None
    offer_id = _mint_offer_id(record, offer_id_factory)
    leased = _slot_offer(offer_id, link_fingerprint, now + OFFER_LEASE_SECONDS, mode)
    return {**record, "live_offer": leased}, Offer(
        mode,
        _generation_id(record),
        offer_id,
        link_fingerprint,
        _window_deadline(record),
    )


def _lease_sweep_offer(record, now, offer_id_factory, mode):
    if mode not in (None, "sweep"):
        return record, None
    index = next(
        (
            position
            for position, entry in enumerate(record["members"])
            if entry["result"] == "pending"
            or (entry["result"] == "offered" and entry["offer_expires_epoch"] <= now)
        ),
        None,
    )
    if index is None:
        return record, None
    offer_id = _mint_offer_id(record, offer_id_factory)
    members = list(record["members"])
    members[index] = {
        **members[index],
        "result": "offered",
        "offer_id": offer_id,
        "offer_expires_epoch": now + OFFER_LEASE_SECONDS,
    }
    return {**record, "members": members}, Offer(
        "sweep",
        record["sweep_id"],
        offer_id,
        members[index]["link_fingerprint"],
        record["deadline_epoch"],
    )


def record_blocked(record, offer, inventory, *, now, cooldown_seconds):
    """Apply one exact BLOCKED report and answer what it changed.

    `cooldown_seconds` is the configured cooldown length, passed in so the
    transition stays pure and never reads settings. Validation order is
    generation lookup, offer identity, accepted-result replay, then the
    situation transition.
    """
    record = expire_decision(record, now=now)
    generation = _generation_id(record)
    if not generation or generation != offer.sweep_id:
        return record, _stale(record, now)
    if record["state"] == "sweeping":
        return _blocked_in_sweep(record, offer, inventory, now, cooldown_seconds)
    if record["state"] == "cooldown":
        return _blocked_in_cooldown(record, offer, now)
    return _blocked_outside_cohort(record, offer, now)


def _blocked_in_sweep(record, offer, inventory, now, cooldown_seconds):
    located = _located_offer(record, offer)
    if located is None:
        return record, _stale(record, now)
    _kind, index, stored = located
    if stored["result"] in _TESTED_RESULTS:
        instruction = stored["response_instruction"]
        return record, _situation(
            record,
            now,
            instruction=instruction,
            hold=instruction in ("hold", "cooldown"),
        )
    members = list(record["members"])
    members[index] = {
        **stored,
        "result": "blocked",
        "tested_epoch": now,
        # An accepted report seals its own lease so the terminal evidence stays
        # coherent even when the two-minute lease had already run out.
        "offer_expires_epoch": max(stored["offer_expires_epoch"], now),
        "response_instruction": "hold",
    }
    return _conclude_sweep(record, members, index, inventory, now, cooldown_seconds)


def _conclude_sweep(record, members, index, inventory, now, cooldown_seconds):
    total = len(members)
    tested = sum(1 for entry in members if entry["result"] in _TESTED_RESULTS)
    all_blocked = all(entry["result"] == "blocked" for entry in members)
    present = _inventory_fingerprints(inventory)
    lost = present is not None and any(
        entry["result"] in _UNTESTED_RESULTS
        and entry["link_fingerprint"] not in present
        for entry in members
    )
    counted = {"observations": 1}

    if (
        all_blocked
        and total >= MINIMUM_CONCLUSIVE_COHORT_SIZE
        and inventory is not None
    ):
        members[index] = {**members[index], "response_instruction": "cooldown"}
        cooled = {
            "schema_version": SWEEP_SCHEMA_VERSION,
            "state": "cooldown",
            "reason_code": record["reason_code"],
            "sweep_id": record["sweep_id"],
            "members": members,
            "cohort_size": total,
            "retry_after_epoch": now + cooldown_seconds,
            "live_offer": None,
        }
        return cooled, _situation(
            cooled,
            now,
            instruction="cooldown",
            hold=True,
            events={**counted, "cooldowns": 1},
        )

    if tested == total or lost:
        # An untested member that left the inventory can never be tested, so the
        # cohort ends inconclusive rather than shrinking to a easier denominator.
        reason = (
            "cohort_too_small"
            if all_blocked and total < MINIMUM_CONCLUSIVE_COHORT_SIZE
            else "sweep_inconclusive"
        )
        concluded = _individual(reason, record["sweep_id"], now + SWEEP_WINDOW_SECONDS)
        return concluded, _situation(
            concluded,
            now,
            instruction="legacy_failure",
            events=counted,
            total=total,
            tested=tested,
        )

    updated = {**record, "members": members}
    return updated, _situation(
        updated, now, instruction="hold", hold=True, events=counted
    )


def _blocked_in_cooldown(record, offer, now):
    located = _located_offer(record, offer)
    if located is None:
        return record, _stale(record, now)
    kind, _index, stored = located
    if kind == "member" or stored["response_instruction"]:
        return record, _situation(
            record, now, instruction=stored["response_instruction"], hold=True
        )
    updated = {
        **record,
        "live_offer": {**stored, "response_instruction": "cooldown"},
    }
    return updated, _situation(updated, now, instruction="cooldown", hold=True)


def _blocked_outside_cohort(record, offer, now):
    """Healthy and individual windows answer ordinary failure, never evidence."""
    located = _located_offer(record, offer)
    if located is None:
        return record, _situation(record, now, instruction="legacy_failure")
    _kind, _index, stored = located
    if stored["response_instruction"]:
        return record, _situation(
            record, now, instruction=stored["response_instruction"]
        )
    updated = _consume_slot(record, offer, "legacy_failure")
    return updated, _situation(updated, now, instruction="legacy_failure")


def _consume_slot(record, offer, instruction):
    """Mark the record-level offer answered and drop a finished retest."""
    updated = {
        **record,
        "live_offer": {**record["live_offer"], "response_instruction": instruction},
    }
    if updated["state"] == "healthy":
        updated["retest_members"] = [
            entry for entry in updated["retest_members"] if entry != offer.fingerprint
        ]
    return updated


def record_access(record, offer, access, inventory, *, now):
    """Apply one exact CLEAR or UNKNOWN report.

    `inventory` is accepted for symmetry with `record_blocked` and deliberately
    unused: an access report never freezes or re-checks a denominator.
    """
    if access not in ("clear", "unknown"):
        raise ValueError(f'Unsupported linkcrypter access value "{access}"')
    del inventory
    record = expire_decision(record, now=now)
    generation = _generation_id(record)
    if not generation or generation != offer.sweep_id:
        return record, _stale(record, now)
    if access == "clear":
        return _record_clear(record, offer, now)
    return _record_unknown(record, offer, now)


def _record_clear(record, offer, now):
    located = _located_offer(record, offer)
    if located is None:
        return record, _stale(record, now)
    if record["state"] == "healthy":
        # Healthy retains the acknowledged offer so a lost response replays; the
        # retry may never extend the suppression window it already granted.
        updated = {
            **record,
            "retest_members": [
                entry
                for entry in record["retest_members"]
                if entry != offer.fingerprint
            ],
        }
        return updated, _situation(updated, now, cleared=True)

    kind, _index, stored = located
    mode = (
        stored["mode"]
        if kind == "slot"
        else ("sweep" if record["state"] == "sweeping" else "probe")
    )
    healthy = {
        "schema_version": SWEEP_SCHEMA_VERSION,
        "state": "healthy",
        "sweep_id": _generation_id(record),
        "until_epoch": now + HEALTHY_SUPPRESSION_SECONDS,
        "retest_members": sorted(
            entry["link_fingerprint"]
            for entry in record.get("members", ())
            if entry["result"] == "blocked"
            and entry["link_fingerprint"] != offer.fingerprint
        ),
        "live_offer": _slot_offer(
            offer.offer_id,
            offer.fingerprint,
            max(stored["offer_expires_epoch"], now),
            mode,
        ),
    }
    return healthy, _situation(healthy, now, cleared=True)


def _record_unknown(record, offer, now):
    located = _located_offer(record, offer)
    if located is None:
        return record, _stale(record, now)
    kind, index, stored = located
    if kind == "slot":
        if stored["response_instruction"]:
            return record, _situation(record, now, accepted="unknown")
        updated = _consume_slot(record, offer, "legacy_failure")
        return updated, _situation(updated, now, accepted="unknown")

    if stored["result"] == "unknown":
        return record, _situation(record, now, accepted="unknown")
    if stored["result"] == "blocked":
        return record, _situation(
            record,
            now,
            instruction=stored["response_instruction"],
            hold=stored["response_instruction"] in ("hold", "cooldown"),
        )

    members = list(record["members"])
    members[index] = {
        **stored,
        "result": "unknown",
        "tested_epoch": now,
        "offer_expires_epoch": max(stored["offer_expires_epoch"], now),
        "response_instruction": "legacy_failure",
    }
    total = len(members)
    tested = sum(1 for entry in members if entry["result"] in _TESTED_RESULTS)
    if tested < total:
        # An UNKNOWN never ends a sweep early; it only makes it inconclusive.
        updated = {**record, "members": members}
        return updated, _situation(updated, now, accepted="unknown")
    concluded = _individual(
        "sweep_inconclusive", record["sweep_id"], now + SWEEP_WINDOW_SECONDS
    )
    return concluded, _situation(
        concluded, now, accepted="unknown", total=total, tested=tested
    )


def _legacy_decision(state, evidence_count, retry_after_epoch, *, recorded=False):
    return {
        "state": state,
        "evidence_count": evidence_count,
        "package_retry_after_epoch": retry_after_epoch,
        "recorded": recorded,
        "cooldown_started": False,
    }


def record_legacy_report(record, *, now, generation_id_factory):
    """Apply one version-one block report to a version-two decision.

    A version-one report carries no cohort evidence, so it may only open or join
    the generation-bound legacy hold of an otherwise available linkcrypter. It
    never changes a sweep, a cooldown, a healthy verdict, or another individual
    reason, and it can never start a global cooldown.
    """
    record = expire_decision(record, now=now)
    if record is None:
        opened = _individual(
            "legacy_v1_hold",
            _identifier(generation_id_factory(), "generation_id"),
            now + SWEEP_WINDOW_SECONDS,
        )
        return opened, _legacy_decision(
            "observing", 1, opened["until_epoch"], recorded=True
        )
    state = record["state"]
    if state == "cooldown":
        evidence = record.get("cohort_size") or record.get("legacy_evidence_count", 0)
        return record, _legacy_decision(
            "cooldown", evidence, record["retry_after_epoch"]
        )
    if state == "sweeping":
        return record, _legacy_decision("observing", 1, 0)
    if state == "individual" and record["reason"] == "legacy_v1_hold":
        return record, _legacy_decision("observing", 1, record["until_epoch"])
    return record, _legacy_decision("available", 0, 0)
