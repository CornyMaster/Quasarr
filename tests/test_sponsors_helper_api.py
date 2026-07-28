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


class SponsorsHelperApiTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
