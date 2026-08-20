# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

"""Carbon renderer for the Statistics view.

Renders the same 37 values as the Classic statistics page
(``quasarr.api.statistics._classic_statistics``) - never more, never fewer -
as four top KPI tiles plus six compact full-width grouped bands (Downloads,
CAPTCHAs, Linkcrypter blocks, Filecrypt cohort, Terminal operations, Cached
metadata). The two CAPTCHA decryption success rates additionally render as
``.cds-progress`` ratio bars (limited to only the two existing CAPTCHA
ratio bars), numeric text kept visible alongside each bar. No provider value is
renamed, dropped, or recomputed; no trend, chart, or blacklist counter is
added.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from typing import Any, Mapping, Sequence

from quasarr.providers.auth import show_logout_link
from quasarr.providers.carbon_templates import (
    TableColumn,
    data_table,
    protected_captcha_count,
    render_carbon_html,
    tag,
    tile,
)
from quasarr.providers.statistics import StatsHelper

# Presentation-only mapping from the Filecrypt lifecycle state text to a tag
# tone; the state text itself is rendered verbatim from the provider.
_STATE_TONES = {
    "available": "green",
    "healthy": "green",
    "sweeping": "blue",
    "cooldown": "red",
    "individual": "purple",
}


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


def _clamp_percent(value: object) -> float:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, pct))


def _kpi(label: str, value_text: str) -> str:
    return tile(f"<p>{value_text}</p>", heading=label, classes="is-compact is-metric")


def _band(caption: str, rows: Sequence[tuple[str, str]]) -> str:
    """A full-width key/value band for plain formatted-text values."""
    columns = (
        TableColumn("metric", "Metric"),
        TableColumn("value", "Value", classes="is-num is-mono"),
    )
    table_rows = [{"metric": label, "value": value} for label, value in rows]
    return data_table(columns, table_rows, caption=caption)


# The exact class attribute `data_table` would compute for a
# TableColumn("value", "Value", classes="is-num is-mono") column, on both
# its header and body cells.
_KV_BAND_VALUE_CLASS = ' class="is-num is-mono"'


def _kv_band(caption: str, rows: Sequence[tuple[str, str]]) -> str:
    """A full-width key/value band whose markup is byte-identical to
    ``data_table((TableColumn("metric", "Metric"), TableColumn("value",
    "Value", classes="is-num is-mono")), ...)`` - same wrapper, caption,
    header cells, and `<td>`/`<td class="is-num is-mono">` body cells -
    except the value cell receives caller-built safe HTML (tags, ``<time>``
    elements, progress bars) instead of being escaped. This is the same
    "trusted pre-built fragment" pattern ``carbon_templates.notification
    (actions=...)`` already uses, and it keeps every band - including this
    one - on the one already-established table layout (borders, padding, height,
    right-aligned monospace values) rather than a second, divergent one.
    """
    body_rows = "".join(
        f"<tr><td>{_h(label)}</td><td{_KV_BAND_VALUE_CLASS}>{value_html}</td></tr>"
        for label, value_html in rows
    )
    return (
        '<div class="cds-table-wrap">'
        '<table class="cds-table">'
        f"<caption>{_h(caption)}</caption>"
        '<thead><tr><th scope="col">Metric</th>'
        f'<th scope="col"{_KV_BAND_VALUE_CLASS}>Value</th></tr></thead>'
        f"<tbody>{body_rows}</tbody>"
        "</table></div>"
    )


def _ratio_bar(label: str, value: object) -> str:
    """A `.cds-progress` bar (carbon.css:672-682, already shipped as part of
    the base Carbon component set and permitted by the dispatcher's
    `style-src 'self' 'unsafe-inline'` CSP) with the numeric percentage kept
    visible as text alongside it - limited to only the two existing CAPTCHA
    ratio bars. The bar width is
    clamped to [0, 100]; the displayed text keeps the provider's own
    formatting, matching every other rendered rate on the page.
    """
    text = _percent(value)
    width = f"{_clamp_percent(value):.1f}"
    return (
        f"<span>{text}</span>"
        '<div class="cds-progress" role="progressbar" aria-valuemin="0" '
        f'aria-valuemax="100" aria-valuenow="{width}" '
        f'aria-label="{_h(label)} {text}">'
        f'<span style="width:{width}%"></span></div>'
    )


def _state_tag(state: object) -> str:
    text = str(state)
    return tag(text, tone=_STATE_TONES.get(text, "gray"))


def _individual_mode_tag(mode: object) -> str:
    text = str(mode) if mode else "None"
    return tag(text, tone="gray" if text == "None" else "purple")


def _deadline_time(epoch: object) -> str:
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


def render_statistics(shared_state) -> str:
    stats: Mapping[str, Any] = StatsHelper(shared_state).get_stats()

    kpis = (
        '<div class="cds-kpi-row">'
        + "".join(
            [
                _kpi(
                    "Total download attempts",
                    _int(stats["total_download_attempts"]),
                ),
                _kpi("Download success rate", _percent(stats["download_success_rate"])),
                _kpi(
                    "Total CAPTCHA decryptions",
                    _int(stats["total_captcha_decryptions"]),
                ),
                _kpi(
                    "Decryption success rate",
                    _percent(stats["decryption_success_rate"]),
                ),
            ]
        )
        + "</div>"
    )

    downloads = _band(
        "Downloads",
        [
            ("Packages downloaded", _int(stats["packages_downloaded"])),
            ("Links processed", _int(stats["links_processed"])),
            ("Failed downloads", _int(stats["failed_downloads"])),
            (
                "Average links per package",
                _decimal(stats["average_links_per_package"]),
            ),
        ],
    )

    captchas = _kv_band(
        "CAPTCHAs",
        [
            ("Automatic decryptions", _int(stats["captcha_decryptions_automatic"])),
            (
                "Automatic success rate",
                _ratio_bar(
                    "Automatic success rate",
                    stats["automatic_decryption_success_rate"],
                ),
            ),
            ("Manual decryptions", _int(stats["captcha_decryptions_manual"])),
            (
                "Manual success rate",
                _ratio_bar(
                    "Manual success rate", stats["manual_decryption_success_rate"]
                ),
            ),
            (
                "Failed auto decryptions",
                _int(stats["failed_decryptions_automatic"]),
            ),
            (
                "Failed manual decryptions",
                _int(stats["failed_decryptions_manual"]),
            ),
        ],
    )

    linkcrypter_blocks = _band(
        "Linkcrypter blocks",
        [
            ("Block observations", _int(stats["crypter_block_observations"])),
            ("Cooldowns started", _int(stats["crypter_cooldowns"])),
            ("Probes spent", _int(stats["crypter_probes"])),
            ("Deferred packages", _int(stats["deferred_packages"])),
        ],
    )

    filecrypt_cohort = _kv_band(
        "Filecrypt cohort",
        [
            ("State", _state_tag(stats["crypter_sweep_state"])),
            (
                "Tested",
                _h(
                    f"{int(stats['crypter_sweep_tested']):,} / "
                    f"{int(stats['crypter_sweep_total']):,}"
                ),
            ),
            ("Deadline", _deadline_time(stats["crypter_sweep_deadline_epoch"])),
            ("Cooldowns", _int(stats["crypter_cooldown_count"])),
            ("Retest queue", _int(stats["crypter_retest_depth"])),
            (
                "Individual mode",
                _individual_mode_tag(stats["crypter_individual_mode"]),
            ),
        ],
    )

    terminal_operations = _band(
        "Terminal operations",
        [
            ("Prepared", _int(stats["terminal_operations_prepared"])),
            ("Submitted", _int(stats["terminal_operations_submitted"])),
            ("Complete", _int(stats["terminal_operations_complete"])),
        ],
    )

    cached_metadata = _band(
        "Cached metadata",
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
        ],
    )

    content = "".join(
        [
            kpis,
            downloads,
            captchas,
            linkcrypter_blocks,
            filecrypt_cohort,
            terminal_operations,
            cached_metadata,
        ]
    )

    return render_carbon_html(
        "statistics",
        content,
        title="Statistics",
        eyebrow="Operations",
        subtitle="Download, CAPTCHA, linkcrypter, and metadata counters",
        captcha_count=protected_captcha_count(shared_state),
        show_user=show_logout_link(),
    )


__all__ = ["render_statistics"]
