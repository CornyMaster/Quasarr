# -*- coding: utf-8 -*-

import importlib
import io
import os
import unittest
from io import BytesIO
from unittest import mock

from bottle import Bottle


class CarbonCspDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dispatch = importlib.import_module("quasarr.providers.page_dispatch")

    def _request(self, app, path="/"):
        if "?" in path:
            path, query = path.split("?", 1)
        else:
            query = ""

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

    def test_render_page_carbon_sets_exact_csp(self):
        app = Bottle()

        @app.get("/")
        def route():
            return self.dispatch.render_page(
                "dashboard",
                lambda: "<html><body>carbon</body></html>",
                lambda: "classic",
            )

        with (
            mock.patch.dict(os.environ, {"QUASARR_UI": "carbon"}, clear=True),
            mock.patch.object(
                self.dispatch, "carbon_assets_available", return_value=True
            ),
        ):
            status, headers, body = self._request(app)

        self.assertEqual(status.split()[0], "200")
        self.assertEqual(body, b"<html><body>carbon</body></html>")
        self.assertEqual(
            self._header_values(headers, "Content-Security-Policy"),
            [self.dispatch.CSP_POLICY],
        )

    def test_render_page_classic_fallback_keeps_body_bytes_and_no_csp(self):
        app = Bottle()
        classic_body = "classic-unchanged-<>&\"'"

        @app.get("/")
        def route():
            return self.dispatch.render_page(
                "dashboard",
                lambda: "carbon",
                lambda: classic_body,
            )

        with (
            mock.patch.dict(os.environ, {"QUASARR_UI": "carbon"}, clear=True),
            mock.patch.object(
                self.dispatch, "carbon_assets_available", return_value=False
            ),
        ):
            status, headers, body = self._request(app)

        self.assertEqual(status.split()[0], "200")
        self.assertEqual(body.decode("utf-8"), classic_body)
        self.assertEqual(self._header_values(headers, "Content-Security-Policy"), [])


if __name__ == "__main__":
    unittest.main()
