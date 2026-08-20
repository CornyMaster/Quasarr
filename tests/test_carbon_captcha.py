# -*- coding: utf-8 -*-

"""Contracts for the Carbon CAPTCHA provider pages.

Pins ``quasarr.api.captcha.carbon.render_captcha(shared_state, provider)``
across all six protected providers (hide, junkies, he, keeplinks, tolink,
filecrypt) driven through the real ``/captcha/<provider>`` routes with
``?ui=carbon``: exact CTA wording, the quick-transfer URL parameter
contract, the base64 package-selector payload shape, the manual
``/captcha/bypass-submit`` form contract, the failed-attempts counter
storage key, ``check_package_exists()`` 404 propagation, and the structural/
privacy guards ``render_carbon_html`` enforces. A second class pins the
``carbon.js``/``carbon.css`` additions shipped for this page: the three
window-scoped attempt-counter compatibility functions, tutorial timing
sourced from server-rendered content (never a URL literal in carbon.js),
package-selector navigation, and the manual-submit toggle/submit wiring.
"""

import json
import sys
import unittest
from base64 import urlsafe_b64encode
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from bottle import Bottle

from quasarr.api import captcha as captcha_init
from quasarr.api.captcha import carbon as captcha_carbon
from quasarr.api.captcha import setup_captcha_routes
from quasarr.providers import shared_state

STATIC_ROOT = Path(__file__).resolve().parent.parent / "quasarr" / "static"

PROVIDER_CTAS = {
    "filecrypt": "Open FileCrypt &amp; Get Download Links",
    "hide": "Open Hide &amp; Get Download Links",
    "junkies": "Open Junkies &amp; Get Download Links",
    "he": "Open HE &amp; Get Download Links",
    "keeplinks": "Open KeepLinks &amp; Get Download Links",
    "tolink": "Open ToLink &amp; Get Download Links",
}


class FakeProtectedDb:
    def __init__(self, packages):
        self.packages = packages

    def retrieve_all_titles(self):
        return list(self.packages)

    def retrieve(self, package_id):
        for pkg_id, payload in self.packages:
            if pkg_id == package_id:
                return payload
        return None


def build_package(pkg_id, url, mirror, password="", title=None, original_url=None):
    return (
        pkg_id,
        json.dumps(
            {
                "title": title or "Synthetic.Release.2024.German.1080p.WEB.H264-GRP",
                "links": [[url, mirror]],
                "password": password,
                "original_url": original_url,
            }
        ),
    )


def encode_data_param(package, **overrides):
    pkg_id, payload = package
    data = json.loads(payload)
    page_payload = {
        "package_id": pkg_id,
        "title": data["title"],
        "password": data["password"],
        "mirror": None,
        "links": data["links"],
        "original_url": data.get("original_url"),
    }
    page_payload.update(overrides)
    return quote(urlsafe_b64encode(json.dumps(page_payload).encode()).decode())


class CaptchaCarbonRenderTests(unittest.TestCase):
    def _request(self, app, path, query=""):
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "SERVER_NAME": "localhost",
            "SERVER_PORT": "8080",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "HTTP_HOST": "localhost:8080",
            "wsgi.url_scheme": "http",
            "wsgi.input": BytesIO(b""),
            "wsgi.errors": sys.stderr,
        }
        captured = {}

        def start_response(status, headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = dict(headers)

        body = b"".join(app(environ, start_response))
        return captured["status"], captured["headers"], body.decode("utf-8", "replace")

    def _serve(self, packages, hostnames=None, junkies_creds=None):
        app = Bottle()
        hostnames = hostnames or {"he": "source.invalid"}
        fake_db = FakeProtectedDb(packages)
        fake_values = {
            "device": object(),
            "config": lambda section: hostnames if section == "Hostnames" else {},
            "database": lambda table: (
                fake_db if table == "protected" else FakeProtectedDb([])
            ),
        }

        def fake_config(section):
            class _Section:
                def get(self, key):
                    if section == "JUNKIES" and junkies_creds:
                        return junkies_creds.get(key)
                    return None

            return _Section()

        stack = ExitStack()
        stack.enter_context(
            patch.multiple(
                shared_state, values=fake_values, get_db=lambda name: fake_db
            )
        )
        stack.enter_context(
            patch(
                "quasarr.providers.page_dispatch.carbon_assets_available",
                return_value=True,
            )
        )
        stack.enter_context(
            patch.object(captcha_carbon, "Config", side_effect=fake_config)
        )
        self.addCleanup(stack.close)
        setup_captcha_routes(app)
        return app

    def _get(self, app, provider, query):
        return self._request(app, f"/captcha/{provider}", f"ui=carbon&{query}")

    def test_carbon_route_sets_csp_and_200(self):
        package = build_package(
            "pkg-fc",
            "https://filecrypt.example.invalid/Container/abc.html",
            "filecrypt",
        )
        app = self._serve([package])

        status, headers, body = self._get(
            app, "filecrypt", f"data={encode_data_param(package)}"
        )

        self.assertEqual("200 OK", status)
        self.assertIn("Content-Security-Policy", headers)
        self.assertIn("cds-shell", body)

    def test_every_provider_renders_exact_cta_and_userscript_route(self):
        for provider, cta in PROVIDER_CTAS.items():
            with self.subTest(provider=provider):
                package = build_package(
                    f"pkg-{provider}",
                    f"https://{provider}.example.invalid/c/abc.html",
                    provider,
                )
                app = self._serve([package])

                status, _, body = self._get(
                    app, provider, f"data={encode_data_param(package)}"
                )

                self.assertEqual("200 OK", status)
                self.assertIn(cta, body)
                self.assertIn(f"/captcha/{provider}.user.js", body)

    def test_hide_provider_renders_the_same_page_shape_as_the_others(self):
        """Hide auto-decrypts upstream and never gets a bespoke Carbon page:
        it must go through the identical unified renderer, not a special
        case that skips the userscript/manual-submit sections."""
        package = build_package(
            "pkg-hide", "https://hide.example.invalid/c/abc.html", "hide"
        )
        app = self._serve([package])

        status, _, body = self._get(app, "hide", f"data={encode_data_param(package)}")

        self.assertEqual("200 OK", status)
        self.assertIn("Open Hide &amp; Get Download Links", body)
        self.assertIn('data-action="captcha-manual-submit"', body)
        self.assertIn('action="/captcha/bypass-submit"', body)

    def test_quick_transfer_url_parameters_are_preserved(self):
        package = build_package(
            "pkg-fc",
            "https://filecrypt.example.invalid/Container/abc.html",
            "filecrypt",
        )
        app = self._serve([package])

        _, _, body = self._get(app, "filecrypt", f"data={encode_data_param(package)}")

        self.assertIn("abc.html?transfer_url=", body)
        self.assertIn("pkg_id=pkg-fc", body)
        self.assertIn("pkg_title=", body)
        self.assertIn("pkg_pass=", body)

    def test_quick_transfer_appends_to_existing_query_string(self):
        package = build_package(
            "pkg-fc",
            "https://filecrypt.example.invalid/Container/abc.html?mirror=2",
            "filecrypt",
        )
        app = self._serve([package])

        _, _, body = self._get(app, "filecrypt", f"data={encode_data_param(package)}")

        self.assertIn("abc.html?mirror=2&amp;transfer_url=", body)
        self.assertNotIn("mirror=2?transfer_url=", body)

    def test_junkies_credentials_are_appended_when_configured(self):
        package = build_package(
            "pkg-jk", "https://junkies.example.invalid/c/abc.html", "junkies"
        )
        app = self._serve(
            [package],
            junkies_creds={"user": "synthetic-user", "password": "synthetic-pass"},
        )

        _, _, body = self._get(app, "junkies", f"data={encode_data_param(package)}")

        self.assertIn("jk_user=synthetic-user", body)
        self.assertIn("jk_pass=synthetic-pass", body)

    def test_junkies_credentials_omitted_when_not_configured(self):
        package = build_package(
            "pkg-jk", "https://junkies.example.invalid/c/abc.html", "junkies"
        )
        app = self._serve([package])

        _, _, body = self._get(app, "junkies", f"data={encode_data_param(package)}")

        self.assertNotIn("jk_user=", body)
        self.assertNotIn("jk_pass=", body)

    def test_he_page_accepts_missing_password(self):
        package = build_package(
            "pkg-he", "https://he.example.invalid/c/abc.html", "he", password=""
        )
        app = self._serve([package])
        data = encode_data_param(package, password=None)

        status, _, body = self._get(app, "he", f"data={data}")

        self.assertEqual("200 OK", status)
        self.assertIn("Open HE &amp; Get Download Links", body)

    def test_manual_submit_form_contract_is_preserved(self):
        package = build_package(
            "pkg-fc",
            "https://filecrypt.example.invalid/Container/abc.html",
            "filecrypt",
        )
        app = self._serve([package])

        _, _, body = self._get(app, "filecrypt", f"data={encode_data_param(package)}")

        self.assertIn(
            '<form action="/captcha/bypass-submit" method="post" '
            'enctype="multipart/form-data" data-action="captcha-manual-submit">',
            body,
        )
        self.assertIn('name="package_id" value="pkg-fc"', body)
        self.assertIn('name="title"', body)
        self.assertIn('name="password"', body)
        self.assertIn('name="links"', body)

    def test_failed_attempts_warning_is_hidden_with_delete_action(self):
        package = build_package(
            "pkg-fc",
            "https://filecrypt.example.invalid/Container/abc.html",
            "filecrypt",
        )
        app = self._serve([package])

        _, _, body = self._get(app, "filecrypt", f"data={encode_data_param(package)}")

        self.assertIn('<div id="failed-attempts-warning" hidden>', body)
        self.assertIn("/captcha/delete/pkg-fc", body)

    def test_reset_tutorial_button_is_hidden_on_first_visit_render(self):
        """Regression pin: the server must still emit `hidden`
        on the Reset Setup Guide button on first render - combined with
        the generic `[hidden]` CSS rule (see
        CaptchaCarbonJsCssStructureTests.test_generic_hidden_attribute_rule_forces_display_none),
        this is what actually keeps it invisible until carbon.js reveals
        it. Neither half alone proves the button is hidden on first
        paint; a source-text test cannot see rendering, so this pins the
        markup half of the contract.
        """
        package = build_package(
            "pkg-fc",
            "https://filecrypt.example.invalid/Container/abc.html",
            "filecrypt",
        )
        app = self._serve([package])

        _, _, body = self._get(app, "filecrypt", f"data={encode_data_param(package)}")

        self.assertIn(
            'data-action="captcha-reset-tutorial" '
            'data-storage-key="hideFileCryptSetupInstructions" hidden>',
            body,
        )

    def test_source_button_present_only_with_original_url(self):
        with_source = build_package(
            "pkg-fc",
            "https://filecrypt.example.invalid/Container/abc.html",
            "filecrypt",
            original_url="https://source.invalid/release/synthetic",
        )
        without_source = build_package(
            "pkg-fc2",
            "https://filecrypt.example.invalid/Container/def.html",
            "filecrypt",
        )

        app = self._serve([with_source])
        _, _, body_with = self._get(
            app, "filecrypt", f"data={encode_data_param(with_source)}"
        )
        self.assertIn('data-action="captcha-open-source"', body_with)

        app2 = self._serve([without_source])
        _, _, body_without = self._get(
            app2, "filecrypt", f"data={encode_data_param(without_source)}"
        )
        self.assertNotIn('data-action="captcha-open-source"', body_without)

    def test_package_selector_present_only_with_multiple_packages(self):
        fc = build_package(
            "pkg-fc",
            "https://filecrypt.example.invalid/Container/abc.html",
            "filecrypt",
        )
        kl = build_package(
            "pkg-kl", "https://keeplinks.example.invalid/p/def", "keeplinks"
        )

        single_app = self._serve([fc])
        _, _, single_body = self._get(
            single_app, "filecrypt", f"data={encode_data_param(fc)}"
        )
        self.assertNotIn('data-action="captcha-package-select"', single_body)

        multi_app = self._serve([fc, kl])
        _, _, multi_body = self._get(
            multi_app, "filecrypt", f"data={encode_data_param(fc)}"
        )
        self.assertIn('data-action="captcha-package-select"', multi_body)
        self.assertIn('<option value="filecrypt|', multi_body)
        self.assertIn('<option value="keeplinks|', multi_body)

    def test_unsupported_provider_raises(self):
        with self.assertRaises(ValueError):
            captcha_carbon.render_captcha(shared_state, "unknown-provider")

    def test_decode_failure_renders_carbon_error_notification(self):
        package = build_package(
            "pkg-fc",
            "https://filecrypt.example.invalid/Container/abc.html",
            "filecrypt",
        )
        app = self._serve([package])

        status, _, body = self._get(app, "filecrypt", "data=not-valid-base64%25%25")

        self.assertEqual("200 OK", status)
        self.assertIn("CAPTCHA data error", body)
        self.assertIn("cds-notification--error", body)

    def test_missing_package_still_returns_404_via_check_package_exists(self):
        package = build_package(
            "pkg-fc",
            "https://filecrypt.example.invalid/Container/abc.html",
            "filecrypt",
        )
        app = self._serve([package])
        # Encode a payload naming a package_id that was never stored.
        stale = build_package(
            "pkg-missing",
            "https://filecrypt.example.invalid/Container/xyz.html",
            "filecrypt",
        )

        status, _, body = self._get(
            app, "filecrypt", f"data={encode_data_param(stale)}"
        )

        self.assertTrue(status.startswith("404"))
        self.assertIn("Package not found or already solved", body)

    def test_missing_package_carbon_404_renders_carbon_error_page(self):
        """The Carbon package-not-found page (``render_carbon_error_page``)
        must actually be wired into
        ``check_package_exists()``'s Carbon path, not just fall back to the
        Classic literal under a Carbon request. A real Carbon shell/
        notification and a genuine 404 status prove that (the previous
        no-op ``response.status`` assignment would have rendered as 200).
        """
        package = build_package(
            "pkg-fc",
            "https://filecrypt.example.invalid/Container/abc.html",
            "filecrypt",
        )
        app = self._serve([package])
        stale = build_package(
            "pkg-missing",
            "https://filecrypt.example.invalid/Container/xyz.html",
            "filecrypt",
        )

        status, headers, body = self._get(
            app, "filecrypt", f"data={encode_data_param(stale)}"
        )

        self.assertTrue(status.startswith("404"))
        self.assertIn("<!doctype html>", body)
        self.assertIn("cds-status-card", body)
        self.assertIn("cds-notification--error", body)
        self.assertIn("Package not found or already solved", body)
        self.assertNotIn("onclick=\"location.href='/'\"", body)
        # The closures must RETURN their HTTPResponse (not raise it
        # directly) so render_page's _apply_csp runs before the caller
        # raises it - a raised HTTPResponse skips _apply_csp entirely
        # (render_page's `except (HTTPError, HTTPResponse): raise` passes
        # it straight through unchanged).
        self.assertIn("Content-Security-Policy", headers)

    def test_missing_package_classic_404_content_byte_identical_to_original(self):
        """Byte-parity evidence: the Classic 404 inner content (the string
        handed to ``render_centered_html``) must stay exactly what
        shipped originally - independently reconstructed here and
        compared verbatim, the same golden technique
        ``test_carbon_auth_pages.py::test_render_success_classic_byte_identical_to_base``
        uses, so the comparison is not coupled to unrelated environment
        state (API key presence) baked into the surrounding page shell.
        """
        package = build_package(
            "pkg-fc",
            "https://filecrypt.example.invalid/Container/abc.html",
            "filecrypt",
        )
        app = self._serve([package])
        stale = build_package(
            "pkg-missing",
            "https://filecrypt.example.invalid/Container/xyz.html",
            "filecrypt",
        )

        images = captcha_init.images
        render_button = captcha_init.render_button
        expected_content = f'''
                <h1><img src="{images.logo}" class="logo"/>Quasarr</h1>
                <p><b>Error:</b> Package not found or already solved.</p>
                <p>
                    {render_button("Back", "secondary", {"onclick": "location.href='/'"})}
                </p>
            '''

        captured = {}
        with patch.object(
            captcha_init,
            "render_centered_html",
            side_effect=lambda content, footer_content="": (
                captured.setdefault("content", content) or "wrapped"
            ),
        ):
            status, _, _body = self._request(
                app,
                "/captcha/filecrypt",
                f"ui=classic&data={encode_data_param(stale)}",
            )

        self.assertTrue(status.startswith("404"))
        self.assertEqual(captured["content"], expected_content)

    def test_missing_package_with_empty_links_returns_404_not_500(self):
        """check_package_exists() must run before any link extraction
        (urls[0]) for keeplinks/tolink/filecrypt, exactly matching Classic's
        order for those three providers, so a stale package_id with
        absent/empty links reaches
        the intended 404 instead of an unhandled IndexError turning into
        a 500. Empty ``links`` is what actually exercises the ordering:
        with the old (post-extraction) order, ``urls[0]`` on an empty
        list raises before check_package_exists() ever runs.
        """
        package = build_package(
            "pkg-fc",
            "https://filecrypt.example.invalid/Container/abc.html",
            "filecrypt",
        )
        stale_payload = quote(
            urlsafe_b64encode(
                json.dumps(
                    {
                        "package_id": "pkg-missing",
                        "title": "Synthetic.Release.2024.German.1080p.WEB.H264-GRP",
                        "password": "",
                        "mirror": None,
                        "links": [],
                        "original_url": None,
                    }
                ).encode()
            ).decode()
        )

        for provider in ("keeplinks", "tolink", "filecrypt"):
            with self.subTest(provider=provider):
                app = self._serve([package])

                status, _, body = self._get(app, provider, f"data={stale_payload}")

                self.assertTrue(status.startswith("404"), status)
                self.assertIn("Package not found or already solved", body)

    def test_removed_server_side_filecrypt_routes_are_still_gone_under_carbon(self):
        package = build_package(
            "pkg-fc",
            "https://filecrypt.example.invalid/Container/abc.html",
            "filecrypt",
        )
        app = self._serve([package])

        for path in (
            "/captcha/cutcaptcha",
            "/captcha/circle",
            "/captcha/filecrypt/manual",
        ):
            with self.subTest(path=path):
                status, _, _ = self._request(app, path, "ui=carbon")
                self.assertTrue(status.startswith("404"))


class CaptchaCarbonJsCssStructureTests(unittest.TestCase):
    """Structural pins on the shipped carbon.js/carbon.css additions:
    tutorial timing, first-use storage, reset, selection, and manual-submit
    interactions live only in carbon.js (strict CSP; delegated data-action;
    no inline scripts), and the three attempt-counter functions stay
    window-scoped for compatibility.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = (STATIC_ROOT / "carbon.js").read_text(encoding="utf-8")
        cls.css = (STATIC_ROOT / "carbon.css").read_text(encoding="utf-8")

    def _function_body(self, name):
        for prefix in ("async function ", "function "):
            marker = f"{prefix}{name}("
            if marker in self.js:
                start = self.js.index(marker)
                break
        else:
            raise AssertionError(f"No function named {name} found in carbon.js")
        depth = 0
        i = self.js.index("{", start)
        body_start = i
        for index in range(i, len(self.js)):
            char = self.js[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return self.js[body_start : index + 1]
        raise AssertionError(f"Unbalanced braces in {name}")

    def test_three_attempt_counter_functions_stay_window_scoped(self):
        for name in (
            "window.incrementCaptchaAttempts",
            "window.getCaptchaAttempts",
            "window.clearCaptchaAttempts",
        ):
            with self.subTest(name=name):
                self.assertIn(f"{name} = function", self.js)

    def test_no_url_literals_in_carbon_js(self):
        # Structural guarantee (also pinned repo-wide by
        # test_carbon_templates.py): carbon.js never embeds an absolute
        # URL - the Tampermonkey/userscript tutorial links are rendered
        # server-side into a hidden container and only read by JS.
        self.assertNotIn("http://", self.js)
        self.assertNotIn("https://", self.js)

    def test_open_provider_reads_tutorial_content_from_dom(self):
        body = self._function_body("openProvider")
        self.assertIn("getElementById('captcha-tutorial-content')", body)
        self.assertIn("window.showModal(", body)
        self.assertIn("window.incrementCaptchaAttempts()", body)

    def test_open_provider_skips_tutorial_when_already_seen(self):
        body = self._function_body("openProvider")
        seen_index = body.index("tutorialSeen(storageKey)")
        navigate_index = body.index("window.location.href = url")
        self.assertLess(seen_index, navigate_index)

    def test_manual_submit_increments_attempts_without_blocking_submission(self):
        body = self._function_body("onCaptchaSubmit")
        self.assertIn("captcha-manual-submit", body)
        self.assertIn("window.incrementCaptchaAttempts()", body)
        self.assertNotIn("preventDefault", body)

    def test_package_select_navigates_to_provider_and_data(self):
        body = self._function_body("onCaptchaChange")
        self.assertIn("captcha-package-select", body)
        self.assertIn("'/captcha/' + parts[0] + '?data=' + parts[1]", body)

    def test_manual_submit_toggle_updates_summary_text(self):
        body = self._function_body("onCaptchaToggle")
        self.assertIn("data-manual-submit", body)
        self.assertIn("Hide Manual Submission", body)
        self.assertIn("Show Manual Submission", body)

    def test_reset_tutorial_removes_storage_and_shows_modal(self):
        body = self._function_body("resetTutorial")
        self.assertIn("removeItem(storageKey)", body)
        self.assertIn("window.showModal(", body)

    def test_captcha_css_actions_row_layout(self):
        self.assertRegex(
            self.css,
            r"\.cds-captcha-actions\s*\{[^}]*display:\s*flex;[^}]*gap:\s*12px;",
        )

    def test_generic_hidden_attribute_rule_forces_display_none(self):
        """Regression pin: an author-origin `display`
        declaration (e.g. `.cds-btn { display: inline-flex }`) beats the
        UA-origin `[hidden] { display: none }` rule regardless of
        selector specificity, so a server-rendered `hidden` `.cds-btn`
        (the Reset Setup Guide button) stayed visible on first paint
        without this. `!important` is required, not incidental - a plain
        `[hidden] { display: none }` here would lose to `.cds-btn` the
        same way the UA rule does.
        """
        self.assertRegex(
            self.css,
            r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important;",
        )


if __name__ == "__main__":
    unittest.main()
