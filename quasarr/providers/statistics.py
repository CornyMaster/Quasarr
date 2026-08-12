# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import time
from datetime import datetime
from json import loads
from typing import Any, Dict

from quasarr.providers.crypter_cooldowns import (
    CRYPTER_EVENT_FIELDS,
    CRYPTER_EVENT_KEY,
    CRYPTER_EVENT_TABLE,
    CrypterCooldownService,
    crypter_blocks_deferred,
    decode_pending_crypter_events,
)
from quasarr.providers.log import debug, warn

# The linkcrypter services record their transitions in one durable ledger row;
# these are the cumulative counters that row is drained into.
CRYPTER_EVENT_COUNTERS = {
    "observations": "crypter_block_observations",
    "cooldowns": "crypter_cooldowns",
    "probes": "crypter_probes",
}
# The ledger and its counters are always read and written as one unit, so a
# flush by another process can never be counted in both halves of a total.
CRYPTER_SNAPSHOT_TARGETS = ((CRYPTER_EVENT_TABLE, CRYPTER_EVENT_KEY),) + tuple(
    ("statistics", key) for key in CRYPTER_EVENT_COUNTERS.values()
)


def _counts_of(values) -> Dict[str, int]:
    """Read one stored counter per field, treating an unusable row as zero."""
    counts = {}
    for field, value in zip(CRYPTER_EVENT_FIELDS, values, strict=True):
        try:
            counts[field] = int(value) if value is not None else 0
        except (TypeError, ValueError):
            counts[field] = 0
    return counts


class StatsHelper:
    """
    Multiprocessing-safe stats helper using separate rows.
    Uses shared_state for database access across processes.
    """

    def __init__(self, shared_state, clock=time.time):
        self.shared_state = shared_state
        self._clock = clock
        self._ensure_stats_exist()

    def _get_db(self):
        """Get database interface through shared_state"""
        return self.shared_state.values["database"]("statistics")

    def _ensure_stats_exist(self):
        """Initialize stats if they don't exist"""
        default_stats = {
            "packages_downloaded": 0,
            "links_processed": 0,
            "captcha_decryptions_automatic": 0,
            "captcha_decryptions_manual": 0,
            "failed_downloads": 0,
            "failed_decryptions_automatic": 0,
            "failed_decryptions_manual": 0,
            "crypter_block_observations": 0,
            "crypter_cooldowns": 0,
            "crypter_probes": 0,
        }

        db = self._get_db()
        for key, default_value in default_stats.items():
            if db.retrieve(key) is None:
                db.store(key, str(default_value))

    def _get_stat(self, key: str, default: int = 0) -> int:
        """Get a single stat value"""
        try:
            db = self._get_db()
            value = db.retrieve(key)
            return int(value) if value is not None else default
        except (ValueError, TypeError):
            return default

    def _increment_stat(self, key: str, count: int = 1):
        """Process-safe increment of a single stat"""
        db = self._get_db()
        current = self._get_stat(key, 0)
        db.update_store(key, str(current + count))

    def _read_crypter_snapshot(self):
        """Read the ledger and all three counters in one transaction.

        Reading them separately lets another process flush in between, which
        counts the same events in the stored half and the pending half of the
        sum.
        """
        values = self._get_db().retrieve_values(CRYPTER_SNAPSHOT_TARGETS)
        pending, readable = decode_pending_crypter_events(values[0])
        return _counts_of(values[1:]), pending, readable

    def flush_crypter_events(self) -> Dict[str, int]:
        """Move the durable linkcrypter ledger into the cumulative counters.

        One transaction adds the pending counts, clears the ledger, and returns
        the committed totals, so an interrupted flush leaves every event
        pending, a repeated flush can never count one twice, and the caller
        never has to read the counters back. The ledger is decoded inside that
        transaction: a row that is still unreadable there is dropped, while a
        valid row written since is counted instead of erased.
        """

        discarded = {"row": False}

        def move(current_values):
            counts, readable = decode_pending_crypter_events(current_values[0])
            discarded["row"] = not readable
            stored = _counts_of(current_values[1:])
            return [None] + [
                str(stored[field] + counts[field]) for field in CRYPTER_EVENT_FIELDS
            ]

        committed = self._get_db().mutate_values(CRYPTER_SNAPSHOT_TARGETS, move)
        if discarded["row"]:
            warn("Discarding an unreadable linkcrypter transition ledger row")
        return _counts_of(committed[1:])

    def crypter_transition_totals(self) -> Dict[str, int]:
        """The cumulative linkcrypter counters, including unflushed events.

        Reading statistics must never fail on a write problem, and an event
        that stays pending must still be visible, so a failed flush falls back
        to the snapshot it already holds instead of reading a second time.
        """
        stored = pending = None
        try:
            stored, pending, readable = self._read_crypter_snapshot()
            if readable and not any(pending.values()):
                return stored
        except Exception as error:
            debug(f"Reading the linkcrypter transition snapshot failed: {error}")
        try:
            return self.flush_crypter_events()
        except Exception as error:
            debug(f"Deferring the linkcrypter statistics flush: {error}")
        if stored is None:
            return {
                field: self._get_stat(key, 0)
                for field, key in CRYPTER_EVENT_COUNTERS.items()
            }
        return {field: stored[field] + pending[field] for field in CRYPTER_EVENT_FIELDS}

    def increment_package_with_links(self, links):
        """Increment package downloaded and links processed for one package, or failed download if no links

        Args:
            links: Can be:
                - list/array: counts the length
                - int: uses the value directly
                - None/False/empty: treats as failed download
        """
        # Handle different input types
        if links is None or links is False:
            link_count = 0
        elif isinstance(links, (list, tuple)):
            link_count = len(links)
        elif isinstance(links, int):
            link_count = links
        else:
            # Handle other falsy values or unexpected types
            try:
                link_count = int(links) if links else 0
            except (ValueError, TypeError):
                link_count = 0

        # Now handle the actual increment logic
        if link_count == 0:
            self._increment_stat("failed_downloads", 1)
        else:
            self._increment_stat("packages_downloaded", 1)
            self._increment_stat("links_processed", link_count)

    def increment_captcha_decryptions_automatic(self):
        """Increment automatic captcha decryptions counter"""
        self._increment_stat("captcha_decryptions_automatic", 1)

    def increment_captcha_decryptions_manual(self):
        """Increment manual captcha decryptions counter"""
        self._increment_stat("captcha_decryptions_manual", 1)

    def increment_failed_downloads(self):
        """Increment failed downloads counter"""
        self._increment_stat("failed_downloads", 1)

    def increment_failed_decryptions_automatic(self):
        """Increment failed automatic decryptions counter"""
        self._increment_stat("failed_decryptions_automatic", 1)

    def increment_failed_decryptions_manual(self):
        """Increment failed manual decryptions counter"""
        self._increment_stat("failed_decryptions_manual", 1)

    def get_imdb_cache_stats(self) -> Dict[str, int]:
        """
        Get statistics about the IMDb metadata cache.
        Returns counts of cached items with various attributes.
        """
        try:
            db = self.shared_state.values["database"]("imdb_metadata")
            all_entries = db.retrieve_all_titles()

            total_cached = 0
            with_title = 0
            with_poster = 0
            with_localized = 0

            for _, data_str in all_entries:
                try:
                    data = loads(data_str)
                    total_cached += 1

                    if data.get("title"):
                        with_title += 1

                    if data.get("poster_link"):
                        with_poster += 1

                    if (
                        data.get("localized")
                        and isinstance(data["localized"], dict)
                        and len(data["localized"]) > 0
                    ):
                        with_localized += 1

                except (ValueError, TypeError):
                    continue

            return {
                "imdb_total_cached": total_cached,
                "imdb_with_title": with_title,
                "imdb_with_poster": with_poster,
                "imdb_with_localized": with_localized,
            }
        except Exception:
            return {
                "imdb_total_cached": 0,
                "imdb_with_title": 0,
                "imdb_with_poster": 0,
                "imdb_with_localized": 0,
            }

    def get_xem_cache_stats(self) -> Dict[str, int]:
        """
        Get statistics about TheXEM metadata caches.

        - xem_all_names contains one global allNames cache row (key: "allnames")
        - xem_season_names contains per-show season name cache rows (key: tvdb_id)
        """
        try:
            now = datetime.now().timestamp()
            all_names_db = self.shared_state.values["database"]("xem_all_names")
            season_names_db = self.shared_state.values["database"]("xem_season_names")

            all_names_cached = 0
            all_names_valid = 0
            all_names_raw = all_names_db.retrieve("allnames")
            if all_names_raw:
                all_names_cached = 1
                try:
                    all_names_data = loads(all_names_raw)
                    if all_names_data.get("ttl") and all_names_data["ttl"] > now:
                        all_names_valid = 1
                except (ValueError, TypeError):
                    pass

            season_total_cached = 0
            season_valid_cached = 0
            season_entries = season_names_db.retrieve_all_titles() or []

            for _, data_str in season_entries:
                try:
                    data = loads(data_str)
                    season_total_cached += 1
                    if data.get("ttl") and data["ttl"] > now:
                        season_valid_cached += 1
                except (ValueError, TypeError):
                    continue

            return {
                "xem_all_names_cached": all_names_cached,
                "xem_all_names_valid": all_names_valid,
                "xem_season_total_cached": season_total_cached,
                "xem_season_valid_cached": season_valid_cached,
                "xem_total_cached": all_names_cached + season_total_cached,
                "xem_total_valid_cached": all_names_valid + season_valid_cached,
            }
        except Exception:
            return {
                "xem_all_names_cached": 0,
                "xem_all_names_valid": 0,
                "xem_season_total_cached": 0,
                "xem_season_valid_cached": 0,
                "xem_total_cached": 0,
                "xem_total_valid_cached": 0,
            }

    def get_crypter_defer_stats(self) -> Dict[str, int]:
        """
        Get the current number of packages held by a linkcrypter block.

        This is a live gauge over the protected packages, never a counter: in
        `fail` block mode no package is held at all, and in `defer` mode holds
        also end silently by expiry.
        """
        try:
            if not crypter_blocks_deferred(self.shared_state):
                return {"deferred_packages": 0}
            service = CrypterCooldownService(self.shared_state, clock=self._clock)
            return {"deferred_packages": service.count_active_deferred_packages()}
        except Exception:
            return {"deferred_packages": 0}

    def get_stats(self) -> Dict[str, Any]:
        """Get all current statistics"""
        crypter_totals = self.crypter_transition_totals()
        stats = {
            "packages_downloaded": self._get_stat("packages_downloaded", 0),
            "links_processed": self._get_stat("links_processed", 0),
            "captcha_decryptions_automatic": self._get_stat(
                "captcha_decryptions_automatic", 0
            ),
            "captcha_decryptions_manual": self._get_stat(
                "captcha_decryptions_manual", 0
            ),
            "failed_downloads": self._get_stat("failed_downloads", 0),
            "failed_decryptions_automatic": self._get_stat(
                "failed_decryptions_automatic", 0
            ),
            "failed_decryptions_manual": self._get_stat("failed_decryptions_manual", 0),
        }
        for field, key in CRYPTER_EVENT_COUNTERS.items():
            stats[key] = crypter_totals[field]

        # Calculate totals and rates
        total_captcha_decryptions = (
            stats["captcha_decryptions_automatic"] + stats["captcha_decryptions_manual"]
        )
        total_failed_decryptions = (
            stats["failed_decryptions_automatic"] + stats["failed_decryptions_manual"]
        )
        total_download_attempts = (
            stats["packages_downloaded"] + stats["failed_downloads"]
        )
        total_decryption_attempts = total_captcha_decryptions + total_failed_decryptions
        total_automatic_attempts = (
            stats["captcha_decryptions_automatic"]
            + stats["failed_decryptions_automatic"]
        )
        total_manual_attempts = (
            stats["captcha_decryptions_manual"] + stats["failed_decryptions_manual"]
        )

        # Add calculated fields
        stats.update(
            {
                "total_captcha_decryptions": total_captcha_decryptions,
                "total_failed_decryptions": total_failed_decryptions,
                "total_download_attempts": total_download_attempts,
                "total_decryption_attempts": total_decryption_attempts,
                "total_automatic_attempts": total_automatic_attempts,
                "total_manual_attempts": total_manual_attempts,
                "download_success_rate": (
                    (stats["packages_downloaded"] / total_download_attempts * 100)
                    if total_download_attempts > 0
                    else 0
                ),
                "decryption_success_rate": (
                    (total_captcha_decryptions / total_decryption_attempts * 100)
                    if total_decryption_attempts > 0
                    else 0
                ),
                "automatic_decryption_success_rate": (
                    (
                        stats["captcha_decryptions_automatic"]
                        / total_automatic_attempts
                        * 100
                    )
                    if total_automatic_attempts > 0
                    else 0
                ),
                "manual_decryption_success_rate": (
                    (stats["captcha_decryptions_manual"] / total_manual_attempts * 100)
                    if total_manual_attempts > 0
                    else 0
                ),
                "average_links_per_package": (
                    stats["links_processed"] / stats["packages_downloaded"]
                    if stats["packages_downloaded"] > 0
                    else 0
                ),
            }
        )

        # Add metadata cache stats
        stats.update(self.get_imdb_cache_stats())
        stats.update(self.get_xem_cache_stats())
        stats.update(self.get_crypter_defer_stats())
        stats["metadata_total_cached"] = (
            stats["imdb_total_cached"] + stats["xem_total_cached"]
        )
        stats["metadata_total_valid_cached"] = (
            stats["imdb_total_cached"] + stats["xem_total_valid_cached"]
        )

        return stats
