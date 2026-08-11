# quasarr/search/ - Search Side

## Purpose

The Newznab-facing search layer: `get_search_results()` fans a single *arr request (IMDb-ID search, phrase search, or feed pull) out in parallel across all discovered source modules, caches per-source results, merges/sorts/filters/paginates them, and returns releases whose `link` points back at Quasarr's own `/download/` endpoint with a base64 payload.

## Ownership

- `__init__.py` - orchestrator: the three search branches, `SearchExecutor` (thread-pool fan-out + per-source status badges), `SearchCache` (TTL cache)
- `runtime.py` - process-local search instrumentation: `SearchRuntime`, the `search_runtime` singleton, `read_process_memory()`
- `sources/` - see Child DOX Index

## Local Contracts

- Per-source gating before dispatch: hostname configured, category in `supported_categories`, category whitelist from `get_search_category_sources`, `supports_imdb` for the imdb branch, `supports_phrase` for the phrase branch, `supports_absolute_numbering` when an episode is given without a season, and `supports_date_numbering` for Sonarr's year + `MM/DD` episode shape. The feed branch checks only hostname/category/whitelist.
- Movie searches and feeds require a cached Radarr client; TV searches and feeds require a cached Sonarr client. Missing clients stop before source dispatch with an error. Book and music phrase searches have no Arr-client gate. IMDb searches warm the metadata cache from the category's required client before fan-out.
- Date-numbered requests are parsed once into a validated `datetime.date`; regular `season`/`episode` are cleared before dispatch and proven sources receive only `episode_date`. Invalid calendar dates stay on the normal numbering path.
- The method names `search` and `feed` are load-bearing - dispatch is `getattr(source, action)`.
- The fan-out is capped by a deadline: `get_search_results` takes an optional absolute `deadline` and otherwise starts one at `SEARCH_FANOUT_DEADLINE_SECONDS`. Callers running several searches for one *arr request must pass their own so the runs share it. IMDb metadata warming and source dispatch are both skipped once it has passed, and sources still running when it passes are badged and dropped from that response, so an *arr client never waits past its own request timeout. The pool is sized to one worker per dispatched source, otherwise the sources dispatched last would spend the deadline queued behind the first ones. Sources must still bound their own work - a dropped source contributes nothing.
- `start_time` is taken before IMDb metadata warming: sources derive their own budget from it, so a later anchor would let them outlive the deadline by whatever the warming cost.
- Cache TTL is 300s for search, 60s for feed; the key nulls `start_time` and uses the cache-owner category. Cached entries skip execution entirely, so source methods must be safe to skip.
- Per-source results are merged, date-sorted descending, title-filtered by `release_matches_search_category`, then offset/limit-sliced; feed responses are never paginated.
- Search sources normally have a same-key download twin (FX is the search-only exception); the `source_key` embedded in the search payload routes the later `download()` call to the same-key twin first when one exists.
- `runtime.py` counters are process-local and fixed-cardinality: `snapshot()` returns exactly the documented counter keys plus `rss_kib`/`pss_kib`/`threads`. Never add a source initial, query, URL, hostname, or category ID to a counter name or value - the snapshot is meant for logs, so every key and value must stay bounded. `record_source_outcome()` accepts only `completed`, `dropped`, `skipped`, `errored`, and `budget_exhausted` and raises `ValueError` otherwise.
- `SearchRuntime` takes an injected `clock` and `memory_reader` so tests stay deterministic; gauges (`active_requests`, `active_source_tasks`) are decremented in `finally`, so a raising body still returns them to zero. `last_activity_at` is stamped from that clock on every gauge transition. `read_process_memory()` reads `/proc/self/status` and `/proc/self/smaps_rollup` and returns `None` readings on non-Linux hosts or when a file is missing - it never raises into a search request.

## Work Guidance

(none beyond the contracts above - see `sources/AGENTS.md` for source-module rules)

## Verification

- Full unit suite: `uv run python -X utf8 -m unittest discover -s tests`
- Live searches/feeds: `uv run cli_tester.py`

## Child DOX Index

- `quasarr/search/sources/AGENTS.md` - search source plug-in contract (`Source` class, `SearchRelease` shape, payload format, conventions)
