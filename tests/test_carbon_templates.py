# -*- coding: utf-8 -*-

import dataclasses
import importlib
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock


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
        via computed styles (`.cds-toolbar` margin-top read 0px instead of
        16px, `.cds-field--search` max-width read "none" instead of
        "320px") before this fix.
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
        self.assertNotRegex(css, r"\.cds-tile[^\{]*\.cds-tile")
        self.assertNotIn("http://", css)
        self.assertNotIn("https://", css)

    def test_carbon_css_kpi_row_layout_contract(self):
        """The statistics KPI row: a 4-column grid at
        desktop width, matching the file's `repeat(4, minmax(0, 1fr))`/16px
        gap conventions, collapsing to 2 columns at the existing 1056px
        breakpoint and 1 column at the existing 672px breakpoint.
        """
        css = self._read_static("carbon.css")

        self.assertRegex(
            css,
            r"\.cds-kpi-row\s*\{[^}]*display:\s*grid;"
            r"[^}]*grid-template-columns:\s*repeat\(4,\s*minmax\(0,\s*1fr\)\);"
            r"[^}]*gap:\s*16px;",
        )
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

    def test_carbon_css_status_card_wide_modifier_and_hostname_row_wrap_contract(self):
        """The wide setup-page card cap,
        and the fix that makes it actually usable - .cds-hostname-row (a
        flex row) must wrap so its inline .cds-hostname-credentials Details
        panel (a flex sibling of status/body/actions on the setup
        Hostnames page, unlike the main Hostnames page's modal-based
        equivalent) gets its own full-width line via flex-basis:100%,
        instead of being squeezed to ~150-190px wide as a fourth column.
        Confirmed live: before this fix the credentials panel's own two
        <input> fields measured ~154px wide at 1280px viewport; after,
        726.86px (the full row width).
        """
        css = self._read_static("carbon.css")

        self.assertRegex(
            css,
            r"\.cds-status-card--wide\s*\{[^}]*max-width:\s*760px;",
        )
        self.assertRegex(
            css,
            r"\.cds-hostname-row\s*\{[^}]*display:\s*flex;[^}]*flex-wrap:\s*wrap;",
        )
        self.assertRegex(
            css,
            r"\.cds-hostname-credentials\s*\{[^}]*flex-basis:\s*100%;",
        )

    def test_reduced_motion_block_was_never_inside_the_corrupted_region(self):
        """Corrects an earlier inaccuracy: prefers-reduced-motion sits
        well ABOVE the two blocks that were missing their own closing brace
        (Hostnames/Categories' 672px media query, .cds-status-footer
        .cds-classic-link) - it was never disabled by that bug, and is not
        "covered transitively" by the brace-balance regression test in any
        special sense beyond what every other rule in the file already gets.
        This just pins its position relative to the once-corrupted region so
        that relationship can't silently get it wrong again.
        """
        css = self._read_static("carbon.css")
        reduced_motion_index = css.index("@media (prefers-reduced-motion: reduce)")
        corrupted_region_index = css.index(
            "@media (max-width: 672px) {\n\t.cds-hostname-row,"
        )
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
        self.assertRegex(
            css,
            r"\.cds-modal__header,\s*\.cds-modal__actions\s*\{[^}]*flex:\s*0 0 auto;",
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

    def test_carbon_js_has_no_emoji_dingbat_or_arrow_glyphs_anywhere(self):
        """Carbon markup has no emoji. The renderer-owned
        guard (`test_renderer_owned_emoji_guard` above) only scans
        server-rendered HTML - it never sees JS-built DOM, which is exactly
        how the mirror modal's bare star emoji and glyph arrows escaped
        review. Scans the WHOLE file, not just one function, so the next
        one cannot land either. Range covers Arrows (2190-21FF), Misc
        Technical (2300-23FF, e.g. hourglass/keyboard glyphs), Geometric
        Shapes (25A0-25FF, e.g. solid triangles), Misc Symbols (2600-26FF),
        Dingbats (2700-27BF), Supplemental Arrows-A (27F0-27FF),
        Supplemental Arrows-B (2900-297F), Misc Symbols and Arrows
        (2B00-2BFF, covers the star that shipped here), and the
        Mahjong/Domino/emoji supplementary planes (1F000-1FAFF).
        """
        js = self._read_static("carbon.js")
        match = re.search(
            "[\u2190-\u21ff\u2300-\u23ff\u25a0-\u25ff\u2600-\u27bf"
            "\u27f0-\u27ff\u2900-\u297f\u2b00-\u2bff\U0001f000-\U0001faff]",
            js,
        )
        self.assertIsNone(
            match,
            "emoji/dingbat/arrow glyph found in carbon.js: "
            f"{match.group()!r} at offset {match.start()}"
            if match
            else "",
        )


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
