# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

from __future__ import annotations

from bottle import HTTPError, HTTPResponse, response

from quasarr.providers.log import warn
from quasarr.providers.static_assets import carbon_assets_available
from quasarr.providers.ui_preference import get_active_ui

RenderResult = str | HTTPResponse
CSP_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; "
    "base-uri 'none'; object-src 'none'; frame-ancestors 'none'"
)


def _snapshot_response():
    return {
        "status": response.status,
        "content_type": response.content_type,
        "headerlist": list(response.headerlist),
    }


def _restore_response(snapshot):
    response.headers.clear()
    for name, value in snapshot["headerlist"]:
        response.add_header(name, value)
    response.status = snapshot["status"]
    response.content_type = snapshot["content_type"]


def _apply_csp(rendered):
    if isinstance(rendered, HTTPResponse):
        rendered.set_header("Content-Security-Policy", CSP_POLICY)
        return rendered

    response.set_header("Content-Security-Policy", CSP_POLICY)
    return rendered


def render_page(page_id, carbon_fn, classic_fn, *, shared_state=None) -> RenderResult:
    if get_active_ui(shared_state) != "carbon":
        return classic_fn()

    snapshot = _snapshot_response()

    try:
        if not carbon_assets_available():
            _restore_response(snapshot)
            warn(f"Carbon assets missing for {page_id}; serving Classic")
            return classic_fn()

        rendered = carbon_fn()
        return _apply_csp(rendered)
    except (HTTPError, HTTPResponse):
        raise
    except Exception as exc:
        _restore_response(snapshot)
        warn(f"Carbon render failed for {page_id} ({type(exc).__name__})")
        return classic_fn()
