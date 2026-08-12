# -*- coding: utf-8 -*-

import dataclasses
import json
import unittest
from pathlib import Path
from unittest import mock

from quasarr.downloads import resolve_protected_crypter_key
from quasarr.providers.crypter_candidates import (
    FilecryptCandidate,
    FilecryptInventory,
    FilecryptOccurrence,
    enumerate_filecrypt_candidates,
    link_fingerprint,
    normalize_crypter_url,
)
from quasarr.providers.crypter_sweeps import (
    MAXIMUM_COHORT_OCCURRENCES,
    MAXIMUM_COHORT_SIZE,
    helper_package_is_candidate,
)


def package_id(index, category="movies"):
    return f"Quasarr_{category}_{index:032x}"


def protected_row(package, links, **extra):
    data = {
        "title": f"Synthetic.Release.{package[-4:]}",
        "links": links,
        "password": "",
    }
    data.update(extra)
    return package, json.dumps(data)


class FingerprintContractTests(unittest.TestCase):
    def test_matches_sponsors_helper_golden_vectors(self):
        vector_path = (
            Path(__file__).with_name("data") / "filecrypt_fingerprint_vectors.json"
        )
        vectors = json.loads(vector_path.read_text(encoding="utf-8"))

        for vector in vectors:
            with self.subTest(vector=vector["name"]):
                self.assertEqual(
                    vector["normalized_url"], normalize_crypter_url(vector["url"])
                )
                self.assertEqual(
                    vector["fingerprint"],
                    link_fingerprint(vector["crypter"], vector["url"]),
                )
                self.assertRegex(vector["fingerprint"], r"^[0-9a-f]{64}$")


class FilecryptCandidateInventoryTests(unittest.TestCase):
    def test_deduplicates_unique_fingerprints_and_orders_every_occurrence(self):
        shared_first = [
            "HTTPS://alice:example@FileCrypt.Invalid:443/Container/AbC?b=2&a=1#one",
            "filecrypt",
        ]
        shared_second = [
            "https://filecrypt.invalid/Container/AbC?a=1&b=2#two",
            "other",
        ]
        shared_third = [
            "https://filecrypt.invalid/Container/AbC?b=2&a=1",
            "filecrypt",
        ]
        distinct = [
            "https://alternate.filecrypt.invalid/Container/Second",
            "other",
        ]
        package_a = package_id(1)
        package_b = package_id(2)
        rows = [
            protected_row(
                package_b,
                [
                    ["https://hoster.invalid/file", "filecrypt"],
                    shared_second,
                    shared_third,
                ],
            ),
            protected_row(
                package_a,
                [shared_first, "malformed-link", distinct],
            ),
        ]

        inventory = enumerate_filecrypt_candidates(rows)

        shared_fingerprint = link_fingerprint("filecrypt", shared_first[0])
        distinct_fingerprint = link_fingerprint("filecrypt", distinct[0])
        self.assertEqual(
            FilecryptInventory(
                candidates=(
                    FilecryptCandidate(
                        fingerprint=shared_fingerprint,
                        occurrences=(
                            FilecryptOccurrence(
                                package_id=package_a,
                                link_index=0,
                                link=shared_first,
                                fingerprint=shared_fingerprint,
                            ),
                            FilecryptOccurrence(
                                package_id=package_b,
                                link_index=1,
                                link=shared_second,
                                fingerprint=shared_fingerprint,
                            ),
                            FilecryptOccurrence(
                                package_id=package_b,
                                link_index=2,
                                link=shared_third,
                                fingerprint=shared_fingerprint,
                            ),
                        ),
                    ),
                    FilecryptCandidate(
                        fingerprint=distinct_fingerprint,
                        occurrences=(
                            FilecryptOccurrence(
                                package_id=package_a,
                                link_index=2,
                                link=distinct,
                                fingerprint=distinct_fingerprint,
                            ),
                        ),
                    ),
                ),
                oversized=False,
            ),
            inventory,
        )

    def test_excludes_ineligible_rows_packages_and_links(self):
        eligible_package = package_id(9, category="movies4k")
        eligible_link = [
            "https://alternate.filecrypt.invalid/Container/Eligible",
            "unsupported-mirror",
        ]
        rows = [
            ("malformed-row",),
            (42, json.dumps({"links": [eligible_link]})),
            (
                "Quasarr_Movies_" + "1" * 32,
                json.dumps({"links": [eligible_link]}),
            ),
            (package_id(3), "not-json"),
            (package_id(4), json.dumps(["not-a-package-object"])),
            (package_id(5), json.dumps({"links": "not-a-list"})),
            protected_row(package_id(6), [eligible_link], disabled=False),
            protected_row(
                eligible_package,
                [
                    ["https://hoster.invalid/file", "filecrypt"],
                    "malformed-link",
                    [None, "filecrypt"],
                    ["https://tolink.invalid/Container/Other", "tolink"],
                    eligible_link,
                ],
            ),
        ]

        inventory = enumerate_filecrypt_candidates(rows)

        fingerprint = link_fingerprint("filecrypt", eligible_link[0])
        self.assertEqual(
            FilecryptInventory(
                candidates=(
                    FilecryptCandidate(
                        fingerprint=fingerprint,
                        occurrences=(
                            FilecryptOccurrence(
                                package_id=eligible_package,
                                link_index=4,
                                link=eligible_link,
                                fingerprint=fingerprint,
                            ),
                        ),
                    ),
                ),
                oversized=False,
            ),
            inventory,
        )

    def test_returns_empty_oversized_sentinel_at_unique_fingerprint_101(self):
        links = [
            [f"https://filecrypt.invalid/Container/{index:03d}", "filecrypt"]
            for index in range(102)
        ]
        resolver_calls = 0

        def guarded_resolver(link):
            nonlocal resolver_calls
            resolver_calls += 1
            if resolver_calls > 101:
                self.fail("inventory inspected a link after unique fingerprint 101")
            return resolve_protected_crypter_key(link)

        with mock.patch(
            "quasarr.providers.crypter_candidates.resolve_protected_crypter_key",
            side_effect=guarded_resolver,
        ):
            inventory = enumerate_filecrypt_candidates(
                [protected_row(package_id(10), links)]
            )

        self.assertEqual(FilecryptInventory(candidates=(), oversized=True), inventory)
        self.assertEqual(101, resolver_calls)

    def test_retains_every_occurrence_up_to_the_occurrence_bound(self):
        # 1000 occurrences spread over the 100 allowed fingerprints stay conclusive.
        links = [
            [
                f"https://filecrypt.invalid/Container/{index % MAXIMUM_COHORT_SIZE:03d}",
                "filecrypt",
            ]
            for index in range(MAXIMUM_COHORT_OCCURRENCES)
        ]

        inventory = enumerate_filecrypt_candidates(
            [protected_row(package_id(11), links)]
        )

        self.assertFalse(inventory.oversized)
        self.assertEqual(MAXIMUM_COHORT_SIZE, len(inventory.candidates))
        self.assertEqual(
            MAXIMUM_COHORT_OCCURRENCES,
            sum(len(candidate.occurrences) for candidate in inventory.candidates),
        )

    def test_returns_empty_oversized_sentinel_at_occurrence_1001(self):
        # The 1001st occurrence repeats an already known fingerprint, so only the
        # occurrence bound can explain the sentinel - never the 100-unique bound.
        links = [
            [
                f"https://filecrypt.invalid/Container/{index % MAXIMUM_COHORT_SIZE:03d}",
                "filecrypt",
            ]
            for index in range(MAXIMUM_COHORT_OCCURRENCES + 3)
        ]
        resolver_calls = 0

        def guarded_resolver(link):
            nonlocal resolver_calls
            resolver_calls += 1
            if resolver_calls > MAXIMUM_COHORT_OCCURRENCES + 1:
                self.fail("inventory inspected a link after occurrence 1000")
            return resolve_protected_crypter_key(link)

        with mock.patch(
            "quasarr.providers.crypter_candidates.resolve_protected_crypter_key",
            side_effect=guarded_resolver,
        ):
            inventory = enumerate_filecrypt_candidates(
                [protected_row(package_id(12), links)]
            )

        self.assertEqual(FilecryptInventory(candidates=(), oversized=True), inventory)
        self.assertEqual(MAXIMUM_COHORT_OCCURRENCES + 1, resolver_calls)

    def test_inventory_admits_exactly_the_shared_handout_candidate_packages(self):
        link = ["https://filecrypt.invalid/Container/Shared", "filecrypt"]
        complete = {
            "title": "Synthetic.Release.Shared",
            "password": "",
            "links": [link],
        }
        rejected = {
            "missing title": {k: v for k, v in complete.items() if k != "title"},
            "missing password": {k: v for k, v in complete.items() if k != "password"},
            "empty links": dict(complete, links=[]),
            "non-list links": dict(complete, links=link[0]),
            "disabled": dict(complete, disabled=False),
        }
        rows = [
            (package_id(20 + index), json.dumps(package))
            for index, package in enumerate(rejected.values())
        ]

        self.assertEqual(
            FilecryptInventory(candidates=(), oversized=False),
            enumerate_filecrypt_candidates(rows),
        )
        for name, package in rejected.items():
            with self.subTest(package=name):
                self.assertFalse(helper_package_is_candidate(package))
        self.assertTrue(helper_package_is_candidate(complete))
        self.assertEqual(
            1,
            len(
                enumerate_filecrypt_candidates(
                    [*rows, (package_id(30), json.dumps(complete))]
                ).candidates
            ),
        )

    def test_dataclasses_are_frozen_and_not_json_serializable(self):
        link = ["https://filecrypt.invalid/Container/Frozen", "filecrypt"]
        fingerprint = link_fingerprint("filecrypt", link[0])
        occurrence = FilecryptOccurrence(package_id(1), 0, link, fingerprint)
        candidate = FilecryptCandidate(fingerprint, (occurrence,))
        inventory = FilecryptInventory((candidate,), False)

        self.assertEqual(
            ("package_id", "link_index", "link", "fingerprint"),
            tuple(field.name for field in dataclasses.fields(FilecryptOccurrence)),
        )
        self.assertEqual(
            ("fingerprint", "occurrences"),
            tuple(field.name for field in dataclasses.fields(FilecryptCandidate)),
        )
        self.assertEqual(
            ("candidates", "oversized"),
            tuple(field.name for field in dataclasses.fields(FilecryptInventory)),
        )
        for value, field_name, replacement in (
            (occurrence, "link_index", 1),
            (candidate, "fingerprint", "0" * 64),
            (inventory, "oversized", True),
        ):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(value, field_name, replacement)

        with self.assertRaises(TypeError):
            json.dumps(inventory)


if __name__ == "__main__":
    unittest.main()
