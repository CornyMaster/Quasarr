"""Contracts for page dispatch, fallback, and Bottle response safety."""

# -*- coding: utf-8 -*-

import importlib
import io
import os
import sys
import unittest
from io import BytesIO
from unittest import mock

from bottle import Bottle, HTTPError, HTTPResponse


class PageDispatchTests(unittest.TestCase):
    def setUp(self):
        self.mod = importlib.import_module("quasarr.providers.page_dispatch")
        self._env_backup = dict(os.environ)
        self._cwd_backup = os.getcwd()
        self._stdout_backup = sys.stdout

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)
        os.chdir(self._cwd_backup)
        sys.stdout = self._stdout_backup

    def _request(self, app, path="/", headers=None):
        headers = dict(headers or {})
        query = ""
        if "?" in path:
            path, query = path.split("?", 1)
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
            "wsgi.errors": io.StringIO(),
        }
        for key, value in headers.items():
            environ[key] = value

        captured = {}

        def start_response(status, response_headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = list(response_headers)

        body = b"".join(app(environ, start_response))
        return (
            captured.get("status", "500 Internal Server Error"),
            captured.get("headers", []),
            body,
        )

    def _header_values(self, headers, name):
        lowered = name.lower()
        return [value for key, value in headers if key.lower() == lowered]

    def test_log_context_uses_concise_page_dispatch_marker(self):
        from quasarr.providers.log import _contexts_to_str

        context, source = _contexts_to_str(["quasarr", "providers", "page_dispatch"])

        self.assertEqual("🔌🧭", context)
        self.assertEqual("", source)

    def test_contract_constants_are_stable(self):
        self.assertEqual(self.mod.RenderResult, str | HTTPResponse)
        self.assertEqual(
            self.mod.CSP_POLICY,
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'",
        )

    def test_recovery_path_env_forces_carbon_and_runtime_error_falls_back_once(self):
        warnings = []
        carbon_calls = []
        classic_calls = []

        def carbon():
            carbon_calls.append("carbon")
            raise RuntimeError("boom")

        def classic():
            classic_calls.append("classic")
            return "classic"

        app = Bottle()

        @app.get("/")
        def route():
            return self.mod.render_page("dashboard", carbon, classic)

        with (
            mock.patch.dict(os.environ, {"QUASARR_UI": "carbon"}, clear=True),
            mock.patch.object(
                self.mod, "warn", side_effect=lambda msg: warnings.append(msg)
            ),
        ):
            status, headers, body = self._request(app)

        self.assertEqual(status.split()[0], "200")
        self.assertEqual(body.decode("utf-8"), "classic")
        self.assertEqual(carbon_calls, ["carbon"])
        self.assertEqual(classic_calls, ["classic"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("dashboard", warnings[0])
        self.assertIn("RuntimeError", warnings[0])
        self.assertEqual(self._header_values(headers, "Content-Security-Policy"), [])

    def test_recovery_path_query_forces_carbon(self):
        app = Bottle()

        @app.get("/")
        def route():
            return self.mod.render_page(
                "settings",
                lambda: "carbon",
                lambda: self.fail("classic should not run"),
            )

        with mock.patch.dict(os.environ, {}, clear=True):
            status, headers, body = self._request(app, "/?ui=carbon")

        self.assertEqual(status.split()[0], "200")
        self.assertEqual(body.decode("utf-8"), "carbon")
        self.assertEqual(
            self._header_values(headers, "Content-Security-Policy"),
            [self.mod.CSP_POLICY],
        )

    def test_recovery_path_cookie_route_forces_carbon(self):
        app = Bottle()

        @app.get("/")
        def route():
            return self.mod.render_page(
                "downloads",
                lambda: "carbon",
                lambda: self.fail("classic should not run"),
            )

        with mock.patch.dict(os.environ, {}, clear=True):
            status, headers, body = self._request(
                app, headers={"HTTP_COOKIE": "quasarr_ui=carbon"}
            )

        self.assertEqual(status.split()[0], "200")
        self.assertEqual(body.decode("utf-8"), "carbon")
        self.assertEqual(
            self._header_values(headers, "Content-Security-Policy"),
            [self.mod.CSP_POLICY],
        )

    def test_classic_exception_passes_through_and_classic_runs_once(self):
        classic_calls = []

        def classic():
            classic_calls.append("classic")
            raise ValueError("classic failed")

        with mock.patch.dict(os.environ, {"QUASARR_UI": "classic"}, clear=True):
            with self.assertRaisesRegex(ValueError, "classic failed"):
                self.mod.render_page(
                    "index",
                    lambda: self.fail("carbon should not run"),
                    classic,
                )

        self.assertEqual(classic_calls, ["classic"])

    def test_missing_assets_restores_response_before_fallback(self):
        app = Bottle()
        warnings = []
        carbon_calls = []
        classic_calls = []

        @app.get("/")
        def route():
            from bottle import response

            response.status = "206 Partial Content"
            response.content_type = "text/plain"
            response.add_header("X-Dupe", "a")
            response.add_header("X-Dupe", "b")

            def assets_available():
                response.status = "500 Internal Server Error"
                response.content_type = "application/json"
                response.set_header("X-Dupe", "mutated")
                return False

            def carbon():
                carbon_calls.append("carbon")
                return "carbon"

            def classic():
                classic_calls.append("classic")
                return "classic"

            with mock.patch.object(
                self.mod, "carbon_assets_available", assets_available
            ):
                return self.mod.render_page("assets", carbon, classic)

        with (
            mock.patch.dict(os.environ, {"QUASARR_UI": "carbon"}, clear=True),
            mock.patch.object(
                self.mod, "warn", side_effect=lambda msg: warnings.append(msg)
            ),
        ):
            status, headers, body = self._request(app)

        self.assertEqual(status, "206 Partial Content")
        self.assertEqual(body.decode("utf-8"), "classic")
        self.assertEqual(carbon_calls, [])
        self.assertEqual(classic_calls, ["classic"])
        self.assertEqual(self._header_values(headers, "X-Dupe"), ["a", "b"])
        self.assertEqual(self._header_values(headers, "Content-Type"), ["text/plain"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("assets", warnings[0])
        self.assertEqual(
            warnings[0], "Carbon assets missing for assets; serving Classic"
        )
        self.assertEqual(self._header_values(headers, "Content-Security-Policy"), [])

    def test_asset_check_exception_falls_back_to_classic(self):
        app = Bottle()
        classic_calls = []

        @app.get("/")
        def route():
            return self.mod.render_page(
                "asset-check",
                lambda: "carbon",
                lambda: classic_calls.append("classic") or "classic",
            )

        with (
            mock.patch.dict(os.environ, {"QUASARR_UI": "carbon"}, clear=True),
            mock.patch.object(
                self.mod, "carbon_assets_available", side_effect=RuntimeError("check")
            ),
        ):
            status, headers, body = self._request(app)

        self.assertEqual(status.split()[0], "200")
        self.assertEqual(body.decode("utf-8"), "classic")
        self.assertEqual(classic_calls, ["classic"])
        self.assertEqual(self._header_values(headers, "Content-Security-Policy"), [])

    def test_carbon_string_result_applies_csp_after_render_completion(self):
        app = Bottle()
        order = []

        @app.get("/")
        def route():
            from bottle import response

            def carbon():
                order.append("carbon")
                response.set_header("X-Order", "rendered")
                return "<html>carbon</html>"

            return self.mod.render_page("carbon", carbon, lambda: "classic")

        with mock.patch.dict(os.environ, {"QUASARR_UI": "carbon"}, clear=True):
            status, headers, body = self._request(app)

        self.assertEqual(status.split()[0], "200")
        self.assertEqual(body.decode("utf-8"), "<html>carbon</html>")
        self.assertEqual(order, ["carbon"])
        self.assertEqual(self._header_values(headers, "X-Order"), ["rendered"])
        self.assertEqual(
            self._header_values(headers, "Content-Security-Policy"),
            [self.mod.CSP_POLICY],
        )

    def test_carbon_httpresponse_gets_csp_without_mutating_global_response(self):
        app = Bottle()

        @app.get("/")
        def route():
            from bottle import response

            response.status = "203 Non-Authoritative Information"
            response.content_type = "text/plain"
            response.add_header("X-Dupe", "a")
            response.add_header("X-Dupe", "b")
            return self.mod.render_page(
                "carbon-httpresponse",
                lambda: HTTPResponse(body="ok", status=201, content_type="text/html"),
                lambda: "classic",
            )

        with mock.patch.dict(os.environ, {"QUASARR_UI": "carbon"}, clear=True):
            status, headers, body = self._request(app)

        self.assertEqual(status, "201 Created")
        self.assertEqual(body.decode("utf-8"), "ok")
        self.assertEqual(self._header_values(headers, "Content-Type"), ["text/html"])
        self.assertEqual(
            self._header_values(headers, "Content-Security-Policy"),
            [self.mod.CSP_POLICY],
        )

    def _assert_exception_fallback_restores_response(self, error):
        app = Bottle()
        classic_calls = []

        @app.get("/")
        def route():
            from bottle import response

            response.status = "206 Partial Content"
            response.content_type = "text/custom"
            response.add_header("X-Dupe", "a")
            response.add_header("X-Dupe", "b")

            def carbon():
                response.status = "500 Internal Server Error"
                response.content_type = "application/problem+json"
                response.set_header("X-Dupe", "mutated")
                raise error

            def classic():
                classic_calls.append("classic")
                return "classic"

            return self.mod.render_page("fallback", carbon, classic)

        with mock.patch.dict(os.environ, {"QUASARR_UI": "carbon"}, clear=True):
            status, headers, body = self._request(app)

        self.assertEqual(status, "206 Partial Content")
        self.assertEqual(body.decode("utf-8"), "classic")
        self.assertEqual(classic_calls, ["classic"])
        self.assertEqual(self._header_values(headers, "X-Dupe"), ["a", "b"])
        self.assertEqual(self._header_values(headers, "Content-Type"), ["text/custom"])
        self.assertEqual(self._header_values(headers, "Content-Security-Policy"), [])

    def test_carbon_runtime_error_and_import_error_restore_response_once(self):
        for exc in [RuntimeError("boom"), ImportError("missing")]:
            with self.subTest(exc=type(exc).__name__):
                self._assert_exception_fallback_restores_response(exc)

    def test_snapshot_failure_propagates_original_exception_not_unboundlocalerror(self):
        """`snapshot` must be bound before the try/except so a failure inside
        `_snapshot_response()` itself surfaces as the real underlying error
        instead of a secondary UnboundLocalError raised while the except
        clause tries to read a never-assigned local."""
        carbon_calls = []
        classic_calls = []

        def carbon():
            carbon_calls.append("carbon")
            return "carbon"

        def classic():
            classic_calls.append("classic")
            return "classic"

        with (
            mock.patch.dict(os.environ, {"QUASARR_UI": "carbon"}, clear=True),
            mock.patch.object(
                self.mod,
                "_snapshot_response",
                side_effect=RuntimeError("snapshot boom"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "snapshot boom"):
                self.mod.render_page("snapshot-failure", carbon, classic)

        self.assertEqual(carbon_calls, [])
        self.assertEqual(classic_calls, [])

    def test_raised_httperror_and_httpresponse_pass_through_unchanged(self):
        scenarios = [
            (
                "http-error",
                lambda: (_ for _ in ()).throw(HTTPError(418, "teapot")),
                "418",
            ),
            (
                "http-response",
                lambda: (_ for _ in ()).throw(
                    HTTPResponse(status=302, headers={"Location": "/target"})
                ),
                "302",
            ),
        ]
        for _label, carbon_fn, expected_status in scenarios:
            app = Bottle()
            classic_calls = []

            @app.get("/")
            def route(current_carbon_fn=carbon_fn, calls=classic_calls):
                def classic():
                    calls.append("classic")
                    return "classic"

                return self.mod.render_page(
                    "control-flow",
                    current_carbon_fn,
                    classic,
                )

            with mock.patch.dict(os.environ, {"QUASARR_UI": "carbon"}, clear=True):
                status, headers, _body = self._request(app)

            self.assertEqual(status.split()[0], expected_status)
            self.assertEqual(classic_calls, [])
            if expected_status == "302":
                self.assertEqual(self._header_values(headers, "Location"), ["/target"])


if __name__ == "__main__":
    unittest.main()
