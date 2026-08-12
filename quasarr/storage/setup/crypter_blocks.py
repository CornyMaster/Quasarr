# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

from bottle import request, response

from quasarr.constants import CRYPTER_BLOCK_SETTINGS_TABLE
from quasarr.providers.crypter_cooldowns import MINIMUM_COOLDOWN_HOURS
from quasarr.storage.sqlite_database import DataBase

DEFAULT_CRYPTER_BLOCK_MODE = "defer"
CRYPTER_BLOCK_MODES = frozenset({"defer", "fail"})


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


def _read_crypter_block_settings():
    settings_db = DataBase(CRYPTER_BLOCK_SETTINGS_TABLE)
    return {
        "mode": _coerce_mode(
            settings_db.retrieve("mode"),
            DEFAULT_CRYPTER_BLOCK_MODE,
        ),
        "cooldown_hours": _coerce_stored_cooldown_hours(
            settings_db.retrieve("cooldown_hours")
        ),
    }


def refresh_crypter_block_settings(shared_state):
    settings = _read_crypter_block_settings()
    shared_state.update("crypter_block_mode", settings["mode"])
    shared_state.update("crypter_cooldown_hours", settings["cooldown_hours"])
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

    settings = refresh_crypter_block_settings(shared_state)
    return {
        "success": True,
        "message": "Linkcrypter block settings saved successfully",
        "settings": settings,
    }
