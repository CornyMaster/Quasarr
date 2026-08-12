# SDD ledger — plan: docs/superpowers/plans/2026-08-11-quasarr-search-resource-balancing.md
Setup: worktree `.worktrees/resource-balancing` on `feature/resource-balanced-search`, base `945f302`.
Baseline: Linux container ran 329 tests with 2 skips; Ruff passed.
Baseline note: native Windows suite has pre-existing spawn/os.replace/SQLite temp-file lock failures; verification uses the exact repo commands in Linux containers matching deployment.
Preflight: Claude Opus 5 plan review completed; no open plan conflicts; Playwright is out of scope and deferred.
Task 1: fix round 1/5 (8 addressed, 0 open; commits 196a5ac..e19196e).
Task 1: complete (commits 945f302..e19196e, review clean).
Task 2: review needs fixes (Important: Ruff format gate red; telemetry emitted at INFO instead of DEBUG).
Task 2: minor (deferred): global deltas under concurrency; redundant create=True patches; identifier assertion could use exact keys; dropped-vs-started semantics need note; API exception-path test gap.
Task 2: fix round 1/5 (2 addressed, 0 open; commits 26d0b26..7e673c1).
Task 2: complete (commits e19196e..7e673c1, review clean; 5 deferred minors).
Task 3: review needs fixes (Important: mutable result alias bypasses release cap; post-deadline/partial results can be cached).
Task 3: minor (deferred): runtime singleton patch aliases; cache concurrency test does not propagate worker failures.
Task 3: fix round 1/5 (2 addressed, 1 new open — eligible result is cached twice; commits 8c55057..296de6a).
Task 3: fix round 2/5 (1 addressed, 0 open; commits 296de6a..cf63bca).
Task 3: complete (commits 7e673c1..cf63bca, review clean; 2 deferred minors).
Task 4: review needs fixes (Important: process-global singleflight registry is not isolated across tests sharing keys).
Task 4: minor (deferred): cancelled shared future follower degrades to timeout/error and DOX overstates queue invariant; wedged flight poisons key; same-key duplicate badge/accounting; live result dict alias invariant; create=True patch nit.
Task 4: fix round 1/5 (1 addressed, 0 open; commits 87f1335..980f3be).
Task 4: complete (commits cf63bca..980f3be, review clean; 5 deferred minors).
Task 5: review needs fixes (Important: clamp_timeout can exceed remaining deadline via 100ms floor).
Task 5: minor (deferred): SourceTaskOutcome/_Completion type annotations; context reset tests do not prove same worker thread reuse.
Task 1: DONE_WITH_CONCERNS - `196a5ac` "Add search runtime instrumentation"; 21 new tests, Linux suite 350 OK, Ruff clean; report `task-1-report.md`.
Task 2: self-review fix round 1/5 (runtime-owner callback leak reproduced RED and fixed; 0 open).
Task 2: complete (commit `26d0b26`; focused 20 OK; Linux suite 357 OK; full Ruff clean).
Task 2: DONE - `26d0b26` "Instrument search request and source lifecycles"; report `task-2-report.md`.
Task 3: DONE - `8c55057` "Bound the search result cache"; 19 new tests, focused 39 OK, Linux suite 376 OK, Ruff check + format clean; report `task-3-report.md`.
Task 3: fix round 1/5 (2 Important + 1 deferred Minor addressed, 1 deferred Minor rejected with rationale, 0 open).
Task 5: DONE - `f4cb92c` "Propagate cooperative search budgets to source workers"; 21 new tests, focused 45 OK, Linux suite 417 OK, Ruff check + format clean; report `task-5-report.md`.
Task 5: fix round 1/5 (1 addressed, 0 open; commits f4cb92c..1d59bd2).
Task 5: complete (commits 980f3be..1d59bd2, review clean; 2 deferred minors).
Task 6: production review clean; minor (deferred): AST contract is not flow-sensitive.
Task 6: branch blocker discovered — pre-existing Task 4 follower-timeout test is timing-flaky under full-suite load (base reproduction 1/30); deterministic test fix required before further gates.
Task 4: branch follow-up complete (commit 5870880; deterministic follower-timeout test passed 100 repetitions and full suite; review clean).
Task 6: complete (commit 111b01f; production review clean; 1 deferred minor).
Task 7: review needs fixes (Important: FF drops collected releases when clamp raises; DD fake response can leak collected releases after budget exhaustion).
Task 7: minor (deferred): IMDb fallback swallows budget under broad except; stale FlareSolverr timeout DOX sentence; AST coverage limits; unbudgeted session-login helpers.
Task 7: fix round 1/5 (3 addressed, 0 open; commits 604b7be..7daee0e).
Task 7: complete (commits 5870880..7daee0e, review clean; 2 remaining deferred minors: AST coverage limits, session-login helper scope).
Task 6: DONE_WITH_CONCERNS - `111b01f` "Stop direct source requests when search budgets expire"; 12 direct-request sources adopt checkpoint()+clamp_timeout() at every request boundary, catch SearchBudgetExhausted at the outer boundary; 7 new tests (AST contract + fake-response behavior), focused 41 OK, Linux suite 433 OK, Ruff check + format clean; report `task-6-report.md`.
Task 6: concern (not caused by Task 6) - `tests.test_search_singleflight.test_follower_timeout_never_cancels_the_running_leader` is a timing flake; reproduced 1/30 on the unmodified base commit via `git archive` export. Task 4 follow-up.
Task 6: deviation - `tests/test_date_source_queries.py` and `tests/test_wx_direct_links.py` left unmodified (behavior unchanged, both pass and were run as regression gates); no checkpoint added to pure in-memory parse loops (no I/O, and their inner `except Exception` would swallow it).
Task 4 follow-up: RED confirmed explicit timeout-controller ownership; stress run 95/100 then exposed nondeterministic leader/follower handle recording, and the focused suite exposed assertions outrunning leader recording.
Task 4 follow-up: DONE - `5870880` "Make singleflight follower timeout test deterministic"; test-only event-driven clock, explicit follower timeout, leader-recorded synchronization, and role-based exact cardinality; exact scenario 100/100, focused 24 OK, Linux suite 433 OK, Ruff check + format clean; report `task-4-report.md`.
Task 7: fix round 1/5 (2 Important + 2 localized minors addressed, 0 open; FF clamp escape, DD fake-release leak, IMDb solver-fallback swallow, stale FlareSolverr DOX promise; 4 new tests, focused 39 OK, Linux suite 453 OK, Ruff check + format clean).
Task 8: DONE - `3022da2` "Reclaim expired search memory after idle periods"; optional canary-gated quiet reclaimer, runtime transition hooks, cached Linux/glibc trim probe, fixed PSS summary, and 18 hermetic tests; report `task-8-report.md`.
Task 8: rubric review needs fix (Important: a failed reclaim attempt did not advance the required five-minute rate limiter; independent subagent dispatch unavailable in this VS Code session).
Task 8: fix round 1/5 (1 addressed, 0 open; commit `848ba66`; failed-attempt RED/GREEN, 19 reclaimer tests, focused 68 OK, Linux suite 472 OK, Ruff check + format clean; scoped re-review clean).
Task 8: complete (commits `7daee0e..848ba66`, task-scoped rubric and scoped re-review clean; no deferred findings).
Task 8: independent review needs fixes (Important: arm-generation handoff race; activation hardening: lock-free timer start/callback handoff, transition callback containment tests, timer failure containment, permanent native probe no-op).
Task 8: fix round 2/5 (5 areas addressed, 0 open; commit `422246d`; strict RED 30 tests with 2 failures + 5 errors, GREEN 30 OK, focused 79 OK, Linux suite 483 OK, repository Ruff lint + format clean; no production constructor).
Task 8: complete (commits `7daee0e..422246d`; review round 2 resolved, no blocking concerns; production activation remains canary-gated).
Task 8: independent review round 3 needs fix (Important: a published but unstarted timer can hide the activity-end schedule and then be discarded; callback/manual-collection and callback/rate-limit token consumption can also leave idle dormancy; stale-token/generation, reclaimer-lock, and public-collect failure coverage need strengthening).
Task 8: fix round 3/5 (1 Important liveness finding + 4 local coverage gaps addressed, 0 open; commit `b235c8e`; strict exact-race RED `2 != 1`, callback RED 2 failures `2 != 1`, deferred-failure RED 2 failures `False is not true`; GREEN 34 reclaimer tests, focused 83 OK, Linux suite 487 OK, repository Ruff lint + format clean; no production constructor).
Task 8: complete (commits `7daee0e..b235c8e`; review round 3 resolved, no blocking concerns; failed timer creation/start records bounded pending work instead of retrying recursively; production activation remains canary-gated).
