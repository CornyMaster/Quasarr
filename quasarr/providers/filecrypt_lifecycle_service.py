# -*- coding: utf-8 -*-
# Quasarr
"""Filecrypt link-lifecycle service: opening, leasing, projections, and outcomes.

Implements opening/leasing/projection (Task 3A), first-time outcome recording
(Task 3B1), and retest/probe outcome recording (Task 3B2A).  Blacklist
confirmation, pruning, migration, route wiring, settings persistence, and
terminal effects are deferred to later tasks.
"""

import json
import re
import secrets
import time

from quasarr.providers import log
from quasarr.providers.crypter_candidates import (
    FilecryptCandidate,
    classify_package_ownership,
    enumerate_filecrypt_lifecycle_candidates,
)
from quasarr.providers.crypter_cooldowns import (
    CRYPTER_EVENT_KEY,
    CRYPTER_EVENT_TABLE,
    MINIMUM_COOLDOWN_HOURS,
    PACKAGE_DEFER_KEY,
    _add_pending_crypter_events,
    decode_package_defer,
    package_defer_covers_fingerprint,
)
from quasarr.providers.crypter_sweeps import OWNERSHIP_OWNED
from quasarr.providers.filecrypt_lifecycle import (
    FILECRYPT_LINK_STATES_TABLE,
    FILECRYPT_OFFER_RECEIPTS_TABLE,
    FILECRYPT_SWEEP_KEY,
    FILECRYPT_SWEEP_MEMBERS_TABLE,
    FILECRYPT_SWEEP_STATE_TABLE,
    MINIMUM_GLOBAL_COOLDOWN_SIZE,
    MINIMUM_SWEEP_SIZE,
    decode_link_state,
    decode_offer_receipt,
    decode_sweep_header,
    decode_sweep_member,
    encode_link_state,
    encode_offer_receipt,
    encode_sweep_header,
    encode_sweep_member,
)
from quasarr.providers.filecrypt_lifecycle_decisions import (
    RECEIPT_RETENTION_SECONDS,
    build_blacklist_decision,
    build_lifecycle_access_decision,
    build_lifecycle_defer_decision,
    normalize_lifecycle_access_report,
    normalize_lifecycle_blocked_report,
    validate_access_response,
    validate_blacklist_response,
    validate_defer_response,
)
from quasarr.providers.terminal_operations import (
    terminal_operation_id as _canonical_top_id,
)

FILECRYPT_LINK_LIFECYCLE_CAPABILITY = "filecrypt_link_lifecycle_v1"
FILECRYPT_CRYPTER = "filecrypt"
DEFAULT_SWEEP_WINDOW_MINUTES = 15
OFFER_LEASE_SECONDS = 120
RECEIPT_ADVISORY_THRESHOLD = 4096

_SCHEMA_VERSION = 1
_WINDOW_MIN = 1
_WINDOW_MAX = 1440
_FP_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _first_handable_occurrence(candidate, excluded_package_ids):
    """Return first occurrence whose package_id is not excluded, else None."""
    excluded = frozenset(excluded_package_ids)
    for occ in candidate.occurrences:
        if occ.package_id not in excluded:
            return occ
    return None


def _by_fp(candidates):
    return {c.fingerprint: c for c in candidates}


class FilecryptLifecycleService:
    def __init__(self, shared_state, clock=time.time, identifier_factory=None):
        self._shared_state = shared_state
        self._clock = clock
        self._ids = (
            identifier_factory
            if identifier_factory is not None
            else (lambda: secrets.token_hex(16))
        )

    # ── migration ─────────────────────────────────────────────────────────────

    def migrate_legacy(self, protected_rows=None):
        """Atomically migrate proven legacy Filecrypt state into lifecycle rows."""
        from quasarr.providers.filecrypt_lifecycle_migration import (
            build_targets_and_mutator,
            prepare_migration,
        )

        now = int(self._clock())
        generation_id = self._ids()

        pre_reads, plan = prepare_migration(
            self._shared_state, now, generation_id, protected_rows
        )
        if pre_reads is None:
            return plan

        targets, mutator, result_ref = build_targets_and_mutator(pre_reads, plan, now)

        self._shared_state.get_db(FILECRYPT_SWEEP_STATE_TABLE).mutate_values(
            targets, mutator
        )
        return result_ref[0]

    # ── settings ──────────────────────────────────────────────────────────────

    def _sweep_window_seconds(self):
        raw = self._shared_state.values.get(
            "filecrypt_sweep_window_minutes", DEFAULT_SWEEP_WINDOW_MINUTES
        )
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_SWEEP_WINDOW_MINUTES * 60
        if not (_WINDOW_MIN <= v <= _WINDOW_MAX):
            return DEFAULT_SWEEP_WINDOW_MINUTES * 60
        return v * 60

    # ── read projections ───────────────────────────────────────────────────────

    def decision(self):
        """Project the sweep-state header."""
        db = self._shared_state.get_db(FILECRYPT_SWEEP_STATE_TABLE)
        raw = db.retrieve(FILECRYPT_SWEEP_KEY)
        if raw is None:
            return {"state": "available", "retry_after_epoch": 0}
        record = decode_sweep_header(raw)
        if record is None:
            return {"state": "unavailable", "retry_after_epoch": 0}
        now = int(self._clock())
        state = record["state"]
        if state == "sweeping":
            if now < record["deadline_epoch"]:
                return {"state": "sweeping", "retry_after_epoch": 0}
            return {"state": "available", "retry_after_epoch": 0}
        if state == "healthy":
            if now < record["until_epoch"]:
                return {"state": "healthy", "retry_after_epoch": record["until_epoch"]}
            return {"state": "available", "retry_after_epoch": 0}
        if state == "cooldown":
            if now < record["retry_after_epoch"]:
                return {
                    "state": "cooldown",
                    "retry_after_epoch": record["retry_after_epoch"],
                }
            return {"state": "available", "retry_after_epoch": 0}
        return {"state": "unavailable", "retry_after_epoch": 0}

    def project_link(self, fingerprint):
        """Project a stored link-state row."""
        db = self._shared_state.get_db(FILECRYPT_LINK_STATES_TABLE)
        raw = db.retrieve(fingerprint)
        if raw is None:
            return {"state": "available", "retry_after_epoch": 0}
        record = decode_link_state(raw)
        if record is None:
            return {"state": "unavailable", "retry_after_epoch": 0}
        state = record["state"]
        if state == "held":
            retry_after = record["retry_after_epoch"]
            now = int(self._clock())
            if now < retry_after:
                return {"state": "held", "retry_after_epoch": retry_after}
            return {"state": "retest", "retry_after_epoch": retry_after}
        if state == "blacklisting":
            return {"state": "blacklisting", "retry_after_epoch": 0}
        if state == "blacklisted":
            return {"state": "blacklisted", "retry_after_epoch": 0}
        return {"state": "unavailable", "retry_after_epoch": 0}

    def active_blacklisted_fingerprints(self):
        """Ascending tuple of fingerprints in final blacklisted state only."""
        db = self._shared_state.get_db(FILECRYPT_LINK_STATES_TABLE)
        all_rows = db.retrieve_all_titles()
        if not all_rows:
            return ()
        result = []
        for fp, raw in all_rows:
            record = decode_link_state(raw)
            if record is not None and record.get("state") == "blacklisted":
                result.append(fp)
        result.sort()
        return tuple(result)

    # ── offer preparation ──────────────────────────────────────────────────────

    def prepare_offer(
        self,
        protected_rows,
        *,
        excluded_package_ids=(),
        preferred_fingerprint=None,
        probe_package_id=None,
    ):
        """Build and atomically commit an offer, or None if unavailable."""
        now = int(self._clock())

        # Enumerate and deterministically sort inventory
        inventory = enumerate_filecrypt_lifecycle_candidates(protected_rows)
        candidates = sorted(inventory.candidates, key=lambda c: c.fingerprint)
        candidates = [
            FilecryptCandidate(
                fingerprint=c.fingerprint,
                occurrences=tuple(
                    sorted(c.occurrences, key=lambda o: (o.package_id, o.link_index))
                ),
            )
            for c in candidates
        ]
        fp_map = _by_fp(candidates)

        # Pre-mutation reads (outside lock; callback re-validates inside)
        header_raw = self._shared_state.get_db(FILECRYPT_SWEEP_STATE_TABLE).retrieve(
            FILECRYPT_SWEEP_KEY
        )
        header = decode_sweep_header(header_raw) if header_raw is not None else None

        existing_member_rows = (
            self._shared_state.get_db(
                FILECRYPT_SWEEP_MEMBERS_TABLE
            ).retrieve_all_titles()
            or []
        )
        existing_member_by_fp = {row[0]: row[1] for row in existing_member_rows}
        existing_member_fps = set(existing_member_by_fp)

        inv_fps = [c.fingerprint for c in candidates]
        ls_db = self._shared_state.get_db(FILECRYPT_LINK_STATES_TABLE)
        link_state_by_fp = {fp: ls_db.retrieve(fp) for fp in inv_fps}

        # Classify each inventory fingerprint
        active_hold_fps: set = set()
        retest_fps: list = []
        first_time_fps: list = []

        for fp in inv_fps:
            ls_raw = link_state_by_fp[fp]
            if ls_raw is None:
                first_time_fps.append(fp)
                continue
            ls = decode_link_state(ls_raw)
            if ls is None:
                pass  # malformed → excluded
            elif ls["state"] == "held":
                if now < ls["retry_after_epoch"]:
                    active_hold_fps.add(fp)
                else:
                    retest_fps.append(fp)
            # blacklisting / blacklisted → excluded from first-time and retest

        # Fail-closed on malformed persisted header
        if header_raw is not None and header is None:
            return None

        # Determine live header state
        header_state = None
        if header is not None:
            s = header["state"]
            if s == "sweeping" and now < header["deadline_epoch"]:
                header_state = "sweeping"
            elif s == "healthy" and now < header["until_epoch"]:
                header_state = "healthy"
            elif s == "cooldown" and now < header["retry_after_epoch"]:
                header_state = "cooldown"

        # Dispatch
        if header_state == "cooldown":
            return self._try_probe(
                header,
                fp_map,
                link_state_by_fp,
                preferred_fingerprint,
                excluded_package_ids,
                now,
                probe_package_id=probe_package_id,
            )

        if header_state == "sweeping":
            ret = self._try_retest(retest_fps, fp_map, excluded_package_ids, now)
            if ret is not None:
                return ret
            return self._try_sweep_lease(
                header, existing_member_by_fp, fp_map, excluded_package_ids, now
            )

        if header_state == "healthy":
            # Retest is allowed even under healthy suppression; new first-time is not
            return self._try_retest(retest_fps, fp_map, excluded_package_ids, now)

        # Available (no live header or all headers expired)
        if retest_fps:
            ret = self._try_retest(retest_fps, fp_map, excluded_package_ids, now)
            if ret is not None:
                return ret

        if not first_time_fps:
            return None

        if len(first_time_fps) < MINIMUM_SWEEP_SIZE:
            return self._try_individual(
                first_time_fps[0],
                fp_map,
                link_state_by_fp,
                existing_member_fps,
                excluded_package_ids,
                now,
            )

        return self._try_sweep_open(
            first_time_fps,
            fp_map,
            link_state_by_fp,
            existing_member_fps,
            excluded_package_ids,
            now,
        )

    # ── atomic sub-operations ─────────────────────────────────────────────────

    def _try_probe(
        self,
        header,
        fp_map,
        link_state_by_fp,
        preferred_fingerprint,
        excluded_package_ids,
        now,
        *,
        probe_package_id=None,
    ):
        """Issue a probe offer under a live cooldown, or None.

        When probe_package_id is supplied, the protected package row is included
        as an atomic target and its deferred metadata is removed on success.
        """
        if preferred_fingerprint is None or preferred_fingerprint not in fp_map:
            return None
        ls_raw = link_state_by_fp.get(preferred_fingerprint)
        if ls_raw is None:
            return None
        ls = decode_link_state(ls_raw)
        if ls is None or ls["state"] != "held" or now >= ls["retry_after_epoch"]:
            return None
        if probe_package_id is not None:
            # Bind to exact owner: lowest link_index in the probe's package
            excluded = frozenset(excluded_package_ids)
            if probe_package_id in excluded:
                return None
            occurrence = None
            for occ in fp_map[preferred_fingerprint].occurrences:
                if occ.package_id == probe_package_id:
                    occurrence = occ
                    break
        else:
            occurrence = _first_handable_occurrence(
                fp_map[preferred_fingerprint], excluded_package_ids
            )
        if occurrence is None:
            return None

        _now = now
        _fp = preferred_fingerprint
        _occ = occurrence
        _gen_id = header["generation_id"]
        _pkg_id = probe_package_id
        result = [None]
        ids = self._ids

        # Pre-read the protected package row if we will consume the marker
        pkg_raw = None
        if _pkg_id is not None:
            pkg_raw = self._shared_state.get_db("protected").retrieve(_pkg_id)
            if pkg_raw is None:
                return None

        def mutator(values):
            if _pkg_id is not None:
                hdr_raw, ls_curr_raw, pkg_curr_raw = values
            else:
                hdr_raw, ls_curr_raw = values
                pkg_curr_raw = None

            hdr = decode_sweep_header(hdr_raw)
            if (
                hdr is None
                or hdr.get("state") != "cooldown"
                or _now >= hdr.get("retry_after_epoch", 0)
            ):
                result[0] = None
                return values
            ls_curr = decode_link_state(ls_curr_raw)
            if (
                ls_curr is None
                or ls_curr.get("state") != "held"
                or _now >= ls_curr.get("retry_after_epoch", 0)
            ):
                result[0] = None
                return values

            # Validate and consume the protected package deferred marker
            new_pkg_raw = pkg_curr_raw
            if _pkg_id is not None:
                try:
                    pkg_data = json.loads(pkg_curr_raw) if pkg_curr_raw else None
                except (TypeError, ValueError):
                    pkg_data = None
                if not isinstance(pkg_data, dict):
                    result[0] = None
                    return values
                try:
                    deferred = decode_package_defer(pkg_data)
                except (TypeError, ValueError):
                    result[0] = None
                    return values
                if (
                    deferred is None
                    or deferred["crypter"] != FILECRYPT_CRYPTER
                    or not deferred["probe_requested"]
                    or not package_defer_covers_fingerprint(deferred, _fp)
                ):
                    result[0] = None
                    return values
                # Remove only the deferred key; preserve all other package data
                cleaned = {k: v for k, v in pkg_data.items() if k != PACKAGE_DEFER_KEY}
                new_pkg_raw = json.dumps(cleaned, separators=(",", ":"))

            offer_id = ids()
            existing_lease = ls_curr.get("lease") or {}
            sweep_id = existing_lease.get("sweep_id") or _gen_id
            new_ls = dict(ls_curr)
            new_ls["lease"] = {
                "sweep_id": sweep_id,
                "offer_id": offer_id,
                "package_id": _occ.package_id,
                "offer_expires_epoch": _now + OFFER_LEASE_SECONDS,
            }
            result[0] = {
                "capability": FILECRYPT_LINK_LIFECYCLE_CAPABILITY,
                "mode": "probe",
                "crypter": FILECRYPT_CRYPTER,
                "sweep_id": sweep_id,
                "offer_id": offer_id,
                "link_fingerprint": _fp,
                "deadline_epoch": _now + OFFER_LEASE_SECONDS,
                "occurrence": _occ,
            }
            encoded_ls = encode_link_state(new_ls)
            if _pkg_id is not None:
                return (hdr_raw, encoded_ls, new_pkg_raw)
            return (hdr_raw, encoded_ls)

        targets = [
            (FILECRYPT_SWEEP_STATE_TABLE, FILECRYPT_SWEEP_KEY),
            (FILECRYPT_LINK_STATES_TABLE, _fp),
        ]
        if _pkg_id is not None:
            targets.append(("protected", _pkg_id))
        self._shared_state.get_db(FILECRYPT_SWEEP_STATE_TABLE).mutate_values(
            targets, mutator
        )
        return result[0]

    def _try_retest(self, retest_fps, fp_map, excluded_package_ids, now):
        """Issue a retest offer for the first eligible expired hold, or None."""
        for fp in retest_fps:
            if fp not in fp_map:
                continue
            occurrence = _first_handable_occurrence(fp_map[fp], excluded_package_ids)
            if occurrence is None:
                continue

            _now = now
            _fp = fp
            _occ = occurrence
            result = [None]
            ids = self._ids

            def mutator(
                values, *, __fp=_fp, __occ=_occ, __now=_now, _result=result, _ids=ids
            ):
                (ls_raw,) = values
                ls = decode_link_state(ls_raw)
                if ls is None or ls.get("state") != "held":
                    _result[0] = None
                    return (ls_raw,)
                if __now < ls.get("retry_after_epoch", 0):
                    _result[0] = None
                    return (ls_raw,)
                sweep_id = _ids()
                offer_id = _ids()
                new_ls = dict(ls)
                new_ls["lease"] = {
                    "sweep_id": sweep_id,
                    "offer_id": offer_id,
                    "package_id": __occ.package_id,
                    "offer_expires_epoch": __now + OFFER_LEASE_SECONDS,
                }
                _result[0] = {
                    "capability": FILECRYPT_LINK_LIFECYCLE_CAPABILITY,
                    "mode": "retest",
                    "crypter": FILECRYPT_CRYPTER,
                    "sweep_id": sweep_id,
                    "offer_id": offer_id,
                    "link_fingerprint": __fp,
                    "deadline_epoch": __now + OFFER_LEASE_SECONDS,
                    "occurrence": __occ,
                }
                return (encode_link_state(new_ls),)

            targets = [(FILECRYPT_LINK_STATES_TABLE, fp)]
            self._shared_state.get_db(FILECRYPT_LINK_STATES_TABLE).mutate_values(
                targets, mutator
            )
            if result[0] is not None:
                return result[0]

        return None

    def _try_individual(
        self,
        fp,
        fp_map,
        link_state_by_fp,
        existing_member_fps,
        excluded_package_ids,
        now,
    ):
        """Open an individual generation for one first-time fingerprint.

        Writes an offered member row only.  No link-state row is created;
        only Task 3B's accepted BLOCKED creates held state.
        """
        occurrence = _first_handable_occurrence(fp_map[fp], excluded_package_ids)
        if occurrence is None:
            return None

        _now = now
        _fp = fp
        _occ = occurrence
        result = [None]
        ids = self._ids

        # Targets: header (live-header check) + all member rows (cleanup + current) + link-state (revalidate None)
        all_member_fps = sorted(existing_member_fps | {fp})
        _fp_member_idx = 1 + all_member_fps.index(fp)
        _ls_idx = 1 + len(all_member_fps)
        _all_member_fps = all_member_fps

        targets = [(FILECRYPT_SWEEP_STATE_TABLE, FILECRYPT_SWEEP_KEY)]
        for m_fp in all_member_fps:
            targets.append((FILECRYPT_SWEEP_MEMBERS_TABLE, m_fp))
        targets.append((FILECRYPT_LINK_STATES_TABLE, fp))

        def mutator(values):
            ts = _now
            hdr_raw = values[0]
            member_raw = values[_fp_member_idx]
            ls_raw = values[_ls_idx]

            # Abort if a concurrent live valid or malformed header is present
            if hdr_raw is not None:
                hdr = decode_sweep_header(hdr_raw)
                if hdr is None:
                    result[0] = None
                    return values
                s = hdr.get("state")
                if (
                    (s == "sweeping" and ts < hdr.get("deadline_epoch", 0))
                    or (s == "healthy" and ts < hdr.get("until_epoch", 0))
                    or (s == "cooldown" and ts < hdr.get("retry_after_epoch", 0))
                ):
                    result[0] = None
                    return values

            # Abort if link-state is not None (fp is not first-time)
            if ls_raw is not None:
                result[0] = None
                return values

            # Abort if a live offered member already exists (no duplicate lease)
            if member_raw is not None:
                m = decode_sweep_member(member_raw)
                if m is None:
                    result[0] = None
                    return values
                if m.get("state") == "offered":
                    lease = m.get("lease") or {}
                    if ts < lease.get("offer_expires_epoch", 0):
                        result[0] = None
                        return values

            generation_id = ids()
            offer_id = ids()
            new_member = {
                "schema_version": _SCHEMA_VERSION,
                "generation_id": generation_id,
                "fingerprint": _fp,
                "state": "offered",
                "lease": {
                    "offer_id": offer_id,
                    "package_id": _occ.package_id,
                    "offer_expires_epoch": ts + OFFER_LEASE_SECONDS,
                },
                "outcome": None,
            }

            new_values = list(values)
            new_values[0] = hdr_raw  # header unchanged
            for i, m_fp in enumerate(_all_member_fps):
                midx = 1 + i
                new_values[midx] = (
                    encode_sweep_member(new_member) if m_fp == _fp else None
                )
            new_values[_ls_idx] = ls_raw  # link-state unchanged (None)

            result[0] = {
                "capability": FILECRYPT_LINK_LIFECYCLE_CAPABILITY,
                "mode": "individual",
                "crypter": FILECRYPT_CRYPTER,
                "sweep_id": generation_id,
                "offer_id": offer_id,
                "link_fingerprint": _fp,
                "deadline_epoch": ts + OFFER_LEASE_SECONDS,
                "occurrence": _occ,
            }
            return tuple(new_values)

        self._shared_state.get_db(FILECRYPT_SWEEP_STATE_TABLE).mutate_values(
            targets, mutator
        )
        return result[0]

    def _try_sweep_open(
        self,
        first_time_fps,
        fp_map,
        link_state_by_fp,
        existing_member_fps,
        excluded_package_ids,
        now,
    ):
        """Open a new sweep generation atomically, leasing the first handable member."""
        _now = now
        _window = self._sweep_window_seconds()

        # Determine the pre-mutation leased fingerprint
        leased_fp = None
        leased_occ = None
        for fp in first_time_fps:
            occ = _first_handable_occurrence(fp_map[fp], excluded_package_ids)
            if occ is not None:
                leased_fp = fp
                leased_occ = occ
                break

        # Fix 4: no handable occurrence → do not open or commit a sweep
        if leased_fp is None:
            return None

        first_time_set = set(first_time_fps)
        all_member_fps = sorted(existing_member_fps | first_time_set)
        n_members = len(all_member_fps)
        # Index of each first-time fp in the link-state target block
        first_time_fps_order = first_time_fps  # preserves iteration order
        first_time_fp_idx = {fp: i for i, fp in enumerate(first_time_fps_order)}

        # Build targets: header + all members + link-state for EVERY first-time fp
        targets = [(FILECRYPT_SWEEP_STATE_TABLE, FILECRYPT_SWEEP_KEY)]
        for fp in all_member_fps:
            targets.append((FILECRYPT_SWEEP_MEMBERS_TABLE, fp))
        for fp in first_time_fps_order:
            targets.append((FILECRYPT_LINK_STATES_TABLE, fp))

        result = [None]
        ids = self._ids
        _first_time_fps = first_time_fps
        _leased_fp = leased_fp
        _leased_occ = leased_occ
        _all_member_fps = all_member_fps
        _first_time_set = first_time_set
        _first_time_fp_idx = first_time_fp_idx
        _n_members = n_members

        def mutator(values):
            ts = _now
            hdr_raw = values[0]

            # Abort if a concurrent live header was installed
            if hdr_raw is not None:
                hdr = decode_sweep_header(hdr_raw)
                if hdr is None:
                    result[0] = None
                    return values
                s = hdr["state"]
                if (
                    (s == "sweeping" and ts < hdr["deadline_epoch"])
                    or (s == "healthy" and ts < hdr["until_epoch"])
                    or (s == "cooldown" and ts < hdr["retry_after_epoch"])
                ):
                    result[0] = None
                    return values

            # Abort if ANY first-time link-state was concurrently written
            ls_base = 1 + _n_members
            for i in range(len(_first_time_fps)):
                if values[ls_base + i] is not None:
                    result[0] = None
                    return values

            generation_id = ids()
            offer_id = ids()
            deadline_epoch = ts + _window
            total = len(_first_time_fps)

            new_values = list(values)

            # Header
            new_hdr = {
                "schema_version": _SCHEMA_VERSION,
                "state": "sweeping",
                "generation_id": generation_id,
                "opened_epoch": ts,
                "deadline_epoch": deadline_epoch,
                "window_seconds": _window,
                "total": total,
                "tested": 0,
                "blocked": 0,
                "global_possible": True,
            }
            new_values[0] = encode_sweep_header(new_hdr)

            # Member rows
            for i, fp in enumerate(_all_member_fps):
                midx = 1 + i
                if fp in _first_time_set:
                    is_leased = fp == _leased_fp
                    member = {
                        "schema_version": _SCHEMA_VERSION,
                        "generation_id": generation_id,
                        "fingerprint": fp,
                        "state": "offered" if is_leased else "pending",
                        "lease": (
                            {
                                "offer_id": offer_id,
                                "package_id": _leased_occ.package_id,
                                "offer_expires_epoch": ts + OFFER_LEASE_SECONDS,
                            }
                            if is_leased
                            else None
                        ),
                        "outcome": None,
                    }
                    new_values[midx] = encode_sweep_member(member)
                else:
                    new_values[midx] = None  # remove stale member

            # Link-state targets are present only for atomic revalidation; all stay None
            result[0] = {
                "capability": FILECRYPT_LINK_LIFECYCLE_CAPABILITY,
                "mode": "sweep",
                "crypter": FILECRYPT_CRYPTER,
                "sweep_id": generation_id,
                "offer_id": offer_id,
                "link_fingerprint": _leased_fp,
                "deadline_epoch": deadline_epoch,
                "occurrence": _leased_occ,
            }

            return tuple(new_values)

        self._shared_state.get_db(FILECRYPT_SWEEP_STATE_TABLE).mutate_values(
            targets, mutator
        )
        return result[0]

    def _try_sweep_lease(
        self, header, existing_member_by_fp, fp_map, excluded_package_ids, now
    ):
        """Lease the next available pending or expired-offered member in a live sweep."""
        generation_id = header["generation_id"]
        deadline_epoch = header["deadline_epoch"]

        # Collect leasable members, sorted by fingerprint
        leasable = []
        for fp in sorted(existing_member_by_fp):
            member_raw = existing_member_by_fp[fp]
            if member_raw is None:
                continue
            member = decode_sweep_member(member_raw)
            if member is None or member.get("generation_id") != generation_id:
                continue
            state = member.get("state")
            if state == "pending":
                leasable.append(fp)
            elif state == "offered":
                lease = member.get("lease") or {}
                if now >= lease.get("offer_expires_epoch", 0):
                    leasable.append(fp)

        for fp in leasable:
            if fp not in fp_map:
                continue
            occurrence = _first_handable_occurrence(fp_map[fp], excluded_package_ids)
            if occurrence is None:
                continue

            _now = now
            _fp = fp
            _occ = occurrence
            _gen_id = generation_id
            _ddl = deadline_epoch
            result = [None]
            ids = self._ids

            def mutator(
                values,
                *,
                __fp=_fp,
                __occ=_occ,
                __gen_id=_gen_id,
                __ddl=_ddl,
                __now=_now,
                _result=result,
                _ids=ids,
            ):
                hdr_raw, member_raw_curr, ls_raw = values

                # Verify header still live with same generation
                hdr = decode_sweep_header(hdr_raw)
                if (
                    hdr is None
                    or hdr.get("state") != "sweeping"
                    or hdr.get("generation_id") != __gen_id
                    or __now >= hdr.get("deadline_epoch", 0)
                ):
                    _result[0] = None
                    return values

                # Verify member still leasable
                m = decode_sweep_member(member_raw_curr)
                if m is None or m.get("generation_id") != __gen_id:
                    _result[0] = None
                    return values
                m_state = m.get("state")
                if m_state == "pending":
                    pass
                elif m_state == "offered":
                    lease = m.get("lease") or {}
                    if __now < lease.get("offer_expires_epoch", 0):
                        _result[0] = None
                        return values
                else:
                    _result[0] = None
                    return values

                # Require link-state exactly None for first-time sweep members
                if ls_raw is not None:
                    _result[0] = None
                    return values

                offer_id = _ids()

                new_member = {
                    "schema_version": _SCHEMA_VERSION,
                    "generation_id": __gen_id,
                    "fingerprint": __fp,
                    "state": "offered",
                    "lease": {
                        "offer_id": offer_id,
                        "package_id": __occ.package_id,
                        "offer_expires_epoch": __now + OFFER_LEASE_SECONDS,
                    },
                    "outcome": None,
                }
                _result[0] = {
                    "capability": FILECRYPT_LINK_LIFECYCLE_CAPABILITY,
                    "mode": "sweep",
                    "crypter": FILECRYPT_CRYPTER,
                    "sweep_id": __gen_id,
                    "offer_id": offer_id,
                    "link_fingerprint": __fp,
                    "deadline_epoch": __ddl,
                    "occurrence": __occ,
                }
                return (
                    hdr_raw,
                    encode_sweep_member(new_member),
                    ls_raw,  # link-state unchanged (None)
                )

            targets = [
                (FILECRYPT_SWEEP_STATE_TABLE, FILECRYPT_SWEEP_KEY),
                (FILECRYPT_SWEEP_MEMBERS_TABLE, fp),
                (FILECRYPT_LINK_STATES_TABLE, fp),
            ]
            self._shared_state.get_db(FILECRYPT_SWEEP_STATE_TABLE).mutate_values(
                targets, mutator
            )
            if result[0] is not None:
                return result[0]

        return None

    # ── outcome recording (Task 3B1) ──────────────────────────────────────────

    def _cooldown_seconds(self):
        configured = self._shared_state.values.get(
            "crypter_cooldown_hours", MINIMUM_COOLDOWN_HOURS
        )
        try:
            hours = int(configured)
        except (TypeError, ValueError):
            hours = MINIMUM_COOLDOWN_HOURS
        return max(MINIMUM_COOLDOWN_HOURS, hours) * 3600

    def record_blocked(self, report, protected_rows):
        """Accept a first-time BLOCKED report, or return None if stale."""
        try:
            report = normalize_lifecycle_blocked_report(report)
        except ValueError:
            return None

        fingerprint = report["link_fingerprint"]
        package_id = report["package_id"]
        offer_id = report["offer_id"]
        sweep_id = report["sweep_id"]
        top_id = report["terminal_operation_id"]

        # Resolve ownership from supplied rows
        raw_package = None
        for row in protected_rows or ():
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                if row[0] == package_id:
                    raw_package = row[1]
                    break

        now = int(self._clock())
        cooldown_secs = self._cooldown_seconds()
        window_secs = self._sweep_window_seconds()

        _now = now
        _fp = fingerprint
        _pkg_id = package_id
        _offer_id = offer_id
        _sweep_id = sweep_id
        _top_id = top_id
        _raw_package = raw_package
        _cooldown = cooldown_secs
        _window = window_secs
        result = [None]

        targets = [
            (FILECRYPT_SWEEP_STATE_TABLE, FILECRYPT_SWEEP_KEY),
            (FILECRYPT_SWEEP_MEMBERS_TABLE, _fp),
            (FILECRYPT_LINK_STATES_TABLE, _fp),
            (FILECRYPT_OFFER_RECEIPTS_TABLE, _offer_id),
            ("protected", _pkg_id),
            (CRYPTER_EVENT_TABLE, CRYPTER_EVENT_KEY),
        ]

        def mutator(values):
            hdr_raw, member_raw, ls_raw, receipt_raw, prot_raw, events_raw = values

            # 1. Check receipt first (replay for first-time and probe blocked)
            if receipt_raw is not None:
                rcpt = decode_offer_receipt(receipt_raw)
                if rcpt is None:
                    result[0] = None
                    return values
                if (
                    rcpt.get("fingerprint") == _fp
                    and rcpt.get("package_id") == _pkg_id
                    and rcpt.get("outcome") == "blocked"
                    and rcpt.get("generation_id") == _sweep_id
                ):
                    resp = rcpt.get("response")
                    # Blacklist receipt replay (retest mode)
                    if rcpt.get("mode") == "retest":
                        try:
                            validate_blacklist_response(resp)
                        except (ValueError, TypeError):
                            pass
                        else:
                            if _canonical_top_id(_pkg_id) == _top_id:
                                result[0] = {
                                    **resp,
                                    "terminal_required": False,
                                    "fingerprint": _fp,
                                    "package_id": _pkg_id,
                                    "terminal_operation_id": _top_id,
                                }
                                return values
                    # Normal defer receipt replay
                    try:
                        validate_defer_response(resp)
                    except (ValueError, TypeError):
                        result[0] = None
                        return values
                    result[0] = resp
                    return values
                # Conflicting receipt
                result[0] = None
                return values

            # 2. Blacklisting replay (retest BLOCKED that already transitioned)
            ls = decode_link_state(ls_raw) if ls_raw is not None else None
            if ls is not None and ls.get("state") == "blacklisting":
                if (
                    ls.get("recheck_sweep_id") == _sweep_id
                    and ls.get("recheck_offer_id") == _offer_id
                    and ls.get("recheck_package_id") == _pkg_id
                    and ls.get("terminal_operation_id") == _top_id
                ):
                    result[0] = {
                        "terminal_required": True,
                        "fingerprint": _fp,
                        "package_id": _pkg_id,
                        "terminal_operation_id": _top_id,
                        "offer_id": _offer_id,
                        "sweep_id": _sweep_id,
                    }
                    return values
                result[0] = None
                return values

            # 3. Validate protected row ownership
            if prot_raw is None:
                result[0] = None
                return values
            ownership = classify_package_ownership(prot_raw, "filecrypt", _fp)
            if ownership != OWNERSHIP_OWNED:
                result[0] = None
                return values

            # 4. Held link-state with matching lease → retest or probe BLOCKED
            if ls is not None and ls.get("state") == "held":
                held_lease = ls.get("lease")
                if (
                    isinstance(held_lease, dict)
                    and held_lease.get("sweep_id") == _sweep_id
                    and held_lease.get("offer_id") == _offer_id
                    and held_lease.get("package_id") == _pkg_id
                    and _now < held_lease.get("offer_expires_epoch", 0)
                ):
                    hdr = decode_sweep_header(hdr_raw) if hdr_raw is not None else None
                    is_live_cooldown = (
                        hdr is not None
                        and hdr.get("state") == "cooldown"
                        and _now < hdr.get("retry_after_epoch", 0)
                    )
                    retry_after = ls.get("retry_after_epoch", 0)

                    if _now >= retry_after and not is_live_cooldown:
                        # Retest BLOCKED → blacklisting, no receipt/counters
                        new_ls = {
                            "schema_version": _SCHEMA_VERSION,
                            "state": "blacklisting",
                            "first_blocked_epoch": ls["first_blocked_epoch"],
                            "recheck_offer_id": _offer_id,
                            "recheck_package_id": _pkg_id,
                            "recheck_sweep_id": _sweep_id,
                            "terminal_operation_id": _top_id,
                        }
                        result[0] = {
                            "terminal_required": True,
                            "fingerprint": _fp,
                            "package_id": _pkg_id,
                            "terminal_operation_id": _top_id,
                            "offer_id": _offer_id,
                            "sweep_id": _sweep_id,
                        }
                        return (
                            hdr_raw,
                            member_raw,
                            encode_link_state(new_ls),
                            receipt_raw,
                            prot_raw,
                            events_raw,
                        )

                    if _now < retry_after and is_live_cooldown:
                        # Probe BLOCKED → clear lease, receipt, probes+1
                        new_ls = dict(ls)
                        new_ls["lease"] = None
                        new_receipt = encode_offer_receipt(
                            {
                                "schema_version": _SCHEMA_VERSION,
                                "generation_id": _sweep_id,
                                "fingerprint": _fp,
                                "package_id": _pkg_id,
                                "mode": "probe",
                                "outcome": "blocked",
                                "response": build_lifecycle_defer_decision(
                                    instruction="cooldown",
                                    state="cooldown",
                                    hold_type="crypter_cooldown",
                                    evidence_count=1,
                                    retry_after_epoch=hdr["retry_after_epoch"],
                                    sweep_id=_sweep_id,
                                    sweep_tested=0,
                                    sweep_total=0,
                                    sweep_deadline_epoch=hdr["sweep_deadline_epoch"],
                                ),
                                "accepted_epoch": _now,
                                "expires_epoch": _now + RECEIPT_RETENTION_SECONDS,
                            }
                        )
                        new_events = _add_pending_crypter_events(events_raw, probes=1)
                        result[0] = build_lifecycle_defer_decision(
                            instruction="cooldown",
                            state="cooldown",
                            hold_type="crypter_cooldown",
                            evidence_count=1,
                            retry_after_epoch=hdr["retry_after_epoch"],
                            sweep_id=_sweep_id,
                            sweep_tested=0,
                            sweep_total=0,
                            sweep_deadline_epoch=hdr["sweep_deadline_epoch"],
                        )
                        return (
                            hdr_raw,
                            member_raw,
                            encode_link_state(new_ls),
                            new_receipt,
                            prot_raw,
                            new_events,
                        )

                    # Every other combination → stale
                    result[0] = None
                    return values
                # Held but lease doesn't match → fall through to first-time check
                # which will reject because ls_raw is not None
                pass

            # 5. First-time: validate member
            m = decode_sweep_member(member_raw)
            if m is None:
                result[0] = None
                return values
            if m.get("state") != "offered":
                result[0] = None
                return values
            lease = m.get("lease")
            if not isinstance(lease, dict):
                result[0] = None
                return values
            if lease.get("offer_id") != _offer_id:
                result[0] = None
                return values
            if lease.get("package_id") != _pkg_id:
                result[0] = None
                return values
            if _now >= lease.get("offer_expires_epoch", 0):
                result[0] = None
                return values
            gen_id = m.get("generation_id")

            # 4. Generation binding: report sweep_id must equal member generation
            if _sweep_id != gen_id:
                result[0] = None
                return values

            # 5. Validate link state is exactly None (first-time)
            if ls_raw is not None:
                result[0] = None
                return values

            # 6. Derive mode from header
            hdr = decode_sweep_header(hdr_raw) if hdr_raw is not None else None
            is_sweep = False
            if hdr_raw is not None and hdr is None:
                # Malformed non-None header → stale
                result[0] = None
                return values
            if hdr is not None:
                s = hdr.get("state")
                if s == "sweeping" and _now < hdr.get("deadline_epoch", 0):
                    if hdr.get("generation_id") == gen_id:
                        is_sweep = True
                    else:
                        result[0] = None
                        return values
                elif s == "healthy" and _now < hdr.get("until_epoch", 0):
                    result[0] = None
                    return values
                elif s == "cooldown" and _now < hdr.get("retry_after_epoch", 0):
                    result[0] = None
                    return values
                # Expired valid header → individual permitted

            mode = "sweep" if is_sweep else "individual"

            # ── state transitions ──
            # Member → blocked
            new_member = {
                "schema_version": _SCHEMA_VERSION,
                "generation_id": gen_id,
                "fingerprint": _fp,
                "state": "blocked",
                "lease": None,
                "outcome": {
                    "offer_id": _offer_id,
                    "package_id": _pkg_id,
                    "accepted_epoch": _now,
                },
            }

            # Link state → held
            new_ls = {
                "schema_version": _SCHEMA_VERSION,
                "state": "held",
                "first_blocked_epoch": _now,
                "retry_after_epoch": _now + _cooldown,
                "lease": None,
            }

            # Header transitions for sweep mode
            new_hdr_raw = hdr_raw
            sweep_tested = 0
            sweep_total = 0
            sweep_deadline_epoch = _now + _cooldown
            sweep_blocked = 0
            is_complete = False
            is_global_cooldown = False

            if is_sweep:
                tested = hdr["tested"] + 1
                blocked = hdr["blocked"] + 1
                gp = hdr["global_possible"]
                total = hdr["total"]
                sweep_tested = tested
                sweep_total = total
                sweep_deadline_epoch = hdr["deadline_epoch"]
                sweep_blocked = blocked

                new_hdr = dict(hdr)
                new_hdr["tested"] = tested
                new_hdr["blocked"] = blocked
                new_hdr_raw = encode_sweep_header(new_hdr)

                is_complete = tested >= total
                is_global_cooldown = (
                    is_complete
                    and gp
                    and blocked == total
                    and total >= MINIMUM_GLOBAL_COOLDOWN_SIZE
                    and _now < hdr["deadline_epoch"]
                )

                if is_global_cooldown:
                    cooldown_hdr = {
                        "schema_version": _SCHEMA_VERSION,
                        "state": "cooldown",
                        "generation_id": gen_id,
                        "sweep_deadline_epoch": hdr["deadline_epoch"],
                        "retry_after_epoch": _now + _cooldown,
                    }
                    new_hdr_raw = encode_sweep_header(cooldown_hdr)
                    new_ls["retry_after_epoch"] = _now + _cooldown
                elif is_complete and not is_global_cooldown:
                    healthy_hdr = {
                        "schema_version": _SCHEMA_VERSION,
                        "state": "healthy",
                        "generation_id": gen_id,
                        "until_epoch": _now + _window,
                    }
                    new_hdr_raw = encode_sweep_header(healthy_hdr)

            # Build response
            if is_global_cooldown:
                response = build_lifecycle_defer_decision(
                    instruction="cooldown",
                    state="cooldown",
                    hold_type="crypter_cooldown",
                    evidence_count=sweep_blocked,
                    retry_after_epoch=_now + _cooldown,
                    sweep_id=_sweep_id,
                    sweep_tested=sweep_tested,
                    sweep_total=sweep_total,
                    sweep_deadline_epoch=sweep_deadline_epoch,
                )
            elif is_sweep and not is_complete:
                response = build_lifecycle_defer_decision(
                    instruction="hold",
                    state="sweeping",
                    hold_type="provisional",
                    evidence_count=sweep_blocked,
                    retry_after_epoch=_now + _cooldown,
                    sweep_id=_sweep_id,
                    sweep_tested=sweep_tested,
                    sweep_total=sweep_total,
                    sweep_deadline_epoch=sweep_deadline_epoch,
                )
            elif is_sweep and is_complete and not is_global_cooldown:
                response = build_lifecycle_defer_decision(
                    instruction="hold",
                    state="individual",
                    hold_type="provisional",
                    evidence_count=0,
                    retry_after_epoch=_now + _cooldown,
                    sweep_id=_sweep_id,
                    sweep_tested=sweep_tested,
                    sweep_total=sweep_total,
                    sweep_deadline_epoch=sweep_deadline_epoch,
                )
            else:
                # Individual BLOCKED
                response = build_lifecycle_defer_decision(
                    instruction="hold",
                    state="individual",
                    hold_type="provisional",
                    evidence_count=0,
                    retry_after_epoch=_now + _cooldown,
                    sweep_id=_sweep_id,
                    sweep_tested=0,
                    sweep_total=0,
                    sweep_deadline_epoch=sweep_deadline_epoch,
                )

            # Receipt
            new_receipt = encode_offer_receipt(
                {
                    "schema_version": _SCHEMA_VERSION,
                    "generation_id": gen_id,
                    "fingerprint": _fp,
                    "package_id": _pkg_id,
                    "mode": mode,
                    "outcome": "blocked",
                    "response": response,
                    "accepted_epoch": _now,
                    "expires_epoch": _now + RECEIPT_RETENTION_SECONDS,
                }
            )

            # Outbox
            events_delta = {"observations": 1}
            if is_global_cooldown:
                events_delta["cooldowns"] = 1
            new_events = _add_pending_crypter_events(events_raw, **events_delta)

            result[0] = response
            return (
                new_hdr_raw,
                encode_sweep_member(new_member),
                encode_link_state(new_ls),
                new_receipt,
                prot_raw,  # protected unchanged
                new_events,
            )

        self._shared_state.get_db(FILECRYPT_SWEEP_STATE_TABLE).mutate_values(
            targets, mutator
        )
        if result[0] is None:
            return None
        if result[0].get("terminal_required"):
            return result[0]
        return {
            **result[0],
            "terminal_required": False,
            "fingerprint": _fp,
            "package_id": _pkg_id,
            "terminal_operation_id": _top_id,
        }

    def record_access(self, report, protected_rows):
        """Accept a first-time CLEAR or UNKNOWN report, or return None if stale."""
        try:
            report = normalize_lifecycle_access_report(report)
        except ValueError:
            return None

        fingerprint = report["link_fingerprint"]
        package_id = report["package_id"]
        offer_id = report["offer_id"]
        sweep_id = report["sweep_id"]
        access = report["access"]
        top_id = report["terminal_operation_id"]

        raw_package = None
        for row in protected_rows or ():
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                if row[0] == package_id:
                    raw_package = row[1]
                    break

        now = int(self._clock())
        window_secs = self._sweep_window_seconds()

        _now = now
        _fp = fingerprint
        _pkg_id = package_id
        _offer_id = offer_id
        _sweep_id = sweep_id
        _access = access
        _top_id = top_id
        _raw_package = raw_package
        _window = window_secs
        result = [None]

        targets = [
            (FILECRYPT_SWEEP_STATE_TABLE, FILECRYPT_SWEEP_KEY),
            (FILECRYPT_SWEEP_MEMBERS_TABLE, _fp),
            (FILECRYPT_LINK_STATES_TABLE, _fp),
            (FILECRYPT_OFFER_RECEIPTS_TABLE, _offer_id),
            ("protected", _pkg_id),
            (CRYPTER_EVENT_TABLE, CRYPTER_EVENT_KEY),
        ]

        def mutator(values):
            hdr_raw, member_raw, ls_raw, receipt_raw, prot_raw, events_raw = values

            # 1. Check receipt first (replay)
            if receipt_raw is not None:
                rcpt = decode_offer_receipt(receipt_raw)
                if rcpt is None:
                    result[0] = None
                    return values
                if (
                    rcpt.get("fingerprint") == _fp
                    and rcpt.get("package_id") == _pkg_id
                    and rcpt.get("outcome") == _access
                    and rcpt.get("generation_id") == _sweep_id
                ):
                    resp = rcpt.get("response")
                    try:
                        validate_access_response(resp)
                    except (ValueError, TypeError):
                        result[0] = None
                        return values
                    result[0] = resp
                    return values
                # Conflicting receipt
                result[0] = None
                return values

            # 2. Validate protected row ownership
            if prot_raw is None:
                result[0] = None
                return values
            ownership = classify_package_ownership(prot_raw, "filecrypt", _fp)
            if ownership != OWNERSHIP_OWNED:
                result[0] = None
                return values

            # 3. Held link-state with matching lease → retest or probe access
            ls = decode_link_state(ls_raw) if ls_raw is not None else None
            if ls is not None and ls.get("state") == "held":
                held_lease = ls.get("lease")
                if (
                    isinstance(held_lease, dict)
                    and held_lease.get("sweep_id") == _sweep_id
                    and held_lease.get("offer_id") == _offer_id
                    and held_lease.get("package_id") == _pkg_id
                    and _now < held_lease.get("offer_expires_epoch", 0)
                ):
                    hdr = decode_sweep_header(hdr_raw) if hdr_raw is not None else None
                    is_live_cooldown = (
                        hdr is not None
                        and hdr.get("state") == "cooldown"
                        and _now < hdr.get("retry_after_epoch", 0)
                    )
                    retry_after = ls.get("retry_after_epoch", 0)

                    if _now >= retry_after and not is_live_cooldown:
                        # Retest access
                        if _access == "clear":
                            new_hdr = {
                                "schema_version": _SCHEMA_VERSION,
                                "state": "healthy",
                                "generation_id": _sweep_id,
                                "until_epoch": _now + _window,
                            }
                            response = build_lifecycle_access_decision(
                                state="healthy",
                                cleared=True,
                                accepted="",
                                sweep_id=_sweep_id,
                                sweep_tested=0,
                                sweep_total=0,
                                sweep_deadline_epoch=_now + _window,
                            )
                            new_receipt = encode_offer_receipt(
                                {
                                    "schema_version": _SCHEMA_VERSION,
                                    "generation_id": _sweep_id,
                                    "fingerprint": _fp,
                                    "package_id": _pkg_id,
                                    "mode": "retest",
                                    "outcome": "clear",
                                    "response": response,
                                    "accepted_epoch": _now,
                                    "expires_epoch": _now + RECEIPT_RETENTION_SECONDS,
                                }
                            )
                            result[0] = response
                            return (
                                encode_sweep_header(new_hdr),
                                member_raw,
                                None,
                                new_receipt,
                                prot_raw,
                                events_raw,
                            )
                        else:
                            # Retest UNKNOWN: clear lease, preserve epochs
                            new_ls = dict(ls)
                            new_ls["lease"] = None
                            response = build_lifecycle_access_decision(
                                state="individual",
                                cleared=False,
                                accepted="unknown",
                                sweep_id=_sweep_id,
                                sweep_tested=0,
                                sweep_total=0,
                                sweep_deadline_epoch=_now + _window,
                            )
                            new_receipt = encode_offer_receipt(
                                {
                                    "schema_version": _SCHEMA_VERSION,
                                    "generation_id": _sweep_id,
                                    "fingerprint": _fp,
                                    "package_id": _pkg_id,
                                    "mode": "retest",
                                    "outcome": "unknown",
                                    "response": response,
                                    "accepted_epoch": _now,
                                    "expires_epoch": _now + RECEIPT_RETENTION_SECONDS,
                                }
                            )
                            result[0] = response
                            return (
                                hdr_raw,
                                member_raw,
                                encode_link_state(new_ls),
                                new_receipt,
                                prot_raw,
                                events_raw,
                            )

                    if _now < retry_after and is_live_cooldown:
                        # Probe access
                        if _access == "clear":
                            new_hdr = {
                                "schema_version": _SCHEMA_VERSION,
                                "state": "healthy",
                                "generation_id": _sweep_id,
                                "until_epoch": _now + _window,
                            }
                            response = build_lifecycle_access_decision(
                                state="healthy",
                                cleared=True,
                                accepted="",
                                sweep_id=_sweep_id,
                                sweep_tested=0,
                                sweep_total=0,
                                sweep_deadline_epoch=_now + _window,
                            )
                            new_receipt = encode_offer_receipt(
                                {
                                    "schema_version": _SCHEMA_VERSION,
                                    "generation_id": _sweep_id,
                                    "fingerprint": _fp,
                                    "package_id": _pkg_id,
                                    "mode": "probe",
                                    "outcome": "clear",
                                    "response": response,
                                    "accepted_epoch": _now,
                                    "expires_epoch": _now + RECEIPT_RETENTION_SECONDS,
                                }
                            )
                            new_events = _add_pending_crypter_events(
                                events_raw, probes=1
                            )
                            result[0] = response
                            return (
                                encode_sweep_header(new_hdr),
                                member_raw,
                                None,
                                new_receipt,
                                prot_raw,
                                new_events,
                            )
                        else:
                            # Probe UNKNOWN: clear lease, header unchanged
                            new_ls = dict(ls)
                            new_ls["lease"] = None
                            response = build_lifecycle_access_decision(
                                state="cooldown",
                                cleared=False,
                                accepted="unknown",
                                sweep_id=_sweep_id,
                                sweep_tested=0,
                                sweep_total=0,
                                sweep_deadline_epoch=hdr["sweep_deadline_epoch"],
                            )
                            new_receipt = encode_offer_receipt(
                                {
                                    "schema_version": _SCHEMA_VERSION,
                                    "generation_id": _sweep_id,
                                    "fingerprint": _fp,
                                    "package_id": _pkg_id,
                                    "mode": "probe",
                                    "outcome": "unknown",
                                    "response": response,
                                    "accepted_epoch": _now,
                                    "expires_epoch": _now + RECEIPT_RETENTION_SECONDS,
                                }
                            )
                            new_events = _add_pending_crypter_events(
                                events_raw, probes=1
                            )
                            result[0] = response
                            return (
                                hdr_raw,
                                member_raw,
                                encode_link_state(new_ls),
                                new_receipt,
                                prot_raw,
                                new_events,
                            )

                    # Every other combination → stale
                    result[0] = None
                    return values
                # Held but lease doesn't match → fall through to first-time check
                # which will reject because ls_raw is not None

            # 4. First-time: validate member
            m = decode_sweep_member(member_raw)
            if m is None:
                result[0] = None
                return values
            if m.get("state") != "offered":
                result[0] = None
                return values
            lease = m.get("lease")
            if not isinstance(lease, dict):
                result[0] = None
                return values
            if lease.get("offer_id") != _offer_id:
                result[0] = None
                return values
            if lease.get("package_id") != _pkg_id:
                result[0] = None
                return values
            if _now >= lease.get("offer_expires_epoch", 0):
                result[0] = None
                return values
            gen_id = m.get("generation_id")

            # 4. Generation binding
            if _sweep_id != gen_id:
                result[0] = None
                return values

            # 5. Link state must be None
            if ls_raw is not None:
                result[0] = None
                return values

            # 6. Derive mode from header
            hdr = decode_sweep_header(hdr_raw) if hdr_raw is not None else None
            is_sweep = False
            if hdr_raw is not None and hdr is None:
                result[0] = None
                return values
            if hdr is not None:
                s = hdr.get("state")
                if s == "sweeping" and _now < hdr.get("deadline_epoch", 0):
                    if hdr.get("generation_id") == gen_id:
                        is_sweep = True
                    else:
                        result[0] = None
                        return values
                elif s == "healthy" and _now < hdr.get("until_epoch", 0):
                    result[0] = None
                    return values
                elif s == "cooldown" and _now < hdr.get("retry_after_epoch", 0):
                    result[0] = None
                    return values

            mode = "sweep" if is_sweep else "individual"

            # ── state transitions ──
            terminal_state = "clear" if _access == "clear" else "unknown"
            new_member = {
                "schema_version": _SCHEMA_VERSION,
                "generation_id": gen_id,
                "fingerprint": _fp,
                "state": terminal_state,
                "lease": None,
                "outcome": {
                    "offer_id": _offer_id,
                    "package_id": _pkg_id,
                    "accepted_epoch": _now,
                },
            }

            # No link-state row for CLEAR/UNKNOWN
            new_ls_raw = None

            # Header: make global_possible=False for sweep; individual writes healthy
            new_hdr_raw = hdr_raw
            sweep_tested = 0
            sweep_total = 0
            sweep_deadline_epoch = _now + _window

            if is_sweep:
                tested = hdr["tested"] + 1
                total = hdr["total"]
                sweep_tested = tested
                sweep_total = total
                sweep_deadline_epoch = hdr["deadline_epoch"]

                new_hdr = dict(hdr)
                new_hdr["tested"] = tested
                new_hdr["global_possible"] = False
                new_hdr_raw = encode_sweep_header(new_hdr)

                is_complete = tested >= total
                if is_complete:
                    healthy_hdr = {
                        "schema_version": _SCHEMA_VERSION,
                        "state": "healthy",
                        "generation_id": gen_id,
                        "until_epoch": _now + _window,
                    }
                    new_hdr_raw = encode_sweep_header(healthy_hdr)
            else:
                # Individual CLEAR/UNKNOWN: write healthy header
                healthy_hdr = {
                    "schema_version": _SCHEMA_VERSION,
                    "state": "healthy",
                    "generation_id": gen_id,
                    "until_epoch": _now + _window,
                }
                new_hdr_raw = encode_sweep_header(healthy_hdr)

            # Build response
            if _access == "clear":
                response = build_lifecycle_access_decision(
                    state="healthy",
                    cleared=True,
                    accepted="",
                    sweep_id=_sweep_id,
                    sweep_tested=0,
                    sweep_total=0,
                    sweep_deadline_epoch=sweep_deadline_epoch,
                )
            else:
                # UNKNOWN
                if is_sweep:
                    is_complete = sweep_tested >= sweep_total
                    if not is_complete:
                        resp_state = "sweeping"
                    else:
                        resp_state = "healthy"
                    response = build_lifecycle_access_decision(
                        state=resp_state,
                        cleared=False,
                        accepted="unknown",
                        sweep_id=_sweep_id,
                        sweep_tested=sweep_tested,
                        sweep_total=sweep_total,
                        sweep_deadline_epoch=sweep_deadline_epoch,
                    )
                else:
                    response = build_lifecycle_access_decision(
                        state="individual",
                        cleared=False,
                        accepted="unknown",
                        sweep_id=_sweep_id,
                        sweep_tested=0,
                        sweep_total=0,
                        sweep_deadline_epoch=sweep_deadline_epoch,
                    )

            # Receipt
            new_receipt = encode_offer_receipt(
                {
                    "schema_version": _SCHEMA_VERSION,
                    "generation_id": gen_id,
                    "fingerprint": _fp,
                    "package_id": _pkg_id,
                    "mode": mode,
                    "outcome": _access,
                    "response": response,
                    "accepted_epoch": _now,
                    "expires_epoch": _now + RECEIPT_RETENTION_SECONDS,
                }
            )

            # No outbox delta for CLEAR/UNKNOWN
            result[0] = response
            return (
                new_hdr_raw,
                encode_sweep_member(new_member),
                new_ls_raw,  # None: no link state
                new_receipt,
                prot_raw,  # protected unchanged
                events_raw,  # outbox unchanged
            )

        self._shared_state.get_db(FILECRYPT_SWEEP_STATE_TABLE).mutate_values(
            targets, mutator
        )
        if result[0] is None:
            return None
        return {
            **result[0],
            "terminal_required": False,
            "fingerprint": _fp,
            "package_id": _pkg_id,
            "terminal_operation_id": _top_id,
        }

    # ── blacklist confirmation (Task 3B2B) ────────────────────────────────────

    def confirm_blacklist(self, fingerprint, offer_id, terminal_operation_id):
        """Confirm a terminal blacklist for a link in blacklisting state.

        Returns the exact wrapper dict on success or replay, None if not
        applicable.  Raises ValueError for invalid argument syntax.
        """
        if not isinstance(fingerprint, str) or not _FP_RE.fullmatch(fingerprint):
            raise ValueError("fingerprint must be 64 lowercase hex")
        if not isinstance(offer_id, str) or not _ID_RE.fullmatch(offer_id):
            raise ValueError("offer_id must be 32 lowercase hex")
        if not isinstance(terminal_operation_id, str) or not _FP_RE.fullmatch(
            terminal_operation_id
        ):
            raise ValueError("terminal_operation_id must be 64 lowercase hex")

        now = int(self._clock())
        _now = now
        _fp = fingerprint
        _offer_id = offer_id
        _top_id = terminal_operation_id
        _window = self._sweep_window_seconds()
        result = [None]

        targets = [
            (FILECRYPT_LINK_STATES_TABLE, _fp),
            (FILECRYPT_OFFER_RECEIPTS_TABLE, _offer_id),
        ]

        def mutator(values):
            ls_raw, receipt_raw = values

            # Receipt-first replay
            if receipt_raw is not None:
                rcpt = decode_offer_receipt(receipt_raw)
                if rcpt is None:
                    result[0] = None
                    return values
                if (
                    rcpt.get("fingerprint") == _fp
                    and rcpt.get("outcome") == "blocked"
                    and rcpt.get("mode") == "retest"
                ):
                    resp = rcpt.get("response")
                    try:
                        validate_blacklist_response(resp)
                    except (ValueError, TypeError):
                        result[0] = None
                        return values
                    pkg_id = rcpt.get("package_id")
                    if _canonical_top_id(pkg_id) != _top_id:
                        result[0] = None
                        return values
                    result[0] = {
                        **resp,
                        "terminal_required": False,
                        "fingerprint": _fp,
                        "package_id": pkg_id,
                        "terminal_operation_id": _top_id,
                    }
                    return values
                # Malformed or conflicting receipt → fail closed
                result[0] = None
                return values

            # Fresh confirmation
            ls = decode_link_state(ls_raw) if ls_raw is not None else None
            if ls is None or ls.get("state") != "blacklisting":
                result[0] = None
                return values

            blacklisting = ls
            if blacklisting.get("recheck_offer_id") != _offer_id:
                result[0] = None
                return values
            if blacklisting.get("terminal_operation_id") != _top_id:
                result[0] = None
                return values

            sweep_id = blacklisting["recheck_sweep_id"]
            package_id = blacklisting["recheck_package_id"]

            response = build_blacklist_decision(
                sweep_id=sweep_id,
                sweep_deadline_epoch=_now + _window,
            )

            new_ls = {
                "schema_version": _SCHEMA_VERSION,
                "state": "blacklisted",
                "first_blocked_epoch": blacklisting["first_blocked_epoch"],
                "blacklisted_epoch": _now,
            }
            new_receipt = encode_offer_receipt(
                {
                    "schema_version": _SCHEMA_VERSION,
                    "generation_id": sweep_id,
                    "fingerprint": _fp,
                    "package_id": package_id,
                    "mode": "retest",
                    "outcome": "blocked",
                    "response": response,
                    "accepted_epoch": _now,
                    "expires_epoch": _now + RECEIPT_RETENTION_SECONDS,
                }
            )
            result[0] = {
                **response,
                "terminal_required": False,
                "fingerprint": _fp,
                "package_id": package_id,
                "terminal_operation_id": _top_id,
            }
            return (encode_link_state(new_ls), new_receipt)

        self._shared_state.get_db(FILECRYPT_LINK_STATES_TABLE).mutate_values(
            targets, mutator
        )
        return result[0]

    # ── receipt pruning (Task 3C) ─────────────────────────────────────────────

    def prune_receipts(self) -> int:
        """Prune expired offer receipts.  Returns the number of rows deleted."""
        receipts_db = self._shared_state.get_db(FILECRYPT_OFFER_RECEIPTS_TABLE)
        all_rows = receipts_db.retrieve_all_titles() or []

        now = int(self._clock())

        expired = []
        malformed_count = 0
        for key, raw in all_rows:
            record = decode_offer_receipt(raw)
            if record is None:
                malformed_count += 1
                continue
            if record["expires_epoch"] <= now:
                expired.append((key, raw))

        total = len(all_rows)
        if total >= RECEIPT_ADVISORY_THRESHOLD:
            log.warn(
                f"filecrypt receipt table has {total} rows "
                f"({len(expired)} expired, {malformed_count} malformed)"
            )
        if malformed_count > 0:
            log.warn(f"filecrypt receipt table has {malformed_count} malformed rows")

        if not expired:
            return 0

        expired.sort(key=lambda x: x[0])
        targets = [(FILECRYPT_OFFER_RECEIPTS_TABLE, key) for key, _ in expired]
        _expired_by_key = {key: raw for key, raw in expired}
        _now = now

        # Overwritten on each invocation; last write wins (never accumulates).
        deletion_flags: list[tuple[bool, ...]] = [()]

        def _pruning_mutator(values):
            flags: list[bool] = []
            result = []
            for i, current_raw in enumerate(values):
                key = targets[i][1]
                enumerated_raw = _expired_by_key[key]
                if current_raw is not None and current_raw == enumerated_raw:
                    record = decode_offer_receipt(current_raw)
                    if record is not None and record["expires_epoch"] <= _now:
                        result.append(None)
                        flags.append(True)
                        continue
                result.append(current_raw)
                flags.append(False)
            deletion_flags[0] = tuple(flags)
            return tuple(result)

        receipts_db.mutate_values(targets, _pruning_mutator)
        return sum(deletion_flags[0])
