# -*- coding: utf-8 -*-

import json
import unittest
from unittest.mock import MagicMock, call, patch

from quasarr.downloads import fail, failed_row_value


class FailedRowValueTests(unittest.TestCase):
    """The exact stored shape of one failed-history row."""

    def test_a_row_carries_the_title_and_reason_and_nothing_else(self):
        stored = json.loads(json.loads(failed_row_value("Title", "Reason")))

        self.assertEqual({"title": "Title", "error": "Reason"}, stored)

    def test_an_operation_row_carries_the_evidence_it_was_written_with(self):
        stored = json.loads(json.loads(failed_row_value("Title", "Reason", "a" * 64)))

        self.assertEqual(
            {"title": "Title", "error": "Reason", "terminal_operation": "a" * 64},
            stored,
        )


class DownloadFailureLoggingTests(unittest.TestCase):
    def test_terminal_failure_logs_error_and_failed_state_warning(self):
        shared_state = MagicMock()
        stats = MagicMock()

        with (
            patch("quasarr.downloads.StatsHelper", return_value=stats),
            patch("quasarr.downloads.error") as error_log,
            patch("quasarr.downloads.warn") as warn_log,
        ):
            result = fail(
                "Synthetic.Release",
                "synthetic-package-id",
                shared_state,
                reason="Synthetic redirect failure",
            )

        self.assertEqual(
            {"success": True, "title": "Synthetic.Release", "failed": True}, result
        )
        error_log.assert_called_once_with(
            "Reason for failure: Synthetic redirect failure"
        )
        warn_log.assert_called_once_with(
            'Package "Synthetic.Release" marked as failed!'
        )
        stats.increment_failed_downloads.assert_called_once_with()
        shared_state.get_db.return_value.store.assert_called_once()

    def test_the_failure_is_stored_before_it_is_ever_counted(self):
        """A counter that runs first can answer for a row that never landed."""
        shared_state = MagicMock()
        stats = MagicMock()
        order = []

        def record_store(*args, **kwargs):
            order.append("store")

        shared_state.get_db.return_value.store.side_effect = record_store
        stats.increment_failed_downloads.side_effect = lambda: order.append("count")

        with (
            patch("quasarr.downloads.StatsHelper", return_value=stats),
            patch("quasarr.downloads.error"),
            patch("quasarr.downloads.warn"),
        ):
            fail("Synthetic.Release", "synthetic-package-id", shared_state)

        self.assertEqual(["store", "count"], order)

    def test_a_failure_that_never_persisted_is_never_reported_as_recorded(self):
        shared_state = MagicMock()
        stats = MagicMock()
        shared_state.get_db.return_value.store.side_effect = RuntimeError(
            "synthetic database failure"
        )

        with (
            patch("quasarr.downloads.StatsHelper", return_value=stats),
            patch("quasarr.downloads.error"),
            patch("quasarr.downloads.warn") as warn_log,
        ):
            result = fail(
                "Synthetic.Release",
                "synthetic-package-id",
                shared_state,
                reason="Synthetic redirect failure",
            )

        self.assertEqual(
            {"success": False, "title": "Synthetic.Release", "failed": True}, result
        )
        stats.increment_failed_downloads.assert_not_called()
        warn_log.assert_not_called()

    def test_failure_persistence_exception_logs_error(self):
        shared_state = MagicMock()
        shared_state.get_db.side_effect = RuntimeError("synthetic database failure")

        with (
            patch("quasarr.downloads.StatsHelper"),
            patch("quasarr.downloads.error") as error_log,
            patch("quasarr.downloads.warn") as warn_log,
        ):
            fail(
                "Synthetic.Release",
                "synthetic-package-id",
                shared_state,
                reason="Synthetic redirect failure",
            )

        self.assertEqual(
            [
                call("Reason for failure: Synthetic redirect failure"),
                call(
                    'Error marking package "synthetic-package-id" as failed: '
                    "synthetic database failure"
                ),
            ],
            error_log.call_args_list,
        )
        warn_log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
