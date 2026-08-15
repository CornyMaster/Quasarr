# integration_tests/ - Cross-Repository End-To-End Harness

## Purpose

The only suite that runs Quasarr and a second repository in one process. It exists because the Filecrypt cohort protocol is a contract between two products: the parts each side owns are already pinned by its own unit suite, and what is left to prove is that a real SponsorsHelper loop and the real Quasarr routes agree about a whole sweep.

## Ownership

`test_*.py` files in this folder. `tests/` owns everything that Quasarr can prove alone; `cli_tester.py` at the repo root owns manual work against a RUNNING instance over real HTTP.

## Local Contracts

- Deliberately NOT under `tests/`: `uv run python -X utf8 -m unittest discover -s tests` must never collect these, because they cannot import without the companion repository on `PYTHONPATH`.
- Both sides are the shipped ones. The helper's HTTP client is doubled, but the double dispatches into the real Bottle callbacks in-process against a real SQLite file; neither state machine may be reimplemented and no recorded response may stand in for a route.
- No socket, no protected host, no JDownloader, no CAPTCHA service. Synthetic `.invalid` values only, per the root synthetic-data rule.
- Only these may be doubled: the clock, the identity factory, the durable state root, the CAPTCHA-handler status, the JDownloader device, and the Filecrypt adapter - and the adapter produces its verdicts through the companion repository's own shipped classifier rather than hand-written payloads.
- A lost answer is injected AFTER the routes ran and committed: the harness discards the response and raises, so every retry can only arrive as a replay of work Quasarr already did. Failing before dispatch would prove nothing, because the first attempt would never have reached a route.
- The trace carries a `complete` event once Quasarr has no handout left, and only then may the companion repository's oracle judge an unfinished re-test queue as an omission rather than a truncated run.
- Both repositories configure the one global `loguru` logger while importing, each removing the default sink by id and registering the same custom levels. In production they are separate processes; here the second importer would raise, so the harness bridges exactly that logger setup for the duration of the import and nothing else.
- `test_filecrypt_cohort_e2e.py` covers five all-blocked members reaching a cooldown, a CLEAR as the third result with the exact retest queue it owes, a lost and retried CLEAR acknowledgement that survives a restart, and terminal cleanup after exactly one JDownloader submission. It finally hands its trace to the companion repository's independent contract oracle, loaded by path from `rix`.

## Work Guidance

- Same conventions as `tests/`: stdlib `unittest` only, `<Subject>Tests(unittest.TestCase)`, exact-equality assertions on whole shapes and recorded call sequences.
- Add a case here only when a single repository genuinely cannot prove it; everything else belongs in `tests/`.

## Verification

Run inside the SponsorsHelper image with both worktrees mounted read-only:

```
podman run --rm --entrypoint sh \
    -v "<quasarr>:/quasarr:ro" -v "<sponsorshelper>:/sponsorhelper:ro" \
    -w /quasarr -e PYTHONPATH=/quasarr:/sponsorhelper \
    sponsorhelper:cohort-final \
    -lc "pip install --no-cache-dir /quasarr && python3 -X utf8 -m unittest integration_tests.test_filecrypt_cohort_e2e -v"
```

## Child DOX Index

None.
