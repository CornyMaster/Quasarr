# -*- coding: utf-8 -*-

import json
import unittest
from inspect import signature
from unittest.mock import MagicMock, patch

from quasarr.downloads import (
    SUBMIT_PHASE_ALL,
    SUBMIT_PHASE_SUBMIT,
    confirm_protected_removal,
    failed_package_records_operation,
    jdownloader_holds_operation,
    submit_final_download_urls,
)
from quasarr.downloads.mirror_filters import (
    filter_final_download_urls,
    normalize_mirror_token,
)
from quasarr.providers.terminal_operations import submission_comment


class NormalizeMirrorTokenTests(unittest.TestCase):
    def test_normalizes_whitelist_names(self):
        self.assertEqual(normalize_mirror_token("DDownload"), "ddownload")
        self.assertEqual(normalize_mirror_token("Keep2Share"), "keep2share")

    def test_normalizes_alias_domains_and_subdomains(self):
        cases = {
            "https://api.ddl.to/container": "ddownload",
            "https://s42.rg.to/file/abc": "rapidgator",
            "https://subdomain.nitroflare.com/view/test": "nitroflare",
            "https://download.ifolder.com.ua/file/123": "turbobit",
            "https://mega.co.nz/file/test": "mega",
            "https://clickndownload.space/abcdef": "clicknupload",
        }

        for raw_value, expected in cases.items():
            with self.subTest(raw_value=raw_value):
                self.assertEqual(normalize_mirror_token(raw_value), expected)


class FilterFinalDownloadUrlsTests(unittest.TestCase):
    def test_keeps_only_allowed_final_urls(self):
        result = filter_final_download_urls(
            [
                "https://rapidgator.net/file/abc",
                "https://cdn.ddownload.com/xyz",
                "https://nitroflare.com/view/test",
            ],
            ["DDownload"],
        )

        self.assertEqual(result["urls"], ["https://cdn.ddownload.com/xyz"])
        self.assertEqual(
            {item["token"] for item in result["dropped"]},
            {"rapidgator", "nitroflare"},
        )


class SubmitFinalDownloadUrlsTests(unittest.TestCase):
    @patch("quasarr.downloads.download_package", return_value=True)
    @patch(
        "quasarr.downloads.get_download_category_mirrors", return_value=["DDownload"]
    )
    @patch("quasarr.downloads.get_download_category_from_package_id", return_value="tv")
    def test_submit_uses_filtered_urls(
        self,
        mock_get_category,
        mock_get_mirrors,
        mock_download_package,
    ):
        shared_state = MagicMock()

        result = submit_final_download_urls(
            shared_state,
            [
                "https://rapidgator.net/file/abc",
                "https://mirror.ddownload.com/file/def",
            ],
            "Example.Release",
            "",
            "Quasarr_tv_deadbeefdeadbeefdeadbeefdeadbeef",
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["links"], ["https://mirror.ddownload.com/file/def"])
        mock_download_package.assert_called_once_with(
            ["https://mirror.ddownload.com/file/def"],
            "Example.Release",
            "",
            "Quasarr_tv_deadbeefdeadbeefdeadbeefdeadbeef",
            shared_state,
            comment="Quasarr_tv_deadbeefdeadbeefdeadbeefdeadbeef",
        )

    @patch("quasarr.downloads.download_package")
    @patch("quasarr.downloads.fail", return_value={"success": True, "failed": True})
    @patch(
        "quasarr.downloads.get_download_category_mirrors", return_value=["DDownload"]
    )
    @patch("quasarr.downloads.get_download_category_from_package_id", return_value="tv")
    def test_submit_persists_failure_when_no_allowed_links_remain(
        self,
        mock_get_category,
        mock_get_mirrors,
        mock_fail,
        mock_download_package,
    ):
        protected_db = MagicMock()
        protected_db.retrieve.return_value = json.dumps(
            {
                "title": "Example.Release",
                "notifications": {"discord": {"message_id": "123"}},
            }
        )
        shared_state = MagicMock()
        shared_state.get_db.return_value = protected_db

        with patch("quasarr.downloads.update_release_notification") as mock_update:
            result = submit_final_download_urls(
                shared_state,
                ["https://rapidgator.net/file/abc"],
                "Example.Release",
                "",
                "Quasarr_tv_deadbeefdeadbeefdeadbeefdeadbeef",
                remove_protected=True,
            )

        self.assertFalse(result["success"])
        self.assertTrue(result["persisted_failure"])
        protected_db.delete.assert_called_once_with(
            "Quasarr_tv_deadbeefdeadbeefdeadbeefdeadbeef"
        )
        mock_fail.assert_called_once()
        mock_download_package.assert_not_called()
        self.assertEqual("failed", mock_update.call_args.args[2].value)
        self.assertIn("reason", mock_update.call_args.kwargs["details"])

    @patch("quasarr.downloads.download_package", return_value=True)
    @patch(
        "quasarr.downloads.get_download_category_mirrors", return_value=["DDownload"]
    )
    @patch("quasarr.downloads.get_download_category_from_package_id", return_value="tv")
    def test_submit_removes_protected_package_after_success_when_requested(
        self,
        mock_get_category,
        mock_get_mirrors,
        mock_download_package,
    ):
        protected_db = MagicMock()
        protected_db.retrieve.return_value = json.dumps(
            {
                "title": "Example.Release",
                "notifications": {"discord": {"message_id": "123"}},
            }
        )
        shared_state = MagicMock()
        shared_state.get_db.return_value = protected_db

        with patch("quasarr.downloads.update_release_notification") as mock_update:
            result = submit_final_download_urls(
                shared_state,
                ["https://mirror.ddownload.com/file/def"],
                "Example.Release",
                "",
                "Quasarr_tv_deadbeefdeadbeefdeadbeefdeadbeef",
                remove_protected=True,
                notification_details={"method": "manual"},
            )

        self.assertTrue(result["success"])
        protected_db.delete.assert_called_once_with(
            "Quasarr_tv_deadbeefdeadbeefdeadbeefdeadbeef"
        )
        mock_download_package.assert_called_once()
        self.assertEqual("solved", mock_update.call_args.args[2].value)
        self.assertEqual(
            {"method": "manual"},
            mock_update.call_args.kwargs["details"],
        )

    @patch("quasarr.downloads.download_package", return_value=True)
    @patch(
        "quasarr.downloads.get_download_category_mirrors", return_value=["DDownload"]
    )
    @patch("quasarr.downloads.get_download_category_from_package_id", return_value="tv")
    def test_the_submit_phase_leaves_the_protected_package_to_the_caller(
        self,
        mock_get_category,
        mock_get_mirrors,
        mock_download_package,
    ):
        protected_db = MagicMock()
        protected_db.retrieve.return_value = json.dumps({"title": "Example.Release"})
        shared_state = MagicMock()
        shared_state.get_db.return_value = protected_db

        with patch("quasarr.downloads.update_release_notification") as mock_update:
            result = submit_final_download_urls(
                shared_state,
                ["https://mirror.ddownload.com/file/def"],
                "Example.Release",
                "",
                "Quasarr_tv_deadbeefdeadbeefdeadbeefdeadbeef",
                remove_protected=True,
                phase=SUBMIT_PHASE_SUBMIT,
            )

        self.assertEqual(
            {"success": True, "links": ["https://mirror.ddownload.com/file/def"]},
            result,
        )
        mock_download_package.assert_called_once()
        protected_db.delete.assert_not_called()
        mock_update.assert_not_called()

    @patch("quasarr.downloads.download_package")
    @patch("quasarr.downloads.fail", return_value={"success": True, "failed": True})
    @patch(
        "quasarr.downloads.get_download_category_mirrors", return_value=["DDownload"]
    )
    @patch("quasarr.downloads.get_download_category_from_package_id", return_value="tv")
    def test_the_submit_phase_still_persists_a_whitelist_rejection(
        self,
        mock_get_category,
        mock_get_mirrors,
        mock_fail,
        mock_download_package,
    ):
        protected_db = MagicMock()
        protected_db.retrieve.return_value = json.dumps({"title": "Example.Release"})
        shared_state = MagicMock()
        shared_state.get_db.return_value = protected_db

        with patch("quasarr.downloads.update_release_notification") as mock_update:
            result = submit_final_download_urls(
                shared_state,
                ["https://rapidgator.net/file/abc"],
                "Example.Release",
                "",
                "Quasarr_tv_deadbeefdeadbeefdeadbeefdeadbeef",
                remove_protected=True,
                phase=SUBMIT_PHASE_SUBMIT,
            )

        self.assertTrue(result["persisted_failure"])
        mock_fail.assert_called_once()
        protected_db.delete.assert_called_once()
        self.assertEqual("failed", mock_update.call_args.args[2].value)
        mock_download_package.assert_not_called()

    def test_the_default_phase_is_the_unchanged_whole_funnel(self):
        self.assertEqual("all", SUBMIT_PHASE_ALL)
        self.assertEqual(
            "all", signature(submit_final_download_urls).parameters["phase"].default
        )

    @patch("quasarr.downloads.download_package", return_value=True)
    @patch("quasarr.downloads.get_download_category_mirrors", return_value=[])
    @patch("quasarr.downloads.get_download_category_from_package_id", return_value="tv")
    def test_a_submission_travels_its_operation_only_when_one_is_named(
        self,
        mock_get_category,
        mock_get_mirrors,
        mock_download_package,
    ):
        shared_state = MagicMock()
        evidence = "c0" * 32

        submit_final_download_urls(
            shared_state,
            ["https://mirror.ddownload.com/file/def"],
            "Example.Release",
            "",
            "Quasarr_tv_deadbeefdeadbeefdeadbeefdeadbeef",
            terminal_operation=evidence,
        )
        submit_final_download_urls(
            shared_state,
            ["https://mirror.ddownload.com/file/def"],
            "Example.Release",
            "",
            "Quasarr_tv_deadbeefdeadbeefdeadbeefdeadbeef",
        )

        self.assertEqual(
            [
                submission_comment(
                    "Quasarr_tv_deadbeefdeadbeefdeadbeefdeadbeef", evidence
                ),
                "Quasarr_tv_deadbeefdeadbeefdeadbeefdeadbeef",
            ],
            [call.kwargs["comment"] for call in mock_download_package.call_args_list],
        )
        self.assertIsNone(
            signature(submit_final_download_urls)
            .parameters["terminal_operation"]
            .default
        )

    @patch("quasarr.downloads.download_package")
    @patch("quasarr.downloads.fail", return_value={"success": True, "failed": True})
    @patch(
        "quasarr.downloads.get_download_category_mirrors", return_value=["DDownload"]
    )
    @patch("quasarr.downloads.get_download_category_from_package_id", return_value="tv")
    def test_a_whitelist_rejection_records_the_operation_that_caused_it(
        self,
        mock_get_category,
        mock_get_mirrors,
        mock_fail,
        mock_download_package,
    ):
        protected_db = MagicMock()
        protected_db.retrieve.return_value = json.dumps({"title": "Example.Release"})
        shared_state = MagicMock()
        shared_state.get_db.return_value = protected_db
        evidence = "c0" * 32

        with patch("quasarr.downloads.update_release_notification"):
            submit_final_download_urls(
                shared_state,
                ["https://rapidgator.net/file/abc"],
                "Example.Release",
                "",
                "Quasarr_tv_deadbeefdeadbeefdeadbeefdeadbeef",
                remove_protected=True,
                phase=SUBMIT_PHASE_SUBMIT,
                terminal_operation=evidence,
            )

        self.assertEqual(evidence, mock_fail.call_args.kwargs["terminal_operation"])


class ProtectedTable:
    def __init__(self, rows=None, deletes=True):
        self.rows = dict(rows or {})
        self.deletes = deletes
        self.delete_calls = 0
        self.unreadable = False

    def retrieve(self, key):
        if self.unreadable:
            raise RuntimeError("protected storage unavailable")
        return self.rows.get(key)

    def delete(self, key):
        self.delete_calls += 1
        if self.deletes:
            self.rows.pop(key, None)
        return True


class ConfirmProtectedRemovalTests(unittest.TestCase):
    PACKAGE_ID = "Quasarr_tv_deadbeefdeadbeefdeadbeefdeadbeef"

    def shared_state(self, table):
        state = MagicMock()
        state.get_db.return_value = table
        return state

    def test_a_present_package_is_removed_proven_gone_and_notified_once(self):
        table = ProtectedTable({self.PACKAGE_ID: json.dumps({"title": "Example"})})
        state = self.shared_state(table)

        with patch("quasarr.downloads.update_release_notification") as mock_update:
            first = confirm_protected_removal(
                state, self.PACKAGE_ID, notification_details={"method": "helper"}
            )
            second = confirm_protected_removal(state, self.PACKAGE_ID)

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(1, table.delete_calls)
        self.assertEqual(1, mock_update.call_count)
        self.assertEqual("solved", mock_update.call_args.args[2].value)

    def test_an_already_absent_package_counts_as_removed_without_notifying(self):
        table = ProtectedTable()
        state = self.shared_state(table)

        with patch("quasarr.downloads.update_release_notification") as mock_update:
            removed = confirm_protected_removal(state, self.PACKAGE_ID)

        self.assertTrue(removed)
        self.assertEqual(0, table.delete_calls)
        mock_update.assert_not_called()

    def test_a_package_that_survives_deletion_is_never_reported_as_removed(self):
        table = ProtectedTable(
            {self.PACKAGE_ID: json.dumps({"title": "Example"})}, deletes=False
        )
        state = self.shared_state(table)

        with patch("quasarr.downloads.update_release_notification") as mock_update:
            removed = confirm_protected_removal(state, self.PACKAGE_ID)

        self.assertFalse(removed)
        mock_update.assert_not_called()

    def test_an_unreadable_store_never_proves_removal(self):
        table = ProtectedTable({self.PACKAGE_ID: json.dumps({"title": "Example"})})
        table.unreadable = True
        state = self.shared_state(table)

        with patch("quasarr.downloads.update_release_notification") as mock_update:
            removed = confirm_protected_removal(state, self.PACKAGE_ID)

        self.assertFalse(removed)
        self.assertEqual(0, table.delete_calls)
        mock_update.assert_not_called()


class JDownloaderReconciliationTests(unittest.TestCase):
    PACKAGE_ID = "Quasarr_tv_deadbeefdeadbeefdeadbeefdeadbeef"
    EVIDENCE = "c0" * 32

    def shared_state(self, device, available=True):
        state = MagicMock()

        def run_device_request(_name, request_fn, default=None):
            return request_fn(device) if available else default

        state.run_device_request.side_effect = run_device_request
        return state

    def device(self, **lists):
        device = MagicMock()
        device.linkgrabber.query_packages.return_value = lists.get("packages", [])
        device.linkgrabber.query_links.return_value = lists.get("links", [])
        device.downloads.query_packages.return_value = lists.get(
            "downloader_packages", []
        )
        device.downloads.query_links.return_value = lists.get("downloader_links", [])
        return device

    def marked(self):
        return {"comment": submission_comment(self.PACKAGE_ID, self.EVIDENCE)}

    def test_every_list_carrying_this_operations_comment_proves_a_submission(self):
        cases = (
            "packages",
            "links",
            "downloader_packages",
            "downloader_links",
        )

        for name in cases:
            with self.subTest(list=name):
                device = self.device(**{name: [self.marked()]})
                state = self.shared_state(device)

                self.assertIs(
                    True,
                    jdownloader_holds_operation(state, self.PACKAGE_ID, self.EVIDENCE),
                )

    def test_a_package_of_an_earlier_lifecycle_never_proves_this_submission(self):
        # The bare package ID is what every legacy, manual and earlier
        # submission of this very release carries, so it can only prove that
        # some submission happened - never that this operation made it.
        device = self.device(
            packages=[{"comment": self.PACKAGE_ID}],
            links=[{"comment": submission_comment(self.PACKAGE_ID, "d1" * 32)}],
        )
        state = self.shared_state(device)

        self.assertIs(
            False,
            jdownloader_holds_operation(state, self.PACKAGE_ID, self.EVIDENCE),
        )

    def test_a_foreign_comment_never_proves_a_submission(self):
        device = self.device(
            packages=[
                {
                    "comment": submission_comment(
                        "Quasarr_tv_00000000000000000000000000000000", self.EVIDENCE
                    )
                }
            ],
            links=[{"comment": None}, "not-a-mapping"],
        )
        state = self.shared_state(device)

        self.assertIs(
            False,
            jdownloader_holds_operation(state, self.PACKAGE_ID, self.EVIDENCE),
        )

    def test_an_unreachable_device_answers_unknown_rather_than_absent(self):
        device = self.device()
        state = self.shared_state(device, available=False)

        self.assertIsNone(
            jdownloader_holds_operation(state, self.PACKAGE_ID, self.EVIDENCE)
        )

    def test_the_linkgrabber_is_asked_before_the_download_list(self):
        device = self.device(packages=[self.marked()])
        state = self.shared_state(device)

        jdownloader_holds_operation(state, self.PACKAGE_ID, self.EVIDENCE)

        device.downloads.query_packages.assert_not_called()
        device.downloads.query_links.assert_not_called()


class FailedHistoryProvenanceTests(unittest.TestCase):
    """Failed history only answers for the operation that actually wrote it."""

    PACKAGE_ID = "Quasarr_tv_deadbeefdeadbeefdeadbeefdeadbeef"
    EVIDENCE = "c0" * 32

    def shared_state(self, table):
        state = MagicMock()
        state.get_db.return_value = table
        return state

    def row(self, **extra):
        blob = {"title": "Example.Release", "error": "boom"}
        blob.update(extra)
        return json.dumps(json.dumps(blob))

    def test_a_row_this_operation_wrote_is_proven(self):
        table = MagicMock()
        table.retrieve.return_value = self.row(terminal_operation=self.EVIDENCE)

        self.assertIs(
            True,
            failed_package_records_operation(
                self.shared_state(table), self.PACKAGE_ID, self.EVIDENCE
            ),
        )

    def test_a_row_of_an_earlier_lifecycle_proves_nothing(self):
        for stored in (self.row(), self.row(terminal_operation="d1" * 32)):
            with self.subTest(stored=stored[-40:]):
                table = MagicMock()
                table.retrieve.return_value = stored

                self.assertIs(
                    False,
                    failed_package_records_operation(
                        self.shared_state(table), self.PACKAGE_ID, self.EVIDENCE
                    ),
                )

    def test_a_missing_or_unreadable_row_answers_absent_rather_than_unknown(self):
        for stored in (None, "{not json", '"text"', "[1, 2]"):
            with self.subTest(stored=str(stored)):
                table = MagicMock()
                table.retrieve.return_value = stored

                self.assertIs(
                    False,
                    failed_package_records_operation(
                        self.shared_state(table), self.PACKAGE_ID, self.EVIDENCE
                    ),
                )

    def test_an_unreadable_store_is_the_only_unknown(self):
        table = MagicMock()
        table.retrieve.side_effect = RuntimeError("failed storage unavailable")

        self.assertIsNone(
            failed_package_records_operation(
                self.shared_state(table), self.PACKAGE_ID, self.EVIDENCE
            )
        )


if __name__ == "__main__":
    unittest.main()
