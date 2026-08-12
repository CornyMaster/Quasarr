# quasarr/search/ - Search Side

## Purpose

The Newznab-facing search layer: `get_search_results()` fans a single *arr request (IMDb-ID search, phrase search, or feed pull) out in parallel across all discovered source modules, caches per-source results, merges/sorts/filters/paginates them, and returns releases whose `link` points back at Quasarr's own `/download/` endpoint with a base64 payload.

## Ownership

- `__init__.py` - orchestrator: the three search branches, `SearchExecutor` (thread-pool fan-out + per-source status badges)
- `cache.py` - `SearchCache` (bounded TTL/LRU result cache) and the `search_cache` singleton, both re-exported from `quasarr.search`
- `runtime.py` - process-local search instrumentation: `SearchRuntime`, the `search_runtime` singleton, `read_process_memory()`
- `sources/` - see Child DOX Index

## Local Contracts

- Per-source gating before dispatch: hostname configured, category in `supported_categories`, category whitelist from `get_search_category_sources`, `supports_imdb` for the imdb branch, `supports_phrase` for the phrase branch, `supports_absolute_numbering` when an episode is given without a season, and `supports_date_numbering` for Sonarr's year + `MM/DD` episode shape. The feed branch checks only hostname/category/whitelist.
- Movie searches and feeds require a cached Radarr client; TV searches and feeds require a cached Sonarr client. Missing clients stop before source dispatch with an error. Book and music phrase searches have no Arr-client gate. IMDb searches warm the metadata cache from the category's required client before fan-out.
- Date-numbered requests are parsed once into a validated `datetime.date`; regular `season`/`episode` are cleared before dispatch and proven sources receive only `episode_date`. Invalid calendar dates stay on the normal numbering path.
- The method names `search` and `feed` are load-bearing - dispatch is `getattr(source, action)`.
- The fan-out is capped by a deadline: `get_search_results` takes an optional absolute `deadline` and otherwise starts one at `SEARCH_FANOUT_DEADLINE_SECONDS`. Callers running several searches for one *arr request must pass their own so the runs share it. IMDb metadata warming and source dispatch are both skipped once it has passed, and sources still running when it passes are badged and dropped from that response, so an *arr client never waits past its own request timeout. The pool is sized to one worker per dispatched source, otherwise the sources dispatched last would spend the deadline queued behind the first ones. Sources must still bound their own work - a dropped source contributes nothing.
- `SearchExecutor.run_all()` wraps every submitted callable in `search_runtime.source_task()` and reports only the fixed outcomes: `completed`/`errored` when a future is collected, `skipped` before submission when no deadline remains, and `dropped` for a still-running future at timeout. A dropped future is never collected or cached later; it owns one opaque overdue token, and its done callback resolves exactly that token once while discarding the late result.
- `start_time` is taken before IMDb metadata warming: sources derive their own budget from it, so a later anchor would let them outlive the deadline by whatever the warming cost.
- Cache TTL is 300s for search, 60s for feed; the key nulls `start_time` and uses the cache-owner category. Cached entries skip execution entirely, so source methods must be safe to skip.
- `SearchCache` is bounded twice: at most `MAX_CACHE_ENTRIES` (2,048) entries and `MAX_CACHE_RELEASES` (50,000) retained releases, because entry count alone says nothing about size - one source can answer a broad query with thousands of releases. The bounds never shorten a TTL: an entry only leaves early when a limit is hit. `set()` removes expired entries first, replaces the key's own release accounting instead of adding to it, then evicts from the LRU front until both limits hold - so a write whose own release count exceeds the limit empties the cache rather than exceeding the bound. `get()` moves a live entry to the back, drops an expired one, and returns `(value, expires_at)` with `expires_at` an absolute wall-clock time, or `(None, 0)`. `sweep(now=None)` and `clear()` return how many entries they removed. The cache stores whatever the `collect()` closure hands it; the never-cache rules stay at that call site.
- Cache counters go through the `quasarr.search.cache.search_runtime` name and are recorded outside the cache lock, so instrumentation can never make a reader wait on another subsystem's lock. `cache_hits`/`cache_misses` are recorded on every `get`; `cache_evictions` counts only limit-driven removals, because an expired entry leaving is normal and would otherwise hide a cache that is genuinely too small.
- Per-source results are merged, date-sorted descending, title-filtered by `release_matches_search_category`, then offset/limit-sliced; feed responses are never paginated.
- Search sources normally have a same-key download twin (FX is the search-only exception); the `source_key` embedded in the search payload routes the later `download()` call to the same-key twin first when one exists.
- `runtime.py` counters are process-local and fixed-cardinality: `snapshot()` returns exactly the documented counter keys plus `rss_kib`/`pss_kib`/`threads`. Never add a source initial, query, URL, hostname, or category ID to a counter name or value - the snapshot is meant for logs, so every key and value must stay bounded. `record_source_outcome()` accepts only `completed`, `dropped`, `skipped`, `errored`, and `budget_exhausted` and raises `ValueError` otherwise.
- `SearchRuntime` takes an injected `clock` and `memory_reader` so tests stay deterministic; gauges (`active_requests`, `active_source_tasks`) are decremented in `finally`, so a raising body still returns them to zero. `snapshot()` never raises and never widens its value types: a `memory_reader` that raises, returns a non-mapping, or yields a non-`int` value degrades that field to `None`, so `rss_kib`/`pss_kib`/`threads` are always `int` or `None`. `read_process_memory()` reads `/proc/self/status` and `/proc/self/smaps_rollup` and returns `None` readings on non-Linux hosts or when a file is missing - it never raises into a search request.
- Overdue accounting is token-owned: `mark_source_overdue()` returns an opaque token and `resolve_source_overdue(token)` releases exactly that token once, returning `False` for a replayed, stale, or foreign token, so a late future can never resolve another task's mark. The gauge is the number of held tokens and therefore cannot go negative. There is deliberately no public `reset()` - zeroing counters while another thread sits inside `request()`/`source_task()` would let that thread's `finally` decrement a gauge below zero; tests build their own `SearchRuntime` or patch the `search_runtime` singleton instead.

## Work Guidance

(none beyond the contracts above - see `sources/AGENTS.md` for source-module rules)

## Verification

- Full unit suite: `uv run python -X utf8 -m unittest discover -s tests`
- Live searches/feeds: `uv run cli_tester.py`

## Child DOX Index

- `quasarr/search/sources/AGENTS.md` - search source plug-in contract (`Source` class, `SearchRelease` shape, payload format, conventions)
