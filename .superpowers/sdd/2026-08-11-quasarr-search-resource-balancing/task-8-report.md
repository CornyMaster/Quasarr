# Task 8 Report: Quiet-Period Memory Reclamation

Plan: `docs/superpowers/plans/2026-08-11-quasarr-search-resource-balancing.md`
Brief: `.superpowers/sdd/2026-08-11-quasarr-search-resource-balancing/task-8-brief.md`
Worktree: `.worktrees/resource-balancing` on `feature/resource-balanced-search`
Base: `7daee0e7110630b6d4f2ceb6247d81e92f4303f6`
Commits:

- `3022da25c56af19930c061fc3f2c00263cf0d404` "Reclaim expired search memory after idle periods"
- `848ba66d3b0e35a696cbc68fd9ed627d79b43121` "Rate-limit failed search reclamation attempts"
- `422246d` "Harden idle memory reclamation"

Status: **DONE**

## Summary

Task 8 adds an optional, process-local quiet-period memory reclaimer without enabling it in production.

- `IdleMemoryReclaimer` registers with a supplied `SearchRuntime`; any request, source task, or overdue token cancels its timer, and a transition back to all three gauges at zero arms one daemon timer after 30 quiet seconds.
- The timer callback revalidates uninterrupted quiet, sweeps expired cache entries, runs `gc.collect()`, optionally calls glibc `malloc_trim(0)`, and records PSS before/after in one fixed-cardinality summary.
- Reclaim passes run at most once per 300 seconds. Activity that starts and finishes during the sweep invalidates that pass by generation and starts a fresh 30-second quiet period instead of being hidden by gauges returning to zero.
- The native trim probe is Linux-only, loads `libc.so.6` with `use_errno=True`, requires `malloc_trim`, assigns `argtypes`/`restype`, and permanently caches every unsupported/failed probe as a no-op.
- Importing `quasarr.search` only re-exports the class and trim function. No production `IdleMemoryReclaimer(...)` instance exists, so rollout remains canary-gated exactly as required.

No worker process, Playwright, Selenium, Linkcrypter, source module, category plan, source eligibility rule, cache TTL, cache limit, deadline, or caller contract changed.

## Files

| File | Change |
| --- | --- |
| `quasarr/search/reclaim.py` | Injected scheduler, arm-time generation claim, fixed summary, contained timer failures, GC pass, and permanently cached Linux/glibc trimmer. |
| `quasarr/search/runtime.py` | Idle-state query and optional request/source/overdue callbacks, now invoked after releasing a non-reentrant transition lock. Existing snapshot keys are unchanged. |
| `quasarr/search/cache.py` | Documented that `sweep()` releases its lock before returning; behavior is unchanged. |
| `quasarr/search/__init__.py` | Explicitly re-exported `IdleMemoryReclaimer` and `trim_native_heap`; no singleton is constructed. |
| `tests/test_search_memory_reclaim.py` | 30-test hermetic scheduler/platform/ordering/locking/failure/metrics suite using injected clocks, timers, GC, native trim, libc loader, and memory reader. |
| `quasarr/search/AGENTS.md` | Recorded ownership, activation gate, quiet/rate limits, platform behavior, lock boundary, and identifier-free summary contract. |

## Strict TDD Evidence

### Initial RED

The complete Task 8 test module was added before production code.

```powershell
uv run python -X utf8 -m unittest tests.test_search_memory_reclaim -v
```

Failed for the intended missing feature:

```text
ModuleNotFoundError: No module named 'quasarr.search.reclaim'
Ran 1 test
FAILED (errors=1)
```

After the initial implementation, all 17 original tests passed.

### Concurrency self-review RED

Review found that activity could start and finish inside `cache.sweep()`. All gauges would be zero again at the next check, allowing GC without a new 30-second quiet period and losing the activity-end schedule while `_collecting` was true.

A deterministic test drives a real `SearchRuntime.request()` transition from inside the fake cache sweep:

```powershell
uv run python -X utf8 -m unittest tests.test_search_memory_reclaim.IdleMemoryReclaimerTests.test_activity_during_sweep_aborts_and_restarts_the_quiet_period -v
```

Before the fix:

```text
AssertionError: True is not false
Ran 1 test
FAILED (failures=1)
```

The implementation now captures an activity generation for every pass, increments it on every activity start, and defers a fresh schedule requested while collection is in progress. The exact regression then passed, followed by all 18 reclaimer tests.

## Implementation

### Runtime transitions

`SearchRuntime` owns one optional reclaimer reference. Request and source entry plus overdue-token creation call `cancel_for_activity()` under the existing runtime transition lock. Their successful return-to-zero paths call `schedule_if_quiet()`. Callback exceptions are contained so memory maintenance can never fail a search response.

`is_idle()` reads only the three gauges under the runtime lock. The existing `snapshot()` key set and all fixed counters remain unchanged.

### Quiet scheduler

The injected monotonic clock controls both the 30-second quiet delay and 300-second minimum interval. One opaque timer token prevents a cancelled/stale callback from clearing or running a replacement timer. Timers are daemon threads.

The callback checks both current gauge state and the captured activity generation before the PSS read, after it, and after cache sweep. If activity is observed, it exits before GC/trim. An activity-end callback received during a pass is retained as a deferred schedule and arms a new timer after `_collecting` clears.

### Reclaim pass and metrics

The pass calls `cache.sweep()` only; it never calls `clear()` and therefore never removes a valid entry. `sweep()` returns after releasing the cache lock. Runtime checks also return before GC and native trim, and tests prove another thread can acquire both locks from inside the injected collector and trimmer.

Every attempted pass returns/logs exactly these fields:

```text
performed
failed
expired_cache_entries
gc_collected
native_heap_trimmed
pss_before_kib
pss_after_kib
```

Only bounded booleans, integers, or `None` are accepted. Extra memory-reader fields are ignored, and the log contains no source initial, hostname, URL, query, title, category, or cache key.

### Native trim

`_NativeHeapTrimmer` is instantiated once for the public `trim_native_heap()` function. Its first call:

1. rejects non-Linux platforms without loading libc;
2. calls `ctypes.CDLL("libc.so.6", use_errno=True)` on Linux;
3. rejects a missing `malloc_trim` attribute;
4. sets `[ctypes.c_size_t]` and `ctypes.c_int` as the function signature;
5. calls `malloc_trim(0)` outside cache/runtime locks.

Every failed/unsupported probe is cached as `None` and never retried. Other platforms still run cyclic GC.

## Verification

Authoritative final Linux command used the existing read-only worktree mount and `quasarr-venv-rb` volume. It captured each component exit and returned success only when all were zero:

```text
Ran 472 tests in 15.689s
OK
All checks passed!
185 files already formatted
GATE_STATUS tests=0 lint=0 format=0
```

Additional evidence:

| Gate | Result |
| --- | --- |
| Initial reclaimer GREEN | 17 tests / OK |
| Activity-during-sweep regression | RED on `performed=True`, then GREEN |
| Failed-attempt rate-limit review regression | RED on a second immediate failed attempt, then GREEN |
| Final reclaimer module | 19 tests / OK |
| Final reclaimer + runtime + cache | 68 tests / OK |
| Focused `ruff check quasarr/search tests/test_search_memory_reclaim.py` | All checks passed |
| Focused Ruff format check | 5 files already formatted |
| Editor diagnostics for all changed Python files | no errors |
| Production constructor search | no production call; only test, DOX, and brief references |
| `git diff --cached --check` | silent |
| Initial staged set | exactly the six Task 8 files |
| Review-fix staged set | exactly `reclaim.py` plus its regression test |
| Post-commit worktree | clean |

One earlier aggregate Podman wrapper reported code 1 despite all three summaries succeeding, and one diagnostic rerun was externally interrupted. Neither was accepted as evidence. The final explicit-status Linux command above is the authoritative gate.

## Review Fix Round 1/5

The task-scoped rubric review found one Important spec gap: `_last_collection_at` advanced only after a successful cache sweep. A failing sweep was logged, but a subsequent activity cycle could schedule another attempt after 30 quiet seconds instead of respecting the required 300-second minimum interval.

Strict TDD reproduced it with a cache whose `sweep()` raises:

```text
test_a_failed_reclaim_attempt_obeys_the_five_minute_rate_limit ... FAIL
AssertionError: True is not false
```

The second immediate `collect_and_trim()` returned `failed=True`, proving the failed attempt retried instead of being limited. The exception path now records the attempt timestamp under the reclaimer lock before returning its fixed failure summary. The regression proves the cache is called once and a schedule one second later waits 299 seconds.

Final review-fix evidence: 19 reclaimer tests, 68 focused integration tests, 472 Linux tests, repository-wide Ruff lint, and repository-wide Ruff format all pass. Addressed: 1 Important. Open: 0.

## DOX

`quasarr/search/AGENTS.md` now owns the reclaimer module and records the explicit construction/activation gate, all-zero trigger, 30-second quiet period, 300-second minimum interval, expired-only sweep, all-platform GC, Linux/glibc-only trim, permanent failed-probe no-op, lock release, and fixed identifier-free PSS summary.

Root, package, source, provider, API, and tests DOX were reviewed and intentionally left unchanged. The durable behavior is wholly owned by the search subsystem; the test framework and test-wide workflow did not change.

## Self-Review And Concerns

- Traced timer creation, duplicate scheduling, activity cancellation, stale callbacks, direct early collection, rate-limit delay, request/source/overdue gating, sweep-time activity, failed libc loading, missing symbols, successful trim calls, and non-Linux GC-only behavior.
- Verified the generation and deferred-schedule locks follow runtime-to-reclaimer ordering; the timer callback releases its own lock before consulting runtime, so it cannot invert those locks.
- Verified PSS reads, GC, and native trim cannot hold cache or runtime locks.
- Verified no valid cache entry is cleared and the existing search/feed TTLs remain 300/60 seconds.
- Verified no production instance is constructed. Task 9/operator canary remains the activation decision.
- claude-mem index lookup timed out twice; this task used repository memory, the supplied ledger/brief/reports, the approved plan/design, and current code.
- No blocking concerns.

## Review Fix Round 2/5

Independent review found one real timer handoff race plus activation hardening requirements. Commit `422246d` addresses the full round without constructing a production reclaimer.

### Strict RED

The regression tests were added before production changes and run with:

```powershell
uv run --directory <resource-balancing-worktree> python -X utf8 -m unittest tests.test_search_memory_reclaim -v
```

The current implementation failed in the intended places:

```text
Ran 30 tests in 0.018s
FAILED (failures=2, errors=5)
```

- The callback handoff test observed `['sweep', 'gc', 'trim']` after activity started and finished in the token-to-collection window; it expected no reclaim work.
- The timer-start lock probe observed `(False, False)` for runtime and reclaimer lock availability; it expected `(True, True)`.
- Synthetic callback-clock, deferred-rearm timer-factory, and timer-start failures escaped their timer path.
- A non-`OSError` libc loader failure and a normal signature-assignment failure escaped the native probe.

Three request/source/overdue tests for raising reclaimer callbacks were already green in the RED run, proving `ba32ee8` contained those exceptions. The lock-free handoff preserves that behavior and adds fixed debug messages. The trim-call failure regression was also already green because `ba32ee8` already cached a call failure as `None`; the test now protects that required behavior. No failing test was manufactured for behavior that was already correct.

### GREEN And Gates

After the fix, the exact reclaim module passed:

```text
Ran 30 tests in 0.008s
OK
```

The adjacent reclaim/runtime/cache slice passed:

```text
Ran 79 tests in 0.024s
OK
```

The authoritative read-only Linux Podman gate ran frozen dependency sync, the full unit suite, repository Ruff lint, and repository Ruff format:

```text
Ran 483 tests in 17.053s
OK
All checks passed!
185 files already formatted
```

Focused Ruff lint passed, focused Ruff format reported all three touched Python files formatted, editor diagnostics reported no errors, `git diff --check` was silent, and a production constructor search found only the explicit activation example in search DOX.

### Review Fix Implementation

- A timer token now closes over the activity generation captured when that arm is reserved. The callback validates token and generation while holding the reclaimer lock and atomically claims `_collecting` before releasing it. Existing generation checks before/after PSS and sweep remain intact. Activity in the old handoff window invalidates the pass and the deferred activity-end schedule arms a fresh 30-second timer.
- `SearchRuntime` now uses `threading.Lock`, updates transition state under that lock, captures the optional reclaimer, and invokes cancel/schedule callbacks only after release. `schedule_if_quiet()` reserves one token under the reclaimer lock but calls `timer.start()` only after both runtime and reclaimer locks are free. Stale callbacks and failed starts use identity-checked cleanup so they cannot clear a replacement timer.
- Raising transition callbacks remain contained for request, source, and overdue paths. Fixed debug messages contain no source, query, URL, category, hostname, title, cache key, or exception text.
- Normal timer scheduling, start, cancel, callback, and deferred-rearm failures are contained with `except Exception`, never `BaseException`. Failed token/start state is cleared, leaving a later quiet transition schedulable.
- Linux libc loading, `malloc_trim` lookup, and ctypes signature assignment now share a normal-`Exception` permanent no-op path. Trim-call failure remains permanently cached. The `argtypes = [ctypes.c_size_t]`, `restype = ctypes.c_int`, cast, and zero-padding call are unchanged.

### DOX And Remaining Concerns

`quasarr/search/AGENTS.md` now owns the arm-generation claim, non-reentrant runtime lock, post-lock callback/start boundary, fixed failure diagnostics, and permanent native no-op contract. Root/package/tests DOX were reviewed and intentionally left unchanged because structure and test workflow did not change.

No blocking concern remains. Production activation is still intentionally absent and remains a later canary decision.