# -*- coding: utf-8 -*-

"""Contracts for the persisted package origin record.

Covers the derivation of crypter/host from a link, the write-once rule that
keeps `added_epoch` at the first contact, the read-side sanitizing that stops
a stored URL from ever reaching the Downloads response, and the delete path.
"""

import json
import threading
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from quasarr.providers.package_origin import (
    PACKAGE_ORIGIN_TABLE,
    crypter_label,
    forget_package_origin,
    read_package_origins,
    record_package_origin,
    safe_mirror,
)

PACKAGE_A = "Quasarr_movies_" + "a" * 32
PACKAGE_B = "Quasarr_movies_" + "b" * 32
NOW = 1_700_000_000


class FakeDatabase:
    def __init__(self):
        self.rows = {}
        self.lock = threading.Lock()

    def retrieve_all_titles(self):
        items = [[key, value] for key, value in sorted(self.rows.items())]
        return items if items else None

    def mutate_value(self, key, mutator):
        with self.lock:
            value = mutator(self.rows.get(key))
            if value is None:
                self.rows.pop(key, None)
            else:
                self.rows[key] = value
            return value


class FakeSharedState:
    def __init__(self):
        self.databases = {}

    def get_db(self, table):
        if table not in self.databases:
            self.databases[table] = FakeDatabase()
        return self.databases[table]


class SafeMirrorTests(unittest.TestCase):
    def test_plain_hosts_pass_through_lowercased(self):
        self.assertEqual("crypter.invalid", safe_mirror("Crypter.Invalid"))
        self.assertEqual("a.b.crypter.invalid", safe_mirror("a.b.crypter.invalid"))
        self.assertEqual("crypter.invalid:8443", safe_mirror("crypter.invalid:8443"))

    def test_anything_carrying_more_than_a_host_is_dropped(self):
        rejected = (
            "https://crypter.invalid/container/1",
            "crypter.invalid/container",
            "user:pw@crypter.invalid",
            "crypter.invalid?a=b",
            "crypter invalid",
            "crypter.invalid:",
            "",
            None,
            123,
            "x" * 300,
        )
        for value in rejected:
            with self.subTest(value=value):
                self.assertEqual("", safe_mirror(value))


class CrypterLabelTests(unittest.TestCase):
    def test_known_keys_get_their_display_name(self):
        self.assertEqual("FileCrypt", crypter_label("filecrypt"))
        self.assertEqual("Junkies", crypter_label("junkies"))
        self.assertEqual("KeepLinks", crypter_label("keeplinks"))
        self.assertEqual("ToLink", crypter_label("tolink"))
        self.assertEqual("Hide", crypter_label("hide"))
        self.assertEqual("Direct", crypter_label("direct"))

    def test_unknown_and_empty_keys_stay_readable(self):
        self.assertEqual("Somecrypter", crypter_label("somecrypter"))
        self.assertEqual("", crypter_label(""))
        self.assertEqual("", crypter_label(None))


class RecordOriginTests(unittest.TestCase):
    def setUp(self):
        self.shared_state = FakeSharedState()

    def _stored(self, package_id):
        return json.loads(
            self.shared_state.get_db(PACKAGE_ORIGIN_TABLE).rows[package_id]
        )

    def test_record_stores_crypter_mirror_and_epoch(self):
        self.assertTrue(
            record_package_origin(
                self.shared_state,
                PACKAGE_A,
                "filecrypt",
                "https://crypter.invalid/container/1",
                now=NOW,
            )
        )
        self.assertEqual(
            {
                "schema_version": 1,
                "crypter": "filecrypt",
                "mirror": "crypter.invalid",
                "added_epoch": NOW,
            },
            self._stored(PACKAGE_A),
        )

    def test_record_never_overwrites_an_existing_row(self):
        record_package_origin(
            self.shared_state, PACKAGE_A, "filecrypt", "https://one.invalid/c", now=NOW
        )
        self.assertFalse(
            record_package_origin(
                self.shared_state,
                PACKAGE_A,
                "junkies",
                "https://two.invalid/c",
                now=NOW + 5000,
            )
        )
        stored = self._stored(PACKAGE_A)
        self.assertEqual("filecrypt", stored["crypter"])
        self.assertEqual("one.invalid", stored["mirror"])
        self.assertEqual(NOW, stored["added_epoch"])

    def test_record_rejects_an_unusable_package_id_or_crypter(self):
        self.assertFalse(
            record_package_origin(
                self.shared_state, "", "filecrypt", "https://a.invalid/c"
            )
        )
        self.assertFalse(
            record_package_origin(
                self.shared_state, PACKAGE_A, "", "https://a.invalid/c"
            )
        )
        self.assertFalse(
            record_package_origin(
                self.shared_state, PACKAGE_A, "BAD KEY", "https://a.invalid/c"
            )
        )
        self.assertEqual({}, self.shared_state.get_db(PACKAGE_ORIGIN_TABLE).rows)

    def test_record_keeps_the_row_when_the_url_yields_no_host(self):
        self.assertTrue(
            record_package_origin(
                self.shared_state, PACKAGE_A, "direct", "not-a-url", now=NOW
            )
        )
        stored = self._stored(PACKAGE_A)
        self.assertEqual("", stored["mirror"])
        self.assertEqual("direct", stored["crypter"])

    def test_credentials_in_a_url_never_reach_the_record(self):
        record_package_origin(
            self.shared_state,
            PACKAGE_A,
            "filecrypt",
            "https://user:secret@crypter.invalid/container/1",
            now=NOW,
        )
        self.assertEqual("crypter.invalid", self._stored(PACKAGE_A)["mirror"])


class ReadOriginTests(unittest.TestCase):
    def setUp(self):
        self.shared_state = FakeSharedState()

    def test_empty_table_reads_as_empty_mapping(self):
        self.assertEqual({}, read_package_origins(self.shared_state))

    def test_rows_are_returned_keyed_by_package_id(self):
        record_package_origin(
            self.shared_state, PACKAGE_A, "filecrypt", "https://a.invalid/c", now=NOW
        )
        record_package_origin(
            self.shared_state, PACKAGE_B, "junkies", "https://b.invalid/c", now=NOW + 1
        )
        origins = read_package_origins(self.shared_state)
        self.assertEqual({PACKAGE_A, PACKAGE_B}, set(origins))
        self.assertEqual("filecrypt", origins[PACKAGE_A]["crypter"])
        self.assertEqual("b.invalid", origins[PACKAGE_B]["mirror"])
        self.assertEqual(NOW, origins[PACKAGE_A]["added_epoch"])

    def test_a_malformed_row_is_skipped_instead_of_raising(self):
        record_package_origin(
            self.shared_state, PACKAGE_A, "filecrypt", "https://a.invalid/c", now=NOW
        )
        self.shared_state.get_db(PACKAGE_ORIGIN_TABLE).rows[PACKAGE_B] = "{not json"
        self.assertEqual({PACKAGE_A}, set(read_package_origins(self.shared_state)))

    def test_a_row_smuggling_a_url_into_mirror_is_sanitized_on_read(self):
        self.shared_state.get_db(PACKAGE_ORIGIN_TABLE).rows[PACKAGE_A] = json.dumps(
            {
                "schema_version": 1,
                "crypter": "filecrypt",
                "mirror": "https://crypter.invalid/container/1",
                "added_epoch": NOW,
            }
        )
        self.assertEqual(
            "", read_package_origins(self.shared_state)[PACKAGE_A]["mirror"]
        )

    def test_a_negative_or_unparsable_epoch_reads_as_zero(self):
        self.shared_state.get_db(PACKAGE_ORIGIN_TABLE).rows[PACKAGE_A] = json.dumps(
            {"crypter": "filecrypt", "mirror": "a.invalid", "added_epoch": -5}
        )
        self.assertEqual(
            0, read_package_origins(self.shared_state)[PACKAGE_A]["added_epoch"]
        )


class ForgetOriginTests(unittest.TestCase):
    def test_forget_removes_the_row_and_tolerates_a_missing_one(self):
        shared_state = FakeSharedState()
        record_package_origin(
            shared_state, PACKAGE_A, "filecrypt", "https://a.invalid/c", now=NOW
        )
        forget_package_origin(shared_state, PACKAGE_A)
        self.assertEqual({}, shared_state.get_db(PACKAGE_ORIGIN_TABLE).rows)
        forget_package_origin(shared_state, PACKAGE_A)
        forget_package_origin(shared_state, "")


class ProcessLinksOriginTests(unittest.TestCase):
    """`process_links()` is the single funnel every package passes through, so
    it is the single place the origin is written - for direct and
    auto-decrypted packages too, not only protected ones.
    """

    def _recorded(self, links, *, protected=False, filecrypt_enabled=True):
        from quasarr.downloads import process_links

        shared_state = MagicMock()
        shared_state.values = {
            "filecrypt_enabled": filecrypt_enabled,
            "external_address": "http://quasarr.invalid:8080",
        }
        calls = []

        def fake_record(_shared_state, package_id, crypter, url, **_kwargs):
            calls.append((package_id, crypter, url))
            return True

        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "quasarr.downloads.filter_offline_links",
                    side_effect=lambda links, **_kwargs: links,
                )
            )
            stack.enter_context(
                patch("quasarr.downloads.record_package_origin", fake_record)
            )
            stack.enter_context(
                patch(
                    "quasarr.downloads.handle_direct_links",
                    return_value={"success": True},
                )
            )
            stack.enter_context(
                patch(
                    "quasarr.downloads.handle_auto_decrypt_links",
                    return_value={"success": True},
                )
            )
            stack.enter_context(
                patch(
                    "quasarr.downloads.store_protected_links",
                    return_value={"success": True},
                )
            )
            stack.enter_context(patch("quasarr.downloads.send_notification"))
            stack.enter_context(
                patch("quasarr.downloads.send_tracked_notification", return_value={})
            )
            stack.enter_context(
                patch(
                    "quasarr.downloads.normalize_download_title",
                    side_effect=lambda value: value,
                )
            )
            source_result = {"links": links}
            if protected:
                source_result["protected"] = True
            process_links(
                shared_state=shared_state,
                source_result=source_result,
                title="Synthetic.Release.2024",
                password="",
                package_id=PACKAGE_A,
                imdb_id=None,
                source_url="https://source.invalid/release",
                size_mb=100,
                label="XX",
            )
        return calls

    def test_a_protected_package_records_its_crypter_and_host(self):
        self.assertEqual(
            [(PACKAGE_A, "filecrypt", "https://filecrypt.invalid/Container/ABC123")],
            self._recorded(
                [["https://filecrypt.invalid/Container/ABC123", "filecrypt"]]
            ),
        )

    def test_a_direct_package_records_the_direct_key(self):
        calls = self._recorded(
            [["https://hoster.invalid/file/1", "rapidgator"]], protected=False
        )
        self.assertEqual(1, len(calls))
        self.assertEqual(PACKAGE_A, calls[0][0])
        self.assertEqual("direct", calls[0][1])

    def test_a_source_flagged_protected_records_the_resolved_crypter(self):
        calls = self._recorded(
            [["https://container.invalid/item", "junkies"]], protected=True
        )
        self.assertEqual(
            [(PACKAGE_A, "junkies", "https://container.invalid/item")], calls
        )

    def test_a_protected_link_of_an_unknown_crypter_still_records_a_row(self):
        calls = self._recorded(
            [["https://container.invalid/item", "somewhere"]], protected=True
        )
        self.assertEqual(1, len(calls))
        self.assertEqual("unknown", calls[0][1])

    def test_nothing_is_recorded_when_no_usable_link_survives(self):
        self.assertEqual([], self._recorded([]))


if __name__ == "__main__":
    unittest.main()
