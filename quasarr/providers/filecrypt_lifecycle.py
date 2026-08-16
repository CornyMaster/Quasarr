# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

"""Pure Filecrypt link-lifecycle record codecs and constants.

This module has no Quasarr imports so it can be tested and reused independently.
It provides strict versioned JSON codecs for five lifecycle record types and two
deadline predicates. Decoders are total: any malformed, oversized, wrong-version,
extra/missing-key, or incoherent input returns None.  Encoders raise TypeError or
ValueError for invalid caller records.

The per-row byte bound (_MAX_LIFECYCLE_RECORD_BYTES) is a safety limit on individual
stored values; it never restricts the number of rows, members, candidates, or owners.

The ``response`` field in offer receipts is caller-owned and treated as an opaque dict
by this codec.  It MUST contain only the normalized identifier/count/state decision
contract; never URLs, titles, raw exception text, or secrets.
"""

import json
import re

# ── table names ───────────────────────────────────────────────────────────────

FILECRYPT_LINK_STATES_TABLE = "filecrypt_link_states"
FILECRYPT_SWEEP_STATE_TABLE = "filecrypt_sweep_state"
FILECRYPT_SWEEP_MEMBERS_TABLE = "filecrypt_sweep_members"
FILECRYPT_OFFER_RECEIPTS_TABLE = "filecrypt_offer_receipts"
FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE = "filecrypt_lifecycle_migrations"

# Singleton key for the sweep-state table
FILECRYPT_SWEEP_KEY = "filecrypt"
# Migration marker key
FILECRYPT_MIGRATION_KEY = "v1"

# ── lifecycle minimums ────────────────────────────────────────────────────────

MINIMUM_SWEEP_SIZE = 2
MINIMUM_GLOBAL_COOLDOWN_SIZE = 5

# ── internal constants ────────────────────────────────────────────────────────

_SCHEMA_VERSION = 1
# Per-row encoded-byte safety bound.  Does NOT limit the number of rows.
_MAX_LIFECYCLE_RECORD_BYTES = 16 * 1024

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
# Mirrors quasarr.constants.PACKAGE_ID_PATTERN without importing it.
_PACKAGE_ID_RE = re.compile(r"^Quasarr_[a-z0-9]+_[a-f0-9]{32}$")

_LINK_STATES = frozenset({"held", "blacklisting", "blacklisted"})
_SWEEP_STATES = frozenset({"sweeping", "healthy", "cooldown"})
_MEMBER_STATES = frozenset({"pending", "offered", "blocked", "clear", "unknown"})
_MEMBER_TERMINAL_STATES = frozenset({"blocked", "clear", "unknown"})
_OFFER_MODES = frozenset({"sweep", "individual", "retest", "probe"})
_OUTCOME_VALUES = frozenset({"blocked", "clear", "unknown"})

# Exact key sets per record shape
_HELD_KEYS = frozenset(
    {"schema_version", "state", "first_blocked_epoch", "retry_after_epoch", "lease"}
)
_BLACKLISTING_KEYS = frozenset(
    {
        "schema_version",
        "state",
        "first_blocked_epoch",
        "recheck_offer_id",
        "recheck_package_id",
        "recheck_sweep_id",
        "terminal_operation_id",
    }
)
_BLACKLISTED_KEYS = frozenset(
    {"schema_version", "state", "first_blocked_epoch", "blacklisted_epoch"}
)
_HELD_LEASE_KEYS = frozenset(
    {"sweep_id", "offer_id", "package_id", "offer_expires_epoch"}
)
_SWEEPING_KEYS = frozenset(
    {
        "schema_version",
        "state",
        "generation_id",
        "opened_epoch",
        "deadline_epoch",
        "window_seconds",
        "total",
        "tested",
        "blocked",
        "global_possible",
    }
)
_HEALTHY_KEYS = frozenset({"schema_version", "state", "generation_id", "until_epoch"})
_SWEEP_COOLDOWN_KEYS = frozenset(
    {
        "schema_version",
        "state",
        "generation_id",
        "sweep_deadline_epoch",
        "retry_after_epoch",
    }
)
_MEMBER_KEYS = frozenset(
    {"schema_version", "generation_id", "fingerprint", "state", "lease", "outcome"}
)
_MEMBER_LEASE_KEYS = frozenset({"offer_id", "package_id", "offer_expires_epoch"})
_MEMBER_OUTCOME_KEYS = frozenset({"offer_id", "package_id", "accepted_epoch"})
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "generation_id",
        "fingerprint",
        "package_id",
        "mode",
        "outcome",
        "response",
        "accepted_epoch",
        "expires_epoch",
    }
)
_MIGRATION_KEYS = frozenset({"schema_version", "completed_epoch"})


# ── low-level helpers ─────────────────────────────────────────────────────────


def _oversized(value):
    if isinstance(value, bytes):
        return len(value) > _MAX_LIFECYCLE_RECORD_BYTES
    if isinstance(value, str):
        try:
            return len(value.encode("utf-8")) > _MAX_LIFECYCLE_RECORD_BYTES
        except UnicodeEncodeError:
            return False
    return False


def _parse(value):
    try:
        obj = json.loads(value)
    except (TypeError, ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def _version_ok(record):
    v = record.get("schema_version")
    return type(v) is int and v == _SCHEMA_VERSION


def _keys_exact(record, keys):
    return set(record.keys()) == keys


def _epoch(v):
    return type(v) is int and v >= 0


def _id_ok(v):
    return isinstance(v, str) and bool(_ID_RE.fullmatch(v))


def _fp_ok(v):
    return isinstance(v, str) and bool(_FINGERPRINT_RE.fullmatch(v))


def _pkg_ok(v):
    return isinstance(v, str) and bool(_PACKAGE_ID_RE.fullmatch(v))


def _encode(record):
    return json.dumps(record, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


# ── sub-record validators (decode path) ───────────────────────────────────────


def _decode_held_lease(lease):
    if lease is None:
        return True
    if not isinstance(lease, dict) or not _keys_exact(lease, _HELD_LEASE_KEYS):
        return False
    return (
        _id_ok(lease.get("sweep_id"))
        and _id_ok(lease.get("offer_id"))
        and _pkg_ok(lease.get("package_id"))
        and _epoch(lease.get("offer_expires_epoch"))
    )


def _decode_member_lease(lease):
    if lease is None:
        return True
    if not isinstance(lease, dict) or not _keys_exact(lease, _MEMBER_LEASE_KEYS):
        return False
    return (
        _id_ok(lease.get("offer_id"))
        and _pkg_ok(lease.get("package_id"))
        and _epoch(lease.get("offer_expires_epoch"))
    )


def _decode_member_outcome(outcome):
    if outcome is None:
        return True
    if not isinstance(outcome, dict) or not _keys_exact(outcome, _MEMBER_OUTCOME_KEYS):
        return False
    return (
        _id_ok(outcome.get("offer_id"))
        and _pkg_ok(outcome.get("package_id"))
        and _epoch(outcome.get("accepted_epoch"))
    )


# ── sub-record validators (encode path) ───────────────────────────────────────


def _encode_held_lease(lease):
    if lease is None:
        return
    if not isinstance(lease, dict):
        raise ValueError("lease must be null or a dict")
    if set(lease.keys()) != _HELD_LEASE_KEYS:
        raise ValueError("held lease key set mismatch")
    if not _id_ok(lease.get("sweep_id")):
        raise ValueError("lease sweep_id must be 32 lowercase hex")
    if not _id_ok(lease.get("offer_id")):
        raise ValueError("lease offer_id must be 32 lowercase hex")
    if not _pkg_ok(lease.get("package_id")):
        raise ValueError("lease package_id must be a canonical package ID")
    if not _epoch(lease.get("offer_expires_epoch")):
        raise ValueError("lease offer_expires_epoch must be a non-negative int")


def _encode_member_lease(lease):
    if lease is None:
        return
    if not isinstance(lease, dict):
        raise ValueError("lease must be null or a dict")
    if set(lease.keys()) != _MEMBER_LEASE_KEYS:
        raise ValueError("member lease key set mismatch")
    if not _id_ok(lease.get("offer_id")):
        raise ValueError("offer_id must be 32 lowercase hex")
    if not _pkg_ok(lease.get("package_id")):
        raise ValueError("package_id must be a canonical package ID")
    if not _epoch(lease.get("offer_expires_epoch")):
        raise ValueError("offer_expires_epoch must be a non-negative int")


def _encode_member_outcome(outcome):
    if outcome is None:
        return
    if not isinstance(outcome, dict):
        raise ValueError("outcome must be null or a dict")
    if set(outcome.keys()) != _MEMBER_OUTCOME_KEYS:
        raise ValueError("member outcome key set mismatch")
    if not _id_ok(outcome.get("offer_id")):
        raise ValueError("outcome offer_id must be 32 lowercase hex")
    if not _pkg_ok(outcome.get("package_id")):
        raise ValueError("outcome package_id must be a canonical package ID")
    if not _epoch(outcome.get("accepted_epoch")):
        raise ValueError("outcome accepted_epoch must be a non-negative int")


# ── link-state codec ──────────────────────────────────────────────────────────


def decode_link_state(value):
    """Decode a stored link-state value.  Clock-free.

    Returns None for malformed, extra/missing-key, oversized, recursive,
    wrong-version, or incoherent input.
    """
    if _oversized(value):
        return None
    record = _parse(value)
    if record is None or not _version_ok(record):
        return None
    state = record.get("state")
    if state == "held":
        if not _keys_exact(record, _HELD_KEYS):
            return None
        if not _epoch(record.get("first_blocked_epoch")):
            return None
        if not _epoch(record.get("retry_after_epoch")):
            return None
        if not _decode_held_lease(record.get("lease")):
            return None
        return record
    if state == "blacklisting":
        if not _keys_exact(record, _BLACKLISTING_KEYS):
            return None
        if not _epoch(record.get("first_blocked_epoch")):
            return None
        if not _id_ok(record.get("recheck_offer_id")):
            return None
        if not _pkg_ok(record.get("recheck_package_id")):
            return None
        if not _id_ok(record.get("recheck_sweep_id")):
            return None
        if not _fp_ok(record.get("terminal_operation_id")):
            return None
        return record
    if state == "blacklisted":
        if not _keys_exact(record, _BLACKLISTED_KEYS):
            return None
        if not _epoch(record.get("first_blocked_epoch")):
            return None
        if not _epoch(record.get("blacklisted_epoch")):
            return None
        return record
    return None


def encode_link_state(record):
    """Encode a link-state record to canonical sorted JSON.

    Raises TypeError for a non-dict caller record; raises ValueError for
    schema violations.
    """
    if not isinstance(record, dict):
        raise TypeError("record must be a dict")
    sv = record.get("schema_version")
    if not (type(sv) is int and sv == _SCHEMA_VERSION):
        raise ValueError("schema_version must be 1")
    state = record.get("state")
    if state not in _LINK_STATES:
        raise ValueError(f"state must be one of {sorted(_LINK_STATES)!r}")
    if state == "held":
        if set(record.keys()) != _HELD_KEYS:
            raise ValueError("held record key set mismatch")
        if not _epoch(record.get("first_blocked_epoch")):
            raise ValueError("first_blocked_epoch must be a non-negative int")
        if not _epoch(record.get("retry_after_epoch")):
            raise ValueError("retry_after_epoch must be a non-negative int")
        _encode_held_lease(record.get("lease"))
    elif state == "blacklisting":
        if set(record.keys()) != _BLACKLISTING_KEYS:
            raise ValueError("blacklisting record key set mismatch")
        if not _epoch(record.get("first_blocked_epoch")):
            raise ValueError("first_blocked_epoch must be a non-negative int")
        if not _id_ok(record.get("recheck_offer_id")):
            raise ValueError("recheck_offer_id must be 32 lowercase hex")
        if not _pkg_ok(record.get("recheck_package_id")):
            raise ValueError("recheck_package_id must be a canonical package ID")
        if not _id_ok(record.get("recheck_sweep_id")):
            raise ValueError("recheck_sweep_id must be 32 lowercase hex")
        if not _fp_ok(record.get("terminal_operation_id")):
            raise ValueError("terminal_operation_id must be 64 lowercase hex")
    else:  # blacklisted
        if set(record.keys()) != _BLACKLISTED_KEYS:
            raise ValueError("blacklisted record key set mismatch")
        if not _epoch(record.get("first_blocked_epoch")):
            raise ValueError("first_blocked_epoch must be a non-negative int")
        if not _epoch(record.get("blacklisted_epoch")):
            raise ValueError("blacklisted_epoch must be a non-negative int")
    return _encode(record)


# ── sweep-header codec ────────────────────────────────────────────────────────


def decode_sweep_header(value, *, now=None):
    """Decode a stored sweep-header value.

    The ``now`` parameter is accepted for API compatibility but does not prune
    expired headers; that is a service-layer concern.  Returns None for any
    structurally invalid input.
    """
    if _oversized(value):
        return None
    record = _parse(value)
    if record is None or not _version_ok(record):
        return None
    state = record.get("state")
    if state == "sweeping":
        if not _keys_exact(record, _SWEEPING_KEYS):
            return None
        if not _id_ok(record.get("generation_id")):
            return None
        for field in (
            "opened_epoch",
            "deadline_epoch",
            "window_seconds",
            "total",
            "tested",
            "blocked",
        ):
            if not _epoch(record.get(field)):
                return None
        if not isinstance(record.get("global_possible"), bool):
            return None
        if record["tested"] > record["total"] or record["blocked"] > record["tested"]:
            return None
        return record
    if state == "healthy":
        if not _keys_exact(record, _HEALTHY_KEYS):
            return None
        if not _id_ok(record.get("generation_id")):
            return None
        if not _epoch(record.get("until_epoch")):
            return None
        return record
    if state == "cooldown":
        if not _keys_exact(record, _SWEEP_COOLDOWN_KEYS):
            return None
        if not _id_ok(record.get("generation_id")):
            return None
        # cooldown deadline must be strictly positive; epoch 0 is invalid
        v = record.get("sweep_deadline_epoch")
        if not (type(v) is int and v > 0):
            return None
        if not _epoch(record.get("retry_after_epoch")):
            return None
        return record
    return None


def encode_sweep_header(record):
    """Encode a sweep-header record to canonical sorted JSON."""
    if not isinstance(record, dict):
        raise TypeError("record must be a dict")
    sv = record.get("schema_version")
    if not (type(sv) is int and sv == _SCHEMA_VERSION):
        raise ValueError("schema_version must be 1")
    state = record.get("state")
    if state not in _SWEEP_STATES:
        raise ValueError(f"state must be one of {sorted(_SWEEP_STATES)!r}")
    if state == "sweeping":
        if set(record.keys()) != _SWEEPING_KEYS:
            raise ValueError("sweeping record key set mismatch")
        if not _id_ok(record.get("generation_id")):
            raise ValueError("generation_id must be 32 lowercase hex")
        for field in (
            "opened_epoch",
            "deadline_epoch",
            "window_seconds",
            "total",
            "tested",
            "blocked",
        ):
            if not _epoch(record.get(field)):
                raise ValueError(f"{field} must be a non-negative int")
        if not isinstance(record.get("global_possible"), bool):
            raise ValueError("global_possible must be a bool")
        if record["tested"] > record["total"] or record["blocked"] > record["tested"]:
            raise ValueError(
                "invariant violated: tested <= total and blocked <= tested"
            )
    elif state == "healthy":
        if set(record.keys()) != _HEALTHY_KEYS:
            raise ValueError("healthy record key set mismatch")
        if not _id_ok(record.get("generation_id")):
            raise ValueError("generation_id must be 32 lowercase hex")
        if not _epoch(record.get("until_epoch")):
            raise ValueError("until_epoch must be a non-negative int")
    else:  # cooldown
        if set(record.keys()) != _SWEEP_COOLDOWN_KEYS:
            raise ValueError("cooldown record key set mismatch")
        if not _id_ok(record.get("generation_id")):
            raise ValueError("generation_id must be 32 lowercase hex")
        # cooldown deadline must be strictly positive; epoch 0 is invalid
        v = record.get("sweep_deadline_epoch")
        if not (type(v) is int and v > 0):
            raise ValueError("sweep_deadline_epoch must be a strictly positive int")
        if not _epoch(record.get("retry_after_epoch")):
            raise ValueError("retry_after_epoch must be a non-negative int")
    return _encode(record)


# ── sweep-member codec ────────────────────────────────────────────────────────


def decode_sweep_member(value):
    """Decode a stored sweep-member value.  Returns None for any invalid input."""
    if _oversized(value):
        return None
    record = _parse(value)
    if record is None or not _version_ok(record):
        return None
    if not _keys_exact(record, _MEMBER_KEYS):
        return None
    if not _id_ok(record.get("generation_id")):
        return None
    if not _fp_ok(record.get("fingerprint")):
        return None
    state = record.get("state")
    if state not in _MEMBER_STATES:
        return None
    if not _decode_member_lease(record.get("lease")):
        return None
    if not _decode_member_outcome(record.get("outcome")):
        return None
    is_terminal = state in _MEMBER_TERMINAL_STATES
    if is_terminal and record.get("outcome") is None:
        return None
    if not is_terminal and record.get("outcome") is not None:
        return None
    return record


def encode_sweep_member(record):
    """Encode a sweep-member record to canonical sorted JSON."""
    if not isinstance(record, dict):
        raise TypeError("record must be a dict")
    sv = record.get("schema_version")
    if not (type(sv) is int and sv == _SCHEMA_VERSION):
        raise ValueError("schema_version must be 1")
    if set(record.keys()) != _MEMBER_KEYS:
        raise ValueError("sweep member key set mismatch")
    if not _id_ok(record.get("generation_id")):
        raise ValueError("generation_id must be 32 lowercase hex")
    if not _fp_ok(record.get("fingerprint")):
        raise ValueError("fingerprint must be 64 lowercase hex")
    state = record.get("state")
    if state not in _MEMBER_STATES:
        raise ValueError(f"state must be one of {sorted(_MEMBER_STATES)!r}")
    _encode_member_lease(record.get("lease"))
    _encode_member_outcome(record.get("outcome"))
    is_terminal = state in _MEMBER_TERMINAL_STATES
    if is_terminal and record.get("outcome") is None:
        raise ValueError("terminal member state requires outcome")
    if not is_terminal and record.get("outcome") is not None:
        raise ValueError("non-terminal member state must have outcome=null")
    return _encode(record)


# ── offer-receipt codec ───────────────────────────────────────────────────────


def decode_offer_receipt(value):
    """Decode a stored offer-receipt value.  Returns None for any invalid input."""
    if _oversized(value):
        return None
    record = _parse(value)
    if record is None or not _version_ok(record):
        return None
    if not _keys_exact(record, _RECEIPT_KEYS):
        return None
    if not _id_ok(record.get("generation_id")):
        return None
    if not _fp_ok(record.get("fingerprint")):
        return None
    if not _pkg_ok(record.get("package_id")):
        return None
    if record.get("mode") not in _OFFER_MODES:
        return None
    if record.get("outcome") not in _OUTCOME_VALUES:
        return None
    if not isinstance(record.get("response"), dict):
        return None
    if not _epoch(record.get("accepted_epoch")):
        return None
    if not _epoch(record.get("expires_epoch")):
        return None
    return record


def encode_offer_receipt(record):
    """Encode an offer-receipt record to canonical sorted JSON."""
    if not isinstance(record, dict):
        raise TypeError("record must be a dict")
    sv = record.get("schema_version")
    if not (type(sv) is int and sv == _SCHEMA_VERSION):
        raise ValueError("schema_version must be 1")
    if set(record.keys()) != _RECEIPT_KEYS:
        raise ValueError("receipt key set mismatch")
    if not _id_ok(record.get("generation_id")):
        raise ValueError("generation_id must be 32 lowercase hex")
    if not _fp_ok(record.get("fingerprint")):
        raise ValueError("fingerprint must be 64 lowercase hex")
    if not _pkg_ok(record.get("package_id")):
        raise ValueError("package_id must be a canonical package ID")
    if record.get("mode") not in _OFFER_MODES:
        raise ValueError(f"mode must be one of {sorted(_OFFER_MODES)!r}")
    if record.get("outcome") not in _OUTCOME_VALUES:
        raise ValueError(f"outcome must be one of {sorted(_OUTCOME_VALUES)!r}")
    if not isinstance(record.get("response"), dict):
        raise ValueError("response must be a dict")
    if not _epoch(record.get("accepted_epoch")):
        raise ValueError("accepted_epoch must be a non-negative int")
    if not _epoch(record.get("expires_epoch")):
        raise ValueError("expires_epoch must be a non-negative int")
    return _encode(record)


# ── migration-marker codec ────────────────────────────────────────────────────


def decode_migration_marker(value):
    """Decode a stored migration-marker value.  Returns None for any invalid input."""
    if _oversized(value):
        return None
    record = _parse(value)
    if record is None or not _version_ok(record):
        return None
    if not _keys_exact(record, _MIGRATION_KEYS):
        return None
    if not _epoch(record.get("completed_epoch")):
        return None
    return record


def encode_migration_marker(record):
    """Encode a migration-marker record to canonical sorted JSON."""
    if not isinstance(record, dict):
        raise TypeError("record must be a dict")
    sv = record.get("schema_version")
    if not (type(sv) is int and sv == _SCHEMA_VERSION):
        raise ValueError("schema_version must be 1")
    if set(record.keys()) != _MIGRATION_KEYS:
        raise ValueError("migration marker key set mismatch")
    if not _epoch(record.get("completed_epoch")):
        raise ValueError("completed_epoch must be a non-negative int")
    return _encode(record)


# ── deadline predicates ───────────────────────────────────────────────────────


def link_state_is_held(record, now):
    """Return True while the hold is active (now < retry_after_epoch)."""
    if not isinstance(record, dict) or record.get("state") != "held":
        return False
    retry_after = record.get("retry_after_epoch")
    if not _epoch(retry_after):
        return False
    return now < retry_after


def link_state_is_recheck_eligible(record, now):
    """Return True when the hold has expired and a recheck may be issued (now >= retry_after_epoch)."""
    if not isinstance(record, dict) or record.get("state") != "held":
        return False
    retry_after = record.get("retry_after_epoch")
    if not _epoch(retry_after):
        return False
    return now >= retry_after
