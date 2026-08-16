# -*- coding: utf-8 -*-
# Quasarr
"""Filecrypt link-lifecycle service: opening, leasing, and read projections.

Implements only the opening/leasing/projection slice (Task 3A).  Receipt
processing, report outcomes, outbox, migration, route wiring, settings
persistence, and terminal effects are deferred to later tasks.
"""

import secrets
import time

from quasarr.providers.crypter_candidates import (
    FilecryptCandidate,
    enumerate_filecrypt_lifecycle_candidates,
)
from quasarr.providers.filecrypt_lifecycle import (
    FILECRYPT_LINK_STATES_TABLE,
    FILECRYPT_SWEEP_KEY,
    FILECRYPT_SWEEP_MEMBERS_TABLE,
    FILECRYPT_SWEEP_STATE_TABLE,
    MINIMUM_SWEEP_SIZE,
    decode_link_state,
    decode_sweep_header,
    decode_sweep_member,
    encode_link_state,
    encode_sweep_header,
    encode_sweep_member,
)

FILECRYPT_LINK_LIFECYCLE_CAPABILITY = "filecrypt_link_lifecycle_v1"
FILECRYPT_CRYPTER = "filecrypt"
DEFAULT_SWEEP_WINDOW_MINUTES = 15
OFFER_LEASE_SECONDS = 120

_SCHEMA_VERSION = 1
_WINDOW_MIN = 1
_WINDOW_MAX = 1440


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
        self, protected_rows, *, excluded_package_ids=(), preferred_fingerprint=None
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
    ):
        """Issue a probe offer under a live cooldown, or None."""
        if preferred_fingerprint is None or preferred_fingerprint not in fp_map:
            return None
        ls_raw = link_state_by_fp.get(preferred_fingerprint)
        if ls_raw is None:
            return None
        ls = decode_link_state(ls_raw)
        if ls is None or ls["state"] != "held" or now >= ls["retry_after_epoch"]:
            return None
        occurrence = _first_handable_occurrence(
            fp_map[preferred_fingerprint], excluded_package_ids
        )
        if occurrence is None:
            return None

        _now = now
        _fp = preferred_fingerprint
        _occ = occurrence
        _gen_id = header["generation_id"]
        result = [None]
        ids = self._ids

        def mutator(values):
            hdr_raw, ls_curr_raw = values
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
                return (hdr_raw, ls_curr_raw)
            offer_id = ids()
            # Reuse existing sweep_id from lease if present, else use cooldown generation
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
            return (hdr_raw, encode_link_state(new_ls))

        targets = [
            (FILECRYPT_SWEEP_STATE_TABLE, FILECRYPT_SWEEP_KEY),
            (FILECRYPT_LINK_STATES_TABLE, _fp),
        ]
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
        """Open an individual generation for one first-time fingerprint."""
        occurrence = _first_handable_occurrence(fp_map[fp], excluded_package_ids)
        if occurrence is None:
            return None

        _now = now
        _fp = fp
        _occ = occurrence
        _window = self._sweep_window_seconds()
        result = [None]
        ids = self._ids

        def mutator(values):
            hdr_raw, ls_raw = values
            # Abort if a concurrent live valid or malformed header is present
            if hdr_raw is not None:
                hdr = decode_sweep_header(hdr_raw)
                if hdr is None:
                    result[0] = None
                    return values
                s = hdr.get("state")
                if (
                    (s == "sweeping" and _now < hdr.get("deadline_epoch", 0))
                    or (s == "healthy" and _now < hdr.get("until_epoch", 0))
                    or (s == "cooldown" and _now < hdr.get("retry_after_epoch", 0))
                ):
                    result[0] = None
                    return values
            if ls_raw is not None:
                result[0] = None
                return values
            generation_id = ids()
            offer_id = ids()
            ts = _now
            retry_after = ts + _window
            new_ls = {
                "schema_version": _SCHEMA_VERSION,
                "state": "held",
                "first_blocked_epoch": ts,
                "retry_after_epoch": retry_after,
                "lease": {
                    "sweep_id": generation_id,
                    "offer_id": offer_id,
                    "package_id": _occ.package_id,
                    "offer_expires_epoch": ts + OFFER_LEASE_SECONDS,
                },
            }
            result[0] = {
                "capability": FILECRYPT_LINK_LIFECYCLE_CAPABILITY,
                "mode": "individual",
                "crypter": FILECRYPT_CRYPTER,
                "sweep_id": generation_id,
                "offer_id": offer_id,
                "link_fingerprint": _fp,
                "deadline_epoch": retry_after,
                "occurrence": _occ,
            }
            return (hdr_raw, encode_link_state(new_ls))

        targets = [
            (FILECRYPT_SWEEP_STATE_TABLE, FILECRYPT_SWEEP_KEY),
            (FILECRYPT_LINK_STATES_TABLE, fp),
        ]
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

            # Link state for leased member only; all others stay absent
            leased_ls_idx = ls_base + _first_time_fp_idx[_leased_fp]
            new_ls = {
                "schema_version": _SCHEMA_VERSION,
                "state": "held",
                "first_blocked_epoch": ts,
                "retry_after_epoch": deadline_epoch,
                "lease": {
                    "sweep_id": generation_id,
                    "offer_id": offer_id,
                    "package_id": _leased_occ.package_id,
                    "offer_expires_epoch": ts + OFFER_LEASE_SECONDS,
                },
            }
            new_values[leased_ls_idx] = encode_link_state(new_ls)
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

                # Verify link state is compatible (None or held)
                if ls_raw is not None:
                    ls_check = decode_link_state(ls_raw)
                    if ls_check is None or ls_check.get("state") != "held":
                        _result[0] = None
                        return values

                offer_id = _ids()
                existing_ls = decode_link_state(ls_raw) if ls_raw is not None else None
                first_blocked = (
                    existing_ls["first_blocked_epoch"] if existing_ls else __now
                )

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
                new_ls = {
                    "schema_version": _SCHEMA_VERSION,
                    "state": "held",
                    "first_blocked_epoch": first_blocked,
                    "retry_after_epoch": __ddl,
                    "lease": {
                        "sweep_id": __gen_id,
                        "offer_id": offer_id,
                        "package_id": __occ.package_id,
                        "offer_expires_epoch": __now + OFFER_LEASE_SECONDS,
                    },
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
                    encode_link_state(new_ls),
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
