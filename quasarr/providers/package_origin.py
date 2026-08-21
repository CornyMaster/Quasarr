# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

"""Durable per-package origin: which linkcrypter a package came from, on
which of that crypter's hosts, and when Quasarr first accepted it.

Only the CAPTCHA flow ever knew this. The protected blob holds the crypter
links and is deleted the moment the CAPTCHA is solved, so queue and history
rows had no way to name their own origin - and nothing recorded when a package
was accepted, which is why the Downloads list had no meaningful order to sort
by. One small row per package, written once, closes both gaps.

Deliberately free of any `quasarr.downloads` import: that package imports
providers, so the crypter key is resolved by the caller (which already holds
`resolve_protected_crypter_key`) and handed in as a plain string.

`safe_mirror()` is the single gate for the one field that could carry more
than a bare host. It is applied on write AND again on read, so a row written
by an older build, edited by hand, or corrupted in storage can never put a
protected URL into the Downloads response.
"""

import json
import re
import time

from quasarr.providers.log import debug

PACKAGE_ORIGIN_TABLE = "package_origin"
ORIGIN_SCHEMA_VERSION = 1

# Crypter keys the writer accepts. A shape rule rather than an allowlist
# import, so this module stays independent of quasarr.downloads; the caller
# passes a key it already resolved through the real allowlist.
_CRYPTER_KEY_PATTERN = re.compile(r"^[a-z0-9_]{1,32}$")

# The longest a hostname may be, port excluded.
_MAXIMUM_HOST_LENGTH = 253

# A bare host: dot-separated labels plus an optional numeric port. No scheme,
# no credentials, no path, no query. Anything richer is not a host and is
# dropped whole rather than trimmed, so a malformed value can never be
# salvaged into something that merely looks safe.
_HOST_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
    r"(?::\d{1,5})?$"
)

_CRYPTER_LABELS = {
    "filecrypt": "FileCrypt",
    "keeplinks": "KeepLinks",
    "tolink": "ToLink",
    "junkies": "Junkies",
    "hide": "Hide",
    "direct": "Direct",
}


def crypter_label(key):
    """Display name for a crypter key; an unknown key stays readable."""
    if not isinstance(key, str) or not key:
        return ""
    return _CRYPTER_LABELS.get(key, key.replace("_", " ").strip().capitalize())


def safe_mirror(value):
    """The crypter host, or "" for anything that is not exactly a host."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip().lower()
    if not candidate or len(candidate) > _MAXIMUM_HOST_LENGTH:
        return ""
    if not _HOST_PATTERN.match(candidate):
        return ""
    return candidate


def mirror_from_url(url):
    """The host of a link, without scheme, credentials, path, or query.

    Parsed by hand rather than through urlsplit() so the authority is cut at
    the first delimiter whichever one appears - urlsplit() keeps credentials
    in netloc, and this must never hand them on.
    """
    if not isinstance(url, str) or "://" not in url:
        return ""
    remainder = url.split("://", 1)[1]
    for separator in ("/", "?", "#"):
        remainder = remainder.split(separator, 1)[0]
    if "@" in remainder:
        remainder = remainder.rsplit("@", 1)[1]
    return safe_mirror(remainder)


def record_package_origin(shared_state, package_id, crypter, url, *, now=None):
    """Store the origin of one package, once. Returns whether a row was written.

    Never overwrites. A re-grab of the same release must not reset
    `added_epoch`, mirroring `store_protected_links()`'s create-or-merge rule,
    and a package that falls back from auto-decrypt to the protected branch
    must keep the crypter of the branch that first accepted it.
    """
    if not isinstance(package_id, str) or not package_id:
        return False
    if not isinstance(crypter, str) or not _CRYPTER_KEY_PATTERN.match(crypter):
        return False

    payload = json.dumps(
        {
            "schema_version": ORIGIN_SCHEMA_VERSION,
            "crypter": crypter,
            "mirror": mirror_from_url(url),
            "added_epoch": int(now if now is not None else time.time()),
        }
    )
    written = False

    def create_only(current_value):
        nonlocal written
        if current_value is not None:
            return current_value
        written = True
        return payload

    try:
        shared_state.get_db(PACKAGE_ORIGIN_TABLE).mutate_value(package_id, create_only)
    except Exception as error:
        debug(f'Storing the origin of package "{package_id}" failed: {error}')
        return False
    return written


def read_package_origins(shared_state):
    """Every stored origin as {package_id: {crypter, mirror, added_epoch}}.

    One read per request. A malformed row is skipped rather than raising, so a
    single corrupt value can never take the Downloads page down.
    """
    try:
        rows = shared_state.get_db(PACKAGE_ORIGIN_TABLE).retrieve_all_titles()
    except Exception as error:
        debug(f"Reading package origins failed: {error}")
        return {}

    origins = {}
    for row in rows or []:
        try:
            package_id, raw = row[0], row[1]
            data = json.loads(raw)
            if not isinstance(data, dict):
                continue
            crypter = data.get("crypter")
            origins[str(package_id)] = {
                "crypter": crypter if isinstance(crypter, str) else "",
                "mirror": safe_mirror(data.get("mirror")),
                "added_epoch": max(0, int(data.get("added_epoch") or 0)),
            }
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    return origins


def forget_package_origin(shared_state, package_id):
    """Drop one package's origin. A missing row is not an error."""
    if not isinstance(package_id, str) or not package_id:
        return
    try:
        shared_state.get_db(PACKAGE_ORIGIN_TABLE).mutate_value(
            package_id, lambda _current_value: None
        )
    except Exception as error:
        debug(f'Removing the origin of package "{package_id}" failed: {error}')


__all__ = [
    "ORIGIN_SCHEMA_VERSION",
    "PACKAGE_ORIGIN_TABLE",
    "crypter_label",
    "forget_package_origin",
    "mirror_from_url",
    "read_package_origins",
    "record_package_origin",
    "safe_mirror",
]
