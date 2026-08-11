# quasarr/storage/ - Persistence

## Purpose

All persistent state: the INI config (`Quasarr.ini`) wrapped by `Config` with transparent AES encryption of secrets, the SQLite key/value store (`Quasarr.db`) wrapped by `DataBase`, cross-process file locking, category persistence, and the first-run setup flows.

## Ownership

- `config.py` - `Config(section)`; `_DEFAULT_CONFIG` is the authoritative schema
- `sqlite_database.py` - `DataBase(table)` key/value store + startup `maintain()`
- `lock.py` - cached cross-process FileLocks (`config`, `database`) in the OS temp dir; rejects nested locked storage calls while an atomic database mutator callback is active
- `categories.py` - download/search category persistence and validation
- `setup/` - see Child DOX Index
- `__init__.py` is intentionally empty

## Local Contracts

- Settings live in `Quasarr.ini` (Config); runtime/preference state lives in `Quasarr.db` (DataBase). Paths come from `shared_state.set_files(config_path)`.
- Every ini write goes through `_write_ini_atomically` (sibling temp file + fsync + `os.replace`): the file lock serializes read-modify-write, the atomic replace guarantees no observer (raw reader, backup tool, or a process killed mid-write) ever sees a truncated `Quasarr.ini`. Never write the ini with a plain `open(path, "w")`. `test_config_atomic_writes.py` pins this.
- `_DEFAULT_CONFIG` is the authoritative schema: `prune_unsupported_keys()` removes unsupported keys from sections known to `_DEFAULT_CONFIG` at startup (best-effort - skipped if the `.bak` backup fails, reverted if post-write verification fails); sections not in `_DEFAULT_CONFIG` are left untouched. Keys typed `secret` are AES-256-CBC encrypted with key/iv stored in SQLite table `secrets`; losing `Quasarr.db` means losing the ability to decrypt `Quasarr.ini` secrets. A secret that cannot be decrypted (an ini restored without its database) logs an error and reads as unset so setup asks for it again, and the stored ciphertext is left in place so restoring the matching `Quasarr.db` recovers the value - `get()` must never raise for it, because the Docker ENTRYPOINT would turn that into a restart loop. Reading can trigger a disk write (lazy re-encryption of plaintext secrets).
- The `Hostnames` section keys are generated from the search source module filenames - adding a source module automatically adds its hostname key. DJ/SJ share the single `JUNKIES` credentials section, and their `skip_login` flags are set/cleared together.
- `DataBase` tables are untyped `(key, value)` pairs created lazily; there is NO migration framework. `update_store()` is the upsert; `store()` is a plain INSERT and can duplicate keys; `retrieve_all_titles()` returns `None` (not `[]`) when empty. `maintain()` is tri-state at startup: False = corrupt → app exits; None = transient lock → continue; True = ok.
- `DataBase.mutate_value(key, mutator)` is the atomic read/decide/write primitive: one `BEGIN IMMEDIATE` transaction runs under the existing database file lock, reads the current string or `None`, invokes the side-effect-free callback exactly once, then upserts its string result or deletes on `None`. Any callback/write exception rolls back. The callback cannot enter another locked `Config` or `DataBase` call; collect settings and dependencies before invoking it.
- Lock ordering invariant (documented in the module docstrings, which must stay accurate): the config lock may be held while acquiring the database lock, never the reverse; `DataBase` methods must never call into `quasarr.storage.config`.
- Boolean flags in SQLite are stored as strings: `notification_settings`, `timeout_slow_mode`, and `filecrypt_enabled` store `'true'`/`'false'`; active `skip_login` and `skip_flaresolverr` preferences store only `'true'` and are cleared by deleting the row. Legacy `skip_radarr` / `skip_sonarr` rows are read during boot migration: a relevant skipped client warns, while both skipped clients reopen the required *arr setup.
- Category DB rows contain only mutable settings (mirrors / search_sources / name / base_type); static metadata like emoji lives in constants and is stripped from rows on read. Custom search category IDs are `100000 + base id`, max 10; download category names are lowercase alnum ≤ 20 chars, max 10 custom.
- Package-ID parsing: `get_download_category_from_package_id()` depends on the `Quasarr_{category}_{hash}` format from `constants.PACKAGE_ID_PATTERN`.

## Work Guidance

- Settings read from disk are pushed into shared_state (`notification_settings`, `timeout_slow_mode`, `filecrypt_enabled`) and consumers read shared_state, not disk - keep that refresh-then-cache pattern.

## Verification

- Targeted test: `test_sqlite_database.py`; full suite: `uv run python -X utf8 -m unittest discover -s tests`

## Child DOX Index

- `quasarr/storage/setup/AGENTS.md` - first-run setup flows: temporary-server pattern, startup order, skip-flag conventions
