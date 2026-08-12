# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import timezone
from email.utils import parsedate_to_datetime

from quasarr.constants import (
    SEARCH_CAT_BOOKS,
    SEARCH_CAT_MOVIES,
    SEARCH_CAT_MUSIC,
    SEARCH_CAT_SHOWS,
    SEARCH_FANOUT_DEADLINE_SECONDS,
)
from quasarr.providers.imdb_metadata import get_imdb_metadata
from quasarr.providers.log import (
    debug,
    error,
    get_source_logger,
    info,
    trace,
    warn,
)
from quasarr.search.cache import SearchCache as SearchCache  # explicit re-export
from quasarr.search.cache import search_cache
from quasarr.search.reclaim import (
    IdleMemoryReclaimer as IdleMemoryReclaimer,  # explicit re-export
)
from quasarr.search.reclaim import trim_native_heap as trim_native_heap
from quasarr.search.runtime import search_runtime
from quasarr.search.singleflight import (
    SearchSingleFlight as SearchSingleFlight,  # explicit re-export
)
from quasarr.search.singleflight import SharedWork as SharedWork  # explicit re-export
from quasarr.search.singleflight import search_singleflight
from quasarr.search.sources import get_sources
from quasarr.search.sources.helpers.budget import (
    SearchBudgetExhausted,
    use_search_budget,
)
from quasarr.search.sources.helpers.search_source import AbstractSearchSource
from quasarr.storage.categories import get_search_category_sources


def get_search_results(
    shared_state,
    request_from,
    search_category,
    imdb_id="",
    search_phrase="",
    season=None,
    episode=None,
    offset=0,
    limit=1000,
    deadline=None,
):
    from quasarr.providers.utils import (
        determine_search_category,
        get_base_search_category_id,
        get_search_behavior_category,
        get_search_cache_owner_category,
        get_search_capability_category,
        parse_episode_date,
        release_matches_search_category,
    )

    sources = get_sources()

    if imdb_id and not imdb_id.startswith("tt"):
        imdb_id = f"tt{imdb_id}"

    episode_date = parse_episode_date(season, episode)
    if episode_date:
        season = None
        episode = None

    # Determine search category if not provided
    if not search_category:
        search_category = determine_search_category(request_from)

    # Resolve base category for logic (Movies, TV, etc.).
    base_search_category = (
        get_base_search_category_id(search_category) or search_category
    )
    behavior_search_category = (
        get_search_behavior_category(search_category) or search_category
    )

    if base_search_category == SEARCH_CAT_MOVIES:
        from quasarr.providers.radarr_api import get_client as get_radarr_client

        if get_radarr_client(shared_state) is None:
            error("Movie search unavailable: Radarr is not configured")
            return []
    elif base_search_category == SEARCH_CAT_SHOWS:
        from quasarr.providers.sonarr_api import get_client as get_sonarr_client

        if get_sonarr_client(shared_state) is None:
            error("TV search unavailable: Sonarr is not configured")
            return []

    # Anchored before metadata warming: sources derive their own budget from this,
    # so starting the clock after the warming would let a source outlive the
    # deadline by however long the warming took.
    start_time = time.time()

    if imdb_id and (deadline is None or time.time() < deadline):
        # A failed refresh is not cached, so every category of a multi-category
        # request would otherwise pay the Arr client timeout again, past the
        # ceiling the deadline exists to hold.
        get_imdb_metadata(shared_state, imdb_id, base_search_category)

    capability_category = get_search_capability_category(search_category)
    is_custom_search_category = False
    try:
        is_custom_search_category = int(search_category) >= 100000
    except (TypeError, ValueError):
        pass
    # Cache keys are shared at the cache-family owner level.
    # Multi-category callers should execute same-family categories in ascending order
    # so the lowest category populates cache before stricter siblings run.
    cache_key_category = (
        search_category
        if is_custom_search_category
        else get_search_cache_owner_category(behavior_search_category)
    )

    # Filter out sources that are not in the search category's whitelist
    # We use the original search_category ID here to get the specific whitelist
    whitelisted_sources = get_search_category_sources(search_category)

    if whitelisted_sources:
        debug(
            f"Using whitelist for category <g>{search_category}</g>: {', '.join([s.upper() for s in whitelisted_sources])}"
        )

    search_executor = SearchExecutor(deadline=deadline)

    # Config retrieval
    config = shared_state.values["config"]("Hostnames")

    use_pagination = True

    # Use base_search_category for logic branching
    if imdb_id:
        stype = f"IMDb-ID <y>{imdb_id}</y>"

        if season:
            stype += f" <g>S{season}</g>"
        if episode:
            stype += f"{'' if season else ' '}<e>E{episode}</e>"
        if episode_date:
            stype += f" <g>{episode_date:%Y}</g>-<e>{episode_date:%m}</e>-<y>{episode_date:%d}</y>"

        if base_search_category in [SEARCH_CAT_MOVIES, SEARCH_CAT_SHOWS]:
            args = (shared_state, start_time, behavior_search_category)
            for source in sources.values():
                source_logger = get_source_logger(source.initials)

                if not config.get(source.initials):
                    source_logger.trace("Hostname missing in config")
                    continue

                if capability_category not in source.supported_categories:
                    source_logger.trace(
                        f"Category <g>{capability_category}</g> not supported"
                    )
                    continue

                if whitelisted_sources and source.initials not in whitelisted_sources:
                    source_logger.trace(
                        f"Category <g>{search_category}</g> not whitelisted"
                    )
                    continue

                if not source.supports_imdb:
                    source_logger.warn("IMDb ID unsupported")
                    continue

                if episode and not season and not source.supports_absolute_numbering:
                    source_logger.trace("Search with absolute EP number unsupported")
                    continue

                kwargs = {
                    "search_string": imdb_id,
                    "season": season,
                    "episode": episode,
                }

                if episode_date:
                    if not source.supports_date_numbering:
                        source_logger.trace("Search with date unsupported")
                        continue

                    kwargs["episode_date"] = episode_date

                search_executor.add(
                    source,
                    args,
                    kwargs,
                    use_cache=True,
                    cache_category=cache_key_category,
                )
        else:
            warn(
                f"{stype} is not supported for <d>{request_from}</d>, category: {search_category} (Base: {base_search_category})"
            )

    elif search_phrase:
        stype = f"Search-Phrase <b>{search_phrase}</b>"
        if base_search_category in [SEARCH_CAT_BOOKS, SEARCH_CAT_MUSIC]:
            args = (shared_state, start_time, behavior_search_category)
            kwargs = {"search_string": search_phrase}
            for source in sources.values():
                source_logger = get_source_logger(source.initials)

                if not config.get(source.initials):
                    source_logger.trace("Hostname missing in config")
                    continue

                if capability_category not in source.supported_categories:
                    source_logger.trace(
                        f"Category <g>{capability_category}</g> not supported"
                    )
                    continue

                if whitelisted_sources and source.initials not in whitelisted_sources:
                    source_logger.trace(
                        f"Category <g>{search_category}</g> not whitelisted"
                    )
                    continue

                if not source.supports_phrase:
                    source_logger.warn("Search phrase unsupported")
                    continue

                search_executor.add(
                    source,
                    args,
                    kwargs,
                    use_cache=True,
                    cache_category=cache_key_category,
                )
        else:
            warn(
                f"{stype} is not supported for <d>{request_from}</d>, category: {search_category} (Base: {base_search_category})"
            )

    else:
        stype = "<b>Feed</b> search"
        args = (shared_state, start_time, behavior_search_category)
        kwargs = {}
        use_pagination = False
        for source in sources.values():
            source_logger = get_source_logger(source.initials)

            if not config.get(source.initials):
                source_logger.trace("Hostname missing in config")
                continue

            if capability_category not in source.supported_categories:
                source_logger.trace(
                    f"Category <g>{capability_category}</g> not supported"
                )
                continue

            if whitelisted_sources and source.initials not in whitelisted_sources:
                source_logger.trace(
                    f"Category <g>{search_category}</g> not whitelisted"
                )
                continue

            search_executor.add(
                source,
                args,
                kwargs,
                use_cache=True,
                ttl=60,
                action="feed",
                cache_category=cache_key_category,
            )

    debug(f"Starting <g>{len(search_executor.searches)}</g> searches for {stype}")

    # Unpack the new return values (all_cached, min_ttl)
    results, status_bar, all_cached, min_ttl = search_executor.run_all()

    elapsed_time = time.time() - start_time

    # Sort results by date (newest first)
    def get_date(item):
        try:
            dt = parsedate_to_datetime(item.get("details", {}).get("date", ""))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return parsedate_to_datetime("Thu, 01 Jan 1970 00:00:00 +0000")

    results.sort(key=get_date, reverse=True)

    filtered_results = [
        release
        for release in results
        if release_matches_search_category(
            search_category,
            release.get("details", {}).get("title", ""),
        )
    ]
    filtered_out_count = len(results) - len(filtered_results)
    if filtered_out_count > 0:
        debug(
            f"Filtered out <r>{filtered_out_count}</r> releases by title rules for category <g>{search_category}</g>"
        )
    results = filtered_results

    # Calculate pagination for logging and return
    total_count = len(results)

    # Slicing
    if use_pagination:
        sliced_results = results[offset : offset + limit]
    else:
        sliced_results = results

    if sliced_results:
        trace(f"First {len(sliced_results)} results sorted by date:")
        for i, res in enumerate(sliced_results):
            details = res.get("details", {})
            trace(f"{i + 1}. {details.get('date')} | {details.get('title')}")

    # Formatting for log (1-based index for humans)
    log_start = min(offset + 1, total_count) if total_count > 0 else 0
    log_end = min(offset + limit, total_count) if use_pagination else total_count

    # Logic to switch between "Time taken" and "from cache"
    if all_cached:
        time_info = f"from cache ({int(min_ttl)}s left)"
    else:
        time_info = f"Time taken: {elapsed_time:.2f} seconds"

    info(
        f"Providing releases <g>{log_start}-{log_end}</g> of <g>{total_count}</g> to <d>{request_from}</d> "
        f"for {stype}{status_bar} <blue>{time_info}</blue>"
    )

    return sliced_results


class SourceTaskOutcome:
    """What one source worker produced, private to `quasarr.search`.

    A bare release list cannot say whether it is the whole answer: a source
    that stops early because its budget went returns the same shape as one that
    finished. Carrying `budget_exhausted` alongside the releases is what lets
    the fan-out answer with a partial result without caching it for a full TTL.
    An exception is carried rather than raised out of the worker so the one
    shared future keeps holding an outcome for every collector.
    """

    __slots__ = ("budget_exhausted", "error", "results")

    def __init__(self, results, budget_exhausted, error):
        self.results = results
        self.budget_exhausted = budget_exhausted
        self.error = error


class _Completion:
    """A source result and its finish time, shared through the future.

    `Future.add_done_callback` cannot carry this: `set_result` notifies the
    `as_completed` waiter *before* it invokes callbacks, so the collecting
    thread can reach the cache write while the timestamp is still missing.
    Returning both values from the submitted callable always wins that race.
    """

    __slots__ = ("at", "result")

    def __init__(self, result, at):
        self.result = result
        self.at = at


def _is_cacheable(future, completion, deadline):
    """Whether a collected result may be written to the search cache.

    A result that landed after the deadline is answered with but never cached:
    the deadline had already given up on it, and caching would keep serving it
    for the whole TTL. The same holds for a source that ran out of its budget:
    it answered with what it had, which is not the answer a later request
    deserves. Raised exceptions are already excluded by the caller's `except`
    branch.
    """
    return (
        future.done()
        and not future.cancelled()
        and not completion.result.budget_exhausted
        and completion.at is not None
        and completion.at <= deadline
    )


class SearchExecutor:
    def __init__(self, deadline=None, clock=time.time):
        self.searches = []
        # Wall clock, injected so deadline behavior stays deterministic in tests.
        # It must stay wall-clock: the deadline is an absolute time.time() that
        # callers hand in, so SearchRuntime's monotonic clock cannot be used.
        self._clock = clock
        # Absolute time this fan-out must be answered by. Callers that run several
        # executors for one *arr request pass their own so the runs share a single
        # deadline instead of each starting a fresh one.
        self.deadline = (
            deadline
            if deadline is not None
            else clock() + SEARCH_FANOUT_DEADLINE_SECONDS
        )

    def add(
        self,
        source: AbstractSearchSource,
        args,
        kwargs,
        use_cache=False,
        ttl=300,
        action="search",
        cache_category=None,
    ):
        key_args = list(args)
        key_args[1] = None
        if cache_category is not None and len(key_args) >= 3:
            key_args[2] = cache_category
        key_args = tuple(key_args)
        key = hash((source.initials, action, key_args, frozenset(kwargs.items())))
        self.searches.append(
            (
                key,
                lambda: getattr(source, action)(*args, **kwargs),
                use_cache,
                ttl,
                source.initials,
            )
        )

    def run_all(self):
        results = []
        future_to_meta = {}
        runtime = search_runtime

        # Track cache state
        all_cached = len(self.searches) > 0
        min_ttl = float("inf")
        bar_str = ""  # Initialize to prevent UnboundLocalError on full cache

        deadline = self.deadline
        # One worker per source: the default pool is sized from the CPU count, so
        # on a small host the last sources would queue behind the first ones and
        # burn the deadline without ever having started.
        # Not a context manager on purpose: its __exit__ joins every worker, which
        # would re-introduce the very wait the deadline exists to prevent.
        executor = ThreadPoolExecutor(max_workers=max(1, len(self.searches)))
        try:
            current_index = 0
            pending_futures = []
            skipped_badges = []

            for key, func, use_cache, ttl, source_name in self.searches:
                cached_result = None
                exp = 0

                if use_cache:
                    # Get both result and expiry
                    cached_result, exp = search_cache.get(key)

                if cached_result is not None:
                    get_source_logger(source_name).debug(
                        f"Using cached result with cache_key '{key}'"
                    )
                    results.extend(cached_result)

                    # Calculate TTL for this cached item
                    ttl_left = exp - self._clock()
                    if ttl_left < min_ttl:
                        min_ttl = ttl_left
                else:
                    all_cached = False
                    if self._clock() >= deadline:
                        # Nothing left to spend. Starting the work anyway would
                        # only detach a worker whose result this response can no
                        # longer use, and hit the source a second time for it.
                        runtime.record_source_outcome("skipped")
                        skipped_badges.append(
                            f"<bg yellow><black>{source_name.upper()}</black></bg yellow>"
                        )
                        get_source_logger(source_name).warn(
                            "Not started, this request is already out of time"
                        )
                        continue

                    def run_source(
                        source_func=func,
                        task_runtime=runtime,
                        now=self._clock,
                        task_deadline=deadline,
                    ):
                        with (
                            task_runtime.source_task(),
                            use_search_budget(task_deadline, clock=now) as budget,
                        ):
                            try:
                                outcome = SourceTaskOutcome(
                                    source_func(), budget.exhausted, None
                                )
                            except SearchBudgetExhausted:
                                # The source stopped itself instead of finishing,
                                # so it has nothing to answer with - but it is out
                                # of time, not broken.
                                outcome = SourceTaskOutcome([], True, None)
                            except Exception as exc:
                                outcome = SourceTaskOutcome([], budget.exhausted, exc)
                        return _Completion(outcome, now())

                    shared_work = search_singleflight.submit(
                        key, executor, run_source, deadline
                    )
                    future = shared_work.future
                    if not shared_work.is_leader:
                        runtime.record_coalesced_waiter()
                    cache_meta = (key, ttl) if use_cache else None
                    future_to_meta[future] = (
                        current_index,
                        cache_meta,
                        source_name,
                        shared_work,
                    )
                    pending_futures.append(future)
                    current_index += 1

            results_badges = [""] * len(pending_futures)
            if pending_futures:
                collected = set()

                def collect(future):
                    collected.add(future)
                    index, cache_meta, source_name, shared_work = future_to_meta[future]
                    try:
                        completion = future.result()
                        outcome = completion.result
                        if outcome.error is not None:
                            # Re-raised here so a failure keeps the one error
                            # path, whether it came out of the future or was
                            # carried back inside the outcome.
                            raise outcome.error
                        res = outcome.results
                        if outcome.budget_exhausted:
                            get_source_logger(source_name).warn(
                                "Ran out of time, this result is incomplete"
                            )
                            badge = f"<bg yellow><black>{source_name.upper()}</black></bg yellow>"
                        elif res and len(res) > 0:
                            badge = f"<bg green><black>{source_name.upper()}</black></bg green>"
                        else:
                            get_source_logger(source_name).debug(
                                "❌ No results returned"
                            )
                            badge = f"<bg black><white>{source_name.upper()}</white></bg black>"

                        results_badges[index] = badge
                        results.extend(res)
                        if (
                            cache_meta
                            and _is_cacheable(future, completion, shared_work.deadline)
                            and shared_work._claim_cache()
                        ):
                            cache_key, cache_ttl = cache_meta
                            search_cache.set(cache_key, res, ttl=cache_ttl)
                        if shared_work.is_leader:
                            runtime.record_source_outcome(
                                "budget_exhausted"
                                if outcome.budget_exhausted
                                else "completed"
                            )
                    except Exception as e:
                        if shared_work.is_leader:
                            runtime.record_source_outcome("errored")
                        results_badges[index] = (
                            f"<bg red><white>{source_name.upper()}</white></bg red>"
                        )
                        get_source_logger(source_name).warn(f"Search error: {e}")
                    finally:
                        shared_work.waiter_done()

                try:
                    for future in as_completed(
                        pending_futures, timeout=max(0.1, deadline - self._clock())
                    ):
                        collect(future)
                except FutureTimeoutError:
                    # Radarr and Sonarr drop an indexer that outlives their own
                    # request timeout, so answer with whatever is ready instead of
                    # waiting for the straggler.
                    for future in pending_futures:
                        if future in collected:
                            continue
                        if future.done():
                            collect(future)
                            continue

                        *meta, shared_work = future_to_meta[future]
                        try:
                            index, _, source_name = meta
                            results_badges[index] = (
                                f"<bg yellow><black>{source_name.upper()}</black></bg yellow>"
                            )
                            get_source_logger(source_name).warn(
                                f"Dropped from this response after "
                                f"{SEARCH_FANOUT_DEADLINE_SECONDS}s"
                            )
                            if shared_work.is_leader:
                                runtime.record_source_outcome("dropped")
                                overdue_token = runtime.mark_source_overdue()

                                def resolve_overdue(
                                    _, token=overdue_token, token_owner=runtime
                                ):
                                    token_owner.resolve_source_overdue(token)

                                future.add_done_callback(resolve_overdue)
                        finally:
                            shared_work.waiter_done()

            if results_badges or skipped_badges:
                bar_str = f" [{' '.join(results_badges + skipped_badges)}]"
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        return results, bar_str, all_cached, min_ttl
