# -*- coding: utf-8 -*-

import json
import sys
import unittest
from base64 import urlsafe_b64encode
from io import BytesIO
from unittest.mock import patch
from urllib.parse import quote

from bottle import Bottle

from quasarr.api.captcha import setup_captcha_routes
from quasarr.providers import shared_state


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


def build_package(pkg_id, url, mirror, password=""):
    return (
        pkg_id,
        json.dumps(
            {
                "title": "Synthetic.Release.2024.German.1080p.WEB.H264-GRP",
                "links": [[url, mirror]],
                "password": password,
            }
        ),
    )


def encode_data_param(package):
    pkg_id, payload = package
    data = json.loads(payload)
    page_payload = {
        "package_id": pkg_id,
        "title": data["title"],
        "password": data["password"],
        "mirror": None,
        "links": data["links"],
        "original_url": None,
    }
    return quote(urlsafe_b64encode(json.dumps(page_payload).encode()).decode())


class CaptchaUserscriptRoutingTests(unittest.TestCase):
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

    def _serve(self, packages):
        app = Bottle()
        fake_values = {
            "device": object(),
            # Sections are plain dicts; only .get() is used by the routes under test
            "config": lambda section: (
                {"he": "source.invalid"} if section == "Hostnames" else {}
            ),
        }
        stack = patch.multiple(
            shared_state,
            values=fake_values,
            get_db=lambda name: FakeProtectedDb(packages),
        )
        stack.start()
        self.addCleanup(stack.stop)
        setup_captcha_routes(app)
        return app

    def test_filecrypt_package_redirects_to_filecrypt_userscript_page(self):
        package = build_package(
            "pkg-fc",
            "https://filecrypt.example.invalid/Container/abc.html",
            "filecrypt",
        )
        app = self._serve([package])

        status, headers, _ = self._request(app, "/captcha")

        self.assertTrue(status.startswith("30"))
        self.assertIn("/captcha/filecrypt?data=", headers["Location"])

    def test_keeplinks_package_still_redirects_to_keeplinks_page(self):
        package = build_package(
            "pkg-kl", "https://keeplinks.example.invalid/p/abc", "keeplinks"
        )
        app = self._serve([package])

        status, headers, _ = self._request(app, "/captcha")

        self.assertTrue(status.startswith("30"))
        self.assertIn("/captcha/keeplinks?data=", headers["Location"])

    def test_he_package_redirects_to_he_userscript_page(self):
        package = build_package(
            "pkg-he", "https://source.invalid/releases/synthetic", "he"
        )
        app = self._serve([package])

        status, headers, _ = self._request(app, "/captcha")

        self.assertTrue(status.startswith("30"))
        self.assertIn("/captcha/he?data=", headers["Location"])

    def test_filecrypt_page_renders_standard_userscript_section(self):
        package = build_package(
            "pkg-fc",
            "https://filecrypt.example.invalid/Container/abc.html",
            "filecrypt",
        )
        app = self._serve([package])

        status, _, body = self._request(
            app, "/captcha/filecrypt", f"data={encode_data_param(package)}"
        )

        self.assertEqual("200 OK", status)
        self.assertIn("Open FileCrypt & Get Download Links", body)
        self.assertIn("/captcha/filecrypt.user.js", body)
        self.assertIn("abc.html?transfer_url=", body)

    def test_filecrypt_page_appends_transfer_params_to_existing_query(self):
        package = build_package(
            "pkg-fc",
            "https://filecrypt.example.invalid/Container/abc.html?mirror=2",
            "filecrypt",
        )
        app = self._serve([package])

        status, _, body = self._request(
            app, "/captcha/filecrypt", f"data={encode_data_param(package)}"
        )

        self.assertEqual("200 OK", status)
        self.assertIn("abc.html?mirror=2&transfer_url=", body)
        self.assertNotIn("mirror=2?transfer_url=", body)

    def test_keeplinks_page_still_renders_standard_userscript_section(self):
        package = build_package(
            "pkg-kl", "https://keeplinks.example.invalid/p/abc", "keeplinks"
        )
        app = self._serve([package])

        status, _, body = self._request(
            app, "/captcha/keeplinks", f"data={encode_data_param(package)}"
        )

        self.assertEqual("200 OK", status)
        self.assertIn("Open KeepLinks & Get Download Links", body)
        self.assertIn("/captcha/keeplinks.user.js", body)
        self.assertIn("quick-transfer", body)

    def test_he_page_renders_standard_userscript_section(self):
        package = build_package(
            "pkg-he", "https://source.invalid/releases/synthetic", "he"
        )
        app = self._serve([package])

        status, _, body = self._request(
            app, "/captcha/he", f"data={encode_data_param(package)}"
        )

        self.assertEqual("200 OK", status)
        self.assertIn("Open HE & Get Download Links", body)
        self.assertIn("/captcha/he.user.js", body)
        self.assertIn("quick-transfer", body)

    def test_he_page_accepts_missing_password(self):
        package = build_package(
            "pkg-he", "https://source.invalid/releases/synthetic", "he", None
        )
        app = self._serve([package])

        status, _, body = self._request(
            app, "/captcha/he", f"data={encode_data_param(package)}"
        )

        self.assertEqual("200 OK", status)
        self.assertIn("Open HE & Get Download Links", body)

    def test_he_userscript_uses_configured_hostname(self):
        package = build_package(
            "pkg-he", "https://source.invalid/releases/synthetic", "he"
        )
        app = self._serve([package])

        status, headers, body = self._request(app, "/captcha/he.user.js")

        self.assertEqual("200 OK", status)
        self.assertEqual("application/javascript", headers["Content-Type"])
        self.assertIn("*://source.invalid/*", body)
        self.assertNotIn("{he}", body)

    def test_removed_server_side_filecrypt_routes_are_gone(self):
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
                status, _, _ = self._request(app, path)
                self.assertTrue(status.startswith("404"))


if __name__ == "__main__":
    unittest.main()
