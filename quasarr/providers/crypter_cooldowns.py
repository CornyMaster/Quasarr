# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import json
import re
import time

from quasarr.constants import PACKAGE_ID_PATTERN
from quasarr.downloads import protected_crypter_keys

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
    if value not in SUPPORTED_REASON_CODES:
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
    if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
        raise ValueError("Invalid persisted linkcrypter record")
    if record["state"] not in {"observing", "cooldown"}:
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
    for observation in record["observations"]:
        if not isinstance(observation, dict) or set(observation) != _OBSERVATION_KEYS:
            raise ValueError("Invalid persisted linkcrypter observation")
        _validate_package_id(observation["package_id"])
        validate_link_fingerprint(observation["link_fingerprint"])
        _validate_epoch(observation["seen_at_epoch"], "seen_at_epoch")
    return record


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

    def observe(self, crypter, package_id, link_fingerprint, reason_code):
        crypter = normalize_crypter_key(crypter)
        package_id = _validate_package_id(package_id)
        link_fingerprint = validate_link_fingerprint(link_fingerprint)
        reason_code = _validate_reason_code(reason_code)
        now = int(self._clock())
        cooldown_seconds = self._cooldown_seconds()
        decision = {}

        def update_record(current_value):
            record = self._prune_record(_decode_record(current_value), now)
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
            else:
                duplicate["seen_at_epoch"] = now

            seen_at = [
                observation["seen_at_epoch"]
                for observation in record["observations"]
            ]
            record["first_seen_epoch"] = min(seen_at)
            record["last_seen_epoch"] = now
            evidence_count = len(record["observations"])
            existing_retry_after = record["retry_after_epoch"]
            if record["state"] == "cooldown" or evidence_count >= EVIDENCE_THRESHOLD:
                record["state"] = "cooldown"
                record["retry_after_epoch"] = max(
                    existing_retry_after, now + cooldown_seconds
                )
                package_retry_after = record["retry_after_epoch"]
            else:
                record["state"] = "observing"
                record["retry_after_epoch"] = 0
                package_retry_after = now + OBSERVATION_WINDOW_SECONDS

            decision.update(
                {
                    "state": record["state"],
                    "evidence_count": evidence_count,
                    "package_retry_after_epoch": package_retry_after,
                }
            )
            return _encode_record(record)

        self._shared_state.get_db("crypter_cooldowns").mutate_value(
            crypter, update_record
        )
        return decision

    def snapshot(self, crypter):
        crypter = normalize_crypter_key(crypter)
        now = int(self._clock())
        result = {}

        def read_record(current_value):
            record = self._prune_record(_decode_record(current_value), now)
            if record is None:
                result.update(_available_snapshot())
                return None
            result.update(record)
            result["evidence_count"] = len(record["observations"])
            return _encode_record(record)

        self._shared_state.get_db("crypter_cooldowns").mutate_value(
            crypter, read_record
        )
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

    def request_probe(self, package_ids):
        if not isinstance(package_ids, (list, tuple, set, frozenset)):
            raise ValueError("package_ids must be a collection")

        requested = []
        rejected = []
        seen = set()
        database = self._shared_state.get_db("protected")
        for package_id in package_ids:
            try:
                package_id = _validate_package_id(package_id)
            except ValueError:
                rejected.append({"package_id": package_id, "reason": "invalid_id"})
                continue
            if package_id in seen:
                continue
            seen.add(package_id)
            outcome = {"requested": False, "reason": "unknown_package"}

            def request(current_value, request_outcome=outcome):
                if current_value is None:
                    return None
                try:
                    package = json.loads(current_value)
                except (TypeError, json.JSONDecodeError):
                    request_outcome["reason"] = "invalid_package"
                    return current_value
                deferred = package.get("deferred") if isinstance(package, dict) else None
                if not isinstance(deferred, dict):
                    request_outcome["reason"] = "not_deferred"
                    return current_value
                try:
                    normalize_crypter_key(deferred.get("crypter"))
                except ValueError:
                    request_outcome["reason"] = "invalid_deferred_crypter"
                    return current_value
                deferred["probe_requested"] = True
                request_outcome["requested"] = True
                return json.dumps(package, separators=(",", ":"), sort_keys=True)

            database.mutate_value(package_id, request)
            if outcome["requested"]:
                requested.append(package_id)
            else:
                rejected.append(
                    {"package_id": package_id, "reason": outcome["reason"]}
                )
        return {"requested": requested, "rejected": rejected}

    def consume_probe(self, package_id, crypter):
        package_id = _validate_package_id(package_id)
        crypter = normalize_crypter_key(crypter)
        consumed = {"value": False}

        def consume(current_value):
            if current_value is None:
                return None
            try:
                package = json.loads(current_value)
            except (TypeError, json.JSONDecodeError):
                return current_value
            deferred = package.get("deferred") if isinstance(package, dict) else None
            if not isinstance(deferred, dict):
                return current_value
            try:
                deferred_crypter = normalize_crypter_key(deferred.get("crypter"))
            except ValueError:
                return current_value
            if deferred_crypter != crypter or deferred.get("probe_requested") is not True:
                return current_value
            deferred["probe_requested"] = False
            consumed["value"] = True
            return json.dumps(package, separators=(",", ":"), sort_keys=True)

        self._shared_state.get_db("protected").mutate_value(package_id, consume)
        return consumed["value"]
