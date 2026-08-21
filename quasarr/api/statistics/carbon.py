# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

"""Carbon renderer for the Statistics view.

Renders the same 37 values as the Classic statistics page
(``quasarr.api.statistics._classic_statistics``) - never more, never fewer -
as four top KPI metric tiles (32px values) followed by five detail tiles
(CAPTCHA decryptions, Cached metadata, Linkcrypter blocks, Filecrypt
cohort, Terminal operations) in one self-arranging ``cds-grid--auto`` grid,
so the tiles fill available rows instead of leaving a ragged gap under the
shorter CAPTCHA tile. CAPTCHA decryption volume and the Filecrypt cohort
tested/total ratio additionally render as ``.cds-progress`` bars, with
every underlying number kept visible as text alongside or inside each bar.
No provider value is renamed, dropped, or recomputed; no trend, chart, or
blacklist counter is added.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from typing import Any, Mapping

from quasarr.providers.auth import show_logout_link
from quasarr.providers.carbon_templates import (
    grid,
    kv_rows,
    metric_tile,
    protected_captcha_count,
    render_carbon_html,
    status,
    tile,
)
from quasarr.providers.statistics import StatsHelper


def _h(value: object) -> str:
    return escape(str(value), quote=True)


def _int(value: object) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return _h(value)


def _percent(value: object) -> str:
    try:
        return f"{float(value):,.1f}%"
    except (TypeError, ValueError):
        return _h(value)


def _decimal(value: object) -> str:
    try:
        return f"{float(value):,.1f}"
    except (TypeError, ValueError):
        return _h(value)


def _pct_of(a: object, b: object) -> int:
    """Safe percentage of ``a`` over ``b`` - 0 (never a ``ZeroDivisionError``)
    when ``b`` is falsy, matching a fresh install's all-zero counters."""
    return 0 if not b else round(100 * int(a) / int(b))


def _deadline(epoch: object) -> str:
    """The Filecrypt sweep deadline as a ``<time data-epoch>`` element the
    client-side clock upgrades to local time plus a relative phrase - or the
    fixed "No active deadline" text while no sweep is active (epoch 0).
    """
    try:
        epoch_int = int(epoch)
    except (TypeError, ValueError):
        epoch_int = 0
    if epoch_int <= 0:
        return _h("No active deadline")
    try:
        moment = datetime.fromtimestamp(epoch_int, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return _h("No active deadline")
    iso = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    display = moment.strftime("%Y-%m-%d %H:%M UTC")
    return f'<time datetime="{_h(iso)}" data-epoch="{epoch_int}">{_h(display)}</time>'


def _bar(
    label: str,
    value_text: str,
    pct: object,
    *,
    tone: str = "interactive",
    help_text: str = "",
) -> str:
    """A `.cds-progress` ratio bar with the numeric value kept visible as
    text in its head row; ``pct`` is clamped to [0, 100] for the fill width
    only - the displayed ``value_text`` is never rewritten by the clamp.
    Carries the same accessibility contract the old `_ratio_bar()` had -
    `role="progressbar"` plus `aria-valuemin`/`aria-valuemax`/`aria-valuenow`
    and an identifying `aria-label` - so a screen reader still announces
    which bar it is and its current value.
    """
    clamped = max(0, min(100, int(pct)))
    help_html = f'<p class="cds-bar__help">{_h(help_text)}</p>' if help_text else ""
    return (
        '<div class="cds-bar">'
        f'<div class="cds-bar__head"><span>{_h(label)}</span>'
        f"<strong>{_h(value_text)}</strong></div>"
        '<div class="cds-progress cds-progress--thick" role="progressbar" '
        f'aria-valuemin="0" aria-valuemax="100" aria-valuenow="{clamped}" '
        f'aria-label="{_h(label)}">'
        f'<div class="cds-progress__fill cds-progress__fill--{tone}" '
        f'style="width:{clamped}%"></div></div>'
        f"{help_html}</div>"
    )


def render_statistics(shared_state) -> str:
    stats: Mapping[str, Any] = StatsHelper(shared_state).get_stats()

    metrics = (
        '<div class="cds-kpi-row">'
        + "".join(
            [
                metric_tile(
                    "Download attempts",
                    _int(stats["total_download_attempts"]),
                    f"{_percent(stats['download_success_rate'])} success rate",
                    sub_success=True,
                ),
                metric_tile(
                    "CAPTCHA decryptions",
                    _int(stats["total_captcha_decryptions"]),
                    f"{_percent(stats['decryption_success_rate'])} success rate",
                    sub_success=True,
                ),
                metric_tile(
                    "Packages downloaded",
                    _int(stats["packages_downloaded"]),
                    f"{_int(stats['links_processed'])} links processed",
                ),
                metric_tile(
                    "Failed downloads",
                    _int(stats["failed_downloads"]),
                    f"{_decimal(stats['average_links_per_package'])} links per package avg.",
                ),
            ]
        )
        + "</div>"
    )

    # `or 1` keeps a fresh install (both counters at 0) from dividing by
    # zero; the resulting 0% fill on both bars is correct either way.
    share = (
        int(stats["captcha_decryptions_automatic"])
        + int(stats["captcha_decryptions_manual"])
        or 1
    )
    captcha_tile = tile(
        _bar(
            "Automatic (SponsorsHelper)",
            _int(stats["captcha_decryptions_automatic"]),
            100 * int(stats["captcha_decryptions_automatic"]) / share,
            help_text=(
                f"{_percent(stats['automatic_decryption_success_rate'])} success · "
                f"{_int(stats['failed_decryptions_automatic'])} failed"
            ),
        )
        + _bar(
            "Manual (browser userscript)",
            _int(stats["captcha_decryptions_manual"]),
            100 * int(stats["captcha_decryptions_manual"]) / share,
            tone="teal",
            help_text=(
                f"{_percent(stats['manual_decryption_success_rate'])} success · "
                f"{_int(stats['failed_decryptions_manual'])} failed"
            ),
        ),
        heading="CAPTCHA decryptions",
    )

    cached_tile = tile(
        kv_rows(
            [
                ("Total cached entries", _int(stats["metadata_total_cached"])),
                ("IMDb cached IDs", _int(stats["imdb_total_cached"])),
                ("IMDb with title", _int(stats["imdb_with_title"])),
                ("IMDb with poster", _int(stats["imdb_with_poster"])),
                ("IMDb with localized title", _int(stats["imdb_with_localized"])),
                ("XEM global name index", _int(stats["xem_all_names_valid"])),
                ("XEM global name cache", _int(stats["xem_all_names_cached"])),
                ("XEM season name caches", _int(stats["xem_season_total_cached"])),
                ("XEM season names valid", _int(stats["xem_season_valid_cached"])),
            ]
        ),
        heading="Cached metadata",
    )

    blocks_tile = tile(
        kv_rows(
            [
                ("Block observations", _int(stats["crypter_block_observations"])),
                ("Cooldowns started", _int(stats["crypter_cooldowns"])),
                ("Probes spent", _int(stats["crypter_probes"])),
                ("Deferred packages", _int(stats["deferred_packages"])),
            ]
        ),
        heading="Linkcrypter blocks",
        help_text="Access blocks observed since first start.",
    )

    cohort_state = str(
        stats["crypter_sweep_state"]
    )  # available | sweeping | cooldown | ...
    cohort_tone = {
        "available": "success",
        "sweeping": "warning",
        "cooldown": "error",
    }.get(cohort_state, "neutral")
    # The State and Deadline rows are hand-built here rather than through
    # `kv_rows()`, which escapes its value - both carry caller-built trusted
    # HTML (a `status()` badge and a `<time data-epoch>` element), the same
    # "trusted pre-built fragment" pattern `carbon_templates.notification
    # (actions=...)` already uses.
    cohort_tile = tile(
        '<div class="cds-kv__row"><span class="cds-kv__label">State</span>'
        f"{status(cohort_state.capitalize(), cohort_tone, strong=True)}</div>"
        + _bar(
            "Tested",
            f"{_int(stats['crypter_sweep_tested'])} / {_int(stats['crypter_sweep_total'])}",
            _pct_of(stats["crypter_sweep_tested"], stats["crypter_sweep_total"]),
        )
        + '<div class="cds-kv__row"><span class="cds-kv__label">Deadline</span>'
        f'<span class="cds-kv__value">{_deadline(stats["crypter_sweep_deadline_epoch"])}</span></div>'
        + kv_rows(
            [
                ("Cooldowns", _int(stats["crypter_cooldown_count"])),
                ("Retest queue", _int(stats["crypter_retest_depth"])),
                ("Individual mode", stats["crypter_individual_mode"] or "none"),
            ]
        ),
        heading="Filecrypt cohort",
        help_text="Live sweep state of the Filecrypt lifecycle.",
    )

    terminal_tile = tile(
        kv_rows(
            [
                ("Prepared", _int(stats["terminal_operations_prepared"])),
                ("Submitted", _int(stats["terminal_operations_submitted"])),
                ("Complete", _int(stats["terminal_operations_complete"])),
            ]
        ),
        heading="Terminal operations",
        help_text="SponsorsHelper hand-offs by phase.",
    )

    content = metrics + grid(
        [captcha_tile, cached_tile, blocks_tile, cohort_tile, terminal_tile], "auto"
    )

    return render_carbon_html(
        "statistics",
        content,
        title="Statistics",
        eyebrow="All time",
        captcha_count=protected_captcha_count(shared_state),
        show_user=show_logout_link(),
    )


__all__ = ["render_statistics"]
