# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

"""Carbon renderers for the eight temporary first-run/reconfiguration setup
servers (path, hostnames, per-source hostname credentials, FlareSolverr,
*arr client selector, Radarr, Sonarr, JDownloader). See
``quasarr/storage/setup/AGENTS.md`` for the module-shape, startup-order, and
skip-flag contracts these forms sit inside - this module only renders their
``GET /`` form; every route, submit handler, ``temp_server_success`` signal,
and skip-table write stays exactly as it was.

Every ``render_setup_*()`` function returns a standalone Carbon document via
``carbon_templates.render_carbon_simple_page()`` - no side nav, header bar,
or CAPTCHA badge chrome, matching every other auth/system page.
None of these forms needs the full-shell modal/toast landmarks
(``render_carbon_simple_page`` deliberately omits them): Save/skip actions
either submit a real ``<form>`` (byte-identical POST contract, full-page
navigation into the existing ``render_success``/``render_fail``/
``render_reconnect_success`` response) or use one of a handful of small
setup-only ``carbon.js`` actions for the genuinely dynamic steps (hostname
import, hostname status/credentials detail, JDownloader device
verification, skip buttons that must POST via fetch before navigating).

Secrets never enter this module's markup: credential fields always start
blank (matching ``api/config/carbon.py``'s Hostnames view), and the
uncontrolled per-hostname ``details`` exception text (which can embed a
configured hostname) is fetched fresh from the existing authenticated
``GET /api/hostnames`` endpoint only when a row's Details panel is
expanded, landing solely as a text node - never into a data attribute,
never logged.
"""

from __future__ import annotations

from html import escape
from typing import Any, Mapping, Sequence

from quasarr.providers.carbon_templates import (
    notification,
    page_header,
    render_carbon_simple_page,
    tile,
)
from quasarr.search.sources.helpers import get_source_metadata
from quasarr.storage.config import Config
from quasarr.storage.setup.hostnames import build_hostname_rows
from quasarr.storage.sqlite_database import DataBase


def _h(value: object) -> str:
    return escape(str(value), quote=True)


def _shell(title: str, content: str) -> str:
    page = page_header("Setup", title) + content
    # wide=True: the narrow 440px default card measurably
    # cramped every setup form once it started actually applying at
    # desktop widths (see the CSS unclosed-block fix) - the Hostnames
    # page's per-row table in particular squeezed its hostname input to
    # ~190px and its inline credentials panel to ~150px wide. Login and
    # every status/error page (the other render_carbon_simple_page
    # callers) deliberately keep the narrow default.
    return render_carbon_simple_page(page, title=title, wide=True)


def _text_field(
    field_id: str,
    label: str,
    *,
    name: str | None = None,
    value: str = "",
    placeholder: str = "",
    input_type: str = "text",
    required: bool = False,
    disabled: bool = False,
    form: str | None = None,
) -> str:
    """Hand-rolled ``cds-field`` input supporting placeholder/disabled/form,
    which ``carbon_templates.field()`` does not - matches the established
    hand-rolled pattern in ``api/config/carbon.py::_hostname_row_html``.
    """
    safe_id = _h(field_id)
    attrs = [
        f'id="{safe_id}"',
        f'name="{_h(name or field_id)}"',
        f'type="{input_type}"',
    ]
    if value:
        attrs.append(f'value="{_h(value)}"')
    if placeholder:
        attrs.append(f'placeholder="{_h(placeholder)}"')
    if required:
        attrs.append("required")
    if disabled:
        attrs.append("disabled")
    if form:
        attrs.append(f'form="{_h(form)}"')
    attrs.append('autocomplete="off"')
    return (
        '<div class="cds-field">'
        f'<label class="cds-field__label" for="{safe_id}">{_h(label)}</label>'
        f'<input class="cds-field__input" {" ".join(attrs)}>'
        "</div>"
    )


# ---------------------------------------------------------------------------
# 1. Path
# ---------------------------------------------------------------------------


def render_setup_path(current_path: str) -> str:
    title = "Press 'Save' to set desired path for configuration"
    form = (
        '<form action="/api/config" method="post" data-guard-submit>'
        + _text_field("config_path", "Configuration path", placeholder=current_path)
        + '<button class="cds-btn cds-btn--primary" type="submit">Save</button>'
        "</form>"
    )
    return _shell(title, tile(form))


# ---------------------------------------------------------------------------
# 2. Hostnames
# ---------------------------------------------------------------------------


def _setup_hostname_row_html(
    row: Mapping[str, Any], *, flaresolverr_skipped: bool
) -> str:
    """Renders through the SAME dense-row builder the main Hostnames page
    uses (``quasarr.api.config.carbon._hostname_row_html``) - imported
    lazily, matching the project's no-module-scope-``.carbon``-import
    convention (see that module's own docstring) - rather than keeping a
    second copy of the row markup. What genuinely differs here stays a
    parameter on that shared call: the status is not independently
    clickable (a separate "Details" button - now living inside the
    capabilities cell, there being no room for a fifth grid column - toggles
    the inline credentials panel below the row instead of a JS-fetched
    modal); the status tag keeps its ``hostname-status-tag-*`` id so
    ``checkSetupHostnameCredentials()`` can still flip it green in place
    after a successful check; the skip-login note keeps its own id/wording
    ("Open Details", since Details still exists here); and the hostname
    input needs an explicit ``form="hostnames-form"`` association, because
    ``render_setup_hostnames`` deliberately leaves that ``<form>`` element
    empty (see its own docstring) so a typed-then-hidden credential value
    can never ride along in the save POST.
    """
    from quasarr.api.config.carbon import _hostname_row_html

    field_id = row["id"]
    safe_id = _h(field_id)
    details_id = f"hostname-details-{safe_id}"

    detail_panel_parts = [
        f'<p class="cds-field__help" id="hostname-details-text-{safe_id}">Loading...</p>'
    ]
    hostname_value = row.get("hostname")
    if hostname_value:
        href = (
            hostname_value
            if hostname_value.startswith(("http://", "https://"))
            else f"https://{hostname_value}"
        )
        detail_panel_parts.append(
            f'<a class="cds-btn cds-btn--tertiary" href="{_h(href)}" '
            f'target="_blank" rel="noopener noreferrer">Open {_h(field_id.upper())}</a>'
        )
    if row.get("supports_login"):
        if row.get("requires_flaresolverr") and flaresolverr_skipped:
            detail_panel_parts.append(
                '<p class="cds-hostname-credentials__warning">'
                "This site requires flaresolverr-next, which was skipped during "
                "setup. Configure it in the web UI before checking credentials."
                "</p>"
                '<a class="cds-btn cds-btn--secondary" href="/flaresolverr">'
                "Configure flaresolverr-next</a>"
            )
        else:
            detail_panel_parts.append(
                _text_field(f"hostname-cred-user-{safe_id}", "Login")
                + _text_field(
                    f"hostname-cred-pass-{safe_id}", "Password", input_type="password"
                )
                + f'<p class="cds-field__help" id="hostname-cred-status-{safe_id}"></p>'
                + '<button class="cds-btn cds-btn--primary" type="button" '
                f'data-action="setup-hostname-credentials-check" '
                f'data-hostname-id="{safe_id}">Check &amp; Save Session</button>'
            )

    details_button = (
        '<button class="cds-btn cds-btn--ghost" type="button" '
        f'data-action="setup-hostname-toggle-details" data-hostname-id="{safe_id}" '
        f'aria-expanded="false" aria-controls="{details_id}">Details</button>'
    )
    skip_note_html = ""
    if row.get("skip_login"):
        skip_note_html = (
            f'<p class="cds-hostname-table__note" id="hostname-skip-banner-{safe_id}">'
            "Login was skipped for this site. Open Details to check credentials "
            "again.</p>"
        )

    row_html = _hostname_row_html(
        row,
        status_action="",
        status_wrapper_id=f"hostname-status-tag-{safe_id}",
        trailing_caps_html=details_button,
        skip_note_html=skip_note_html,
        input_form="hostnames-form",
    )

    return (
        row_html
        + f'<div id="{details_id}" class="cds-hostname-credentials" hidden>'
        + "".join(detail_panel_parts)
        + "</div>"
    )


def render_setup_hostnames(shared_state) -> str:
    title = "Set at least one valid hostname"
    rows = build_hostname_rows(shared_state)
    stored_url = Config("Settings").get("hostnames_url") or ""
    flaresolverr_skipped = bool(DataBase("skip_flaresolverr").retrieve("skipped"))

    rows_html = "".join(
        _setup_hostname_row_html(row, flaresolverr_skipped=flaresolverr_skipped)
        for row in rows
    )
    # Same wrapper the main Hostnames page uses around its rows: without it,
    # carbon.css's narrow-viewport rule (.cds-hostname-table__row's forced
    # 760px min-width) still applies - the rows carry that class from the
    # shared row builder regardless - but with no .cds-hostname-table
    # ancestor to become a horizontal scroll container, so the rows simply
    # overflowed this page's narrower (760px-max) card instead of scrolling.
    table_html = f'<div class="cds-hostname-table">{rows_html}</div>'

    instructions = tile(
        "<p>If you're having trouble setting this up, take a closer look at "
        '<a href="https://github.com/rix1337/Quasarr?tab=readme-ov-file#quasarr" '
        'target="_blank" rel="noopener noreferrer">the instructions</a>.</p>'
    )

    import_section = tile(
        "<p>Import hostname definitions from a URL (one entry per line, "
        "formatted as <code>ab = example.com</code>).</p>"
        '<div class="cds-field">'
        '<label class="cds-field__label" for="hostnames-import-url">Source URL</label>'
        f'<input class="cds-field__input" id="hostnames-import-url" '
        f'name="hostnames_url" form="hostnames-form" type="url" '
        f'value="{_h(stored_url)}" '
        'placeholder="https://example.invalid/hostnames.ini" autocomplete="off">'
        "</div>"
        '<button class="cds-btn cds-btn--secondary" type="button" '
        'data-action="setup-hostnames-import">Import from URL</button>'
        '<p class="cds-field__help" id="setup-hostnames-import-status"></p>',
        heading="Import hostnames",
    )

    # The hostnames-form element is deliberately left EMPTY (association
    # only) rather than wrapping the rows tile: every Details panel's
    # credential inputs live inside that tile, and an element merely being
    # `hidden` does NOT exclude it from its enclosing form's submission -
    # only `disabled` or the absence of any form association does. Nesting
    # the tile inside <form> would let a typed-then-hidden password ride
    # along in POST /api/hostnames, where it is silently discarded. Every
    # control that SHOULD submit here (each hostname input, the import URL
    # field, the Save button) instead declares an explicit
    # form="hostnames-form" attribute; credential inputs declare none, so
    # they belong to no form at all.
    sources_section = (
        '<form id="hostnames-form" action="/api/hostnames" method="post" '
        "data-guard-submit></form>"
        + tile(table_html, heading="Configured sources")
        + '<button class="cds-btn cds-btn--primary" type="submit" '
        'form="hostnames-form">Save</button>'
    )

    content = instructions + import_section + sources_section
    return _shell(title, content)


# ---------------------------------------------------------------------------
# 3. Per-source hostname credentials
# ---------------------------------------------------------------------------


def render_setup_hostname_credentials(shared_state, shorthand: str, domain: str) -> str:
    title = f"Set User and Password for {shorthand}"
    shorthand_lower = shorthand.lower()

    flaresolverr_url = Config("FlareSolverr").get("url")
    source_meta = get_source_metadata().get(shorthand_lower, {})
    is_missing_flaresolverr = bool(
        source_meta.get("requires_flaresolverr") and not flaresolverr_url
    )

    register_info = tile(
        "<p>If required register account at: "
        f'<a href="https://{_h(domain)}" target="_blank" rel="noopener noreferrer">'
        f"{_h(domain)}</a>!</p>"
    )

    flaresolverr_section = ""
    if is_missing_flaresolverr:
        flaresolverr_section = tile(
            notification(
                "warning",
                "flaresolverr-next Required",
                "This site requires flaresolverr-next. Configure it below "
                "before checking credentials.",
            )
            + '<form action="/api/flaresolverr_inline" method="post" data-guard-submit>'
            + _text_field(
                "hostname-cred-flaresolverr-url",
                "flaresolverr-next URL",
                name="url",
                placeholder="http://192.168.0.1:8191/v1",
            )
            + '<button class="cds-btn cds-btn--secondary" type="submit">Save URL</button>'
            "</form>"
        )

    credentials_section = tile(
        f'<form action="/api/credentials/{_h(shorthand)}" method="post" data-guard-submit>'
        + _text_field(
            "user", "Login", disabled=is_missing_flaresolverr, placeholder="User"
        )
        + _text_field(
            "password",
            "Password",
            input_type="password",
            disabled=is_missing_flaresolverr,
            placeholder="Password",
        )
        + '<button class="cds-btn cds-btn--primary" type="submit">Save</button> '
        + '<button class="cds-btn cds-btn--secondary" type="button" '
        f'data-action="setup-credentials-skip" data-shorthand="{_h(shorthand_lower)}">'
        "Skip for now</button>"
        + '<p class="cds-field__help" id="setup-credentials-skip-status"></p>'
        "</form>"
        "<p>Skipping will allow Quasarr to start, but this site won't work "
        "until credentials are provided.</p>",
        heading="Credentials",
    )

    content = register_info + flaresolverr_section + credentials_section
    return _shell(title, content)


# ---------------------------------------------------------------------------
# 4. FlareSolverr
# ---------------------------------------------------------------------------


def render_setup_flaresolverr(shared_state) -> str:
    title = "Set flaresolverr-next URL"
    current_url = Config("FlareSolverr").get("url") or ""

    info = tile(
        '<p><a href="https://github.com/rix1337/flaresolverr-next" target="_blank" '
        'rel="noopener noreferrer">flaresolverr-next</a> must be running and '
        "reachable to Quasarr for some sites to work.</p>"
    )

    form = (
        '<form action="/api/flaresolverr" method="post" data-guard-submit>'
        + _text_field(
            "url",
            "flaresolverr-next URL",
            placeholder="http://192.168.0.1:8191/v1",
            value=current_url,
        )
        + '<button class="cds-btn cds-btn--primary" type="submit">Save</button> '
        + '<button class="cds-btn cds-btn--secondary" type="button" '
        'data-action="setup-flaresolverr-skip">Skip for now</button>'
        + '<p class="cds-field__help" id="setup-flaresolverr-skip-status"></p>'
        "</form>"
    )

    content = info + tile(form)
    return _shell(title, content)


# ---------------------------------------------------------------------------
# 5. *arr client selector
# ---------------------------------------------------------------------------


def render_setup_arr_client(
    radarr_required_sites: Sequence[str], sonarr_required_sites: Sequence[str]
) -> str:
    title = "Choose your *arr client"
    radarr_sites = ", ".join(sorted(site.upper() for site in radarr_required_sites))
    sonarr_sites = ", ".join(sorted(site.upper() for site in sonarr_required_sites))

    info = notification(
        "info",
        "Both clients are supported",
        "Configured hostnames support both movie and TV searches. Quasarr "
        "needs one *arr client to launch, but does not require both. You "
        "can configure the other later in Settings.",
    )

    form = (
        '<form action="/api/arr/client" method="post" data-guard-submit>'
        '<button class="cds-btn cds-btn--primary" type="submit" name="client" '
        f'value="radarr">Use Radarr</button>'
        f'<p class="cds-field__help">Required by: {_h(radarr_sites)}</p>'
        '<button class="cds-btn cds-btn--primary" type="submit" name="client" '
        f'value="sonarr">Use Sonarr</button>'
        f'<p class="cds-field__help">Required by: {_h(sonarr_sites)}</p>'
        "</form>"
    )

    content = info + tile(form)
    return _shell(title, content)


# ---------------------------------------------------------------------------
# 6/7. Radarr / Sonarr
# ---------------------------------------------------------------------------


def _radarr_sonarr_content(
    *,
    client_name: str,
    required_sites: Sequence[str],
    current_url: str,
    current_api_key: str,
    save_action: str,
    reason: str,
) -> str:
    site_list = ", ".join(sorted(s.upper() for s in required_sites))
    info = notification(
        "info",
        f"{client_name} required",
        f"One or more configured hostnames ({site_list}) require {client_name} "
        f"to look up {reason}. Provide your {client_name} URL and API key below.",
    )
    form = (
        f'<form action="{save_action}" method="post" data-guard-submit>'
        + _text_field(
            "url",
            f"{client_name} URL",
            placeholder="http://192.168.0.1:7878"
            if client_name == "Radarr"
            else "http://192.168.0.1:8989",
            value=current_url,
            required=True,
        )
        + _text_field(
            "api_key",
            f"{client_name} API Key",
            placeholder=f"{client_name} API key",
            value=current_api_key,
            required=True,
        )
        + '<button class="cds-btn cds-btn--primary" type="submit">Save</button>'
        "</form>"
    )
    return info + tile(form)


def render_setup_radarr(current_url: str, current_api_key: str, required_sites) -> str:
    title = "Set Radarr URL and API Key"
    content = _radarr_sonarr_content(
        client_name="Radarr",
        required_sites=required_sites,
        current_url=current_url,
        current_api_key=current_api_key,
        save_action="/api/radarr/save",
        reason="movie metadata",
    )
    return _shell(title, content)


def render_setup_sonarr(current_url: str, current_api_key: str, required_sites) -> str:
    title = "Set Sonarr URL and API Key"
    content = _radarr_sonarr_content(
        client_name="Sonarr",
        required_sites=required_sites,
        current_url=current_url,
        current_api_key=current_api_key,
        save_action="/api/sonarr/save",
        reason="series metadata",
    )
    return _shell(title, content)


# ---------------------------------------------------------------------------
# 8. JDownloader
# ---------------------------------------------------------------------------


def render_setup_jdownloader() -> str:
    title = "Set your credentials for My JDownloader"

    info = tile(
        "<p>If required register account at: <a "
        'href="https://my.jdownloader.org/login.html#register" target="_blank" '
        'rel="noopener noreferrer">my.jdownloader.org</a>!</p>'
        "<p><strong>JDownloader must be running and connected to My "
        "JDownloader!</strong></p>"
    )

    verify_form = tile(
        _text_field("jd-user", "E-Mail", placeholder="user@example.org")
        + _text_field("jd-pass", "Password", input_type="password")
        + '<button class="cds-btn cds-btn--secondary" type="button" '
        'id="setup-jd-verify-btn" data-action="setup-jd-verify">'
        "Verify Credentials</button>"
        '<p class="cds-field__help" id="setup-jd-verify-status"></p>'
    )

    device_section = (
        '<section class="cds-tile" id="setup-jd-device-tile" hidden>'
        '<h2 class="cds-tile__heading">Select device</h2>'
        '<div class="cds-tile__content">'
        '<form id="setup-jd-device-form" action="/api/store_jdownloader" '
        'method="post" data-guard-submit>'
        '<input type="hidden" id="jd-hidden-user" name="user">'
        '<input type="hidden" id="jd-hidden-pass" name="pass">'
        '<div class="cds-field">'
        '<label class="cds-field__label" for="jd-device">JDownloader</label>'
        '<select class="cds-field__select" id="jd-device" name="device"></select>'
        "</div>"
        '<button class="cds-btn cds-btn--primary" type="submit">Save</button>'
        "</form>"
        "<p>Saving may take a while!</p>"
        "</div>"
        "</section>"
    )

    content = info + verify_form + device_section
    return _shell(title, content)


__all__ = [
    "render_setup_path",
    "render_setup_hostnames",
    "render_setup_hostname_credentials",
    "render_setup_flaresolverr",
    "render_setup_arr_client",
    "render_setup_radarr",
    "render_setup_sonarr",
    "render_setup_jdownloader",
]
