# -*- coding: utf-8 -*-

import dataclasses
import importlib
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from quasarr.providers.carbon_templates import (
    grid,
    icon_button,
    kv_rows,
    metric_tile,
    render_carbon_html,
    status,
    tile,
)


class _TagCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs), False))

    def handle_startendtag(self, tag, attrs):
        self.tags.append((tag, dict(attrs), True))


class CarbonTemplateContractsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = importlib.import_module("quasarr.providers.carbon_templates")
        cls.icons = importlib.import_module("quasarr.providers.carbon_icons")

    def _collect(self, html):
        parser = _TagCollector()
        parser.feed(html)
        return parser.tags

    def _ids(self, html):
        ids = []
        for _tag, attrs, _closed in self._collect(html):
            value = attrs.get("id")
            if value:
                ids.append(value)
        return ids

    def _assert_no_inline_handlers(self, html):
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", html, flags=re.IGNORECASE))

    def _assert_no_inline_script(self, html):
        for tag, attrs, _closed in self._collect(html):
            if tag.lower() == "script":
                self.assertIn("src", attrs)

    def _assert_only_local_urls(self, html):
        for _tag, attrs, _closed in self._collect(html):
            for key in ("href", "src"):
                value = attrs.get(key)
                if not value:
                    continue
                if value.startswith("data:"):
                    continue
                if value.startswith("#"):
                    continue
                self.assertFalse(value.startswith("http://"))
                self.assertFalse(value.startswith("https://"))
                self.assertTrue(value.startswith("/"), msg=f"non-local URL: {value}")

    def _assert_target_blank_rel(self, html):
        for _tag, attrs, _closed in self._collect(html):
            if attrs.get("target") == "_blank":
                rel = attrs.get("rel", "")
                parts = {p.strip() for p in rel.split() if p.strip()}
                self.assertEqual(parts, {"noopener", "noreferrer"})

    def _assert_no_duplicate_ids(self, html):
        ids = self._ids(html)
        self.assertEqual(len(ids), len(set(ids)))

    def _assert_icon_buttons_labeled(self, html):
        for tag, attrs, _closed in self._collect(html):
            if tag != "button":
                continue
            classes = attrs.get("class", "")
            if "icon" not in classes and "cds-icon" not in classes:
                continue
            self.assertTrue(attrs.get("aria-label"))
            self.assertTrue(attrs.get("title"))

    def test_public_api_contract_exists(self):
        self.assertTrue(dataclasses.is_dataclass(self.mod.TableColumn))
        self.assertTrue(self.mod.TableColumn.__dataclass_params__.frozen)
        self.assertEqual(
            [f.name for f in dataclasses.fields(self.mod.TableColumn)],
            ["key", "label", "classes"],
        )
        self.assertEqual(self.mod.TableColumn("k", "l").classes, "")

        required = {
            "render_carbon_html",
            "page_header",
            "tile",
            "tag",
            "status",
            "grid",
            "kv_rows",
            "metric_tile",
            "icon_button",
            "field",
            "toggle",
            "data_table",
            "notification",
            "simple_page",
            "render_carbon_simple_page",
            "render_carbon_error_page",
        }
        self.assertTrue(required.issubset(set(dir(self.mod))))

    def test_render_shell_document_landmarks_and_controls(self):
        body = self.mod.page_header("Ops", "Dashboard", "Runtime") + self.mod.tile(
            "Body"
        )
        html = self.mod.render_carbon_html(
            "dashboard",
            body,
            title="Dashboard",
            eyebrow="Ops",
            subtitle="Runtime",
            captcha_count=3,
            show_user=True,
        )

        self.assertIn("<!doctype html>", html.lower())
        self.assertIn('<html lang="en" data-carbon-theme="', html)
        self.assertIn('<meta charset="utf-8">', html)
        self.assertIn(
            '<meta name="viewport" content="width=device-width, initial-scale=1">', html
        )
        self.assertIn('<meta name="description" content="Quasarr - Dashboard">', html)
        self.assertIn('href="/static/carbon.css?v=', html)
        self.assertIn('src="/static/carbon.js?v=', html)
        self.assertIn('class="cds-skip-link"', html)
        self.assertIn('aria-label="Main"', html)
        self.assertIn('role="dialog"', html)
        self.assertIn('aria-modal="true"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertLess(
            html.index('class="cds-shell"'), html.index('class="cds-skip-link"')
        )
        self.assertIn('<div class="cds-modal__header">', html)
        self.assertIn('<div id="cds-modal-actions" class="cds-modal__actions">', html)
        self.assertNotIn('<header class="cds-modal__header">', html)
        self.assertNotIn('<footer id="cds-modal-actions"', html)
        self.assertIn('<link rel="icon" href="data:,">', html)
        self.assertIn('href="/ui/classic?next=/"', html)
        self.assertIn('aria-controls="cds-side-nav"', html)
        self.assertIn('aria-expanded="false"', html)
        self.assertIn(
            'href="/captcha" aria-label="Notifications, 3 CAPTCHA items"', html
        )
        self.assertIn('href="/settings" aria-label="User settings"', html)

        for href in (
            "/",
            "/packages",
            "/statistics",
            "/hostnames",
            "/categories",
            "/captcha",
            "/settings",
        ):
            self.assertIn(f'href="{href}"', html)

        self.assertIn('aria-current="page"', html)
        self.assertIn("CAPTCHA", html)
        self.assertIn("Logout", html)
        self.assertEqual(html.count(">Ops<"), 1)
        self.assertEqual(html.count(">Runtime<"), 1)

        self._assert_no_inline_handlers(html)
        self._assert_no_inline_script(html)
        self._assert_only_local_urls(html)
        self._assert_target_blank_rel(html)
        self._assert_no_duplicate_ids(html)
        self._assert_icon_buttons_labeled(html)

    def test_show_user_false_hides_user_and_logout(self):
        html = self.mod.render_carbon_html(
            "dashboard",
            self.mod.page_header("Ops", "Dashboard"),
            title="Dashboard",
            show_user=False,
        )
        self.assertNotIn("Logout", html)
        self.assertNotIn('aria-label="User menu"', html)

    def test_meta_api_key_only_when_safe(self):
        with (
            mock.patch.object(self.mod, "is_auth_enabled", return_value=False),
            mock.patch.object(self.mod, "Config") as config_cls,
        ):
            config_cls.return_value.get.return_value = "unsafe<key>"
            html = self.mod.render_carbon_html(
                "dashboard", self.mod.page_header("Ops", "Dash"), title="T"
            )
        self.assertIn('meta name="quasarr-api-key"', html)
        self.assertIn("unsafe&lt;key&gt;", html)

        with (
            mock.patch.object(self.mod, "is_auth_enabled", return_value=True),
            mock.patch.object(self.mod, "is_browser_authenticated", return_value=False),
            mock.patch.object(self.mod, "Config") as config_cls,
        ):
            config_cls.return_value.get.return_value = "should-not-appear"
            html = self.mod.render_carbon_html(
                "dashboard", self.mod.page_header("Ops", "Dash"), title="T"
            )
        self.assertIn('meta name="quasarr-api-key" content=""', html)
        self.assertNotIn("should-not-appear", html)

        with (
            mock.patch.object(self.mod, "is_auth_enabled", return_value=False),
            mock.patch.object(self.mod, "Config") as config_cls,
        ):
            config_cls.return_value.get.return_value = True
            html = self.mod.render_carbon_html(
                "dashboard", self.mod.page_header("Ops", "Dash"), title="T"
            )
        self.assertIn('meta name="quasarr-api-key" content=""', html)
        self.assertNotIn('content="True"', html)

    def test_escaping_and_renderer_owned_html_boundaries(self):
        html = self.mod.render_carbon_html(
            "dashboard",
            self.mod.page_header("<b>e</b>", "<title>", "<s>")
            + self.mod.tile("<p id='safe'>raw</p>", heading="<h>")
            + self.mod.notification(
                "info",
                "<x>",
                "<y>",
                actions='<a target="_blank" rel="noopener noreferrer" href="/x">A</a>',
            ),
            title='T"<',
            eyebrow="<e>",
            subtitle="<s>",
        )

        self.assertIn("&lt;title&gt;", html)
        self.assertIn("&lt;b&gt;e&lt;/b&gt;", html)
        self.assertIn("&lt;x&gt;", html)
        self.assertIn("&lt;y&gt;", html)
        self.assertIn("<p id='safe'>raw</p>", html)

    def test_component_contracts(self):
        field_html = self.mod.field(
            "f<id>",
            "Label<&>",
            value='v"<',
            input_type="text",
            help_text="Help<&>",
            required=True,
        )
        self.assertIn('for="f&lt;id&gt;"', field_html)
        self.assertIn("required", field_html)
        self.assertIn("Help&lt;&amp;&gt;", field_html)

        toggle_html = self.mod.toggle(
            "t1", "On/Off", checked=True, compact=True, help_text="Assist"
        )
        self.assertIn('type="checkbox"', toggle_html)
        self.assertIn('role="switch"', toggle_html)
        self.assertIn('aria-checked="true"', toggle_html)
        self.assertRegex(
            toggle_html,
            r'<label class="cds-toggle__label" for="t1">'
            r'<span class="cds-toggle__label-text">On/Off</span>'
            r'<span class="cds-toggle__control" aria-hidden="true"></span>'
            r"</label>",
        )

        table_html = self.mod.data_table(
            [
                self.mod.TableColumn("name", "Name"),
                self.mod.TableColumn("status", "Status", classes="is-status"),
            ],
            [{"name": "A<", "status": "ok&"}],
            caption="Rows<&>",
        )
        self.assertIn("<caption>", table_html)
        self.assertIn('scope="col"', table_html)
        self.assertIn("A&lt;", table_html)
        self.assertIn("ok&amp;", table_html)

    def test_closed_token_validation(self):
        with self.assertRaises(ValueError):
            self.mod.tag("X", tone="bad")
        with self.assertRaises(ValueError):
            self.mod.notification("bad", "t", "m")
        with self.assertRaises(ValueError):
            self.mod.field("x", "L", input_type="image")

        with self.assertRaises(ValueError):
            self.mod.render_carbon_html(
                "unknown", self.mod.page_header("Ops", "Dash"), title="Dash"
            )
        with self.assertRaises(ValueError):
            self.mod.render_carbon_html(
                "dashboard",
                self.mod.page_header("Ops", "Dash"),
                title="Dash",
                captcha_count=-1,
            )
        with self.assertRaises(ValueError):
            self.mod.render_carbon_html(
                "dashboard",
                self.mod.page_header("Ops", "Dash"),
                title="Dash",
                captcha_count=True,
            )

    def test_structural_guards_reject_unsafe_renderer_fragments(self):
        fragments = {
            "inline handler": '<button onclick="run()">Run</button>',
            "inline script": "<script>run()</script>",
            "remote source": '<img src="https://assets.invalid/x.png">',
            "protocol-relative source": '<script src="//assets.invalid/x.js"></script>',
            "data script source": (
                '<script src="data:text/javascript,alert(1)"></script>'
            ),
            "blob image source": '<img src="blob:https://app.invalid/id">',
            "file image source": '<img src="file:///tmp/image.png">',
            "ftp image source": '<img src="ftp://assets.invalid/image.png">',
            "remote stylesheet": (
                '<link rel="stylesheet" href="https://assets.invalid/x.css">'
            ),
            "remote preload": '<link rel="preload" href="https://assets.invalid/x">',
            "remote preconnect": (
                '<link rel="preconnect" href="https://assets.invalid">'
            ),
            "unlabeled icon control": (
                '<button class="cds-icon-button" type="button">X</button>'
            ),
            "icon control without tooltip": (
                '<button class="cds-icon-button" type="button" '
                'aria-label="Close">X</button>'
            ),
            "duplicate id": '<div id="same"></div><span id="same"></span>',
            "multiple page headings": "<h1>First</h1><h1>Second</h1>",
            "unsafe blank target": (
                '<a href="https://docs.invalid" target="_blank">Docs</a>'
            ),
        }

        for label, fragment in fragments.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.mod._assert_structural_guards(fragment)

    def test_structural_guards_allow_explicit_external_navigation(self):
        fragment = (
            '<a href="https://docs.invalid/guide">Guide</a>'
            '<a href="https://docs.invalid/reference" target="_blank" '
            'rel="nofollow noopener noreferrer">Reference</a>'
            '<img src="data:image/png;base64,AA==" alt="Local preview">'
        )

        self.mod._assert_structural_guards(fragment)

    def test_notification_roles_match_urgency(self):
        for kind in ("warning", "error"):
            with self.subTest(kind=kind):
                self.assertIn('role="alert"', self.mod.notification(kind, "T", "M"))
        for kind in ("info", "success"):
            with self.subTest(kind=kind):
                self.assertIn('role="status"', self.mod.notification(kind, "T", "M"))

    def test_visible_icon_requires_accessible_title(self):
        with self.assertRaises(ValueError):
            self.icons.render_icon("user", aria_hidden=False)

        icon = self.icons.render_icon(
            "user", aria_hidden=False, title="Authenticated user"
        )
        self.assertIn('aria-label="Authenticated user"', icon)
        self.assertIn("<title>Authenticated user</title>", icon)

    def test_shell_adds_page_header_only_when_content_has_no_h1(self):
        html = self.mod.render_carbon_html(
            "dashboard",
            self.mod.tile("Body"),
            title="Fallback title",
            eyebrow="Operations",
            subtitle="Fallback subtitle",
        )

        self.assertEqual(len(re.findall(r"<h1\b", html, flags=re.IGNORECASE)), 1)
        self.assertIn(">Fallback title</h1>", html)
        self.assertIn(">Operations</p>", html)
        self.assertIn(">Fallback subtitle</p>", html)

    def test_single_h1_owned_by_page_header_not_shell_branding(self):
        html = self.mod.render_carbon_html(
            "dashboard",
            self.mod.page_header("Ops", "Main H1", "Sub") + self.mod.tile("X"),
            title="Main H1",
        )
        h1_count = len(re.findall(r"<h1\b", html, flags=re.IGNORECASE))
        self.assertEqual(h1_count, 1)

    def test_renderer_owned_emoji_guard(self):
        """Range covers flag emoji (regional indicators, 1F1E6-1F1FF - the
        codepoints LANGUAGE_FLAG_EMOJI is built from), Misc Technical through
        Geometric Shapes (2300-25FF), Misc Symbols/Dingbats (2600-27BF, kept
        from the original guard), and the emoji supplementary planes
        (1F300-1FAFF, also kept).
        """
        html = self.mod.render_carbon_html(
            "dashboard",
            self.mod.page_header("Ops", "Dashboard") + self.mod.tile("X"),
            title="Dashboard",
            captcha_count=2,
        )
        self.assertIsNone(
            re.search(
                "[\U0001f1e6-\U0001f1ff\u2300-\u27bf\U0001f300-\U0001faff]",
                html,
            )
        )

    def test_render_carbon_simple_page_structural_contract(self):
        """The minimal auth/system-page shell has no side nav,
        header bar, or CAPTCHA badge - just one h1 (via the caller's
        page_header) and asset links - yet still passes every structural
        guard render_carbon_html enforces.
        """
        content = self.mod.page_header("Access", "Login") + self.mod.tile("Body")
        html = self.mod.render_carbon_simple_page(content, title="Login")

        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("<title>Login</title>", html)
        self.assertIn(f'href="{self.mod.asset_url("carbon.css")}"', html)
        self.assertIn(f'src="{self.mod.asset_url("carbon.js")}"', html)
        self.assertNotIn("cds-side-nav", html)
        self.assertNotIn("cds-header", html)
        self.assertNotIn("cds-modal", html)
        self.assertEqual(html.count("<h1"), 1)

        self._assert_no_inline_handlers(html)
        self._assert_no_inline_script(html)
        self._assert_only_local_urls(html)
        self._assert_no_duplicate_ids(html)

    def test_render_carbon_simple_page_classic_switch_toggle(self):
        content = self.mod.tile("Body")
        shown = self.mod.render_carbon_simple_page(content, title="T")
        hidden = self.mod.render_carbon_simple_page(
            content, title="T", show_classic_switch=False
        )
        self.assertIn("/ui/classic?next=/", shown)
        self.assertIn("Switch to Classic UI", shown)
        self.assertNotIn("/ui/classic", hidden)
        self.assertNotIn("Switch to Classic UI", hidden)

    def test_render_carbon_simple_page_wide_card_opt_in(self):
        """wide=True (used only by the eight setup-server pages via their
        shared _shell() wrapper - see
        tests/test_carbon_setup_pages.py::SetupPagesUseWideStatusCardTests)
        adds the cds-status-card--wide modifier class; the default (used by
        login and every status/error page) stays the plain narrow card.
        """
        content = self.mod.tile("Body")
        narrow = self.mod.render_carbon_simple_page(content, title="T")
        wide = self.mod.render_carbon_simple_page(content, title="T", wide=True)

        self.assertIn('class="cds-status-card"', narrow)
        self.assertNotIn("cds-status-card--wide", narrow)
        self.assertIn('class="cds-status-card cds-status-card--wide"', wide)

    def test_render_carbon_simple_page_api_key_blank_when_unauthenticated(self):
        with (
            mock.patch.object(self.mod, "is_auth_enabled", return_value=True),
            mock.patch.object(self.mod, "is_browser_authenticated", return_value=False),
            mock.patch.object(self.mod, "Config") as config_cls,
        ):
            config_cls.return_value.get.return_value = "should-not-appear"
            html = self.mod.render_carbon_simple_page(
                self.mod.tile("Body"), title="Login"
            )
        self.assertIn('meta name="quasarr-api-key" content=""', html)
        self.assertNotIn("should-not-appear", html)

    def test_render_carbon_error_page_closed_status_set(self):
        for status_code in (401, 403, 404):
            with self.subTest(status_code=status_code):
                html = self.mod.render_carbon_error_page(status_code)
                self.assertIn("<!doctype html>", html)
                self.assertIn("cds-notification--error", html)

        with self.assertRaises(ValueError):
            self.mod.render_carbon_error_page(500)
        with self.assertRaises(ValueError):
            self.mod.render_carbon_error_page(200)

    def test_render_carbon_error_page_message_and_title_overrides(self):
        html = self.mod.render_carbon_error_page(
            404,
            "Package not found or already solved.",
            title="Package Not Found",
            back_href="/packages",
        )
        self.assertIn("Package Not Found", html)
        self.assertIn("Package not found or already solved.", html)
        self.assertIn('href="/packages"', html)

    def test_render_carbon_error_page_default_messages_present(self):
        import html as html_module

        defaults = {
            401: "Authentication is required to access this page.",
            403: "You don't have permission to access this page.",
            404: "The page you're looking for doesn't exist.",
        }
        for status_code, message in defaults.items():
            with self.subTest(status_code=status_code):
                html = self.mod.render_carbon_error_page(status_code)
                self.assertIn(html_module.escape(message, quote=True), html)

    def test_render_carbon_error_page_escapes_message_and_back_href(self):
        html = self.mod.render_carbon_error_page(
            404, "<script>alert(1)</script>", back_href='/"><script>x</script>'
        )
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self._assert_no_inline_script(html)


class CarbonTemplateHelperTests(unittest.TestCase):
    """The five design-system helpers every page renderer composes with,
    plus the shell chrome they sit inside. Each helper owns one repeated
    markup shape from the approved design so no page renderer hand-writes
    it: the status dot, the page grids, the key-value list, the metric
    tile, and the compact row-action icon button.
    """

    def test_status_renders_dot_and_text(self):
        html = status("Connected", "success", strong=True)
        self.assertEqual(
            html,
            '<span class="cds-status cds-status--success cds-status--strong">'
            '<span class="cds-status__dot" aria-hidden="true"></span>Connected</span>',
        )

    def test_status_as_button_carries_action_and_data(self):
        html = status(
            "Login failed",
            "error",
            tinted=True,
            as_button=True,
            action="hostname-status",
            data={"hostname-id": "nx"},
        )
        self.assertTrue(
            html.startswith(
                '<button type="button" class="cds-status cds-status--error '
                'cds-status--tinted cds-status--link"'
            )
        )
        self.assertIn('data-action="hostname-status"', html)
        self.assertIn('data-hostname-id="nx"', html)
        self.assertIn("Login failed</button>", html)

    def test_status_escapes_text_action_and_data(self):
        html = status(
            "<b>down</b>",
            "warning",
            as_button=True,
            action='x"><script>',
            data={"id": '"><script>'},
        )
        self.assertNotIn("<b>down</b>", html)
        self.assertIn("&lt;b&gt;down&lt;/b&gt;", html)
        self.assertNotIn("<script>", html)

    def test_status_rejects_unknown_tone(self):
        with self.assertRaises(ValueError):
            status("x", "pink")

    def test_status_dot_only_hides_visible_text_but_keeps_it_accessible(self):
        """dot_only=True must never leave colour as the only carrier of
        meaning: the label moves to `title` (hover) and a visually-hidden
        text node (screen readers), and the dot itself is unchanged.
        """
        html = status("Hostname not configured", "neutral", tinted=True, dot_only=True)
        self.assertEqual(
            html,
            '<span class="cds-status cds-status--neutral cds-status--tinted '
            'cds-status--dot-only" title="Hostname not configured">'
            '<span class="cds-status__dot" aria-hidden="true"></span>'
            '<span class="cds-visually-hidden">Hostname not configured</span>'
            "</span>",
        )
        self.assertNotIn(
            ">Hostname not configured<", html.split("cds-visually-hidden")[0]
        )

    def test_status_dot_only_as_button_stays_keyboard_reachable(self):
        html = status(
            "Working normally",
            "success",
            as_button=True,
            action="hostname-status",
            data={"hostname-id": "nx"},
            dot_only=True,
        )
        self.assertEqual(
            html,
            '<button type="button" class="cds-status cds-status--success '
            'cds-status--dot-only cds-status--link" title="Working normally" '
            'data-action="hostname-status" data-hostname-id="nx">'
            '<span class="cds-status__dot" aria-hidden="true"></span>'
            '<span class="cds-visually-hidden">Working normally</span>'
            "</button>",
        )

    def test_status_dot_only_escapes_text_in_title_and_hidden_node(self):
        html = status("<b>bad</b>", "error", dot_only=True)
        self.assertNotIn("<b>bad</b>", html)
        self.assertIn('title="&lt;b&gt;bad&lt;/b&gt;"', html)
        self.assertIn("&lt;b&gt;bad&lt;/b&gt;</span>", html)

    def test_grid_variants(self):
        self.assertEqual(
            grid(["<a></a>", "<b></b>"], "dashboard"),
            '<div class="cds-grid--dashboard"><a></a><b></b></div>',
        )
        self.assertEqual(
            grid(["<a></a>"], "stack"), '<div class="cds-stack"><a></a></div>'
        )
        self.assertEqual(
            grid(["<a></a>", "<b></b>"], "auto"),
            '<div class="cds-grid--auto"><a></a><b></b></div>',
        )
        with self.assertRaises(ValueError):
            grid([], "4")

    def test_metric_tile_markup(self):
        html = metric_tile(
            "Download attempts", "1,284", "94.2% success rate", sub_success=True
        )
        self.assertIn('class="cds-tile cds-tile--is-status cds-tile--is-metric"', html)
        self.assertIn('<h2 class="cds-tile__heading">Download attempts</h2>', html)
        self.assertIn('<p class="cds-metric__value">1,284</p>', html)
        self.assertIn(
            '<p class="cds-metric__sub cds-metric__sub--success">94.2% success rate</p>',
            html,
        )

    def test_metric_tile_without_sub_line(self):
        html = metric_tile("Sources", "3")
        self.assertIn('<p class="cds-metric__value">3</p>', html)
        self.assertNotIn("cds-metric__sub", html)

    def test_kv_rows_escape_values(self):
        self.assertEqual(
            kv_rows([("IMDb cached IDs", "<1>")]),
            '<div class="cds-kv__row"><span class="cds-kv__label">IMDb cached IDs</span>'
            '<span class="cds-kv__value">&lt;1&gt;</span></div>',
        )

    def test_icon_button_markup_and_danger_variant(self):
        html = icon_button(
            "trash-can", "Delete package", action="package-delete", data={"id": "p1"}
        )
        self.assertIn('class="cds-icon-button cds-icon-button--sm"', html)
        self.assertIn('type="button"', html)
        self.assertIn('aria-label="Delete package"', html)
        self.assertIn('title="Delete package"', html)
        self.assertIn('data-action="package-delete"', html)
        self.assertIn('data-id="p1"', html)
        self.assertIn("<svg", html)

        danger = icon_button(
            "trash-can", "Delete", action="package-delete", danger=True
        )
        self.assertIn(
            'class="cds-icon-button cds-icon-button--sm cds-icon-button--danger"',
            danger,
        )

    def test_tile_help_text_follows_the_heading(self):
        html = tile("Body", heading="Sources", help_text="Two configured")
        self.assertIn(
            '<h2 class="cds-tile__heading">Sources</h2>'
            '<p class="cds-tile__help">Two configured</p>'
            '<div class="cds-tile__content">Body</div>',
            html,
        )
        self.assertNotIn("cds-tile__help", tile("Body", heading="Sources"))

    def test_shell_brand_and_footer(self):
        html = render_carbon_html(
            "dashboard", "<p>x</p>", title="Dashboard", captcha_count=2, show_user=False
        )
        self.assertIn(
            '<span class="cds-product"><strong>Quasarr</strong> Web UI</span>', html
        )
        self.assertNotIn("cds-captcha-count", html)
        self.assertNotIn("cds-header__status", html)
        self.assertIn('class="cds-header__badge"', html)
        # Behavior kept from the previous shell: the switch returns to the
        # Classic equivalent of the current page, not to Classic's home.
        self.assertIn('href="/ui/classic?next=/"', html)
        self.assertIn('class="cds-nav__link cds-nav__link--footer"', html)
        self.assertIn('<p class="cds-nav__version">Quasarr v', html)

    def test_shell_badge_only_when_captcha_items_are_waiting(self):
        """Spec 2.5: the bell badge appears only for a counter > 0; the
        text indicator "CAPTCHA n" that used to sit in the header center
        is gone entirely.
        """
        empty = render_carbon_html("dashboard", "<p>x</p>", title="Dashboard")
        self.assertNotIn("cds-header__badge", empty)
        self.assertIn('aria-label="Notifications, 0 CAPTCHA items"', empty)

        waiting = render_carbon_html(
            "dashboard", "<p>x</p>", title="Dashboard", captcha_count=7
        )
        self.assertIn(
            '<span class="cds-header__badge" aria-hidden="true">7</span>', waiting
        )


class ProtectedCaptchaCountTests(unittest.TestCase):
    """``protected_captcha_count`` is the Carbon-UI-integration hoist of the
    shell's CAPTCHA-badge count out of the private
    ``quasarr.api.statistics.carbon._captcha_count`` helper: every future
    Carbon view (dashboard, settings, downloads, config, captcha) needs the
    same live protected-queue count for the shell header badge, not just
    Statistics.
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = importlib.import_module("quasarr.providers.carbon_templates")

    @staticmethod
    def _shared_state(titles):
        db = mock.Mock()
        db.retrieve_all_titles.return_value = titles
        return mock.Mock(values={"database": lambda table: db})

    def test_counts_titles_from_the_protected_database(self):
        shared_state = self._shared_state([("id-1", "t"), ("id-2", "t")])
        self.assertEqual(self.mod.protected_captcha_count(shared_state), 2)

    def test_empty_titles_count_as_zero(self):
        shared_state = self._shared_state([])
        self.assertEqual(self.mod.protected_captcha_count(shared_state), 0)

    def test_lookup_failure_returns_zero_without_raising(self):
        shared_state = mock.Mock()
        shared_state.values = {
            "database": mock.Mock(side_effect=RuntimeError("db down at source.invalid"))
        }

        with mock.patch.object(self.mod, "warn") as warn_mock:
            count = self.mod.protected_captcha_count(shared_state)

        self.assertEqual(count, 0)
        warn_mock.assert_called_once()
        (logged_message,), _kwargs = warn_mock.call_args
        # Only the exception class may be logged - the exception text could
        # carry a hostname, which must never reach the log.
        self.assertIn("RuntimeError", logged_message)
        self.assertNotIn("source.invalid", logged_message)
        self.assertNotIn("db down", logged_message)


def _css_unclosed_block_starts(css):
    """Comment-aware brace-stack walk: returns the character offset of every
    `{` that is never matched by a later `}` before EOF. A browser's CSS
    tokenizer balances braces strictly by position (not by which selector
    "should" own which block), so an unclosed block silently nests every
    rule that follows it - including entire unrelated sections - until
    something eventually closes it (or EOF, per error recovery). Substring/
    regex checks on the CSS text (e.g. `assertIn(".cds-toolbar", css)`)
    cannot catch this: the selector text is still present, it is merely
    nested somewhere it was never meant to be and therefore never matches
    any real element at the intended specificity/scope.
    """
    # Neutralize comment bodies (including any literal `{`/`}` inside them,
    # e.g. a comment illustrating CSS syntax) while preserving every other
    # character 1:1 by offset - `cleaned` stays the same length as `css`,
    # so offsets into it are also offsets into the original text.
    cleaned_chars = list(css)
    index = 0
    length = len(css)
    while index < length:
        if css[index : index + 2] == "/*":
            end = css.index("*/", index + 2) + 2
            for position in range(index, end):
                if cleaned_chars[position] not in ("\n",):
                    cleaned_chars[position] = " "
            index = end
        else:
            index += 1
    cleaned = "".join(cleaned_chars)

    stack = []
    for offset, char in enumerate(cleaned):
        if char == "{":
            stack.append(offset)
        elif char == "}" and stack:
            stack.pop()
    return stack


class CarbonStaticContractsTests(unittest.TestCase):
    def _read_static(self, filename):
        root = Path(__file__).resolve().parent.parent
        return root.joinpath("quasarr", "static", filename).read_text(encoding="utf-8")

    def test_carbon_css_has_no_unclosed_rule_or_media_blocks(self):
        """Guards against the exact class of bug two separate
        blocks had (Hostnames/Categories' 672px media query, and
        `.cds-status-footer .cds-classic-link`) - both were missing their
        own closing `}`, which silently nested EVERY rule that followed
        (Downloads' toolbar/search/sticky-column CSS included) inside them.
        Above the un-nested block's own trigger condition, none of that
        nested CSS ever actually applied in a real browser - confirmed live
        via computed styles (the Downloads search field's max-width read
        "none" instead of its declared value, and the bulk-selection
        header's margin read 0px) before this fix.
        """
        css = self._read_static("carbon.css")
        unclosed = _css_unclosed_block_starts(css)
        if unclosed:
            locations = [
                f"line {css.count(chr(10), 0, offset) + 1}" for offset in unclosed
            ]
            self.fail(f"unclosed CSS block(s) starting at: {', '.join(locations)}")

    def test_carbon_js_structure_contract(self):
        js = self._read_static("carbon.js")

        self.assertIn("localStorage.getItem('quasarr_theme')", js)
        self.assertIn("window.matchMedia('(prefers-color-scheme: dark)')", js)
        self.assertIn("document.documentElement.setAttribute('data-carbon-theme'", js)
        self.assertIn("document.addEventListener('DOMContentLoaded'", js)
        self.assertLess(
            js.find("document.documentElement.setAttribute('data-carbon-theme'"),
            js.find("document.addEventListener('DOMContentLoaded'"),
        )
        self.assertIn("window.quasarrApiFetch", js)
        self.assertIn("data-action", js)
        self.assertIn("window.showModal", js)
        self.assertIn("window.closeModal", js)
        self.assertIn("'Escape'", js)
        self.assertRegex(js, r"\bTab\b")
        self.assertIn("lastFocusedElement", js)
        self.assertGreaterEqual(js.count("try {"), 2)
        self.assertGreaterEqual(js.count("catch ("), 2)
        self.assertIn("new URL(", js)
        self.assertIn("requestUrl.origin !== window.location.origin", js)
        self.assertIn("new Headers(", js)
        self.assertIn("setAttribute('aria-checked'", js)
        self.assertIn("setAttribute('aria-expanded'", js)
        self.assertNotIn("http://", js)
        self.assertNotIn("https://", js)

    def test_carbon_css_structure_contract(self):
        css = self._read_static("carbon.css")

        self.assertGreaterEqual(css.count("@font-face"), 5)
        self.assertIn("font-display: swap", css)
        self.assertIn("--cds-bg: #f4f4f4", css)
        self.assertIn("--cds-layer: #ffffff", css)
        self.assertIn("--cds-bg: #161616", css)
        self.assertIn("--cds-layer: #262626", css)
        self.assertIn("--cds-support-success: #24a148", css)
        self.assertIn("--cds-support-warning: #f1c21b", css)
        self.assertIn("--cds-support-error: #da1e28", css)
        self.assertIn("--cds-tag-blue-bg: #d0e2ff", css)
        self.assertIn("height: 48px", css)
        self.assertIn("width: 256px", css)
        for variant in (
            "primary",
            "secondary",
            "tertiary",
            "ghost",
            "danger-ghost",
        ):
            with self.subTest(button_variant=variant):
                self.assertIn(f".cds-btn--{variant}", css)
        self.assertRegex(css, r"\.cds-btn\s*\{[^}]*min-height:\s*40px")
        for kind in ("info", "warning", "error", "success"):
            with self.subTest(notification_kind=kind):
                self.assertRegex(
                    css,
                    rf"\.cds-notification--{kind}\s*\{{[^}}]*background:",
                )
        self.assertIn(
            ".cds-toggle__input:focus-visible + .cds-toggle__label .cds-toggle__control",
            css,
        )
        self.assertIn(".cds-nav-backdrop[hidden]", css)
        self.assertRegex(
            css,
            r"@media\s*\(max-width:\s*1056px\)[\s\S]*?\.cds-nav\s*\{"
            r"[^}]*visibility:\s*hidden",
        )
        self.assertRegex(css, r"@media\s*\(max-width:\s*1056px\)")
        self.assertRegex(css, r"@media\s*\(max-width:\s*672px\)")
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertRegex(css, r"min-height:\s*44px")
        # Guards the real mistake this pin exists for - a stacked duplicate
        # class typo like `.cds-tile.cds-tile` - without flagging the
        # legitimate comma-grouped (`.cds-tile--is-compact,\n.cds-tile--is-status`)
        # and descendant (`.cds-tile--is-status .cds-tile__heading`) selectors
        # the target design now ships.
        self.assertNotRegex(css, r"\.cds-tile\.cds-tile\b")
        self.assertNotIn("http://", css)
        self.assertNotIn("https://", css)

    def test_carbon_css_kpi_row_layout_contract(self):
        """The statistics KPI row: a 4-column grid at desktop width,
        matching the file's `repeat(4, minmax(0, 1fr))`/16px gap
        conventions (a deliberate override of the design's original 1px
        hairline gap - separate tiles read as separate tiles), collapsing
        to 2 columns at the existing 1056px breakpoint and 1 column at the
        existing 672px breakpoint.
        """
        css = self._read_static("carbon.css")

        self.assertRegex(
            css,
            r"\.cds-kpi-row\s*\{[^}]*display:\s*grid;"
            r"[^}]*grid-template-columns:\s*repeat\(4,\s*minmax\(0,\s*1fr\)\);"
            r"[^}]*gap:\s*16px;",
        )
        # Status dot component + page grids exist
        self.assertRegex(
            css,
            r"\.cds-status__dot\s*\{[^}]*width:\s*10px;[^}]*height:\s*10px;[^}]*border-radius:\s*50%;",
        )
        # The Hostnames table's dot-only status keeps the 10px dot itself
        # but pads its clickable/hoverable box out to 24px - a 10px target
        # alone is too small.
        self.assertRegex(
            css,
            r"\.cds-status--dot-only\s*\{[^}]*width:\s*24px;[^}]*height:\s*24px;",
        )
        self.assertRegex(
            css, r"\.cds-grid--dashboard\s*\{[^}]*grid-template-columns:\s*1\.4fr 1fr;"
        )
        self.assertRegex(
            css,
            r"\.cds-grid--settings\s*\{[^}]*repeat\(auto-fit,\s*minmax\(400px,\s*1fr\)\);",
        )
        # Statistics' single self-arranging detail-tile grid: auto-fit
        # columns and align-items: stretch so tiles sharing a visual row
        # share a height instead of leaving a ragged gap under a shorter
        # tile (the "start" every other grid variant uses).
        self.assertRegex(
            css,
            r"\.cds-grid--auto\s*\{[^}]*display:\s*grid;"
            r"[^}]*grid-template-columns:\s*repeat\(auto-fit,\s*minmax\(300px,\s*1fr\)\);"
            r"[^}]*gap:\s*16px;"
            r"[^}]*align-items:\s*stretch;",
        )
        self.assertRegex(
            css,
            r"\.cds-modal__actions \.cds-btn\s*\{[^}]*flex:\s*1 1 0;[^}]*min-height:\s*56px;",
        )
        self.assertNotRegex(css, r"\.cds-tile\s*\{[^}]*border:\s*1px solid")
        self.assertRegex(
            css,
            r"@media\s*\(max-width:\s*1056px\)[\s\S]*?\.cds-kpi-row\s*\{"
            r"[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\);",
        )
        self.assertRegex(
            css,
            r"@media\s*\(max-width:\s*672px\)[\s\S]*?\.cds-kpi-row\s*\{"
            r"[^}]*grid-template-columns:\s*repeat\(1,\s*minmax\(0,\s*1fr\)\);",
        )

    def test_carbon_css_status_card_wide_modifier_and_credentials_panel_contract(self):
        """The wide setup-page card cap, and the credentials Details panel
        it has to fit.

        The panel used to be a flex child of a `.cds-hostname-row` and
        needed `flex-basis: 100%` to stop being squeezed to ~150-190px as a
        fourth column. The dense-table rework replaced that row with
        `.cds-hostname-table__row` and made the panel a plain block-level
        sibling, so full width now comes from normal block layout. The
        full-width guarantee itself is guarded structurally against the
        rendered markup by SetupHostnameCredentialsPanelWidthTests below;
        what this test still pins is the wide card cap and the panel's own
        separation from the row above it.
        """
        css = self._read_static("carbon.css")

        self.assertRegex(
            css,
            r"\.cds-status-card--wide\s*\{[^}]*max-width:\s*760px;",
        )
        self.assertNotIn(".cds-hostname-row {", css)
        self.assertRegex(
            css,
            r"\.cds-hostname-credentials\s*\{[^}]*margin-top:\s*16px;"
            r"[^}]*border-top:[^}]*padding-top:\s*16px;",
        )

    def test_reduced_motion_block_sits_above_the_once_corrupted_region(self):
        """Corrects an earlier inaccuracy: prefers-reduced-motion sits
        well ABOVE the two blocks that were missing their own closing brace
        (Hostnames/Categories' 672px media query, .cds-status-footer
        .cds-classic-link) - it was never disabled by that bug, and is not
        "covered transitively" by the brace-balance regression test in any
        special sense beyond what every other rule in the file already gets.
        This just pins its position relative to the once-corrupted region so
        that relationship can't silently get it wrong again. The anchor is
        the Hostnames section marker rather than the `.cds-hostname-row`
        media query the dense-table rework retired.
        """
        css = self._read_static("carbon.css")
        reduced_motion_index = css.index("@media (prefers-reduced-motion: reduce)")
        corrupted_region_index = css.index("/* ---- Hostnames ---- */")
        self.assertLess(reduced_motion_index, corrupted_region_index)

    def test_carbon_css_modal_surface_scrolls_when_taller_than_viewport(self):
        """Regression pin: `.cds-modal__surface` had no
        max-height/overflow handling and `.cds-modal` centers vertically -
        with the real 23 SHARE_HOSTERS the mirror-priority modal measured
        ~1523px tall live at a 1000px viewport: header clipped above,
        Save/Cancel clipped below, nothing scrollable. The surface now caps
        its own height and becomes a flex column so only its body scrolls,
        keeping header and actions always reachable. The `100vh` cap is
        also a `100dvh` fallback pair (later declaration wins where
        supported, earlier one is the fallback) so a mobile browser's
        dynamic toolbar chrome can't hide the actions either, and the
        scrolling body contains its own overscroll so it never chains a
        scroll gesture through to the page behind the backdrop.
        """
        css = self._read_static("carbon.css")
        self.assertRegex(
            css,
            r"\.cds-modal__surface\s*\{[^}]*max-height:\s*calc\(100vh - 32px\);"
            r"\s*max-height:\s*calc\(100dvh - 32px\);"
            r"[^}]*display:\s*flex;[^}]*flex-direction:\s*column;[^}]*\}",
        )
        self.assertRegex(
            css,
            r"\.cds-modal__body\s*\{[^}]*overflow-y:\s*auto;[^}]*min-height:\s*0;"
            r"[^}]*overscroll-behavior:\s*contain;[^}]*\}",
        )
        # .cds-modal__header and .cds-modal__actions are separate rules
        # (not a combined selector) since the design's footer buttons need
        # their own flex/sizing distinct from the header's.
        self.assertRegex(
            css,
            r"\.cds-modal__header\s*\{[^}]*flex:\s*0 0 auto;",
        )
        self.assertRegex(
            css,
            r"\.cds-modal__actions\s*\{[^}]*flex:\s*0 0 auto;",
        )

    def test_carbon_css_mirror_row_move_buttons_are_scoped_for_the_light_modal_surface(
        self,
    ):
        """Regression pin: the mirror-priority modal's reorder buttons reuse
        the bare `.cds-icon-button` class, which is sized/hover-tinted for
        the dark header bar - near-invisible on the light modal surface.
        Mirrors the same `.cds-row-actions` scoped-override precedent the
        Downloads table already uses: 32px sizing, a visible hover tint,
        and the existing 672px 44px tap-target bump.

        Also pins that convention for `.cds-icon-button--sm`, the third
        32px override in the file. Every 32px override MUST ship a paired
        672px 44px bump, because all of them sit after the generic
        `@media (max-width: 672px) .cds-icon-button { 44px }` rule at equal
        specificity and would otherwise win on source order alone.
        `--sm` matters most: it is the only unscoped one, so a missing bump
        shrinks the tap target of every `icon_button()` on every page.
        """
        css = self._read_static("carbon.css")
        self.assertRegex(
            css,
            r"\.cds-mirror-row__move \.cds-icon-button\s*\{"
            r"[^}]*min-width:\s*32px;[^}]*min-height:\s*32px;[^}]*\}",
        )
        self.assertRegex(
            css,
            r"\.cds-mirror-row__move \.cds-icon-button:hover\s*\{"
            r"[^}]*background:\s*var\(--cds-hover\);[^}]*\}",
        )
        self.assertRegex(
            css,
            r"@media \(max-width:\s*672px\)[\s\S]*?"
            r"\.cds-mirror-row__move \.cds-icon-button\s*\{"
            r"[^}]*min-width:\s*44px;[^}]*min-height:\s*44px;[^}]*\}",
        )
        self.assertRegex(
            css,
            r"\.cds-icon-button--sm\s*\{"
            r"[^}]*min-width:\s*32px;[^}]*min-height:\s*32px;[^}]*\}",
        )
        self.assertRegex(
            css,
            r"@media \(max-width:\s*672px\)[\s\S]*?"
            r"\.cds-icon-button--sm\s*\{"
            r"[^}]*min-width:\s*44px;[^}]*min-height:\s*44px;[^}]*\}",
        )

    def test_carbon_css_compact_button_keeps_a_44px_tap_target(self):
        """`.cds-btn--compact` is the fourth 32px override in the file,
        alongside `.cds-mirror-row__move .cds-icon-button`,
        `.cds-icon-button--sm`, and `.cds-row-actions .cds-icon-button`
        pinned above - it sits after the generic `@media (max-width: 672px)
        .cds-btn { 44px }` rule at equal specificity and would otherwise
        win on source order alone. It matters as much as `--sm`: it is
        unscoped, so a missing bump shrinks the tap target of every
        buildTextActionButton() caller - currently the deferred table's
        Check and Remove buttons, two per row.
        """
        css = self._read_static("carbon.css")
        self.assertRegex(
            css,
            r"\.cds-btn--compact\s*\{[^}]*min-height:\s*32px;[^}]*\}",
        )
        self.assertRegex(
            css,
            r"@media \(max-width:\s*672px\)[\s\S]*?"
            r"\.cds-btn--compact\s*\{[^}]*min-height:\s*44px;[^}]*\}",
        )

    def test_carbon_css_nav_footer_link_keeps_a_44px_tap_target(self):
        """The nav footer's classic switch used to be `.cds-classic-link`,
        which carries its own `min-height: 44px`. As a
        `.cds-nav__link--footer` it inherits `.cds-nav__link`'s 32px (40px
        in the compact overlay) instead, so it needs an explicit bump at
        the 672px breakpoint to keep a touch-sized target. The file's
        generic `min-height: 44px` assertions elsewhere cannot catch this -
        they match other rules and stay green while this one regresses.
        """
        css = self._read_static("carbon.css")
        self.assertRegex(
            css,
            r"@media \(max-width:\s*672px\)[\s\S]*?"
            r"\.cds-nav__link--footer\s*\{[^}]*min-height:\s*44px;[^}]*\}",
        )

    GLYPH_AS_ICON_PATTERN = (
        "[\u2190-\u2191\u2193-\u21ff\u2300-\u23ff\u25a0-\u25ff\u2600-\u27bf"
        "\u27f0-\u27ff\u2900-\u297f\u2b00-\u2bff\U0001f000-\U0001faff]"
    )

    def test_carbon_js_uses_no_glyph_as_an_icon(self):
        """Carbon markup draws its icons as real SVG, never as a character.
        The renderer-owned guard (`test_renderer_owned_emoji_guard` above)
        only scans server-rendered HTML - it never sees JS-built DOM, which
        is exactly how the mirror modal's bare star and its glyph arrows -
        a Recommended marker and two reorder buttons - shipped. Scans the
        WHOLE file, not just one function, so the next one cannot land
        either. Range covers Arrows (2190-21FF), Misc Technical (2300-23FF,
        e.g. hourglass/keyboard glyphs), Geometric Shapes (25A0-25FF, e.g.
        solid triangles), Misc Symbols (2600-26FF), Dingbats (2700-27BF),
        Supplemental Arrows-A (27F0-27FF), Supplemental Arrows-B
        (2900-297F), Misc Symbols and Arrows (2B00-2BFF, covers the star
        that shipped here), and the Mahjong/Domino/emoji supplementary
        planes (1F000-1FAFF).

        U+2192 RIGHTWARDS ARROW is the one exception, because it is
        typography inside a link label rather than a control drawn as a
        character: it names no action of its own, it is never an element's
        only content, and dropping it would leave the label complete. The
        Python renderers already ship that exact form ("Solve CAPTCHA -> "
        on the dashboard CAPTCHA banner and "Statistics -> " on the all-time
        tile, both in `quasarr/api/carbon.py`), so the JS-built inline link
        beside a queue release would otherwise be the only one spelling the
        same affordance differently. Every other arrow in the block stays
        forbidden, so a reorder control can still never be a character.
        """
        js = self._read_static("carbon.js")
        match = re.search(self.GLYPH_AS_ICON_PATTERN, js)
        self.assertIsNone(
            match,
            "glyph used as an icon found in carbon.js: "
            f"{match.group()!r} at offset {match.start()}"
            if match
            else "",
        )

    def test_carbon_js_glyph_guard_permits_only_the_one_typographic_arrow(self):
        """The carve-out above is exactly one codepoint wide, and the label
        it exists for really is what carbon.js ships.
        """
        for codepoint in (0x2190, 0x2191, 0x2193, 0x21D2, 0x27A1, 0x2B50, 0x2705):
            with self.subTest(codepoint=hex(codepoint)):
                self.assertIsNotNone(
                    re.search(self.GLYPH_AS_ICON_PATTERN, chr(codepoint))
                )
        self.assertIsNone(re.search(self.GLYPH_AS_ICON_PATTERN, chr(0x2192)))
        js = self._read_static("carbon.js")
        self.assertIn("'Solve CAPTCHA " + chr(0x2192) + "'", js)


def _javascript_function_body(source, name):
    """Brace-matching JS function body extractor (house standard - see
    tests/test_carbon_setup_pages.py, tests/test_carbon_downloads.py). Not
    shared across files by convention; this copy is local to this file.
    """
    for prefix in ("async function ", "function "):
        marker = f"{prefix}{name}("
        if marker in source:
            start = source.index(marker)
            break
    else:
        raise AssertionError(f"No function named {name} found")
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : index]
    raise AssertionError(f"unbalanced braces in {name}")


class ModalFocusChainTests(unittest.TestCase):
    """Chained modals - a button
    rendered inside an already-open modal's body opens a second, replacement
    dialog (e.g. Hostnames' status detail modal -> its "flaresolverr-next
    required"/"Require login again?" follow-up modals) - must restore focus
    to the ORIGINAL page-level opener when the chain finally closes, not to
    a node that belonged to the first modal's now-replaced body.
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = root.joinpath("quasarr", "static", "carbon.js").read_text(
            encoding="utf-8"
        )

    def test_show_modal_only_captures_opener_while_modal_is_hidden(self):
        body = _javascript_function_body(self.js, "showModal")
        # The capture must be guarded by modalElement.hidden - a chained
        # showModal() call (fired while the first modal is still visible,
        # i.e. modalElement.hidden is false) must skip the reassignment so
        # the ORIGINAL opener captured by the chain's first call survives.
        self.assertRegex(
            body,
            r"if\s*\(\s*modalElement\.hidden\s*\)\s*\{\s*"
            r"lastFocusedElement\s*=\s*document\.activeElement;\s*\}",
        )

    def test_show_modal_has_exactly_one_capture_site_guarded_by_hidden_check(self):
        body = _javascript_function_body(self.js, "showModal")
        # An unconditional (unguarded) reassignment is exactly the earlier
        # regression: there must be exactly one assignment site, and the
        # nearest preceding `if (` before it must be the modalElement.hidden
        # guard proven above - never a bare statement reachable regardless
        # of whether a modal is already open.
        assignment = "lastFocusedElement = document.activeElement;"
        self.assertEqual(body.count(assignment), 1)
        assignment_index = body.index(assignment)
        guard_index = body.rindex("if (", 0, assignment_index)
        guard_condition = body[guard_index : body.index(")", guard_index) + 1]
        self.assertEqual(guard_condition, "if (modalElement.hidden)")

    def test_close_modal_still_focuses_and_clears_last_focused(self):
        # closeModalInternal()'s own restore-and-clear behavior is untouched
        # by this fix - only WHEN lastFocusedElement gets set changed.
        body = _javascript_function_body(self.js, "closeModalInternal")
        self.assertIn(
            "if (lastFocusedElement && typeof lastFocusedElement.focus === 'function') {",
            body,
        )
        self.assertIn("lastFocusedElement.focus();", body)
        self.assertIn("lastFocusedElement = null;", body)


class SetupHostnameCredentialsPanelWidthTests(unittest.TestCase):
    """The setup Hostnames page's inline credentials panel must span the
    full width of the rows container, not sit inside a row.

    This is the same guarantee the retired `.cds-hostname-row` +
    `flex-basis: 100%` CSS pin existed for - measured live at the time:
    the panel's own two `<input>` fields were ~154px wide at a 1280px
    viewport while the panel was a fourth child of the row, and 726.86px
    once it got its own full-width line. The dense-table rework achieved
    the same result differently, by emitting the panel as a block sibling
    of `.cds-hostname-table__row` inside `.cds-hostname-table`, so the
    guard now asserts that structure against the rendered HTML instead of
    a CSS declaration that no longer exists. `.cds-hostname-table__row` is
    a four-column grid (`56px 48px minmax(200px,1.2fr)
    minmax(260px,1.6fr)`), so a panel nested inside one would be squeezed
    into a single track exactly as before.
    """

    # Elements that never receive an end tag, so they must not be pushed
    # onto the ancestor stack (`<input>` in particular is all over these
    # rows).
    VOID_ELEMENTS = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
    )

    class _AncestorCollector(HTMLParser):
        """Records the open-element class stack above every element
        carrying the class it was asked about.
        """

        def __init__(self, wanted_class, void_elements):
            super().__init__(convert_charrefs=True)
            self.wanted_class = wanted_class
            self.void_elements = void_elements
            self.stack = []
            self.matches = []

        def handle_starttag(self, tag, attrs):
            classes = frozenset(
                dict(attrs).get("class", "").split() if dict(attrs).get("class") else []
            )
            if self.wanted_class in classes:
                self.matches.append(list(self.stack))
            if tag not in self.void_elements:
                self.stack.append(classes)

        def handle_startendtag(self, tag, attrs):
            classes = frozenset(
                dict(attrs).get("class", "").split() if dict(attrs).get("class") else []
            )
            if self.wanted_class in classes:
                self.matches.append(list(self.stack))

        def handle_endtag(self, tag):
            if self.stack:
                self.stack.pop()

    def _render_setup_hostnames(self):
        from types import SimpleNamespace

        from quasarr.storage.setup import carbon as setup_carbon

        row = {
            "id": "gb",
            "label": "GB",
            "hostname": "gb-fixture.invalid",
            "status": "skipped",
            "status_emoji": "",
            "status_title": "Login was skipped",
            "details": "",
            "timestamp": "",
            "operation": "",
            "missing_arr_client": None,
            "supports_login": True,
            "credential_section": "GB",
            "skip_login": True,
            "language": "de",
            "categories": [],
            "invite_only": False,
            "requires_login": True,
            "requires_account": False,
            "requires_flaresolverr": False,
        }

        class _Section:
            def get(self, _key):
                return ""

        class _Table:
            def retrieve(self, _key):
                return None

        with (
            mock.patch.object(setup_carbon, "build_hostname_rows", return_value=[row]),
            mock.patch.object(setup_carbon, "Config", lambda _section: _Section()),
            mock.patch.object(setup_carbon, "DataBase", lambda _table: _Table()),
        ):
            return setup_carbon.render_setup_hostnames(
                SimpleNamespace(
                    values={
                        "sites": ["GB"],
                        "external_address": "http://setup.invalid:8080",
                        "port": 8080,
                    }
                )
            )

    def test_credentials_panel_is_a_block_sibling_of_the_row(self):
        html = self._render_setup_hostnames()
        parser = self._AncestorCollector("cds-hostname-credentials", self.VOID_ELEMENTS)
        parser.feed(html)
        parser.close()

        self.assertEqual(
            len(parser.matches),
            1,
            "expected exactly one credentials panel for the one fixture row",
        )
        ancestors = parser.matches[0]
        for ancestor_classes in ancestors:
            self.assertNotIn(
                "cds-hostname-table__row",
                ancestor_classes,
                "credentials panel is nested inside a row - its fields would "
                "be squeezed into one grid track instead of taking the full "
                "row width",
            )
        self.assertIn(
            "cds-hostname-table",
            ancestors[-1],
            "credentials panel must be a direct child of the rows container, "
            """so normal block layout gives it the full width""",
        )

    def test_the_row_itself_really_is_the_narrow_grid_this_guards_against(self):
        """Without this the sibling assertion above could quietly stop
        meaning anything: it only protects a real width if the row it must
        stay out of is still a multi-column grid.
        """
        css = (
            Path(__file__)
            .resolve()
            .parent.parent.joinpath("quasarr", "static", "carbon.css")
            .read_text(encoding="utf-8")
        )
        self.assertRegex(
            css,
            r"\.cds-hostname-table__head,\s*\n\.cds-hostname-table__row\s*\{"
            r"[^}]*display:\s*grid;[^}]*grid-template-columns:",
        )


class ThemeSwitcherTests(unittest.TestCase):
    """Settings' Light | Dark | System content switcher drives the shell's
    own theme functions. "System" is not a third stored theme: it clears
    the stored preference and falls back to the OS media query, exactly
    like a first visit does.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = (
            Path(__file__)
            .resolve()
            .parent.parent.joinpath("quasarr", "static", "carbon.js")
            .read_text(encoding="utf-8")
        )

    def test_change_handler_reacts_to_the_switcher_not_a_select(self):
        body = _javascript_function_body(self.js, "onChange")
        self.assertIn("'theme-switch'", body)
        self.assertIn("applyThemePreference(", body)
        self.assertNotIn("theme-select", self.js)

    def test_system_preference_clears_storage_and_reads_the_media_query(self):
        body = _javascript_function_body(self.js, "applyThemePreference")
        self.assertIn("'system'", body)
        self.assertIn("localStorage.removeItem(THEME_KEY)", body)
        self.assertIn("systemTheme()", body)
        self.assertIn("setTheme(preference)", body)

        system_body = _javascript_function_body(self.js, "systemTheme")
        self.assertIn("prefers-color-scheme: dark", system_body)

    def test_switcher_selection_is_restored_from_the_stored_preference(self):
        body = _javascript_function_body(self.js, "updateThemeSwitcher")
        self.assertIn('input[name="theme"]', body)
        self.assertIn("input.checked = input.value === preference", body)

        stored_body = _javascript_function_body(self.js, "storedThemePreference")
        self.assertIn("localStorage.getItem(THEME_KEY)", stored_body)
        self.assertIn("'system'", stored_body)

    def test_header_toggle_keeps_the_switcher_in_sync(self):
        # The header's light/dark toggle stores an explicit theme, which
        # must move the switcher off "System" rather than leave it lying.
        body = _javascript_function_body(self.js, "setTheme")
        self.assertIn("updateThemeSwitcher();", body)


class ModalAnatomyOptionsTests(unittest.TestCase):
    """The design's modal anatomy adds an eyebrow above the title and a
    wide-surface variant for content-heavy dialogs (e.g. the mirrors
    editor); showModal() gains a fourth `options` parameter carrying both.
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = root.joinpath("quasarr", "static", "carbon.js").read_text(
            encoding="utf-8"
        )

    def test_show_modal_reads_eyebrow_and_wide_from_options(self):
        body = _javascript_function_body(self.js, "showModal")
        self.assertIn("options && options.eyebrow", body)
        self.assertIn("cds-modal__surface--wide", body)


class NavOverlayFocusTrapTests(unittest.TestCase):
    """Below the 1056px breakpoint the side nav becomes a
    hamburger-controlled overlay (a "modal nav overlay"),
    but only the CSS/backdrop visually hid the rest of the page - Tab was
    never trapped, so a keyboard user opening the nav could Tab straight
    past its own last link into the page content sitting behind the
    backdrop. Confirmed live via Playwright before this fix: 9 Tab presses
    from the nav's close button landed on "Import from URL" - a button in
    the underlying Hostnames page, not `.cds-side-nav`.
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = root.joinpath("quasarr", "static", "carbon.js").read_text(
            encoding="utf-8"
        )

    def test_trap_nav_focus_function_shipped_with_open_and_viewport_guards(self):
        body = _javascript_function_body(self.js, "trapNavFocus")
        self.assertIn("event.key !== 'Tab'", body)
        self.assertIn("compactNavIsActive()", body)
        self.assertIn("navElement.classList.contains('is-open')", body)
        self.assertIn("getFocusable(navElement)", body)

    def test_trap_nav_focus_wraps_both_directions_like_the_modal_trap(self):
        body = _javascript_function_body(self.js, "trapNavFocus")
        self.assertIn("event.shiftKey && document.activeElement === first", body)
        self.assertIn("last.focus();", body)
        self.assertIn("document.activeElement === last", body)
        self.assertIn("first.focus();", body)

    def test_trap_nav_focus_is_wired_into_the_shared_keydown_handler(self):
        body = _javascript_function_body(self.js, "onKeydown")
        self.assertIn("trapModalFocus(event);", body)
        self.assertIn("trapNavFocus(event);", body)
        # Both traps run unconditionally after the Escape branches - each
        # guards its own precondition (modal hidden / nav not open), so
        # calling both is safe regardless of which surface is active.
        self.assertLess(
            body.index("trapModalFocus(event);"), body.index("trapNavFocus(event);")
        )


class NavOverlayInertContentTests(unittest.TestCase):
    """trapNavFocus() only stops the Tab
    key - it does nothing for a screen reader's virtual/browse-mode cursor,
    which can still reach page content sitting behind the (visual-only)
    backdrop because that content stays in the accessibility tree.
    showModal()/closeModalInternal() already solve this for the modal via
    shellElement.inert + aria-hidden; the nav overlay needs the same
    treatment, scoped narrower: `.cds-shell` wraps the nav ITSELF too, so
    reusing shellElement.inert for the nav would make the nav unreachable
    while it's the thing meant to stay open. `setNavContentInert()` instead
    targets exactly what the backdrop visually covers (main content, plus
    the skip link, which would otherwise offer a dead-end jump into a
    now-inert target) - the header stays untouched, since the backdrop's
    own `inset: 48px 0 0 0` never covers it and it stays genuinely visible
    and interactive.
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = root.joinpath("quasarr", "static", "carbon.js").read_text(
            encoding="utf-8"
        )

    def test_set_nav_content_inert_targets_skip_link_and_main_only(self):
        # "updateThemeControl();" is also called (as a statement, not a
        # definition) inside setTheme() earlier in the file, so the slice
        # end must be searched for AFTER the DOMContentLoaded start point,
        # not from offset 0 - an unqualified self.js.index() would find
        # that earlier call instead and produce an empty (start > end)
        # slice, silently passing every assertion below for the wrong
        # reason.
        dom_ready_start = self.js.index("document.addEventListener('DOMContentLoaded'")
        init_body = self.js[
            dom_ready_start : self.js.index("updateThemeControl();", dom_ready_start)
        ]
        self.assertIn("navContentInertTargets = [", init_body)
        self.assertIn("document.querySelector('.cds-skip-link')", init_body)
        self.assertIn("document.getElementById('main-content')", init_body)
        # The nav element and header must never be added to this list - the
        # nav is the thing staying open and reachable, and the header stays
        # visually interactive (the backdrop never covers it).
        self.assertNotIn(
            "navElement", init_body[init_body.index("navContentInertTargets") :]
        )

    def test_set_nav_content_inert_toggles_inert_and_aria_hidden_together(self):
        body = _javascript_function_body(self.js, "setNavContentInert")
        self.assertIn("element.inert = isOpen;", body)
        self.assertIn("element.setAttribute('aria-hidden', 'true');", body)
        self.assertIn("element.removeAttribute('aria-hidden');", body)
        self.assertIn("navContentInertTargets.forEach(", body)

    def test_set_nav_open_calls_set_nav_content_inert(self):
        body = _javascript_function_body(self.js, "setNavOpen")
        self.assertIn("setNavContentInert(shouldOpen);", body)

    def test_sync_nav_viewport_clears_inert_state_on_desktop_transition(self):
        # setNavOpen() is NOT called when resizing back to desktop while the
        # nav happened to be open (syncNavViewport() resets the nav's own
        # state directly instead) - without its own explicit call here, a
        # stale inert/aria-hidden flag on main-content would survive a
        # resize away from the compact breakpoint.
        body = _javascript_function_body(self.js, "syncNavViewport")
        self.assertIn("setNavContentInert(false);", body)

    def test_nav_element_itself_is_never_in_its_own_inert_target_list(self):
        # Regression guard for the exact failure mode the review named:
        # mirroring shellElement.inert verbatim (which wraps the nav too)
        # would make the open nav unreachable. navContentInertTargets must
        # never resolve to navElement/navBackdrop.
        body = _javascript_function_body(self.js, "setNavContentInert")
        self.assertNotIn("navElement", body)
        self.assertNotIn("navBackdrop", body)


if __name__ == "__main__":
    unittest.main()
