# -*- coding: utf-8 -*-

import json
import unittest
from unittest import mock

from bottle import Bottle, HTTPError

from quasarr.api.sponsors_helper import (
    normalize_helper_supported_urls,
    select_helper_package,
    setup_sponsors_helper_routes,
)

PACKAGE_A = "Quasarr_movies_00000000000000000000000000000000"
PACKAGE_B = "Quasarr_movies_11111111111111111111111111111111"
NOW = 1_700_000_000


def protected_package(package_id, title, links, **extra):
    data = {
        "title": title,
        "links": links,
        "password": "",
    }
    data.update(extra)
    return package_id, json.dumps(data)


def package_defer(crypter="filecrypt", active=True, probe_requested=False):
    return {
        "crypter": crypter,
        "reason_code": "ip_block_suspected",
        "since_epoch": NOW - 60,
        "retry_after_epoch": NOW + 900 if active else NOW - 1,
        "probe_requested": probe_requested,
        "observation_holds": 1,
    }


class FakeCooldownService:
    def __init__(
        self,
        cooling_crypters=(),
        package_defers=None,
        failed_probe_consumptions=(),
    ):
        self.cooling_crypters = set(cooling_crypters)
        self.package_defers = package_defers or {}
        self.failed_probe_consumptions = set(failed_probe_consumptions)
        self.probe_consumptions = []

    def snapshot(self, crypter):
        cooling = crypter in self.cooling_crypters
        return {
            "state": "cooldown" if cooling else "available",
            "reason_code": "ip_block_suspected" if cooling else None,
            "first_seen_epoch": NOW - 120 if cooling else 0,
            "last_seen_epoch": NOW - 60 if cooling else 0,
            "retry_after_epoch": NOW + 86_400 if cooling else 0,
            "observations": [],
            "evidence_count": 3 if cooling else 0,
        }

    def is_cooling(self, crypter):
        return crypter in self.cooling_crypters

    def get_package_defer(self, package_id):
        return self.package_defers.get(package_id)

    def project_package_defer(self, deferred, snapshot):
        crypter_retry_after = (
            snapshot["retry_after_epoch"] if snapshot["state"] == "cooldown" else 0
        )
        retry_after_epoch = max(deferred["retry_after_epoch"], crypter_retry_after)
        if crypter_retry_after > NOW:
            hold_type = "crypter_cooldown"
        elif retry_after_epoch > NOW:
            hold_type = "provisional"
        else:
            hold_type = "none"
        projected = dict(deferred)
        projected.update(
            {
                "retry_after_epoch": retry_after_epoch,
                "state": snapshot["state"],
                "evidence_count": snapshot["evidence_count"],
                "hold_type": hold_type,
                "active": hold_type != "none",
            }
        )
        return projected

    def consume_probe(self, package_id, crypter):
        self.probe_consumptions.append((package_id, crypter))
        if package_id in self.failed_probe_consumptions:
            return False
        deferred = self.package_defers.get(package_id)
        if (
            not deferred
            or deferred.get("crypter") != crypter
            or not deferred.get("probe_requested")
        ):
            return False
        deferred["probe_requested"] = False
        return True


class SponsorsHelperApiTests(unittest.TestCase):
    def call_to_decrypt(self, protected_packages, payload, cooldown_service=None):
        app = Bottle()
        setup_sponsors_helper_routes(app)
        route = next(
            route
            for route in app.routes
            if route.rule == "/sponsors_helper/api/to_decrypt/"
        )
        protected_db = mock.Mock()
        protected_db.retrieve_all_titles.return_value = protected_packages

        with (
            mock.patch("quasarr.api.sponsors_helper.shared_state.update"),
            mock.patch(
                "quasarr.api.sponsors_helper.shared_state.get_db",
                return_value=protected_db,
            ),
            mock.patch(
                "quasarr.api.sponsors_helper.request",
                mock.Mock(json=payload),
            ),
            mock.patch(
                "quasarr.api.sponsors_helper.CrypterCooldownService",
                return_value=cooldown_service or FakeCooldownService(),
                create=True,
            ) as cooldown_type,
        ):
            return route.callback(), cooldown_type

    def test_to_decrypt_route_accepts_only_post(self):
        app = Bottle()
        setup_sponsors_helper_routes(app)

        methods = {
            route.method
            for route in app.routes
            if route.rule == "/sponsors_helper/api/to_decrypt/"
        }

        self.assertEqual({"POST"}, methods)

    def test_to_decrypt_route_preserves_invalid_payload_status(self):
        app = Bottle()
        setup_sponsors_helper_routes(app)
        route = next(
            route
            for route in app.routes
            if route.rule == "/sponsors_helper/api/to_decrypt/"
        )
        protected_db = mock.Mock()
        protected_db.retrieve_all_titles.return_value = [("pkg-1", "{}")]

        with (
            mock.patch("quasarr.api.sponsors_helper.shared_state.update"),
            mock.patch(
                "quasarr.api.sponsors_helper.shared_state.get_db",
                return_value=protected_db,
            ),
            mock.patch(
                "quasarr.api.sponsors_helper.request",
                mock.Mock(json={}),
            ),
            self.assertRaises(HTTPError) as context,
        ):
            route.callback()

        self.assertEqual(400, context.exception.status_code)

    def test_normalize_helper_supported_urls_deduplicates_and_lowercases(self):
        self.assertEqual(
            ["container.", "alpha.", "beta."],
            normalize_helper_supported_urls(
                [" Container. ", "ALPHA.", "", None, "beta.", "container."]
            ),
        )

    def test_select_helper_package_moves_supported_url_to_front(self):
        protected_packages = [
            (
                "pkg-1",
                json.dumps(
                    {
                        "title": "Example.Release",
                        "links": [
                            ["https://unsupported.invalid/path", "other"],
                            ["https://container.invalid/Container/abc", "container"],
                        ],
                        "password": "",
                    }
                ),
            )
        ]

        package_id, data, prioritized_links = select_helper_package(
            protected_packages,
            ["container."],
        )

        self.assertEqual("pkg-1", package_id)
        self.assertEqual("Example.Release", data["title"])
        self.assertEqual(
            "https://container.invalid/Container/abc",
            prioritized_links[0][0],
        )

    def test_select_helper_package_skips_unsupported_packages_until_match(self):
        protected_packages = [
            (
                "pkg-1",
                json.dumps(
                    {
                        "title": "Unsupported.First",
                        "links": [["https://unknown.invalid/path", "other"]],
                        "password": "",
                    }
                ),
            ),
            (
                "pkg-2",
                json.dumps(
                    {
                        "title": "Supported.Second",
                        "links": [["https://alpha.invalid/f/abc", "alpha"]],
                        "password": "",
                    }
                ),
            ),
        ]

        package_id, data, prioritized_links = select_helper_package(
            protected_packages,
            ["container.", "alpha."],
        )

        self.assertEqual("pkg-2", package_id)
        self.assertEqual("Supported.Second", data["title"])
        self.assertEqual("https://alpha.invalid/f/abc", prioritized_links[0][0])

    def test_select_helper_package_accepts_advertised_mirror(self):
        protected_packages = [
            (
                "pkg-1",
                json.dumps(
                    {
                        "title": "Example.Release",
                        "links": [["https://source.invalid/release", "he"]],
                        "password": "",
                    }
                ),
            )
        ]

        package_id, _, links = select_helper_package(
            protected_packages, ["container."], ["he"]
        )

        self.assertEqual("pkg-1", package_id)
        self.assertEqual("he", links[0][1])

    def test_select_helper_package_returns_none_when_nothing_matches(self):
        protected_packages = [
            (
                "pkg-1",
                json.dumps(
                    {
                        "title": "Unsupported.Only",
                        "links": [["https://unknown.invalid/path", "other"]],
                        "password": "",
                    }
                ),
            )
        ]

        self.assertIsNone(select_helper_package(protected_packages, ["container."]))

    def test_select_helper_package_orders_links_by_mirror_whitelist(self):
        protected_packages = [
            (
                "Quasarr_movies_hash",
                json.dumps(
                    {
                        "title": "Example.Release",
                        "links": [
                            ["https://a.invalid/1", "ddownload"],
                            ["https://b.invalid/2", "rapidgator"],
                            ["https://c.invalid/3", "turbobit"],
                        ],
                        "password": "",
                    }
                ),
            )
        ]

        with (
            mock.patch(
                "quasarr.api.sponsors_helper.get_download_category_from_package_id",
                return_value="movies",
            ),
            mock.patch(
                "quasarr.api.sponsors_helper.get_download_category_mirrors",
                return_value=["turbobit", "rapidgator"],
            ),
        ):
            _, _, prioritized_links = select_helper_package(protected_packages, [])

        # Whitelist order is the ranking; unlisted mirrors keep their order last.
        self.assertEqual(
            ["turbobit", "rapidgator", "ddownload"],
            [link[1] for link in prioritized_links],
        )

    def test_select_helper_package_falls_back_to_rapidgator_first(self):
        protected_packages = [
            (
                "Quasarr_movies_hash",
                json.dumps(
                    {
                        "title": "Example.Release",
                        "links": [
                            ["https://a.invalid/1", "ddownload"],
                            ["https://b.invalid/2", "rapidgator"],
                        ],
                        "password": "",
                    }
                ),
            )
        ]

        with (
            mock.patch(
                "quasarr.api.sponsors_helper.get_download_category_from_package_id",
                return_value="movies",
            ),
            mock.patch(
                "quasarr.api.sponsors_helper.get_download_category_mirrors",
                return_value=[],
            ),
        ):
            _, _, prioritized_links = select_helper_package(protected_packages, [])

        # No whitelist configured: legacy rapidgator-first default is preserved.
        self.assertEqual(
            ["rapidgator", "ddownload"],
            [link[1] for link in prioritized_links],
        )

    def test_select_helper_package_skips_cooled_link_with_other_crypter_available(self):
        service = FakeCooldownService(cooling_crypters={"filecrypt"})
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Alternate.Crypter",
                [
                    ["https://filecrypt.invalid/container/1", "filecrypt"],
                    ["https://tolink.invalid/container/2", "tolink"],
                ],
            )
        ]

        package_id, _, links = select_helper_package(
            protected_packages,
            ["filecrypt.", "tolink."],
            cooldown_service=service,
        )

        self.assertEqual(PACKAGE_A, package_id)
        self.assertEqual(
            [["https://tolink.invalid/container/2", "tolink"]],
            links,
        )

    def test_select_helper_package_uses_later_package_when_only_link_is_cooled(self):
        service = FakeCooldownService(cooling_crypters={"filecrypt"})
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Cooled.First",
                [["https://filecrypt.invalid/container/1", "filecrypt"]],
            ),
            protected_package(
                PACKAGE_B,
                "Unrelated.Second",
                [["https://tolink.invalid/container/2", "tolink"]],
            ),
        ]

        package_id, data, _ = select_helper_package(
            protected_packages,
            ["filecrypt.", "tolink."],
            cooldown_service=service,
        )

        self.assertEqual(PACKAGE_B, package_id)
        self.assertEqual("Unrelated.Second", data["title"])

    def test_provisional_hold_skips_only_matching_package_and_crypter(self):
        service = FakeCooldownService(package_defers={PACKAGE_A: package_defer()})
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Held.First",
                [
                    ["https://filecrypt.invalid/container/1", "filecrypt"],
                    ["https://tolink.invalid/container/1", "tolink"],
                ],
            ),
            protected_package(
                PACKAGE_B,
                "Same.Crypter.Second",
                [["https://filecrypt.invalid/container/2", "filecrypt"]],
            ),
        ]

        package_id, _, links = select_helper_package(
            protected_packages,
            ["filecrypt.", "tolink."],
            cooldown_service=service,
        )
        later_package_id, _, _ = select_helper_package(
            protected_packages,
            ["filecrypt."],
            cooldown_service=service,
        )

        self.assertEqual(PACKAGE_A, package_id)
        self.assertEqual(
            [["https://tolink.invalid/container/1", "tolink"]],
            links,
        )
        self.assertEqual(PACKAGE_B, later_package_id)

    def test_probe_allows_one_handoff_and_is_consumed_before_return(self):
        service = FakeCooldownService(
            cooling_crypters={"filecrypt"},
            package_defers={PACKAGE_A: package_defer(probe_requested=True)},
        )
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Probe.First",
                [["https://filecrypt.invalid/container/1", "filecrypt"]],
            ),
            protected_package(
                PACKAGE_B,
                "Fallback.Second",
                [["https://tolink.invalid/container/2", "tolink"]],
            ),
        ]

        first_package, _, _ = select_helper_package(
            protected_packages,
            ["filecrypt.", "tolink."],
            cooldown_service=service,
        )
        second_package, _, _ = select_helper_package(
            protected_packages,
            ["filecrypt.", "tolink."],
            cooldown_service=service,
        )

        self.assertEqual(PACKAGE_A, first_package)
        self.assertEqual(PACKAGE_B, second_package)
        self.assertEqual([(PACKAGE_A, "filecrypt")], service.probe_consumptions)

    def test_failed_probe_consumption_skips_the_whole_package(self):
        service = FakeCooldownService(
            cooling_crypters={"filecrypt"},
            package_defers={PACKAGE_A: package_defer(probe_requested=True)},
            failed_probe_consumptions={PACKAGE_A},
        )
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Lost.Probe",
                [
                    ["https://filecrypt.invalid/container/1", "filecrypt"],
                    ["https://tolink.invalid/container/1", "tolink"],
                ],
            ),
            protected_package(
                PACKAGE_B,
                "Fallback.Second",
                [["https://tolink.invalid/container/2", "tolink"]],
            ),
        ]

        package_id, _, _ = select_helper_package(
            protected_packages,
            ["filecrypt.", "tolink."],
            cooldown_service=service,
        )

        self.assertEqual(PACKAGE_B, package_id)
        self.assertEqual([(PACKAGE_A, "filecrypt")], service.probe_consumptions)

    def test_exclusions_ignore_invalid_duplicates_and_cap_valid_ids_at_100(self):
        package_ids = [f"Quasarr_movies_{index:032x}" for index in range(101)]
        protected_packages = [
            protected_package(
                package_id,
                f"Package.{index}",
                [[f"https://tolink.invalid/container/{index}", "tolink"]],
            )
            for index, package_id in enumerate(package_ids)
        ]
        excluded_package_ids = [
            None,
            "not-a-package-id",
            package_ids[0],
            package_ids[0],
            *package_ids[1:100],
            42,
            package_ids[100],
        ]

        package_id, _, _ = select_helper_package(
            protected_packages,
            ["tolink."],
            cooldown_service=FakeCooldownService(),
            excluded_package_ids=excluded_package_ids,
        )

        self.assertEqual(package_ids[100], package_id)

    def test_disabled_packages_stay_excluded_with_cooldown_filtering(self):
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Disabled.First",
                [["https://tolink.invalid/container/1", "tolink"]],
                disabled=True,
            ),
            protected_package(
                PACKAGE_B,
                "Enabled.Second",
                [["https://tolink.invalid/container/2", "tolink"]],
            ),
        ]

        package_id, data, _ = select_helper_package(
            protected_packages,
            ["tolink."],
            cooldown_service=FakeCooldownService(),
        )

        self.assertEqual(PACKAGE_B, package_id)
        self.assertEqual("Enabled.Second", data["title"])

    def test_legacy_request_ignores_cooldown_and_exclusions(self):
        service = FakeCooldownService(cooling_crypters={"filecrypt"})
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Legacy.First",
                [["https://filecrypt.invalid/container/1", "filecrypt"]],
            ),
            protected_package(
                PACKAGE_B,
                "Alternative.Second",
                [["https://tolink.invalid/container/2", "tolink"]],
            ),
        ]

        result, cooldown_type = self.call_to_decrypt(
            protected_packages,
            {
                "supported_urls": ["filecrypt.", "tolink."],
                "excluded_package_ids": [PACKAGE_A],
            },
            cooldown_service=service,
        )

        self.assertEqual("Legacy.First", result["to_decrypt"]["name"])
        self.assertEqual(
            [["https://filecrypt.invalid/container/1", "filecrypt"]],
            result["to_decrypt"]["url"],
        )
        cooldown_type.assert_not_called()

    def test_capable_request_filters_cooled_package_and_selects_alternative(self):
        service = FakeCooldownService(cooling_crypters={"filecrypt"})
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Cooled.First",
                [["https://filecrypt.invalid/container/1", "filecrypt"]],
            ),
            protected_package(
                PACKAGE_B,
                "Alternative.Second",
                [["https://tolink.invalid/container/2", "tolink"]],
            ),
        ]

        result, cooldown_type = self.call_to_decrypt(
            protected_packages,
            {
                "supported_urls": ["filecrypt.", "tolink."],
                "capabilities": ["crypter_defer_v1"],
            },
            cooldown_service=service,
        )

        self.assertEqual("Alternative.Second", result["to_decrypt"]["name"])
        cooldown_type.assert_called_once()

    def test_capable_request_applies_excluded_package_ids(self):
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Excluded.First",
                [["https://tolink.invalid/container/1", "tolink"]],
            ),
            protected_package(
                PACKAGE_B,
                "Eligible.Second",
                [["https://tolink.invalid/container/2", "tolink"]],
            ),
        ]

        result, _ = self.call_to_decrypt(
            protected_packages,
            {
                "supported_urls": ["tolink."],
                "capabilities": ["crypter_defer_v1"],
                "excluded_package_ids": [PACKAGE_A],
            },
        )

        self.assertEqual("Eligible.Second", result["to_decrypt"]["name"])

    def test_legacy_request_retains_exact_response_shape(self):
        protected_packages = [
            protected_package(
                PACKAGE_A,
                "Legacy.Shape",
                [["https://filecrypt.invalid/container/1", "filecrypt"]],
            )
        ]

        result, _ = self.call_to_decrypt(
            protected_packages,
            {"supported_urls": ["filecrypt."]},
        )

        self.assertEqual(
            {
                "to_decrypt": {
                    "name": "Legacy.Shape",
                    "id": PACKAGE_A,
                    "url": [["https://filecrypt.invalid/container/1", "filecrypt"]],
                    "mirror": "filecrypt",
                    "password": "",
                    "max_attempts": 3,
                }
            },
            result,
        )


if __name__ == "__main__":
    unittest.main()
