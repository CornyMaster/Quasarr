# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import logging
import os

from bottle import request, response

from quasarr.constants import CRYPTER_BLOCK_SETTINGS_TABLE
from quasarr.providers.crypter_cooldowns import (
    DEFAULT_CRYPTER_BLOCK_MODE,
    LEGACY_CRYPTER_BLOCK_MODE,
    MINIMUM_COOLDOWN_HOURS,
)
from quasarr.storage.sqlite_database import DataBase

CRYPTER_BLOCK_MODES = frozenset({DEFAULT_CRYPTER_BLOCK_MODE, LEGACY_CRYPTER_BLOCK_MODE})

FILECRYPT_SWEEP_WINDOW_ENV = "FILECRYPT_SWEEP_WINDOW_MINUTES"
DEFAULT_FILECRYPT_SWEEP_WINDOW_MINUTES = 15
MINIMUM_FILECRYPT_SWEEP_WINDOW_MINUTES = 1
MAXIMUM_FILECRYPT_SWEEP_WINDOW_MINUTES = 1440
FILECRYPT_SWEEP_WINDOW_KEY = "filecrypt_sweep_window_minutes"

_UNSET = object()
_log = logging.getLogger(__name__)


def _coerce_mode(value, default):
    if isinstance(value, str) and value in CRYPTER_BLOCK_MODES:
        return value
    return default


def _coerce_stored_cooldown_hours(value):
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return MINIMUM_COOLDOWN_HOURS
    return max(MINIMUM_COOLDOWN_HOURS, hours)


def _coerce_requested_cooldown_hours(value, default):
    if type(value) is int and value >= MINIMUM_COOLDOWN_HOURS:
        return value
    return default


def _resolve_sweep_window(stored_raw):
    """Return (effective_minutes, override_or_None, source_str) using precedence: stored > ENV > default."""
    override = None
    if stored_raw is not None:
        try:
            v = int(stored_raw)
            if (
                MINIMUM_FILECRYPT_SWEEP_WINDOW_MINUTES
                <= v
                <= MAXIMUM_FILECRYPT_SWEEP_WINDOW_MINUTES
            ):
                override = v
        except (TypeError, ValueError):
            pass
    if override is not None:
        return override, override, "stored"
    env_raw = os.environ.get(FILECRYPT_SWEEP_WINDOW_ENV, "")
    if env_raw:
        try:
            env_v = int(env_raw)
        except (TypeError, ValueError):
            _log.warning("FILECRYPT_SWEEP_WINDOW_MINUTES is invalid; using default")
            return DEFAULT_FILECRYPT_SWEEP_WINDOW_MINUTES, None, "default"
        if (
            MINIMUM_FILECRYPT_SWEEP_WINDOW_MINUTES
            <= env_v
            <= MAXIMUM_FILECRYPT_SWEEP_WINDOW_MINUTES
        ):
            return env_v, None, "environment"
        _log.warning("FILECRYPT_SWEEP_WINDOW_MINUTES is invalid; using default")
        return DEFAULT_FILECRYPT_SWEEP_WINDOW_MINUTES, None, "default"
    return DEFAULT_FILECRYPT_SWEEP_WINDOW_MINUTES, None, "default"


def _read_crypter_block_settings():
    settings_db = DataBase(CRYPTER_BLOCK_SETTINGS_TABLE)
    mode = _coerce_mode(settings_db.retrieve("mode"), DEFAULT_CRYPTER_BLOCK_MODE)
    cooldown_hours = _coerce_stored_cooldown_hours(
        settings_db.retrieve("cooldown_hours")
    )
    effective, override, source = _resolve_sweep_window(
        settings_db.retrieve(FILECRYPT_SWEEP_WINDOW_KEY)
    )
    return {
        "mode": mode,
        "cooldown_hours": cooldown_hours,
        "filecrypt_sweep_window_minutes": effective,
        "filecrypt_sweep_window_override": override,
        "filecrypt_sweep_window_source": source,
    }


def refresh_crypter_block_settings(shared_state):
    settings = _read_crypter_block_settings()
    shared_state.update("crypter_block_mode", settings["mode"])
    shared_state.update("crypter_cooldown_hours", settings["cooldown_hours"])
    shared_state.update(
        "filecrypt_sweep_window_minutes", settings["filecrypt_sweep_window_minutes"]
    )
    shared_state.update(
        "filecrypt_sweep_window_override", settings["filecrypt_sweep_window_override"]
    )
    shared_state.update(
        "filecrypt_sweep_window_source", settings["filecrypt_sweep_window_source"]
    )
    return settings


def initialize_crypter_block_settings(shared_state):
    return refresh_crypter_block_settings(shared_state)


def get_crypter_block_settings_data(shared_state):
    response.content_type = "application/json"
    return {
        "success": True,
        "settings": refresh_crypter_block_settings(shared_state),
    }


def save_crypter_block_settings(shared_state):
    response.content_type = "application/json"

    data = request.json
    if not isinstance(data, dict):
        return {"success": False, "message": "Invalid JSON payload"}

    current_settings = _read_crypter_block_settings()
    mode = _coerce_mode(data.get("mode"), current_settings["mode"])
    cooldown_hours = _coerce_requested_cooldown_hours(
        data.get("cooldown_hours"),
        current_settings["cooldown_hours"],
    )

    settings_db = DataBase(CRYPTER_BLOCK_SETTINGS_TABLE)
    settings_db.update_store("mode", mode)
    settings_db.update_store("cooldown_hours", str(cooldown_hours))

    sweep_payload = data.get(FILECRYPT_SWEEP_WINDOW_KEY, _UNSET)
    if sweep_payload is None:
        settings_db.delete(FILECRYPT_SWEEP_WINDOW_KEY)
    elif sweep_payload is not _UNSET:
        if (
            type(sweep_payload) is int
            and MINIMUM_FILECRYPT_SWEEP_WINDOW_MINUTES
            <= sweep_payload
            <= MAXIMUM_FILECRYPT_SWEEP_WINDOW_MINUTES
        ):
            settings_db.update_store(FILECRYPT_SWEEP_WINDOW_KEY, str(sweep_payload))

    settings = refresh_crypter_block_settings(shared_state)
    return {
        "success": True,
        "message": "Linkcrypter block settings saved successfully",
        "settings": settings,
    }
