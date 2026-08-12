# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import json
import re
import secrets
import time
from collections import namedtuple

from quasarr.constants import PACKAGE_ID_PATTERN
from quasarr.providers.crypter_sweeps import (
    FAIL_CLOSED_INDIVIDUAL_REASON,
    MAXIMUM_COHORT_SIZE,
    SWEEP_SCHEMA_VERSION,
    Offer,
    bypass_decision,
    decision_snapshot,
    decode_decision_record,
    encode_decision_record,
    healthy_from_legacy_success,
    is_decision_record,
    lease_next_offer,
    migrate_legacy_record,
    prepare_decision,
    record_access,
    record_blocked,
    record_legacy_report,
    stale_decision,
)
from quasarr.providers.log import warn

OBSERVATION_WINDOW_SECONDS = 15 * 60
MINIMUM_COOLDOWN_HOURS = 24
EVIDENCE_THRESHOLD = 3
SUPPORTED_REASON_CODES = frozenset({"ip_block_suspected"})
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SWEEP_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_RECORD_KEYS = {
    "state",
    "reason_code",
    "first_seen_epoch",
    "last_seen_epoch",
    "retry_after_epoch",
    "observations",
}
_OBSERVATION_KEYS = {"package_id", "link_fingerprint", "seen_at_epoch"}
PACKAGE_DEFER_KEY = "deferred"
MAXIMUM_OBSERVATION_HOLDS = 1
DEFAULT_CRYPTER_BLOCK_MODE = "defer"
LEGACY_CRYPTER_BLOCK_MODE = "fail"
CRYPTER_EVENT_TABLE = "crypter_events"
CRYPTER_EVENT_KEY = "pending"
CRYPTER_EVENT_FIELDS = ("observations", "cooldowns", "probes")
# A ledger count is a cumulative total, so it is bounded by what one row can
# hold and round-trip, not by a plausible number of events. The ceiling stays
# far below the interpreter's integer/string conversion limit.
MAXIMUM_CRYPTER_EVENT_DIGITS = 1000
MAXIMUM_CRYPTER_EVENT_COUNT = 10**MAXIMUM_CRYPTER_EVENT_DIGITS - 1
_PACKAGE_DEFER_KEYS = frozenset(
    {
        "crypter",
        "reason_code",
        "since_epoch",
        "retry_after_epoch",
        "probe_requested",
        "observation_holds",
    }
)
_PACKAGE_DEFER_V2_KEYS = _PACKAGE_DEFER_KEYS | {
    "schema_version",
    "sweep_id",
    "link_fingerprints",
}
_LEGACY_DEFER_SHAPE = "legacy"
_GENERATION_DEFER_SHAPE = "generation"
_DECISION_STATES = frozenset(
    {"available", "sweeping", "cooldown", "healthy", "individual"}
)
# One linkcrypter row projected both ways. Deriving the legacy-shaped snapshot
# and the version-two decision from two separate reads lets a transition in
# between combine a stale cooldown with a newer decision, or hide a cooldown
# that has just started.
CrypterProjection = namedtuple("CrypterProjection", ("snapshot", "decision"))


def crypter_blocks_deferred(shared_state):
    """Whether cooldown and package defer holds may gate a linkcrypter.

    Reads the cached block mode only, so hot paths never touch the settings
    table. Anything but the exact legacy mode defers, and the legacy mode is a
    pure bypass: persisted cooldown and defer metadata stay untouched so
    switching back restores the held state.
    """
    mode = shared_state.values.get("crypter_block_mode", DEFAULT_CRYPTER_BLOCK_MODE)
    return mode != LEGACY_CRYPTER_BLOCK_MODE


def normalize_crypter_key(value):
    from quasarr.downloads import protected_crypter_keys

    if not isinstance(value, str):
        raise ValueError("Unsupported linkcrypter key")
    normalized = value.strip().lower()
    if normalized not in protected_crypter_keys():
        raise ValueError(f'Unsupported linkcrypter key "{value}"')
    return normalized


def validate_link_fingerprint(value):
    if not isinstance(value, str) or not _FINGERPRINT_PATTERN.fullmatch(value):
        raise ValueError("Link fingerprint must be 64 lowercase hexadecimal characters")
    return value


def _validate_link_fingerprints(value):
    """The exact set of links one package hold speaks for.

    A package can carry several Filecrypt containers, so a hold has to be able
    to name more than one of them. Ascending order is the deterministic form and
    doubles as the uniqueness proof, and the cohort maximum bounds it.
    """
    if not isinstance(value, list) or not 1 <= len(value) <= MAXIMUM_COHORT_SIZE:
        raise ValueError("Package defer must hold at least one link fingerprint")
    fingerprints = []
    for entry in value:
        fingerprint = validate_link_fingerprint(entry)
        if fingerprints and fingerprint <= fingerprints[-1]:
            raise ValueError("Held link fingerprints must be unique and ordered")
        fingerprints.append(fingerprint)
    return fingerprints


def _validate_identifier(value, field_name):
    if not isinstance(value, str) or not _SWEEP_ID_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be 32 lowercase hexadecimal characters")
    return value


def _validate_sweep_id(value):
    return _validate_identifier(value, "Sweep ID")


def _validate_package_id(value):
    if not isinstance(value, str) or not PACKAGE_ID_PATTERN.fullmatch(value):
        raise ValueError(f'Invalid Quasarr package ID "{value}"')
    return value


def _validate_reason_code(value):
    if not isinstance(value, str) or value not in SUPPORTED_REASON_CODES:
        raise ValueError(f'Unsupported linkcrypter reason code "{value}"')
    return value


def _validate_epoch(value, field_name):
    if type(value) is not int or value < 0:
        raise ValueError(f'Invalid persisted linkcrypter field "{field_name}"')
    return value


def _validate_observation_holds(value):
    """A package owns at most one provisional hold, so only 0 and 1 are legal."""
    if type(value) is not int or not 0 <= value <= MAXIMUM_OBSERVATION_HOLDS:
        raise ValueError('Invalid persisted linkcrypter field "observation_holds"')
    return value


def _decode_record(value):
    if value is None:
        return None
    try:
        record = json.loads(value)
    except (TypeError, ValueError, RecursionError) as error:
        # Not every parse failure is a JSONDecodeError: an oversized integer
        # literal raises a plain ValueError and a value nested past the recursion
        # limit a RecursionError. Either one escaping here would brick the read.
        raise ValueError("Invalid persisted linkcrypter JSON") from error
    if (
        not isinstance(record, dict)
        or "schema_version" in record
        or not _RECORD_KEYS.issubset(record)
    ):
        # Key presence establishes versioning. Unsupported or malformed
        # versioned rows must never inherit permissive legacy compatibility.
        raise ValueError("Invalid persisted linkcrypter record")
    if not isinstance(record["state"], str) or record["state"] not in {
        "observing",
        "cooldown",
    }:
        raise ValueError("Invalid persisted linkcrypter state")
    _validate_reason_code(record["reason_code"])
    for field_name in (
        "first_seen_epoch",
        "last_seen_epoch",
        "retry_after_epoch",
    ):
        _validate_epoch(record[field_name], field_name)
    if not isinstance(record["observations"], list):
        raise ValueError("Invalid persisted linkcrypter observations")
    observations = []
    for observation in record["observations"]:
        if not isinstance(observation, dict) or not _OBSERVATION_KEYS.issubset(
            observation
        ):
            raise ValueError("Invalid persisted linkcrypter observation")
        _validate_package_id(observation["package_id"])
        validate_link_fingerprint(observation["link_fingerprint"])
        _validate_epoch(observation["seen_at_epoch"], "seen_at_epoch")
        observations.append(
            {
                "package_id": observation["package_id"],
                "link_fingerprint": observation["link_fingerprint"],
                "seen_at_epoch": observation["seen_at_epoch"],
            }
        )
    return {
        "state": record["state"],
        "reason_code": record["reason_code"],
        "first_seen_epoch": record["first_seen_epoch"],
        "last_seen_epoch": record["last_seen_epoch"],
        "retry_after_epoch": record["retry_after_epoch"],
        "observations": observations,
    }


def _encode_record(record):
    return json.dumps(record, separators=(",", ":"), sort_keys=True)


def _decode_package(value):
    if value is None:
        return None
    try:
        package = json.loads(value)
    except (TypeError, ValueError, RecursionError):
        # Not every parse failure is a JSONDecodeError: an integer literal with
        # more digits than Python converts raises a plain ValueError and a value
        # nested past the recursion limit a RecursionError. Either one escaping
        # here would abort the read or sweep that carries the row.
        return None
    return package if isinstance(package, dict) else None


def _row_entry(row):
    """The key and raw value of one enumerated storage row, or None when unusable."""
    if not isinstance(row, (list, tuple)) or len(row) < 2:
        return None
    package_id = row[0]
    return (package_id, row[1]) if isinstance(package_id, str) else None


def decode_package_defer(package_data):
    """Project the deferred block out of an already-parsed protected package.

    Two shapes are legal: the legacy six-key block and the version-two block,
    which adds exactly `schema_version`, `sweep_id`, and `link_fingerprints`.
    Both are exact key sets, so a partial, hybrid, or future shape is malformed
    rather than silently half-understood, and no shape can carry a raw link.
    """
    if not isinstance(package_data, dict):
        return None
    deferred = package_data.get(PACKAGE_DEFER_KEY)
    if deferred is None:
        return None
    if not isinstance(deferred, dict):
        raise ValueError("Invalid persisted package defer metadata")
    keys = set(deferred)
    if keys not in (_PACKAGE_DEFER_KEYS, _PACKAGE_DEFER_V2_KEYS):
        raise ValueError("Invalid persisted package defer metadata")
    if type(deferred["probe_requested"]) is not bool:
        raise ValueError('Invalid persisted package defer field "probe_requested"')
    decoded = {
        "crypter": normalize_crypter_key(deferred["crypter"]),
        "reason_code": _validate_reason_code(deferred["reason_code"]),
        "since_epoch": _validate_epoch(deferred["since_epoch"], "since_epoch"),
        "retry_after_epoch": _validate_epoch(
            deferred["retry_after_epoch"], "retry_after_epoch"
        ),
        "probe_requested": deferred["probe_requested"],
        "observation_holds": _validate_observation_holds(deferred["observation_holds"]),
    }
    if keys == _PACKAGE_DEFER_V2_KEYS:
        version = deferred["schema_version"]
        if type(version) is not int or version != SWEEP_SCHEMA_VERSION:
            raise ValueError('Invalid persisted package defer field "schema_version"')
        decoded.update(
            {
                "schema_version": SWEEP_SCHEMA_VERSION,
                "sweep_id": _validate_sweep_id(deferred["sweep_id"]),
                "link_fingerprints": _validate_link_fingerprints(
                    deferred["link_fingerprints"]
                ),
            }
        )
    return decoded


def _defer_shape(deferred):
    """Classify decoded or projected defer metadata, or None when unusable."""
    if not isinstance(deferred, dict) or not _PACKAGE_DEFER_KEYS.issubset(deferred):
        return None
    if "schema_version" not in deferred:
        return _LEGACY_DEFER_SHAPE
    if not _PACKAGE_DEFER_V2_KEYS.issubset(deferred):
        return None
    version = deferred["schema_version"]
    if type(version) is not int or version != SWEEP_SCHEMA_VERSION:
        return None
    return _GENERATION_DEFER_SHAPE


def _epoch_or_zero(mapping, field_name):
    value = mapping.get(field_name)
    return value if type(value) is int and value >= 0 else 0


def package_defer_is_active(deferred, decision_snapshot, *, now):
    """Whether the current linkcrypter decision still authorizes a package hold.

    `decision_snapshot` is `crypter_sweeps.decision_snapshot()` of the row that
    is current right now, or `None` when the linkcrypter carries no version-two
    decision at all. A version-two hold belongs to exactly one generation, so a
    healthy verdict, another sweep, an expired individual decision, or a missing
    decision invalidates it logically long before any row is physically cleaned
    up - and a hold whose cleanup never ran can therefore never come back. A
    legacy hold predates generations: without a version-two decision it keeps
    its own deadline, a marked legacy cooldown extends it, and no other
    version-two state may adopt it. Metadata or a decision this cannot read
    proves nothing and holds nothing.
    """
    shape = _defer_shape(deferred)
    if shape is None:
        return False
    if decision_snapshot is None:
        state = "available"
    elif (
        isinstance(decision_snapshot, dict)
        and decision_snapshot.get("state") in _DECISION_STATES
    ):
        state = decision_snapshot["state"]
    else:
        return False

    if shape == _GENERATION_DEFER_SHAPE:
        sweep_id = deferred["sweep_id"]
        if state == "sweeping":
            return decision_snapshot.get("sweep_id") == sweep_id
        if state == "cooldown":
            return (
                decision_snapshot.get("legacy_cooldown") is not True
                and decision_snapshot.get("sweep_id") == sweep_id
                and now < _epoch_or_zero(decision_snapshot, "retry_after_epoch")
            )
        if state == "individual":
            reason = decision_snapshot.get("reason_code")
            if reason not in ("legacy_v1_hold", FAIL_CLOSED_INDIVIDUAL_REASON):
                return False
            if decision_snapshot.get("generation_id") != sweep_id:
                return False
            if now >= _epoch_or_zero(decision_snapshot, "until_epoch"):
                return False
            if reason == "legacy_v1_hold":
                return True
            # The fail-closed reason retains exactly the fingerprints its own
            # report proved, so every earlier hold of that generation is
            # logically released even though its row still names the sweep.
            retained = decision_snapshot.get("hold_fingerprints") or ()
            return any(
                fingerprint in retained for fingerprint in deferred["link_fingerprints"]
            )
        return False

    if state == "available":
        return now < _epoch_or_zero(deferred, "retry_after_epoch")
    if state == "cooldown" and decision_snapshot.get("legacy_cooldown") is True:
        return now < max(
            _epoch_or_zero(deferred, "retry_after_epoch"),
            _epoch_or_zero(decision_snapshot, "retry_after_epoch"),
        )
    return False


def package_defer_covers_fingerprint(deferred, link_fingerprint):
    """Whether a package hold speaks for this exact link.

    A version-two hold names every link of that package a report already tested,
    so each of those stays held wherever it occurs while a different, never
    tested link in the same package does not. A legacy hold predates
    fingerprints and covers the whole package.
    """
    shape = _defer_shape(deferred)
    if shape is None:
        return False
    if shape == _LEGACY_DEFER_SHAPE:
        return True
    return link_fingerprint in deferred["link_fingerprints"]


def _available_snapshot():
    return {
        "state": "available",
        "reason_code": None,
        "first_seen_epoch": 0,
        "last_seen_epoch": 0,
        "retry_after_epoch": 0,
        "observations": [],
        "evidence_count": 0,
    }


def _legacy_shaped_snapshot(projected):
    """Express a version-two decision in the snapshot shape callers read today.

    `projected` is `crypter_sweeps.decision_snapshot()` of that same decision, so
    both halves of a projection always describe one row. Task 4 owns the
    transitions; until then a version-two row only has to answer the existing
    keys, and only a real cooldown may gate a linkcrypter. A sweep is the closest
    analogue of the legacy evidence-gathering state, while healthy and individual
    suppress global blocking and therefore read as available.
    """
    if projected["state"] == "cooldown":
        return {
            "state": "cooldown",
            "reason_code": projected["reason_code"],
            "first_seen_epoch": 0,
            "last_seen_epoch": 0,
            "retry_after_epoch": projected["retry_after_epoch"],
            "observations": [],
            "evidence_count": projected["evidence_count"],
        }
    if projected["state"] == "sweeping":
        return {
            "state": "observing",
            "reason_code": projected["reason_code"],
            "first_seen_epoch": projected["opened_epoch"],
            "last_seen_epoch": projected["opened_epoch"],
            "retry_after_epoch": 0,
            "observations": [],
            "evidence_count": projected["evidence_count"],
        }
    return _available_snapshot()


def decode_pending_crypter_events(value):
    """Decode the durable transition ledger into its bounded counters.

    The ledger is one row of three counters and nothing else, so it can never
    carry a package ID, link, or linkcrypter. Returns the counters plus whether
    the stored row was readable, because an unreadable row is worth nothing and
    must be pruned instead of blocking every later flush.
    """
    empty = dict.fromkeys(CRYPTER_EVENT_FIELDS, 0)
    if value is None:
        return empty, True
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, RecursionError):
        # Not every parse failure is a JSONDecodeError: an integer literal with
        # more digits than Python converts raises a plain ValueError, and a
        # value nested past the recursion limit raises RecursionError. Either
        # one escaping here would abort the transition that carries the row.
        return empty, False
    if not isinstance(decoded, dict) or set(decoded) != set(CRYPTER_EVENT_FIELDS):
        return empty, False

    counts = {}
    for field in CRYPTER_EVENT_FIELDS:
        amount = decoded[field]
        if type(amount) is not int or not 0 <= amount <= MAXIMUM_CRYPTER_EVENT_COUNT:
            return empty, False
        counts[field] = amount
    return counts, True


def encode_pending_crypter_events(counts):
    """Encode the ledger, or None once nothing is pending so the row is dropped.

    Counts stay exact at any size a row can hold. A count that no longer fits
    raises, which rolls back the whole transition rather than committing a
    silently smaller number the counters could never catch up with.
    """
    pending = {field: counts.get(field, 0) for field in CRYPTER_EVENT_FIELDS}
    if any(amount > MAXIMUM_CRYPTER_EVENT_COUNT for amount in pending.values()):
        raise OverflowError("Linkcrypter transition count is too large to store")
    if not any(pending.values()):
        return None
    return json.dumps(pending, separators=(",", ":"), sort_keys=True)


def _add_pending_crypter_events(current_value, **deltas):
    """Add transition deltas to the ledger, leaving the row alone when empty."""
    if not any(deltas.values()):
        return current_value
    counts, _readable = decode_pending_crypter_events(current_value)
    for field, delta in deltas.items():
        counts[field] += delta
    return encode_pending_crypter_events(counts)


def _write_generation_hold(
    current_value,
    now,
    *,
    crypter,
    reason_code,
    sweep_id,
    link_fingerprint,
    retry_after_epoch,
    observation_holds,
    retain_existing=True,
):
    """Bind one protected row to the generation that is holding it.

    A generation-bound hold is not the legacy provisional budget: each sweep is
    its own bounded window and dies with its generation, so a new generation
    always writes its own hold instead of being refused by an older marker.
    A package can carry several blocked containers, so a hold of the same
    generation collects them instead of dropping the link it held before.
    `retain_existing=False` is the fail-closed write, which keeps exactly the
    fingerprint this report proved.
    """
    package = _decode_package(current_value)
    if package is None:
        return current_value
    try:
        existing = decode_package_defer(package)
    except ValueError:
        existing = None
    if existing is not None and existing["crypter"] != crypter:
        existing = None
    held = {link_fingerprint}
    if (
        retain_existing
        and existing is not None
        and existing.get("sweep_id") == sweep_id
    ):
        held.update(existing["link_fingerprints"])
    if len(held) > MAXIMUM_COHORT_SIZE:
        raise OverflowError("Package defer holds too many link fingerprints")
    package[PACKAGE_DEFER_KEY] = {
        "crypter": crypter,
        "reason_code": reason_code,
        "since_epoch": existing["since_epoch"] if existing else now,
        "retry_after_epoch": retry_after_epoch,
        "probe_requested": existing["probe_requested"] if existing else False,
        "observation_holds": observation_holds,
        "schema_version": SWEEP_SCHEMA_VERSION,
        "sweep_id": sweep_id,
        "link_fingerprints": sorted(held),
    }
    return json.dumps(package)


def _clear_generation_hold(current_value, _now, *, crypter, sweep_id):
    """Drop a hold only while it still belongs to this exact generation."""
    package = _decode_package(current_value)
    if package is None or PACKAGE_DEFER_KEY not in package:
        return current_value
    try:
        deferred = decode_package_defer(package)
    except ValueError:
        return current_value
    if (
        deferred is None
        or deferred["crypter"] != crypter
        or deferred.get("sweep_id") != sweep_id
    ):
        return current_value
    package.pop(PACKAGE_DEFER_KEY)
    return json.dumps(package)


def _package_defer_or_none(current_value):
    """The decoded defer metadata of one raw row, plus whether it was unusable."""
    package = _decode_package(current_value)
    if package is None:
        return None, False
    try:
        return decode_package_defer(package), False
    except ValueError:
        return None, True


def _write_legacy_defer(
    current_value, now, *, crypter, reason_code, retry_after_epoch, observation_holds
):
    """Attach the version-one hold to one protected row.

    Answers the new raw value, the stored metadata (None when the row is missing
    or unreadable), and whether existing metadata had to be discarded.
    """
    package = _decode_package(current_value)
    if package is None:
        return current_value, None, False
    existing, invalid = _package_defer_or_none(current_value)

    previous_holds = existing["observation_holds"] if existing else 0
    previous_retry_after = existing["retry_after_epoch"] if existing else 0
    if observation_holds and previous_holds >= observation_holds:
        retry_after = previous_retry_after
    else:
        retry_after = max(previous_retry_after, retry_after_epoch)

    stored = {
        "crypter": crypter,
        "reason_code": reason_code,
        "since_epoch": existing["since_epoch"] if existing else now,
        "retry_after_epoch": retry_after,
        "probe_requested": existing["probe_requested"] if existing else False,
        "observation_holds": max(previous_holds, observation_holds),
    }
    if existing and existing["crypter"] == crypter and "schema_version" in existing:
        # This never mints a generation; it only refuses to strip one a stored
        # hold already carries, which would leave that hold bound to nothing and
        # impossible to clear by generation. A hold of a different linkcrypter
        # never inherits it.
        stored.update(
            {
                "schema_version": existing["schema_version"],
                "sweep_id": existing["sweep_id"],
                "link_fingerprints": existing["link_fingerprints"],
            }
        )
    package[PACKAGE_DEFER_KEY] = dict(stored)
    return json.dumps(package), stored, invalid


def version_one_route_answer(decision, *, cohort_live, existing_defer):
    """The exact version-one response mapping for one recorded report.

    Pure, and decided inside the recording transaction: a live version-two
    decision always outranks the version-one accumulator, so only a confirmed
    cooldown still writes a package hold under one. `observation_holds` is None
    when the answer writes no package metadata at all.
    """
    if decision["state"] == "cooldown":
        return {
            "instruction": "cooldown",
            "hold_type": "crypter_cooldown",
            "observation_holds": 0,
            "retry_after_epoch": decision["package_retry_after_epoch"],
        }
    if cohort_live:
        return {
            "instruction": "legacy_failure",
            "hold_type": "none",
            "observation_holds": None,
            "retry_after_epoch": 0,
        }
    if existing_defer and existing_defer["observation_holds"]:
        return {
            "instruction": "legacy_failure",
            "hold_type": "none",
            "observation_holds": 1,
            "retry_after_epoch": 0,
        }
    return {
        "instruction": "hold",
        "hold_type": "provisional",
        "observation_holds": 1,
        "retry_after_epoch": decision["package_retry_after_epoch"],
    }


class CrypterCooldownService:
    def __init__(self, shared_state, clock=time.time):
        self._shared_state = shared_state
        self._clock = clock

    def _cooldown_seconds(self):
        configured = self._shared_state.values.get(
            "crypter_cooldown_hours", MINIMUM_COOLDOWN_HOURS
        )
        try:
            hours = int(configured)
        except (TypeError, ValueError):
            hours = MINIMUM_COOLDOWN_HOURS
        return max(MINIMUM_COOLDOWN_HOURS, hours) * 60 * 60

    @staticmethod
    def _warn_invalid_record(crypter):
        warn(f'Discarding invalid persisted cooldown for linkcrypter "{crypter}"')

    @staticmethod
    def _warn_invalid_package_defer(package_id):
        warn(f'Ignoring invalid persisted defer metadata for package "{package_id}"')

    def _new_identifier(self):
        """A fresh sweep, generation, or offer ID; injected in hermetic tests."""
        return secrets.token_hex(16)

    def _current_decision(self, current_value, now):
        """The version-two decision one transition starts from.

        A live version-one cooldown is migrated rather than overwritten, so it
        keeps its exact retry deadline instead of being replaced by a sweep. A
        version-one `observing` row is not a cohort and migrates to nothing.
        """
        decision = decode_decision_record(current_value, now=now)
        if decision is not None:
            return decision
        return migrate_legacy_record(current_value, now=now)

    def _report_ownership(self, crypter, package_id, link_fingerprint, inventory):
        """Whether one report may write, and which protected rows it may write.

        An offer identity proves a generation, never a container, so the write
        set is the live occurrence set of the reported link. While the inventory
        can be read, a reporting package that does not own that link is stale
        and writes nothing at all - a deleted, replaced, or simply wrong package
        must never collect a hold for a link it does not carry. While it cannot,
        one narrow row read may still prove the reporter itself; ownership that
        stays unproven writes no row, so the fail-closed decision transition is
        the only thing the report can still change.

        Resolved before the transaction opens, so no mutation callback ever
        enumerates or reads storage.
        """
        if inventory is not None:
            owners = sorted(
                {
                    occurrence.package_id
                    for candidate in inventory.candidates
                    if candidate.fingerprint == link_fingerprint
                    for occurrence in candidate.occurrences
                }
            )
            if package_id not in owners:
                return True, ()
            return False, tuple(owners)
        try:
            raw_package = self._shared_state.get_db("protected").retrieve(package_id)
        except Exception:
            raw_package = None
        # Imported here because the candidate module reaches back into
        # `quasarr.downloads`, which imports this one at module scope.
        from quasarr.providers.crypter_candidates import package_owns_fingerprint

        if package_owns_fingerprint(raw_package, crypter, link_fingerprint):
            return False, (package_id,)
        return False, ()

    def _commit_transition(self, crypter, targets, transition, package_write):
        """Commit one decision, its package rows, and its ledger deltas together.

        `transition(record, now)` answers the new record and its decision;
        `package_write(decision)` answers the writer for one protected row, or
        None when the transition touches no package. Nothing inside the
        transaction resolves storage or settings, and an oversized record or a
        ledger overflow raises there, which rolls every row back at once.
        """
        now = int(self._clock())
        keys = (
            ("crypter_cooldowns", crypter),
            *(("protected", package_id) for package_id in targets),
            (CRYPTER_EVENT_TABLE, CRYPTER_EVENT_KEY),
        )
        committed = {}

        def commit(current_values):
            current = self._current_decision(current_values[0], now)
            record, decision = transition(current, now)
            committed.update(decision)
            current_accepted = (
                current.get("accepted_offers") if isinstance(current, dict) else None
            )
            record_accepted = (
                record.get("accepted_offers") if isinstance(record, dict) else None
            )
            write = (
                package_write(decision) if record_accepted != current_accepted else None
            )
            return (
                None if record is None else encode_decision_record(record),
                *(
                    value if write is None else write(value, now)
                    for value in current_values[1:-1]
                ),
                _add_pending_crypter_events(current_values[-1], **decision["events"]),
            )

        self._shared_state.get_db("crypter_cooldowns").mutate_values(keys, commit)
        return committed

    def prepare_offer(
        self, crypter, inventory, *, mode=None, preferred_fingerprint=None
    ):
        """Open or advance the decision and lease at most one offer.

        `mode` requests a specific handout - the manual `probe` a cohort
        cooldown issues - and otherwise the natural mode of the current state.
        `preferred_fingerprint` names the exact member the caller can hand out,
        which a probe must supply because it authorizes one package. `fail` mode
        is a pure bypass that neither creates, advances, expires, nor clears
        cohort state. Returns the offer to hand out, or None when there is no
        work.
        """
        crypter = normalize_crypter_key(crypter)
        if not crypter_blocks_deferred(self._shared_state):
            return None
        if preferred_fingerprint is not None:
            preferred_fingerprint = validate_link_fingerprint(preferred_fingerprint)
        now = int(self._clock())
        leased = {"offer": None}

        def advance(current_value):
            record = prepare_decision(
                inventory,
                self._current_decision(current_value, now),
                now=now,
                sweep_id_factory=self._new_identifier,
            )
            record, offer = lease_next_offer(
                record,
                inventory,
                now=now,
                offer_id_factory=self._new_identifier,
                mode=mode,
                preferred_fingerprint=preferred_fingerprint,
            )
            leased["offer"] = offer
            return None if record is None else encode_decision_record(record)

        self._shared_state.get_db("crypter_cooldowns").mutate_value(crypter, advance)
        offer = leased["offer"]
        if offer is None:
            return None
        return {
            "mode": offer.mode,
            "sweep_id": offer.sweep_id,
            "offer_id": offer.offer_id,
            "link_fingerprint": offer.fingerprint,
            "deadline_epoch": offer.deadline_epoch,
        }

    def record_cohort_blocked(
        self,
        crypter,
        package_id,
        link_fingerprint,
        sweep_id,
        offer_id,
        reason_code,
        inventory,
    ):
        """Commit one exact cohort BLOCKED report and its holds atomically."""
        crypter = normalize_crypter_key(crypter)
        package_id = _validate_package_id(package_id)
        link_fingerprint = validate_link_fingerprint(link_fingerprint)
        offer = Offer(
            "",
            _validate_sweep_id(sweep_id),
            _validate_identifier(offer_id, "Offer ID"),
            link_fingerprint,
            0,
        )
        reason_code = _validate_reason_code(reason_code)
        if not crypter_blocks_deferred(self._shared_state):
            return bypass_decision()
        cooldown_seconds = self._cooldown_seconds()
        stale, targets = self._report_ownership(
            crypter, package_id, link_fingerprint, inventory
        )

        def transition(record, now):
            if stale:
                return record, stale_decision(record, now=now)
            return record_blocked(
                record, offer, inventory, now=now, cooldown_seconds=cooldown_seconds
            )

        def package_write(decision):
            if decision["instruction"] not in ("hold", "cooldown"):
                return None
            return lambda value, now: _write_generation_hold(
                value,
                now,
                crypter=crypter,
                reason_code=reason_code,
                sweep_id=decision["sweep_id"],
                link_fingerprint=link_fingerprint,
                retry_after_epoch=decision["retry_after_epoch"],
                observation_holds=0 if decision["instruction"] == "cooldown" else 1,
                # An individual decision never holds a cohort, so its hold is
                # exactly the link this report proved and nothing else.
                retain_existing=decision["state"] != "individual",
            )

        return self._commit_transition(
            crypter,
            targets,
            transition,
            package_write,
        )

    def record_cohort_access(
        self,
        crypter,
        package_id,
        link_fingerprint,
        sweep_id,
        offer_id,
        access,
        inventory,
    ):
        """Commit one exact cohort CLEAR or UNKNOWN report atomically.

        A committed CLEAR then triggers best-effort physical cleanup of the rest
        of that generation. It runs outside the decision transaction and can only
        remove metadata the committed `healthy` record already invalidated, so
        its failure can neither reactivate a hold nor change the response.
        """
        crypter = normalize_crypter_key(crypter)
        package_id = _validate_package_id(package_id)
        link_fingerprint = validate_link_fingerprint(link_fingerprint)
        offer = Offer(
            "",
            _validate_sweep_id(sweep_id),
            _validate_identifier(offer_id, "Offer ID"),
            link_fingerprint,
            0,
        )
        if access not in ("clear", "unknown"):
            raise ValueError(f'Unsupported linkcrypter access value "{access}"')
        if not crypter_blocks_deferred(self._shared_state):
            return bypass_decision()
        stale, targets = self._report_ownership(
            crypter, package_id, link_fingerprint, inventory
        )

        def transition(record, now):
            if stale:
                return record, stale_decision(record, now=now)
            return record_access(record, offer, access, inventory, now=now)

        def package_write(decision):
            if not decision["cleared"]:
                return None
            return lambda value, now: _clear_generation_hold(
                value, now, crypter=crypter, sweep_id=offer.sweep_id
            )

        decision = self._commit_transition(
            crypter,
            targets,
            transition,
            package_write,
        )
        if decision["cleared"]:
            try:
                self.clear_crypter_generation_holds(crypter, sweep_id=offer.sweep_id)
            except Exception:
                warn(
                    "Deferred package cleanup after a successful linkcrypter "
                    f'access report failed for "{crypter}"; those holds are '
                    "already inactive"
                )
        return decision

    @staticmethod
    def _prune_record(record, now):
        if record is None:
            return None
        if record["state"] == "cooldown" and record["retry_after_epoch"] <= now:
            return None

        cutoff = now - OBSERVATION_WINDOW_SECONDS
        record["observations"] = [
            observation
            for observation in record["observations"]
            if observation["seen_at_epoch"] >= cutoff
        ]
        if record["state"] == "observing" and not record["observations"]:
            return None
        if record["observations"]:
            seen_at = [
                observation["seen_at_epoch"] for observation in record["observations"]
            ]
            record["first_seen_epoch"] = min(seen_at)
            record["last_seen_epoch"] = max(seen_at)
        return record

    def _cleanup_projection(self, database, crypter, now, invalid_observed=False):
        result = {}
        current = {"decision": None}
        invalid = {"observed": invalid_observed}

        def cleanup(current_value):
            decision = decode_decision_record(current_value, now=now)
            if decision is not None:
                invalid["observed"] = False
                projected = decision_snapshot(decision, now=now)
                current["decision"] = projected
                result.update(_legacy_shaped_snapshot(projected))
                return current_value
            current["decision"] = None
            if is_decision_record(current_value):
                # A valid version-two row whose own window merely ended is a
                # clean expiry, not the malformed row the legacy reader reports.
                invalid["observed"] = False
                result.update(_available_snapshot())
                return None

            try:
                record = _decode_record(current_value)
            except ValueError:
                invalid["observed"] = True
                result.update(_available_snapshot())
                return None
            invalid["observed"] = False

            observation_count = len(record["observations"]) if record else 0
            record = self._prune_record(record, now)
            if record is None:
                result.update(_available_snapshot())
                return None

            result.update(record)
            result["evidence_count"] = len(record["observations"])
            if len(record["observations"]) == observation_count:
                return current_value
            return _encode_record(record)

        database.mutate_value(crypter, cleanup)
        if invalid["observed"]:
            self._warn_invalid_record(crypter)
        return CrypterProjection(result, current["decision"])

    def observe(self, crypter, package_id, link_fingerprint, reason_code):
        """Record one version-one block observation and report what it changed.

        `recorded` and `cooldown_started` are decided inside the same
        transaction that writes the record, and that transaction also adds them
        to the durable event ledger, so no crash between the two can lose or
        repeat a transition and no caller has to re-derive one from a later read.

        A version-two row is never legacy-decoded and never overwritten here: it
        follows the version-one precedence rules instead, which is why the old
        fixed three-observation path can no longer reach a global cooldown once a
        cohort decision exists.
        """
        crypter = normalize_crypter_key(crypter)
        outcome = self._version_one_outcome(package_id, link_fingerprint, reason_code)
        database = self._shared_state.get_db("crypter_cooldowns")

        def record_and_count(current_values):
            record_value, ledger_value = current_values
            new_record = outcome["mutate_record"](record_value)
            return new_record, _add_pending_crypter_events(
                ledger_value, **self._legacy_events(outcome)
            )

        database.mutate_values(
            (("crypter_cooldowns", crypter), (CRYPTER_EVENT_TABLE, CRYPTER_EVENT_KEY)),
            record_and_count,
        )
        if outcome["invalid_record"]:
            self._warn_invalid_record(crypter)
        return outcome["decision"]

    def record_version_one_report(
        self, crypter, package_id, link_fingerprint, reason_code
    ):
        """Commit one version-one report and its route answer in one transaction.

        The accumulator, the version-two precedence mapping, and the package
        hold are all decided against the row current inside this transaction, so
        a cohort decision that opens between an earlier read and the commit can
        never be answered as a version-one hold, nor have a legacy hold written
        under it. Returns the recorded legacy decision plus the exact answer the
        route owes, including whether the protected package was missing.
        """
        crypter = normalize_crypter_key(crypter)
        outcome = self._version_one_outcome(package_id, link_fingerprint, reason_code)
        package_id = _validate_package_id(package_id)
        reason_code = _validate_reason_code(reason_code)
        now = outcome["now"]
        answer = {}

        def commit(current_values):
            record_value, package_value, ledger_value = current_values
            new_record = outcome["mutate_record"](record_value)
            existing, invalid = _package_defer_or_none(package_value)
            outcome["invalid_defer"] = invalid
            answer.update(
                version_one_route_answer(
                    outcome["decision"],
                    cohort_live=outcome["cohort"],
                    existing_defer=existing,
                )
            )
            new_package = package_value
            if answer["observation_holds"] is not None:
                new_package, stored, _invalid = _write_legacy_defer(
                    package_value,
                    now,
                    crypter=crypter,
                    reason_code=reason_code,
                    retry_after_epoch=outcome["decision"]["package_retry_after_epoch"],
                    observation_holds=answer["observation_holds"],
                )
                answer["package_missing"] = stored is None
            return (
                new_record,
                new_package,
                _add_pending_crypter_events(
                    ledger_value, **self._legacy_events(outcome)
                ),
            )

        self._shared_state.get_db("crypter_cooldowns").mutate_values(
            (
                ("crypter_cooldowns", crypter),
                ("protected", package_id),
                (CRYPTER_EVENT_TABLE, CRYPTER_EVENT_KEY),
            ),
            commit,
        )
        if outcome["invalid_record"]:
            self._warn_invalid_record(crypter)
        if outcome["invalid_defer"]:
            self._warn_invalid_package_defer(package_id)
        return {
            "state": outcome["decision"]["state"],
            "evidence_count": outcome["decision"]["evidence_count"],
            "instruction": answer["instruction"],
            "hold_type": answer["hold_type"],
            "retry_after_epoch": answer["retry_after_epoch"],
            "package_missing": answer.get("package_missing", False),
        }

    @staticmethod
    def _legacy_events(outcome):
        decision = outcome["decision"]
        return {
            "observations": int(decision["recorded"]),
            "cooldowns": int(decision["cooldown_started"]),
        }

    def _version_one_outcome(self, package_id, link_fingerprint, reason_code):
        """The shared version-one recording state and its linkcrypter mutator.

        `mutate_record` answers the new linkcrypter row for the value current
        inside a transaction and fills `decision`, `cohort` - whether a live
        version-two decision took precedence - and `invalid_record`, so every
        caller answers and writes from that one value instead of an earlier read.
        """
        package_id = _validate_package_id(package_id)
        link_fingerprint = validate_link_fingerprint(link_fingerprint)
        reason_code = _validate_reason_code(reason_code)
        now = int(self._clock())
        cooldown_seconds = self._cooldown_seconds()
        outcome = {
            "now": now,
            "decision": {},
            "cohort": False,
            "invalid_record": False,
            "invalid_defer": False,
        }
        decision = outcome["decision"]

        def mutate_record(current_value):
            if is_decision_record(current_value):
                current = decode_decision_record(current_value, now=now)
                outcome["cohort"] = current is not None
                record, legacy = record_legacy_report(
                    current,
                    now=now,
                    generation_id_factory=self._new_identifier,
                )
                decision.update(legacy)
                return None if record is None else encode_decision_record(record)

            try:
                record = _decode_record(current_value)
            except ValueError:
                outcome["invalid_record"] = True
                record = None
            record = self._prune_record(record, now)
            previous_state = record["state"] if record is not None else "available"
            if record is None:
                record = {
                    "state": "observing",
                    "reason_code": reason_code,
                    "first_seen_epoch": now,
                    "last_seen_epoch": now,
                    "retry_after_epoch": 0,
                    "observations": [],
                }

            duplicate = next(
                (
                    observation
                    for observation in record["observations"]
                    if observation["package_id"] == package_id
                    or observation["link_fingerprint"] == link_fingerprint
                ),
                None,
            )
            if duplicate is None:
                record["observations"].append(
                    {
                        "package_id": package_id,
                        "link_fingerprint": link_fingerprint,
                        "seen_at_epoch": now,
                    }
                )

            seen_at = [
                observation["seen_at_epoch"] for observation in record["observations"]
            ]
            record["first_seen_epoch"] = min(seen_at)
            record["last_seen_epoch"] = max(seen_at)
            evidence_count = len(record["observations"])
            existing_retry_after = record["retry_after_epoch"]
            if record["state"] == "cooldown" or evidence_count >= EVIDENCE_THRESHOLD:
                record["state"] = "cooldown"
                record["retry_after_epoch"] = max(
                    existing_retry_after,
                    record["last_seen_epoch"] + cooldown_seconds,
                )
                package_retry_after = record["retry_after_epoch"]
            else:
                record["state"] = "observing"
                record["retry_after_epoch"] = 0
                observed_at = (
                    duplicate["seen_at_epoch"] if duplicate is not None else now
                )
                package_retry_after = observed_at + OBSERVATION_WINDOW_SECONDS

            decision.update(
                {
                    "state": record["state"],
                    "evidence_count": evidence_count,
                    "package_retry_after_epoch": package_retry_after,
                    "recorded": duplicate is None,
                    "cooldown_started": previous_state != "cooldown"
                    and record["state"] == "cooldown",
                }
            )
            return _encode_record(record)

        outcome["mutate_record"] = mutate_record
        return outcome

    def crypter_projection(self, crypter):
        """The legacy-shaped snapshot and the version-two decision of one row read.

        Both halves come from the same raw value and the same `now`, so no row
        transition can combine a stale cooldown snapshot with a newer decision or
        hide a cooldown that started between two reads. A valid row is retrieved
        exactly once and never written; only an unreadable or expired legacy row
        takes the existing lazy cleanup transaction, which re-derives the pair
        from the value current inside it.
        """
        crypter = normalize_crypter_key(crypter)
        now = int(self._clock())
        database = self._shared_state.get_db("crypter_cooldowns")
        current_value = database.retrieve(crypter)
        if current_value is None:
            return CrypterProjection(_available_snapshot(), None)
        decision = decode_decision_record(current_value, now=now)
        if decision is not None:
            projected = decision_snapshot(decision, now=now)
            return CrypterProjection(_legacy_shaped_snapshot(projected), projected)
        if is_decision_record(current_value):
            return self._cleanup_projection(database, crypter, now)
        try:
            record = _decode_record(current_value)
        except ValueError:
            return self._cleanup_projection(
                database, crypter, now, invalid_observed=True
            )

        observation_count = len(record["observations"])
        record = self._prune_record(record, now)
        if record is None or len(record["observations"]) != observation_count:
            return self._cleanup_projection(database, crypter, now)

        result = dict(record)
        result["evidence_count"] = len(record["observations"])
        return CrypterProjection(result, None)

    def snapshot(self, crypter):
        return self.crypter_projection(crypter).snapshot

    def is_cooling(self, crypter):
        return self.snapshot(crypter)["state"] == "cooldown"

    def retry_after(self, crypter):
        return self.snapshot(crypter)["retry_after_epoch"]

    def record_success(self, crypter):
        crypter = normalize_crypter_key(crypter)
        self._shared_state.get_db("crypter_cooldowns").mutate_value(
            crypter, lambda _current_value: None
        )

    def record_legacy_success(self, crypter, *, package_id=None):
        """Apply a validated version-one CLEAR as the global healthy transition.

        The version-one route has no offer identity to validate, so this is the
        one safe path it may take into version-two state: it only ever replaces
        the decision with a health window, which logically invalidates every
        generation-bound hold at once and can never resurrect or extend a
        cooldown.

        The health window is committed first and alone. Only afterwards does the
        physical release run, and every part of it is best effort, so a failed
        cleanup can never downgrade a proven acknowledgement and a failed commit
        can never have released a hold that is still authorized. Returns the
        generation the committed window owns.
        """
        crypter = normalize_crypter_key(crypter)
        if package_id is not None:
            package_id = _validate_package_id(package_id)
        now = int(self._clock())
        committed = {}

        def advance(current_value):
            record = healthy_from_legacy_success(
                self._current_decision(current_value, now),
                now=now,
                generation_id_factory=self._new_identifier,
            )
            committed.update(record)
            return encode_decision_record(record)

        self._shared_state.get_db("crypter_cooldowns").mutate_value(crypter, advance)
        sweep_id = committed["sweep_id"]
        self._release_proven_holds(crypter, sweep_id, package_id)
        return sweep_id

    def _release_proven_holds(self, crypter, sweep_id, package_id):
        """Best-effort physical cleanup after a committed health window.

        Every removal compares inside its own transaction against the row that
        is current there, so a newer generation installed meanwhile survives:
        proving one container healthy releases the holds that generation was
        carrying, never metadata written after the proof.
        """
        try:
            self.clear_crypter_generation_holds(crypter, sweep_id=sweep_id)
        except Exception:
            warn(
                "Deferred package cleanup after a successful linkcrypter access "
                f'report failed for "{crypter}"; those holds are already inactive'
            )
        if package_id is None:
            return
        try:
            # A hold the version-one accumulator wrote carries no generation, so
            # the generation cleanup cannot name it; the reporting package is
            # the one row a version-one CLEAR can still release by itself.
            self._clear_proven_package_hold(package_id, crypter, sweep_id)
        except Exception:
            warn(
                "Deferred metadata cleanup after a successful linkcrypter "
                f'access report failed for "{crypter}"; that hold is already '
                "inactive"
            )

    def _clear_proven_package_hold(self, package_id, crypter, sweep_id):
        """Drop the reporting package's hold unless a newer generation owns it."""

        def clear_proven(current_value, package, deferred):
            if deferred["crypter"] != crypter:
                return current_value
            stored = deferred.get("sweep_id")
            if stored is not None and stored != sweep_id:
                return current_value
            package.pop(PACKAGE_DEFER_KEY)
            return json.dumps(package)

        return self._mutate_deferred_package(package_id, clear_proven) == "deferred"

    def defer_package(
        self, package_id, crypter, reason_code, retry_after_epoch, observation_holds
    ):
        """Attach deferred metadata to an existing protected package.

        `observation_holds` is the provisional hold budget this call consumes: 1
        for a provisional hold and 0 for a confirmed crypter cooldown. A package
        never receives a second provisional hold, so one dead container cannot
        collect an unlimited sequence of fresh holds. Returns the stored
        metadata, or None when the protected package is missing or unreadable.
        """
        package_id = _validate_package_id(package_id)
        crypter = normalize_crypter_key(crypter)
        reason_code = _validate_reason_code(reason_code)
        retry_after_epoch = _validate_epoch(retry_after_epoch, "retry_after_epoch")
        observation_holds = _validate_observation_holds(observation_holds)
        now = int(self._clock())
        stored = {}
        invalid_defer = {"found": False}

        def update_package(current_value):
            new_value, written, invalid = _write_legacy_defer(
                current_value,
                now,
                crypter=crypter,
                reason_code=reason_code,
                retry_after_epoch=retry_after_epoch,
                observation_holds=observation_holds,
            )
            invalid_defer["found"] = invalid
            if written is not None:
                stored.update(written)
            return new_value

        self._shared_state.get_db("protected").mutate_value(package_id, update_package)
        if invalid_defer["found"]:
            self._warn_invalid_package_defer(package_id)
        return dict(stored) if stored else None

    def clear_package_defer(self, package_id):
        """Remove deferred metadata while preserving every other package field."""
        package_id = _validate_package_id(package_id)
        cleared = {"done": False}

        def update_package(current_value):
            package = _decode_package(current_value)
            if package is None or PACKAGE_DEFER_KEY not in package:
                return current_value
            package.pop(PACKAGE_DEFER_KEY)
            cleared["done"] = True
            return json.dumps(package)

        self._shared_state.get_db("protected").mutate_value(package_id, update_package)
        return cleared["done"]

    def get_package_defer(self, package_id):
        package_id = _validate_package_id(package_id)
        package = _decode_package(
            self._shared_state.get_db("protected").retrieve(package_id)
        )
        if package is None:
            return None
        try:
            return decode_package_defer(package)
        except ValueError:
            self._warn_invalid_package_defer(package_id)
            return None

    def crypter_decision(self, crypter):
        """The current version-two decision projection, or None for a legacy row.

        Read-only, and expired decisions read as None, so a caller can only ever
        see the decision that is authoritative right now.
        """
        crypter = normalize_crypter_key(crypter)
        now = int(self._clock())
        current_value = self._shared_state.get_db("crypter_cooldowns").retrieve(crypter)
        decision = decode_decision_record(current_value, now=now)
        return None if decision is None else decision_snapshot(decision, now=now)

    def _mutate_deferred_package(self, package_id, decide, count_events=None):
        """Run one atomic protected-row transaction gated on live defer metadata.

        `decide(current_value, package, deferred)` runs inside the transaction
        and only for a readable package that still carries valid defer metadata,
        so no caller can act on a package state it observed earlier.
        `count_events()` reports the transition deltas the same transaction must
        add to the durable event ledger, so the decision and its accounting can
        never be committed apart. Returns "not_found", "not_deferred", or
        "deferred".
        """
        outcome = {"status": "not_found", "invalid": False}

        def update_package(current_value):
            package = _decode_package(current_value)
            if package is None:
                return current_value
            try:
                deferred = decode_package_defer(package)
            except ValueError:
                outcome["invalid"] = True
                deferred = None
            if deferred is None:
                outcome["status"] = "not_deferred"
                return current_value
            outcome["status"] = "deferred"
            return decide(current_value, package, deferred)

        database = self._shared_state.get_db("protected")
        if count_events is None:
            database.mutate_value(package_id, update_package)
        else:

            def decide_and_count(current_values):
                package_value, ledger_value = current_values
                new_package = update_package(package_value)
                return new_package, _add_pending_crypter_events(
                    ledger_value, **count_events()
                )

            database.mutate_values(
                (
                    ("protected", package_id),
                    (CRYPTER_EVENT_TABLE, CRYPTER_EVENT_KEY),
                ),
                decide_and_count,
            )
        if outcome["invalid"]:
            self._warn_invalid_package_defer(package_id)
        return outcome["status"]

    def request_probe(self, package_ids):
        """Queue a one-time availability probe on deferred protected packages.

        Requesting an already queued probe is idempotent and rewrites nothing.
        """
        requested = []
        rejected = []
        seen = set()

        for package_id in package_ids or []:
            try:
                normalized = _validate_package_id(package_id)
            except ValueError:
                rejected.append(
                    {"package_id": package_id, "reason": "invalid_package_id"}
                )
                continue
            if normalized in seen:
                rejected.append({"package_id": normalized, "reason": "duplicate"})
                continue
            seen.add(normalized)

            def queue_probe(current_value, package, deferred):
                if deferred["probe_requested"]:
                    return current_value
                deferred["probe_requested"] = True
                package[PACKAGE_DEFER_KEY] = deferred
                return json.dumps(package)

            status = self._mutate_deferred_package(normalized, queue_probe)
            if status == "deferred":
                requested.append(normalized)
            else:
                rejected.append({"package_id": normalized, "reason": status})

        return {"requested": requested, "rejected": rejected}

    def consume_probe(self, package_id, crypter):
        """Atomically spend a queued probe before the package is handed out.

        Clearing the flag inside the same transaction that reads it means a
        crashed consumer cannot replay one probe into an unbounded retry loop,
        and counting the spent probe in that same transaction means the count
        can never disagree with the handout it belongs to.
        """
        package_id = _validate_package_id(package_id)
        crypter = normalize_crypter_key(crypter)
        consumed = {"done": False}

        def spend_probe(current_value, package, deferred):
            if deferred["crypter"] != crypter or not deferred["probe_requested"]:
                return current_value
            deferred["probe_requested"] = False
            package[PACKAGE_DEFER_KEY] = deferred
            consumed["done"] = True
            return json.dumps(package)

        self._mutate_deferred_package(
            package_id,
            spend_probe,
            count_events=lambda: {"probes": int(consumed["done"])},
        )
        return consumed["done"]

    def delete_deferred_package(self, package_id):
        """Delete a protected package only while it is still deferred.

        Returns "deleted", "not_found", or "not_deferred" for the state the row
        had inside the deleting transaction, never for an earlier observation.
        """
        package_id = _validate_package_id(package_id)
        status = self._mutate_deferred_package(
            package_id, lambda _current_value, _package, _deferred: None
        )
        return "deleted" if status == "deferred" else status

    def _compare_and_clear(self, package_id, crypter, sweep_id, link_fingerprint=None):
        """Drop a package hold only while it still belongs to this generation.

        The comparison runs inside the clearing transaction against the value
        that is current there, so a newer generation written meanwhile survives
        instead of being erased by a decision taken on an earlier read.
        `link_fingerprint` releases exactly one held link and leaves the other
        links of the same package held; the block itself is removed once the
        last one is released.
        """
        cleared = {"done": False}

        def clear_matching_generation(current_value, package, deferred):
            if deferred.get("sweep_id") != sweep_id:
                return current_value
            if crypter is not None and deferred["crypter"] != crypter:
                return current_value
            if link_fingerprint is not None:
                remaining = [
                    entry
                    for entry in deferred["link_fingerprints"]
                    if entry != link_fingerprint
                ]
                if len(remaining) == len(deferred["link_fingerprints"]):
                    return current_value
                cleared["done"] = True
                if remaining:
                    deferred["link_fingerprints"] = remaining
                    package[PACKAGE_DEFER_KEY] = deferred
                    return json.dumps(package)
            package.pop(PACKAGE_DEFER_KEY)
            cleared["done"] = True
            return json.dumps(package)

        status = self._mutate_deferred_package(package_id, clear_matching_generation)
        if cleared["done"]:
            return "cleared"
        return "generation_mismatch" if status == "deferred" else status

    def compare_and_clear_package_defer(
        self, package_id, *, sweep_id, link_fingerprint=None
    ):
        """Clear one package hold of exactly this sweep generation."""
        package_id = _validate_package_id(package_id)
        sweep_id = _validate_sweep_id(sweep_id)
        if link_fingerprint is not None:
            link_fingerprint = validate_link_fingerprint(link_fingerprint)
        return (
            self._compare_and_clear(package_id, None, sweep_id, link_fingerprint)
            == "cleared"
        )

    def clear_crypter_generation_holds(self, crypter, *, sweep_id):
        """Best-effort removal of every package hold of one sweep generation.

        Rows are enumerated and validated before any mutation runs, so no scan
        ever happens inside a mutation callback, and each removal re-compares
        against the row current in its own transaction. The linkcrypter decision
        is never touched: logical invalidation always comes from the decision
        projection, and this only drops metadata that is already dead.

        Best-effort means no single row can end the sweep-wide cleanup. A row
        this cannot read at all - malformed, deeply nested, or carrying an
        integer literal Python refuses to convert - is reported as
        `not_deferred` and left untouched, and a target whose own transaction
        fails is reported as `storage_error` without its exception text while
        every later target still runs and every earlier clear stays committed.
        Results are reported per package ID in ascending order.
        """
        crypter = normalize_crypter_key(crypter)
        sweep_id = _validate_sweep_id(sweep_id)
        rows = self._shared_state.get_db("protected").retrieve_all_titles() or []
        targets = set()
        unreadable = set()

        for row in rows:
            entry = _row_entry(row)
            if entry is None:
                continue
            package_id, raw_value = entry
            try:
                package = _decode_package(raw_value)
                deferred = decode_package_defer(package) if package else None
            except (TypeError, ValueError, RecursionError):
                unreadable.add(package_id)
                continue
            if package is None:
                unreadable.add(package_id)
                continue
            if deferred is None:
                continue
            if deferred.get("sweep_id") == sweep_id and deferred["crypter"] == crypter:
                targets.add(package_id)

        cleared = []
        rejected = []
        for package_id in sorted(targets | unreadable):
            try:
                _validate_package_id(package_id)
            except ValueError:
                rejected.append(
                    {"package_id": package_id, "reason": "invalid_package_id"}
                )
                continue
            if package_id in unreadable:
                rejected.append({"package_id": package_id, "reason": "not_deferred"})
                continue
            try:
                status = self._compare_and_clear(package_id, crypter, sweep_id)
            except Exception:
                rejected.append({"package_id": package_id, "reason": "storage_error"})
                continue
            if status == "cleared":
                cleared.append(package_id)
            else:
                rejected.append({"package_id": package_id, "reason": status})

        return {"cleared": cleared, "rejected": rejected}

    def project_package_defer(self, deferred, snapshot, decision_snapshot=None):
        """Merge stored package defer metadata with a live crypter snapshot.

        The global cooldown and the package hold are combined on every read and
        nothing is written. `decision_snapshot` is the current version-two
        decision (or None), which is what decides whether the stored hold still
        belongs to a live generation; the legacy fields, state, and hold type
        stay exactly what existing callers already read.
        """
        now = int(self._clock())
        crypter_retry_after = (
            snapshot["retry_after_epoch"] if snapshot["state"] == "cooldown" else 0
        )
        retry_after_epoch = max(deferred["retry_after_epoch"], crypter_retry_after)
        if crypter_retry_after > now:
            hold_type = "crypter_cooldown"
        elif package_defer_is_active(deferred, decision_snapshot, now=now):
            hold_type = "provisional"
        else:
            hold_type = "none"

        projected = dict(deferred)
        projected.update(
            {
                "retry_after_epoch": retry_after_epoch,
                "state": snapshot["state"],
                "evidence_count": snapshot["evidence_count"],
                "hold_type": hold_type,
                "active": hold_type != "none",
            }
        )
        return projected

    def count_active_deferred_packages(self):
        """Current number of protected packages under an active linkcrypter hold.

        Derived from the protected rows on every read instead of maintained as a
        counter: holds also end by lazy expiry, which has no event to decrement
        on, and a derived gauge can never drift or go negative.
        """
        rows = self._shared_state.get_db("protected").retrieve_all_titles() or []
        projections = {}
        active = 0

        for row in rows:
            package = _decode_package(row[1])
            if package is None:
                continue
            try:
                deferred = decode_package_defer(package)
            except ValueError:
                continue
            if not deferred:
                continue

            crypter = deferred["crypter"]
            if crypter not in projections:
                projections[crypter] = self.crypter_projection(crypter)
            projection = projections[crypter]
            if self.project_package_defer(
                deferred, projection.snapshot, projection.decision
            )["active"]:
                active += 1

        return active
