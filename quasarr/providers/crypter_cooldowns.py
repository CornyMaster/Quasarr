# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import json
import re
import time

from quasarr.constants import PACKAGE_ID_PATTERN
from quasarr.providers.log import warn

OBSERVATION_WINDOW_SECONDS = 15 * 60
MINIMUM_COOLDOWN_HOURS = 24
EVIDENCE_THRESHOLD = 3
SUPPORTED_REASON_CODES = frozenset({"ip_block_suspected"})
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
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
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid persisted linkcrypter JSON") from error
    if not isinstance(record, dict) or not _RECORD_KEYS.issubset(record):
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
    except (TypeError, json.JSONDecodeError):
        return None
    return package if isinstance(package, dict) else None


def decode_package_defer(package_data):
    """Project the deferred block out of an already-parsed protected package."""
    if not isinstance(package_data, dict):
        return None
    deferred = package_data.get(PACKAGE_DEFER_KEY)
    if deferred is None:
        return None
    if not isinstance(deferred, dict) or set(deferred) != _PACKAGE_DEFER_KEYS:
        raise ValueError("Invalid persisted package defer metadata")
    if type(deferred["probe_requested"]) is not bool:
        raise ValueError('Invalid persisted package defer field "probe_requested"')
    return {
        "crypter": normalize_crypter_key(deferred["crypter"]),
        "reason_code": _validate_reason_code(deferred["reason_code"]),
        "since_epoch": _validate_epoch(deferred["since_epoch"], "since_epoch"),
        "retry_after_epoch": _validate_epoch(
            deferred["retry_after_epoch"], "retry_after_epoch"
        ),
        "probe_requested": deferred["probe_requested"],
        "observation_holds": _validate_observation_holds(deferred["observation_holds"]),
    }


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
    except (TypeError, ValueError):
        # An integer literal with more digits than Python converts raises a
        # plain ValueError, so JSONDecodeError alone would let a malformed row
        # abort the transition that carries it.
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

    def _cleanup_snapshot(self, database, crypter, now, invalid_observed=False):
        result = {}
        invalid = {"observed": invalid_observed}

        def cleanup(current_value):
            try:
                record = _decode_record(current_value)
            except ValueError:
                invalid["observed"] = True
                result.update(_available_snapshot())
                return None

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
        return result

    def observe(self, crypter, package_id, link_fingerprint, reason_code):
        """Record one block observation and report what it changed.

        `recorded` and `cooldown_started` are decided inside the same
        transaction that writes the record, and that transaction also adds them
        to the durable event ledger, so no crash between the two can lose or
        repeat a transition and no caller has to re-derive one from a later read.
        """
        crypter = normalize_crypter_key(crypter)
        package_id = _validate_package_id(package_id)
        link_fingerprint = validate_link_fingerprint(link_fingerprint)
        reason_code = _validate_reason_code(reason_code)
        now = int(self._clock())
        cooldown_seconds = self._cooldown_seconds()
        decision = {}
        invalid_record = {"found": False}

        def update_record(current_value):
            try:
                record = _decode_record(current_value)
            except ValueError:
                invalid_record["found"] = True
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

        database = self._shared_state.get_db("crypter_cooldowns")

        def record_and_count(current_values):
            record_value, ledger_value = current_values
            new_record = update_record(record_value)
            return new_record, _add_pending_crypter_events(
                ledger_value,
                observations=int(decision["recorded"]),
                cooldowns=int(decision["cooldown_started"]),
            )

        database.mutate_values(
            (("crypter_cooldowns", crypter), (CRYPTER_EVENT_TABLE, CRYPTER_EVENT_KEY)),
            record_and_count,
        )
        if invalid_record["found"]:
            self._warn_invalid_record(crypter)
        return decision

    def snapshot(self, crypter):
        crypter = normalize_crypter_key(crypter)
        now = int(self._clock())
        database = self._shared_state.get_db("crypter_cooldowns")
        current_value = database.retrieve(crypter)
        if current_value is None:
            return _available_snapshot()
        try:
            record = _decode_record(current_value)
        except ValueError:
            return self._cleanup_snapshot(database, crypter, now, invalid_observed=True)

        observation_count = len(record["observations"])
        record = self._prune_record(record, now)
        if record is None:
            return self._cleanup_snapshot(database, crypter, now)
        if len(record["observations"]) != observation_count:
            return self._cleanup_snapshot(database, crypter, now)

        result = dict(record)
        result["evidence_count"] = len(record["observations"])
        return result

    def is_cooling(self, crypter):
        return self.snapshot(crypter)["state"] == "cooldown"

    def retry_after(self, crypter):
        return self.snapshot(crypter)["retry_after_epoch"]

    def record_success(self, crypter):
        crypter = normalize_crypter_key(crypter)
        self._shared_state.get_db("crypter_cooldowns").mutate_value(
            crypter, lambda _current_value: None
        )

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
            package = _decode_package(current_value)
            if package is None:
                return current_value
            try:
                existing = decode_package_defer(package)
            except ValueError:
                invalid_defer["found"] = True
                existing = None

            previous_holds = existing["observation_holds"] if existing else 0
            previous_retry_after = existing["retry_after_epoch"] if existing else 0
            if observation_holds and previous_holds >= observation_holds:
                retry_after = previous_retry_after
            else:
                retry_after = max(previous_retry_after, retry_after_epoch)

            stored.update(
                {
                    "crypter": crypter,
                    "reason_code": reason_code,
                    "since_epoch": existing["since_epoch"] if existing else now,
                    "retry_after_epoch": retry_after,
                    "probe_requested": existing["probe_requested"]
                    if existing
                    else False,
                    "observation_holds": max(previous_holds, observation_holds),
                }
            )
            package[PACKAGE_DEFER_KEY] = dict(stored)
            return json.dumps(package)

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

    def project_package_defer(self, deferred, snapshot):
        """Merge stored package defer metadata with a live crypter snapshot."""
        now = int(self._clock())
        crypter_retry_after = (
            snapshot["retry_after_epoch"] if snapshot["state"] == "cooldown" else 0
        )
        retry_after_epoch = max(deferred["retry_after_epoch"], crypter_retry_after)
        if crypter_retry_after > now:
            hold_type = "crypter_cooldown"
        elif retry_after_epoch > now:
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
        snapshots = {}
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
            if crypter not in snapshots:
                snapshots[crypter] = self.snapshot(crypter)
            if self.project_package_defer(deferred, snapshots[crypter])["active"]:
                active += 1

        return active
