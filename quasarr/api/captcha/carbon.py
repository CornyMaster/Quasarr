# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

"""Carbon renderer for the six protected-provider CAPTCHA pages.

``render_captcha(shared_state, provider)`` is the exact symbol
``quasarr/api/captcha/__init__.py``'s six ``GET /captcha/<provider>`` routes
(``hide``, ``junkies``, ``he``, ``keeplinks``, ``tolink``, ``filecrypt``)
import lazily, one call per request. ``/captcha`` itself stays the untouched
Classic-only redirect dispatcher.

Quasarr never solves a CAPTCHA server-side: every protected provider is
solved in the user's own browser by a Tampermonkey userscript served from
``/captcha/<provider>.user.js``. This module only re-skins the page around
that flow - the userscript bodies, the provider URLs, the quick-transfer
query-parameter contract (``transfer_url``/``pkg_id``/``pkg_title``/
``pkg_pass`` and Junkies' ``jk_user``/``jk_pass``), the manual
``/captcha/bypass-submit`` form fields, the base64 package-selector payload
shape, and the ``captcha_attempts_<package_id>`` failed-attempt counter are
all byte-for-byte the same contract Classic already serves. Only the DOM
markup and interactivity are rebuilt for strict CSP: tutorial timing,
first-use storage, reset, package selection, and manual-submit wiring move
into ``carbon.js`` behind delegated ``data-action`` handlers instead of the
inline ``onclick``/``onsubmit``/``<script>`` Classic embeds per page load.
``check_package_exists()`` is reused unchanged and always raises 404 for a
missing package under both UIs; its Classic body stays byte-for-byte what
Classic already returns, while a Carbon request now gets the Carbon
package-not-found page (``carbon_templates.render_carbon_error_page``) with
the same "Package not found or already solved." message text.

The page itself is a centred 720px column (``.cds-page--narrow``): an
eyebrow/title pair, an inline info notification, one tile holding the
package selector plus a Crypter/Mirror/Links meta line, a 48px primary CTA
plus a secondary source-page CTA, a helper line that reopens the exact same
first-time tutorial flow as the primary CTA (never a fork of
``openProvider()`` in carbon.js), a collapsible manual-submission tile, and
a danger-ghost delete action. The "Back" button is the one control this
design deliberately drops - the delete action, the source-page link, the
package selector and the manual-submission textarea all keep working.
"""

from __future__ import annotations

import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from html import escape
from urllib.parse import quote, unquote, urlsplit

from bottle import request

from quasarr.api.captcha import check_package_exists, is_he_link, is_junkies_link
from quasarr.providers.auth import show_logout_link
from quasarr.providers.carbon_icons import render_icon
from quasarr.providers.carbon_templates import (
    grid,
    notification,
    page_header,
    protected_captcha_count,
    render_carbon_html,
    tile,
)
from quasarr.storage.config import Config

# Mirrors the Classic `provider_names` map in
# `quasarr.api.captcha.setup_captcha_routes.render_userscript_section`.
_PROVIDER_NAMES = {
    "filecrypt": "FileCrypt",
    "hide": "Hide",
    "junkies": "Junkies",
    "he": "HE",
    "keeplinks": "KeepLinks",
    "tolink": "ToLink",
}


def _h(value: object) -> str:
    return escape(str(value), quote=True)


def _decode_payload():
    """Same contract as Classic's private `decode_payload()`: the ``data``
    query parameter is urlsafe-base64 JSON; any failure is returned as an
    ``{"error": ...}`` mapping instead of raising.
    """
    encoded = request.query.get("data")
    try:
        decoded = urlsafe_b64decode(unquote(encoded)).decode()
        return json.loads(decoded)
    except Exception as exc:
        return {"error": f"Failed to decode payload: {str(exc)}"}


def _delete_url(package_id, title):
    delete_url = f"/captcha/delete/{package_id}"
    if title:
        delete_url += f"?title={quote(title)}"
    return delete_url


def _link_url(link):
    return link[0] if isinstance(link, (list, tuple)) else link


def _captcha_type_for_links(sj, dj, he_hostname, links):
    """Same classification order Classic's `render_package_selector` uses."""
    if any("hide." in _link_url(link) for link in links):
        return "hide"
    if any(is_junkies_link(sj, dj, link) for link in links):
        return "junkies"
    if any(is_he_link(he_hostname, link) for link in links):
        return "he"
    if any("keeplinks." in _link_url(link) for link in links):
        return "keeplinks"
    if any("tolink." in _link_url(link) for link in links):
        return "tolink"
    return "filecrypt"


def _render_error_page(shared_state, message):
    content = notification(
        "error",
        "CAPTCHA data error",
        message,
        actions='<a class="cds-btn cds-btn--secondary" href="/">Back</a>',
    )
    return render_carbon_html(
        "captcha",
        content,
        title="CAPTCHA",
        eyebrow="CAPTCHA",
        captcha_count=protected_captcha_count(shared_state),
        show_user=show_logout_link(),
    )


def _render_package_tile(
    shared_state, current_package_id, provider_label, mirror, count
):
    """One tile: the existing package selector (relabeled "Package · N
    waiting"), reused unchanged for 2+ packages, plus a meta line that
    always shows the current package's crypter/mirror/link-count context
    even with only one package queued.
    """
    protected = shared_state.get_db("protected").retrieve_all_titles()
    selector_html = ""
    if len(protected) > 1:
        hostnames = shared_state.values["config"]("Hostnames")
        sj = hostnames.get("sj")
        dj = hostnames.get("dj")
        he_hostname = hostnames.get("he")

        options = []
        for pkg_id, raw in protected:
            data = json.loads(raw)
            title = data.get("title", "Unknown")
            links = data.get("links", [])
            password = data.get("password", "")
            pkg_mirror = data.get("mirror")
            original_url = data.get("original_url")

            rapid = [ln for ln in links if "rapidgator" in ln[1].lower()]
            others = [ln for ln in links if "rapidgator" not in ln[1].lower()]
            prioritized = rapid + others

            page_payload = {
                "package_id": pkg_id,
                "title": title,
                "password": password,
                "mirror": pkg_mirror,
                "links": prioritized,
                "original_url": original_url,
            }
            encoded = urlsafe_b64encode(json.dumps(page_payload).encode()).decode()
            captcha_type = _captcha_type_for_links(sj, dj, he_hostname, prioritized)
            option_value = f"{captcha_type}|{quote(encoded)}"
            selected = " selected" if pkg_id == current_package_id else ""
            options.append(
                f'<option value="{_h(option_value)}"{selected}>{_h(title)}</option>'
            )

        label_text = f"Package · {len(protected)} waiting"
        selector_html = (
            '<div class="cds-field">'
            '<label class="cds-field__label" for="captcha-package-select">'
            f"{_h(label_text)}</label>"
            '<select id="captcha-package-select" class="cds-field__select cds-mono" '
            'data-action="captcha-package-select">' + "".join(options) + "</select>"
            "</div>"
        )

    meta_line = (
        '<p class="cds-meta">'
        f"Crypter: <strong>{_h(provider_label)}</strong> · "
        f"Mirror: <strong>{_h(mirror)}</strong> · "
        f"Links: <strong>{count}</strong>"
        "</p>"
    )
    return tile(f"{selector_html}{meta_line}")


def _render_failed_attempts_warning(package_id, title):
    actions = (
        '<a class="cds-btn cds-btn--danger-ghost" '
        f'href="{_h(_delete_url(package_id, title))}">Delete Package</a>'
    )
    body = notification(
        "warning",
        "Multiple Failed Attempts Detected",
        "This CAPTCHA has failed multiple times. The link may be offline or "
        "require a different solution method. Please verify the link is "
        "still valid, or delete this package if it's no longer available.",
        actions=actions,
    )
    return f'<div id="failed-attempts-warning" hidden>{body}</div>'


def _render_primary_actions(
    url, package_id, title, password, provider, provider_name, original_url
):
    base_url = request.urlparts.scheme + "://" + request.urlparts.netloc
    transfer_url = f"{base_url}/captcha/quick-transfer"

    extra_params = ""
    if provider == "junkies":
        junkies_user = Config("JUNKIES").get("user")
        junkies_pass = Config("JUNKIES").get("password")
        if junkies_user and junkies_pass:
            extra_params = (
                f"&jk_user={quote(junkies_user)}&jk_pass={quote(junkies_pass)}"
            )

    separator = "&" if "?" in url else "?"
    open_url = (
        f"{url}{separator}"
        f"transfer_url={quote(transfer_url)}&"
        f"pkg_id={quote(package_id)}&"
        f"pkg_title={quote(title)}&"
        f"pkg_pass={quote(password)}"
        f"{extra_params}"
    )

    userscript_url = f"/captcha/{provider}.user.js"
    storage_key = f"hide{provider_name}SetupInstructions"

    open_button = (
        '<button type="button" class="cds-btn cds-btn--primary cds-btn--cta" '
        'data-action="captcha-open" '
        f'data-open-url="{_h(open_url)}" data-storage-key="{_h(storage_key)}">'
        f"{_h(f'Open {provider_name} & get links')}</button>"
    )

    reset_button = (
        '<button type="button" class="cds-btn cds-btn--ghost" '
        'data-action="captcha-reset-tutorial" '
        f'data-storage-key="{_h(storage_key)}" hidden>Reset Setup Guide</button>'
    )

    source_button = ""
    if original_url:
        source_button = (
            '<button type="button" class="cds-btn cds-btn--secondary cds-btn--cta" '
            'data-action="captcha-open-source" '
            f'data-source-url="{_h(original_url)}">Open source page</button>'
        )

    # The helper line points at the exact same reset-tutorial action and
    # storage key as the Reset Setup Guide button below - it reopens the
    # real tutorial modal (via resetTutorial() in carbon.js), never
    # captcha-open, which would silently navigate straight to the provider
    # once tutorialSeen() is true instead of explaining anything.
    helper_line = (
        '<p class="cds-field__help">First time? '
        '<button type="button" class="cds-text-link" '
        'data-action="captcha-reset-tutorial" '
        f'data-storage-key="{_h(storage_key)}">'
        "Install Tampermonkey and the Quasarr userscript"
        "</button> — the flow guides you once.</p>"
    )

    # First-time-setup instructions are pre-rendered server-side (never
    # assembled from URL literals in carbon.js) and revealed by JS inside
    # the shared modal only on first use; see carbon.js `openProvider()`.
    tutorial_content = (
        '<div id="captcha-tutorial-content" hidden>'
        '<p style="margin-bottom: 8px;">'
        '<a href="https://www.tampermonkey.net/" target="_blank" '
        'rel="noopener noreferrer">1. On mobile Safari/Firefox or any '
        "Desktop Browser install Tampermonkey</a></p>"
        '<p style="margin-top: 0; margin-bottom: 8px;">'
        f'<a href="{_h(userscript_url)}" target="_blank" '
        f'rel="noopener noreferrer">2. Install the {_h(provider_name)} '
        "userscript</a></p>"
        '<p style="margin-top: 0; margin-bottom: 12px;">'
        "3. Open link, solve CAPTCHAs, and links are automatically sent "
        "back to Quasarr!</p>"
        "</div>"
    )

    return tile(
        f'<div class="cds-captcha-actions">{open_button}{source_button}'
        f"{reset_button}</div>{helper_line}{tutorial_content}"
    )


def _render_manual_submission(package_id, title, password):
    body = (
        "<details data-manual-submit>"
        "<summary>Show Manual Submission</summary>"
        '<div class="cds-manual-submit-body">'
        "<p>If the userscript doesn't work, you can manually paste the "
        "links below:</p>"
        '<form action="/captcha/bypass-submit" method="post" '
        'enctype="multipart/form-data" data-action="captcha-manual-submit">'
        f'<input type="hidden" name="package_id" value="{_h(package_id)}">'
        f'<input type="hidden" name="title" value="{_h(title)}">'
        f'<input type="hidden" name="password" value="{_h(password)}">'
        '<div class="cds-field">'
        '<label class="cds-field__label" for="captcha-links-input">'
        "Paste the download links (one per line)</label>"
        '<textarea id="captcha-links-input" name="links" rows="5" '
        'class="cds-field__textarea"></textarea>'
        "</div>"
        '<button type="submit" class="cds-btn cds-btn--secondary">'
        "Submit links</button>"
        "</form>"
        "</div>"
        "</details>"
    )
    return tile(body, heading="Manual submission", classes="is-compact")


def _render_delete_action(package_id, title):
    delete_url = _delete_url(package_id, title)
    return (
        '<div class="cds-captcha-actions">'
        '<button class="cds-btn cds-btn--danger-ghost" type="button" '
        'data-action="package-delete" '
        f'data-href="{_h(delete_url)}">'
        f"{render_icon('trash-can')}Delete package &amp; files</button>"
        "</div>"
    )


def render_captcha(shared_state, provider):
    provider_name = _PROVIDER_NAMES.get(provider)
    if provider_name is None:
        raise ValueError(f"Unsupported CAPTCHA provider: {provider}")

    payload = _decode_payload()
    if "error" in payload:
        return _render_error_page(shared_state, payload["error"])

    package_id = payload.get("package_id")
    title = payload.get("title")
    password = payload.get("password") or ""
    urls = payload.get("links")
    original_url = payload.get("original_url")

    # check_package_exists() must run before any link extraction below -
    # exactly Classic's keeplinks/tolink/filecrypt order - so a stale
    # package_id (already solved/deleted, possibly with absent or empty
    # links) reaches the intended 404 instead of an IndexError turning
    # into an unhandled 500.
    check_package_exists(package_id)

    url = urls[0][0] if isinstance(urls[0], (list, tuple)) else urls[0]
    # Never a hardcoded/invented value - the mirror shown to the user is
    # always derived from the actual runtime link, never written literally.
    mirror = urlsplit(url).netloc

    info_notification = notification(
        "info",
        "Links return automatically.",
        "Solve the CAPTCHA on the crypter page — the userscript sends the "
        "links back to Quasarr.",
    ).replace(
        'cds-notification--info"',
        'cds-notification--info cds-notification--inline"',
        1,
    )

    # The package tile, the failed-attempts warning, the primary-actions
    # tile and the manual-submission tile carry no margin of their own
    # (.cds-tile has none) - stacked as plain siblings they would render
    # flush against each other with no gap. grid(..., "stack") is the
    # existing helper for exactly this: a 16px gap between every child.
    tiles = grid(
        [
            _render_package_tile(
                shared_state, package_id, provider_name, mirror, len(urls)
            ),
            _render_failed_attempts_warning(package_id, title),
            _render_primary_actions(
                url, package_id, title, password, provider, provider_name, original_url
            ),
            _render_manual_submission(package_id, title, password),
        ],
        "stack",
    )

    body = "".join([info_notification, tiles, _render_delete_action(package_id, title)])

    page = (
        f'<div class="cds-captcha-page" data-package-id="{_h(package_id)}">'
        '<div class="cds-page--narrow">'
        + page_header("Link protection", "Solve CAPTCHA", title or "")
        + body
        + "</div></div>"
    )

    return render_carbon_html(
        "captcha",
        page,
        title=f"{provider_name} CAPTCHA",
        eyebrow="Link protection",
        captcha_count=protected_captcha_count(shared_state),
        show_user=show_logout_link(),
    )


__all__ = ["render_captcha"]
