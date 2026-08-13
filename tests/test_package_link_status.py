# -*- coding: utf-8 -*-

import unittest

from quasarr.downloads.packages import (
    get_links_comment,
    get_links_status,
    is_not_downloadable,
    is_quasarr_package,
)
from quasarr.providers.terminal_operations import submission_comment

PACKAGE = {"uuid": 1, "name": "Synthetic.Release.Example"}


def make_link(
    uuid, url, availability="ONLINE", status="", finished=False, status_icon=""
):
    return {
        "uuid": uuid,
        "packageUUID": 1,
        "name": f"file-{uuid}.mkv",
        "url": url,
        "availability": availability,
        "status": status,
        "finished": finished,
        "statusIconKey": status_icon,
    }


class IsQuasarrPackageTests(unittest.TestCase):
    def test_accepts_every_download_category_the_settings_allow(self):
        # add_download_category() accepts ^[a-z0-9]+$, so any package ID built
        # from a custom category must satisfy the canonical ID contract too.
        for category in ("movies", "tv", "movies4k", "4k", "docs2"):
            with self.subTest(category=category):
                self.assertTrue(is_quasarr_package(f"Quasarr_{category}_" + "a1" * 16))

    def test_rejects_ids_outside_the_package_id_contract(self):
        for package_id in (
            "Quasarr_Movies_" + "a" * 32,  # uppercase category
            "Quasarr_movies-4k_" + "a" * 32,  # punctuation in category
            "Quasarr_movies_4k_" + "a" * 32,  # extra separator
            "Quasarr__" + "a" * 32,  # empty category
            "Quasarr_movies_" + "A" * 32,  # uppercase hash
            "Quasarr_movies_" + "g" * 32,  # non-hex hash
            "Quasarr_movies_" + "a" * 31,  # short hash
            "quasarr_movies_" + "a" * 32,  # wrong prefix casing
            "0-invalid-package",
            "",
            None,
        ):
            with self.subTest(package_id=package_id):
                self.assertFalse(is_quasarr_package(package_id))


class LinksCommentTests(unittest.TestCase):
    """A terminal submission stays a Quasarr package everywhere it is read.

    The additive operation marker travels in the same comment field, so the
    package projection has to read the package ID out of it - otherwise every
    version-two download would look foreign and lose its category, status and
    auto-start.
    """

    PACKAGE_ID = "Quasarr_movies_" + "a1" * 16

    def link(self, comment):
        return {"uuid": 9, "packageUUID": 1, "comment": comment}

    def test_a_marked_comment_resolves_to_its_package_id(self):
        marked = submission_comment(self.PACKAGE_ID, "c0" * 32)

        self.assertEqual(
            self.PACKAGE_ID, get_links_comment(PACKAGE, [self.link(marked)])
        )

    def test_a_bare_comment_keeps_resolving_unchanged(self):
        self.assertEqual(
            self.PACKAGE_ID,
            get_links_comment(PACKAGE, [self.link(self.PACKAGE_ID)]),
        )

    def test_a_marked_comment_is_preferred_over_a_foreign_fallback(self):
        marked = submission_comment(self.PACKAGE_ID, "c0" * 32)

        self.assertEqual(
            self.PACKAGE_ID,
            get_links_comment(PACKAGE, [self.link("foreign"), self.link(marked)]),
        )

    def test_a_foreign_comment_is_still_reported_verbatim(self):
        self.assertEqual(
            "foreign comment",
            get_links_comment(PACKAGE, [self.link("foreign comment")]),
        )


class IsNotDownloadableTests(unittest.TestCase):
    def test_matches_status_case_insensitively(self):
        self.assertTrue(is_not_downloadable("Not downloadable!"))
        self.assertTrue(is_not_downloadable("NOT DOWNLOADABLE! (Premium needed)"))

    def test_ignores_other_or_missing_status(self):
        self.assertFalse(is_not_downloadable("Extraction OK"))
        self.assertFalse(is_not_downloadable(""))
        self.assertFalse(is_not_downloadable(None))


class GetLinksStatusNotDownloadableTests(unittest.TestCase):
    def test_all_mirrors_not_downloadable_sets_error(self):
        links = [
            make_link(11, "http://mirror-a.invalid/f1", status="Not downloadable!"),
            make_link(12, "http://mirror-a.invalid/f2", status="Not downloadable!"),
        ]

        result = get_links_status(PACKAGE, links)

        self.assertEqual(result["error"], "Links not downloadable for all mirrors")
        self.assertFalse(result["all_finished"])

    def test_not_downloadable_mirror_does_not_count_as_online(self):
        links = [
            make_link(11, "http://mirror-a.invalid/f1", availability="OFFLINE"),
            make_link(21, "http://mirror-b.invalid/f1", status="Not downloadable!"),
        ]

        result = get_links_status(PACKAGE, links)

        self.assertIn("for all mirrors", result["error"])
        self.assertEqual(result["offline_mirror_linkids"], [])

    def test_online_mirror_suppresses_not_downloadable_error(self):
        links = [
            make_link(11, "http://mirror-a.invalid/f1", finished=True),
            make_link(21, "http://mirror-b.invalid/f1", status="Not downloadable!"),
        ]

        result = get_links_status(PACKAGE, links)

        self.assertIsNone(result["error"])
        self.assertFalse(result["all_finished"])
        # A not-downloadable link keeps availability "online", so it must be
        # queued for removal by id (not via the offline-only cleanup), otherwise
        # it lingers and keeps the package from ever finishing.
        self.assertEqual(result["not_downloadable_linkids"], [21])
        self.assertEqual(result["offline_mirror_linkids"], [])

    def test_offline_links_collected_for_cleanup_with_online_mirror(self):
        links = [
            make_link(11, "http://mirror-a.invalid/f1", finished=True),
            make_link(21, "http://mirror-b.invalid/f1", availability="OFFLINE"),
        ]

        result = get_links_status(PACKAGE, links)

        self.assertIsNone(result["error"])
        self.assertEqual(result["offline_mirror_linkids"], [21])
        self.assertEqual(result["not_downloadable_linkids"], [])

    def test_download_list_mirror_healthy_without_availability(self):
        # Links already in JD's download list have no "availability" field. A
        # healthy mirror there (empty availability) must still count as online so
        # a not-downloadable sibling is collected for removal instead of taking
        # the all-mirrors error path.
        links = [
            make_link(11, "http://mirror-a.invalid/f1", availability="", finished=True),
            make_link(
                21,
                "http://mirror-b.invalid/f1",
                availability="",
                status="Not downloadable!",
            ),
        ]

        result = get_links_status(PACKAGE, links)

        self.assertIsNone(result["error"])
        self.assertEqual(result["not_downloadable_linkids"], [21])
        self.assertEqual(result["offline_mirror_linkids"], [])

    def test_file_error_link_removed_when_mirror_healthy(self):
        # In the download list, JD reports a dead link via statusIconKey "false"
        # (no "availability"/offline). With a healthy mirror it must be removed,
        # not fail the whole package.
        links = [
            make_link(11, "http://mirror-a.invalid/f1", availability="", finished=True),
            make_link(
                21, "http://mirror-b.invalid/f1", availability="", status_icon="false"
            ),
        ]

        result = get_links_status(PACKAGE, links)

        self.assertIsNone(result["error"])
        self.assertEqual(result["file_error_linkids"], [21])

    def test_file_error_link_fails_package_without_mirror(self):
        # Only mirror has a file error -> no healthy fallback -> package fails.
        links = [
            make_link(
                11, "http://mirror-a.invalid/f1", availability="", status_icon="false"
            ),
        ]

        result = get_links_status(PACKAGE, links)

        self.assertEqual(result["error"], "File error in package")
        self.assertEqual(result["file_error_linkids"], [])


if __name__ == "__main__":
    unittest.main()
