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
from quasarr.constants import (
    TIMEOUT_SLOW_MODE_DEFINITIONS,
    TIMEOUT_SLOW_MODE_MULTIPLIER,
)
from quasarr.providers.auth import show_logout_link
from quasarr.providers.carbon_icons import render_icon
from quasarr.providers.carbon_templates import (
    field,
    grid,
    icon_button,
    kv_rows,
    notification,
    protected_captcha_count,
    render_carbon_html,
    status,
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
from quasarr.providers.version import get_version
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
    logic (mirrors ``quasarr.api._classic_dashboard``), and build a short,
    sanitized issue summary line from the same pass. The line never carries
    the raw stored error text (``hostname_issues[...]["error"]``) - only the
    shorthand and the sanitized ``operation`` label, matching the
    ``f"Error in {operation}"`` convention ``storage/setup/hostnames.py``
    already uses for the same data.
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
    issue_entries: list[str] = []
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
        issue = hostname_issues.get(shorthand)
        if issue:
            operation = issue.get("operation") or "unknown"
            issue_entries.append(f"{shorthand.upper()} error in {operation}")
            continue
        if missing_arr_client_requirement(
            shorthand, radarr_required, sonarr_required, radarr_ok, sonarr_ok
        ):
            continue
        working_count += 1

    if not issue_entries:
        issue_line = ""
    elif len(issue_entries) <= 2:
        issue_line = " · ".join(issue_entries)
    else:
        issue_line = (
            " · ".join(issue_entries[:2]) + f" · +{len(issue_entries) - 2} more"
        )

    return {"working": working_count, "total": total_count, "issue_line": issue_line}


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
        "hostnames_issue_line": hostnames["issue_line"],
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


def _status_tile(
    heading: str, text: str, tone: str, detail: str = "", *, detail_mono: bool = False
) -> str:
    detail_class = ' class="cds-mono"' if detail_mono else ""
    detail_html = f"<p{detail_class}>{_h(detail)}</p>" if detail else ""
    return tile(
        f"{status(text, tone, strong=True)}{detail_html}",
        heading=heading,
        classes="is-status",
    )


def _dashboard_status_tiles(model: Mapping[str, Any]) -> str:
    if model["jd_connected"]:
        jd_tile = _status_tile(
            "JDownloader",
            "Connected",
            "success",
            model["jd_device_name"] or "",
            detail_mono=True,
        )
    else:
        jd_tile = _status_tile(
            "JDownloader", "Disconnected", "error", "Check My JDownloader credentials"
        )

    working, total = model["hostnames_working"], model["hostnames_total"]
    if total == 0:
        hostnames_tile = _status_tile(
            "Hostnames",
            "None configured",
            "neutral",
            "Add a hostname to start searching",
        )
    elif working == total:
        hostnames_tile = _status_tile(
            "Hostnames", f"{working} of {total} operational", "success"
        )
    elif working == 0:
        hostnames_tile = _status_tile(
            "Hostnames",
            f"0 of {total} operational",
            "error",
            model.get("hostnames_issue_line", ""),
        )
    else:
        hostnames_tile = _status_tile(
            "Hostnames",
            f"{working} of {total} operational",
            "warning",
            model.get("hostnames_issue_line", ""),
        )

    if model["flaresolverr_configured"]:
        fs_tile = _status_tile(
            "FlareSolverr",
            "Reachable",
            "success",
            model.get("flaresolverr_url", ""),
            detail_mono=True,
        )
    elif model["flaresolverr_skipped"]:
        fs_tile = _status_tile(
            "FlareSolverr", "Skipped", "neutral", "Some sites need flaresolverr-next"
        )
    else:
        fs_tile = _status_tile(
            "FlareSolverr", "Not configured", "error", "Configure it in Settings"
        )

    if model["helper_active"]:
        helper_tile = _status_tile(
            "SponsorsHelper", "Active", "success", "Solving CAPTCHAs automatically"
        )
    else:
        helper_tile = _status_tile(
            "SponsorsHelper",
            "Inactive",
            "neutral",
            "Automated CAPTCHA solving for sponsors",
        )

    return f'<div class="cds-kpi-row">{jd_tile}{hostnames_tile}{fs_tile}{helper_tile}</div>'


def _dashboard_captcha_banner(model: Mapping[str, Any]) -> str:
    count = model["captcha_count"]
    if count <= 0:
        return ""
    plural = "s" if count != 1 else ""
    verb = "is" if count == 1 else "are"
    return (
        '<section class="cds-notification cds-notification--warning cds-notification--inline" role="alert">'
        f'<p class="cds-notification__message"><strong>Action required.</strong> {count} link{plural} {verb} waiting for a CAPTCHA solution.</p>'
        f'<a class="cds-btn cds-btn--ghost" href="/captcha">Solve CAPTCHA{plural} →</a>'
        "</section>"
    )


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
    body = (
        "<p>Use these settings for Newznab Indexer and SABnzbd Download Client "
        "in Radarr/Sonarr.</p>"
        '<div class="cds-field-row">'
        f'<span class="cds-field-row__value cds-mono" id="dashboard-api-url">{url}</span>'
        + icon_button(
            "copy",
            "Copy URL",
            action="copy",
            data={"copy-target": "dashboard-api-url"},
        )
        + "</div>"
        '<div class="cds-field-row">'
        f'<input class="cds-field-row__input" id="dashboard-api-key" type="password" '
        f'value="{api_key}" readonly>'
        + icon_button(
            "view",
            "Show API key",
            action="reveal",
            data={"reveal-target": "dashboard-api-key"},
        )
        + icon_button(
            "copy",
            "Copy Key",
            action="copy",
            data={"copy-target": "dashboard-api-key"},
        )
        + "</div>"
    )
    return (
        '<section class="cds-tile" id="dashboard-api-tile">'
        '<h2 class="cds-tile__heading">API access</h2>'
        f'<div class="cds-tile__content">{body}</div>'
        "</section>"
    )


def _dashboard_summary_tile(model: Mapping[str, Any]) -> str:
    stats = model["stats"]
    rows = (
        (
            "Download attempts",
            f"{int(stats.get('total_download_attempts', 0)):,}",
        ),
        (
            "Download success rate",
            f"{float(stats.get('download_success_rate', 0)):.1f}%",
        ),
        (
            "CAPTCHA decryptions",
            f"{int(stats.get('total_captcha_decryptions', 0)):,}",
        ),
    )
    head_row = (
        '<div class="cds-tile__head-row">'
        '<h2 class="cds-tile__heading">All time</h2>'
        '<a class="cds-btn cds-btn--ghost" href="/statistics">Statistics →</a>'
        "</div>"
    )
    return tile(head_row + kv_rows(rows))


def render_dashboard(shared_state) -> str:
    model = build_dashboard_model(shared_state)

    content = (
        _dashboard_captcha_banner(model)
        + _dashboard_status_tiles(model)
        + grid(
            [
                _dashboard_queue_tile(),
                grid(
                    [_dashboard_api_tile(model), _dashboard_summary_tile(model)],
                    "stack",
                ),
            ],
            "dashboard",
        )
    )

    return render_carbon_html(
        "dashboard",
        content,
        title="Dashboard",
        eyebrow=f"Quasarr v{get_version()}",
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


def _setting_row(label: str, help_text: str, control: str) -> str:
    """One label/description line with its control on the right.

    The design's Appearance tile is built from these rows; the control is
    renderer-owned markup (a switcher, a link button), never user input.
    """
    return (
        '<div class="cds-setting-row">'
        '<div class="cds-setting-row__text">'
        f'<span class="cds-setting-row__label">{_h(label)}</span>'
        f'<span class="cds-setting-row__help">{_h(help_text)}</span>'
        "</div>"
        f'<div class="cds-setting-row__control">{control}</div>'
        "</div>"
    )


def _switcher(
    items: tuple[tuple[str, str, bool], ...],
    *,
    legend: str,
    name: str,
    action: str = "",
    id_prefix: str = "",
) -> str:
    """The design's content switcher: a radio group rendered as one
    segmented control. Used for the theme preference and for the
    linkcrypter block policy, so both look and behave identically.

    ``legend`` names the group for assistive technology and is visually
    hidden; the visible caption is the surrounding row label or subheading.
    """
    options = []
    for value, label, checked in items:
        element_id = f' id="{_h(id_prefix)}{_h(value)}"' if id_prefix else ""
        options.append(
            '<label class="cds-switcher__item">'
            f'<input type="radio" name="{_h(name)}"{element_id} value="{_h(value)}"'
            f"{' checked' if checked else ''}>"
            f"<span>{_h(label)}</span></label>"
        )
    action_attr = f' data-action="{_h(action)}"' if action else ""
    return (
        f'<fieldset class="cds-switcher"{action_attr}>'
        f'<legend class="cds-visually-hidden">{_h(legend)}</legend>'
        + "".join(options)
        + "</fieldset>"
    )


def _appearance_section() -> str:
    """Theme preference plus the escape hatch back to the Classic UI.

    The theme lives in ``localStorage``, which the server cannot read, so
    "System" always ships pre-selected and ``carbon.js``'s
    ``updateThemeSwitcher()`` corrects the selection on DOMContentLoaded.
    """
    theme_switcher = _switcher(
        (
            ("light", "Light", False),
            ("dark", "Dark", False),
            ("system", "System", True),
        ),
        legend="Theme",
        name="theme",
        action="theme-switch",
    )
    classic_link = (
        '<a class="cds-btn cds-btn--tertiary" href="/ui/classic">Open Classic UI'
        f"{render_icon('launch', class_name='cds-icon cds-icon--sm')}</a>"
    )
    return tile(
        _setting_row(
            "Theme",
            "Applies immediately and is remembered on this device.",
            theme_switcher,
        )
        + _setting_row(
            "Interface design",
            "Carbon (new) is active",
            classic_link,
        ),
        heading="Appearance",
    )


def _jdownloader_section(model: Mapping[str, Any]) -> str:
    jd = model["jdownloader"]
    connected = bool(jd["connected"])
    device = jd["device"]

    if device:
        options = f'<option value="{_h(device)}" selected>{_h(device)}</option>'
    else:
        options = '<option value="">Verify credentials to list instances</option>'

    head_row = (
        '<div class="cds-tile__head-row">'
        '<h2 class="cds-tile__heading">JDownloader</h2>'
        + status(
            "Connected" if connected else "Disconnected",
            "success" if connected else "error",
        )
        + "</div>"
    )

    return tile(
        head_row
        + '<p class="cds-tile__help">JDownloader must be running and connected to '
        "My JDownloader.</p>"
        + field("settings-jd-user", "E-mail", value=jd["user"])
        + field(
            "settings-jd-pass", "Password", value=jd["password"], input_type="password"
        )
        + '<div class="cds-field">'
        '<label class="cds-field__label" for="settings-jd-device">Instance</label>'
        f'<select class="cds-field__select" id="settings-jd-device" '
        f'data-current="{_h(device)}">{options}</select>'
        "</div>"
        '<div class="cds-btn-row">'
        '<button class="cds-btn cds-btn--primary" type="button" data-action="jd-save">'
        "Save</button>"
        '<button class="cds-btn cds-btn--tertiary" type="button" data-action="jd-verify">'
        "Verify credentials</button>"
        "</div>"
        '<p id="settings-jd-status" class="cds-field__help" aria-live="polite"></p>'
    )


def _timeout_row(timeout_key: str, definition: Mapping[str, Any], enabled: bool) -> str:
    """One slow-mode switch plus the timeout it currently produces.

    Both help strings are rendered as data attributes so ``carbon.js`` can
    swap the visible one the moment the switch is flipped (the value saves
    on change) without re-deriving seconds client-side - the multiplier
    stays owned by ``quasarr/constants``.
    """
    base_seconds = int(definition["base_seconds"])
    slow_seconds = base_seconds * TIMEOUT_SLOW_MODE_MULTIPLIER
    normal_help = f"Current: {base_seconds} s (normal)"
    slow_help = f"Current: {slow_seconds} s (slow)"
    return (
        '<div class="cds-timeout-row" '
        f'data-timeout-help-normal="{_h(normal_help)}" '
        f'data-timeout-help-slow="{_h(slow_help)}">'
        + toggle(
            f"settings-timeout-{timeout_key}",
            f"{definition['label']} (slow mode)",
            checked=enabled,
            help_text=slow_help if enabled else normal_help,
        )
        + "</div>"
    )


def _api_timeouts_section(model: Mapping[str, Any]) -> str:
    api_key = _h(model["api_key"])
    url = _h(model["internal_address"])
    timeout_settings = model["timeout_slow_mode"]

    rows = "".join(
        _timeout_row(timeout_key, definition, bool(timeout_settings.get(timeout_key)))
        for timeout_key, definition in TIMEOUT_SLOW_MODE_DEFINITIONS.items()
    )

    api_rows = (
        '<div class="cds-field-row">'
        '<span class="cds-field-row__label">URL</span>'
        f'<span class="cds-field-row__value cds-mono" id="settings-api-url">{url}</span>'
        + icon_button(
            "copy",
            "Copy URL",
            action="copy",
            data={"copy-target": "settings-api-url"},
        )
        + "</div>"
        '<div class="cds-field-row">'
        '<label class="cds-field-row__label" for="settings-api-key">API key</label>'
        f'<input class="cds-field-row__input" id="settings-api-key" type="password" '
        f'value="{api_key}" readonly>'
        + icon_button(
            "view",
            "Show API key",
            action="reveal",
            data={"reveal-target": "settings-api-key"},
        )
        + icon_button(
            "copy",
            "Copy API key",
            action="copy",
            data={"copy-target": "settings-api-key"},
        )
        + "</div>"
    )

    return tile(
        rows
        + '<p id="settings-timeouts-status" class="cds-field__help" aria-live="polite"></p>'
        '<h3 class="cds-subheading">API access</h3>'
        '<p class="cds-field__help">Use this URL and key for Newznab Indexer and '
        "SABnzbd Download Client in Radarr/Sonarr.</p>"
        + api_rows
        + '<div class="cds-btn-row">'
        '<button class="cds-btn cds-btn--tertiary" type="button" '
        'data-action="regenerate-api-key">Regenerate API key</button>'
        "</div>",
        heading="API & timeouts",
        help_text=(
            "Slow mode triples the request timeout for that operation. "
            "Each switch saves as soon as you flip it."
        ),
    )


_SWEEP_SOURCE_LABELS = {
    "stored": ("Web UI override", "blue"),
    "environment": ("Docker environment", "teal"),
    "default": ("Default", "gray"),
}


def _link_protection_section(model: Mapping[str, Any]) -> str:
    crypter = model["crypter_block"]

    source_text, source_tone = _SWEEP_SOURCE_LABELS.get(
        crypter["sweep_window_source"], _SWEEP_SOURCE_LABELS["default"]
    )
    override_is_none = crypter["sweep_window_override"] is None
    sweep_disabled = " disabled" if override_is_none else ""

    mode_switcher = _switcher(
        (
            ("defer", "Hold and retest", crypter["mode"] == "defer"),
            ("fail", "Fail immediately", crypter["mode"] == "fail"),
        ),
        legend="When a linkcrypter blocks Quasarr",
        name="settings-crypter-block-mode",
        id_prefix="settings-crypter-block-mode-",
    )

    number_fields = grid(
        [
            '<div class="cds-field">'
            '<label class="cds-field__label" for="settings-crypter-cooldown-hours">'
            "Cooldown (hours)</label>"
            f'<input class="cds-field__input" id="settings-crypter-cooldown-hours" '
            f'type="number" min="24" step="1" value="{crypter["cooldown_hours"]}">'
            "</div>",
            '<div class="cds-field">'
            '<label class="cds-field__label" for="settings-filecrypt-sweep-window">'
            f"Filecrypt sweep window (minutes) {tag(source_text, tone=source_tone)}</label>"
            f'<input class="cds-field__input" id="settings-filecrypt-sweep-window" '
            f'type="number" min="1" max="1440" step="1" '
            f'value="{crypter["sweep_window_minutes"]}"{sweep_disabled}>'
            "</div>",
        ],
        "2",
    )

    return tile(
        toggle(
            "settings-filecrypt-enabled",
            "Decrypt CAPTCHA-protected Filecrypt links",
            checked=crypter["filecrypt_enabled"],
            help_text=(
                "Disable while Filecrypt CAPTCHAs are unsolvable. Affected releases "
                "fail so *arr grabs an alternative. Applies to new grabs only."
            ),
        )
        + '<h3 class="cds-subheading">Linkcrypter access blocks</h3>'
        + mode_switcher
        + "<p>Hold and retest pauses affected releases until the cooldown ends. Fail "
        "immediately fails them at once so *arr grabs an alternative; blocks stay "
        "recorded either way.</p>"
        + number_fields
        + toggle(
            "settings-filecrypt-sweep-window-default",
            "Use Docker default",
            checked=override_is_none,
            compact=True,
        )
        + '<div class="cds-btn-row">'
        '<button class="cds-btn cds-btn--primary" type="button" '
        'data-action="link-protection-save">Save</button>'
        "</div>"
        '<p id="settings-link-protection-status" class="cds-field__help" '
        'aria-live="polite"></p>',
        heading="Link protection",
    )


def _flaresolverr_section(model: Mapping[str, Any]) -> str:
    fs = model["flaresolverr"]
    if fs["url"] and not fs["skipped"]:
        state = status("Reachable", "success")
    elif fs["skipped"]:
        state = status("Skipped", "neutral")
    else:
        state = status("Not configured", "error")

    warning = ""
    if fs["skipped"]:
        warning = notification(
            "warning",
            "flaresolverr-next setup was skipped",
            "Some sites may not work until flaresolverr-next is configured.",
        )

    head_row = (
        '<div class="cds-tile__head-row">'
        '<h2 class="cds-tile__heading">FlareSolverr</h2>'
        f"{state}</div>"
    )

    return tile(
        head_row + '<p class="cds-tile__help">'
        '<a href="https://github.com/rix1337/flaresolverr-next" target="_blank" '
        'rel="noopener noreferrer">flaresolverr-next</a> must be running and reachable '
        "to Quasarr for some sites to work.</p>"
        + warning
        + field("settings-flaresolverr-url", "URL", value=fs["url"])
        + '<div class="cds-btn-row">'
        '<button class="cds-btn cds-btn--primary" type="button" '
        'data-action="flaresolverr-save">Save</button>'
        "</div>"
        '<p id="settings-flaresolverr-status" class="cds-field__help" '
        'aria-live="polite"></p>'
    )


def _notification_matrix(
    provider: str,
    cases: Any,
    provider_toggles: Mapping[str, Any],
    provider_silent: Mapping[str, Any],
) -> str:
    """The design's EVENT / ENABLED / SILENT matrix.

    Each switch keeps the exact id ``carbon.js``'s ``readCheckboxValue()``
    reads. Their own label text is visually hidden by ``.cds-matrix`` (the
    visible caption is the row's EVENT cell) but stays in the accessible
    name, so a screen reader still hears which event and which column a
    switch belongs to.
    """
    rows = []
    for case_key, case_label in cases:
        rows.append(
            '<div class="cds-matrix__row">'
            f'<span class="cds-matrix__label">{_h(case_label)}</span>'
            + toggle(
                f"settings-notif-{provider}-{case_key}",
                f"{case_label} enabled",
                checked=provider_toggles.get(case_key, True),
                compact=True,
            )
            + toggle(
                f"settings-notif-{provider}-{case_key}-silent",
                f"{case_label} silent",
                checked=provider_silent.get(case_key, False),
                compact=True,
            )
            + "</div>"
        )
    return (
        '<div class="cds-matrix">'
        '<div class="cds-matrix__head"><span>Event</span><span>Enabled</span>'
        "<span>Silent</span></div>" + "".join(rows) + "</div>"
    )


def _notifications_section(model: Mapping[str, Any]) -> str:
    notifications = model["notifications"]
    settings = notifications["settings"]
    cases = notifications["cases"]
    cases_json = _h(json.dumps([case_key for case_key, _label in cases]))
    cases_marker = (
        '<span id="settings-notification-{provider}-cases" hidden '
        f'data-cases="{cases_json}"></span>'
    )

    discord = (
        '<h3 class="cds-subheading">Discord</h3>'
        + field(
            "settings-notification-discord-webhook",
            "Webhook URL",
            value=settings.get("discord_webhook", ""),
        )
        + cases_marker.format(provider="discord")
        + _notification_matrix(
            "discord",
            cases,
            settings.get("toggles", {}).get("discord", {}),
            settings.get("silent", {}).get("discord", {}),
        )
    )

    telegram_configured = bool(settings.get("telegram_bot_token")) and bool(
        settings.get("telegram_chat_id")
    )
    telegram = (
        '<details class="cds-details"><summary>Telegram '
        '<span class="cds-tile__count">'
        f"({'configured' if telegram_configured else 'not configured'})"
        "</span></summary>"
        + field(
            "settings-notification-telegram-token",
            "Bot token",
            value=settings.get("telegram_bot_token", ""),
        )
        + field(
            "settings-notification-telegram-chat-id",
            "Chat ID",
            value=settings.get("telegram_chat_id", ""),
        )
        + cases_marker.format(provider="telegram")
        + _notification_matrix(
            "telegram",
            cases,
            settings.get("toggles", {}).get("telegram", {}),
            settings.get("silent", {}).get("telegram", {}),
        )
        + "</details>"
    )

    return tile(
        discord + telegram + '<div class="cds-btn-row">'
        '<button class="cds-btn cds-btn--primary" type="button" '
        'data-action="notifications-save">Save</button>'
        '<button class="cds-btn cds-btn--tertiary" type="button" '
        'data-action="notifications-test">Send test</button>'
        "</div>"
        '<p id="settings-notifications-status" class="cds-field__help" '
        'aria-live="polite"></p>',
        heading="Notifications",
        help_text=(
            "Configure at least one provider for an optimal user experience. One "
            "Save covers both providers, so a typed edit anywhere in this tile is "
            "never silently discarded; Send test reaches every provider you have "
            "configured."
        ),
    )


def _arr_service_block(service: str, label: str, model: Mapping[str, Any]) -> str:
    """One *arr client's fields plus its own Clear.

    Clearing stays per service on purpose: blanking the API key field and
    saving cannot clear a stored key, because ``saveArrSettings()`` falls
    back to the fetched key so a URL-only edit never wipes it.
    """
    settings = model[service]
    return (
        f'<h3 class="cds-subheading">{_h(label)}</h3>'
        + field(f"settings-{service}-url", "URL", value=settings["url"])
        + field(f"settings-{service}-api-key", "API key", value=settings["api_key"])
        + '<div class="cds-btn-row">'
        f'<button class="cds-btn cds-btn--danger-ghost" type="button" '
        f'data-action="{service}-clear-open">Clear</button>'
        "</div>"
    )


def _arr_section(model: Mapping[str, Any]) -> str:
    return tile(
        _arr_service_block("radarr", "Radarr", model)
        + _arr_service_block("sonarr", "Sonarr", model)
        + '<div class="cds-btn-row">'
        '<button class="cds-btn cds-btn--primary" type="button" '
        'data-action="arr-save">Save</button>'
        "</div>"
        '<p id="settings-arr-status" class="cds-field__help" aria-live="polite"></p>',
        heading="*arr clients",
        help_text=(
            "Required for configured movie or TV sources. Configure the client(s) "
            "you use."
        ),
    )


def render_settings(shared_state) -> str:
    model = build_settings_model(shared_state)

    content = grid(
        [
            _appearance_section(),
            _jdownloader_section(model),
            _api_timeouts_section(model),
            _link_protection_section(model),
            _flaresolverr_section(model),
            _notifications_section(model),
            _arr_section(model),
        ],
        "settings",
    )

    return render_carbon_html(
        "settings",
        content,
        title="Settings",
        eyebrow="Configuration",
        captcha_count=model["captcha_count"],
        show_user=model["show_user"],
    )


__all__ = [
    "build_dashboard_model",
    "build_settings_model",
    "render_dashboard",
    "render_settings",
]
