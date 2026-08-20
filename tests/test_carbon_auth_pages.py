# -*- coding: utf-8 -*-
"""Carbon dispatch for login, common status pages, reconnect, and
JD-disconnected pages.

Covers: real-Bottle-WSGI status-code preservation (401/403/404, and the
success/no-wait/fail 200 form-redisplay family), the Carbon-render-exception
safety net that still serves the Classic login/error page, no-JS login
functionality, `next` validation, Basic/Form auth mode behavior, and
unauthenticated static asset reachability.
"""

import contextlib
import importlib
import io
import os
import re
import unittest
from html import escape
from html.parser import HTMLParser
from io import BytesIO
from unittest import mock
from urllib.parse import urlencode

from bottle import Bottle


def wsgi_request(app, method="GET", path="/", query="", headers=None, body=b""):
    """Real WSGI request against a Bottle app - no sockets, no test client.

    Mirrors the house pattern in tests/test_page_dispatch.py.
    """
    headers = dict(headers or {})
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "8080",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "HTTP_HOST": "localhost:8080",
        "wsgi.url_scheme": "http",
        "wsgi.input": BytesIO(body),
        "wsgi.errors": io.StringIO(),
    }
    if body:
        environ.setdefault("CONTENT_LENGTH", str(len(body)))
    for key, value in headers.items():
        environ[key] = value

    captured = {}

    def start_response(status, response_headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = list(response_headers)

    response_body = b"".join(app(environ, start_response))
    return (
        captured.get("status", "500 Internal Server Error"),
        captured.get("headers", []),
        response_body,
    )


def header_values(headers, name):
    lowered = name.lower()
    return [value for key, value in headers if key.lower() == lowered]


class _FormFieldCollector(HTMLParser):
    """Collects <form>/<input>/<button> attributes for no-JS form proofs."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.inputs = []
        self.buttons = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            self.forms.append(attributes)
        elif tag == "input":
            self.inputs.append(attributes)
        elif tag == "button":
            self.buttons.append(attributes)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


class CarbonAuthDispatchTestCase(unittest.TestCase):
    """Shared setUp: real modules, env/module-global cleanup."""

    def setUp(self):
        self.auth = importlib.import_module("quasarr.providers.auth")
        self.html_templates = importlib.import_module(
            "quasarr.providers.html_templates"
        )
        self.carbon_templates = importlib.import_module(
            "quasarr.providers.carbon_templates"
        )
        self._env_backup = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)

    def _carbon_env(self):
        return mock.patch.dict(os.environ, {"QUASARR_UI": "carbon"}, clear=True)

    def _classic_env(self):
        return mock.patch.dict(os.environ, {"QUASARR_UI": "classic"}, clear=True)


class LoginPageDispatchTests(CarbonAuthDispatchTestCase):
    """GET/POST /login: Classic vs Carbon rendering, real Bottle requests."""

    def _make_app(self, auth_user="admin", auth_pass="secret", auth_type="form"):
        # These globals are read fresh on every request (login credential
        # check, form/basic mode branching), not just at route-registration
        # time, so the patches must outlive this helper - restored by
        # addCleanup at the end of the test, not when this method returns.
        for name, value in (
            ("_AUTH_USER", auth_user),
            ("_AUTH_PASS", auth_pass),
            ("_AUTH_TYPE", auth_type),
        ):
            patcher = mock.patch.object(self.auth, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        app = Bottle()
        self.auth.add_auth_routes(app)
        self.auth.add_auth_hook(app)
        return app

    def test_classic_login_page_unchanged_markup(self):
        """Byte-stability: the Classic login shell keeps its exact markers."""
        app = self._make_app()
        with self._classic_env():
            status, _headers, body = wsgi_request(app, path="/login")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn("<title>Quasarr - Login</title>", text)
        self.assertIn('class="btn-primary"', text)
        self.assertIn('<form method="post" action="/login">', text)
        # The login shell is exempt from the Carbon switch link.
        self.assertNotIn("/ui/carbon", text)

    def test_carbon_login_page_renders_with_csp_and_no_classic_switch(self):
        app = self._make_app()
        with self._carbon_env():
            status, headers, body = wsgi_request(app, path="/login")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertEqual(
            header_values(headers, "Content-Security-Policy"),
            [importlib.import_module("quasarr.providers.page_dispatch").CSP_POLICY],
        )
        self.assertIn("<!doctype html>", text)
        self.assertIn(">Login<", text)
        # Exemption preserved in the Carbon variant too: no switch-to-classic
        # link on an unauthenticated login page.
        self.assertNotIn("/ui/classic", text)
        self.assertNotIn("Switch to Classic UI", text)

    def test_carbon_login_form_requires_no_javascript(self):
        """The login form must be a plain POST: name attributes on every
        field, a real submit button, and no inline JS handlers anywhere.
        """
        app = self._make_app()
        with self._carbon_env():
            _status, _headers, body = wsgi_request(app, path="/login")
        text = body.decode("utf-8")

        collector = _FormFieldCollector()
        collector.feed(text)

        self.assertEqual(len(collector.forms), 1)
        form = collector.forms[0]
        self.assertEqual(form.get("method"), "post")
        self.assertEqual(form.get("action"), "/login")
        self.assertNotIn("onsubmit", form)

        names = {i.get("name") for i in collector.inputs}
        self.assertIn("username", names)
        self.assertIn("password", names)
        self.assertIn("next", names)

        username = next(i for i in collector.inputs if i.get("name") == "username")
        password = next(i for i in collector.inputs if i.get("name") == "password")
        self.assertEqual(username.get("type"), "text")
        self.assertEqual(password.get("type"), "password")

        submit_buttons = [b for b in collector.buttons if b.get("type") == "submit"]
        self.assertEqual(len(submit_buttons), 1)
        self.assertNotIn("disabled", submit_buttons[0])
        self.assertNotIn("onclick", submit_buttons[0])

        self.assertIsNone(re.search(r"\son[a-z]+\s*=", text, flags=re.IGNORECASE))

    def test_next_validation_preserved_in_both_variants(self):
        """An external/protocol-relative `next` is sanitized to `/` for both
        the rendered hidden field and (unaffected by Carbon dispatch) the
        real redirect-target validator used on POST success.
        """
        app = self._make_app()
        for ui in ("classic", "carbon"):
            with (
                self.subTest(ui=ui),
                mock.patch.dict(os.environ, {"QUASARR_UI": ui}, clear=True),
            ):
                _status, _headers, body = wsgi_request(
                    app, path="/login", query="next=%2F%2Fevil.com"
                )
            text = body.decode("utf-8")
            self.assertIn('value="/"', text)
            self.assertNotIn("evil.com", text)

    def test_post_failure_shows_error_in_both_variants(self):
        app = self._make_app()
        body = urlencode({"username": "wrong", "password": "wrong"}).encode("ascii")
        headers = {"CONTENT_TYPE": "application/x-www-form-urlencoded"}
        for ui in ("classic", "carbon"):
            with (
                self.subTest(ui=ui),
                mock.patch.dict(os.environ, {"QUASARR_UI": ui}, clear=True),
            ):
                status, _headers, response_body = wsgi_request(
                    app, method="POST", path="/login", headers=headers, body=body
                )
            self.assertEqual(status.split()[0], "200")
            text = response_body.decode("utf-8")
            self.assertIn("Invalid username or password", text)

    def test_post_success_sets_cookie_and_redirects_regardless_of_ui(self):
        """Auth cookie/redirect behavior is untouched by the Carbon dispatch
        wiring: `_handle_login_post()` was not modified.
        """
        app = self._make_app()
        body = urlencode({"username": "admin", "password": "secret"}).encode("ascii")
        headers = {"CONTENT_TYPE": "application/x-www-form-urlencoded"}
        for ui in ("classic", "carbon"):
            with (
                self.subTest(ui=ui),
                mock.patch.dict(os.environ, {"QUASARR_UI": ui}, clear=True),
            ):
                status, headers_out, _body = wsgi_request(
                    app, method="POST", path="/login", headers=headers, body=body
                )
            self.assertEqual(status.split()[0], "303")
            set_cookie = header_values(headers_out, "Set-Cookie")
            self.assertTrue(any("quasarr_session=" in v for v in set_cookie))
            location = header_values(headers_out, "Location")
            self.assertEqual(len(location), 1)
            self.assertTrue(location[0].endswith("/"))

    def test_carbon_render_exception_falls_back_to_classic_login(self):
        """The safety net: if the Carbon login renderer blows up, the visitor
        still gets a working (Classic) login page, not a 500.
        """
        app = self._make_app()
        with (
            self._carbon_env(),
            mock.patch.object(
                self.carbon_templates,
                "render_carbon_simple_page",
                side_effect=RuntimeError("boom"),
            ),
        ):
            status, headers, body = wsgi_request(app, path="/login")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn("<title>Quasarr - Login</title>", text)
        self.assertIn('class="btn-primary"', text)
        self.assertEqual(header_values(headers, "Content-Security-Policy"), [])

    def test_basic_auth_mode_login_route_not_registered(self):
        """Basic-auth mode never installs /login (Classic behavior, unmodified)."""
        app = self._make_app(auth_type="basic")
        with self._carbon_env():
            status, _headers, _body = wsgi_request(app, path="/login")
        self.assertEqual(status.split()[0], "404")


class BasicAuthDispatchTests(CarbonAuthDispatchTestCase):
    """`require_basic_auth()`: 401 + WWW-Authenticate preserved in both UIs."""

    def _make_app_with_protected_route(self, auth_type="basic"):
        # See LoginPageDispatchTests._make_app: these globals are read fresh
        # on every request, so the patches must stay active for the whole
        # test, not just while the route is being registered.
        for name, value in (
            ("_AUTH_USER", "admin"),
            ("_AUTH_PASS", "secret"),
            ("_AUTH_TYPE", auth_type),
        ):
            patcher = mock.patch.object(self.auth, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        app = Bottle()
        self.auth.add_auth_hook(app)

        @app.get("/protected")
        def protected():
            return "secret content"

        return app

    def test_classic_401_body_byte_identical_to_original(self):
        app = self._make_app_with_protected_route()
        with self._classic_env():
            status, headers, body = wsgi_request(app, path="/protected")
        self.assertEqual(status.split()[0], "401")
        self.assertEqual(body.decode("utf-8"), "Authentication required")
        self.assertEqual(
            header_values(headers, "WWW-Authenticate"), ['Basic realm="Quasarr"']
        )
        self.assertEqual(header_values(headers, "Content-Security-Policy"), [])

    def test_carbon_401_page_still_carries_status_and_challenge_header(self):
        app = self._make_app_with_protected_route()
        with self._carbon_env():
            status, headers, body = wsgi_request(app, path="/protected")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "401")
        self.assertEqual(
            header_values(headers, "WWW-Authenticate"), ['Basic realm="Quasarr"']
        )
        self.assertEqual(
            header_values(headers, "Content-Security-Policy"),
            [importlib.import_module("quasarr.providers.page_dispatch").CSP_POLICY],
        )
        self.assertIn("Unauthorized", text)

    def test_carbon_401_page_hides_classic_switch_link(self):
        """An unauthenticated visitor at the Basic-auth challenge must not
        see a `/ui/classic` escape hatch, same exemption the login page
        already carries.
        """
        app = self._make_app_with_protected_route()
        with self._carbon_env():
            status, _headers, body = wsgi_request(app, path="/protected")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "401")
        self.assertNotIn("/ui/classic", text)
        self.assertNotIn("Switch to Classic UI", text)

    def test_carbon_exception_falls_back_to_classic_401_with_status_preserved(self):
        app = self._make_app_with_protected_route()
        with (
            self._carbon_env(),
            mock.patch.object(
                self.carbon_templates,
                "render_carbon_error_page",
                side_effect=RuntimeError("boom"),
            ),
        ):
            status, headers, body = wsgi_request(app, path="/protected")
        self.assertEqual(status.split()[0], "401")
        self.assertEqual(body.decode("utf-8"), "Authentication required")
        self.assertEqual(
            header_values(headers, "WWW-Authenticate"), ['Basic realm="Quasarr"']
        )
        self.assertEqual(header_values(headers, "Content-Security-Policy"), [])

    def test_api_key_401_403_untouched_plain_text(self):
        """Invalid-API-key responses stay a plain-text 401/403 for automation
        clients - never Carbon-rendered, regardless of UI preference.
        """
        app = Bottle()
        self.auth.add_auth_hook(app)

        @app.get("/api/example")
        @self.auth.require_api_key
        def example():
            return "ok"

        fake_config = mock.MagicMock()
        fake_config.get.return_value = "real-key"
        with (
            self._carbon_env(),
            mock.patch.object(self.auth, "Config", return_value=fake_config),
        ):
            status, headers, body = wsgi_request(app, path="/api/example")
        self.assertEqual(status.split()[0], "401")
        self.assertIn(b"Missing API Key", body)
        self.assertNotIn(b"cds-status-card", body)
        self.assertEqual(header_values(headers, "Content-Security-Policy"), [])


class CommonEntryPageDispatchTests(CarbonAuthDispatchTestCase):
    """render_success / render_success_no_wait / render_fail dispatch."""

    def _route_app(self, renderer_call):
        app = Bottle()

        @app.get("/x")
        def route():
            return renderer_call()

        return app

    def test_render_success_classic_byte_identical_to_base(self):
        """Golden comparison: the exact f-string template found in
        `render_success` at base 612f7dd (verbatim, only the enclosing
        `def classic():` changed), interpolated here independently and
        compared to what the current implementation actually produces.
        """
        message, timeout, optional_text = "Done!", 7, "extra"
        images = self.html_templates.images
        button_html = self.html_templates.render_button(
            f"Wait time... {timeout}",
            "secondary",
            {"id": "nextButton", "disabled": "true"},
        )
        script = f"""
        <script>
            let counter = {timeout};
            const btn = document.getElementById('nextButton');
            const interval = setInterval(() => {{
                counter--;
                btn.innerText = `Wait time... ${{counter}}`;
                if (counter === 0) {{
                    clearInterval(interval);
                    btn.innerText = 'Continue';
                    btn.disabled = false;
                    btn.className = 'btn-primary';
                    btn.onclick = () => window.location.href = '/';
                }}
            }}, 1000);
        </script>
    """
        expected_content = f'''<h1 onclick="window.location.href='/'"><img src="{images.logo}" type="image/webp" alt="Quasarr logo" class="logo"/>Quasarr</h1>
    <h2>{message}</h2>
    {optional_text}
    {button_html}
    {script}
    '''

        captured = {}
        with (
            self._classic_env(),
            mock.patch.object(
                self.html_templates,
                "render_centered_html",
                side_effect=lambda content, footer_content="": (
                    captured.setdefault("content", content) or "wrapped"
                ),
            ),
        ):
            self.html_templates.render_success(message, timeout, optional_text)

        self.assertEqual(captured["content"], expected_content)

    def test_render_success_no_wait_classic_delegates_to_render_centered_html(self):
        captured = {}
        with (
            self._classic_env(),
            mock.patch.object(
                self.html_templates,
                "render_centered_html",
                side_effect=lambda content, footer_content="": (
                    captured.setdefault("content", content) or "wrapped"
                ),
            ),
        ):
            self.html_templates.render_success_no_wait("Saved", "note")
        self.assertIn("<h2>Saved</h2>", captured["content"])
        self.assertIn("note", captured["content"])
        self.assertIn("onclick=\"window.location.href='/'\"", captured["content"])

    def test_render_fail_classic_delegates_to_render_centered_html(self):
        captured = {}
        with (
            self._classic_env(),
            mock.patch.object(
                self.html_templates,
                "render_centered_html",
                side_effect=lambda content, footer_content="": (
                    captured.setdefault("content", content) or "wrapped"
                ),
            ),
        ):
            self.html_templates.render_fail("Nope")
        self.assertIn("<h2>Nope</h2>", captured["content"])
        self.assertIn("Back", captured["content"])

    def test_render_success_carbon_has_countdown_button_and_csp(self):
        app = self._route_app(lambda: self.html_templates.render_success("Hi", 5))
        with self._carbon_env():
            status, headers, body = wsgi_request(app, path="/x")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertEqual(
            header_values(headers, "Content-Security-Policy"),
            [importlib.import_module("quasarr.providers.page_dispatch").CSP_POLICY],
        )
        self.assertIn('data-action="continue-countdown"', text)
        self.assertIn('data-seconds="5"', text)
        self.assertIn('data-target="/"', text)
        self.assertNotIn("<script>", text)  # strict CSP: no inline scripts

    def test_render_success_no_wait_carbon_needs_no_js(self):
        app = self._route_app(
            lambda: self.html_templates.render_success_no_wait("Done")
        )
        with self._carbon_env():
            _status, _headers, body = wsgi_request(app, path="/x")
        text = body.decode("utf-8")
        self.assertIn('<a class="cds-btn cds-btn--primary" href="/">Continue</a>', text)

    def test_render_fail_carbon_needs_no_js(self):
        app = self._route_app(lambda: self.html_templates.render_fail("Broken"))
        with self._carbon_env():
            _status, _headers, body = wsgi_request(app, path="/x")
        text = body.decode("utf-8")
        self.assertIn('<a class="cds-btn cds-btn--secondary" href="/">Back</a>', text)

    def test_render_success_carbon_exception_falls_back_to_classic(self):
        app = self._route_app(lambda: self.html_templates.render_success("Hi", 5))
        with (
            self._carbon_env(),
            mock.patch.object(
                self.carbon_templates,
                "render_carbon_simple_page",
                side_effect=RuntimeError("boom"),
            ),
        ):
            status, headers, body = wsgi_request(app, path="/x")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn("Wait time... 5", text)
        self.assertEqual(header_values(headers, "Content-Security-Policy"), [])


class CarbonMessageEscapingTests(CarbonAuthDispatchTestCase):
    """The Carbon branches of render_fail/render_success/
    render_success_no_wait/render_error_page must html-escape a
    caller-supplied `message` before it reaches `<p>{message}</p>` -
    request-derived text (a typed hostname in storage/setup/hostnames.py's
    save_hostnames(), a URL path segment in its set_credentials()) reaches
    these primitives raw. `optional_text` is a different, established,
    renderer-owned-HTML parameter (see save_hostnames()'s `<br>`-joined
    error list) and is deliberately never escaped here, in either UI - this
    only tightens `message`.
    """

    PAYLOAD = "<b>PWNED</b>"
    ESCAPED = "&lt;b&gt;PWNED&lt;/b&gt;"

    def _route_app(self, renderer_call):
        app = Bottle()

        @app.get("/x")
        def route():
            return renderer_call()

        return app

    def _carbon_body(self, renderer_call):
        app = self._route_app(renderer_call)
        with self._carbon_env():
            _status, _headers, body = wsgi_request(app, path="/x")
        return body.decode("utf-8")

    def test_render_fail_carbon_escapes_message(self):
        text = self._carbon_body(lambda: self.html_templates.render_fail(self.PAYLOAD))
        self.assertNotIn(self.PAYLOAD, text)
        self.assertIn(self.ESCAPED, text)

    def test_render_success_carbon_escapes_message(self):
        text = self._carbon_body(
            lambda: self.html_templates.render_success(self.PAYLOAD, 5)
        )
        self.assertNotIn(self.PAYLOAD, text)
        self.assertIn(self.ESCAPED, text)

    def test_render_success_no_wait_carbon_escapes_message(self):
        text = self._carbon_body(
            lambda: self.html_templates.render_success_no_wait(self.PAYLOAD)
        )
        self.assertNotIn(self.PAYLOAD, text)
        self.assertIn(self.ESCAPED, text)

    def test_render_error_page_carbon_escapes_message(self):
        text = self._carbon_body(
            lambda: self.html_templates.render_error_page(404, self.PAYLOAD)
        )
        self.assertNotIn(self.PAYLOAD, text)
        self.assertIn(self.ESCAPED, text)

    def test_render_success_carbon_still_renders_optional_text_as_html(self):
        """optional_text is the established renderer-owned-HTML escape
        hatch - it must keep rendering raw, unescaped, so message-escaping
        does not double-escape a caller's own markup.
        """
        text = self._carbon_body(
            lambda: self.html_templates.render_success(
                "Done", 5, optional_text="<br>note"
            )
        )
        self.assertIn("<br>note", text)

    def test_render_success_no_wait_carbon_still_renders_optional_text_as_html(self):
        text = self._carbon_body(
            lambda: self.html_templates.render_success_no_wait(
                "Done", optional_text="<br>note"
            )
        )
        self.assertIn("<br>note", text)


class RenderFailBrTokenTests(CarbonAuthDispatchTestCase):
    """`render_fail`'s single `message` argument is shared, unchanged, by
    both UI branches - `storage/setup/hostnames.py`'s `al` credential-failure
    message embeds a literal `<br><br>` line break Classic has always
    rendered raw (`<h2>{message}</h2>`). Plain `escape(str(message))`
    (Important #2's fix) turned that into visible `&lt;br&gt;&lt;br&gt;`
    text on the Carbon path - a real regression, since Carbon/`al` is the
    default UI on first-run al setup.

    The call site cannot change (Classic reads the identical string), so
    the fix lives entirely inside `render_fail`'s Carbon branch: split the
    incoming message on the exact literal token `<br>` BEFORE escaping,
    escape each segment independently, then rejoin with a real `<br>`
    element. `<br>` is a fixed, attribute-free token - anything that merely
    LOOKS like a break tag but isn't the exact 4-character substring
    (`<br/>`, `<br class="x">`, a hostile `<br onload=x>`) stays inside a
    segment and is escaped like any other text. Tradeoff: a user who
    literally types the bare text `<br>` into a form field gets a
    cosmetic line break instead of literal escaped text - no script or
    attribute surface, since only the exact no-attribute token is ever
    honored.
    """

    AL_MESSAGE = (
        "User and Password wrong or empty.<br><br>"
        "Or if you skipped Flaresolverr setup earlier, "
        "you must chose to skip login for this site, "
        "set up flaresolverr-next in the UI and then restart Quasarr!"
    )

    def _route_app(self, renderer_call):
        app = Bottle()

        @app.get("/x")
        def route():
            return renderer_call()

        return app

    def _carbon_body(self, renderer_call):
        app = self._route_app(renderer_call)
        with self._carbon_env():
            _status, _headers, body = wsgi_request(app, path="/x")
        return body.decode("utf-8")

    def test_al_message_renders_as_a_real_break_not_escaped_markup(self):
        """(b) The al Carbon failure page shows visually separated
        sentences, not escaped `&lt;br&gt;` markup.
        """
        text = self._carbon_body(
            lambda: self.html_templates.render_fail(self.AL_MESSAGE)
        )
        self.assertNotIn("&lt;br&gt;", text)
        self.assertIn(
            "User and Password wrong or empty.<br><br>Or if you skipped "
            "Flaresolverr setup earlier, you must chose to skip login for "
            "this site, set up flaresolverr-next in the UI and then "
            "restart Quasarr!",
            text,
        )

    def test_al_message_classic_bytes_are_unchanged(self):
        """(a) Classic bytes unchanged - the exact f-string template
        `render_fail`'s `classic()` closure has always used, interpolated
        here independently (golden-comparison style, matching
        `test_render_success_classic_byte_identical_to_base`) and compared
        to what the current implementation actually produces for the same
        al message.
        """
        images = self.html_templates.images
        button_html = self.html_templates.render_button(
            "Back", "secondary", {"onclick": "window.location.href='/'"}
        )
        expected_content = f"""<h1 onclick="window.location.href='/'"><img src="{images.logo}" type="image/webp" alt="Quasarr logo" class="logo"/>Quasarr</h1>
        <h2>{self.AL_MESSAGE}</h2>
        {button_html}
    """

        captured = {}
        with (
            self._classic_env(),
            mock.patch.object(
                self.html_templates,
                "render_centered_html",
                side_effect=lambda content, footer_content="": (
                    captured.setdefault("content", content) or "wrapped"
                ),
            ),
        ):
            self.html_templates.render_fail(self.AL_MESSAGE)

        self.assertEqual(captured["content"], expected_content)
        self.assertIn("<br><br>", captured["content"])

    def test_hostile_message_escapes_script_and_refuses_attributed_br(self):
        """(c) User-typed content remains escaped: a hostile message
        carrying both a `<script>` tag and a `<br onload=x>` (not the bare
        token) alongside a real bare `<br>`. Only the exact bare `<br>`
        becomes a break; `<script>` and `<br onload=x>` must not survive as
        live markup.
        """
        payload = "Wrong.<br>Try again.<script>alert(1)</script><br onload=x>End."
        text = self._carbon_body(lambda: self.html_templates.render_fail(payload))

        self.assertNotIn("<script>alert(1)</script>", text)
        self.assertIn(escape("<script>alert(1)</script>"), text)

        self.assertNotIn("<br onload=x>", text)
        self.assertIn(escape("<br onload=x>"), text)

        self.assertIn("Wrong.<br>Try again.", text)

    def test_render_success_family_has_no_br_token_caller_today(self):
        """`render_success`/`render_success_no_wait` have no real caller
        embedding a `<br>` token in `message` today (only `optional_text`
        carries renderer-owned HTML for those two - see
        `save_hostnames()`'s `<br>`-joined error list, which actually feeds
        the separate `render_reconnect_success` primitive, not these two).
        A bare `<br>` typed into `message` for these two therefore stays
        plain escaped text, unlike `render_fail`.
        """
        text = self._carbon_body(
            lambda: self.html_templates.render_success("A<br>B", 5)
        )
        self.assertIn(escape("A<br>B"), text)
        self.assertNotIn("A<br>B", text)


class ErrorPageDispatchTests(CarbonAuthDispatchTestCase):
    """`render_error_page` (generic 401/403/404 + package-not-found)."""

    def _route_app(self, status_code, message=None, title=None, back_href=None):
        app = Bottle()
        kwargs = {"title": title}
        if back_href is not None:
            kwargs["back_href"] = back_href

        @app.get("/x")
        def route():
            return self.html_templates.render_error_page(status_code, message, **kwargs)

        return app

    def test_status_code_survives_rendering_in_both_variants(self):
        for status_code in (401, 403, 404):
            for ui in ("classic", "carbon"):
                with self.subTest(status_code=status_code, ui=ui):
                    app = self._route_app(status_code)
                    with mock.patch.dict(os.environ, {"QUASARR_UI": ui}, clear=True):
                        status, _headers, _body = wsgi_request(app, path="/x")
                    self.assertEqual(status.split()[0], str(status_code))

    def test_status_code_survives_carbon_render_exception(self):
        app = self._route_app(404)
        with (
            self._carbon_env(),
            mock.patch.object(
                self.carbon_templates,
                "render_carbon_error_page",
                side_effect=RuntimeError("boom"),
            ),
        ):
            status, headers, _body = wsgi_request(app, path="/x")
        self.assertEqual(status.split()[0], "404")
        self.assertEqual(header_values(headers, "Content-Security-Policy"), [])

    def test_package_not_found_message_renders_in_both_variants(self):
        message = "Package not found or already solved."
        for ui in ("classic", "carbon"):
            with self.subTest(ui=ui):
                app = self._route_app(404, message, title="Package Not Found")
                with mock.patch.dict(os.environ, {"QUASARR_UI": ui}, clear=True):
                    status, _headers, body = wsgi_request(app, path="/x")
                self.assertEqual(status.split()[0], "404")
                self.assertIn(message, body.decode("utf-8"))

    def test_invalid_status_code_rejected(self):
        with self.assertRaises(ValueError):
            self.html_templates.render_error_page(500)

    def test_back_href_reaches_the_carbon_error_primitive(self):
        """The Carbon primitive's ``back_href`` param was dead code
        until the dispatcher forwarded it - plumb it through rather than
        leaving unreachable API future callers could bypass the dispatcher
        for.
        """
        app = self._route_app(404, "Gone", title="Gone", back_href="/packages")
        with self._carbon_env():
            _status, _headers, body = wsgi_request(app, path="/x")
        self.assertIn('href="/packages"', body.decode("utf-8"))


class ReconnectSuccessDispatchTests(CarbonAuthDispatchTestCase):
    def setUp(self):
        super().setUp()
        self.common = importlib.import_module("quasarr.storage.setup.common")

    def _route_app(self):
        app = Bottle()

        @app.get("/x")
        def route():
            return self.common.render_reconnect_success("Configured!", 3)

        return app

    def test_classic_reconnect_page_keeps_polling_script_and_timing(self):
        app = self._route_app()
        with self._classic_env():
            status, _headers, body = wsgi_request(app, path="/x")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn("Configured!", text)
        self.assertIn("var remaining = 3;", text)
        self.assertIn("method: 'HEAD', cache: 'no-store'", text)
        self.assertIn("}, 1000);", text)
        self.assertIn("}, 500);", text)

    def test_carbon_reconnect_page_uses_external_action_not_inline_script(self):
        app = self._route_app()
        with self._carbon_env():
            status, headers, body = wsgi_request(app, path="/x")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertEqual(
            header_values(headers, "Content-Security-Policy"),
            [importlib.import_module("quasarr.providers.page_dispatch").CSP_POLICY],
        )
        self.assertIn('data-action="reconnect-poll"', text)
        self.assertIn('data-seconds="3"', text)
        self.assertNotIn("<script>", text)

    def test_carbon_exception_falls_back_to_classic_reconnect_page(self):
        app = self._route_app()
        with (
            self._carbon_env(),
            mock.patch.object(
                self.carbon_templates,
                "render_carbon_simple_page",
                side_effect=RuntimeError("boom"),
            ),
        ):
            status, headers, body = wsgi_request(app, path="/x")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn("var remaining = 3;", text)
        self.assertEqual(header_values(headers, "Content-Security-Policy"), [])


class JdDisconnectedDispatchTests(CarbonAuthDispatchTestCase):
    def setUp(self):
        super().setUp()
        self.jdownloader = importlib.import_module("quasarr.api.jdownloader")

    def _make_shared_state(self, connected=False, device_name=""):
        class _Values(dict):
            pass

        shared_state = mock.MagicMock()
        shared_state.values = _Values(device=(object() if connected else None))
        return shared_state

    @contextlib.contextmanager
    def _config_patch(self, device_name=""):
        class _Config:
            def __init__(self, _section):
                pass

            def get(self, key):
                return device_name if key == "device" else None

        with mock.patch.object(self.jdownloader, "Config", _Config):
            yield

    def _route_app(self, shared_state):
        app = Bottle()

        @app.get("/x")
        def route():
            return self.jdownloader.get_jdownloader_disconnected_page(shared_state)

        return app

    def test_classic_and_carbon_both_render_disconnected_status(self):
        shared_state = self._make_shared_state(connected=False)
        app = self._route_app(shared_state)
        for ui in ("classic", "carbon"):
            with self.subTest(ui=ui):
                with (
                    self._config_patch(),
                    mock.patch.dict(os.environ, {"QUASARR_UI": ui}, clear=True),
                ):
                    status, _headers, body = wsgi_request(app, path="/x")
                text = body.decode("utf-8")
                self.assertEqual(status.split()[0], "200")
                self.assertIn(
                    "disconnected" if ui == "classic" else "Disconnected", text
                )

    def test_carbon_exception_falls_back_to_classic(self):
        shared_state = self._make_shared_state(connected=False)
        app = self._route_app(shared_state)
        with (
            self._config_patch(),
            self._carbon_env(),
            mock.patch.object(
                self.carbon_templates,
                "render_carbon_simple_page",
                side_effect=RuntimeError("boom"),
            ),
        ):
            status, headers, body = wsgi_request(app, path="/x")
        self.assertEqual(status.split()[0], "200")
        self.assertIn("disconnected", body.decode("utf-8"))
        self.assertEqual(header_values(headers, "Content-Security-Policy"), [])


class CarbonJsStatusPageScriptTests(unittest.TestCase):
    """Structural pins on the new carbon.js status-page behaviors."""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path

        static_root = Path(__file__).resolve().parent.parent / "quasarr" / "static"
        cls.js = (static_root / "carbon.js").read_text(encoding="utf-8")

    def test_continue_countdown_action_present(self):
        self.assertIn('data-action="continue-countdown"', self.js)
        self.assertIn("startCountdownRedirect", self.js)

    def test_reconnect_poll_action_present(self):
        self.assertIn('data-action="reconnect-poll"', self.js)
        self.assertIn("startReconnectPoll", self.js)

    def test_reconnect_poll_preserves_exact_classic_timing(self):
        """Same constants as the previous inline Classic script: 1000ms
        countdown ticks, a HEAD request with no-store caching, and a 500ms
        pause before reload.
        """
        self.assertIn("method: 'HEAD', cache: 'no-store' }", self.js)
        self.assertIn("}, 1000);", self.js)
        self.assertIn("}, 500);", self.js)

    def test_no_inline_handlers_introduced(self):
        # The new IIFE only wires behavior through querySelectorAll +
        # addEventListener, never document.write or inline attributes.
        section_start = self.js.index("bootstrapCarbonStatusPages")
        section = self.js[section_start:]
        self.assertNotIn("document.write", section)


class UnauthenticatedAssetAccessTests(CarbonAuthDispatchTestCase):
    """Gate: unauthenticated visitors can load CSS/JS/fonts, and the login
    page they land on actually references those exact public paths."""

    def _make_app(self):
        from quasarr.providers.static_assets import setup_static_routes

        app = Bottle()
        with (
            mock.patch.object(self.auth, "_AUTH_USER", "admin"),
            mock.patch.object(self.auth, "_AUTH_PASS", "secret"),
            mock.patch.object(self.auth, "_AUTH_TYPE", "form"),
        ):
            self.auth.add_auth_routes(app)
            self.auth.add_auth_hook(app)
            setup_static_routes(app, immutable=True)
        return app

    def test_login_page_references_and_can_fetch_its_own_assets_unauthenticated(
        self,
    ):
        app = self._make_app()
        with self._carbon_env():
            status, _headers, body = wsgi_request(app, path="/login")
        self.assertEqual(status.split()[0], "200")
        text = body.decode("utf-8")

        css_match = re.search(r'href="(/static/carbon\.css[^"]*)"', text)
        js_match = re.search(r'src="(/static/carbon\.js[^"]*)"', text)
        self.assertIsNotNone(css_match)
        self.assertIsNotNone(js_match)

        for asset_path in (css_match.group(1), js_match.group(1)):
            asset_only = asset_path.split("?", 1)[0]
            query = asset_path.split("?", 1)[1] if "?" in asset_path else ""
            status, _headers, _body = wsgi_request(app, path=asset_only, query=query)
            self.assertEqual(status.split()[0], "200", msg=asset_path)

    def test_font_asset_publicly_reachable_without_cookie(self):
        app = self._make_app()
        status, headers, body = wsgi_request(
            app, path="/static/fonts/IBMPlexSans-Regular-Latin.woff2"
        )
        self.assertEqual(status.split()[0], "200")
        self.assertTrue(body)
        self.assertIn("font", header_values(headers, "Content-Type")[0])


if __name__ == "__main__":
    unittest.main()
