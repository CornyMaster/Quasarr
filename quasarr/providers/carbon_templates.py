# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from typing import Mapping, Sequence
from urllib.parse import urlsplit

from quasarr.providers.auth import is_auth_enabled, is_browser_authenticated
from quasarr.providers.carbon_icons import render_icon
from quasarr.providers.log import warn
from quasarr.providers.static_assets import asset_url
from quasarr.providers.version import get_version
from quasarr.storage.config import Config


@dataclass(frozen=True)
class TableColumn:
    key: str
    label: str
    classes: str = ""


_NAV_ITEMS: tuple[tuple[str, str, str, str], ...] = (
    ("dashboard", "Dashboard", "/", "home"),
    ("downloads", "Downloads", "/packages", "download"),
    ("statistics", "Statistics", "/statistics", "chart--column"),
    ("hostnames", "Hostnames", "/hostnames", "security"),
    ("categories", "Categories", "/categories", "settings"),
    ("captcha", "CAPTCHA", "/captcha", "notification"),
    ("settings", "Settings", "/settings", "settings"),
)

_ALLOWED_TILE_CLASS_TOKENS = frozenset({"is-compact", "is-wide", "is-metric"})
_ALLOWED_TABLE_COLUMN_CLASS_TOKENS = frozenset(
    {"is-num", "is-status", "is-mono", "is-right", "is-center"}
)
_ALLOWED_TAG_TONES = frozenset({"gray", "blue", "green", "red", "purple", "teal"})
_ALLOWED_NOTIFICATION_KINDS = frozenset({"info", "warning", "error", "success"})
_ALLOWED_INPUT_TYPES = frozenset(
    {"text", "password", "search", "number", "url", "email"}
)
_REMOTE_RESOURCE_LINK_RELS = frozenset({"stylesheet", "preload", "preconnect"})


def _is_remote_resource_url(value: str, *, allow_data: bool = False) -> bool:
    candidate = value.strip()
    parsed = urlsplit(candidate)
    if candidate.startswith("//") or parsed.netloc:
        return True
    if not parsed.scheme:
        return False
    return not (allow_data and parsed.scheme.lower() == "data")


class _StructuralGuardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.h1_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): value or "" for name, value in attrs}

        if any(name.startswith("on") for name in attributes):
            raise ValueError("Inline event handlers are not allowed")

        element_id = attributes.get("id")
        if element_id:
            if element_id in self.ids:
                raise ValueError("Duplicate IDs are not allowed")
            self.ids.add(element_id)

        if tag == "h1":
            self.h1_count += 1
            if self.h1_count > 1:
                raise ValueError("Multiple page headings are not allowed")

        if tag == "script" and not attributes.get("src"):
            raise ValueError("Inline executable scripts are not allowed")

        source = attributes.get("src")
        if source and _is_remote_resource_url(source, allow_data=tag == "img"):
            raise ValueError("Remote resources are not allowed")

        href = attributes.get("href", "")
        href_scheme = urlsplit(href.strip()).scheme.lower()
        if href_scheme in {"javascript", "vbscript"}:
            raise ValueError("Unsafe navigation URLs are not allowed")

        rel_tokens = {
            token.strip().lower()
            for token in attributes.get("rel", "").split()
            if token.strip()
        }
        if (
            tag == "link"
            and rel_tokens.intersection(_REMOTE_RESOURCE_LINK_RELS)
            and href
            and _is_remote_resource_url(href)
        ):
            raise ValueError("Remote resources are not allowed")

        class_tokens = {
            token.strip() for token in attributes.get("class", "").split() if token
        }
        if "cds-icon-button" in class_tokens and tag in {"a", "button"}:
            if not attributes.get("aria-label") or not attributes.get("title"):
                raise ValueError("Icon controls require a label and tooltip")

        if tag == "a" and attributes.get("target", "").lower() == "_blank":
            if not {"noopener", "noreferrer"}.issubset(rel_tokens):
                raise ValueError("target=_blank anchor has unsafe rel")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def _h(value: object) -> str:
    return escape(str(value), quote=True)


def _validate_classes(classes: str, allowed: frozenset[str], label: str) -> str:
    tokens = [token for token in classes.split() if token]
    for token in tokens:
        if token not in allowed:
            raise ValueError(f"Unsupported {label} class token")
    return " ".join(tokens)


def _validate_active_page(active_page: str) -> tuple[str, str]:
    for key, _title, href, _icon in _NAV_ITEMS:
        if key == active_page:
            return key, href
    raise ValueError("Unsupported active page")


def _read_api_key_for_browser() -> str:
    if is_auth_enabled() and not is_browser_authenticated():
        return ""
    try:
        api_key = Config("API").get("key")
        return api_key if isinstance(api_key, str) else ""
    except Exception:
        return ""


def protected_captcha_count(shared_state) -> int:
    """Live count of packages waiting for CAPTCHA, for the shell nav badge.

    This mirrors the Classic dashboard's protected-queue count; it is shell
    chrome shared by every Carbon page (dashboard, settings, downloads,
    config, captcha - not just Statistics), which is why it lives here
    rather than privately on one view. A read failure defaults to 0,
    matching StatsHelper's own defensive style, but logs the swallowed
    exception's class name so a DB outage is no longer silently masked as a
    plain zero - never the exception text, which could carry a hostname.
    """
    try:
        titles = shared_state.values["database"]("protected").retrieve_all_titles()
        return len(titles) if titles else 0
    except Exception as error:
        warn(f"Failed to read protected CAPTCHA count ({type(error).__name__})")
        return 0


def _assert_structural_guards(html: str) -> None:
    parser = _StructuralGuardParser()
    parser.feed(html)
    parser.close()


def page_header(eyebrow: str, title: str, subtitle: str = "") -> str:
    safe_eyebrow = _h(eyebrow)
    safe_title = _h(title)
    safe_subtitle = _h(subtitle)
    subtitle_html = (
        f'<p class="cds-page-header__subtitle">{safe_subtitle}</p>' if subtitle else ""
    )
    return (
        '<header class="cds-page-header">'
        f'<p class="cds-page-header__eyebrow">{safe_eyebrow}</p>'
        f'<h1 class="cds-page-header__title">{safe_title}</h1>'
        f"{subtitle_html}"
        "</header>"
    )


def _ensure_page_header(
    content: str, *, title: str, eyebrow: str, subtitle: str
) -> str:
    parser = _StructuralGuardParser()
    parser.feed(content)
    parser.close()
    if parser.h1_count:
        return content
    return page_header(eyebrow, title, subtitle) + content


def tile(content: str, *, heading: str | None = None, classes: str = "") -> str:
    safe_classes = _validate_classes(classes, _ALLOWED_TILE_CLASS_TOKENS, "tile")
    class_attr = (
        f" cds-tile--{safe_classes.replace(' ', ' cds-tile--')}" if safe_classes else ""
    )
    heading_html = (
        f'<h2 class="cds-tile__heading">{_h(heading)}</h2>'
        if heading is not None
        else ""
    )
    return (
        f'<section class="cds-tile{class_attr}">'
        f'{heading_html}<div class="cds-tile__content">{content}</div>'
        "</section>"
    )


def tag(text: str, tone: str = "gray") -> str:
    if tone not in _ALLOWED_TAG_TONES:
        raise ValueError("Unsupported tag tone")
    return f'<span class="cds-tag cds-tag--{tone}">{_h(text)}</span>'


def field(
    field_id: str,
    label: str,
    *,
    value: str = "",
    input_type: str = "text",
    help_text: str = "",
    required: bool = False,
) -> str:
    if input_type not in _ALLOWED_INPUT_TYPES:
        raise ValueError("Unsupported input type")

    safe_id = _h(field_id)
    safe_label = _h(label)
    safe_value = _h(value)
    help_id = f"{safe_id}-help"
    required_attr = " required" if required else ""
    required_marker = (
        '<span class="cds-required" aria-hidden="true">*</span>' if required else ""
    )
    help_html = (
        f'<p class="cds-field__help" id="{help_id}">{_h(help_text)}</p>'
        if help_text
        else ""
    )
    described_by = f' aria-describedby="{help_id}"' if help_text else ""

    return (
        '<div class="cds-field">'
        f'<label class="cds-field__label" for="{safe_id}">{safe_label}{required_marker}</label>'
        f'<input class="cds-field__input" id="{safe_id}" type="{input_type}" value="{safe_value}"{required_attr}{described_by}>'
        f"{help_html}"
        "</div>"
    )


def toggle(
    toggle_id: str,
    label: str,
    *,
    checked: bool = False,
    compact: bool = False,
    help_text: str = "",
) -> str:
    safe_id = _h(toggle_id)
    safe_label = _h(label)
    checked_attr = " checked" if checked else ""
    compact_class = " cds-toggle--compact" if compact else ""
    checked_text = "true" if checked else "false"
    help_id = f"{safe_id}-help"
    help_html = (
        f'<p class="cds-toggle__help" id="{help_id}">{_h(help_text)}</p>'
        if help_text
        else ""
    )
    described_by = f' aria-describedby="{help_id}"' if help_text else ""

    return (
        f'<div class="cds-toggle{compact_class}">'
        f'<input id="{safe_id}" class="cds-toggle__input" type="checkbox" role="switch" aria-checked="{checked_text}"{checked_attr}{described_by}>'
        f'<label class="cds-toggle__label" for="{safe_id}">'
        f'<span class="cds-toggle__label-text">{safe_label}</span>'
        '<span class="cds-toggle__control" aria-hidden="true"></span>'
        "</label>"
        f"{help_html}"
        "</div>"
    )


def data_table(
    columns: Sequence[TableColumn],
    rows: Sequence[Mapping[str, object]],
    *,
    caption: str,
) -> str:
    headers = []
    for column in columns:
        column_classes = _validate_classes(
            column.classes,
            _ALLOWED_TABLE_COLUMN_CLASS_TOKENS,
            "table column",
        )
        class_attr = f' class="{column_classes}"' if column_classes else ""
        headers.append(f'<th scope="col"{class_attr}>{_h(column.label)}</th>')

    body_rows = []
    for row in rows:
        cells = []
        for column in columns:
            cell_value = row.get(column.key, "")
            column_classes = _validate_classes(
                column.classes,
                _ALLOWED_TABLE_COLUMN_CLASS_TOKENS,
                "table column",
            )
            class_attr = f' class="{column_classes}"' if column_classes else ""
            cells.append(f"<td{class_attr}>{_h(cell_value)}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    return (
        '<div class="cds-table-wrap">'
        '<table class="cds-table">'
        f"<caption>{_h(caption)}</caption>"
        "<thead><tr>"
        + "".join(headers)
        + "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table></div>"
    )


def notification(kind: str, title: str, message: str, actions: str = "") -> str:
    if kind not in _ALLOWED_NOTIFICATION_KINDS:
        raise ValueError("Unsupported notification kind")
    role = "alert" if kind in {"warning", "error"} else "status"
    actions_html = (
        f'<div class="cds-notification__actions">{actions}</div>' if actions else ""
    )
    return (
        f'<section class="cds-notification cds-notification--{kind}" role="{role}">'
        f'<h2 class="cds-notification__title">{_h(title)}</h2>'
        f'<p class="cds-notification__message">{_h(message)}</p>'
        f"{actions_html}"
        "</section>"
    )


def simple_page(title: str, content: str, *, status: str = "info") -> str:
    if status not in _ALLOWED_NOTIFICATION_KINDS:
        raise ValueError("Unsupported simple-page status")
    return (
        page_header("System", title) + notification(status, title, "") + tile(content)
    )


_ERROR_PAGE_DEFAULTS: dict[int, tuple[str, str]] = {
    401: ("Unauthorized", "Authentication is required to access this page."),
    403: ("Forbidden", "You don't have permission to access this page."),
    404: ("Not Found", "The page you're looking for doesn't exist."),
}


def render_carbon_simple_page(
    content: str,
    *,
    title: str,
    show_classic_switch: bool = True,
    wide: bool = False,
) -> str:
    """Minimal standalone Carbon document for auth/system/error pages.

    Unlike ``render_carbon_html`` this renders no side navigation, header
    bar, or CAPTCHA badge - callers are unauthenticated visitors (login) or
    mid-flow status pages (success/failure/reconnect/errors) that should not
    imply a live authenticated session or dashboard chrome. Content is
    caller-built HTML (typically via ``simple_page`` or hand-composed
    ``page_header``/``notification``/``tile`` calls for interactive forms
    such as login).

    ``wide=True`` adds the ``cds-status-card--wide`` modifier
    class, raising the card's cap from 440px to 760px. Login and every
    status/error page keep the narrow default - only the eight
    setup-server forms opt in (via their shared ``_shell()`` wrapper),
    since their content (the Hostnames page's per-row table with inline
    Details/credentials panels in particular) was measurably cramped at
    440px on a real desktop viewport once that width actually applied.
    """
    api_key = _h(_read_api_key_for_browser())
    safe_title = _h(title)
    version = _h(get_version())

    footer_html = f'<p class="cds-version">Quasarr v{version}</p>'
    if show_classic_switch:
        footer_html += (
            '<a class="cds-classic-link" href="/ui/classic?next=/">'
            "Switch to Classic UI</a>"
        )

    card_class = "cds-status-card cds-status-card--wide" if wide else "cds-status-card"

    html = (
        "<!doctype html>"
        '<html lang="en" data-carbon-theme="light">'
        "<head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{safe_title}</title>"
        f'<meta name="description" content="Quasarr - {safe_title}">'
        f'<meta name="quasarr-api-key" content="{api_key}">'
        '<link rel="icon" href="data:,">'
        f'<link rel="stylesheet" href="{asset_url("carbon.css")}">'
        f'<script src="{asset_url("carbon.js")}"></script>'
        "</head>"
        "<body>"
        '<a class="cds-skip-link" href="#main-content">Skip to main content</a>'
        '<main id="main-content" class="cds-status-main" aria-label="Main">'
        f'<div class="{card_class}">'
        f"{content}"
        f'<footer class="cds-status-footer">{footer_html}</footer>'
        "</div>"
        "</main>"
        "</body></html>"
    )

    _assert_structural_guards(html)
    return html


def render_carbon_error_page(
    status_code: int,
    message: str | None = None,
    *,
    title: str | None = None,
    back_href: str = "/",
    show_classic_switch: bool = True,
) -> str:
    """Carbon page for a real HTTP 401/403/404 response.

    Closed to the three status codes Quasarr's browser surface actually
    raises to a visitor (Basic-auth challenges, missing resources). Reused
    for the "package not found" case by passing a specific ``message``.
    ``show_classic_switch=False`` reproduces the login page's exemption for
    an unauthenticated 401 challenge - no ``/ui/classic`` escape hatch shown
    before the visitor has proven who they are.
    """
    if status_code not in _ERROR_PAGE_DEFAULTS:
        raise ValueError("Unsupported error status code")

    default_title, default_message = _ERROR_PAGE_DEFAULTS[status_code]
    resolved_title = title if title is not None else default_title
    resolved_message = message if message is not None else default_message

    back_link = f'<a class="cds-btn cds-btn--primary" href="{_h(back_href)}">Back</a>'
    body = f"<p>{_h(resolved_message)}</p>{back_link}"
    page_content = simple_page(resolved_title, body, status="error")
    return render_carbon_simple_page(
        page_content, title=resolved_title, show_classic_switch=show_classic_switch
    )


def render_carbon_html(
    active_page: str,
    content: str,
    *,
    title: str,
    eyebrow: str = "",
    subtitle: str = "",
    captcha_count: int = 0,
    show_user: bool = False,
) -> str:
    if type(captcha_count) is not int or captcha_count < 0:
        raise ValueError("captcha_count must be a nonnegative integer")

    page_key, current_href = _validate_active_page(active_page)
    api_key = _h(_read_api_key_for_browser())
    safe_title = _h(title)
    version = _h(get_version())
    notification_label = f"Notifications, {captcha_count} CAPTCHA items"
    page_content = _ensure_page_header(
        content,
        title=title,
        eyebrow=eyebrow,
        subtitle=subtitle,
    )

    nav_items = []
    for key, label, href, icon in _NAV_ITEMS:
        active_attr = ' aria-current="page"' if key == page_key else ""
        if key == "captcha":
            label_html = f"{_h(label)} {tag(str(captcha_count), tone='gray')}"
        else:
            label_html = _h(label)

        nav_items.append(
            '<li class="cds-nav__item">'
            f'<a class="cds-nav__link" href="{href}"{active_attr}>'
            f"{render_icon(icon, class_name='cds-icon cds-icon--sm')}"
            f"<span>{label_html}</span>"
            "</a></li>"
        )

    user_controls = ""
    if show_user:
        user_controls = (
            '<a class="cds-icon-button" href="/settings" aria-label="User settings" title="User settings">'
            f"{render_icon('user', class_name='cds-icon cds-icon--sm')}"
            "</a>"
            '<a class="cds-icon-button" href="/logout" aria-label="Logout" title="Logout">'
            f"{render_icon('logout', class_name='cds-icon cds-icon--sm')}"
            "</a>"
        )

    html = (
        "<!doctype html>"
        '<html lang="en" data-carbon-theme="light">'
        "<head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{safe_title}</title>"
        f'<meta name="description" content="Quasarr - {safe_title}">'
        f'<meta name="quasarr-api-key" content="{api_key}">'
        '<link rel="icon" href="data:,">'
        f'<link rel="stylesheet" href="{asset_url("carbon.css")}">'
        f'<script src="{asset_url("carbon.js")}"></script>'
        "</head>"
        "<body>"
        '<div class="cds-shell">'
        '<a class="cds-skip-link" href="#main-content">Skip to main content</a>'
        '<header class="cds-header">'
        '<div class="cds-header__brand">'
        '<button class="cds-icon-button" type="button" data-action="nav-open" aria-controls="cds-side-nav" aria-expanded="false" aria-label="Open navigation" title="Open navigation">'
        f"{render_icon('menu', class_name='cds-icon cds-icon--sm')}"
        "</button>"
        '<span class="cds-product">Quasarr</span>'
        "</div>"
        '<div class="cds-header__status">'
        '<span class="cds-captcha-count" aria-label="CAPTCHA items">'
        "CAPTCHA "
        f"<strong>{captcha_count}</strong>"
        "</span></div>"
        '<div class="cds-header__controls">'
        '<button class="cds-icon-button" type="button" data-action="theme-toggle" aria-label="Toggle theme" title="Toggle theme">'
        '<span class="cds-theme-icon cds-theme-icon--light">'
        f"{render_icon('light', class_name='cds-icon cds-icon--sm')}"
        "</span>"
        '<span class="cds-theme-icon cds-theme-icon--dark">'
        f"{render_icon('moon', class_name='cds-icon cds-icon--sm')}"
        "</span>"
        "</button>"
        f'<a class="cds-icon-button" href="/captcha" aria-label="{notification_label}" title="{notification_label}">'
        f"{render_icon('notification', class_name='cds-icon cds-icon--sm')}"
        f'<span class="cds-header__badge" aria-hidden="true">{captcha_count}</span>'
        "</a>"
        f"{user_controls}"
        "</div>"
        "</header>"
        '<aside class="cds-nav" id="cds-side-nav" aria-label="Primary">'
        '<div class="cds-nav__header">'
        f'<button class="cds-icon-button" type="button" data-action="nav-close" aria-label="Close navigation" title="Close navigation">{render_icon("close", class_name="cds-icon cds-icon--sm")}</button>'
        "</div>"
        '<nav><ul class="cds-nav__list">' + "".join(nav_items) + "</ul></nav>"
        '<div class="cds-nav__footer">'
        f'<a class="cds-classic-link" href="/ui/classic?next={current_href}">Switch to Classic UI</a>'
        f'<p class="cds-version">v{version}</p>'
        "</div>"
        "</aside>"
        '<div class="cds-nav-backdrop" id="cds-nav-backdrop" data-action="nav-close" hidden></div>'
        '<main id="main-content" class="cds-main" aria-label="Main">'
        '<div class="cds-main__inner">'
        f"{page_content}"
        "</div>"
        "</main>"
        "</div>"
        '<section id="cds-modal" class="cds-modal" role="dialog" aria-modal="true" aria-labelledby="cds-modal-title" tabindex="-1" hidden>'
        '<div class="cds-modal__surface">'
        '<div class="cds-modal__header">'
        '<h2 id="cds-modal-title">Dialog</h2>'
        '<button class="cds-icon-button" type="button" data-action="modal-close" aria-label="Close dialog" title="Close dialog">'
        f"{render_icon('close', class_name='cds-icon cds-icon--sm')}"
        "</button>"
        "</div>"
        '<div id="cds-modal-body" class="cds-modal__body"></div>'
        '<div id="cds-modal-actions" class="cds-modal__actions"></div>'
        "</div>"
        "</section>"
        '<section id="cds-toast-region" class="cds-toast-region" aria-live="polite" aria-atomic="true"></section>'
        "</body></html>"
    )

    _assert_structural_guards(html)
    return html


__all__ = [
    "TableColumn",
    "render_carbon_html",
    "page_header",
    "protected_captcha_count",
    "tile",
    "tag",
    "field",
    "toggle",
    "data_table",
    "notification",
    "simple_page",
    "render_carbon_simple_page",
    "render_carbon_error_page",
]
