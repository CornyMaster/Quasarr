# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

"""Carbon renderers for the Dashboard and Settings views.

``render_dashboard(shared_state)`` and ``render_settings(shared_state)`` are
the exact two symbols ``quasarr/api/__init__.py``'s ``/`` and ``/settings``
routes import lazily. Both delegate to a pure model builder
(``build_dashboard_model`` / ``build_settings_model``) that only reads
already-cached ``shared_state.values``, ``Config``, and ``DataBase`` - never
``shared_state.get_device()`` or ``downloads.packages.get_packages()`` - so
constructing either page never blocks on JDownloader, and a HEAD ``/``
request (which Bottle answers by running the same GET callback and
discarding the body) never performs JD I/O either.

The dashboard's queue tile is intentionally NOT populated here: it ships as
an empty/loading placeholder and is filled client-side from
``GET /api/packages/list`` (the shared packages-list data contract) after
first paint, with
an isolated unavailable state scoped to that one tile on error/timeout.
"""

from __future__ import annotations

import json
from html import escape
from typing import Any, Mapping

from quasarr.api.jdownloader import get_jdownloader_status
from quasarr.constants import TIMEOUT_SLOW_MODE_DEFINITIONS
from quasarr.providers.auth import show_logout_link
from quasarr.providers.carbon_templates import (
    TableColumn,
    data_table,
    notification,
    protected_captcha_count,
    render_carbon_html,
    tag,
    tile,
    toggle,
)
from quasarr.providers.hostname_issues import get_all_hostname_issues
from quasarr.providers.notifications.helpers.notification_types import (
    get_notification_type_label,
    get_user_configurable_notification_types,
)
from quasarr.providers.statistics import StatsHelper
from quasarr.search.sources.helpers import (
    get_login_required_hostnames,
    get_radarr_required_hostnames,
    get_sonarr_required_hostnames,
)
from quasarr.storage.config import Config
from quasarr.storage.setup.arr import missing_arr_client_requirement
from quasarr.storage.setup.radarr import is_radarr_configured
from quasarr.storage.setup.sonarr import is_sonarr_configured
from quasarr.storage.sqlite_database import DataBase


def _h(value: object) -> str:
    return escape(str(value), quote=True)


def _hostname_status(shared_state) -> dict[str, Any]:
    """Reuse the existing hostname-counting / skip-login / missing-*arr
    logic (mirrors ``quasarr.api._classic_dashboard``).
    """
    hostnames_config = Config("Hostnames")
    skip_login_db = DataBase("skip_login")
    hostname_issues = get_all_hostname_issues()
    login_required = set(get_login_required_hostnames())

    radarr_required = set(get_radarr_required_hostnames())
    radarr_ok = is_radarr_configured(shared_state)
    sonarr_required = set(get_sonarr_required_hostnames())
    sonarr_ok = is_sonarr_configured(shared_state)

    working_count = 0
    total_count = 0
    for site_key in shared_state.values.get("sites", []) or []:
        shorthand = site_key.lower()
        current_value = hostnames_config.get(shorthand)
        if not current_value:
            continue
        if shorthand in login_required:
            skip_val = skip_login_db.retrieve(shorthand)
            if skip_val and str(skip_val).lower() == "true":
                continue
        total_count += 1
        if shorthand in hostname_issues:
            continue
        if missing_arr_client_requirement(
            shorthand, radarr_required, sonarr_required, radarr_ok, sonarr_ok
        ):
            continue
        working_count += 1

    return {"working": working_count, "total": total_count}


def build_dashboard_model(shared_state) -> dict[str, Any]:
    """Pure model for the Dashboard page.

    Reads only cached shared_state values, Config, and DataBase - never
    ``shared_state.get_device()`` or ``get_packages()``.
    """
    jd_status = get_jdownloader_status(shared_state)
    hostnames = _hostname_status(shared_state)

    captcha_count = protected_captcha_count(shared_state)
    helper_active = bool(shared_state.values.get("helper_active"))

    flaresolverr_url = Config("FlareSolverr").get("url") or ""
    flaresolverr_skipped = bool(DataBase("skip_flaresolverr").retrieve("skipped"))

    api_key = Config("API").get("key") or ""
    internal_address = shared_state.values.get("internal_address", "") or ""

    stats: Mapping[str, Any] = StatsHelper(shared_state).get_stats()

    return {
        "jd_connected": bool(jd_status["connected"]),
        "jd_device_name": jd_status["device_name"],
        "hostnames_working": hostnames["working"],
        "hostnames_total": hostnames["total"],
        "captcha_count": captcha_count,
        "helper_active": helper_active,
        "flaresolverr_url": flaresolverr_url,
        "flaresolverr_skipped": flaresolverr_skipped,
        "flaresolverr_configured": bool(flaresolverr_url) and not flaresolverr_skipped,
        "api_key": api_key,
        "internal_address": internal_address,
        "stats": stats,
        "show_user": show_logout_link(),
    }


def _status_tile(heading: str, tag_text: str, tone: str, detail: str = "") -> str:
    detail_html = f"<p>{_h(detail)}</p>" if detail else ""
    body = f"{tag(tag_text, tone=tone)}{detail_html}"
    return tile(body, heading=heading, classes="is-compact")


def _dashboard_status_tiles(model: Mapping[str, Any]) -> str:
    if model["jd_connected"]:
        jd_tone, jd_text = "green", "Connected"
    else:
        jd_tone, jd_text = "red", "Disconnected"
    jd_detail = model["jd_device_name"] or ""
    jd_tile = _status_tile("JDownloader", jd_text, jd_tone, jd_detail)

    working = model["hostnames_working"]
    total = model["hostnames_total"]
    if total == 0:
        host_tone, host_text = "red", "None configured"
    elif working == 0:
        host_tone, host_text = "red", f"0 / {total} operational"
    elif working < total:
        host_tone, host_text = "blue", f"{working} / {total} operational"
    else:
        host_tone, host_text = "green", f"{working} / {total} operational"
    hostnames_tile = _status_tile("Hostnames", host_text, host_tone)

    captcha_count = model["captcha_count"]
    if captcha_count > 0:
        captcha_tone, captcha_text = "red", f"{captcha_count} waiting"
    else:
        captcha_tone, captcha_text = "green", "Clear"
    captcha_tile = _status_tile("CAPTCHA queue", captcha_text, captcha_tone)

    if model["flaresolverr_configured"]:
        fs_tone, fs_text = "green", "Configured"
    elif model["flaresolverr_skipped"]:
        fs_tone, fs_text = "gray", "Skipped"
    else:
        fs_tone, fs_text = "red", "Not configured"
    flaresolverr_tile = _status_tile("FlareSolverr", fs_text, fs_tone)

    return (
        '<div class="cds-kpi-row">'
        f"{jd_tile}{hostnames_tile}{captcha_tile}{flaresolverr_tile}"
        "</div>"
    )


def _dashboard_captcha_banner(model: Mapping[str, Any]) -> str:
    captcha_count = model["captcha_count"]
    if captcha_count <= 0:
        return ""

    plural = "s" if captcha_count != 1 else ""
    message = f"{captcha_count} link{plural} waiting for a CAPTCHA solution."
    if not model["helper_active"]:
        message += " SponsorsHelper can solve these automatically."

    actions = (
        f'<a class="cds-btn cds-btn--primary" href="/captcha">Solve CAPTCHA{plural}</a>'
    )
    return notification("warning", "CAPTCHA required", message, actions=actions)


def _dashboard_queue_tile() -> str:
    """Empty/loading placeholder filled client-side from
    ``GET /api/packages/list`` after first paint (see carbon.js
    ``loadDashboardQueue``). Never touches JDownloader server-side.
    """
    return (
        '<section class="cds-tile" id="dashboard-queue-tile" data-state="loading">'
        '<h2 class="cds-tile__heading">Top downloads</h2>'
        '<div class="cds-tile__content" id="dashboard-queue-content">'
        "<p>Loading queue…</p>"
        "</div></section>"
    )


def _dashboard_api_tile(model: Mapping[str, Any]) -> str:
    url = _h(model["internal_address"])
    api_key = _h(model["api_key"])
    return (
        '<section class="cds-tile" id="dashboard-api-tile">'
        '<h2 class="cds-tile__heading">API access</h2>'
        '<div class="cds-tile__content">'
        "<p>Use these settings for Newznab Indexer and SABnzbd Download Client "
        "in Radarr/Sonarr.</p>"
        '<div class="cds-field">'
        '<label class="cds-field__label" for="dashboard-api-url">URL</label>'
        f'<input class="cds-field__input" id="dashboard-api-url" type="text" value="{url}" readonly>'
        "</div>"
        '<button class="cds-btn cds-btn--ghost" type="button" data-action="copy" '
        'data-copy-target="dashboard-api-url">Copy URL</button>'
        '<div class="cds-field">'
        '<label class="cds-field__label" for="dashboard-api-key">API Key</label>'
        f'<input class="cds-field__input" id="dashboard-api-key" type="password" value="{api_key}" readonly>'
        "</div>"
        '<button class="cds-btn cds-btn--ghost" type="button" data-action="reveal" '
        'data-reveal-target="dashboard-api-key">Show</button> '
        '<button class="cds-btn cds-btn--ghost" type="button" data-action="copy" '
        'data-copy-target="dashboard-api-key">Copy Key</button>'
        "</div></section>"
    )


def _dashboard_summary_tile(model: Mapping[str, Any]) -> str:
    stats = model["stats"]
    columns = (
        TableColumn("metric", "Metric"),
        TableColumn("value", "Value", classes="is-num is-mono"),
    )
    rows = [
        {
            "metric": "Packages downloaded",
            "value": f"{int(stats.get('packages_downloaded', 0)):,}",
        },
        {
            "metric": "Failed downloads",
            "value": f"{int(stats.get('failed_downloads', 0)):,}",
        },
        {
            "metric": "Total CAPTCHA decryptions",
            "value": f"{int(stats.get('total_captcha_decryptions', 0)):,}",
        },
        {
            "metric": "Decryption success rate",
            "value": f"{float(stats.get('decryption_success_rate', 0)):.1f}%",
        },
    ]
    return data_table(columns, rows, caption="All-time summary")


def render_dashboard(shared_state) -> str:
    model = build_dashboard_model(shared_state)

    content = "".join(
        [
            _dashboard_captcha_banner(model),
            _dashboard_status_tiles(model),
            _dashboard_queue_tile(),
            _dashboard_api_tile(model),
            _dashboard_summary_tile(model),
        ]
    )

    return render_carbon_html(
        "dashboard",
        content,
        title="Dashboard",
        eyebrow="Overview",
        subtitle="JDownloader, hostnames, and download activity at a glance",
        captcha_count=model["captcha_count"],
        show_user=model["show_user"],
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _notification_settings_model(shared_state) -> dict[str, Any]:
    """Reads the already-cached ``notification_settings`` shared_state value
    (never the DB directly) - matching ``storage/AGENTS.md``'s
    refresh-then-cache contract and Classic's own dashboard read.
    """
    notification_settings = shared_state.values.get("notification_settings", {})
    if not isinstance(notification_settings, dict):
        notification_settings = {}

    toggles = notification_settings.get("toggles")
    if not isinstance(toggles, dict):
        toggles = {"discord": {}, "telegram": {}}
    silent = notification_settings.get("silent")
    if not isinstance(silent, dict):
        silent = {"discord": {}, "telegram": {}}

    settings = {
        "discord_webhook": notification_settings.get("discord_webhook") or "",
        "telegram_bot_token": notification_settings.get("telegram_bot_token") or "",
        "telegram_chat_id": notification_settings.get("telegram_chat_id") or "",
        "toggles": toggles,
        "silent": silent,
    }

    cases = [
        (notification_type.value, get_notification_type_label(notification_type))
        for notification_type in get_user_configurable_notification_types()
    ]
    return {"settings": settings, "cases": cases}


def _crypter_block_model(shared_state) -> dict[str, Any]:
    filecrypt_enabled = bool(shared_state.values.get("filecrypt_enabled", True))

    crypter_block_mode = shared_state.values.get("crypter_block_mode", "defer")
    if crypter_block_mode not in {"defer", "fail"}:
        crypter_block_mode = "defer"

    try:
        crypter_cooldown_hours = int(
            shared_state.values.get("crypter_cooldown_hours", 24)
        )
    except (TypeError, ValueError):
        crypter_cooldown_hours = 24
    crypter_cooldown_hours = max(24, crypter_cooldown_hours)

    try:
        sweep_window_minutes = int(
            shared_state.values.get("filecrypt_sweep_window_minutes", 15)
        )
    except (TypeError, ValueError):
        sweep_window_minutes = 15
    sweep_window_minutes = max(1, min(1440, sweep_window_minutes))

    sweep_window_override = shared_state.values.get("filecrypt_sweep_window_override")
    sweep_window_source = (
        shared_state.values.get("filecrypt_sweep_window_source") or "default"
    )

    return {
        "filecrypt_enabled": filecrypt_enabled,
        "mode": crypter_block_mode,
        "cooldown_hours": crypter_cooldown_hours,
        "sweep_window_minutes": sweep_window_minutes,
        "sweep_window_override": sweep_window_override,
        "sweep_window_source": sweep_window_source,
    }


def build_settings_model(shared_state) -> dict[str, Any]:
    """Pure model for the Settings page.

    Reads only cached shared_state values, Config, and DataBase - never
    ``shared_state.get_device()`` or ``get_packages()``.
    """
    jd_status = get_jdownloader_status(shared_state)
    jd_config = Config("JDownloader")

    api_key = Config("API").get("key") or ""
    internal_address = shared_state.values.get("internal_address", "") or ""

    timeout_slow_mode_settings = shared_state.values.get("timeout_slow_mode", {})
    if not isinstance(timeout_slow_mode_settings, dict):
        timeout_slow_mode_settings = {}

    flaresolverr_url = Config("FlareSolverr").get("url") or ""
    flaresolverr_skipped = bool(DataBase("skip_flaresolverr").retrieve("skipped"))

    radarr_config = Config("Radarr")
    sonarr_config = Config("Sonarr")

    return {
        "show_user": show_logout_link(),
        "captcha_count": protected_captcha_count(shared_state),
        "jdownloader": {
            "connected": bool(jd_status["connected"]),
            "user": jd_config.get("user") or "",
            "password": jd_config.get("password") or "",
            "device": jd_config.get("device") or "",
        },
        "api_key": api_key,
        "internal_address": internal_address,
        "timeout_slow_mode": timeout_slow_mode_settings,
        "crypter_block": _crypter_block_model(shared_state),
        "flaresolverr": {
            "url": flaresolverr_url,
            "skipped": flaresolverr_skipped,
        },
        "notifications": _notification_settings_model(shared_state),
        "radarr": {
            "url": radarr_config.get("url") or "",
            "api_key": radarr_config.get("api_key") or "",
        },
        "sonarr": {
            "url": sonarr_config.get("url") or "",
            "api_key": sonarr_config.get("api_key") or "",
        },
    }


def _appearance_section() -> str:
    return tile(
        '<div class="cds-field">'
        '<label class="cds-field__label" for="settings-theme">Theme</label>'
        '<select class="cds-field__select" id="settings-theme" data-action="theme-select">'
        '<option value="light">Light</option>'
        '<option value="dark">Dark</option>'
        "</select>"
        '<p class="cds-field__help">Applies immediately and is remembered on this device.</p>'
        "</div>",
        heading="Appearance",
    )


def _jdownloader_section(model: Mapping[str, Any]) -> str:
    jd = model["jdownloader"]
    status_tone = "green" if jd["connected"] else "red"
    status_text = "Connected" if jd["connected"] else "Disconnected"

    return tile(
        f"{tag(status_text, tone=status_tone)}"
        "<p>JDownloader must be running and connected to My JDownloader.</p>"
        '<div class="cds-field">'
        '<label class="cds-field__label" for="settings-jd-user">E-Mail</label>'
        f'<input class="cds-field__input" id="settings-jd-user" type="text" value="{_h(jd["user"])}">'
        "</div>"
        '<div class="cds-field">'
        '<label class="cds-field__label" for="settings-jd-pass">Password</label>'
        f'<input class="cds-field__input" id="settings-jd-pass" type="password" value="{_h(jd["password"])}">'
        "</div>"
        '<button class="cds-btn cds-btn--secondary" type="button" data-action="jd-verify">'
        "Verify Credentials</button>"
        '<p id="settings-jd-status" class="cds-field__help" aria-live="polite"></p>'
        '<div id="settings-jd-device-section" hidden>'
        '<div class="cds-field">'
        '<label class="cds-field__label" for="settings-jd-device">Instance</label>'
        f'<select class="cds-field__select" id="settings-jd-device" data-current="{_h(jd["device"])}"></select>'
        "</div>"
        '<button class="cds-btn cds-btn--primary" type="button" data-action="jd-save">Save</button>'
        '<p id="settings-jd-save-status" class="cds-field__help" aria-live="polite"></p>'
        "</div>",
        heading="JDownloader",
    )


def _api_timeouts_section(model: Mapping[str, Any]) -> str:
    api_key = _h(model["api_key"])
    url = _h(model["internal_address"])
    timeout_settings = model["timeout_slow_mode"]

    rows = []
    for timeout_key, definition in TIMEOUT_SLOW_MODE_DEFINITIONS.items():
        rows.append(
            toggle(
                f"settings-timeout-{timeout_key}",
                f"{definition['label']} (slow mode)",
                checked=bool(timeout_settings.get(timeout_key)),
                compact=True,
            )
        )

    return tile(
        '<div class="cds-field">'
        '<label class="cds-field__label" for="settings-api-url">URL</label>'
        f'<input class="cds-field__input" id="settings-api-url" type="text" value="{url}" readonly>'
        "</div>"
        '<div class="cds-field">'
        '<label class="cds-field__label" for="settings-api-key">API Key</label>'
        f'<input class="cds-field__input" id="settings-api-key" type="password" value="{api_key}" readonly>'
        "</div>"
        '<button class="cds-btn cds-btn--ghost" type="button" data-action="reveal" '
        'data-reveal-target="settings-api-key">Show</button> '
        '<button class="cds-btn cds-btn--ghost" type="button" data-action="copy" '
        'data-copy-target="settings-api-key">Copy Key</button> '
        '<button class="cds-btn cds-btn--danger-ghost" type="button" data-action="regenerate-api-key">'
        "Regenerate API Key</button>"
        "<h3>Timeouts</h3>"
        "<p>Enable slow mode only if you are willing to wait longer on slow sites.</p>"
        + "".join(rows)
        + '<button class="cds-btn cds-btn--primary" type="button" data-action="timeouts-save">'
        "Save Timeout Settings</button>"
        '<p id="settings-timeouts-status" class="cds-field__help" aria-live="polite"></p>',
        heading="API & Timeouts",
    )


_SWEEP_SOURCE_LABELS = {
    "stored": ("Web UI override", "blue"),
    "environment": ("Docker environment", "teal"),
    "default": ("Default", "gray"),
}


def _link_protection_section(model: Mapping[str, Any]) -> str:
    crypter = model["crypter_block"]
    defer_checked = " checked" if crypter["mode"] == "defer" else ""
    fail_checked = " checked" if crypter["mode"] == "fail" else ""

    source_text, source_tone = _SWEEP_SOURCE_LABELS.get(
        crypter["sweep_window_source"], _SWEEP_SOURCE_LABELS["default"]
    )
    override_is_none = crypter["sweep_window_override"] is None
    sweep_disabled = " disabled" if override_is_none else ""

    return tile(
        toggle(
            "settings-filecrypt-enabled",
            "Decrypt CAPTCHA-protected Filecrypt links",
            checked=crypter["filecrypt_enabled"],
        )
        + "<p>Disable while Filecrypt CAPTCHAs are unsolvable. Affected releases fail so "
        "*arr grabs an alternative. Applies to new grabs only.</p>"
        '<button class="cds-btn cds-btn--primary" type="button" data-action="filecrypt-save">'
        "Save Filecrypt Setting</button>"
        '<p id="settings-filecrypt-status" class="cds-field__help" aria-live="polite"></p>'
        "<h3>Linkcrypter-wide access blocks</h3>"
        '<fieldset class="cds-segmented">'
        '<legend class="cds-segmented__legend">When a linkcrypter blocks Quasarr</legend>'
        '<div class="cds-segmented__group" role="radiogroup" '
        'aria-label="Linkcrypter-wide access blocks">'
        '<label class="cds-segmented__option">'
        '<input class="cds-segmented__input" type="radio" name="settings-crypter-block-mode" '
        f'id="settings-crypter-block-mode-defer" value="defer"{defer_checked}>'
        '<span class="cds-segmented__label">Hold and retest</span>'
        "</label>"
        '<label class="cds-segmented__option">'
        '<input class="cds-segmented__input" type="radio" name="settings-crypter-block-mode" '
        f'id="settings-crypter-block-mode-fail" value="fail"{fail_checked}>'
        '<span class="cds-segmented__label">Fail immediately</span>'
        "</label>"
        "</div></fieldset>"
        "<p>Hold and retest keeps affected releases waiting in the queue until the cooldown "
        "expires. Fail immediately restores the legacy behavior at once: releases fail again "
        "so *arr grabs an alternative, and recorded blocks are kept but ignored until you "
        "switch back.</p>"
        '<div class="cds-field">'
        '<label class="cds-field__label" for="settings-crypter-cooldown-hours">Cooldown (hours)</label>'
        f'<input class="cds-field__input" id="settings-crypter-cooldown-hours" type="number" '
        f'min="24" step="1" value="{crypter["cooldown_hours"]}">'
        "</div>"
        '<div class="cds-field">'
        '<label class="cds-field__label" for="settings-filecrypt-sweep-window">'
        f"Filecrypt sweep window (minutes) {tag(source_text, tone=source_tone)}</label>"
        f'<input class="cds-field__input" id="settings-filecrypt-sweep-window" type="number" '
        f'min="1" max="1440" step="1" value="{crypter["sweep_window_minutes"]}"{sweep_disabled}>'
        "</div>"
        + toggle(
            "settings-filecrypt-sweep-window-default",
            "Use Docker/default value",
            checked=override_is_none,
            compact=True,
        )
        + '<button class="cds-btn cds-btn--primary" type="button" data-action="crypter-block-save">'
        "Save Block Settings</button>"
        '<p id="settings-crypter-block-status" class="cds-field__help" aria-live="polite"></p>',
        heading="Link Protection",
    )


def _flaresolverr_section(model: Mapping[str, Any]) -> str:
    fs = model["flaresolverr"]
    warning = ""
    if fs["skipped"]:
        warning = notification(
            "warning",
            "flaresolverr-next setup was skipped",
            "Some sites may not work until flaresolverr-next is configured.",
        )

    return tile(
        warning + "<p>"
        '<a href="https://github.com/rix1337/flaresolverr-next" target="_blank" '
        'rel="noopener noreferrer">flaresolverr-next</a> must be running and reachable '
        "to Quasarr for some sites to work.</p>"
        '<div class="cds-field">'
        '<label class="cds-field__label" for="settings-flaresolverr-url">URL</label>'
        f'<input class="cds-field__input" id="settings-flaresolverr-url" type="text" value="{_h(fs["url"])}">'
        "</div>"
        '<button class="cds-btn cds-btn--primary" type="button" data-action="flaresolverr-save">'
        "Save</button>"
        '<p id="settings-flaresolverr-status" class="cds-field__help" aria-live="polite"></p>',
        heading="FlareSolverr",
    )


def _notification_provider_card(
    provider: str, label: str, model: Mapping[str, Any]
) -> str:
    notifications = model["notifications"]
    settings = notifications["settings"]
    cases = notifications["cases"]

    provider_toggles = settings.get("toggles", {}).get(provider, {})
    provider_silent = settings.get("silent", {}).get(provider, {})

    if provider == "discord":
        credential_fields = (
            '<div class="cds-field">'
            '<label class="cds-field__label" for="settings-notification-discord-webhook">'
            "Webhook URL</label>"
            '<input class="cds-field__input" id="settings-notification-discord-webhook" '
            f'type="text" value="{_h(settings.get("discord_webhook", ""))}">'
            "</div>"
        )
    else:
        credential_fields = (
            '<div class="cds-field">'
            '<label class="cds-field__label" for="settings-notification-telegram-token">'
            "Bot Token</label>"
            '<input class="cds-field__input" id="settings-notification-telegram-token" '
            f'type="text" value="{_h(settings.get("telegram_bot_token", ""))}">'
            "</div>"
            '<div class="cds-field">'
            '<label class="cds-field__label" for="settings-notification-telegram-chat-id">'
            "Chat ID</label>"
            '<input class="cds-field__input" id="settings-notification-telegram-chat-id" '
            f'type="text" value="{_h(settings.get("telegram_chat_id", ""))}">'
            "</div>"
        )

    case_rows = []
    for case_key, case_label in cases:
        case_rows.append(
            '<div class="cds-toggle-row">'
            + toggle(
                f"settings-notif-{provider}-{case_key}",
                case_label,
                checked=provider_toggles.get(case_key, True),
                compact=True,
            )
            + toggle(
                f"settings-notif-{provider}-{case_key}-silent",
                "Silent",
                checked=provider_silent.get(case_key, False),
                compact=True,
            )
            + "</div>"
        )

    cases_json = _h(json.dumps([case_key for case_key, _label in cases]))

    return (
        '<div class="cds-tile__content">'
        f"<h3>{_h(label)}</h3>"
        f"{credential_fields}"
        f'<span id="settings-notification-{provider}-cases" hidden data-cases="{cases_json}"></span>'
        + "".join(case_rows)
        + f'<button class="cds-btn cds-btn--secondary" type="button" '
        f'data-action="notifications-test" data-provider="{provider}">Send Test</button>'
        f'<p id="settings-notification-{provider}-status" class="cds-field__help" aria-live="polite"></p>'
        "</div>"
    )


def _notifications_section(model: Mapping[str, Any]) -> str:
    body = (
        "<p>It is recommended to configure at least one provider below for an optimal "
        "user experience. One Save covers both providers below, so a typed edit "
        "anywhere in this section is never silently discarded.</p>"
        + _notification_provider_card("discord", "Discord", model)
        + _notification_provider_card("telegram", "Telegram", model)
        + '<button class="cds-btn cds-btn--primary" type="button" '
        'data-action="notifications-save">Save Notifications</button>'
        '<p id="settings-notifications-status" class="cds-field__help" aria-live="polite"></p>'
    )
    return f'<section class="cds-tile"><h2 class="cds-tile__heading">Notifications</h2>{body}</section>'


def _arr_service_card(service: str, label: str, model: Mapping[str, Any]) -> str:
    settings = model[service]
    return (
        '<div class="cds-tile__content">'
        f"<h3>{_h(label)}</h3>"
        '<div class="cds-field">'
        f'<label class="cds-field__label" for="settings-{service}-url">URL</label>'
        f'<input class="cds-field__input" id="settings-{service}-url" type="text" '
        f'value="{_h(settings["url"])}">'
        "</div>"
        '<div class="cds-field">'
        f'<label class="cds-field__label" for="settings-{service}-api-key">API Key</label>'
        f'<input class="cds-field__input" id="settings-{service}-api-key" type="text" '
        f'value="{_h(settings["api_key"])}">'
        "</div>"
        f'<button class="cds-btn cds-btn--primary" type="button" '
        f'data-action="{service}-save">Save {_h(label)} Settings</button> '
        f'<button class="cds-btn cds-btn--danger-ghost" type="button" '
        f'data-action="{service}-clear">Clear</button>'
        f'<p id="settings-{service}-status" class="cds-field__help" aria-live="polite"></p>'
        "</div>"
    )


def _arr_section(model: Mapping[str, Any]) -> str:
    body = (
        "<p>Required for configured movie or TV sources. Configure the client(s) you use.</p>"
        + _arr_service_card("radarr", "Radarr", model)
        + _arr_service_card("sonarr", "Sonarr", model)
    )
    return f'<section class="cds-tile"><h2 class="cds-tile__heading">*arr</h2>{body}</section>'


def render_settings(shared_state) -> str:
    model = build_settings_model(shared_state)

    content = "".join(
        [
            _appearance_section(),
            _jdownloader_section(model),
            _api_timeouts_section(model),
            _link_protection_section(model),
            _flaresolverr_section(model),
            _notifications_section(model),
            _arr_section(model),
        ]
    )

    return render_carbon_html(
        "settings",
        content,
        title="Settings",
        eyebrow="Configuration",
        subtitle="JDownloader, notifications, link protection, and *arr clients",
        captcha_count=model["captcha_count"],
        show_user=model["show_user"],
    )


__all__ = [
    "build_dashboard_model",
    "build_settings_model",
    "render_dashboard",
    "render_settings",
]
