# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import dataclasses
import hashlib
import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from quasarr.constants import PACKAGE_ID_PATTERN
from quasarr.downloads import resolve_protected_crypter_key
from quasarr.providers.crypter_sweeps import (
    MAXIMUM_COHORT_OCCURRENCES,
    MAXIMUM_COHORT_SIZE,
    helper_package_is_candidate,
)

_FILECRYPT_CRYPTER = "filecrypt"
_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclasses.dataclass(frozen=True)
class FilecryptOccurrence:
    package_id: str
    link_index: int
    link: object
    fingerprint: str


@dataclasses.dataclass(frozen=True)
class FilecryptCandidate:
    fingerprint: str
    occurrences: tuple[FilecryptOccurrence, ...]


@dataclasses.dataclass(frozen=True)
class FilecryptInventory:
    candidates: tuple[FilecryptCandidate, ...]
    oversized: bool


def normalize_crypter_url(url: str) -> str:
    """Return a deterministic, credential-free, fragment-free URL."""
    parts = urlsplit((url or "").strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{port}"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((scheme, host, parts.path, query, ""))


def link_fingerprint(crypter: str, url: str) -> str:
    """Return the full lowercase SHA-256 digest for one crypter URL."""
    material = f"{(crypter or '').strip().lower()}\n{normalize_crypter_url(url)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _canonical_rows(protected_rows):
    rows = []
    for row in protected_rows or ():
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        package_id = row[0]
        if not isinstance(package_id, str) or not PACKAGE_ID_PATTERN.fullmatch(
            package_id
        ):
            continue
        rows.append((package_id, row[1]))
    return sorted(rows, key=lambda row: row[0])


def enumerate_filecrypt_candidates(protected_rows) -> FilecryptInventory:
    """Build the bounded unique Filecrypt inventory from protected DB rows.

    Both capacity limits fail closed. Truncating occurrences would leave a
    denominator that looks complete, so the whole inventory becomes inconclusive
    instead - unique fingerprint 101 and occurrence 1001 each return the same
    empty sentinel without inspecting any later link.
    """
    occurrences_by_fingerprint = {}
    occurrence_count = 0

    for package_id, raw_package in _canonical_rows(protected_rows):
        try:
            package = json.loads(raw_package)
        except (TypeError, ValueError, RecursionError):
            continue
        if not helper_package_is_candidate(package):
            continue

        for link_index, link in enumerate(package["links"]):
            if resolve_protected_crypter_key(link) != _FILECRYPT_CRYPTER:
                continue
            if occurrence_count == MAXIMUM_COHORT_OCCURRENCES:
                return FilecryptInventory(candidates=(), oversized=True)
            fingerprint = link_fingerprint(_FILECRYPT_CRYPTER, link[0])
            occurrences = occurrences_by_fingerprint.get(fingerprint)
            if occurrences is None:
                if len(occurrences_by_fingerprint) == MAXIMUM_COHORT_SIZE:
                    return FilecryptInventory(candidates=(), oversized=True)
                occurrences = []
                occurrences_by_fingerprint[fingerprint] = occurrences
            occurrence_count += 1
            occurrences.append(
                FilecryptOccurrence(
                    package_id=package_id,
                    link_index=link_index,
                    link=link,
                    fingerprint=fingerprint,
                )
            )

    return FilecryptInventory(
        candidates=tuple(
            FilecryptCandidate(
                fingerprint=fingerprint,
                occurrences=tuple(occurrences),
            )
            for fingerprint, occurrences in occurrences_by_fingerprint.items()
        ),
        oversized=False,
    )
