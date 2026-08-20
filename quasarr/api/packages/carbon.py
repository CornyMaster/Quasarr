# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

"""Carbon Downloads data contract and page renderer.

Builds the `PackageListResponse` schema consumed by the Carbon Downloads page
from the existing SABnzbd-shaped package aggregation, and renders the Carbon
Downloads page shell itself: `render_downloads(shared_state)`.

The row builders (`build_package_list_response()` and friends) are
self-contained and never import Classic's HTML-rendering helpers in
`quasarr.api.packages`, so `_render_packages_content()` and
`/api/packages/content` stay byte-for-byte unaffected by this module.

`render_downloads()` never touches JDownloader itself: it renders an empty
loading skeleton (the same house pattern `api.carbon._dashboard_queue_tile()`
established for the Dashboard queue preview) and every deferred/queue/
history/other row is built client-side in `carbon.js` from the JSON
`GET /api/packages/list` contract built for exactly this purpose. This
keeps one single row-rendering implementation (JS) instead of duplicating it
in Python, and keeps page construction free of JD I/O like every other Carbon
page.

Security contract: a row here never carries a protected URL, source hostname,
sweep ID, offer ID, operation ID, or link fingerprint - only the sanitized
fields the schema below allows.
"""

import re

from bottle import request

from quasarr.downloads.packages import (
    DEFERRED_STATUS_PREFIX,
    PROTECTED_STATUS_PREFIX,
    get_packages_for_device,
)
from quasarr.providers.auth import show_logout_link
from quasarr.providers.carbon_icons import render_icon
from quasarr.providers.carbon_templates import (
    notification,
    protected_captcha_count,
    render_carbon_html,
)

# Sentinel timeleft values meaning "no known ETA yet" (see downloads/packages
# format_eta() and the initial "23:59:59" default before any ETA is known).
_UNKNOWN_ETA_VALUES = frozenset({"23:59:59", "??:??:??"})

# HistoryRow.error can carry a raw exception message (e.g. the generic
# "Unexpected error: {e}" persisted by downloads/__init__.py's failure path),
# which is not guaranteed to be free of a protected URL/hostname. Both
# patterns are applied only to that one field before it enters the row.
_URL_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*://\S+")
_BARE_HOST_PATTERN = re.compile(
    r"\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+(?P<tld>[a-zA-Z]{2,24})\b"
)
# A conservative allowlist of real-world TLD-shaped endings, deliberately
# excluding common media/archive extensions (mkv, rar, nfo, srt, ...) so
# ordinary release-title text is never mistaken for a hostname.
_HOSTNAME_TLDS = frozenset(
    {
        "com",
        "net",
        "org",
        "io",
        "co",
        "info",
        "biz",
        "xyz",
        "cc",
        "me",
        "tv",
        "to",
        "link",
        "click",
        "site",
        "online",
        "pw",
        "top",
        "club",
        "host",
        "download",
        "cloud",
        "invalid",
        "de",
        "nl",
        "ru",
        "uk",
        "eu",
        "in",
        "cn",
        "app",
        "dev",
        "page",
        "live",
    }
)
_REDACTED_LINK_MARKER = "[link removed]"

# Filename prefixes Classic's queue renderer strips before display. Kept as a
# private, self-contained copy so this module never imports Classic's
# rendering helpers.
_QUEUE_NAME_PREFIXES = (
    "[Downloading] ",
    "[Extracting] ",
    "[Paused] ",
    "[Linkgrabber] ",
    PROTECTED_STATUS_PREFIX,
)

_DEFERRED_NAME_PREFIXES = (DEFERRED_STATUS_PREFIX, PROTECTED_STATUS_PREFIX)

_CRYPTER_LABELS = {"filecrypt": "Filecrypt", "junkies": "Junkies"}
_REASON_LABELS = {"ip_block_suspected": "IP access block suspected"}

QUEUE_STATUS_VALUES = frozenset(
    {"waiting_captcha", "downloading", "extracting", "queued"}
)
HISTORY_STATUS_VALUES = frozenset({"completed", "failed"})
# "retest" is part of the contract and stays a valid, accepted enum value,
# but the current backend never emits it - see _deferred_state()'s docstring.
DEFERRED_STATE_VALUES = frozenset({"observing", "cooldown", "probe_queued", "retest"})


def _label(value, labels):
    text = str(value or "unknown")
    return labels.get(text, text.replace("_", " ").strip().capitalize())


def _scrub_protected_links(text):
    """Redact URL-shaped and bare-hostname-shaped substrings from free text.

    The projection's security contract is absolute - no protected URL or
    source hostname may reach the response - but HistoryRow.error can carry a
    raw exception message that is not guaranteed to be free of one (e.g. the
    generic "Unexpected error: {e}" reason downloads/__init__.py persists on
    an unclassified failure). Applied only to that one field; every other
    free-text field (name, labels) is either a fixed label or expected to be
    a release title, never a URL.
    """
    if not text:
        return text
    scrubbed = _URL_PATTERN.sub(_REDACTED_LINK_MARKER, text)

    def _redact_bare_host(match):
        if match.group("tld").lower() in _HOSTNAME_TLDS:
            return _REDACTED_LINK_MARKER
        return match.group(0)

    return _BARE_HOST_PATTERN.sub(_redact_bare_host, scrubbed)


def _nonneg_int(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _clamp_percentage(value):
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0


def _format_size_label(mb=None, bytes_val=None):
    if bytes_val is not None:
        if bytes_val == 0:
            return "? MB"
        if bytes_val < 1024:
            return f"{bytes_val} B"
        if bytes_val < 1024 * 1024:
            return f"{bytes_val // 1024} KB"
        mb = bytes_val / (1024 * 1024)
    if mb is None or mb == 0:
        return "? MB"
    if mb < 1024:
        return f"{mb:.0f} MB"
    return f"{mb / 1024:.1f} GB"


def _strip_prefix(name, prefixes):
    for prefix in prefixes:
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def _queue_status(filename):
    """The shared status projection: filename prefixes -> one of the four
    QueueRow status values. Never mutates the raw item; Classic's own prefix
    handling and `timeleft` stay untouched.
    """
    if "[CAPTCHA" in filename:
        return "waiting_captcha"
    if "[Extracting]" in filename:
        return "extracting"
    if "[Paused]" in filename:
        return "queued"
    if "[Linkgrabber]" in filename:
        return "queued"
    return "downloading"


def _deferred_state(deferred, *, probe_requested):
    """Map the live crypter-cooldown projection onto the DeferredRow state enum.

    `hold_type` ("provisional" vs "crypter_cooldown") is the primary signal:
    a provisional hold is still gathering evidence ("observing"). Once the
    crypter itself is cooling, an operator-queued probe takes precedence
    ("probe_queued" - the next check is already scheduled), otherwise a
    plain cooldown.

    "retest" is a valid schema value (see DEFERRED_STATE_VALUES) but is never
    emitted here. `retest_members` (crypter_sweeps.decision_snapshot()) is
    only ever non-empty for a `healthy`/`individual` decision, which
    `_legacy_shaped_snapshot()` maps to legacy state "available" - so
    `hold_type == "crypter_cooldown"` (which requires legacy state
    "cooldown") and a non-empty retest queue are mutually exclusive within
    one consistent read of the same row (see
    downloads/packages/__init__.py's project_package_defer() invariant: a
    transition between two reads can never be projected half-applied). An
    earlier version of this function derived "retest" from a second,
    independently-timed crypter_projection() read; that second read could
    observe a cooldown -> healthy transition that read #1 (inside
    get_packages_for_device()) had not yet seen, producing an internally
    contradictory row (state="retest" alongside cohort_retest_depth=0) that
    could flicker between requests. Removed for that reason - `retest`
    becomes reachable only once a genuine same-read per-package signal
    exists; `cohort_retest_depth` remains the count `_build_deferred_row()`
    surfaces for a "Retest queue: N" display, matching Classic.
    """
    hold_type = deferred.get("hold_type")
    if hold_type == "crypter_cooldown":
        if probe_requested:
            return "probe_queued"
        return "cooldown"
    return "observing"


def _build_queue_row(item):
    """Project one raw queue item (linkgrabber/downloader/protected) into a
    QueueRow. Defensive against missing/malformed fields: every value falls
    back to a safe default rather than raising.
    """
    filename = str(item.get("filename", "Unknown"))
    status = _queue_status(filename)
    name = _strip_prefix(filename, _QUEUE_NAME_PREFIXES)

    timeleft = str(item.get("timeleft", "??:??:??"))
    eta_unknown = timeleft in _UNKNOWN_ETA_VALUES
    eta = "" if eta_unknown else timeleft

    bytes_val = item.get("bytes", 0) or 0
    mb = item.get("mb", 0) or 0
    size_label = (
        _format_size_label(bytes_val=bytes_val)
        if bytes_val
        else _format_size_label(mb=mb)
    )

    return {
        "package_id": str(item.get("nzo_id", "")),
        "name": name,
        "category": str(item.get("cat", "not_quasarr")),
        "size_label": size_label,
        "eta": eta,
        "eta_unknown": eta_unknown,
        "percentage": _clamp_percentage(item.get("percentage", 0)),
        "status": status,
        "can_solve_captcha": status == "waiting_captcha",
        "is_archive": bool(item.get("is_archive", False)),
        "extraction_status": "RUNNING" if status == "extracting" else "",
        "storage": str(item.get("storage", "")),
    }


def _build_history_row(item):
    """Project one raw history item (linkgrabber/downloader/failed) into a
    HistoryRow.
    """
    status_raw = str(item.get("status", "") or "").strip().lower()
    status = "completed" if status_raw == "completed" else "failed"
    bytes_val = item.get("bytes", 0) or 0

    return {
        "package_id": str(item.get("nzo_id", "")),
        "name": str(item.get("name", "Unknown")),
        "category": str(item.get("category", "not_quasarr")),
        "size_label": _format_size_label(bytes_val=bytes_val),
        "status": status,
        "error": _scrub_protected_links(str(item.get("fail_message", "") or "")),
    }


def _build_deferred_row(item):
    """Project one active-hold protected queue item into a DeferredRow.

    Only ever called for items whose projected `deferred.active` is True -
    the caller (build_package_list_response) applies that gate, matching the
    existing Classic deferred-section rule.
    """
    deferred = item.get("deferred") or {}
    filename = str(item.get("filename", "Unknown"))
    name = _strip_prefix(filename, _DEFERRED_NAME_PREFIXES)

    probe_requested = deferred.get("probe_requested") is True

    return {
        "package_id": str(item.get("nzo_id", "")),
        "name": name,
        "state": _deferred_state(deferred, probe_requested=probe_requested),
        "crypter_label": _label(deferred.get("crypter"), _CRYPTER_LABELS),
        "reason_label": _label(deferred.get("reason_code"), _REASON_LABELS),
        "evidence_count": _nonneg_int(deferred.get("evidence_count")),
        "retry_after_epoch": _nonneg_int(deferred.get("retry_after_epoch")),
        "probe_requested": probe_requested,
        "cohort_tested": _nonneg_int(deferred.get("cohort_tested")),
        "cohort_total": _nonneg_int(deferred.get("cohort_total")),
        "cohort_deadline_epoch": _nonneg_int(deferred.get("cohort_deadline_epoch")),
        "cohort_retest_depth": _nonneg_int(deferred.get("cohort_retest_depth")),
        "can_solve_captcha": True,
    }


def empty_package_list_response():
    """The PackageListResponse shape returned when no device is connected.

    Returns a fresh dict/sub-dict every call so a caller can never mutate
    shared state through the response.
    """
    return {
        "connected": False,
        "linkgrabber": {"is_collecting": False, "is_stopped": True},
        "deferred": [],
        "queue": [],
        "history": [],
        "other_queue": [],
        "other_history": [],
    }


def build_package_list_response(shared_state, device, *, auto_start=True):
    """Build the Carbon PackageListResponse for one connected device.

    Delegates all JDownloader/database aggregation to
    `get_packages_for_device()` (which never calls shared_state.get_device()),
    then splits the resulting queue into deferred/ordinary and Quasarr/other
    buckets exactly like the Classic fragment does - without importing or
    touching Classic's own rendering functions - and maps each row through
    the exact PackageListResponse schema.
    """
    downloads = get_packages_for_device(shared_state, device, auto_start=auto_start)

    deferred_rows = []
    queue_rows = []
    other_queue_rows = []
    for item in downloads.get("queue", []):
        if item.get("cat") == "not_quasarr":
            other_queue_rows.append(_build_queue_row(item))
            continue
        deferred = item.get("deferred")
        if isinstance(deferred, dict) and deferred.get("active") is True:
            deferred_rows.append(_build_deferred_row(item))
        else:
            queue_rows.append(_build_queue_row(item))

    history_rows = []
    other_history_rows = []
    for item in downloads.get("history", []):
        if item.get("category") == "not_quasarr":
            other_history_rows.append(_build_history_row(item))
        else:
            history_rows.append(_build_history_row(item))

    linkgrabber = downloads.get(
        "linkgrabber", {"is_collecting": False, "is_stopped": True}
    )

    return {
        "connected": True,
        "linkgrabber": {
            "is_collecting": bool(linkgrabber.get("is_collecting", False)),
            "is_stopped": bool(linkgrabber.get("is_stopped", True)),
        },
        "deferred": deferred_rows,
        "queue": queue_rows,
        "history": history_rows,
        "other_queue": other_queue_rows,
        "other_history": other_history_rows,
    }


# ---------------------------------------------------------------------------
# Page renderer
# ---------------------------------------------------------------------------


def _downloads_delete_status_banner():
    """Mirrors Classic's `deleted=1`/`deleted=0` redirect-status query param
    from `/packages/delete/<id>` (unchanged, still owned by
    `quasarr.api.packages`). Ordinary queue/history deletion keeps navigating
    to that exact route (D1), so this is the one server-rendered echo of its
    result. `carbon.js`'s `clearDeletedQueryParamAndScheduleBannerHide()`
    strips the query param via `history.replaceState` and auto-hides this
    banner after 5 seconds, matching Classic's own `deleted=` handling
    (`quasarr/api/packages/__init__.py`'s inline script).
    """
    deleted = request.query.get("deleted")
    if deleted == "1":
        return notification(
            "success", "Package deleted", "Package deleted successfully."
        )
    if deleted == "0":
        return notification("error", "Delete failed", "Failed to delete package.")
    return ""


def _downloads_toolbar():
    """Search input, slow-connection warning, and the delete-status banner -
    all deliberately OUTSIDE `#downloads-content` (the subtree carbon.js
    replaces on every poll), so a typed filter value and input focus survive
    every refresh cycle.
    """
    return (
        '<div class="cds-toolbar">'
        '<div class="cds-field cds-field--search">'
        '<label class="cds-field__label" for="downloads-search">Filter by name</label>'
        '<input class="cds-field__input" id="downloads-search" type="search" '
        'placeholder="Filter packages by name" autocomplete="off">'
        "</div></div>"
        '<div id="downloads-slow-warning" hidden>'
        + notification(
            "warning",
            "Slow connection",
            "The package list is taking longer than usual to update.",
        )
        + "</div>"
        f'<div id="downloads-status-banner">{_downloads_delete_status_banner()}</div>'
    )


def _deferred_table_skeleton():
    return (
        '<section class="cds-tile" id="downloads-deferred-section" data-state="loading">'
        '<div class="cds-section-header">'
        '<h2 class="cds-tile__heading">Deferred linkcrypter checks</h2>'
        '<div class="cds-bulk-toolbar" id="deferred-bulk-toolbar">'
        '<button class="cds-btn cds-btn--secondary cds-btn--compact" type="button" '
        'data-action="deferred-probe-selected" disabled title="Check selected packages now" '
        'aria-label="Check selected packages now">'
        f"{render_icon('renew', class_name='cds-icon cds-icon--sm')}"
        "<span>Check selected</span></button>"
        '<button class="cds-btn cds-btn--danger-ghost cds-btn--compact" type="button" '
        'data-action="deferred-remove-selected" disabled title="Remove selected pending packages" '
        'aria-label="Remove selected pending packages">'
        f"{render_icon('trash-can', class_name='cds-icon cds-icon--sm')}"
        "<span>Remove selected</span></button>"
        '<span id="deferred-selection-count" class="cds-field__help">0 selected</span>'
        "</div></div>"
        '<div id="deferred-action-status" class="cds-field__help" aria-live="polite"></div>'
        '<div class="cds-table-wrap">'
        '<table class="cds-table cds-table--sticky-col" id="deferred-table">'
        "<caption>Deferred linkcrypter checks</caption>"
        "<thead><tr>"
        '<th scope="col"><input type="checkbox" id="deferred-select-all" '
        'aria-label="Select all deferred packages"></th>'
        '<th scope="col">Release</th>'
        '<th scope="col">State</th>'
        '<th scope="col">Evidence</th>'
        '<th scope="col">Next check</th>'
        '<th scope="col">Sweep progress</th>'
        '<th scope="col">Actions</th>'
        "</tr></thead>"
        '<tbody id="deferred-table-body"></tbody>'
        "</table></div>"
        '<p id="deferred-empty-message" class="cds-field__help">Loading deferred packages…</p>'
        "</section>"
    )


def _queue_table_skeleton():
    return (
        '<section class="cds-tile" id="downloads-queue-section" data-state="loading">'
        '<h2 class="cds-tile__heading">Queue</h2>'
        '<div class="cds-table-wrap">'
        '<table class="cds-table" id="queue-table">'
        "<caption>Active downloads</caption>"
        "<thead><tr>"
        '<th scope="col">Release</th>'
        '<th scope="col">Category</th>'
        '<th scope="col">Status</th>'
        '<th scope="col">Progress</th>'
        '<th scope="col">ETA</th>'
        '<th scope="col">Size</th>'
        '<th scope="col">Actions</th>'
        "</tr></thead>"
        '<tbody id="queue-table-body"></tbody>'
        "</table></div>"
        '<p id="queue-empty-message" class="cds-field__help">Loading queue…</p>'
        "</section>"
    )


def _history_table_skeleton():
    return (
        '<section class="cds-tile" id="downloads-history-section" data-state="loading">'
        '<h2 class="cds-tile__heading">History</h2>'
        '<div class="cds-table-wrap">'
        '<table class="cds-table" id="history-table">'
        "<caption>Recent history</caption>"
        "<thead><tr>"
        '<th scope="col">Release</th>'
        '<th scope="col">Category</th>'
        '<th scope="col">Status</th>'
        '<th scope="col">Size</th>'
        '<th scope="col">Actions</th>'
        "</tr></thead>"
        '<tbody id="history-table-body"></tbody>'
        "</table></div>"
        '<p id="history-empty-message" class="cds-field__help">Loading history…</p>'
        "</section>"
    )


def _other_packages_skeleton():
    return (
        '<section id="downloads-other-section" hidden>'
        '<details id="otherPackagesDetails">'
        '<summary id="otherPackagesSummary">Show <span id="downloads-other-count">0</span>'
        " other package(s)</summary>"
        '<div class="cds-tile">'
        '<h3 class="cds-tile__heading">Other queue</h3>'
        '<div class="cds-table-wrap">'
        '<table class="cds-table" id="other-queue-table">'
        "<caption>Other packages in progress</caption>"
        "<thead><tr>"
        '<th scope="col">Release</th>'
        '<th scope="col">Category</th>'
        '<th scope="col">Status</th>'
        '<th scope="col">Progress</th>'
        '<th scope="col">ETA</th>'
        '<th scope="col">Size</th>'
        '<th scope="col">Actions</th>'
        "</tr></thead>"
        '<tbody id="other-queue-table-body"></tbody>'
        "</table></div>"
        '<h3 class="cds-tile__heading">Other history</h3>'
        '<div class="cds-table-wrap">'
        '<table class="cds-table" id="other-history-table">'
        "<caption>Other packages history</caption>"
        "<thead><tr>"
        '<th scope="col">Release</th>'
        '<th scope="col">Category</th>'
        '<th scope="col">Status</th>'
        '<th scope="col">Size</th>'
        '<th scope="col">Actions</th>'
        "</tr></thead>"
        '<tbody id="other-history-table-body"></tbody>'
        "</table></div>"
        "</div></details></section>"
    )


def _downloads_noscript_notice() -> str:
    """Every row of every section on this page (deferred/queue/history/
    other) is populated client-side from `GET /api/packages/list` - the
    loading skeletons below never resolve without JavaScript. A JS-disabled
    visitor otherwise gets a silent empty shell with no indication anything
    is wrong; point them at the fully server-rendered Classic UI instead.
    """
    return (
        "<noscript>"
        + notification(
            "warning",
            "JavaScript is required",
            "Downloads loads its data live in your browser. Enable "
            "JavaScript, or use the Classic UI instead.",
            actions=(
                '<a class="cds-btn cds-btn--primary" '
                'href="/ui/classic?next=/packages">Switch to Classic UI</a>'
            ),
        )
        + "</noscript>"
    )


def render_downloads(shared_state) -> str:
    """Carbon Downloads page (A1). Renders the search toolbar (outside the
    refreshed subtree) plus a loading skeleton for the deferred/queue/history/
    other-packages sections; `carbon.js` populates and refreshes all of them
    from `GET /api/packages/list` every 5 seconds, exactly like the Dashboard
    queue tile does for its own preview. A <noscript> notice fronts all of
    that for a JS-disabled visitor, since none of it ever renders without JS.
    """
    content = (
        _downloads_noscript_notice()
        + _downloads_toolbar()
        + '<div id="downloads-content" data-state="loading">'
        + _deferred_table_skeleton()
        + _queue_table_skeleton()
        + _history_table_skeleton()
        + _other_packages_skeleton()
        + "</div>"
    )

    return render_carbon_html(
        "downloads",
        content,
        title="Downloads",
        eyebrow="Packages",
        subtitle="Deferred linkcrypter checks, active downloads, and recent history",
        captcha_count=protected_captcha_count(shared_state),
        show_user=show_logout_link(),
    )


__all__ = [
    "DEFERRED_STATE_VALUES",
    "HISTORY_STATUS_VALUES",
    "QUEUE_STATUS_VALUES",
    "build_package_list_response",
    "empty_package_list_response",
    "render_downloads",
]
