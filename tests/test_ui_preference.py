"""Contracts for UI preference state, routes, and boot ordering."""

# -*- coding: utf-8 -*-

import importlib
import io
import json
import os
import sys
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlsplit

from bottle import Bottle


class FakeUiPreferenceTable:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.retrieve_calls = []
        self.update_store_calls = []

    def retrieve(self, key):
        self.retrieve_calls.append(key)
        if self.error is not None:
            raise self.error
        return self.value

    def update_store(self, key, value):
        self.update_store_calls.append((key, value))
        if isinstance(self.error, BaseException):
            raise self.error


class FakeUiPreferenceSharedState:
    def __init__(self, table=None, cached_mode=None):
        self.table = table or FakeUiPreferenceTable()
        self.values = {
            "database": self._database,
            "ui_preference": cached_mode,
        }
        self.updates = []

    def _database(self, table_name):
        self.values["database_table_name"] = table_name
        return self.table

    def update(self, key, value):
        self.updates.append((key, value))
        self.values[key] = value


class FakeRequest:
    def __init__(self, query=None, cookies=None):
        self.query = query or {}
        self._cookies = cookies or {}

    def get_cookie(self, name, default=None):
        return self._cookies.get(name, default)


class UiPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.mod = importlib.import_module("quasarr.providers.ui_preference")
        self.api = importlib.import_module("quasarr.api")
        self._env_backup = dict(os.environ)
        self._cwd_backup = os.getcwd()
        self._stdout_backup = sys.stdout

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)
        os.chdir(self._cwd_backup)
        sys.stdout = self._stdout_backup

    def _with_request(self, query=None, cookies=None):
        return mock.patch.object(
            self.mod, "request", FakeRequest(query=query, cookies=cookies)
        )

    def test_log_context_uses_concise_ui_preference_marker(self):
        from quasarr.providers.log import _contexts_to_str

        context, source = _contexts_to_str(["quasarr", "providers", "ui_preference"])

        self.assertEqual("🔌🎛️", context)
        self.assertEqual("", source)

    def _request(self, app, path, *, method="GET", headers=None, body=b""):
        headers = dict(headers or {})
        query = ""
        if "?" in path:
            path, query = path.split("?", 1)
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
            environ["CONTENT_LENGTH"] = str(len(body))
        for key, value in headers.items():
            environ[key] = value

        captured = {}

        def start_response(status, response_headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = list(response_headers)

        body_bytes = b"".join(app(environ, start_response))
        return (
            captured.get("status", "500 Internal Server Error"),
            list(captured.get("headers", [])),
            body_bytes,
        )

    def _header_values(self, headers, name):
        lowered = name.lower()
        return [value for key, value in headers if key.lower() == lowered]

    def _json_body(self, body):
        text = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body
        return json.loads(text)

    def test_contract_constants_are_stable(self):
        self.assertEqual(self.mod.VALID_UI_MODES, frozenset({"carbon", "classic"}))
        self.assertEqual(self.mod.DEFAULT_UI, "carbon")
        self.assertEqual(self.mod.UI_PREFERENCE_TABLE, "ui_preference")
        self.assertEqual(self.mod.UI_PREFERENCE_KEY, "mode")
        self.assertEqual(self.mod.UI_COOKIE_NAME, "quasarr_ui")

    def test_get_active_ui_respects_exact_precedence_and_invalid_fallthrough(self):
        cases = [
            (
                "env wins over everything",
                {"QUASARR_UI": "carbon"},
                {"ui": "classic"},
                {"quasarr_ui": "classic"},
                {"ui_preference": "classic"},
                "carbon",
            ),
            (
                "invalid env falls through to query",
                {"QUASARR_UI": "invalid"},
                {"ui": "carbon"},
                {"quasarr_ui": "classic"},
                {"ui_preference": "classic"},
                "carbon",
            ),
            (
                "invalid env and query fall through to cookie",
                {"QUASARR_UI": "invalid"},
                {"ui": "invalid"},
                {"quasarr_ui": "carbon"},
                {"ui_preference": "classic"},
                "carbon",
            ),
            (
                "invalid env/query/cookie fall through to cached shared state",
                {"QUASARR_UI": "invalid"},
                {"ui": "invalid"},
                {"quasarr_ui": "invalid"},
                {"ui_preference": "carbon"},
                "carbon",
            ),
            (
                "everything invalid falls back to default",
                {"QUASARR_UI": "invalid"},
                {"ui": "invalid"},
                {"quasarr_ui": "invalid"},
                {"ui_preference": "invalid"},
                "carbon",
            ),
        ]

        for label, environ, query, cookies, cached, expected in cases:
            with self.subTest(label=label):
                shared_state = SimpleNamespace(values=dict(cached))
                with (
                    self._with_request(query=query, cookies=cookies),
                    mock.patch.dict(os.environ, environ, clear=True),
                ):
                    self.assertEqual(
                        self.mod.get_active_ui(shared_state),
                        expected,
                    )

    def test_get_active_ui_never_needs_database_or_request_context(self):
        shared_state = SimpleNamespace(
            values={
                "database": lambda *_args, **_kwargs: self.fail(
                    "database must not be consulted by get_active_ui"
                ),
                "ui_preference": "classic",
            }
        )
        with mock.patch.dict(os.environ, {}, clear=True), self._with_request():
            self.assertEqual(self.mod.get_active_ui(shared_state), "classic")

    def test_load_ui_preference_reads_once_caches_and_defaults_on_storage_failure(self):
        valid_table = FakeUiPreferenceTable(value="carbon")
        shared_state = FakeUiPreferenceSharedState(valid_table)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.mod.load_ui_preference(shared_state), "carbon")
        self.assertEqual(valid_table.retrieve_calls, [self.mod.UI_PREFERENCE_KEY])
        self.assertEqual(
            shared_state.values["database_table_name"], self.mod.UI_PREFERENCE_TABLE
        )
        self.assertEqual(shared_state.updates, [("ui_preference", "carbon")])

        failing_table = FakeUiPreferenceTable(error=RuntimeError("storage boom"))
        failing_state = FakeUiPreferenceSharedState(failing_table)
        warnings = []
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                self.mod, "warn", lambda message: warnings.append(message)
            ),
        ):
            self.assertEqual(self.mod.load_ui_preference(failing_state), "carbon")
        self.assertEqual(failing_table.retrieve_calls, [self.mod.UI_PREFERENCE_KEY])
        self.assertEqual(failing_state.updates, [("ui_preference", "carbon")])
        self.assertEqual(len(warnings), 1)
        self.assertIn("RuntimeError", warnings[0])
        self.assertNotIn("storage boom", warnings[0])

    def test_load_ui_preference_invalid_db_value_defaults_and_updates_cache_once(self):
        invalid_table = FakeUiPreferenceTable(value="invalid")
        shared_state = FakeUiPreferenceSharedState(invalid_table)
        warnings = []
        with mock.patch.object(
            self.mod, "warn", lambda message: warnings.append(message)
        ):
            mode = self.mod.load_ui_preference(shared_state)
        self.assertEqual(mode, "carbon")
        self.assertEqual(invalid_table.retrieve_calls, [self.mod.UI_PREFERENCE_KEY])
        self.assertEqual(shared_state.updates, [("ui_preference", "carbon")])
        # A genuinely invalid stored value is still worth a warning - only
        # an absent row (fresh install, see below) is silent.
        self.assertEqual(len(warnings), 1)

    def test_load_ui_preference_absent_row_defaults_silently_without_warning(self):
        """A fresh install has no ui_preference row yet: retrieve() returns
        None. That is the expected first-boot state, not a storage failure
        or a corrupt value, so it must default quietly - not log a spurious
        'UI preference load failed (ValueError)' on every first boot.
        """
        absent_table = FakeUiPreferenceTable(value=None)
        shared_state = FakeUiPreferenceSharedState(absent_table)
        warnings = []
        with mock.patch.object(
            self.mod, "warn", lambda message: warnings.append(message)
        ):
            mode = self.mod.load_ui_preference(shared_state)
        self.assertEqual(mode, "carbon")
        self.assertEqual(absent_table.retrieve_calls, [self.mod.UI_PREFERENCE_KEY])
        self.assertEqual(shared_state.updates, [("ui_preference", "carbon")])
        self.assertEqual(warnings, [])

    def test_persist_ui_preference_validates_and_writes_before_cache(self):
        table = FakeUiPreferenceTable()
        shared_state = FakeUiPreferenceSharedState(table)
        self.assertEqual(
            self.mod.persist_ui_preference(shared_state, "carbon"),
            "carbon",
        )
        self.assertEqual(
            table.update_store_calls, [(self.mod.UI_PREFERENCE_KEY, "carbon")]
        )
        self.assertEqual(shared_state.updates, [("ui_preference", "carbon")])

        for invalid_mode in ["invalid", "CARBON", "", None, 3, [], {}]:
            with self.subTest(invalid_mode=invalid_mode):
                with self.assertRaises(ValueError):
                    self.mod.persist_ui_preference(shared_state, invalid_mode)
        self.assertEqual(
            table.update_store_calls, [(self.mod.UI_PREFERENCE_KEY, "carbon")]
        )
        self.assertEqual(shared_state.updates, [("ui_preference", "carbon")])

    def test_persist_ui_preference_does_not_cache_failed_storage(self):
        class FailingTable(FakeUiPreferenceTable):
            def update_store(self, key, value):
                super().update_store(key, value)
                raise RuntimeError("write failed")

        shared_state = FakeUiPreferenceSharedState(FailingTable())
        with self.assertRaises(RuntimeError):
            self.mod.persist_ui_preference(shared_state, "classic")
        self.assertEqual(shared_state.updates, [])

    def test_setup_ui_preference_routes_registers_browser_and_api_auth_modes(self):
        app = Bottle()
        shared_state = FakeUiPreferenceSharedState()
        self.api.setup_ui_preference_routes(app, shared_state)

        rules = {route.rule: route for route in app.routes}
        self.assertIn("/ui/<mode>", rules)
        self.assertIn("/api/ui-preference", rules)
        self.assertEqual(
            getattr(rules["/ui/<mode>"].callback, "__quasarr_auth_mode__", ""),
            "browser",
        )
        self.assertEqual(
            getattr(rules["/api/ui-preference"].callback, "__quasarr_auth_mode__", ""),
            "api_key",
        )

        self.assertIsNone(
            self.api.audit_route_auth_modes(
                app,
                api_key_prefixes=("/api",),
                public_whitelist=(".user.js",),
            )
        )

    def test_ui_route_sets_cookie_and_sanitizes_redirect_target(self):
        app = Bottle()
        shared_state = FakeUiPreferenceSharedState()
        self.api.setup_ui_preference_routes(app, shared_state)

        status, headers, _ = self._request(
            app,
            "/ui/carbon?next=/dashboard?tab=downloads",
        )
        self.assertTrue(status.startswith("30"))
        location = self._header_values(headers, "Location")[0]
        parsed = urlsplit(location)
        self.assertEqual(parsed.path, "/dashboard")
        self.assertEqual(parsed.query, "tab=downloads")
        cookie = self._header_values(headers, "Set-Cookie")[0]
        self.assertIn("quasarr_ui=carbon", cookie)
        self.assertIn("Path=/", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("samesite=lax", cookie.lower())
        self.assertEqual(shared_state.table.update_store_calls, [])

        for label, next_value in [
            ("scheme", "https://evil.invalid/path"),
            ("netloc", "//evil.invalid/path"),
            ("backslash", "/safe\\path"),
            ("parent segment", "/safe/../admin"),
            ("encoded parent segment", "/safe/%2e%2e/admin"),
            ("login", "/login"),
            ("logout", "/logout"),
            ("crlf", "/safe%0d%0aSet-Cookie:bad"),
        ]:
            with self.subTest(label=label):
                status, headers, _ = self._request(
                    app,
                    f"/ui/classic?next={next_value}",
                )
                self.assertTrue(status.startswith("30"))
                location = self._header_values(headers, "Location")[0]
                self.assertEqual(urlsplit(location).path, "/")

        status, _, body = self._request(app, "/ui/invalid")
        self.assertEqual(status.split()[0], "404")
        self.assertTrue(body)
        self.assertEqual(shared_state.table.update_store_calls, [])

    def test_api_route_enforces_real_auth_hook_and_persists_once(self):
        app = Bottle()
        table = FakeUiPreferenceTable()
        shared_state = FakeUiPreferenceSharedState(table)
        self.api.setup_ui_preference_routes(app, shared_state)
        self.api.add_auth_hook(app, whitelist=[".user.js"])

        with mock.patch("quasarr.providers.auth.Config") as config_mock:
            config_mock.return_value.get.return_value = "api-secret"

            status, _, _ = self._request(
                app,
                "/api/ui-preference",
                method="POST",
                headers={"CONTENT_TYPE": "application/json"},
                body=b'{"mode":"carbon"}',
            )
            self.assertEqual(status.split()[0], "401")

            status, _, _ = self._request(
                app,
                "/api/ui-preference",
                method="POST",
                headers={
                    "CONTENT_TYPE": "application/json",
                    "HTTP_X_API_KEY": "wrong",
                },
                body=b'{"mode":"carbon"}',
            )
            self.assertEqual(status.split()[0], "403")

            status, _, body = self._request(
                app,
                "/api/ui-preference",
                method="POST",
                headers={
                    "CONTENT_TYPE": "application/json",
                    "HTTP_X_API_KEY": "api-secret",
                },
                body=b'{"mode":"carbon"}',
            )
            self.assertEqual(status.split()[0], "200")
            self.assertEqual(
                self._json_body(body),
                {"success": True, "mode": "carbon"},
            )

        self.assertEqual(
            table.update_store_calls,
            [(self.mod.UI_PREFERENCE_KEY, "carbon")],
        )
        self.assertEqual(shared_state.updates, [("ui_preference", "carbon")])

    def test_api_ui_preference_rejects_invalid_and_malformed_json_with_exact_400_shape(
        self,
    ):
        app = Bottle()
        table = FakeUiPreferenceTable()
        shared_state = FakeUiPreferenceSharedState(table)
        self.api.setup_ui_preference_routes(app, shared_state)
        self.api.add_auth_hook(app, whitelist=[".user.js"])

        with mock.patch("quasarr.providers.auth.Config") as config_mock:
            config_mock.return_value.get.return_value = "api-secret"

            status, headers, body = self._request(
                app,
                "/api/ui-preference",
                method="POST",
                headers={
                    "CONTENT_TYPE": "application/json",
                    "HTTP_X_API_KEY": "api-secret",
                },
                body=json.dumps({"mode": "carbon"}).encode("utf-8"),
            )
            self.assertEqual(status.split()[0], "200")
            self.assertEqual(self._json_body(body), {"success": True, "mode": "carbon"})
            self.assertEqual(
                table.update_store_calls, [(self.mod.UI_PREFERENCE_KEY, "carbon")]
            )
            self.assertEqual(shared_state.updates, [("ui_preference", "carbon")])

            status, _, body = self._request(
                app,
                "/api/ui-preference",
                method="POST",
                headers={
                    "CONTENT_TYPE": "application/json",
                    "HTTP_X_API_KEY": "api-secret",
                },
                body=json.dumps({"mode": []}).encode("utf-8"),
            )
            self.assertEqual(status.split()[0], "400")
            self.assertEqual(
                self._json_body(body),
                {"success": False, "message": "Invalid UI mode"},
            )
            self.assertEqual(
                table.update_store_calls, [(self.mod.UI_PREFERENCE_KEY, "carbon")]
            )
            self.assertEqual(shared_state.updates, [("ui_preference", "carbon")])

            status, _, body = self._request(
                app,
                "/api/ui-preference",
                method="POST",
                headers={
                    "CONTENT_TYPE": "application/json",
                    "HTTP_X_API_KEY": "api-secret",
                },
                body=json.dumps({"mode": "invalid"}).encode("utf-8"),
            )
            self.assertEqual(status.split()[0], "400")
            self.assertEqual(
                self._json_body(body),
                {"success": False, "message": "Invalid UI mode"},
            )
            self.assertEqual(
                table.update_store_calls, [(self.mod.UI_PREFERENCE_KEY, "carbon")]
            )
            self.assertEqual(shared_state.updates, [("ui_preference", "carbon")])

            status, _, body = self._request(
                app,
                "/api/ui-preference",
                method="POST",
                headers={
                    "CONTENT_TYPE": "application/json",
                    "HTTP_X_API_KEY": "api-secret",
                },
                body=b"{broken",
            )
            self.assertEqual(status.split()[0], "400")
            self.assertEqual(
                self._json_body(body),
                {"success": False, "message": "Invalid UI mode"},
            )
            self.assertEqual(
                table.update_store_calls,
                [(self.mod.UI_PREFERENCE_KEY, "carbon")],
            )
            self.assertEqual(shared_state.updates, [("ui_preference", "carbon")])

    def test_boot_calls_load_ui_preference_once_after_database_registration(self):
        quasarr_init = importlib.import_module("quasarr.__init__")

        class FakeManagerContext:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def dict(self):
                return {}

            def Lock(self):
                return object()

        class FakeTemporaryFile:
            def close(self):
                return None

        class FakeSharedState:
            def __init__(self):
                self.values = {}
                self.update_calls = []

            def set_state(self, *_args):
                return None

            def set_connection_info(self, *_args):
                return None

            def set_files(self, _config_path):
                self.values["dbfile"] = "fake.db"
                self.values["configfile"] = "fake.ini"

            def update(self, key, value):
                self.update_calls.append((key, value))
                self.values[key] = value

        fake_shared = FakeSharedState()
        sentinel = RuntimeError("stop-after-load")
        load_calls = []

        def fake_load_ui_preference(state):
            load_calls.append("called")
            self.assertIs(state.values.get("database"), quasarr_init.DataBase)
            raise sentinel

        with (
            mock.patch.object(
                quasarr_init.multiprocessing,
                "set_start_method",
                return_value=None,
            ),
            mock.patch.object(
                quasarr_init.multiprocessing,
                "Manager",
                return_value=FakeManagerContext(),
            ),
            mock.patch.object(quasarr_init, "shared_state", fake_shared),
            mock.patch.object(quasarr_init.DataBase, "maintain", return_value=True),
            mock.patch.object(
                quasarr_init.Config, "prune_unsupported_keys", return_value=None
            ),
            mock.patch.object(quasarr_init.os.path, "exists", return_value=True),
            mock.patch.object(quasarr_init.os, "makedirs", return_value=None),
            mock.patch.object(
                quasarr_init.tempfile,
                "TemporaryFile",
                return_value=FakeTemporaryFile(),
            ),
            mock.patch("builtins.open", mock.mock_open(read_data="C:/fake-config\n")),
            mock.patch.object(
                quasarr_init, "load_ui_preference", side_effect=fake_load_ui_preference
            ),
            mock.patch.object(quasarr_init, "Unbuffered", side_effect=lambda s: s),
            mock.patch.object(quasarr_init, "check_ip", return_value="127.0.0.1"),
            mock.patch.dict(
                os.environ,
                {
                    "DOCKER": "1",
                    "INTERNAL_ADDRESS": "http://127.0.0.1:8080",
                },
                clear=True,
            ),
        ):
            sys.stdout = io.StringIO()
            with self.assertRaisesRegex(RuntimeError, "stop-after-load"):
                quasarr_init.run()

        self.assertEqual(load_calls, ["called"])
        self.assertIn(("database", quasarr_init.DataBase), fake_shared.update_calls)
        db_index = fake_shared.update_calls.index(("database", quasarr_init.DataBase))
        self.assertLess(db_index, len(fake_shared.update_calls))

    def test_boot_reconfigures_stdout_to_utf8_before_first_print(self):
        """A non-UTF-8 console codepage must not crash the startup banner.

        Regression test for a real crash observed running the PyInstaller
        standalone build on a non-UTF-8-locale Windows host: `sys.stdout`
        defaulted to cp1252, which cannot encode the box-drawing startup
        banner, raising UnicodeEncodeError on the very first print() before
        any error handling exists. `run()` must reconfigure `sys.stdout` to
        UTF-8 (with a safe error handler) before printing anything.
        """
        quasarr_init = importlib.import_module("quasarr.__init__")

        class FakeManagerContext:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def dict(self):
                return {}

            def Lock(self):
                return object()

        class FakeTemporaryFile:
            def close(self):
                return None

        class FakeSharedState:
            def __init__(self):
                self.values = {}

            def set_state(self, *_args):
                return None

            def set_connection_info(self, *_args):
                return None

            def set_files(self, _config_path):
                self.values["dbfile"] = "fake.db"
                self.values["configfile"] = "fake.ini"

            def update(self, key, value):
                self.values[key] = value

        class Cp1252LikeStdout:
            """Encodes like a real cp1252 console stream until reconfigured."""

            def __init__(self):
                self.encoding = "cp1252"
                self.reconfigure_calls = []
                self.written = []

            def reconfigure(self, *, encoding=None, errors=None):
                self.reconfigure_calls.append((encoding, errors))
                self.encoding = encoding or self.encoding

            def write(self, data):
                # Mirror what a real cp1252 TextIOWrapper does: raise on any
                # character cp1252 cannot represent, unless reconfigured.
                if self.encoding != "utf-8":
                    data.encode("cp1252")
                self.written.append(data)
                return len(data)

            def flush(self):
                return None

        fake_shared = FakeSharedState()
        fake_stdout = Cp1252LikeStdout()
        sentinel = RuntimeError("stop-after-load")

        def fake_load_ui_preference(_state):
            raise sentinel

        with (
            mock.patch.object(
                quasarr_init.multiprocessing,
                "set_start_method",
                return_value=None,
            ),
            mock.patch.object(
                quasarr_init.multiprocessing,
                "Manager",
                return_value=FakeManagerContext(),
            ),
            mock.patch.object(quasarr_init, "shared_state", fake_shared),
            mock.patch.object(quasarr_init.DataBase, "maintain", return_value=True),
            mock.patch.object(
                quasarr_init.Config, "prune_unsupported_keys", return_value=None
            ),
            mock.patch.object(quasarr_init.os.path, "exists", return_value=True),
            mock.patch.object(quasarr_init.os, "makedirs", return_value=None),
            mock.patch.object(
                quasarr_init.tempfile,
                "TemporaryFile",
                return_value=FakeTemporaryFile(),
            ),
            mock.patch("builtins.open", mock.mock_open(read_data="C:/fake-config\n")),
            mock.patch.object(
                quasarr_init, "load_ui_preference", side_effect=fake_load_ui_preference
            ),
            mock.patch.object(quasarr_init, "check_ip", return_value="127.0.0.1"),
            mock.patch.dict(
                os.environ,
                {
                    "DOCKER": "1",
                    "INTERNAL_ADDRESS": "http://127.0.0.1:8080",
                },
                clear=True,
            ),
        ):
            self._stdout_backup = sys.stdout
            sys.stdout = fake_stdout
            try:
                with self.assertRaisesRegex(RuntimeError, "stop-after-load"):
                    quasarr_init.run()
            finally:
                sys.stdout = self._stdout_backup

        self.assertIn(("utf-8", "replace"), fake_stdout.reconfigure_calls)
        self.assertTrue(
            any("Quasarr" in chunk for chunk in fake_stdout.written),
            "the startup banner must actually have been written",
        )


if __name__ == "__main__":
    unittest.main()
