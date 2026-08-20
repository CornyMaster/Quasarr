"""Contracts for lazy Carbon dispatch and Classic parity."""

# -*- coding: utf-8 -*-

import ast
import io
import sys
import threading
import unittest
from contextlib import ExitStack
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from types import ModuleType
from unittest import mock

from quasarr.providers import shared_state as provider_shared_state
from quasarr.providers.html_templates import render_centered_html

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class RouteContract:
    path: str
    module_path: str
    page_id: str
    carbon_module: str
    carbon_symbol: str
    classic_symbol: str


ROUTE_CONTRACTS = (
    RouteContract(
        "/",
        "quasarr/api/__init__.py",
        "dashboard",
        "quasarr.api.carbon",
        "render_dashboard",
        "_classic_dashboard",
    ),
    RouteContract(
        "/settings",
        "quasarr/api/__init__.py",
        "settings",
        "quasarr.api.carbon",
        "render_settings",
        "_classic_settings",
    ),
    RouteContract(
        "/packages",
        "quasarr/api/packages/__init__.py",
        "downloads",
        "quasarr.api.packages.carbon",
        "render_downloads",
        "_classic_downloads",
    ),
    RouteContract(
        "/statistics",
        "quasarr/api/statistics/__init__.py",
        "statistics",
        "quasarr.api.statistics.carbon",
        "render_statistics",
        "_classic_statistics",
    ),
    RouteContract(
        "/hostnames",
        "quasarr/api/config/__init__.py",
        "hostnames",
        "quasarr.api.config.carbon",
        "render_hostnames",
        "_classic_hostnames",
    ),
    RouteContract(
        "/categories",
        "quasarr/api/config/__init__.py",
        "categories",
        "quasarr.api.config.carbon",
        "render_categories",
        "_classic_categories",
    ),
    *(
        RouteContract(
            f"/captcha/{provider}",
            "quasarr/api/captcha/__init__.py",
            "captcha",
            "quasarr.api.captcha.carbon",
            "render_captcha",
            f"_classic_{provider}_captcha",
        )
        for provider in (
            "hide",
            "junkies",
            "he",
            "keeplinks",
            "tolink",
            "filecrypt",
        )
    ),
)


def _parse(relative_path):
    return ast.parse(ROOT.joinpath(relative_path).read_text(encoding="utf-8"))


def _route_path(function):
    for decorator in function.decorator_list:
        if not isinstance(decorator, ast.Call) or not decorator.args:
            continue
        target = decorator.func
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "app"
            and target.attr == "get"
            and isinstance(decorator.args[0], ast.Constant)
        ):
            return decorator.args[0].value
    return None


def _route_function(tree, path):
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and _route_path(node) == path
    ]
    if len(matches) != 1:
        raise AssertionError(f"Expected one GET {path} callback, found {len(matches)}")
    return matches[0]


class CapturingServer:
    app = None

    def __init__(self, app, **_kwargs):
        type(self).app = app

    def serve_forever(self):
        return None


class CarbonRouteDispatchTests(unittest.TestCase):
    def setUp(self):
        previous_values = provider_shared_state.values
        previous_lock = provider_shared_state.lock
        self.addCleanup(provider_shared_state.set_state, previous_values, previous_lock)

    def _build_main_app(self):
        import quasarr.api as api

        setup_names = (
            "add_auth_routes",
            "add_auth_hook",
            "setup_static_routes",
            "setup_arr_routes",
            "setup_captcha_routes",
            "setup_config",
            "setup_statistics",
            "setup_sponsors_helper_routes",
            "setup_packages_routes",
            "setup_ui_preference_routes",
            "audit_route_auth_modes",
        )
        with ExitStack() as stack:
            for setup_name in setup_names:
                stack.enter_context(mock.patch.object(api, setup_name))
            stack.enter_context(mock.patch.object(api, "Server", CapturingServer))
            api.get_api({"port": 8080, "ui_preference": "classic"}, threading.Lock())
        return CapturingServer.app

    def _request(self, app, target):
        path, _, query = target.partition("?")
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

        def start_response(status, headers, _exc_info=None):
            captured["status"] = status
            captured["headers"] = list(headers)

        body = b"".join(app(environ, start_response))
        return captured["status"], captured["headers"], body

    def test_classic_footer_has_one_fixed_carbon_switch(self):
        html = render_centered_html("<p>Classic body</p>")
        link = '<a href="/ui/carbon?next=/">Carbon UI</a>'

        self.assertEqual(1, html.count(link))
        self.assertIn(f" · {link}", html)

    def test_every_browser_route_has_lazy_dispatch_contract(self):
        trees = {}
        for contract in ROUTE_CONTRACTS:
            tree = trees.setdefault(contract.module_path, _parse(contract.module_path))
            route = _route_function(tree, contract.path)
            calls = [
                node
                for node in ast.walk(route)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "render_page"
            ]

            with self.subTest(path=contract.path):
                self.assertEqual(1, len(calls))
                call = calls[0]
                self.assertGreaterEqual(len(call.args), 3)
                self.assertEqual(contract.page_id, call.args[0].value)
                self.assertIsInstance(call.args[2], ast.Name)
                self.assertEqual(contract.classic_symbol, call.args[2].id)
                self.assertTrue(
                    any(keyword.arg == "shared_state" for keyword in call.keywords)
                )
                self.assertTrue(
                    any(
                        isinstance(node, ast.ImportFrom)
                        and node.module == contract.carbon_module
                        and any(
                            alias.name == contract.carbon_symbol for alias in node.names
                        )
                        for node in ast.walk(route)
                    )
                )
                self.assertTrue(
                    any(
                        isinstance(node, ast.FunctionDef)
                        and node.name == contract.classic_symbol
                        for node in ast.walk(tree)
                    )
                )

    def test_route_owners_have_no_module_scope_carbon_imports(self):
        for relative_path in sorted({item.module_path for item in ROUTE_CONTRACTS}):
            tree = _parse(relative_path)
            module_imports = [
                node
                for node in tree.body
                if isinstance(node, (ast.Import, ast.ImportFrom))
            ]
            with self.subTest(path=relative_path):
                for node in module_imports:
                    if isinstance(node, ast.ImportFrom):
                        self.assertFalse((node.module or "").endswith(".carbon"))
                    else:
                        self.assertFalse(
                            any(alias.name.endswith(".carbon") for alias in node.names)
                        )

    def test_settings_classic_route_redirects_to_existing_dashboard(self):
        app = self._build_main_app()
        status, headers, _body = self._request(app, "/settings?ui=classic")

        self.assertEqual("303", status.split()[0])
        locations = [value for name, value in headers if name.lower() == "location"]
        self.assertEqual(["http://localhost:8080/"], locations)

    def test_settings_carbon_renderer_is_imported_only_on_demand(self):
        module = ModuleType("quasarr.api.carbon")
        module.render_settings = lambda state: (
            "carbon-settings" if state is provider_shared_state else "wrong-state"
        )
        app = self._build_main_app()

        with (
            mock.patch.dict(sys.modules, {"quasarr.api.carbon": module}),
            mock.patch(
                "quasarr.providers.page_dispatch.carbon_assets_available",
                return_value=True,
            ),
        ):
            status, headers, body = self._request(app, "/settings?ui=carbon")

        self.assertEqual("200", status.split()[0])
        self.assertEqual(b"carbon-settings", body)
        self.assertEqual(
            1,
            len(
                [
                    value
                    for name, value in headers
                    if name.lower() == "content-security-policy"
                ]
            ),
        )


if __name__ == "__main__":
    unittest.main()
