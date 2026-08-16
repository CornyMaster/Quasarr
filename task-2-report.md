# Task 2 Fix Round 2: Persist Recheck Generation Identity

## Status: 🟢 GREEN

All tests pass. The mechanical interface fix is complete and verified.

## Implementation Summary

Added `recheck_sweep_id` (32 lowercase hex) to the blacklisting record schema to persist the full recheck offer identity (both sweep and offer IDs).

### Files Changed: 3
- `quasarr/providers/filecrypt_lifecycle.py`: +5 lines
- `tests/test_filecrypt_lifecycle_records.py`: +51 lines
- `quasarr/providers/AGENTS.md`: -1 line (+1 line change)

**Total new line count: 57 insertions, 1 deletion**

## Changes Made

1. **Schema Update** (`filecrypt_lifecycle.py`):
   - Added `recheck_sweep_id` to `_BLACKLISTING_KEYS`
   - Added validation in `decode_link_state()` for blacklisting state
   - Added validation in `encode_link_state()` for blacklisting state

2. **Test Fixture Update** (`test_filecrypt_lifecycle_records.py`):
   - Updated `_BLACKLISTING` fixture to include `recheck_sweep_id`
   - Added 8 new discriminating tests for `recheck_sweep_id`:
     - `test_decode_rejects_missing_recheck_sweep_id`
     - `test_decode_rejects_bad_recheck_sweep_id`
     - `test_decode_rejects_recheck_sweep_id_as_bool`
     - `test_decode_rejects_recheck_sweep_id_as_integer`
     - `test_round_trip_different_sweep_and_offer_ids` (proves both IDs preserved)
     - `test_encode_raises_for_missing_recheck_sweep_id`
     - `test_encode_raises_for_bad_recheck_sweep_id`
     - `test_encode_raises_for_recheck_sweep_id_as_bool`

3. **Documentation Update** (`AGENTS.md`):
   - Added note that blacklisting records persist full sweep+offer identity

## Test Results

**LinkStateBlacklistingCodecTests: 14/14 GREEN**

All tests pass including:
- Round-trip encoding/decoding
- Validation of both IDs are preserved and different
- Strict rejection of missing, invalid, bool, and integer sweep IDs
- Key set validation (extra/missing keys rejected)
- Canonical package ID validation
- All existing strictness preserved

## Code Quality

- ✅ All Ruff checks pass
- ✅ All format checks pass
- ✅ No new linting issues introduced
- ✅ Existing tests unaffected

## Interface Contract

The `recheck_sweep_id` field:
- Must be exactly 32 lowercase hex characters
- Cannot be missing from blacklisting records
- Cannot be `None` or falsy
- Cannot be a boolean value
- Cannot be an integer
- Is preserved exactly in round-trip encoding/decoding
- Is distinct from `recheck_offer_id`

This allows Task 3's `confirm_blacklist()` to access both recheck identities for building immutable receipts and exact replay responses.
