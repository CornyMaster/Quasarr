# -*- coding: utf-8 -*-
# Quasarr
"""Pure report normalization, response validation, and decision builders.

This module has no clock, storage, or config reads.  It imports only the
canonical pattern and the terminal_operation_id derivation.
"""

import re

from quasarr.constants import PACKAGE_ID_PATTERN
from quasarr.providers.terminal_operations import terminal_operation_id

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_FP_RE = re.compile(r"^[0-9a-f]{64}$")

LIFECYCLE_BLOCKED_REPORT_KEYS = frozenset(
    {
        "package_id",
        "crypter",
        "reason_code",
        "link_fingerprint",
        "sweep_id",
        "offer_id",
        "protocol_version",
        "terminal_operation_id",
    }
)
LIFECYCLE_ACCESS_REPORT_KEYS = frozenset(
    {
        "package_id",
        "crypter",
        "access",
        "link_fingerprint",
        "sweep_id",
        "offer_id",
        "protocol_version",
        "terminal_operation_id",
    }
)
RECEIPT_RETENTION_SECONDS = 30 * 24 * 60 * 60

_DEFER_RESPONSE_KEYS = frozenset(
    {
        "instruction",
        "state",
        "hold_type",
        "evidence_count",
        "retry_after_epoch",
        "sweep_id",
        "sweep_tested",
        "sweep_total",
        "sweep_deadline_epoch",
    }
)
_ACCESS_RESPONSE_KEYS = frozenset(
    {
        "state",
        "cleared",
        "accepted",
        "sweep_id",
        "sweep_tested",
        "sweep_total",
        "sweep_deadline_epoch",
    }
)
_DEFER_INSTRUCTIONS = frozenset({"hold", "cooldown"})
_DEFER_STATES = frozenset({"sweeping", "individual", "cooldown"})
_DEFER_HOLD_TYPES = frozenset({"provisional", "crypter_cooldown"})
_ACCESS_STATES = frozenset({"sweeping", "healthy", "individual", "cooldown"})
_ACCESS_ACCEPTED_VALUES = frozenset({"", "unknown"})


def normalize_lifecycle_blocked_report(report):
    """Validate and return a normalized blocked report dict, or raise ValueError."""
    if not isinstance(report, dict):
        raise ValueError("report must be a dict")
    if set(report.keys()) != LIFECYCLE_BLOCKED_REPORT_KEYS:
        raise ValueError("exact key set required")
    if report.get("crypter") != "filecrypt":
        raise ValueError("crypter must be 'filecrypt'")
    pv = report.get("protocol_version")
    if not (type(pv) is int and pv == 2):
        raise ValueError("protocol_version must be integer 2")
    pkg = report.get("package_id")
    if not isinstance(pkg, str) or not PACKAGE_ID_PATTERN.fullmatch(pkg):
        raise ValueError("invalid package_id")
    fp = report.get("link_fingerprint")
    if not isinstance(fp, str) or not _FP_RE.fullmatch(fp):
        raise ValueError("link_fingerprint must be 64 lowercase hex")
    sid = report.get("sweep_id")
    if not isinstance(sid, str) or not _ID_RE.fullmatch(sid):
        raise ValueError("sweep_id must be 32 lowercase hex")
    oid = report.get("offer_id")
    if not isinstance(oid, str) or not _ID_RE.fullmatch(oid):
        raise ValueError("offer_id must be 32 lowercase hex")
    top = report.get("terminal_operation_id")
    if not isinstance(top, str) or not _FP_RE.fullmatch(top):
        raise ValueError("terminal_operation_id must be 64 lowercase hex")
    if top != terminal_operation_id(pkg):
        raise ValueError("terminal_operation_id does not match package_id")
    rc = report.get("reason_code")
    if not isinstance(rc, str) or not rc:
        raise ValueError("reason_code must be a non-empty string")
    return dict(report)


def normalize_lifecycle_access_report(report):
    """Validate and return a normalized access report dict, or raise ValueError."""
    if not isinstance(report, dict):
        raise ValueError("report must be a dict")
    if set(report.keys()) != LIFECYCLE_ACCESS_REPORT_KEYS:
        raise ValueError("exact key set required")
    if report.get("crypter") != "filecrypt":
        raise ValueError("crypter must be 'filecrypt'")
    pv = report.get("protocol_version")
    if not (type(pv) is int and pv == 2):
        raise ValueError("protocol_version must be integer 2")
    pkg = report.get("package_id")
    if not isinstance(pkg, str) or not PACKAGE_ID_PATTERN.fullmatch(pkg):
        raise ValueError("invalid package_id")
    fp = report.get("link_fingerprint")
    if not isinstance(fp, str) or not _FP_RE.fullmatch(fp):
        raise ValueError("link_fingerprint must be 64 lowercase hex")
    sid = report.get("sweep_id")
    if not isinstance(sid, str) or not _ID_RE.fullmatch(sid):
        raise ValueError("sweep_id must be 32 lowercase hex")
    oid = report.get("offer_id")
    if not isinstance(oid, str) or not _ID_RE.fullmatch(oid):
        raise ValueError("offer_id must be 32 lowercase hex")
    top = report.get("terminal_operation_id")
    if not isinstance(top, str) or not _FP_RE.fullmatch(top):
        raise ValueError("terminal_operation_id must be 64 lowercase hex")
    if top != terminal_operation_id(pkg):
        raise ValueError("terminal_operation_id does not match package_id")
    access = report.get("access")
    if access not in ("clear", "unknown"):
        raise ValueError("access must be 'clear' or 'unknown'")
    return dict(report)


def validate_defer_response(response):
    """Raise ValueError if response does not match the exact defer shape."""
    if not isinstance(response, dict) or set(response.keys()) != _DEFER_RESPONSE_KEYS:
        raise ValueError("invalid defer response key set")
    if response["instruction"] not in _DEFER_INSTRUCTIONS:
        raise ValueError("invalid instruction")
    if response["state"] not in _DEFER_STATES:
        raise ValueError("invalid state")
    if response["hold_type"] not in _DEFER_HOLD_TYPES:
        raise ValueError("invalid hold_type")
    for field in (
        "evidence_count",
        "retry_after_epoch",
        "sweep_tested",
        "sweep_total",
        "sweep_deadline_epoch",
    ):
        v = response[field]
        if not (type(v) is int and v >= 0):
            raise ValueError(f"{field} must be a non-negative int")
    if response["sweep_tested"] > response["sweep_total"]:
        raise ValueError("sweep_tested must not exceed sweep_total")
    if not (
        type(response["sweep_deadline_epoch"]) is int
        and response["sweep_deadline_epoch"] > 0
    ):
        raise ValueError("sweep_deadline_epoch must be strictly positive")
    if not (
        type(response["retry_after_epoch"]) is int and response["retry_after_epoch"] > 0
    ):
        raise ValueError("retry_after_epoch must be strictly positive")
    # State-pairing coherence
    inst = response["instruction"]
    state = response["state"]
    hold = response["hold_type"]
    if inst == "hold" and state == "cooldown":
        raise ValueError("hold cannot pair with cooldown state")
    if inst == "cooldown" and state != "cooldown":
        raise ValueError("cooldown instruction requires cooldown state")
    if inst == "cooldown" and hold != "crypter_cooldown":
        raise ValueError("cooldown instruction requires crypter_cooldown hold_type")
    if state == "cooldown" and hold != "crypter_cooldown":
        raise ValueError("cooldown state requires crypter_cooldown hold_type")
    if state in ("sweeping", "individual") and hold == "crypter_cooldown":
        raise ValueError("sweeping/individual cannot pair with crypter_cooldown")
    sid = response["sweep_id"]
    if not isinstance(sid, str) or not _ID_RE.fullmatch(sid):
        raise ValueError("sweep_id must be 32 lowercase hex")


def validate_access_response(response):
    """Raise ValueError if response does not match the exact access shape."""
    if not isinstance(response, dict) or set(response.keys()) != _ACCESS_RESPONSE_KEYS:
        raise ValueError("invalid access response key set")
    if response["state"] not in _ACCESS_STATES:
        raise ValueError("invalid state")
    if not isinstance(response["cleared"], bool):
        raise ValueError("cleared must be bool")
    if response["accepted"] not in _ACCESS_ACCEPTED_VALUES:
        raise ValueError("accepted must be '' or 'unknown'")
    if response["cleared"] and response["accepted"] != "":
        raise ValueError("cleared=True requires accepted=''")
    if response["cleared"] and response["state"] != "healthy":
        raise ValueError("cleared=True requires state='healthy'")
    if not response["cleared"] and response["accepted"] != "unknown":
        raise ValueError("cleared=False requires accepted='unknown'")
    for field in ("sweep_tested", "sweep_total", "sweep_deadline_epoch"):
        v = response[field]
        if not (type(v) is int and v >= 0):
            raise ValueError(f"{field} must be a non-negative int")
    if response["sweep_tested"] > response["sweep_total"]:
        raise ValueError("sweep_tested must not exceed sweep_total")
    if not (
        type(response["sweep_deadline_epoch"]) is int
        and response["sweep_deadline_epoch"] > 0
    ):
        raise ValueError("sweep_deadline_epoch must be strictly positive")
    sid = response["sweep_id"]
    if not isinstance(sid, str) or not _ID_RE.fullmatch(sid):
        raise ValueError("sweep_id must be 32 lowercase hex")


def build_lifecycle_defer_decision(
    *,
    instruction,
    state,
    hold_type,
    evidence_count,
    retry_after_epoch,
    sweep_id,
    sweep_tested,
    sweep_total,
    sweep_deadline_epoch,
):
    """Build and validate a defer response dict."""
    resp = {
        "instruction": instruction,
        "state": state,
        "hold_type": hold_type,
        "evidence_count": evidence_count,
        "retry_after_epoch": retry_after_epoch,
        "sweep_id": sweep_id,
        "sweep_tested": sweep_tested,
        "sweep_total": sweep_total,
        "sweep_deadline_epoch": sweep_deadline_epoch,
    }
    validate_defer_response(resp)
    return resp


def build_lifecycle_access_decision(
    *,
    state,
    cleared,
    accepted,
    sweep_id,
    sweep_tested,
    sweep_total,
    sweep_deadline_epoch,
):
    """Build and validate an access response dict."""
    resp = {
        "state": state,
        "cleared": cleared,
        "accepted": accepted,
        "sweep_id": sweep_id,
        "sweep_tested": sweep_tested,
        "sweep_total": sweep_total,
        "sweep_deadline_epoch": sweep_deadline_epoch,
    }
    validate_access_response(resp)
    return resp


BLACKLIST_RESPONSE_KEYS = frozenset(
    {
        "instruction",
        "state",
        "hold_type",
        "evidence_count",
        "retry_after_epoch",
        "sweep_id",
        "sweep_tested",
        "sweep_total",
        "sweep_deadline_epoch",
    }
)


def validate_blacklist_response(response):
    """Raise ValueError if response does not match the exact blacklist shape."""
    if (
        not isinstance(response, dict)
        or set(response.keys()) != BLACKLIST_RESPONSE_KEYS
    ):
        raise ValueError("invalid blacklist response key set")
    if response["instruction"] != "blacklist":
        raise ValueError("instruction must be 'blacklist'")
    if response["state"] != "individual":
        raise ValueError("state must be 'individual'")
    if response["hold_type"] != "none":
        raise ValueError("hold_type must be 'none'")
    for field in ("evidence_count", "retry_after_epoch", "sweep_tested", "sweep_total"):
        v = response[field]
        if not (type(v) is int and v == 0):
            raise ValueError(f"{field} must be exactly 0")
    v = response["sweep_deadline_epoch"]
    if not (type(v) is int and v > 0):
        raise ValueError("sweep_deadline_epoch must be strictly positive")
    sid = response["sweep_id"]
    if not isinstance(sid, str) or not _ID_RE.fullmatch(sid):
        raise ValueError("sweep_id must be 32 lowercase hex")


def build_blacklist_decision(*, sweep_id, sweep_deadline_epoch):
    """Build and validate a blacklist response dict."""
    resp = {
        "instruction": "blacklist",
        "state": "individual",
        "hold_type": "none",
        "evidence_count": 0,
        "retry_after_epoch": 0,
        "sweep_id": sweep_id,
        "sweep_tested": 0,
        "sweep_total": 0,
        "sweep_deadline_epoch": sweep_deadline_epoch,
    }
    validate_blacklist_response(resp)
    return resp
