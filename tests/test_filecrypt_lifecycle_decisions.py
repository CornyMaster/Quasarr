# -*- coding: utf-8 -*-
"""Tests for filecrypt_lifecycle_decisions: report normalization and response builders."""

import json
import unittest

from quasarr.providers.filecrypt_lifecycle_decisions import (
    LIFECYCLE_ACCESS_REPORT_KEYS,
    LIFECYCLE_BLOCKED_REPORT_KEYS,
    RECEIPT_RETENTION_SECONDS,
    build_lifecycle_access_decision,
    build_lifecycle_defer_decision,
    normalize_lifecycle_access_report,
    normalize_lifecycle_blocked_report,
    validate_access_response,
    validate_defer_response,
)
from quasarr.providers.terminal_operations import terminal_operation_id

PKG = "Quasarr_movies_" + "a" * 32
FP = "b" * 64
SWEEP_ID = "c" * 32
OFFER_ID = "d" * 32
TOP = terminal_operation_id(PKG)


def _blocked_report(**overrides):
    base = {
        "package_id": PKG,
        "crypter": "filecrypt",
        "reason_code": "ip_block_suspected",
        "link_fingerprint": FP,
        "sweep_id": SWEEP_ID,
        "offer_id": OFFER_ID,
        "protocol_version": 2,
        "terminal_operation_id": TOP,
    }
    base.update(overrides)
    return base


def _access_report(**overrides):
    base = {
        "package_id": PKG,
        "crypter": "filecrypt",
        "access": "clear",
        "link_fingerprint": FP,
        "sweep_id": SWEEP_ID,
        "offer_id": OFFER_ID,
        "protocol_version": 2,
        "terminal_operation_id": TOP,
    }
    base.update(overrides)
    return base


class TestBlockedReportNormalization(unittest.TestCase):
    """RED: exact blocked report key sets; malformed IDs rejected."""

    def test_valid_report_accepted(self):
        result = normalize_lifecycle_blocked_report(_blocked_report())
        self.assertEqual(set(result.keys()), LIFECYCLE_BLOCKED_REPORT_KEYS)

    def test_extra_key_rejected(self):
        with self.assertRaises(ValueError):
            normalize_lifecycle_blocked_report(_blocked_report(extra="x"))

    def test_missing_key_rejected(self):
        r = _blocked_report()
        del r["reason_code"]
        with self.assertRaises(ValueError):
            normalize_lifecycle_blocked_report(r)

    def test_wrong_crypter_rejected(self):
        with self.assertRaises(ValueError):
            normalize_lifecycle_blocked_report(_blocked_report(crypter="other"))

    def test_bool_protocol_version_rejected(self):
        with self.assertRaises(ValueError):
            normalize_lifecycle_blocked_report(_blocked_report(protocol_version=True))

    def test_protocol_version_3_rejected(self):
        with self.assertRaises(ValueError):
            normalize_lifecycle_blocked_report(_blocked_report(protocol_version=3))

    def test_bad_package_id_rejected(self):
        with self.assertRaises(ValueError):
            normalize_lifecycle_blocked_report(_blocked_report(package_id="bad"))

    def test_bad_fingerprint_rejected(self):
        with self.assertRaises(ValueError):
            normalize_lifecycle_blocked_report(
                _blocked_report(link_fingerprint="ABCD" * 16)
            )

    def test_bad_sweep_id_rejected(self):
        with self.assertRaises(ValueError):
            normalize_lifecycle_blocked_report(_blocked_report(sweep_id="x" * 32))

    def test_bad_offer_id_rejected(self):
        with self.assertRaises(ValueError):
            normalize_lifecycle_blocked_report(_blocked_report(offer_id="short"))

    def test_wrong_terminal_operation_id_rejected(self):
        with self.assertRaises(ValueError):
            normalize_lifecycle_blocked_report(
                _blocked_report(terminal_operation_id="e" * 64)
            )

    def test_empty_reason_code_rejected(self):
        with self.assertRaises(ValueError):
            normalize_lifecycle_blocked_report(_blocked_report(reason_code=""))

    def test_non_dict_rejected(self):
        with self.assertRaises(ValueError):
            normalize_lifecycle_blocked_report("not a dict")


class TestAccessReportNormalization(unittest.TestCase):
    """RED: exact access report key sets; bad access values rejected."""

    def test_valid_clear_accepted(self):
        result = normalize_lifecycle_access_report(_access_report())
        self.assertEqual(set(result.keys()), LIFECYCLE_ACCESS_REPORT_KEYS)

    def test_valid_unknown_accepted(self):
        result = normalize_lifecycle_access_report(_access_report(access="unknown"))
        self.assertEqual(result["access"], "unknown")

    def test_blocked_access_rejected(self):
        with self.assertRaises(ValueError):
            normalize_lifecycle_access_report(_access_report(access="blocked"))

    def test_extra_key_rejected(self):
        with self.assertRaises(ValueError):
            normalize_lifecycle_access_report(_access_report(mode="sweep"))

    def test_missing_key_rejected(self):
        r = _access_report()
        del r["access"]
        with self.assertRaises(ValueError):
            normalize_lifecycle_access_report(r)

    def test_bool_protocol_version_rejected(self):
        with self.assertRaises(ValueError):
            normalize_lifecycle_access_report(_access_report(protocol_version=True))

    def test_wrong_terminal_operation_id_rejected(self):
        with self.assertRaises(ValueError):
            normalize_lifecycle_access_report(
                _access_report(terminal_operation_id="f" * 64)
            )


class TestDeferResponseValidation(unittest.TestCase):
    """RED: every defer response key set/type matrix enforced."""

    def _valid(self):
        return {
            "instruction": "hold",
            "state": "sweeping",
            "hold_type": "provisional",
            "evidence_count": 1,
            "retry_after_epoch": 1000,
            "sweep_id": "a" * 32,
            "sweep_tested": 1,
            "sweep_total": 5,
            "sweep_deadline_epoch": 2000,
        }

    def test_valid_hold_sweeping(self):
        validate_defer_response(self._valid())

    def test_valid_cooldown(self):
        r = self._valid()
        r["instruction"] = "cooldown"
        r["state"] = "cooldown"
        r["hold_type"] = "crypter_cooldown"
        validate_defer_response(r)

    def test_bad_instruction_rejected(self):
        r = self._valid()
        r["instruction"] = "reject"
        with self.assertRaises(ValueError):
            validate_defer_response(r)

    def test_negative_evidence_rejected(self):
        r = self._valid()
        r["evidence_count"] = -1
        with self.assertRaises(ValueError):
            validate_defer_response(r)

    def test_extra_key_rejected(self):
        r = self._valid()
        r["extra"] = 1
        with self.assertRaises(ValueError):
            validate_defer_response(r)

    def test_missing_key_rejected(self):
        r = self._valid()
        del r["sweep_id"]
        with self.assertRaises(ValueError):
            validate_defer_response(r)

    def test_bool_evidence_rejected(self):
        r = self._valid()
        r["evidence_count"] = True
        with self.assertRaises(ValueError):
            validate_defer_response(r)


class TestAccessResponseValidation(unittest.TestCase):
    """RED: every access response key set/type matrix enforced."""

    def _valid(self):
        return {
            "state": "healthy",
            "cleared": True,
            "accepted": "",
            "sweep_id": "a" * 32,
            "sweep_tested": 0,
            "sweep_total": 0,
            "sweep_deadline_epoch": 1000,
        }

    def test_valid_healthy_clear(self):
        validate_access_response(self._valid())

    def test_valid_unknown(self):
        r = self._valid()
        r["cleared"] = False
        r["accepted"] = "unknown"
        r["state"] = "sweeping"
        validate_access_response(r)

    def test_bad_accepted_rejected(self):
        r = self._valid()
        r["accepted"] = "blocked"
        with self.assertRaises(ValueError):
            validate_access_response(r)

    def test_non_bool_cleared_rejected(self):
        r = self._valid()
        r["cleared"] = 1
        with self.assertRaises(ValueError):
            validate_access_response(r)

    def test_extra_key_rejected(self):
        r = self._valid()
        r["extra"] = True
        with self.assertRaises(ValueError):
            validate_access_response(r)


class TestBuildDeferDecision(unittest.TestCase):
    """RED: builder produces validated response."""

    def test_builds_valid(self):
        resp = build_lifecycle_defer_decision(
            instruction="hold",
            state="individual",
            hold_type="provisional",
            evidence_count=0,
            retry_after_epoch=5000,
            sweep_id="a" * 32,
            sweep_tested=0,
            sweep_total=0,
            sweep_deadline_epoch=5000,
        )
        self.assertEqual(resp["instruction"], "hold")

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            build_lifecycle_defer_decision(
                instruction="bad",
                state="individual",
                hold_type="provisional",
                evidence_count=0,
                retry_after_epoch=5000,
                sweep_id="a" * 32,
                sweep_tested=0,
                sweep_total=0,
                sweep_deadline_epoch=5000,
            )


class TestBuildAccessDecision(unittest.TestCase):
    """RED: builder produces validated response."""

    def test_builds_valid(self):
        resp = build_lifecycle_access_decision(
            state="healthy",
            cleared=True,
            accepted="",
            sweep_id="a" * 32,
            sweep_tested=0,
            sweep_total=0,
            sweep_deadline_epoch=1000,
        )
        self.assertEqual(resp["cleared"], True)

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            build_lifecycle_access_decision(
                state="bad",
                cleared=True,
                accepted="",
                sweep_id="a" * 32,
                sweep_tested=0,
                sweep_total=0,
                sweep_deadline_epoch=1000,
            )


class TestReceiptPrivacy(unittest.TestCase):
    """RED: serialized receipt must not contain URL/title/reason text."""

    def test_defer_response_no_url_or_title(self):
        resp = build_lifecycle_defer_decision(
            instruction="hold",
            state="sweeping",
            hold_type="provisional",
            evidence_count=1,
            retry_after_epoch=5000,
            sweep_id="a" * 32,
            sweep_tested=1,
            sweep_total=5,
            sweep_deadline_epoch=2000,
        )
        serialized = json.dumps(resp)
        self.assertNotIn("http", serialized)
        self.assertNotIn("title", serialized)
        self.assertNotIn("reason", serialized)
        self.assertNotIn("error", serialized)

    def test_access_response_no_url_or_title(self):
        resp = build_lifecycle_access_decision(
            state="healthy",
            cleared=True,
            accepted="",
            sweep_id="a" * 32,
            sweep_tested=0,
            sweep_total=0,
            sweep_deadline_epoch=1000,
        )
        serialized = json.dumps(resp)
        self.assertNotIn("http", serialized)
        self.assertNotIn("title", serialized)
        self.assertNotIn("error", serialized)


class TestReceiptRetention(unittest.TestCase):
    """Retention constant is 30 days."""

    def test_retention_seconds(self):
        self.assertEqual(RECEIPT_RETENTION_SECONDS, 30 * 24 * 60 * 60)


if __name__ == "__main__":
    unittest.main()
