# integration_tests/ - Cross-Repository End-To-End Harness

## Purpose

The only suite that runs Quasarr and a second repository in one process. It exists because the Filecrypt lifecycle protocol is a contract between two products: the parts each side owns are already pinned by its own unit suite, and what is left to prove is that a real SponsorsHelper loop and the real Quasarr routes agree about a complete lifecycle workflow.

## Ownership

`test_*.py` files in this folder. `tests/` owns everything that Quasarr can prove alone; `cli_tester.py` at the repo root owns manual work against a RUNNING instance over real HTTP.

## Local Contracts

- Deliberately NOT under `tests/`: `uv run python -X utf8 -m unittest discover -s tests` must never collect these, because they cannot import without the companion repository on `PYTHONPATH`.
- Both sides are the shipped ones. The helper's HTTP client is doubled, but the double dispatches into the real Bottle callbacks in-process against a real SQLite file; neither state machine may be reimplemented and no recorded response may stand in for a route.
- No socket, no protected host, no JDownloader, no CAPTCHA service. Synthetic `.invalid` values only, per the root synthetic-data rule.
- Only these may be doubled: the clock, the identity factory, the durable state root, the CAPTCHA-handler status, the JDownloader device, and the Filecrypt adapter - and the adapter produces its verdicts through the companion repository's own shipped classifier rather than hand-written payloads.
- A lost answer is injected AFTER the routes ran and committed: the harness discards the response and raises, so every retry can only arrive as a replay of work Quasarr already did.
- The trace carries a `complete` event once Quasarr has no handout left, and only then may the companion repository's oracle judge an unfinished re-test queue as an omission rather than a truncated run.
- Both repositories configure the one global `loguru` logger while importing; the harness bridges exactly that logger setup for the duration of the import.
- The offer trace event now includes a `capability` field (`filecrypt_link_lifecycle_v1` or `filecrypt_cohort_sweep_v1`) so the oracle can distinguish lifecycle from legacy cohort semantics.

## Test Coverage

Six new lifecycle workflow methods, three existing terminal-cleanup methods, and one updated oracle method:

1. `LifecycleSweepTests.test_500_blocked_no_cap_all_held_global_cooldown` — 500 blocked → unlimited denominator, all held, global cooldown
2. `LifecycleSweepTests.test_499_blocked_one_clear_no_global_cooldown` — 499 blocked + CLEAR prevents global cooldown; held states remain
3. `LifecycleLinkTests.test_untested_link_first_time_after_global_cooldown` — new fingerprint added during cooldown; first post-expiry offer is individual mode
4. `LifecycleLinkTests.test_first_blocked_recheck_clear_downloads_no_arr_failure` — retest CLEAR: deterministic retest mode, exact CLEAR ack, link neither held nor blacklisted after
5. `LifecycleLinkTests.test_first_blocked_second_blocked_terminal_blacklist_and_scrub` — second BLOCKED → blacklist ack, no helper /fail/, shared-owner scrub, empty-owner terminal, pre-blacklisted prevention
6. `LifecycleLinkTests.test_table_driven_recovery_and_config` — lost ack replay (exact terminal ID); sweep-window config: stored > ENV > default through real route

## Work Guidance

- Same conventions as `tests/`: stdlib `unittest` only, `<Subject>Tests(unittest.TestCase)`, exact-equality assertions on whole shapes and recorded call sequences.
- Add a case here only when a single repository genuinely cannot prove it; everything else belongs in `tests/`.

## Verification

Run inside the SponsorsHelper image with the Quasarr worktree mounted read-only:

```powershell
$helper='C:/Users/taalaco2/OneDrive - Swisscom/_git/_projects/sponsorhelper/clean_code_container_repo/.worktrees/filecrypt-link-lifecycle'
$quasarr='C:/Users/taalaco2/OneDrive - Swisscom/_git/_projects/sponsorhelper/Quasarr/.worktrees/stable-newznab-pubdates'
Set-Location $helper
$tag='sponsor-helper-run-'+(Get-Date -Format 'yyyyMMddHHmmss')
podman build --no-cache -t $tag .
podman run --rm --entrypoint sh -v "${quasarr}:/quasarr:ro" -w /quasarr -e PYTHONPATH=/quasarr:/app $tag -lc "pip install --no-cache-dir /quasarr >/dev/null && python3 -X utf8 -m unittest discover -s integration_tests -v"
```

## Child DOX Index

None.
