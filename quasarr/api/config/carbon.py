# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

"""Carbon renderers for the Hostnames and Categories config views.

``render_hostnames(shared_state)`` and ``render_categories(shared_state)``
are the exact two symbols ``quasarr/api/config/__init__.py``'s ``/hostnames``
and ``/categories`` routes import lazily (callback-local, matching the
no-module-scope-``.carbon``-import rule).

Hostnames consumes ``storage.setup.hostnames.build_hostname_rows()`` (the
shared hostnames data layer) directly - it never re-derives status/capability
logic. The per-row
``details`` field carries uncontrolled exception text that can embed a
configured source hostname, so it is deliberately never rendered into the
initial page HTML, a data attribute, or any other persistent DOM node here:
it is fetched fresh from the existing authenticated ``GET /api/hostnames``
endpoint by ``carbon.js`` only at the moment a user opens a row's status
modal, and lands solely as a text node inside that modal body. Stored
user/password credential values never leave the server at all - the
credentials section always renders blank inputs, matching the projection's
own secret-free contract.

Categories consumes ``storage.categories`` the same way the existing Classic
``_classic_categories`` view does (add/edit/delete endpoints and payload
shapes are unchanged), just projected through a pure model builder and
Carbon markup instead of the Classic inline-onclick template.
"""

from __future__ import annotations

import json
from html import escape
from typing import Any, Mapping

from quasarr.constants import (
    DOWNLOAD_CATEGORIES,
    RECOMMENDED_HOSTERS,
    SEARCH_CAT_BOOKS,
    SEARCH_CAT_MOVIES,
    SEARCH_CAT_MUSIC,
    SEARCH_CAT_SHOWS,
    SEARCH_CAT_SHOWS_ANIME,
    SEARCH_CAT_SHOWS_DOCUMENTARY,
    SHARE_HOSTERS,
)
from quasarr.providers.auth import show_logout_link
from quasarr.providers.carbon_icons import render_icon
from quasarr.providers.carbon_templates import (
    grid,
    protected_captcha_count,
    render_carbon_html,
    status,
    tag,
    tile,
)
from quasarr.providers.utils import (
    get_search_capability_category,
    has_source_capability_for_category,
)
from quasarr.search.sources import get_sources
from quasarr.search.sources.helpers import get_hostnames
from quasarr.storage.categories import (
    get_download_categories,
    get_download_category_mirrors,
    get_search_categories,
    get_search_category_sources,
    get_search_category_ui_heading,
    get_search_category_whitelist_owner,
)
from quasarr.storage.config import Config
from quasarr.storage.setup.hostnames import build_hostname_rows
from quasarr.storage.sqlite_database import DataBase


def _h(value: object) -> str:
    return escape(str(value), quote=True)


# ---------------------------------------------------------------------------
# Hostnames
# ---------------------------------------------------------------------------

_STATUS_TONE = {
    "ok": "success",
    "error": "error",
    "login_failed": "error",
    "skipped": "warning",
    "unset": "neutral",
}

_CAP_TONE = {
    "German": "gray",
    "English": "gray",
    "French": "gray",
    "Movies": "blue",
    "TV": "purple",
    "Anime": "teal",
    "Music": "teal",
    "Books": "teal",
    "Docs": "teal",
    "Login required": "red",
    "Invite only": "red",
    "FlareSolverr": "red",
    "Account required": "red",
}

_LANGUAGE_LABELS = {"en": "English", "de": "German", "fr": "French"}

# Default skip-login note for the main Hostnames page's dense row (the setup
# wizard's row still says "Open Details" - it keeps its own separate
# Details toggle, so it passes its own skip_note_html to _hostname_row_html
# instead of this default).
_SKIP_LOGIN_NOTE = "Login skipped – open the status to require login again."

_CATEGORY_CHIP_ORDER = (
    (SEARCH_CAT_MOVIES, "Movies"),
    (SEARCH_CAT_SHOWS, "TV"),
    (SEARCH_CAT_SHOWS_ANIME, "Anime"),
    (SEARCH_CAT_SHOWS_DOCUMENTARY, "Docs"),
    (SEARCH_CAT_MUSIC, "Music"),
    (SEARCH_CAT_BOOKS, "Books"),
)


def build_hostnames_model(shared_state) -> dict[str, Any]:
    """Pure-ish model for the Hostnames page: reads build_hostname_rows()
    (the shared hostnames data layer) plus the small amount of page-level
    chrome state Classic's editor also shows (stored import URL,
    FlareSolverr-skipped flag). Never re-derives row status/capability logic
    itself.
    """
    return {
        "rows": build_hostname_rows(shared_state),
        "hostnames_url": Config("Settings").get("hostnames_url") or "",
        "flaresolverr_skipped": bool(DataBase("skip_flaresolverr").retrieve("skipped")),
        "captcha_count": protected_captcha_count(shared_state),
        "show_user": show_logout_link(),
    }


def _hostname_capability_chips(row: Mapping[str, Any]) -> str:
    chips = []

    language = row.get("language")
    if language in _LANGUAGE_LABELS:
        label = _LANGUAGE_LABELS[language]
        chips.append(tag(label, tone=_CAP_TONE.get(label, "gray")))

    if row.get("invite_only"):
        chips.append(tag("Invite only", tone=_CAP_TONE["Invite only"]))
    if row.get("requires_login"):
        chips.append(tag("Login required", tone=_CAP_TONE["Login required"]))
    elif row.get("requires_account"):
        # requires_account is a different real thing than requires_login
        # (see quasarr/search/sources/helpers/search_source.py) and several
        # real sources set it without also setting requires_login, so it
        # keeps its own distinct chip - but per spec §2.1's capability
        # palette (gray for language, red for every "you need something
        # extra to use this" flag) it joins the same red cluster as Login
        # required/Invite only/FlareSolverr, sentence-cased to match them.
        chips.append(tag("Account required", tone=_CAP_TONE["Account required"]))
    if row.get("requires_flaresolverr"):
        chips.append(tag("FlareSolverr", tone=_CAP_TONE["FlareSolverr"]))

    categories = set(row.get("categories") or [])
    for cat_id, label in _CATEGORY_CHIP_ORDER:
        if cat_id in categories:
            chips.append(tag(label, tone=_CAP_TONE.get(label, "gray")))

    return "".join(chips)


def _hostname_row_html(
    row: Mapping[str, Any],
    *,
    status_action: str = "hostname-status",
    status_wrapper_id: str = "",
    trailing_caps_html: str = "",
    skip_note_html: str | None = None,
    input_form: str = "",
) -> str:
    """The shared dense hostname-table row: code, clickable status,
    hostname input, capability chips. Shared verbatim between this page and
    the setup wizard's Hostnames step
    (``quasarr/storage/setup/carbon.py::_setup_hostname_row_html``), whose
    genuinely different bits (a non-clickable status plus a separate
    Details toggle button that expands an inline credentials panel, instead
    of this page's status opening a JS-fetched modal; its own skip-login
    banner id/wording; the ``form=`` attribute its deliberately-empty
    ``<form>`` needs on every field that should submit) are passed in
    explicitly rather than forked into a second copy of this function.
    """
    safe_id = _h(row["id"])
    tone = _STATUS_TONE.get(row["status"], "neutral")
    if status_action:
        status_html = status(
            row["status_title"],
            tone,
            tinted=tone in {"error", "warning", "neutral"},
            as_button=True,
            action=status_action,
            data={"hostname-id": row["id"]},
        )
    else:
        status_html = status(
            row["status_title"], tone, tinted=tone in {"error", "warning", "neutral"}
        )
    wrapper_id_attr = f' id="{_h(status_wrapper_id)}"' if status_wrapper_id else ""

    if skip_note_html is None:
        skip_note_html = (
            f'<p class="cds-hostname-table__note">{_h(_SKIP_LOGIN_NOTE)}</p>'
            if row.get("skip_login")
            else ""
        )

    form_attr = f' form="{_h(input_form)}"' if input_form else ""

    return (
        f'<div class="cds-hostname-table__row" data-hostname-id="{safe_id}">'
        f'<span class="cds-hostname-table__code">{_h(row["label"])}</span>'
        f"<span{wrapper_id_attr}>{status_html}</span>"
        f'<span><input class="cds-hostname-table__input" id="hostname-{safe_id}" '
        f'name="{safe_id}" type="text" '
        f'value="{_h(row["hostname"])}" placeholder="example.com" '
        f'autocomplete="off" autocorrect="off"{form_attr} '
        f'aria-label="Hostname for {_h(row["label"])}">{skip_note_html}</span>'
        f'<span class="cds-hostname-table__caps">'
        f"{_hostname_capability_chips(row)}{trailing_caps_html}</span>"
        "</div>"
    )


def _hostnames_import_section(model: Mapping[str, Any]) -> str:
    # value="{stored_url}" is not in the design's condensed inline markup
    # spec but is kept here regardless: without it, a previously-configured
    # import URL would silently stop showing in this visible field on page
    # load (it would still be present in the hidden #hostnames-url-hidden
    # field used by Save, just invisible to the user re-opening the page).
    stored_url = _h(model["hostnames_url"])
    return tile(
        '<p class="cds-field__label">Import from URL</p>'
        '<div class="cds-inline-form">'
        '<input class="cds-field__input cds-mono" id="hostname-import-url" '
        f'type="url" value="{stored_url}" '
        'placeholder="https://quasarr-hostnames.pages.dev/ini?token=…">'
        '<button class="cds-btn cds-btn--tertiary" type="button" '
        'data-action="hostname-import">Import</button>'
        "</div>"
        '<p class="cds-field__help">One hostname per line, e.g. '
        '"fx = fx.example.com"</p>'
    )


def render_hostnames(shared_state) -> str:
    model = build_hostnames_model(shared_state)
    rows = model["rows"]
    rows_html = "".join(_hostname_row_html(row) for row in rows)
    stored_url = _h(model["hostnames_url"])
    flaresolverr_flag = "true" if model["flaresolverr_skipped"] else "false"

    total = len(rows)
    configured = sum(1 for row in rows if row["hostname"])
    working = sum(1 for row in rows if row["status"] == "ok")

    table_head = (
        '<div class="cds-hostname-table__head">'
        "<span></span><span>Status</span><span>Hostname</span>"
        "<span>Capabilities</span></div>"
    )
    table_tile = (
        '<section class="cds-tile cds-tile--is-table">'
        '<div class="cds-tile__head-row">'
        '<h2 class="cds-tile__heading">Sources <span class="cds-tile__count">'
        f"({working} of {configured} operational · {total} supported)"
        "</span></h2>"
        '<input class="cds-field__input cds-filter" type="search" '
        'placeholder="Filter sources" data-action="hostname-filter">'
        "</div>"
        f'<div class="cds-hostname-table">{table_head}{rows_html}</div>'
        "</section>"
    )
    cta_row = (
        '<div class="cds-cta-row">'
        '<button class="cds-btn cds-btn--primary cds-btn--cta" type="submit">'
        "Save hostnames</button>"
        '<button class="cds-btn cds-btn--secondary cds-btn--cta" type="button" '
        'data-action="hostname-reset">Cancel</button>'
        '<span class="cds-field__help">Saving a changed hostname asks to '
        "restart Quasarr.</span>"
        "</div>"
    )

    content = "".join(
        [
            _hostnames_import_section(model),
            '<span id="hostnames-flaresolverr-skipped" hidden '
            f'data-skipped="{flaresolverr_flag}"></span>',
            '<form id="hostnames-form" action="/api/hostnames" method="post">',
            '<input type="hidden" id="hostnames-url-hidden" name="hostnames_url" '
            f'value="{stored_url}">',
            table_tile,
            cta_row,
            "</form>",
            tile(
                "<p>Restarting Quasarr applies configuration changes that need a "
                "full reload.</p>"
                '<button class="cds-btn cds-btn--danger-ghost" type="button" '
                'data-action="hostnames-restart-open">Restart Quasarr</button>',
                heading="Maintenance",
            ),
        ]
    )

    return render_carbon_html(
        "hostnames",
        content,
        title="Hostnames",
        eyebrow="Sources",
        subtitle="Configure one hostname per source. Click a status to review "
        "errors or update credentials.",
        captcha_count=model["captcha_count"],
        show_user=model["show_user"],
    )


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


def build_categories_model(shared_state) -> dict[str, Any]:
    """Pure-ish model for the Categories page, ported from the exact
    grouping/filtering rules ``_classic_categories`` uses (default vs custom
    download categories, default/subcategory/custom search categories,
    selectable base types, per-source category support) - the storage layer
    and its payload shapes are untouched, only the presentation differs.
    """
    categories = get_download_categories()
    default_download_categories = [c for c in DOWNLOAD_CATEGORIES if c in categories]
    custom_download_categories = sorted(
        c for c in categories if c not in DOWNLOAD_CATEGORIES
    )
    ordered_download_categories = (
        default_download_categories + custom_download_categories
    )
    can_add_download_category = len(custom_download_categories) < 10

    download_rows = []
    for cat in ordered_download_categories:
        emoji = DOWNLOAD_CATEGORIES.get(cat, {}).get("emoji", "📁")
        mirrors = get_download_category_mirrors(cat)
        download_rows.append(
            {
                "name": cat,
                "emoji": emoji,
                "mirrors": mirrors,
                "is_custom": cat not in DOWNLOAD_CATEGORIES,
            }
        )

    supported_categories_union = set()
    for source in get_sources().values():
        supported_categories_union.update(source.supported_categories)

    all_search_categories = get_search_categories()
    sorted_search_cats = sorted(all_search_categories.items(), key=lambda x: int(x[0]))
    sorted_search_cats = [
        (cat_id, details)
        for cat_id, details in sorted_search_cats
        if has_source_capability_for_category(cat_id, supported_categories_union)
    ]

    default_search_cats: list[tuple[int, dict]] = []
    custom_search_cats: list[tuple[int, dict]] = []
    grouped_default_subcategories: dict[int, list[tuple[int, dict]]] = {}

    for cat_id, details in sorted_search_cats:
        cat_id = int(cat_id)
        if cat_id >= 100000:
            custom_search_cats.append((cat_id, details))
            continue

        owner_cat_id = get_search_category_whitelist_owner(cat_id)
        if owner_cat_id != cat_id:
            grouped_default_subcategories.setdefault(owner_cat_id, []).append(
                (cat_id, details)
            )
            continue

        default_search_cats.append((cat_id, details))

    default_search_cats.sort(key=lambda x: int(x[0]))
    custom_search_cats.sort(key=lambda x: int(x[0]))
    for owner_cat_id in grouped_default_subcategories:
        grouped_default_subcategories[owner_cat_id].sort(key=lambda x: int(x[0]))

    used_custom_base_types = set()
    for custom_cat_id, custom_details in custom_search_cats:
        custom_base_type = custom_details.get("base_type")
        try:
            used_custom_base_types.add(int(custom_base_type))
        except (TypeError, ValueError):
            if int(custom_cat_id) >= 100000:
                used_custom_base_types.add(int(custom_cat_id) - 100000)

    selectable_base_categories = [
        (int(cat_id), details)
        for cat_id, details in sorted_search_cats
        if int(cat_id) < 100000 and int(cat_id) not in used_custom_base_types
    ]
    can_add_search_category = len(custom_search_cats) < 10 and bool(
        selectable_base_categories
    )

    default_search_rows = []
    for cat_id, details in default_search_cats:
        name = details["name"]
        search_sources = get_search_category_sources(cat_id)
        base_source_category_id = get_search_capability_category(cat_id) or cat_id
        inherited_subcats = grouped_default_subcategories.get(cat_id, [])
        default_search_rows.append(
            {
                "cat_id": cat_id,
                "name": name,
                "heading": get_search_category_ui_heading(name),
                "emoji": details["emoji"],
                "search_sources": search_sources,
                "base_source_category_id": base_source_category_id,
                # Own Newznab-ID pill first (matches Classic's
                # `[f"{name} ({cat_id})"] + [...]`), so a default category
                # with no inherited subcategories still surfaces its ID.
                # Each pill keeps its own numeric cat_id alongside its label
                # so the renderer can tone it by Newznab base type.
                "category_pills": [(cat_id, f"{name} ({cat_id})")]
                + [
                    (sub_cat_id, f"{sub_details['name']} ({sub_cat_id})")
                    for sub_cat_id, sub_details in inherited_subcats
                ],
            }
        )

    custom_search_rows = []
    for cat_id, details in custom_search_cats:
        cat_id = int(cat_id)
        name = details["name"]
        search_sources = get_search_category_sources(cat_id)
        base_source_category_id = get_search_capability_category(cat_id) or cat_id
        custom_base_type = details.get("base_type")
        try:
            custom_base_type = int(custom_base_type)
        except (TypeError, ValueError):
            custom_base_type = get_search_capability_category(cat_id) or cat_id
        custom_search_rows.append(
            {
                "cat_id": cat_id,
                "name": name,
                "heading": name,
                "emoji": details["emoji"],
                "search_sources": search_sources,
                "base_source_category_id": base_source_category_id,
                "custom_pill": f"Custom ({cat_id} -> {custom_base_type})",
            }
        )

    base_category_options = [
        {"id": int(cat_id), "label": f"{details['name']} ({int(cat_id)})"}
        for cat_id, details in selectable_base_categories
    ]

    supported_categories_per_source = {
        source.initials: list(source.supported_categories)
        for source in get_sources().values()
    }

    return {
        "download_rows": download_rows,
        "can_add_download_category": can_add_download_category,
        "default_search_rows": default_search_rows,
        "custom_search_rows": custom_search_rows,
        "can_add_search_category": can_add_search_category,
        "base_category_options": base_category_options,
        "all_hosters": SHARE_HOSTERS,
        "recommended_hosters": RECOMMENDED_HOSTERS,
        "hostnames": get_hostnames(),
        "supported_categories_per_source": supported_categories_per_source,
        "captcha_count": protected_captcha_count(shared_state),
        "show_user": show_logout_link(),
    }


def _download_category_row_html(row: Mapping[str, Any]) -> str:
    mirrors = row["mirrors"]
    mirrors_text = ", ".join(mirrors) if mirrors else "All"
    mirrors_json = _h(json.dumps(mirrors))
    safe_name = _h(row["name"])

    delete_btn = ""
    if row["is_custom"]:
        delete_btn = (
            '<button class="cds-btn cds-btn--danger-ghost" type="button" '
            f'data-action="download-category-delete" data-category="{safe_name}">'
            "Delete</button>"
        )

    return (
        '<div class="cds-category-row">'
        '<div class="cds-category-row__body">'
        f'<span class="cds-category-row__name">{safe_name}</span>'
        f'<span class="cds-category-row__detail">Mirrors: {_h(mirrors_text)}</span>'
        "</div>"
        '<div class="cds-category-row__actions">'
        f"{delete_btn}"
        '<button class="cds-btn cds-btn--ghost" type="button" '
        f'data-action="download-category-edit" data-category="{safe_name}" '
        f'data-mirrors="{mirrors_json}">Edit</button>'
        "</div>"
        "</div>"
    )


# Tag tone by Newznab base type - first digit of the category id. A custom
# category's synthetic id (>= 100000) always starts with a digit outside
# this table, so it falls through to the same gray it always rendered.
_SEARCH_CATEGORY_TAG_TONE = {"2": "blue", "5": "purple", "3": "teal", "7": "gray"}


def _search_category_tag_tone(cat_id: Any) -> str:
    return _SEARCH_CATEGORY_TAG_TONE.get(str(cat_id)[0], "gray")


def _search_category_row_html(row: Mapping[str, Any], *, is_custom: bool) -> str:
    sources = row["search_sources"]
    sources_text = ", ".join(s.upper() for s in sources) if sources else "All"
    sources_json = _h(json.dumps(sources))
    safe_heading = _h(row["heading"])

    pills = (
        [(row["cat_id"], row["custom_pill"])]
        if is_custom
        else (row.get("category_pills") or [])
    )
    pills_html = ""
    if pills:
        pills_html = (
            '<div class="cds-category-row__pills">'
            + "".join(
                tag(text, tone=_search_category_tag_tone(pill_cat_id))
                for pill_cat_id, text in pills
            )
            + "</div>"
        )

    delete_btn = ""
    if is_custom:
        delete_btn = (
            '<button class="cds-btn cds-btn--danger-ghost" type="button" '
            f'data-action="search-category-delete" data-cat-id="{row["cat_id"]}" '
            f'data-name="{safe_heading}">Delete</button>'
        )

    return (
        '<div class="cds-category-row">'
        '<div class="cds-category-row__body">'
        f'<span class="cds-category-row__name">{safe_heading}</span>'
        f'<span class="cds-category-row__detail">Hostnames: {_h(sources_text)}</span>'
        f"{pills_html}"
        "</div>"
        '<div class="cds-category-row__actions">'
        f"{delete_btn}"
        '<button class="cds-btn cds-btn--ghost" type="button" '
        f'data-action="search-category-edit" data-cat-id="{row["cat_id"]}" '
        f'data-name="{safe_heading}" '
        f'data-base-category="{row["base_source_category_id"]}" '
        f'data-search-sources="{sources_json}">Edit</button>'
        "</div>"
        "</div>"
    )


def _download_category_add_form(model: Mapping[str, Any]) -> str:
    if not model["can_add_download_category"]:
        return ""
    return (
        '<div class="cds-category-add-row">'
        '<div class="cds-field">'
        '<label class="cds-field__label" for="download-category-new-name">'
        "New category name</label>"
        '<input class="cds-field__input" id="download-category-new-name" type="text" '
        'placeholder="a-z, 0-9" pattern="[a-z0-9]+" autocomplete="off">'
        "</div>"
        '<button class="cds-btn cds-btn--tertiary" type="button" '
        'data-action="download-category-add">'
        f"{render_icon('add', class_name='cds-icon cds-icon--sm')}<span>Add</span></button>"
        "</div>"
    )


def _search_category_add_form(model: Mapping[str, Any]) -> str:
    if not model["can_add_search_category"]:
        return ""
    options = "".join(
        f'<option value="{opt["id"]}">{_h(opt["label"])}</option>'
        for opt in model["base_category_options"]
    )
    return (
        '<div class="cds-category-add-row">'
        '<div class="cds-field">'
        '<label class="cds-field__label" for="search-category-new-base">'
        "Base category type</label>"
        f'<select class="cds-field__select" id="search-category-new-base">{options}</select>'
        "</div>"
        '<button class="cds-btn cds-btn--tertiary" type="button" '
        'data-action="search-category-add">'
        f"{render_icon('add', class_name='cds-icon cds-icon--sm')}"
        "<span>Add custom category</span></button>"
        "</div>"
    )


def render_categories(shared_state) -> str:
    model = build_categories_model(shared_state)

    download_rows_html = "".join(
        _download_category_row_html(row) for row in model["download_rows"]
    )
    search_rows_html = "".join(
        _search_category_row_html(row, is_custom=False)
        for row in model["default_search_rows"]
    ) + "".join(
        _search_category_row_html(row, is_custom=True)
        for row in model["custom_search_rows"]
    )

    hosters_json = _h(json.dumps(model["all_hosters"]))
    tier1_json = _h(json.dumps(model["recommended_hosters"]))
    hostnames_json = _h(json.dumps(model["hostnames"]))
    supported_json = _h(json.dumps(model["supported_categories_per_source"]))

    content = "".join(
        [
            '<span id="categories-hoster-data" hidden '
            f'data-all-hosters="{hosters_json}" data-tier1-hosters="{tier1_json}"></span>',
            '<span id="categories-source-data" hidden '
            f'data-hostnames="{hostnames_json}" data-supported="{supported_json}"></span>',
            grid(
                [
                    tile(
                        download_rows_html + _download_category_add_form(model),
                        heading="Download categories",
                        help_text="Used to organize downloads in JDownloader. "
                        "Mirror whitelists apply to the download client.",
                    ),
                    tile(
                        search_rows_html + _search_category_add_form(model),
                        heading="Search categories",
                        help_text="Hostname whitelists for Newznab search "
                        "categories used by the indexer.",
                    ),
                ],
                "2",
            ),
        ]
    )

    return render_carbon_html(
        "categories",
        content,
        title="Categories",
        eyebrow="Organization",
        subtitle="Manage download categories, mirror priority, and search-source whitelists",
        captcha_count=model["captcha_count"],
        show_user=model["show_user"],
    )


__all__ = [
    "build_hostnames_model",
    "build_categories_model",
    "render_hostnames",
    "render_categories",
]
