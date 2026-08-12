# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

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
        record = _validate_record(raw)
    except (TypeError, ValueError):
        return None
    return _prune_expired(record, now)


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
