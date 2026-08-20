# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

from __future__ import annotations

import os

from bottle import request

from quasarr.providers.log import warn

VALID_UI_MODES = frozenset({"carbon", "classic"})
DEFAULT_UI = "carbon"
UI_PREFERENCE_TABLE = "ui_preference"
UI_PREFERENCE_KEY = "mode"
UI_COOKIE_NAME = "quasarr_ui"


def _is_valid_ui_mode(mode: object) -> bool:
    return isinstance(mode, str) and mode in VALID_UI_MODES


def load_ui_preference(shared_state) -> str:
    mode = DEFAULT_UI
    try:
        stored_mode = shared_state.values["database"](UI_PREFERENCE_TABLE).retrieve(
            UI_PREFERENCE_KEY
        )
    except Exception as exc:
        warn(f"UI preference load failed ({type(exc).__name__})")
    else:
        if stored_mode is None:
            pass  # No row yet (fresh install) - silently keep the default.
        elif _is_valid_ui_mode(stored_mode):
            mode = stored_mode
        else:
            warn("UI preference load failed (invalid stored value)")

    shared_state.update("ui_preference", mode)
    return mode


def get_active_ui(shared_state=None) -> str:
    env_mode = os.environ.get("QUASARR_UI")
    if _is_valid_ui_mode(env_mode):
        return env_mode

    try:
        query_mode = request.query.get("ui")
    except Exception:
        query_mode = None
    if _is_valid_ui_mode(query_mode):
        return query_mode

    try:
        cookie_mode = request.get_cookie(UI_COOKIE_NAME)
    except Exception:
        cookie_mode = None
    if _is_valid_ui_mode(cookie_mode):
        return cookie_mode

    if shared_state is not None:
        cached_mode = getattr(shared_state, "values", {}).get("ui_preference")
        if _is_valid_ui_mode(cached_mode):
            return cached_mode

    return DEFAULT_UI


def persist_ui_preference(shared_state, mode: str) -> str:
    if not _is_valid_ui_mode(mode):
        raise ValueError("Invalid UI mode")

    shared_state.values["database"](UI_PREFERENCE_TABLE).update_store(
        UI_PREFERENCE_KEY,
        mode,
    )
    shared_state.update("ui_preference", mode)
    return mode
