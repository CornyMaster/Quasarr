# -*- coding: utf-8 -*-
# Quasarr
"""Filecrypt lifecycle state migration: atomically migrate proven legacy state.

Reads old crypter_cooldowns[filecrypt] and package deferred metadata, proves
lifecycle holds from observations and v2 defer fingerprints, then atomically
writes lifecycle header/link-states and cleans represented package metadata.
"""

import json

from quasarr.providers.crypter_cooldowns import (
    _decode_record,
    decode_package_defer,
)
from quasarr.providers.crypter_sweeps import (
    decode_decision_record,
    migrate_legacy_record,
)
from quasarr.providers.filecrypt_lifecycle import (
    FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE,
    FILECRYPT_LINK_STATES_TABLE,
    FILECRYPT_MIGRATION_KEY,
    FILECRYPT_SWEEP_KEY,
    FILECRYPT_SWEEP_STATE_TABLE,
    decode_link_state,
    decode_migration_marker,
    decode_sweep_header,
    encode_link_state,
    encode_migration_marker,
    encode_sweep_header,
)

_RESULT_ALREADY = {
    "status": "already_migrated",
    "held": 0,
    "packages_cleaned": 0,
    "global_cooldown": False,
}
_RESULT_UNAVAILABLE = {
    "status": "unavailable",
    "held": 0,
    "packages_cleaned": 0,
    "global_cooldown": False,
}
_RESULT_CONFLICT = {
    "status": "conflict",
    "held": 0,
    "packages_cleaned": 0,
    "global_cooldown": False,
}
_FILECRYPT_CRYPTER = "filecrypt"


def _parse_package(raw):
    if raw is None:
        return None
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def _collect_legacy_observations(old_raw):
    """Extract valid observations from v1 legacy row, or empty list."""
    try:
        record = _decode_record(old_raw)
    except (TypeError, ValueError, RecursionError):
        return []
    if record is None:
        return []
    return record.get("observations") or []


def _decode_old_decision(raw, now):
    """Decode the old decision: try v2 first, then v1 legacy cooldown."""
    if raw is None:
        return None
    decision = decode_decision_record(raw, now=now)
    if decision is not None:
        return decision
    return migrate_legacy_record(raw, now=now)


def _old_row_is_valid(raw):
    """Whether the old crypter_cooldowns row is parseable (valid, not malformed)."""
    if raw is None:
        return True  # absent is fine
    # Try v2 decode (expiry-blind)
    from quasarr.providers.crypter_sweeps import is_decision_record

    if is_decision_record(raw):
        return True
    # Try v1 legacy decode
    try:
        record = _decode_record(raw)
        return record is not None
    except (TypeError, ValueError, RecursionError):
        return False


def _is_active_cooldown(decision):
    return isinstance(decision, dict) and decision.get("state") == "cooldown"


def _build_lifecycle_header(decision, generation_id, now):
    """Build the lifecycle cooldown header from the old decision."""
    if decision.get("legacy_cooldown") is True:
        return {
            "schema_version": 1,
            "state": "cooldown",
            "generation_id": generation_id,
            "sweep_deadline_epoch": max(1, now),
            "retry_after_epoch": decision["retry_after_epoch"],
        }
    # Cohort v2 cooldown
    sweep_id = decision.get("sweep_id", "")
    deadline_epoch = decision.get("deadline_epoch", 0)
    return {
        "schema_version": 1,
        "state": "cooldown",
        "generation_id": sweep_id,
        "sweep_deadline_epoch": deadline_epoch,
        "retry_after_epoch": decision["retry_after_epoch"],
    }


def _prove_holds(protected_rows, observations, now):
    """Collect provable held fingerprints with their since/retry epochs.

    Returns dict: fingerprint -> {"since_epoch": int, "retry_after_epoch": int}
    Only active holds (retry_after_epoch > now) are included.
    """
    # Index observations by package_id
    obs_by_pkg = {}
    for obs in observations:
        pid = obs.get("package_id")
        if pid:
            obs_by_pkg.setdefault(pid, []).append(obs)

    # For each protected row, extract proven fingerprints from its defer
    holds = {}  # fingerprint -> {"since": min, "retry": max active}

    for row in protected_rows or ():
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        package_id, raw = row[0], row[1]
        if not isinstance(package_id, str):
            continue
        pkg_data = _parse_package(raw)
        if pkg_data is None:
            continue
        try:
            deferred = decode_package_defer(pkg_data)
        except (TypeError, ValueError):
            continue
        if deferred is None:
            continue
        if deferred.get("crypter") != _FILECRYPT_CRYPTER:
            continue

        since = deferred.get("since_epoch", 0)
        retry = deferred.get("retry_after_epoch", 0)

        # V2 defer: has link_fingerprints
        if "link_fingerprints" in deferred:
            for fp in deferred["link_fingerprints"]:
                if retry > now:
                    _merge_hold(holds, fp, since, retry)
                # Even if expired, it's still "represented" for cleanup purposes
        else:
            # Legacy (generationless) defer: only proves fingerprints that appear
            # in legacy observations with matching package_id
            pkg_obs = obs_by_pkg.get(package_id, [])
            for obs in pkg_obs:
                fp = obs.get("link_fingerprint")
                if fp and retry > now:
                    _merge_hold(holds, fp, since, retry)

    return holds


def _merge_hold(holds, fingerprint, since, retry):
    """Deterministic merge: minimum since_epoch, maximum retry_after_epoch."""
    existing = holds.get(fingerprint)
    if existing is None:
        holds[fingerprint] = {"since_epoch": since, "retry_after_epoch": retry}
    else:
        existing["since_epoch"] = min(existing["since_epoch"], since)
        existing["retry_after_epoch"] = max(existing["retry_after_epoch"], retry)


def _represented_fingerprints(protected_rows, observations, now):
    """All fingerprints that are represented by proof (active or expired).

    Used to determine which package defers can be cleaned up.
    """
    obs_by_pkg = {}
    for obs in observations:
        pid = obs.get("package_id")
        if pid:
            obs_by_pkg.setdefault(pid, []).append(obs)

    result = {}  # package_id -> set of represented fingerprints

    for row in protected_rows or ():
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        package_id, raw = row[0], row[1]
        if not isinstance(package_id, str):
            continue
        pkg_data = _parse_package(raw)
        if pkg_data is None:
            continue
        try:
            deferred = decode_package_defer(pkg_data)
        except (TypeError, ValueError):
            continue
        if deferred is None:
            continue
        if deferred.get("crypter") != _FILECRYPT_CRYPTER:
            continue

        # probe_requested defers stay for the queue to work
        if deferred.get("probe_requested"):
            continue

        fps = set()
        if "link_fingerprints" in deferred:
            fps.update(deferred["link_fingerprints"])
        else:
            # Legacy defer: only represented if matching observation exists
            pkg_obs = obs_by_pkg.get(package_id, [])
            for obs in pkg_obs:
                fp = obs.get("link_fingerprint")
                if fp:
                    fps.add(fp)
            if not fps:
                # Generationless defer with no matching observation: unprovable
                continue

        result[package_id] = fps

    return result


def prepare_migration(shared_state, now, generation_id, protected_rows):
    """Pre-read all migration inputs and compute the atomic target set.

    Returns (pre_reads, plan) or (None, result_dict) on early exit.
    pre_reads: dict of (table, key) -> raw value
    plan: dict with computed targets and new values
    """
    # Read marker
    marker_raw = shared_state.get_db(FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE).retrieve(
        FILECRYPT_MIGRATION_KEY
    )

    # Fast path: already migrated
    marker = decode_migration_marker(marker_raw)
    if marker is not None:
        return None, _RESULT_ALREADY

    # Malformed non-None marker
    if marker_raw is not None:
        return None, _RESULT_UNAVAILABLE

    # Read old decision
    old_raw = shared_state.get_db("crypter_cooldowns").retrieve(_FILECRYPT_CRYPTER)

    # Malformed non-None old decision halts migration
    if old_raw is not None and not _old_row_is_valid(old_raw):
        return None, _RESULT_UNAVAILABLE

    # Read lifecycle header
    header_raw = shared_state.get_db(FILECRYPT_SWEEP_STATE_TABLE).retrieve(
        FILECRYPT_SWEEP_KEY
    )

    # Decode old decision
    old_decision = _decode_old_decision(old_raw, now)

    # Decode observations from the v1 legacy row (for fingerprint proof)
    observations = _collect_legacy_observations(old_raw)

    # Determine if we write a cooldown header
    write_header = False
    new_header = None
    if _is_active_cooldown(old_decision) and header_raw is None:
        write_header = True
        new_header = _build_lifecycle_header(old_decision, generation_id, now)

    # If existing lifecycle header present, validate it
    if header_raw is not None:
        existing_header = decode_sweep_header(header_raw)
        if existing_header is None:
            return None, _RESULT_UNAVAILABLE

    # Prove holds: v2 defers are self-proving, legacy needs observations
    holds = _prove_holds(protected_rows, observations, now)

    # Read existing link-state for each target fingerprint
    ls_db = shared_state.get_db(FILECRYPT_LINK_STATES_TABLE)
    ls_pre_reads = {}
    for fp in holds:
        ls_pre_reads[fp] = ls_db.retrieve(fp)

    # Validate existing link states
    for _fp, ls_raw in ls_pre_reads.items():
        if ls_raw is not None:
            ls = decode_link_state(ls_raw)
            if ls is None:
                return None, _RESULT_UNAVAILABLE

    # Compute represented packages for cleanup
    represented = _represented_fingerprints(protected_rows, observations, now)

    # Read protected packages for their current raw values
    prot_db = shared_state.get_db("protected")
    prot_pre_reads = {}
    for pkg_id in represented:
        prot_pre_reads[pkg_id] = prot_db.retrieve(pkg_id)

    # Build pre-reads map
    pre_reads = {
        (FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE, FILECRYPT_MIGRATION_KEY): marker_raw,
        ("crypter_cooldowns", _FILECRYPT_CRYPTER): old_raw,
        (FILECRYPT_SWEEP_STATE_TABLE, FILECRYPT_SWEEP_KEY): header_raw,
    }
    for fp, raw in ls_pre_reads.items():
        pre_reads[(FILECRYPT_LINK_STATES_TABLE, fp)] = raw
    for pkg_id, raw in prot_pre_reads.items():
        pre_reads[("protected", pkg_id)] = raw

    plan = {
        "write_header": write_header,
        "new_header": new_header,
        "holds": holds,
        "represented": represented,
        "old_raw": old_raw,
        "old_decision": old_decision,
        "old_row_valid": _old_row_is_valid(old_raw),
        "observations": observations,
    }
    return pre_reads, plan


def build_targets_and_mutator(pre_reads, plan, now):
    """Build the sorted target list and the atomic mutator callback.

    Returns (targets, mutator_fn, result_ref).
    """
    write_header = plan["write_header"]
    new_header = plan["new_header"]
    holds = plan["holds"]
    represented = plan["represented"]
    old_row_valid = plan["old_row_valid"]

    # Build sorted unique target list
    target_set = set(pre_reads.keys())
    targets = sorted(target_set)
    target_index = {t: i for i, t in enumerate(targets)}

    result = [None]

    def mutator(current_values):
        # Step 1: marker check
        marker_idx = target_index[
            (FILECRYPT_LIFECYCLE_MIGRATIONS_TABLE, FILECRYPT_MIGRATION_KEY)
        ]
        marker_curr = current_values[marker_idx]
        marker_decoded = decode_migration_marker(marker_curr)
        if marker_decoded is not None:
            result[0] = _RESULT_ALREADY
            return current_values
        if marker_curr is not None:
            result[0] = _RESULT_UNAVAILABLE
            return current_values

        # Step 2: exact-compare every pre-read against current
        for target, pre_val in pre_reads.items():
            idx = target_index[target]
            if current_values[idx] != pre_val:
                result[0] = _RESULT_CONFLICT
                return current_values

        # Step 3: validate existing lifecycle header/link-states inside callback
        hdr_idx = target_index[(FILECRYPT_SWEEP_STATE_TABLE, FILECRYPT_SWEEP_KEY)]
        hdr_curr = current_values[hdr_idx]
        if hdr_curr is not None:
            if decode_sweep_header(hdr_curr) is None:
                result[0] = _RESULT_UNAVAILABLE
                return current_values

        for fp in holds:
            key = (FILECRYPT_LINK_STATES_TABLE, fp)
            if key in target_index:
                ls_curr = current_values[target_index[key]]
                if ls_curr is not None and decode_link_state(ls_curr) is None:
                    result[0] = _RESULT_UNAVAILABLE
                    return current_values

        # Build new values
        new_values = list(current_values)
        held_count = 0
        packages_cleaned = 0

        # Write lifecycle header if needed
        if write_header:
            new_values[hdr_idx] = encode_sweep_header(new_header)

        # Write held link-state rows for proven fingerprints
        for fp, hold_data in holds.items():
            key = (FILECRYPT_LINK_STATES_TABLE, fp)
            idx = target_index[key]
            existing_raw = current_values[idx]
            if existing_raw is not None:
                # Preserve existing valid state (never downgrade)
                existing = decode_link_state(existing_raw)
                if existing is not None:
                    continue
            # Write new held row
            new_ls = {
                "schema_version": 1,
                "state": "held",
                "first_blocked_epoch": hold_data["since_epoch"],
                "retry_after_epoch": hold_data["retry_after_epoch"],
                "lease": None,
            }
            new_values[idx] = encode_link_state(new_ls)
            held_count += 1

        # Determine which fingerprints are now "represented" in lifecycle
        # (held, blacklisting, or blacklisted exist in lifecycle)
        all_lifecycle_fps = set()
        for fp in holds:
            key = (FILECRYPT_LINK_STATES_TABLE, fp)
            idx = target_index[key]
            raw = new_values[idx]
            if raw is not None:
                ls = decode_link_state(raw)
                if ls is not None:
                    all_lifecycle_fps.add(fp)

        # Also check pre-existing link states not in holds target set
        for target_key in target_index:
            if target_key[0] == FILECRYPT_LINK_STATES_TABLE:
                fp = target_key[1]
                idx = target_index[target_key]
                raw = new_values[idx]
                if raw is not None:
                    ls = decode_link_state(raw)
                    if ls is not None:
                        all_lifecycle_fps.add(fp)

        # Clean represented package defers
        for pkg_id, pkg_fps in represented.items():
            key = ("protected", pkg_id)
            if key not in target_index:
                continue
            idx = target_index[key]
            pkg_raw = current_values[idx]
            pkg_data = _parse_package(pkg_raw)
            if pkg_data is None:
                continue
            if "deferred" not in pkg_data:
                continue
            # Check if all fingerprints are either in lifecycle OR the defer is expired
            try:
                deferred = decode_package_defer(pkg_data)
            except (TypeError, ValueError):
                continue
            if deferred is None:
                continue
            defer_expired = deferred.get("retry_after_epoch", 0) <= now
            all_represented = pkg_fps.issubset(all_lifecycle_fps)
            if not all_represented and not defer_expired:
                continue
            # Remove only 'deferred' key, preserve everything else
            cleaned = {k: v for k, v in pkg_data.items() if k != "deferred"}
            new_values[idx] = json.dumps(cleaned, separators=(",", ":"), sort_keys=True)
            packages_cleaned += 1

        # Remove valid old Filecrypt decision row (preserve malformed)
        old_key = ("crypter_cooldowns", _FILECRYPT_CRYPTER)
        old_idx = target_index[old_key]
        if old_row_valid:
            new_values[old_idx] = None
        # else: malformed old row -> preserve (leave unchanged)

        # Write migration marker last
        new_values[marker_idx] = encode_migration_marker(
            {"schema_version": 1, "completed_epoch": now}
        )

        global_cooldown = write_header
        result[0] = {
            "status": "complete",
            "held": held_count,
            "packages_cleaned": packages_cleaned,
            "global_cooldown": global_cooldown,
        }
        return tuple(new_values)

    return targets, mutator, result
