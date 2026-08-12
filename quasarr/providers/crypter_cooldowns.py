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
                observation["seen_at_epoch"]
                for observation in record["observations"]
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
                observation["seen_at_epoch"]
                for observation in record["observations"]
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
                }
            )
            return _encode_record(record)

        database = self._shared_state.get_db("crypter_cooldowns")
        database.mutate_value(crypter, update_record)
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
            return self._cleanup_snapshot(
                database, crypter, now, invalid_observed=True
            )

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
