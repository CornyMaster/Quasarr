# -*- coding: utf-8 -*-

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from bottle import Bottle

import quasarr.api.packages as packages_api
from quasarr.providers.auth import audit_route_auth_modes

PACKAGE_A = "Quasarr_movies_" + "a" * 32
PACKAGE_B = "Quasarr_movies_" + "b" * 32
PACKAGE_C = "Quasarr_movies_" + "c" * 32
PACKAGE_D = "Quasarr_movies_" + "d" * 32
RETRY_AFTER = 4_000_000_000


def deferred_block(
    *,
    state="observing",
    hold_type="provisional",
    evidence_count=1,
    probe_requested=False,
    crypter="filecrypt",
    reason_code="ip_block_suspected",
    active=True,
):
    return {
        "crypter": crypter,
        "reason_code": reason_code,
        "since_epoch": 1_700_000_000,
        "retry_after_epoch": RETRY_AFTER,
        "probe_requested": probe_requested,
        "observation_holds": 1,
        "state": state,
        "evidence_count": evidence_count,
        "hold_type": hold_type,
        "active": active,
    }


def queue_item(package_id, title, deferred=None, storage=""):
    item = {
        "nzo_id": package_id,
        "filename": title,
        "cat": "movies",
        "type": "protected",
        "percentage": 0,
        "timeleft": "23:59:59",
        "mb": 1024,
        "bytes": 0,
        "storage": storage,
    }
    if deferred is not None:
        item["deferred"] = deferred
    return item


def history_item(package_id, name):
    return {
        "nzo_id": package_id,
        "name": name,
        "category": "movies",
        "status": "Failed",
        "fail_message": "Synthetic failure",
        "bytes": 0,
        "storage": "",
    }


class MemoryDatabase:
    def __init__(self):
        self.rows = {}

    def retrieve(self, key):
        return self.rows.get(key)

    def mutate_value(self, key, mutator):
        value = mutator(self.rows.get(key))
        if value is None:
            self.rows.pop(key, None)
        else:
            self.rows[key] = value
        return value

    def delete_exact(self, key, value):
        if self.rows.get(key) != value:
            return False
        self.rows.pop(key)
        return True


class DeferredApiState:
    def __init__(self):
        self.values = {"crypter_cooldown_hours": 24}
        self.databases = {
            "protected": MemoryDatabase(),
            "failed": MemoryDatabase(),
        }
        self.device_calls = 0

    @property
    def protected(self):
        return self.databases["protected"]

    def get_db(self, table):
        return self.databases[table]

    def get_device(self):
        self.device_calls += 1
        raise AssertionError("deferred package commands must not access JDownloader")

    def add_package(self, package_id, *, deferred):
        package = {
            "title": f"Synthetic {package_id[-4:]}",
            "links": [],
            "password": "",
            "size_mb": 1,
        }
        if deferred:
            package["deferred"] = {
                "crypter": "filecrypt",
                "reason_code": "ip_block_suspected",
                "since_epoch": 1_700_000_000,
                "retry_after_epoch": RETRY_AFTER,
                "probe_requested": False,
                "observation_holds": 1,
            }
        self.protected.rows[package_id] = json.dumps(package)


def route_callback(app, method, rule):
    return next(
        route.callback
        for route in app.routes
        if route.method == method and route.rule == rule
    )


class DeferredPackagesRenderingTests(unittest.TestCase):
    def test_active_deferred_cards_render_separately_without_sensitive_links(self):
        downloads = {
            "queue": [
                queue_item(
                    PACKAGE_A,
                    "[Waiting for linkcrypter retry] Deferred <script>alert(1)</script>",
                    deferred_block(evidence_count=2),
                    storage="https://secret-source.invalid/private",
                ),
                queue_item(
                    PACKAGE_B,
                    "[Waiting for linkcrypter retry] Cooldown package",
                    deferred_block(
                        state="cooldown",
                        hold_type="crypter_cooldown",
                        evidence_count=3,
                        probe_requested=True,
                        crypter='<img src=x onerror="alert(2)">',
                        reason_code="blocked<script>alert(3)</script>",
                    ),
                ),
                queue_item(
                    PACKAGE_D,
                    "[CAPTCHA not solved!] Ordinary package",
                ),
            ],
            "history": [],
        }

        with mock.patch.object(packages_api, "get_packages", return_value=downloads):
            rendered = packages_api._render_packages_content()

        self.assertIn("Deferred linkcrypter checks", rendered)
        self.assertEqual(
            2, rendered.count('class="package-card deferred-package-card"')
        )
        self.assertIn("Observing", rendered)
        self.assertIn("Cooldown", rendered)
        self.assertIn("<strong>Evidence:</strong> 2", rendered)
        self.assertIn("<strong>Evidence:</strong> 3", rendered)
        self.assertIn("Retry in", rendered)
        self.assertIn(f'data-retry-after-epoch="{RETRY_AFTER}"', rendered)
        self.assertIn("Probe not queued", rendered)
        self.assertIn("Probe queued", rendered)
        self.assertIn("Linkcrypter:", rendered)
        self.assertIn("Reason:", rendered)
        self.assertIn("IP access block suspected", rendered)
        self.assertIn("Check selected", rendered)
        self.assertIn("Delete selected packages", rendered)
        self.assertEqual(2, rendered.count('class="deferred-package-select"'))
        self.assertIn(f'href="/captcha?package_id={PACKAGE_A}"', rendered)
        self.assertIn(f'href="/captcha?package_id={PACKAGE_B}"', rendered)
        self.assertIn("Solve CAPTCHA", rendered)
        self.assertIn("Deferred &lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertIn("&lt;img src=x onerror=&quot;alert(2)&quot;&gt;", rendered)
        self.assertNotIn("<script>alert", rendered)
        self.assertNotIn("<img src=x", rendered)
        self.assertNotIn("secret-source.invalid", rendered)
        self.assertLess(
            rendered.index("Deferred linkcrypter checks"),
            rendered.index("⬇️ Downloading"),
        )

    def test_inactive_or_unprojected_packages_use_the_normal_queue(self):
        downloads = {
            "queue": [
                queue_item(
                    PACKAGE_A,
                    "[CAPTCHA not solved!] Expired hold",
                    deferred_block(active=False, hold_type="none"),
                ),
                queue_item(
                    PACKAGE_B,
                    "[CAPTCHA not solved!] Fail mode package",
                ),
            ],
            "history": [],
        }

        with mock.patch.object(packages_api, "get_packages", return_value=downloads):
            rendered = packages_api._render_packages_content()

        self.assertNotIn("Deferred linkcrypter checks", rendered)
        self.assertIn("⬇️ Downloading", rendered)
        self.assertIn("Expired hold", rendered)
        self.assertIn("Fail mode package", rendered)
        self.assertEqual(2, rendered.count("Solve CAPTCHA"))
        self.assertNotIn("deferred-package-select", rendered)

    def test_ordinary_queue_and_history_keep_actions_and_escape_dynamic_text(self):
        malicious_package_id = '"><svg/onload=alert(9)>'
        downloads = {
            "queue": [
                queue_item(
                    malicious_package_id,
                    '[CAPTCHA not solved!] Queue <img src=x onerror="alert(1)">',
                )
            ],
            "history": [
                history_item(
                    PACKAGE_B,
                    'History </p><script>alert("history")</script>',
                )
            ],
        }

        with mock.patch.object(packages_api, "get_packages", return_value=downloads):
            rendered = packages_api._render_packages_content()

        self.assertIn("⬇️ Downloading", rendered)
        self.assertIn("📜 Recent History", rendered)
        self.assertIn("Solve CAPTCHA", rendered)
        self.assertIn("confirmDelete", rendered)
        self.assertIn("showPackageDetails", rendered)
        self.assertIn("Queue &lt;img src=x onerror=&quot;alert(1)&quot;&gt;", rendered)
        self.assertIn("History &lt;/p&gt;&lt;script&gt;alert(", rendered)
        self.assertIn("&quot;history&quot;", rendered)
        self.assertNotIn("<img src=x", rendered)
        self.assertNotIn("<script>alert", rendered)
        self.assertNotIn(malicious_package_id, rendered)
        self.assertNotIn("<svg", rendered)
        self.assertIn(
            "%22%3E%3Csvg%2Fonload%3Dalert%289%29%3E",
            rendered,
        )

    def test_ajax_content_endpoint_uses_the_same_deferred_section(self):
        app = Bottle()
        packages_api.setup_packages_routes(app)
        content_route = route_callback(app, "GET", "/api/packages/content")
        downloads = {
            "queue": [
                queue_item(
                    PACKAGE_A,
                    "[Waiting for linkcrypter retry] AJAX package",
                    deferred_block(),
                )
            ],
            "history": [],
        }

        with (
            mock.patch.dict(packages_api.shared_state.values, {"device": object()}),
            mock.patch.object(packages_api, "get_packages", return_value=downloads),
        ):
            rendered = content_route()

        self.assertIn("Deferred linkcrypter checks", rendered)
        self.assertIn("AJAX package", rendered)

    def test_packages_page_runs_individual_and_bulk_commands_through_api_fetch(self):
        app = Bottle()
        packages_api.setup_packages_routes(app)
        page_route = route_callback(app, "GET", "/packages")

        with (
            mock.patch.dict(packages_api.shared_state.values, {"device": object()}),
            mock.patch.object(packages_api, "request", SimpleNamespace(query={})),
            mock.patch.object(
                packages_api,
                "_render_packages_content",
                return_value='<div id="deferred-action-status"></div>',
            ),
        ):
            rendered = page_route()

        self.assertIn("quasarrApiFetch(endpoint", rendered)
        self.assertIn("'/api/packages/deferred/probe'", rendered)
        self.assertIn("'/api/packages/deferred'", rendered)
        self.assertIn("'DELETE'", rendered)
        self.assertIn("checkDeferredPackage", rendered)
        self.assertIn("deleteDeferredPackage", rendered)
        self.assertIn("checkSelectedDeferred", rendered)
        self.assertIn("deleteSelectedDeferred", rendered)
        self.assertIn(".deferred-package-select:checked", rendered)
        self.assertIn("button.disabled = true", rendered)
        self.assertIn("button.disabled = false", rendered)
        self.assertIn("statusElement.textContent", rendered)
        self.assertIn("await refreshContent()", rendered)


class DeferredPackagesRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Bottle()
        packages_api.setup_packages_routes(self.app)

    def test_command_routes_use_exact_methods_and_api_key_authentication(self):
        methods = {
            route.rule: route.method
            for route in self.app.routes
            if route.rule.startswith("/api/packages/deferred")
        }

        self.assertEqual(
            {
                "/api/packages/deferred/probe": "POST",
                "/api/packages/deferred": "DELETE",
            },
            methods,
        )
        audit_route_auth_modes(
            self.app,
            api_key_prefixes=("/api",),
            public_whitelist=(),
        )

    def test_structurally_malformed_payloads_are_rejected_before_delegation(self):
        probe_route = route_callback(self.app, "POST", "/api/packages/deferred/probe")
        delete_route = route_callback(self.app, "DELETE", "/api/packages/deferred")

        for payload in (None, [], {}, {"package_ids": None}, {"package_ids": "x"}):
            with self.subTest(payload=payload):
                with (
                    mock.patch.object(
                        packages_api, "request", SimpleNamespace(json=payload)
                    ),
                    mock.patch.object(
                        packages_api, "CrypterCooldownService"
                    ) as service,
                    mock.patch.object(
                        packages_api, "delete_database_packages"
                    ) as delete,
                ):
                    self.assertEqual(
                        {
                            "success": False,
                            "message": "package_ids must be a list",
                        },
                        probe_route(),
                    )
                    self.assertEqual(
                        {
                            "success": False,
                            "message": "package_ids must be a list",
                        },
                        delete_route(),
                    )

                service.assert_not_called()
                delete.assert_not_called()

    def test_routes_delegate_valid_lists_to_the_atomic_service_and_batch_helper(self):
        package_ids = [PACKAGE_A, PACKAGE_B]
        probe_result = {"requested": package_ids, "rejected": []}
        delete_result = {"deleted": package_ids, "rejected": []}
        probe_route = route_callback(self.app, "POST", "/api/packages/deferred/probe")
        delete_route = route_callback(self.app, "DELETE", "/api/packages/deferred")

        with (
            mock.patch.object(
                packages_api,
                "request",
                SimpleNamespace(json={"package_ids": package_ids}),
            ),
            mock.patch.object(packages_api, "CrypterCooldownService") as service,
            mock.patch.object(
                packages_api,
                "delete_database_packages",
                return_value=delete_result,
            ) as delete,
        ):
            service.return_value.request_probe.return_value = probe_result

            self.assertEqual(probe_result, probe_route())
            self.assertEqual(delete_result, delete_route())

        service.assert_called_once_with(packages_api.shared_state)
        service.return_value.request_probe.assert_called_once_with(package_ids)
        delete.assert_called_once_with(
            packages_api.shared_state,
            package_ids,
            expected_type="protected",
        )

    def test_probe_returns_mixed_per_id_results_and_changes_only_selected_deferred_ids(
        self,
    ):
        state = DeferredApiState()
        state.add_package(PACKAGE_A, deferred=True)
        state.add_package(PACKAGE_B, deferred=False)
        state.add_package(PACKAGE_D, deferred=True)
        probe_route = route_callback(self.app, "POST", "/api/packages/deferred/probe")
        package_ids = [
            PACKAGE_A,
            PACKAGE_B,
            PACKAGE_C,
            "malformed",
            None,
            PACKAGE_A,
        ]

        with (
            mock.patch.object(packages_api, "shared_state", state),
            mock.patch.object(
                packages_api,
                "request",
                SimpleNamespace(json={"package_ids": package_ids}),
            ),
        ):
            result = probe_route()

        self.assertEqual(
            {
                "requested": [PACKAGE_A],
                "rejected": [
                    {"package_id": PACKAGE_B, "reason": "not_deferred"},
                    {"package_id": PACKAGE_C, "reason": "not_found"},
                    {
                        "package_id": "malformed",
                        "reason": "invalid_package_id",
                    },
                    {"package_id": None, "reason": "invalid_package_id"},
                    {"package_id": PACKAGE_A, "reason": "duplicate"},
                ],
            },
            result,
        )
        self.assertTrue(
            json.loads(state.protected.rows[PACKAGE_A])["deferred"]["probe_requested"]
        )
        self.assertFalse(
            json.loads(state.protected.rows[PACKAGE_D])["deferred"]["probe_requested"]
        )
        self.assertNotIn("deferred", json.loads(state.protected.rows[PACKAGE_B]))
        self.assertEqual(0, state.device_calls)

    def test_delete_returns_mixed_per_id_results_and_keeps_unselected_deferred_ids(
        self,
    ):
        state = DeferredApiState()
        state.add_package(PACKAGE_A, deferred=True)
        state.add_package(PACKAGE_B, deferred=False)
        state.add_package(PACKAGE_D, deferred=True)
        delete_route = route_callback(self.app, "DELETE", "/api/packages/deferred")
        package_ids = [
            PACKAGE_A,
            PACKAGE_B,
            PACKAGE_C,
            "malformed",
            None,
            PACKAGE_A,
        ]

        with (
            mock.patch.object(packages_api, "shared_state", state),
            mock.patch.object(
                packages_api,
                "request",
                SimpleNamespace(json={"package_ids": package_ids}),
            ),
        ):
            result = delete_route()

        self.assertEqual(
            {
                "deleted": [PACKAGE_A],
                "rejected": [
                    {"package_id": PACKAGE_B, "reason": "not_deferred"},
                    {"package_id": PACKAGE_C, "reason": "not_found"},
                    {
                        "package_id": "malformed",
                        "reason": "invalid_package_id",
                    },
                    {"package_id": None, "reason": "invalid_package_id"},
                    {"package_id": PACKAGE_A, "reason": "duplicate"},
                ],
            },
            result,
        )
        self.assertNotIn(PACKAGE_A, state.protected.rows)
        self.assertIn(PACKAGE_B, state.protected.rows)
        self.assertIn(PACKAGE_D, state.protected.rows)
        self.assertEqual(0, state.device_calls)


if __name__ == "__main__":
    unittest.main()
