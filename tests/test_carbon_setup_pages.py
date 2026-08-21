# -*- coding: utf-8 -*-
"""Carbon dispatch for the eight temporary first-run/reconfiguration
setup servers (path, hostnames, per-source hostname credentials,
FlareSolverr, *arr client selector, Radarr, Sonarr, JDownloader).

Two layers of coverage:

- Direct unit tests of each ``quasarr.storage.setup.carbon.render_setup_*()``
  function: structural guards pass (any violation raises inside
  ``render_carbon_simple_page``, so a passing call is itself the proof),
  secrets (stored hostname credentials) never enter the markup, and the
  uncontrolled per-hostname ``details`` exception text never enters the
  initial page HTML.
- Real Bottle-WSGI route-dispatch tests (the ``tests/test_page_dispatch.py``
  house pattern, reused by ``test_carbon_auth_pages.py`` and here) proving
  each of the eight ``*_config()`` functions' ``GET /`` route wires
  ``render_page`` correctly: Classic output is byte-identical to the
  original body (spot-checked via literal fragments the extraction must
  not disturb) and Carbon output is a real ``<!doctype html>`` document with
  no side-nav/header chrome (``render_carbon_simple_page``), reached via
  ``Server`` replaced by a non-blocking ``CapturingServer`` (mirrors
  ``test_carbon_route_dispatch.py``'s pattern) so ``serve_temporarily()``
  never actually binds a socket.
"""

import io
import os
import re
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest import mock

from quasarr.storage.setup import carbon as setup_carbon

# ---------------------------------------------------------------------------
# Shared fakes / helpers
# ---------------------------------------------------------------------------


class _FakeConfigSection:
    def __init__(self, data):
        self._data = dict(data)

    def get(self, key):
        return self._data.get(key)

    def save(self, key, value):
        self._data[key] = value


def _config_factory(config_data):
    def factory(section):
        return _FakeConfigSection(config_data.get(section, {}))

    return factory


class _FakeDataBase:
    def __init__(self, data):
        self._data = dict(data)

    def retrieve(self, key):
        return self._data.get(key)

    def update_store(self, key, value):
        self._data[key] = value

    def delete(self, key):
        self._data.pop(key, None)


def _database_factory(tables):
    def factory(table):
        return _FakeDataBase(tables.get(table, {}))

    return factory


def _shared_state(sites=("GA", "GB")):
    return SimpleNamespace(
        values={
            "sites": list(sites),
            "external_address": "http://setup.invalid:8080",
            "port": 8080,
        }
    )


class CapturingServer:
    """Replaces ``web_server.Server`` so ``*_config()`` returns immediately
    with the built Bottle app captured, instead of binding a real socket and
    blocking in ``serve_temporarily()``. Mirrors
    ``test_carbon_route_dispatch.py``'s ``CapturingServer``.
    """

    app = None

    def __init__(self, wsgi_app, **_kwargs):
        type(self).app = wsgi_app

    def serve_temporarily(self):
        return True


def wsgi_request(app, method="GET", path="/", query=""):
    """Real WSGI request against a Bottle app - no sockets, no test client."""
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "8080",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "HTTP_HOST": "localhost:8080",
        "wsgi.url_scheme": "http",
        "wsgi.input": BytesIO(b""),
        "wsgi.errors": io.StringIO(),
    }
    captured = {}

    def start_response(status, headers, _exc_info=None):
        captured["status"] = status
        captured["headers"] = list(headers)

    body = b"".join(app(environ, start_response))
    return captured["status"], captured["headers"], body


# ---------------------------------------------------------------------------
# Direct render-function tests
# ---------------------------------------------------------------------------


class SetupPathCarbonRenderTests(unittest.TestCase):
    def test_renders_field_and_form(self):
        html = setup_carbon.render_setup_path("/opt/quasarr")
        self.assertIn("<!doctype html>", html)
        self.assertIn('action="/api/config"', html)
        self.assertIn('name="config_path"', html)
        self.assertIn('placeholder="/opt/quasarr"', html)
        self.assertIn("<title>Press &#x27;Save&#x27;", html)

    def test_escapes_hostile_current_path(self):
        html = setup_carbon.render_setup_path('"><script>alert(1)</script>')
        self.assertNotIn("<script>alert(1)</script>", html)


HOSTNAME_ROWS = [
    {
        "id": "ga",
        "label": "GA",
        "hostname": "ga-fixture.invalid",
        "status": "ok",
        "status_emoji": "\U0001f7e2",
        "status_title": "Working normally",
        "details": "SECRET-EXCEPTION-DETAIL-ga-fixture.invalid",
        "timestamp": "2026-01-01T00:00:00Z",
        "operation": "search",
        "missing_arr_client": None,
        "supports_login": False,
        "credential_section": None,
        "skip_login": False,
        "language": "en",
        "categories": [],
        "invite_only": False,
        "requires_login": False,
        "requires_account": False,
        "requires_flaresolverr": False,
    },
    {
        "id": "gb",
        "label": "GB",
        "hostname": "gb-fixture.invalid",
        "status": "skipped",
        "status_emoji": "\U0001f7e1",
        "status_title": "Login was skipped",
        "details": "SECRET-EXCEPTION-DETAIL-gb",
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
        "requires_flaresolverr": True,
    },
]

GB_SECRET_PASSWORD = "gb-super-secret-password"


class SetupHostnamesCarbonRenderTests(unittest.TestCase):
    def _render(self, *, flaresolverr_skipped=False, rows=None):
        settings_config = {
            "Settings": {"hostnames_url": "https://hostnames.invalid/list.ini"},
            # A configured secret must never reach this page's markup even if
            # a caller-shaped Config stub happens to carry one under the
            # credential section - this row builder never reads it directly,
            # but the guard proves it structurally too.
            "GB": {"user": "gb-user", "password": GB_SECRET_PASSWORD},
        }
        with (
            mock.patch.object(
                setup_carbon,
                "build_hostname_rows",
                return_value=rows if rows is not None else HOSTNAME_ROWS,
            ),
            mock.patch.object(
                setup_carbon, "Config", side_effect=_config_factory(settings_config)
            ),
            mock.patch.object(
                setup_carbon,
                "DataBase",
                side_effect=_database_factory(
                    {"skip_flaresolverr": {"skipped": "true"}}
                    if flaresolverr_skipped
                    else {}
                ),
            ),
        ):
            return setup_carbon.render_setup_hostnames(_shared_state())

    def test_renders_one_row_per_hostname(self):
        html = self._render()
        self.assertIn('data-hostname-id="ga"', html)
        self.assertIn('data-hostname-id="gb"', html)
        self.assertIn('value="ga-fixture.invalid"', html)
        self.assertIn('value="gb-fixture.invalid"', html)

    def test_rows_are_wrapped_in_the_scroll_container(self):
        """Regression pin: without this wrapper, carbon.css's narrow-
        viewport rule (a forced 760px min-width on every
        .cds-hostname-table__row, which these rows carry regardless, via
        the shared row builder) had no .cds-hostname-table ancestor to
        become a horizontal scroll container, so the rows overflowed this
        page's narrower (760px-max) card instead of scrolling.

        Walks balanced <div>/</div> tags from the wrapper's own opening tag
        to find where THAT div actually closes, rather than just checking
        some </div> exists later in the page (true of any HTML document)
        or that the next </div> after a row closes it (the row's own
        nested credentials panel <div> would satisfy that without the
        wrapper ever really containing the rows).
        """
        html = self._render()
        start = html.index('<div class="cds-hostname-table">')
        depth = 0
        wrapper_end = None
        for match in re.finditer(r"<div\b|</div>", html[start:]):
            depth += -1 if match.group() == "</div>" else 1
            if depth == 0:
                wrapper_end = start + match.end()
                break
        self.assertIsNotNone(
            wrapper_end, "the .cds-hostname-table wrapper never closes"
        )
        wrapped = html[start:wrapper_end]
        self.assertIn('data-hostname-id="ga"', wrapped)
        self.assertIn('data-hostname-id="gb"', wrapped)

    def test_uncontrolled_details_text_never_in_initial_markup(self):
        """The exception-derived `details` text may embed a configured
        hostname; it must be fetched fresh by carbon.js, never rendered here.
        """
        html = self._render()
        self.assertNotIn("SECRET-EXCEPTION-DETAIL", html)

    def test_credential_fields_always_render_blank(self):
        html = self._render()
        self.assertNotIn(GB_SECRET_PASSWORD, html)
        self.assertIn('id="hostname-cred-user-gb"', html)
        self.assertIn('id="hostname-cred-pass-gb"', html)
        # blank: the credential inputs never carry a value= attribute at all
        user_input = re.search(
            r'<input class="cds-field__input"[^>]*id="hostname-cred-user-gb"[^>]*>',
            html,
        )
        pass_input = re.search(
            r'<input class="cds-field__input"[^>]*id="hostname-cred-pass-gb"[^>]*>',
            html,
        )
        self.assertIsNotNone(user_input)
        self.assertIsNotNone(pass_input)
        self.assertNotIn("value=", user_input.group(0))
        self.assertNotIn("value=", pass_input.group(0))

    def test_skip_banner_rendered_for_skipped_row(self):
        html = self._render()
        self.assertIn('id="hostname-skip-banner-gb"', html)
        self.assertNotIn('id="hostname-skip-banner-ga"', html)

    def test_flaresolverr_required_warning_rendered_when_skipped(self):
        html = self._render(flaresolverr_skipped=True)
        self.assertIn("flaresolverr-next, which was skipped", html)

    def test_import_field_carries_stored_url(self):
        html = self._render()
        self.assertIn('value="https://hostnames.invalid/list.ini"', html)

    def test_save_form_targets_existing_route(self):
        html = self._render()
        self.assertIn('id="hostnames-form" action="/api/hostnames" method="post"', html)

    def test_credential_inputs_never_form_associated_with_hostnames_form(self):
        """Regression pin: a `hidden` element is
        NOT excluded from its enclosing form's submission - only `disabled`
        or having no form association is. The hostnames-form element must
        therefore be empty (no rows tile nested inside it, so a typed
        credential password can never ride along in POST /api/hostnames),
        and every hostname-cred-user-*/hostname-cred-pass-* input must
        declare no form="hostnames-form" association at all.
        """
        html = self._render()

        form_match = re.search(
            r'<form id="hostnames-form"[^>]*>(.*?)</form>', html, re.DOTALL
        )
        self.assertIsNotNone(form_match)
        self.assertEqual(form_match.group(1).strip(), "")

        user_input = re.search(
            r'<input class="cds-field__input"[^>]*id="hostname-cred-user-gb"[^>]*>',
            html,
        )
        pass_input = re.search(
            r'<input class="cds-field__input"[^>]*id="hostname-cred-pass-gb"[^>]*>',
            html,
        )
        self.assertIsNotNone(user_input)
        self.assertIsNotNone(pass_input)
        self.assertNotIn("hostnames-form", user_input.group(0))
        self.assertNotIn("hostnames-form", pass_input.group(0))

        # The Save button still submits hostnames-form despite living
        # outside its (now-empty) element, via an explicit form= attribute.
        self.assertIn('type="submit" form="hostnames-form">Save</button>', html)

    def test_open_hostname_link_present_when_configured(self):
        html = self._render()
        self.assertIn(
            '<a class="cds-btn cds-btn--tertiary" href="https://ga-fixture.invalid" '
            'target="_blank" rel="noopener noreferrer">Open GA</a>',
            html,
        )
        self.assertIn(
            '<a class="cds-btn cds-btn--tertiary" href="https://gb-fixture.invalid" '
            'target="_blank" rel="noopener noreferrer">Open GB</a>',
            html,
        )

    def test_flaresolverr_warning_includes_configure_link(self):
        html = self._render(flaresolverr_skipped=True)
        self.assertIn(
            '<a class="cds-btn cds-btn--secondary" href="/flaresolverr">'
            "Configure flaresolverr-next</a>",
            html,
        )

    def test_hostile_label_is_escaped(self):
        hostile_rows = [dict(HOSTNAME_ROWS[0])]
        hostile_rows[0]["label"] = "<script>alert(1)</script>"
        html = self._render(rows=hostile_rows)
        self.assertNotIn("<script>alert(1)</script>", html)


class SetupHostnameCredentialsCarbonRenderTests(unittest.TestCase):
    def _render(self, *, flaresolverr_url="", requires_flaresolverr=False):
        config_data = {"FlareSolverr": {"url": flaresolverr_url}}
        with (
            mock.patch.object(
                setup_carbon, "Config", side_effect=_config_factory(config_data)
            ),
            mock.patch.object(
                setup_carbon,
                "get_source_metadata",
                return_value={"ga": {"requires_flaresolverr": requires_flaresolverr}},
            ),
        ):
            return setup_carbon.render_setup_hostname_credentials(
                _shared_state(), "GA", "ga-fixture.invalid"
            )

    def test_renders_credentials_form(self):
        html = self._render()
        self.assertIn('action="/api/credentials/GA"', html)
        self.assertIn('name="user"', html)
        self.assertIn('name="password"', html)
        self.assertIn('data-action="setup-credentials-skip"', html)
        self.assertIn('data-shorthand="ga"', html)

    def test_no_flaresolverr_warning_when_not_required(self):
        html = self._render()
        self.assertNotIn("flaresolverr-next Required", html)

    def test_flaresolverr_warning_and_disabled_fields_when_missing(self):
        html = self._render(flaresolverr_url="", requires_flaresolverr=True)
        self.assertIn("flaresolverr-next Required", html)
        self.assertIn('action="/api/flaresolverr_inline"', html)
        user_input = re.search(
            r'<input class="cds-field__input"[^>]*id="user"[^>]*>', html
        )
        password_input = re.search(
            r'<input class="cds-field__input"[^>]*id="password"[^>]*>', html
        )
        self.assertIsNotNone(user_input)
        self.assertIsNotNone(password_input)
        self.assertIn("disabled", user_input.group(0))
        self.assertIn("disabled", password_input.group(0))

    def test_no_warning_when_flaresolverr_configured(self):
        html = self._render(
            flaresolverr_url="http://flaresolverr.invalid:8191/v1",
            requires_flaresolverr=True,
        )
        self.assertNotIn("flaresolverr-next Required", html)

    def test_domain_rendered_as_safe_link(self):
        html = self._render()
        self.assertIn(
            '<a href="https://ga-fixture.invalid" target="_blank" '
            'rel="noopener noreferrer">',
            html,
        )


class SetupFlareSolverrCarbonRenderTests(unittest.TestCase):
    def _render(self, url=""):
        with mock.patch.object(
            setup_carbon,
            "Config",
            side_effect=_config_factory({"FlareSolverr": {"url": url}}),
        ):
            return setup_carbon.render_setup_flaresolverr(_shared_state())

    def test_renders_form_and_skip_action(self):
        html = self._render()
        self.assertIn('action="/api/flaresolverr" method="post"', html)
        self.assertIn('data-action="setup-flaresolverr-skip"', html)

    def test_prefills_existing_url(self):
        html = self._render("http://flaresolverr.invalid:8191/v1")
        self.assertIn('value="http://flaresolverr.invalid:8191/v1"', html)


class SetupArrClientCarbonRenderTests(unittest.TestCase):
    def test_renders_both_client_choices(self):
        html = setup_carbon.render_setup_arr_client(["ga"], ["gb", "gc"])
        self.assertIn('value="radarr"', html)
        self.assertIn('value="sonarr"', html)
        self.assertIn("Required by: GA", html)
        self.assertIn("Required by: GB, GC", html)
        self.assertIn('action="/api/arr/client"', html)


class SetupRadarrSonarrCarbonRenderTests(unittest.TestCase):
    def test_radarr_renders_form(self):
        html = setup_carbon.render_setup_radarr(
            "http://radarr.invalid:7878", "key123", ["ga"]
        )
        self.assertIn('action="/api/radarr/save"', html)
        self.assertIn('value="http://radarr.invalid:7878"', html)
        self.assertIn('value="key123"', html)
        self.assertIn("required", html)

    def test_sonarr_renders_form(self):
        html = setup_carbon.render_setup_sonarr(
            "http://sonarr.invalid:8989", "key456", ["gb"]
        )
        self.assertIn('action="/api/sonarr/save"', html)
        self.assertIn('value="http://sonarr.invalid:8989"', html)
        self.assertIn('value="key456"', html)


class SetupJDownloaderCarbonRenderTests(unittest.TestCase):
    def test_renders_verify_and_device_sections(self):
        html = setup_carbon.render_setup_jdownloader()
        self.assertIn('data-action="setup-jd-verify"', html)
        self.assertIn('id="setup-jd-device-tile" hidden', html)
        self.assertIn('action="/api/store_jdownloader"', html)
        self.assertIn('id="jd-hidden-user"', html)
        self.assertIn('id="jd-hidden-pass"', html)


class SetupPagesUseWideStatusCardTests(unittest.TestCase):
    """The shared `_shell()` wrapper every render_setup_*() goes through
    must opt into render_carbon_simple_page's `wide=True` card. The 440px
    default
    measurably cramped these forms once it started actually applying at
    desktop widths (see the CSS unclosed-block fix) - live browser
    measurement on the Hostnames page showed the hostname `<input>` squeezed
    to ~190px and the inline credentials panel to ~150px wide at 1280px
    viewport before this fix; both now render at their full row width
    (760px card minus tile padding). Login and every status/error page
    (the other `render_carbon_simple_page` callers) are NOT touched by this
    change and keep the narrow 440px default - see
    `tests/test_carbon_templates.py`'s `RenderCarbonSimplePageWideCardTests`
    for that half of the pin.
    """

    def test_shell_passes_wide_true_to_render_carbon_simple_page(self):
        with mock.patch.object(
            setup_carbon, "render_carbon_simple_page", return_value="<html></html>"
        ) as mock_render:
            setup_carbon._shell("Title", "<p>content</p>")
        mock_render.assert_called_once()
        _args, kwargs = mock_render.call_args
        self.assertIs(kwargs.get("wide"), True)

    def test_every_simple_setup_page_carries_the_wide_card_class(self):
        # End-to-end proof (not just the _shell() call-arg check above) that
        # the wide modifier class actually reaches the rendered document,
        # for the setup pages that need no extra fixture wiring.
        renders = {
            "path": setup_carbon.render_setup_path("/opt/quasarr"),
            "arr_client": setup_carbon.render_setup_arr_client(["ga"], ["gb"]),
            "radarr": setup_carbon.render_setup_radarr(
                "http://radarr.invalid", "key", ["ga"]
            ),
            "sonarr": setup_carbon.render_setup_sonarr(
                "http://sonarr.invalid", "key", ["gb"]
            ),
            "jdownloader": setup_carbon.render_setup_jdownloader(),
        }
        for name, html in renders.items():
            with self.subTest(page=name):
                self.assertIn('class="cds-status-card cds-status-card--wide"', html)

    def test_hostnames_page_carries_the_wide_card_class(self):
        settings_config = {"Settings": {"hostnames_url": ""}}
        with (
            mock.patch.object(
                setup_carbon, "build_hostname_rows", return_value=HOSTNAME_ROWS
            ),
            mock.patch.object(
                setup_carbon, "Config", side_effect=_config_factory(settings_config)
            ),
            mock.patch.object(
                setup_carbon, "DataBase", side_effect=_database_factory({})
            ),
        ):
            html = setup_carbon.render_setup_hostnames(_shared_state())
        self.assertIn('class="cds-status-card cds-status-card--wide"', html)

    def test_hostname_credentials_page_carries_the_wide_card_class(self):
        with (
            mock.patch.object(
                setup_carbon,
                "Config",
                side_effect=_config_factory({"FlareSolverr": {"url": ""}}),
            ),
            mock.patch.object(
                setup_carbon,
                "get_source_metadata",
                return_value={"ga": {"requires_flaresolverr": False}},
            ),
        ):
            html = setup_carbon.render_setup_hostname_credentials(
                _shared_state(), "GA", "ga-fixture.invalid"
            )
        self.assertIn('class="cds-status-card cds-status-card--wide"', html)

    def test_flaresolverr_page_carries_the_wide_card_class(self):
        with mock.patch.object(
            setup_carbon,
            "Config",
            side_effect=_config_factory({"FlareSolverr": {"url": ""}}),
        ):
            html = setup_carbon.render_setup_flaresolverr(_shared_state())
        self.assertIn('class="cds-status-card cds-status-card--wide"', html)


# ---------------------------------------------------------------------------
# Route-dispatch tests: real Bottle apps built by the eight *_config()
# functions, Server replaced so serve_temporarily() never blocks.
# ---------------------------------------------------------------------------


class SetupRouteDispatchTestsBase(unittest.TestCase):
    """``*_config()`` registers Bottle route closures that read module
    globals (``Config``, ``DataBase``, ``get_source_metadata``, ...) fresh on
    every request, not just at registration time - a ``with mock.patch(...):``
    block that exits before the later ``wsgi_request()`` call would silently
    un-patch them before the route ever runs. Patches here are started with
    ``addCleanup`` instead, so they stay active for the whole test method
    (mirrors ``test_carbon_route_dispatch.py``'s ``_make_app`` comment).
    """

    def setUp(self):
        patcher = mock.patch(
            "quasarr.providers.page_dispatch.carbon_assets_available",
            return_value=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _enter(self, *patchers):
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)


class PathRouteDispatchTests(SetupRouteDispatchTestsBase):
    def _build_app(self):
        from quasarr.storage.setup import path as path_module

        with mock.patch.object(path_module, "Server", CapturingServer):
            path_module.path_config(_shared_state())
        return CapturingServer.app

    def test_classic_route_unchanged(self):
        app = self._build_app()
        status, _headers, body = wsgi_request(app, query="ui=classic")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn('id="config_path"', text)
        self.assertIn('onsubmit="return handleSubmit(this)"', text)
        self.assertNotIn("<!doctype html>", text)

    def test_carbon_route_renders_standalone_document(self):
        app = self._build_app()
        status, headers, body = wsgi_request(app, query="ui=carbon")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn("<!doctype html>", text)
        self.assertIn('action="/api/config"', text)
        self.assertTrue(
            any(name.lower() == "content-security-policy" for name, _ in headers)
        )

    def test_classic_via_quasarr_ui_env_var(self):
        """Closes a coverage gap: QUASARR_UI=classic exercises a different
        precedence level than the `?ui=classic` query param exercised above
        (env wins over query, cookie, and cached preference per
        ui_preference.get_active_ui()). No query string is passed here,
        proving the env var alone selects Classic.
        """
        app = self._build_app()
        with mock.patch.dict(os.environ, {"QUASARR_UI": "classic"}, clear=True):
            status, _headers, body = wsgi_request(app)
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn('id="config_path"', text)
        self.assertIn('onsubmit="return handleSubmit(this)"', text)
        self.assertNotIn("<!doctype html>", text)


class HostnamesRouteDispatchTests(SetupRouteDispatchTestsBase):
    def _build_app(self):
        from quasarr.storage.setup import hostnames as hostnames_module

        self._enter(
            mock.patch.object(hostnames_module, "Server", CapturingServer),
            mock.patch.object(
                hostnames_module, "Config", side_effect=_config_factory({})
            ),
            mock.patch.object(
                hostnames_module, "DataBase", side_effect=_database_factory({})
            ),
            mock.patch.object(
                hostnames_module, "get_all_hostname_issues", return_value={}
            ),
            mock.patch.object(hostnames_module, "get_source_metadata", return_value={}),
            mock.patch.object(
                hostnames_module, "get_login_required_hostnames", return_value=[]
            ),
            mock.patch.object(
                hostnames_module, "get_radarr_required_hostnames", return_value=[]
            ),
            mock.patch.object(
                hostnames_module, "get_sonarr_required_hostnames", return_value=[]
            ),
            mock.patch(
                "quasarr.storage.setup.radarr.is_radarr_configured",
                return_value=True,
            ),
            mock.patch(
                "quasarr.storage.setup.sonarr.is_sonarr_configured",
                return_value=True,
            ),
            mock.patch.object(setup_carbon, "Config", side_effect=_config_factory({})),
            mock.patch.object(
                setup_carbon, "DataBase", side_effect=_database_factory({})
            ),
        )
        hostnames_module.hostnames_config(_shared_state(sites=["GA"]))
        return CapturingServer.app

    def test_classic_route_unchanged(self):
        app = self._build_app()
        status, _headers, body = wsgi_request(app, query="ui=classic")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn("take a closer look at", text)
        self.assertIn('id="ga"', text)
        self.assertNotIn("<!doctype html>", text)

    def test_carbon_route_renders_standalone_document(self):
        app = self._build_app()
        status, _headers, body = wsgi_request(app, query="ui=carbon")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn("<!doctype html>", text)
        self.assertIn('data-hostname-id="ga"', text)

    def test_get_api_hostnames_route_exists_for_details_panel_fetch(self):
        """Regression pin (caught by a live browser smoke test, not the
        WSGI-level tests above): bootstrapCarbonSetupFlows' Details panel
        calls GET /api/hostnames - the setup app registered only the POST
        (save) route before this fix, so opening Details 405'd in a real
        browser even though every server-side unit test passed.
        """
        import json

        app = self._build_app()
        status, headers, body = wsgi_request(app, path="/api/hostnames")
        self.assertEqual(status.split()[0], "200")
        content_types = [v for k, v in headers if k.lower() == "content-type"]
        self.assertTrue(any("json" in v for v in content_types))
        payload = json.loads(body.decode("utf-8"))
        self.assertIn("hostnames", payload)
        self.assertEqual(payload["hostnames"][0]["id"], "ga")
        self.assertNotIn("password", payload["hostnames"][0])
        self.assertNotIn("user", payload["hostnames"][0])


class HostnameCredentialsRouteDispatchTests(SetupRouteDispatchTestsBase):
    def _build_app(self):
        from quasarr.storage.setup import hostnames as hostnames_module

        self._enter(
            mock.patch.object(hostnames_module, "Server", CapturingServer),
            mock.patch.object(
                hostnames_module,
                "Config",
                side_effect=_config_factory({"FlareSolverr": {"url": ""}}),
            ),
            mock.patch.object(hostnames_module, "get_source_metadata", return_value={}),
            mock.patch.object(
                setup_carbon,
                "Config",
                side_effect=_config_factory({"FlareSolverr": {"url": ""}}),
            ),
            mock.patch.object(setup_carbon, "get_source_metadata", return_value={}),
        )
        hostnames_module.hostname_credentials_config(
            _shared_state(), "ga", "ga-fixture.invalid"
        )
        return CapturingServer.app

    def test_classic_route_unchanged(self):
        app = self._build_app()
        status, _headers, body = wsgi_request(app, query="ui=classic")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn('id="credentialsForm"', text)
        self.assertNotIn("<!doctype html>", text)

    def test_carbon_route_renders_standalone_document(self):
        app = self._build_app()
        status, _headers, body = wsgi_request(app, query="ui=carbon")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn("<!doctype html>", text)
        self.assertIn('action="/api/credentials/GA"', text)


class FlareSolverrRouteDispatchTests(SetupRouteDispatchTestsBase):
    def _build_app(self):
        from quasarr.storage.setup import flaresolverr as flaresolverr_module

        self._enter(
            mock.patch.object(flaresolverr_module, "Server", CapturingServer),
            mock.patch.object(
                flaresolverr_module,
                "Config",
                side_effect=_config_factory({"FlareSolverr": {"url": ""}}),
            ),
            mock.patch.object(
                flaresolverr_module, "DataBase", side_effect=_database_factory({})
            ),
            mock.patch.object(
                setup_carbon,
                "Config",
                side_effect=_config_factory({"FlareSolverr": {"url": ""}}),
            ),
        )
        flaresolverr_module.flaresolverr_config(_shared_state())
        return CapturingServer.app

    def test_classic_route_unchanged(self):
        app = self._build_app()
        status, _headers, body = wsgi_request(app, query="ui=classic")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn('id="skipBtn"', text)
        self.assertNotIn("<!doctype html>", text)

    def test_carbon_route_renders_standalone_document(self):
        app = self._build_app()
        status, _headers, body = wsgi_request(app, query="ui=carbon")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn("<!doctype html>", text)
        self.assertIn('data-action="setup-flaresolverr-skip"', text)


class ArrClientRouteDispatchTests(SetupRouteDispatchTestsBase):
    def _build_server_capture(self):
        from quasarr.storage.setup import arr as arr_module

        self._enter(mock.patch.object(arr_module, "Server", CapturingServer))
        arr_module.select_arr_client_config(_shared_state(), ["ga"], ["gb"])
        return CapturingServer.app

    def test_classic_route_unchanged(self):
        app = self._build_server_capture()
        status, _headers, body = wsgi_request(app, query="ui=classic")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn('value="radarr"', text)
        self.assertNotIn("<!doctype html>", text)

    def test_carbon_route_renders_standalone_document(self):
        app = self._build_server_capture()
        status, _headers, body = wsgi_request(app, query="ui=carbon")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn("<!doctype html>", text)
        self.assertIn('value="sonarr"', text)


class RadarrRouteDispatchTests(SetupRouteDispatchTestsBase):
    def _build_app(self):
        from quasarr.storage.setup import radarr as radarr_module

        self._enter(
            mock.patch.object(radarr_module, "Server", CapturingServer),
            mock.patch.object(radarr_module, "Config", side_effect=_config_factory({})),
        )
        radarr_module.radarr_config(_shared_state(), ["ga"])
        return CapturingServer.app

    def test_classic_route_unchanged(self):
        app = self._build_app()
        status, _headers, body = wsgi_request(app, query="ui=classic")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn('id="submitBtn"', text)
        self.assertNotIn("<!doctype html>", text)

    def test_carbon_route_renders_standalone_document(self):
        app = self._build_app()
        status, _headers, body = wsgi_request(app, query="ui=carbon")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn("<!doctype html>", text)
        self.assertIn('action="/api/radarr/save"', text)


class SonarrRouteDispatchTests(SetupRouteDispatchTestsBase):
    def _build_app(self):
        from quasarr.storage.setup import sonarr as sonarr_module

        self._enter(
            mock.patch.object(sonarr_module, "Server", CapturingServer),
            mock.patch.object(sonarr_module, "Config", side_effect=_config_factory({})),
        )
        sonarr_module.sonarr_config(_shared_state(), ["gb"])
        return CapturingServer.app

    def test_classic_route_unchanged(self):
        app = self._build_app()
        status, _headers, body = wsgi_request(app, query="ui=classic")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn('id="submitBtn"', text)
        self.assertNotIn("<!doctype html>", text)

    def test_carbon_route_renders_standalone_document(self):
        app = self._build_app()
        status, _headers, body = wsgi_request(app, query="ui=carbon")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn("<!doctype html>", text)
        self.assertIn('action="/api/sonarr/save"', text)


class JDownloaderRouteDispatchTests(SetupRouteDispatchTestsBase):
    def _build_app(self):
        from quasarr.storage.setup import jdownloader as jdownloader_module

        with mock.patch.object(jdownloader_module, "Server", CapturingServer):
            jdownloader_module.jdownloader_config(_shared_state())
        return CapturingServer.app

    def test_classic_route_unchanged(self):
        app = self._build_app()
        status, _headers, body = wsgi_request(app, query="ui=classic")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn('id="verifyForm"', text)
        self.assertNotIn("<!doctype html>", text)

    def test_carbon_route_renders_standalone_document(self):
        app = self._build_app()
        status, _headers, body = wsgi_request(app, query="ui=carbon")
        text = body.decode("utf-8")
        self.assertEqual(status.split()[0], "200")
        self.assertIn("<!doctype html>", text)
        self.assertIn('data-action="setup-jd-verify"', text)


# ---------------------------------------------------------------------------
# Static asset registration: every temporary app must serve
# /static/** through the common setup_auth() wiring - this pins it stays
# wired for a representative setup app.
# ---------------------------------------------------------------------------


class SetupStaticAssetsRegisteredTests(unittest.TestCase):
    def test_path_app_serves_carbon_assets_unauthenticated(self):
        from quasarr.storage.setup import path as path_module

        with mock.patch.object(path_module, "Server", CapturingServer):
            path_module.path_config(_shared_state())
        app = CapturingServer.app

        status, headers, body = wsgi_request(app, path="/static/carbon.css")
        self.assertEqual(status.split()[0], "200")
        self.assertGreater(len(body), 0)
        content_types = [v for k, v in headers if k.lower() == "content-type"]
        self.assertTrue(any("css" in v for v in content_types))


# ---------------------------------------------------------------------------
# carbon.js structural pins for the new bootstrapCarbonSetupFlows IIFE.
# ---------------------------------------------------------------------------


def javascript_function_body(source, name):
    """Brace-matched body of a named `function <name>(...) { ... }`
    declaration - the house standard from test_carbon_downloads.py /
    test_deferred_packages_api.py, copied here per the no-shared-helpers
    convention (each test file defines its own).
    """
    start = source.index(f"function {name}(")
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


class CarbonJsSetupFlowsScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pathlib import Path

        static_root = Path(__file__).resolve().parent.parent / "quasarr" / "static"
        cls.js = (static_root / "carbon.js").read_text(encoding="utf-8")

    def test_marker_present_exactly_once(self):
        marker = "(function bootstrapCarbonSetupFlows() {"
        self.assertEqual(self.js.count(marker), 1)

    def test_every_iife_marker_present_exactly_once(self):
        """General regression pin: adding a new IIFE must never duplicate an
        existing marker, and every marker this file is known to ship must
        still appear exactly once.
        """
        markers = [
            "(function bootstrapCarbonUi() {",
            "(function bootstrapCarbonDashboardAndSettings() {",
            "(function bootstrapCarbonHostnamesAndCategories() {",
            "(function bootstrapCarbonCaptcha() {",
            "(function bootstrapCarbonStatusPages() {",
            "(function bootstrapCarbonSetupFlows() {",
            "(function bootstrapCarbonTime() {",
            "(function bootstrapCarbonDownloads() {",
        ]
        for marker in markers:
            with self.subTest(marker=marker):
                self.assertEqual(self.js.count(marker), 1)

    def test_setup_flows_is_before_time_and_downloads(self):
        """test_carbon_downloads.py documents bootstrapCarbonTime then
        bootstrapCarbonDownloads as the last two IIFEs with nothing after
        them (it slices marker-to-EOF on that assumption) - this new IIFE
        must sit before that pair, not after.
        """
        setup_idx = self.js.index("(function bootstrapCarbonSetupFlows() {")
        time_idx = self.js.index("(function bootstrapCarbonTime() {")
        downloads_idx = self.js.index("(function bootstrapCarbonDownloads() {")
        self.assertLess(setup_idx, time_idx)
        self.assertLess(time_idx, downloads_idx)

    def test_all_setup_actions_present(self):
        """Each setup data-action has a matching `case '<action>':` label in
        the new IIFE's dispatch switch (the HTML side - `data-action="..."`
        attributes - lives in the Python-rendered markup, pinned directly by
        the render-function tests above).
        """
        for action in (
            "setup-hostnames-import",
            "setup-hostname-toggle-details",
            "setup-hostname-credentials-check",
            "setup-credentials-skip",
            "setup-flaresolverr-skip",
            "setup-jd-verify",
        ):
            with self.subTest(action=action):
                self.assertIn(f"case '{action}':", self.js)

    def test_no_remote_urls_in_new_iife(self):
        start = self.js.index("(function bootstrapCarbonSetupFlows() {")
        end = self.js.index("(function bootstrapCarbonTime() {")
        slice_ = self.js[start:end]
        self.assertNotIn("http://", slice_)
        self.assertNotIn("https://", slice_)

    def test_import_body_hits_the_import_endpoint(self):
        body = javascript_function_body(self.js, "performSetupHostnamesImport")
        self.assertIn("/api/hostnames/import-url", body)
        self.assertIn("applyImportedHostnames(", body)

    def test_toggle_details_body_fetches_rows_and_writes_text_content(self):
        body = javascript_function_body(self.js, "toggleSetupHostnameDetails")
        self.assertIn("fetchHostnameRows()", body)
        self.assertIn("textContent", body)
        self.assertNotIn("innerHTML", body)

    def test_credentials_check_body_hits_the_check_endpoint(self):
        body = javascript_function_body(self.js, "checkSetupHostnameCredentials")
        self.assertIn("/api/hostnames/check-credentials/", body)

    def test_credentials_check_success_uses_the_status_component_not_a_tag(self):
        """The dense-row redesign replaced the row's status .cds-tag pill
        with the dot+text status component everywhere else - this in-place
        update after a successful check must match, not reintroduce a
        `.cds-tag cds-tag--green` pill next to it.
        """
        body = javascript_function_body(self.js, "checkSetupHostnameCredentials")
        self.assertIn("cds-status cds-status--success", body)
        self.assertIn("cds-status__dot", body)
        self.assertNotIn("cds-tag cds-tag--green", body)

    def test_credentials_skip_body_posts_then_navigates(self):
        body = javascript_function_body(self.js, "performCredentialsSkip")
        self.assertIn("/skip'", body)
        self.assertIn("method: 'POST'", body)
        self.assertIn("window.location.href = '/skip-success'", body)

    def test_flaresolverr_skip_body_posts_then_navigates(self):
        body = javascript_function_body(self.js, "performFlaresolverrSkip")
        self.assertIn("/api/flaresolverr/skip", body)
        self.assertIn("method: 'POST'", body)
        self.assertIn("window.location.href = '/skip-success'", body)

    def test_jd_verify_body_disables_visible_fields_on_success(self):
        """Regression pin: after a successful
        verify, only the hidden jd-hidden-user/jd-hidden-pass fields are
        submitted by the device form - the visible jd-user/jd-pass fields
        must be disabled so an edit made after verification (which the
        hidden copies never see again, and which the now-hidden Verify
        button can no longer re-sync) cannot be silently lost.
        """
        body = javascript_function_body(self.js, "performJdVerify")
        self.assertIn("userField.disabled = true", body)
        self.assertIn("passField.disabled = true", body)
        # The disable must happen in the success branch, after the hidden
        # copies are populated - not before verification even starts.
        hidden_copy_index = body.index("hiddenPass.value = pass")
        disable_index = body.index("userField.disabled = true")
        self.assertLess(hidden_copy_index, disable_index)

    def test_submit_guard_body_matches_classic_semantics(self):
        """Regression pin: every native setup
        <form> lost Classic's submit-guard + button-disable behavior when
        moved to Carbon. onSetupFormSubmit restores it: a form must opt in
        via data-guard-submit, a second submit of the same form is
        cancelled outright (mirrors Classic's `if (formSubmitted) return
        false;`), and the submitter is disabled/relabelled only via a
        deferred macrotask so a named submit button's value (e.g. the *arr
        selector's name="client" value="radarr") is not dropped from the
        request by disabling it synchronously inside its own submit event.
        """
        body = javascript_function_body(self.js, "onSetupFormSubmit")
        self.assertIn("data-guard-submit", body)
        self.assertIn("event.preventDefault()", body)
        self.assertIn("window.setTimeout(", body)
        self.assertIn("submitter.disabled = true", body)
        self.assertIn("Saving...", body)

    def test_submit_guard_is_wired_into_dom_content_loaded(self):
        self.assertIn(
            "document.addEventListener('submit', onSetupFormSubmit);", self.js
        )


if __name__ == "__main__":
    unittest.main()
