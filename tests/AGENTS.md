# tests/ - Unit Test Suite

## Purpose

Hermetic unit tests for Quasarr, built exclusively on the standard-library `unittest` framework. Covers download-link extraction, search-source behavior, search runtime instrumentation and resource-load bounds, the userscript CAPTCHA routing, mirror filtering, download orchestration, notifications, SponsorsHelper helpers, release matching, and the SQLite layer.

## Ownership

`test_*.py` files in this folder. `cli_tester.py` at the repo root is NOT part of this suite - it is a separate interactive/scriptable end-to-end client that exercises a RUNNING Quasarr instance over real HTTP against real configured sources; never confuse the two.

## Local Contracts

- Framework: stdlib `unittest` only - no pytest, no conftest, no coverage tooling, no test config in `pyproject.toml`. Classes are `<Subject>Tests(unittest.TestCase)`, files `test_*.py`, methods `test_*`, each file ends with an `if __name__ == "__main__": unittest.main()` block.
- Full-suite command: `uv run python -X utf8 -m unittest discover -s tests` (the `-X utf8` flag avoids Windows console encoding noise in log output).
- Tests must not perform network I/O or touch JDownloader. Patch in the consuming module's namespace (e.g. `quasarr.downloads.sources.<xx>.requests.Session`), not the `requests` library globally. Only the storage-layer tests (`test_sqlite_database.py`, `test_config_secret_decryption.py`, `test_config_atomic_writes.py`) touch disk, via `tempfile.TemporaryDirectory`.
- Synthetic-data rule (security-critical): source hostnames in tests are fake domains on the reserved `.invalid` TLD; use synthetic release titles (never paste real ones). Real public hoster/crypter domains are permitted only where the production matching logic keys on those literal domains - they are hoster/crypter services, not protected sources.
- `shared_state` is always faked (MagicMock with a `.values` dict, SimpleNamespace, or a small local class whose `values["config"]` is a callable returning dicts) - except the storage-layer tests above, which mutate the real module in `setUp`.
- Tests that run the real `SearchExecutor` with synthetic sources patch `quasarr.search.search_singleflight` to a fresh `SearchSingleFlight()` for each test, preferably from `setUp` with `ExitStack`. Never clear the process-global registry while work is live; leader callbacks own flight removal.
- There is no fixtures directory and no shared test-helpers module: each file defines its own `FakeResponse`/`FakeSession`/fake shared_state inline.
- Run the full suite after touching shared providers, download flow, search behavior, or notification logic. Per root change discipline, tests change only when the intended behavior in the covered area changed or the existing test is incorrect.
- `test_search_runtime.py` covers `quasarr/search/runtime.py`: it builds `SearchRuntime` with an injected clock and memory reader (never the module singleton's real ones), and asserts the snapshot key set exactly, so a counter carrying a source initial, query, or category ID fails the suite. The `/proc` reader is driven through `patch("quasarr.search.runtime.open", ...)` with in-memory file content, so no test reads a real `/proc` path or any other file.
- `test_search_resource_load.py` is the offline Task 9 burst gate. Its single `_run_burst(config)` drives the real Newznab category planner, source eligibility, `SearchExecutor`, bounded cache, process-global singleflight shape, worker budget, and overdue callback with synthetic doubles and `.invalid` URLs; events force overlap and timeout, and every wait/join has a five-second failure bound instead of a sleep. The burst also writes 2,049 minimal entries through `SearchCache.set()`: production must evict to at most 2,048 entries and 50,000 releases, while the deliberately unbounded configuration must behaviorally retain more than the production entry bound. The timeout control records the real drop/overdue counters and samples `overdue_drained.is_set()` at the grace measurement before cleanup; production must have drained, while the deadline-ignoring configuration must still hold its overdue token. Production defaults must yield no `_bound_violations(...)`; the permanent unbounded/no-budget/deadline-ignoring configuration must yield behavioral cache, budget, and overdue violations and then drain every harness-owned thread during test cleanup. The test never uses network I/O, real source values, the live deployment, or `IdleMemoryReclaimer`.

## Work Guidance

- Parameterized cases use `self.subTest(...)`; many simultaneous patches use `contextlib.ExitStack` or the parenthesized multi-context `with` form.
- Tests may reach into private underscore-prefixed helpers freely.
- Document behavioral intent with comments inside tests - explain WHY a rule/ordering exists (see `test_wx_direct_links.py`).
- Prefer exact-equality assertions on whole result shapes and on recorded request sequences.

## Verification

- `uv run python -X utf8 -m unittest discover -s tests`

## Child DOX Index

None.
