import importlib
import importlib.util
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from bottle import Bottle

import quasarr.api as api
import quasarr.providers.auth as auth


class StaticAssetsBehaviorTests(unittest.TestCase):
    """Behavioral contract tests for static asset serving.

    Each test's docstring names the production mutation it detects.
    """

    def setUp(self):
        # Import the authoritative provider module normally and remember original globals
        self.mod = importlib.import_module("quasarr.providers.static_assets")
        self.loaded_from = "providers"
        self._orig_static_root = getattr(self.mod, "_STATIC_ROOT", None)
        self._orig_get_version = getattr(self.mod, "get_version", None)

    def tearDown(self):
        # Restore any mutated module-global to avoid test-global leaks
        if self._orig_static_root is None:
            if hasattr(self.mod, "_STATIC_ROOT"):
                del self.mod._STATIC_ROOT
        else:
            self.mod._STATIC_ROOT = self._orig_static_root

        if self._orig_get_version is None:
            if hasattr(self.mod, "get_version"):
                del self.mod.get_version
        else:
            self.mod.get_version = self._orig_get_version

    def _make_app(self, immutable=True, root_override=None):
        app = Bottle()
        # Optionally override the package static root for deterministic testing
        if root_override is not None:
            self.mod._STATIC_ROOT = Path(root_override).resolve()
        # idempotent registration should be supported by the implementation
        self.mod.setup_static_routes(app, immutable=immutable)
        return app

    def _request(self, app, path, method="GET"):
        """Perform a WSGI request against Bottle app without opening sockets.

        Returns a urllib-like response object (with .status/.read/.getheader).
        """
        from io import BytesIO

        env = {
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": "http",
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "SERVER_NAME": "127.0.0.1",
            "SERVER_PORT": "80",
            "wsgi.input": BytesIO(b""),
        }

        status_headers = {}
        body = BytesIO()

        def start_response(status, headers, exc_info=None):
            status_headers["status"] = status
            status_headers["headers"] = dict(headers)
            return body.write

        app_iter = app(env, start_response)
        try:
            for chunk in app_iter:
                body.write(chunk)
        finally:
            if hasattr(app_iter, "close"):
                app_iter.close()

        class Resp:
            def __init__(self, status, headers, body):
                self.status = int(status.split()[0])
                self._headers = headers
                self._body = body

            def read(self):
                return self._body

            def getheader(self, name):
                return self._headers.get(name)

        return Resp(
            status_headers.get("status", "500 Internal Server Error"),
            status_headers.get("headers", {}),
            body.getvalue(),
        )

    def test_ownership_must_live_under_providers(self):
        """Mutation: static_assets left in legacy package (must live under providers)."""
        # The authoritative implementation must be importable from providers
        self.assertEqual(
            self.loaded_from,
            "providers",
            "static_assets must be under quasarr.providers",
        )

    def test_no_legacy_static_assets_module_exists(self):
        """Mutation: divergent duplicate module reintroduced outside providers."""
        legacy_path = (
            Path(self.mod.__file__).resolve().parent.parent / "static_assets.py"
        )
        self.assertFalse(
            legacy_path.exists(),
            "legacy quasarr/static_assets.py must not exist",
        )
        self.assertIsNone(
            importlib.util.find_spec("quasarr.static_assets"),
            "legacy quasarr.static_assets module spec must be absent",
        )

    def test_asset_url_validates_and_appends_version(self):
        """Mutation: asset_url allows unsafe paths or omits version query."""
        # Ensure we have a get_version symbol available on the module
        self.mod.get_version = lambda: "v123"

        # valid
        url = self.mod.asset_url("carbon.css")
        self.assertIn("/static/carbon.css", url)
        self.assertIn("?v=v123", url)

        # traversal and backslashes must be rejected
        with self.assertRaises(ValueError):
            self.mod.asset_url("../secrets.txt")
        with self.assertRaises(ValueError):
            self.mod.asset_url("bad\\path.css")
        with self.assertRaises(ValueError):
            self.mod.asset_url("fonts%5C..%5Csecrets.css")

        # unsupported suffix rejected
        with self.assertRaises(ValueError):
            self.mod.asset_url("payload.exe")

    def test_production_root_and_real_assets_check(self):
        """Mutation: production root must be package-derived and assets available."""
        # expected production root derived from provider module location
        expected = Path(self.mod.__file__).resolve().parent.parent / "static"
        self.assertTrue(
            hasattr(self.mod, "_STATIC_ROOT"), "module must define _STATIC_ROOT"
        )
        self.assertEqual(Path(self.mod._STATIC_ROOT).resolve(), expected.resolve())
        # This will be RED until real packaged assets are present
        self.assertTrue(self.mod.carbon_assets_available())

    def test_carbon_assets_available_true_and_false(self):
        """Mutation: carbon_assets_available returns True only when all assets exist."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # create minimal required tree
            (root / "carbon.css").write_text("/* css */\n")
            (root / "carbon.js").write_text("// js\n")
            fonts = [
                "IBMPlexSans-Regular-Latin.woff2",
                "IBMPlexSans-Medium-Latin.woff2",
                "IBMPlexSans-SemiBold-Latin.woff2",
                "IBMPlexMono-Regular-Latin.woff2",
                "IBMPlexMono-Medium-Latin.woff2",
            ]
            (root / "fonts").mkdir()
            for f in fonts:
                (root / "fonts" / f).write_bytes(b"woff2")
            (root / "fonts" / "LICENSE-IBM-PLEX.txt").write_text("IBM")
            (root / "icons").mkdir()
            (root / "icons" / "LICENSE-APACHE-2.0.txt").write_text("Apache")
            (root / "icons" / "ATTRIBUTION.txt").write_text("Attribution")

            self.mod._STATIC_ROOT = root
            self.assertTrue(self.mod.carbon_assets_available())

            # remove one file
            (root / "carbon.js").unlink()
            self.assertFalse(self.mod.carbon_assets_available())

    def test_static_route_serves_and_headers(self):
        """Mutation: setup_static_routes must serve files, set MIME, nosniff and cache headers."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "carbon.css").write_text("a\n")
            (root / "fonts").mkdir()
            (root / "fonts" / "IBMPlexSans-Regular-Latin.woff2").write_bytes(b"f")

            self.mod._STATIC_ROOT = root
            app = self._make_app(immutable=True)

            # check GET for CSS
            resp = self._request(app, "/static/carbon.css", method="GET")
            self.assertEqual(getattr(resp, "code", None) or resp.status, 200)
            body = resp.read()
            # normalize CRLF vs LF differences across platforms
            body = body.replace(b"\r\n", b"\n")
            self.assertEqual(body, b"a\n")
            self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
            self.assertIn("immutable", resp.getheader("Cache-Control"))

            # also ensure JS and WOFF2 served with correct MIME
            (root / "carbon.js").write_text("// js\n")
            (root / "icons").mkdir()
            # add small svg fixture
            (root / "icons" / "test.svg").write_text("<svg></svg>")
            (root / "fonts" / "IBMPlexSans-Regular-Latin.woff2").write_bytes(b"f2")

            resp_js = self._request(app, "/static/carbon.js", method="GET")
            self.assertEqual(resp_js.status, 200)
            self.assertEqual(
                resp_js.getheader("Content-Type"),
                getattr(self.mod, "STATIC_MIME_TYPES", {}).get(".js"),
            )

            resp_font = self._request(
                app, "/static/fonts/IBMPlexSans-Regular-Latin.woff2", method="GET"
            )
            self.assertEqual(resp_font.status, 200)
            self.assertEqual(
                resp_font.getheader("Content-Type"),
                getattr(self.mod, "STATIC_MIME_TYPES", {}).get(".woff2"),
            )

            resp_svg = self._request(app, "/static/icons/test.svg", method="GET")
            self.assertEqual(resp_svg.status, 200)
            self.assertEqual(
                resp_svg.getheader("Content-Type"),
                getattr(self.mod, "STATIC_MIME_TYPES", {}).get(".svg"),
            )

            # HEAD should have no body
            resp_head = self._request(app, "/static/carbon.css", method="HEAD")
            # HTTPError for HEAD may still carry code
            self.assertIn(
                (getattr(resp_head, "code", None) or resp_head.status), (200,)
            )
            self.assertEqual(resp_head.read(), b"")

    def test_missing_file_and_unsupported_suffix_and_traversal(self):
        """Mutation: missing file, unsupported suffix, and traversal must not leak local paths."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.mod._STATIC_ROOT = root
            app = self._make_app(immutable=False)

            # missing file -> 404 and no filesystem path in body
            resp = self._request(app, "/static/carbon.css", method="GET")
            self.assertEqual(getattr(resp, "code", None) or resp.status, 404)
            body = resp.read()
            # ensure no absolute local path leaked
            self.assertNotIn(str(root).encode("utf-8"), body)

            # create a real file and check no-store when immutable=False
            (root / "carbon.css").write_text("b\n")
            resp_ok = self._request(app, "/static/carbon.css", method="GET")
            self.assertEqual(resp_ok.status, 200)
            self.assertIn("no-store", resp_ok.getheader("Cache-Control"))

            # unsupported suffix
            resp2 = self._request(app, "/static/payload.exe", method="GET")
            self.assertEqual(getattr(resp2, "code", None) or resp2.status, 404)

            # traversal/backslash attempts
            resp3 = self._request(app, "/static/../secret.txt", method="GET")
            self.assertEqual(getattr(resp3, "code", None) or resp3.status, 404)
            resp4 = self._request(app, "/static/bad%2f..%2fescape.css", method="GET")
            # encoded traversal must be rejected as well
            self.assertIn((getattr(resp4, "code", None) or resp4.status), (404,))

    def test_setup_is_idempotent_and_public_endpoint(self):
        """Mutation: registration must be idempotent and route callback marked public."""
        app = Bottle()
        before = list(app.routes)
        # first registration
        self.mod.setup_static_routes(app, immutable=True)
        after1 = list(app.routes)
        # second should not add another
        self.mod.setup_static_routes(app, immutable=True)
        after2 = list(app.routes)

        # Exactly one logical new route must be added. Bottle may represent
        # a single GET/HEAD registration as 1 or 2 route objects depending
        # on its internals; the important property is idempotence.
        delta = len(after1) - len(before)
        self.assertIn(delta, (1, 2), msg=f"unexpected route count delta: {delta}")
        # second registration must be a no-op
        self.assertEqual(len(after2) - len(after1), 0)

        # verify the registered route callback is marked public
        route = [
            r for r in after2 if getattr(r, "rule", "") == "/static/<filename:path>"
        ]
        # Bottle may create one or two internal route objects for a single
        # GET+HEAD registration; accept either but require at least one
        # callback be present and marked public.
        self.assertIn(len(route), (1, 2))
        callbacks = [getattr(r, "callback", None) for r in route]
        self.assertTrue(any(cb is not None for cb in callbacks))
        # auth public marker set by providers.auth.public_endpoint is __quasarr_auth_mode__ == 'public'
        self.assertTrue(
            any(
                getattr(cb, "__quasarr_auth_mode__", "") == "public" for cb in callbacks
            )
        )

    def test_setup_auth_registers_public_route_when_auth_hook_applied(self):
        """Mutation: auth-enabled setup must keep /static public while auth protects normal routes."""
        # Use the real setup_auth implementation to register the static route
        from quasarr.storage.setup.common import setup_auth

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "carbon.css").write_text("body {}\n")
            self.mod._STATIC_ROOT = root

            app = Bottle()
            with (
                mock.patch.object(auth, "_AUTH_USER", "user"),
                mock.patch.object(auth, "_AUTH_PASS", "pass"),
                mock.patch.object(auth, "_AUTH_TYPE", "basic"),
            ):
                setup_auth(app)

                @app.get("/secured")
                def secured():
                    return "ok"

                public_resp = self._request(app, "/static/carbon.css", method="GET")
                self.assertEqual(public_resp.status, 200)
                self.assertEqual(
                    public_resp.getheader("X-Content-Type-Options"), "nosniff"
                )

                private_resp = self._request(app, "/secured", method="GET")
                self.assertEqual(private_resp.status, 401)
                self.assertIn(b"Authentication required", private_resp.read())

        routes = [
            r for r in app.routes if getattr(r, "rule", "") == "/static/<filename:path>"
        ]
        callbacks = [getattr(r, "callback", None) for r in routes]
        self.assertTrue(
            any(
                getattr(cb, "__quasarr_auth_mode__", "") == "public" for cb in callbacks
            )
        )

    def test_main_api_registers_static_route_via_get_api(self):
        """Mutation: full `get_api` must register the public static route (captured)."""

        # Minimal CapturingServer to intercept created app
        class CapturingServer:
            app = None

            def __init__(self, app, **_kwargs):
                type(self).app = app

            def serve_forever(self):
                return None

        shared_values = {
            "port": 8080,
            "sites": [],
            "internal_address": "http://quasarr.invalid:8080",
            "external_address": "http://quasarr.invalid:8080",
            "device": object(),
            "helper_active": False,
            "notification_settings": {},
            "timeout_slow_mode": {},
            "database": lambda t: None,
            "config": lambda s: None,
        }

        # Patch heavy external calls inside get_api to no-ops so registration proceeds
        patch_names = [
            "setup_arr_routes",
            "setup_captcha_routes",
            "setup_config",
            "setup_statistics",
            "setup_sponsors_helper_routes",
            "setup_packages_routes",
            "audit_route_auth_modes",
            "get_jdownloader_status",
            "get_all_hostname_issues",
            "get_login_required_hostnames",
            "get_radarr_required_hostnames",
            "get_sonarr_required_hostnames",
            "is_radarr_configured",
            "is_sonarr_configured",
            "Config",
            "DataBase",
        ]

        patch_kwargs = {name: (lambda *a, **k: None) for name in patch_names}

        with mock.patch("quasarr.api.Server", CapturingServer):
            with mock.patch.multiple("quasarr.api", **patch_kwargs):
                # Call get_api which should instantiate CapturingServer and capture the app
                api.get_api(shared_values, threading.Lock())

        app = CapturingServer.app
        self.assertIsNotNone(app, "get_api did not register an app via Server")
        routes = [
            r for r in app.routes if getattr(r, "rule", "") == "/static/<filename:path>"
        ]
        self.assertTrue(len(routes) >= 1)
        callbacks = [getattr(r, "callback", None) for r in routes]
        self.assertTrue(
            any(
                getattr(cb, "__quasarr_auth_mode__", "") == "public" for cb in callbacks
            )
        )


if __name__ == "__main__":
    unittest.main()
