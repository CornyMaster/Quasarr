# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

"""Pure parsing and rendering of the Filecrypt cohort protocol.

Nothing here reads storage, settings, or a clock. A report is classified by
what it carries, not by what the helper claims, and that classification is
tri-state: a body with no cohort identity field at all is ordinary version-one
work, a body carrying the complete exact identity is a cohort report, and a
body that names cohort intent it cannot spell correctly is malformed. Malformed
is its own answer because falling back to the state-changing version-one route
would let a typo mutate a package hold the helper never meant to touch.
"""

import hashlib
import re

FILECRYPT_COHORT_CAPABILITY = "filecrypt_cohort_sweep_v1"
CRYPTER_DEFER_CAPABILITY = "crypter_defer_v1"
COHORT_CRYPTER = "filecrypt"
TERMINAL_OPERATION_DOMAIN = "sponsors-helper-terminal-v2"

COHORT_ACCESS_VALUES = frozenset({"clear", "unknown"})

VERSION_ONE_REPORT = "v1"
COHORT_REPORT = "cohort"
MALFORMED_REPORT = "malformed"

_IDENTIFIER_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_OFFER_IDENTITY_FIELDS = ("link_fingerprint", "sweep_id", "offer_id")
_COHORT_INTENT_FIELDS = ("sweep_id", "offer_id")


def _capabilities(payload):
    if not isinstance(payload, dict):
        return frozenset()
    advertised = payload.get("capabilities")
    if not isinstance(advertised, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(entry for entry in advertised if isinstance(entry, str))


def helper_supports_defer(payload):
    return CRYPTER_DEFER_CAPABILITY in _capabilities(payload)


def helper_supports_cohort(payload):
    """Cohort behavior needs both capabilities; the sweep one alone is not enough."""
    advertised = _capabilities(payload)
    return {CRYPTER_DEFER_CAPABILITY, FILECRYPT_COHORT_CAPABILITY} <= advertised


def terminal_operation_id(package_id):
    """The stable terminal operation identity of one package.

    Derived rather than stored so it survives a restart on either side and
    carries no URL or title. Task 6 owns the terminal endpoints; this is only
    the value they will later verify.
    """
    material = f"{TERMINAL_OPERATION_DOMAIN}\n{package_id}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def render_crypter_offer(offer, occurrence):
    """The handout block for one leased offer, or None when it names no link.

    An offer is only rendered together with the live occurrence it was mapped
    to, so the block can never advertise a member the handout does not contain.
    """
    if not offer or occurrence is None:
        return None
    if occurrence.fingerprint != offer["link_fingerprint"]:
        return None
    return {
        "capability": FILECRYPT_COHORT_CAPABILITY,
        "mode": offer["mode"],
        "crypter": COHORT_CRYPTER,
        "sweep_id": offer["sweep_id"],
        "offer_id": offer["offer_id"],
        "link_fingerprint": offer["link_fingerprint"],
        "deadline_epoch": offer["deadline_epoch"],
    }


def _offer_identity(payload):
    """The exact offer identity a cohort report must carry, or None.

    The linkcrypter name is compared literally: a cohort offer is minted against
    one exact spelling, so accepting a normalized variant here would make the
    identity the helper echoes back differ from the one it was handed.
    """
    if not isinstance(payload, dict):
        return None
    if not {"package_id", "crypter"}.issubset(payload):
        return None
    if payload["crypter"] != COHORT_CRYPTER:
        return None
    if not isinstance(payload["package_id"], str):
        return None
    identity = {}
    for field in _OFFER_IDENTITY_FIELDS:
        value = payload.get(field)
        pattern = (
            _FINGERPRINT_PATTERN if field == "link_fingerprint" else _IDENTIFIER_PATTERN
        )
        if not isinstance(value, str) or not pattern.fullmatch(value):
            return None
        identity[field] = value
    identity["package_id"] = payload["package_id"]
    identity["crypter"] = COHORT_CRYPTER
    return identity


def _classify(payload, complete):
    """Split one report body into version-one, cohort, or malformed intent."""
    if not isinstance(payload, dict):
        return VERSION_ONE_REPORT, None
    if not any(field in payload for field in _COHORT_INTENT_FIELDS):
        return VERSION_ONE_REPORT, None
    identity = _offer_identity(payload)
    if identity is None:
        return MALFORMED_REPORT, None
    report = complete(payload, identity)
    if report is None:
        return MALFORMED_REPORT, None
    return COHORT_REPORT, report


def _complete_blocked(payload, identity):
    reason_code = payload.get("reason_code")
    if not isinstance(reason_code, str):
        return None
    identity["reason_code"] = reason_code
    return identity


def _complete_access(payload, identity):
    access = payload.get("access")
    if access not in COHORT_ACCESS_VALUES:
        return None
    identity["access"] = access
    return identity


def classify_blocked_report(payload):
    """The intent of one BLOCKED body and its cohort report when it has one."""
    return _classify(payload, _complete_blocked)


def classify_access_report(payload):
    """The intent of one access body and its cohort report when it has one."""
    return _classify(payload, _complete_access)


def normalize_blocked_report(payload):
    """One strictly valid cohort BLOCKED report, or None for anything else."""
    return classify_blocked_report(payload)[1]


def normalize_access_report(payload):
    """One strictly valid cohort access report, or None for anything else."""
    return classify_access_report(payload)[1]


def render_defer_response(decision):
    """The cohort `/defer/` body: the legacy fields plus the sweep counters."""
    return {
        "success": True,
        "instruction": decision["instruction"],
        "state": decision["state"],
        "hold_type": decision["hold_type"],
        "evidence_count": decision["evidence_count"],
        "retry_after_epoch": decision["retry_after_epoch"],
        "sweep_id": decision["sweep_id"],
        "sweep_tested": decision["sweep_tested"],
        "sweep_total": decision["sweep_total"],
        "sweep_deadline_epoch": decision["sweep_deadline_epoch"],
    }


def render_access_response(decision, *, offer_id):
    """The cohort access body and its status code.

    `offer_id` is passed explicitly because the transition decision identifies a
    generation, not one lease. Only a CLEAR or an accepted UNKNOWN is an
    acknowledgement; every other outcome - a superseded lease, a wrong
    generation, a `fail`-mode bypass, or an offer this route cannot answer
    because it was already resolved as BLOCKED - is the same non-destructive
    `stale` classification under HTTP 409.
    """
    common = {
        "sweep_id": decision["sweep_id"],
        "offer_id": offer_id,
        "sweep_tested": decision["sweep_tested"],
        "sweep_total": decision["sweep_total"],
        "sweep_deadline_epoch": decision["sweep_deadline_epoch"],
    }
    if decision["cleared"]:
        return {
            "success": True,
            "state": decision["state"],
            "cleared": True,
            **common,
        }, 200
    if decision["accepted"] == "unknown":
        return {
            "success": True,
            "state": decision["state"],
            "accepted": "unknown",
            **common,
        }, 200
    return {
        "success": True,
        "instruction": "stale",
        "state": decision["state"],
        **common,
    }, 409
