# -*- coding: utf-8 -*-
"""Provider-owned static asset helpers for Carbon UI.

This module implements safe URL helpers, static-asset availability checks,
and a single idempotent Bottle route to serve package-local static files.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Dict

from bottle import abort, static_file

from quasarr.providers.auth import public_endpoint
from quasarr.providers.version import get_version

STATIC_MIME_TYPES: Dict[str, str] = {
    ".css": "text/css",
    ".js": "text/javascript",
    ".woff2": "font/woff2",
    ".svg": "image/svg+xml",
}


# Absolute package-local static root (package root / static)
# production root must be the package parent (quasarr/static)
_STATIC_ROOT = Path(__file__).resolve().parent.parent.joinpath("static").resolve()


def _safe_rel_path(rel: str) -> bool:
    """Return True for a safe POSIX-like relative path.

    Rejects empty, absolute, dot/dot-dot segments, backslashes, drive letters,
    and percent-encoded traversal sequences.
    """
    if not rel:
        return False

    # disallow obvious absolute paths
    if rel.startswith("/") or rel.startswith("\\"):
        return False

    if "\\" in rel:
        return False

    # decode percent encodings and validate the decoded shape
    decoded = urllib.parse.unquote(rel)
    if decoded.startswith("/") or "\\" in decoded:
        return False

    parts = decoded.split("/")
    # disallow empty segments, '.' or '..'
    for seg in parts:
        if seg in ("", ".", ".."):
            return False
        # disallow drive-letter like C: or other absolute hints
        if ":" in seg:
            return False

    return True


def asset_url(relative_path: str) -> str:
    """Return a safe same-origin URL for an asset and append cache-buster.

    Raises ValueError for unsafe or unsupported paths.
    """
    if not _safe_rel_path(relative_path):
        raise ValueError("unsafe relative_path")
    suffix = Path(relative_path).suffix
    if suffix not in STATIC_MIME_TYPES:
        raise ValueError("unsupported suffix")
    v = get_version()
    return f"/static/{relative_path}?v={v}"


def carbon_assets_available() -> bool:
    """Return True when all required Carbon UI files are present.

    Checks for CSS, JS, five fonts, IBM license, Carbon license, and attribution.
    """
    required = [
        "carbon.css",
        "carbon.js",
        "fonts/IBMPlexSans-Regular-Latin.woff2",
        "fonts/IBMPlexSans-Medium-Latin.woff2",
        "fonts/IBMPlexSans-SemiBold-Latin.woff2",
        "fonts/IBMPlexMono-Regular-Latin.woff2",
        "fonts/IBMPlexMono-Medium-Latin.woff2",
        "fonts/LICENSE-IBM-PLEX.txt",
        "icons/LICENSE-APACHE-2.0.txt",
        "icons/ATTRIBUTION.txt",
    ]
    for p in required:
        if not _STATIC_ROOT.joinpath(p).is_file():
            return False
    return True


def setup_static_routes(app, *, immutable: bool = True) -> None:
    """Idempotently register a single `/static/<filename:path>` route.

    The callback is decorated with `@public_endpoint` so it remains accessible
    when auth is enabled. `immutable` toggles long-term immutable caching
    (one year) or `no-store` for setup flows.
    """
    # Avoid double-registration by checking existing rules
    for route in app.routes:
        if getattr(route, "rule", "") == "/static/<filename:path>":
            return

    @app.get("/static/<filename:path>")
    @public_endpoint
    def _handler(filename):
        if not _safe_rel_path(filename):
            abort(404, "Not found")
        suffix = Path(filename).suffix
        if suffix not in STATIC_MIME_TYPES:
            abort(404, "Not found")

        root = str(_STATIC_ROOT)

        headers = {"X-Content-Type-Options": "nosniff"}
        if immutable:
            headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            headers["Cache-Control"] = "no-store"

        # Bottle handles HEAD via GET; keep one route registration only.
        return static_file(
            filename,
            root=root,
            mimetype=STATIC_MIME_TYPES[suffix],
            charset=None,
            headers=headers,
        )


__all__ = [
    "STATIC_MIME_TYPES",
    "asset_url",
    "carbon_assets_available",
    "setup_static_routes",
]
