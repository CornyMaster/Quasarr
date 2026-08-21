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

The one deliberate exception is `mirror`: the bare host of the linkcrypter
itself, which the CAPTCHA page already shows openly in its "Crypter · Mirror ·
Links" line. Never a scheme, path, query, credential, or a source/indexer
hostname. That exception cannot widen, because `_origin_fields()` re-applies
`package_origin.safe_mirror()` to every stored value before it enters a row -
the writer's own validation is not trusted to be the only gate, so a row
written by an older build or edited in storage still cannot smuggle a URL out.
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
from quasarr.providers.package_origin import crypter_label as _origin_crypter_label
from quasarr.providers.package_origin import read_package_origins, safe_mirror

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

_REASON_LABELS = {"ip_block_suspected": "IP access block suspected"}

# The origin block of a row whose package predates the package_origin table,
# or which Quasarr never created at all (an "other" package). Read-only; every
# consumer copies out of it rather than mutating it.
_EMPTY_ORIGIN = {"crypter": "", "mirror": "", "added_epoch": 0}

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


def _origin_fields(package_id, origins, *, crypter_override="", mirror_override=""):
    """The four origin fields of one row.

    Live values win over the stored record. A still-protected package carries
    its crypter links right now (`downloads/packages._protected_origin()`
    derives them exactly as `/captcha` does), and a live hold names the
    crypter that is actually blocking - both are better answers than whatever
    the package was first accepted through, and both are available for
    packages older than the `package_origin` table. The stored row remains
    the only source of `added_epoch`, which nothing else records.
    """
    origin = origins.get(package_id) or _EMPTY_ORIGIN
    crypter = crypter_override or str(origin.get("crypter") or "")
    return {
        "crypter": crypter,
        "crypter_label": _origin_crypter_label(crypter),
        "mirror": safe_mirror(mirror_override) or safe_mirror(origin.get("mirror")),
        "added_epoch": _nonneg_int(origin.get("added_epoch")),
    }


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


def _build_queue_row(item, origins):
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

    # The label is for reading, `size_bytes` for sorting - a lexical sort over
    # "9 MB" and "10 GB" puts them in the wrong order. Protected packages carry
    # no reliable byte count, so their megabyte figure is converted instead.
    size_bytes = int(bytes_val) if bytes_val else int((mb or 0) * 1024 * 1024)

    return {
        "package_id": str(item.get("nzo_id", "")),
        "name": name,
        "category": str(item.get("cat", "not_quasarr")),
        "size_label": size_label,
        "size_bytes": max(0, size_bytes),
        **_origin_fields(
            str(item.get("nzo_id", "")),
            origins,
            crypter_override=str(item.get("crypter") or ""),
            mirror_override=str(item.get("mirror") or ""),
        ),
        "eta": eta,
        "eta_unknown": eta_unknown,
        "percentage": _clamp_percentage(item.get("percentage", 0)),
        "status": status,
        "can_solve_captcha": status == "waiting_captcha",
        "is_archive": bool(item.get("is_archive", False)),
        "extraction_status": "RUNNING" if status == "extracting" else "",
        "storage": str(item.get("storage", "")),
    }


def _build_history_row(item, origins):
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
        "size_bytes": max(0, int(bytes_val)),
        **_origin_fields(str(item.get("nzo_id", "")), origins),
        "status": status,
        "error": _scrub_protected_links(str(item.get("fail_message", "") or "")),
    }


def _build_deferred_row(item, origins):
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
        **_origin_fields(
            str(item.get("nzo_id", "")),
            origins,
            # The live hold outranks the link's own crypter; the link still
            # supplies the host, which no hold carries.
            crypter_override=str(deferred.get("crypter") or item.get("crypter") or ""),
            mirror_override=str(item.get("mirror") or ""),
        ),
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
    # One read for the whole response, never one per row: the table is small
    # and every section needs it.
    origins = read_package_origins(shared_state)

    deferred_rows = []
    queue_rows = []
    other_queue_rows = []
    for item in downloads.get("queue", []):
        if item.get("cat") == "not_quasarr":
            other_queue_rows.append(_build_queue_row(item, origins))
            continue
        deferred = item.get("deferred")
        if isinstance(deferred, dict) and deferred.get("active") is True:
            deferred_rows.append(_build_deferred_row(item, origins))
        else:
            queue_rows.append(_build_queue_row(item, origins))

    history_rows = []
    other_history_rows = []
    for item in downloads.get("history", []):
        if item.get("category") == "not_quasarr":
            other_history_rows.append(_build_history_row(item, origins))
        else:
            history_rows.append(_build_history_row(item, origins))

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


def _downloads_notices():
    """The page-level notices above the tables: the slow-connection warning
    carbon.js unhides when a poll takes too long, and the delete-status
    banner echoing the `/packages/delete/<id>` redirect.
    """
    return (
        '<div id="downloads-slow-warning" hidden>'
        + notification(
            "warning",
            "Slow connection",
            "The package list is taking longer than usual to update.",
        )
        + "</div>"
        f'<div id="downloads-status-banner">{_downloads_delete_status_banner()}</div>'
    )


def _sortable_th(label, sort_key, *, extra_attributes=""):
    """A column head that can be sorted by clicking or by keyboard.

    `aria-sort` lives on the `th`, which is where assistive technology looks
    for it, while the button inside carries the action - a clickable `th`
    alone is not reachable by keyboard. Both are rendered unsorted here:
    `carbon.js` owns the live sort state (it survives reloads in
    localStorage) and rewrites `aria-sort` on every render, so shipping a
    pre-sorted head in the markup would only fight it.
    """
    attributes = f" {extra_attributes}" if extra_attributes else ""
    return (
        f'<th scope="col" aria-sort="none" data-sort-key="{sort_key}"{attributes}>'
        f'<button class="cds-table__sort" type="button" data-action="table-sort" '
        f'data-sort-key="{sort_key}">{label}'
        '<span class="cds-table__sort-icon" aria-hidden="true"></span>'
        "</button></th>"
    )


def _table_search_field(field_id, label):
    """One search field per table.

    Each lives in its tile's head row, outside the `<tbody>` `carbon.js`
    replaces on every 5s poll, so a typed value and the input's focus
    survive every refresh. The placeholder is not a label, so the real one
    stays in the accessibility tree.
    """
    return (
        '<div class="cds-field cds-field--search">'
        f'<label class="cds-field__label cds-visually-hidden" for="{field_id}">'
        f"{label}</label>"
        f'<input class="cds-field__input" id="{field_id}" type="search" '
        'placeholder="Search releases" autocomplete="off">'
        "</div>"
    )


def _deferred_table_skeleton():
    return (
        '<section class="cds-tile" id="downloads-deferred-section" data-state="loading">'
        '<div class="cds-section-header">'
        '<h2 class="cds-tile__heading">Deferred linkcrypter checks</h2>'
        '<div class="cds-bulk-toolbar" id="deferred-bulk-toolbar">'
        '<span id="deferred-selection-count" class="cds-field__help">0 selected</span>'
        '<button class="cds-btn cds-btn--tertiary cds-btn--compact" type="button" '
        'data-action="deferred-probe-selected" disabled title="Check selected packages now" '
        'aria-label="Check selected packages now">'
        f"{render_icon('renew', class_name='cds-icon cds-icon--sm')}"
        "<span>Check selected</span></button>"
        '<button class="cds-btn cds-btn--danger-ghost cds-btn--compact" type="button" '
        'data-action="deferred-remove-selected" disabled title="Remove selected pending packages" '
        'aria-label="Remove selected pending packages">'
        f"{render_icon('trash-can', class_name='cds-icon cds-icon--sm')}"
        "<span>Remove selected</span></button>"
        "</div>"
        # Last child, so it lands at the far right exactly like the Queue
        # and History fields - the same control in the same place on every
        # tile.
        + _table_search_field("deferred-search", "Filter deferred packages")
        + "</div>"
        '<div id="deferred-action-status" class="cds-field__help" aria-live="polite"></div>'
        '<div class="cds-table-wrap">'
        '<table class="cds-table cds-table--sticky-col" id="deferred-table" '
        'data-table-key="deferred">'
        '<caption class="cds-visually-hidden">Deferred linkcrypter checks</caption>'
        "<thead><tr>"
        '<th scope="col"><input type="checkbox" id="deferred-select-all" '
        'aria-label="Select all deferred packages"></th>'
        # Release stays second: .cds-table--sticky-col pins the first two
        # columns below 672px, and Crypter in front of it would pin the wrong
        # one.
        + _sortable_th("Release", "name")
        + _sortable_th("Crypter", "crypter")
        + _sortable_th("State", "state")
        + _sortable_th("Evidence", "evidence")
        + _sortable_th("Next check", "next-check")
        + _sortable_th("Sweep progress", "sweep")
        + _sortable_th("Added", "added")
        + '<th scope="col"></th>'
        "</tr></thead>"
        '<tbody id="deferred-table-body"></tbody>'
        "</table></div>"
        '<p id="deferred-empty-message" class="cds-field__help">Loading deferred packages…</p>'
        "</section>"
    )


def _queue_table_head():
    """The Queue column order, shared by the Queue and Other-queue tables.

    Both are populated by carbon.js's one `buildQueueRow()`, so their heads
    have to stay identical. The first column is the status dot and the last
    the row actions - both unlabelled, because a coloured dot and a trash
    icon carry their own accessible names per row and a repeated column
    title would only add noise.
    """
    return (
        "<thead><tr>"
        '<th scope="col"></th>'
        + _sortable_th("Release", "name")
        + _sortable_th("Crypter", "crypter")
        + _sortable_th("Category", "category")
        + _sortable_th("Size", "size")
        + _sortable_th("ETA", "eta")
        + _sortable_th("Progress", "progress")
        + _sortable_th("Added", "added")
        + '<th scope="col"></th>'
        "</tr></thead>"
    )


def _history_table_head():
    """The History column order, shared by the History and Other-history
    tables - both populated by carbon.js's one `buildHistoryRow()`.
    """
    return (
        "<thead><tr>"
        + _sortable_th("Status", "status")
        + _sortable_th("Release", "name")
        + _sortable_th("Crypter", "crypter")
        + _sortable_th("Category", "category")
        + _sortable_th("Size", "size")
        + _sortable_th("Added", "added")
        + '<th scope="col"></th>'
        "</tr></thead>"
    )


def _queue_table_skeleton():
    """The Queue tile. Its head row carries the live count and the release
    filter; `carbon.js` only ever swaps `<tbody>` rows, never this subtree,
    so a typed filter value and the input's focus survive every poll.
    """
    return (
        '<section class="cds-tile" id="downloads-queue-section" data-state="loading">'
        '<div class="cds-tile__head-row">'
        '<h2 class="cds-tile__heading">Queue '
        '<span class="cds-tile__count" id="queue-count">(0)</span></h2>'
        + _table_search_field("downloads-search", "Filter releases by name")
        + "</div>"
        '<div class="cds-table-wrap">'
        '<table class="cds-table" id="queue-table" data-table-key="queue">'
        '<caption class="cds-visually-hidden">Active downloads</caption>'
        f"{_queue_table_head()}"
        '<tbody id="queue-table-body"></tbody>'
        "</table></div>"
        '<p id="queue-empty-message" class="cds-field__help">Loading queue…</p>'
        "</section>"
    )


def _history_table_skeleton():
    return (
        '<section class="cds-tile" id="downloads-history-section" data-state="loading">'
        '<div class="cds-tile__head-row">'
        '<h2 class="cds-tile__heading">History</h2>'
        + _table_search_field("history-search", "Filter history by name")
        + "</div>"
        '<div class="cds-table-wrap">'
        '<table class="cds-table" id="history-table" data-table-key="history">'
        '<caption class="cds-visually-hidden">Recent history</caption>'
        f"{_history_table_head()}"
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
        # One field for both "other" tables: they are one collapsed section
        # to the reader, and splitting the filter would only make the two
        # halves disagree.
        + _table_search_field("other-search", "Filter other packages by name")
        + '<h3 class="cds-tile__heading">Other queue</h3>'
        '<div class="cds-table-wrap">'
        '<table class="cds-table" id="other-queue-table" data-table-key="other-queue">'
        "<caption>Other packages in progress</caption>"
        f"{_queue_table_head()}"
        '<tbody id="other-queue-table-body"></tbody>'
        "</table></div>"
        '<h3 class="cds-tile__heading">Other history</h3>'
        '<div class="cds-table-wrap">'
        '<table class="cds-table" id="other-history-table" '
        'data-table-key="other-history">'
        "<caption>Other packages history</caption>"
        f"{_history_table_head()}"
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
    """Carbon Downloads page (A1). Renders the page-level notices plus a
    loading skeleton for the deferred/queue/history/other-packages sections;
    `carbon.js` populates and refreshes all of them from
    `GET /api/packages/list` every 5 seconds, exactly like the Dashboard
    queue tile does for its own preview. The release filter lives in the
    Queue tile's own head row - safe there because every refresh replaces
    `<tbody>` rows only, never a whole tile. A <noscript> notice fronts all
    of that for a JS-disabled visitor, since none of it ever renders without
    JS.
    """
    # cds-stack (the same 16px rhythm every other Carbon page uses between
    # tiles) separates the deferred/queue/history/other sections - without
    # it they run together as one undifferentiated block, since .cds-tile
    # itself carries no margin. carbon.js only ever swaps <tbody> rows or
    # toggles `hidden`/`data-state` here, never this class, so it survives
    # every poll.
    content = (
        _downloads_noscript_notice()
        + _downloads_notices()
        + '<div id="downloads-content" class="cds-stack" data-state="loading">'
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
