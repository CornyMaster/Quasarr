# -*- coding: utf-8 -*-

"""Contracts for the Carbon Settings view.

Pins the pure ``build_settings_model`` builder (never touching
``shared_state.get_device()``/``get_packages()``), the rendered Appearance,
JDownloader, API & Timeouts, Link Protection (B1 segmented control + sweep
source tag + "Use Docker/default value" checkbox), FlareSolverr,
Notifications, and *arr sections, the shipped external-JS merge-before-save
mechanism (behavior-equivalent to the Classic strings pinned by
``test_crypter_block_settings.py`` - Carbon gets its own contract, not
source-string coupling), and the additive JSON branch on
``save_flaresolverr_url``.
"""

import importlib
import json
import re
import unittest
from pathlib import Path
from unittest import mock

STATIC_ROOT = Path(__file__).resolve().parent.parent / "quasarr" / "static"


class _FakeProtectedDB:
    def __init__(self, titles):
        self._titles = titles

    def retrieve_all_titles(self):
        return self._titles


class _FakeSharedState:
    def __init__(self, **overrides):
        self.values = {
            "database": lambda table: _FakeProtectedDB([]),
            "timeout_slow_mode": {"search": True, "feed": False},
            "filecrypt_enabled": True,
            "crypter_block_mode": "fail",
            "crypter_cooldown_hours": 72,
            "filecrypt_sweep_window_minutes": 15,
            "filecrypt_sweep_window_override": None,
            "filecrypt_sweep_window_source": "default",
            "notification_settings": {
                "discord_webhook": "https://discord.invalid/hook",
                "telegram_bot_token": "123:abc",
                "telegram_chat_id": "999",
                "toggles": {"discord": {"captcha": False}, "telegram": {}},
                "silent": {"discord": {}, "telegram": {"solved": True}},
            },
        }
        self.values.update(overrides)

    def get_device(self):
        raise AssertionError("build_settings_model must never call get_device()")

    def get_packages(self):
        raise AssertionError("build_settings_model must never call get_packages()")


class _FakeConfigSection:
    def __init__(self, data):
        self._data = data

    def get(self, key):
        return self._data.get(key)


_CONFIG_FIXTURES = {
    "JDownloader": {
        "user": "jd@quasarr.invalid",
        "password": "hunter2",
        "device": "MyJD",
    },
    "API": {"key": "test-api-key"},
    "FlareSolverr": {"url": "http://flaresolverr.invalid:8191/v1"},
    "Radarr": {"url": "http://radarr.invalid:7878", "api_key": "radarr-key"},
    "Sonarr": {"url": "http://sonarr.invalid:8989", "api_key": "sonarr-key"},
}


def _fake_config(section):
    return _FakeConfigSection(_CONFIG_FIXTURES.get(section, {}))


class _FakeDataBase:
    def __init__(self, table):
        self.table = table

    def retrieve(self, key):
        return None


def _fake_jd_status(shared_state):
    """Mirrors get_jdownloader_status()'s connected/device_name shape
    without its internal (real) Config("JDownloader") read.
    """
    device = shared_state.values.get("device")
    connected = device is not None and device is not False
    return {
        "connected": connected,
        "device_name": _CONFIG_FIXTURES["JDownloader"].get("device", ""),
        "status_text": "connected" if connected else "disconnected",
        "status_class": "success" if connected else "error",
    }


class CarbonSettingsModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = importlib.import_module("quasarr.api.carbon")

    def _build(self, shared_state=None):
        shared_state = shared_state or _FakeSharedState()
        with (
            mock.patch.object(self.mod, "Config", side_effect=_fake_config),
            mock.patch.object(self.mod, "DataBase", _FakeDataBase),
            mock.patch.object(
                self.mod, "get_jdownloader_status", side_effect=_fake_jd_status
            ),
            mock.patch.object(self.mod, "show_logout_link", return_value=False),
        ):
            return self.mod.build_settings_model(shared_state)

    def test_model_never_touches_device_or_packages(self):
        self._build()

    def test_model_key_set(self):
        model = self._build()
        expected_keys = {
            "show_user",
            "captcha_count",
            "jdownloader",
            "api_key",
            "internal_address",
            "timeout_slow_mode",
            "crypter_block",
            "flaresolverr",
            "notifications",
            "radarr",
            "sonarr",
        }
        self.assertEqual(expected_keys, set(model.keys()))

    def test_jdownloader_fields(self):
        model = self._build()
        self.assertEqual(model["jdownloader"]["user"], "jd@quasarr.invalid")
        self.assertEqual(model["jdownloader"]["device"], "MyJD")

    def test_crypter_block_reads_cached_shared_state(self):
        model = self._build()
        crypter = model["crypter_block"]
        self.assertEqual(crypter["mode"], "fail")
        self.assertEqual(crypter["cooldown_hours"], 72)
        self.assertEqual(crypter["sweep_window_minutes"], 15)
        self.assertIsNone(crypter["sweep_window_override"])
        self.assertEqual(crypter["sweep_window_source"], "default")

    def test_notifications_read_from_cache_not_disk(self):
        model = self._build()
        settings = model["notifications"]["settings"]
        self.assertEqual(settings["discord_webhook"], "https://discord.invalid/hook")
        self.assertEqual(settings["telegram_chat_id"], "999")
        self.assertFalse(settings["toggles"]["discord"]["captcha"])
        self.assertTrue(settings["silent"]["telegram"]["solved"])


class CarbonSettingsRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = importlib.import_module("quasarr.api.carbon")
        cls.templates = importlib.import_module("quasarr.providers.carbon_templates")

    def _default_model(self, **overrides):
        model = {
            "show_user": False,
            "captcha_count": 0,
            "jdownloader": {
                "connected": True,
                "user": "jd@quasarr.invalid",
                "password": "hunter2",
                "device": "MyJD",
            },
            "api_key": "test-api-key-value",
            "internal_address": "http://quasarr.invalid:8080",
            "timeout_slow_mode": {"search": True, "feed": False},
            "crypter_block": {
                "filecrypt_enabled": True,
                "mode": "fail",
                "cooldown_hours": 72,
                "sweep_window_minutes": 15,
                "sweep_window_override": None,
                "sweep_window_source": "default",
            },
            "flaresolverr": {
                "url": "http://flaresolverr.invalid:8191/v1",
                "skipped": False,
            },
            "notifications": {
                "settings": {
                    "discord_webhook": "https://discord.invalid/configured-hook",
                    "telegram_bot_token": "123456:configured-token",
                    "telegram_chat_id": "999888777",
                    "toggles": {"discord": {}, "telegram": {}},
                    "silent": {"discord": {}, "telegram": {}},
                },
                "cases": [
                    ("captcha", "CAPTCHA Required"),
                    ("solved", "CAPTCHA Solved"),
                ],
            },
            "radarr": {"url": "http://radarr.invalid:7878", "api_key": "radarr-key"},
            "sonarr": {"url": "http://sonarr.invalid:8989", "api_key": "sonarr-key"},
        }
        model.update(overrides)
        return model

    def _render(self, **overrides):
        model = self._default_model(**overrides)
        with mock.patch.object(self.mod, "build_settings_model", return_value=model):
            html = self.mod.render_settings(object())
        return html, model

    def test_render_settings_exists(self):
        self.assertTrue(callable(self.mod.render_settings))

    def test_active_page_is_settings(self):
        html, _model = self._render()
        self.assertIn('href="/settings" aria-current="page"', html)
        self.assertIn("<title>Settings</title>", html)

    def test_section_headings_present(self):
        html, _model = self._render()
        for heading in (
            "Appearance",
            "JDownloader",
            "API &amp; timeouts",
            "Link protection",
            "FlareSolverr",
            "Notifications",
            "*arr clients",
        ):
            with self.subTest(heading=heading):
                self.assertIn(heading, html)

    def test_settings_grid_and_single_primary_per_tile(self):
        """The target design lays every section out in one auto-fit tile
        grid and allows at most one filled primary action per tile; the
        equal-weight `--secondary` fill is not part of the Settings button
        vocabulary at all (Verify credentials / Send test / Regenerate are
        `--tertiary`, destructive actions are `--danger-ghost`).
        """
        html, _model = self._render()
        self.assertIn('<div class="cds-grid--settings">', html)
        sections = re.findall(
            r'<section class="cds-tile[^"]*">.*?</section>', html, flags=re.S
        )
        self.assertGreaterEqual(len(sections), 7)
        for section in sections:
            self.assertLessEqual(section.count("cds-btn--primary"), 1, section[:120])
        self.assertNotIn("cds-btn--secondary", html)

    def test_appearance_has_theme_switcher_and_classic_link(self):
        html, _model = self._render()
        self.assertIn(
            '<fieldset class="cds-switcher" data-action="theme-switch">', html
        )
        for value in ("light", "dark", "system"):
            with self.subTest(theme=value):
                self.assertIn(f'<input type="radio" name="theme" value="{value}"', html)
        # The server cannot know the visitor's localStorage preference, so
        # it always ships "System" pre-selected and carbon.js corrects the
        # selection on DOMContentLoaded.
        self.assertIn('<input type="radio" name="theme" value="system" checked>', html)
        self.assertIn(
            'class="cds-btn cds-btn--tertiary" href="/ui/classic">Open Classic UI',
            html,
        )
        self.assertNotIn('data-action="theme-select"', html)
        self.assertNotIn('id="settings-theme"', html)

    def test_timeouts_use_standard_toggles_with_current_value(self):
        """Timeouts save on change now, so the tile has no Save button and
        each row states the timeout currently in force.
        """
        html, _model = self._render()
        self.assertNotIn("Save Timeout Settings", html)
        self.assertNotIn('data-action="timeouts-save"', html)
        self.assertIn("Current: ", html)
        timeouts = html[
            html.index("API &amp; timeouts") : html.index("Link protection")
        ]
        self.assertNotIn("cds-toggle--compact", timeouts)
        # search is the one enabled slow-mode key in the fixture model.
        self.assertIn("Current: 45 s (slow)", timeouts)
        self.assertIn("Current: 30 s (normal)", timeouts)
        # The two help strings for a row are rendered as data attributes so
        # carbon.js can swap them on change without re-deriving seconds.
        self.assertIn('data-timeout-help-normal="Current: 15 s (normal)"', timeouts)
        self.assertIn('data-timeout-help-slow="Current: 45 s (slow)"', timeouts)

    def test_api_key_uses_the_dashboard_field_row_with_reveal_and_copy(self):
        html, _model = self._render()
        self.assertIn(
            '<input class="cds-field-row__input" id="settings-api-key" '
            'type="password" value="test-api-key-value" readonly>',
            html,
        )
        # Both rows say what they are; the key row's label is a real
        # <label for>, which is also the readonly input's accessible name.
        self.assertIn('<span class="cds-field-row__label">URL</span>', html)
        self.assertIn(
            '<label class="cds-field-row__label" for="settings-api-key">'
            "API key</label>",
            html,
        )
        self.assertIn('data-reveal-target="settings-api-key"', html)
        self.assertIn('data-copy-target="settings-api-key"', html)
        self.assertIn(
            '<button class="cds-btn cds-btn--tertiary" type="button" '
            'data-action="regenerate-api-key">Regenerate API key</button>',
            html,
        )

    def test_flaresolverr_has_its_own_tile(self):
        html, _model = self._render()
        self.assertIn('<h2 class="cds-tile__heading">FlareSolverr</h2>', html)
        self.assertIn('<h2 class="cds-tile__heading">Link protection</h2>', html)

    def test_jdownloader_head_row_status_and_always_visible_instance(self):
        html, _model = self._render()
        self.assertIn(
            '<div class="cds-tile__head-row">'
            '<h2 class="cds-tile__heading">JDownloader</h2>',
            html,
        )
        self.assertIn('<span class="cds-status cds-status--success">', html)
        # The instance select is no longer hidden behind a verify step: the
        # stored device is offered as the current option straight away.
        self.assertNotIn('id="settings-jd-device-section"', html)
        self.assertIn(
            '<select class="cds-field__select" id="settings-jd-device" '
            'data-current="MyJD"><option value="MyJD" selected>MyJD</option></select>',
            html,
        )
        self.assertIn(
            '<button class="cds-btn cds-btn--primary" type="button" '
            'data-action="jd-save">Save</button>',
            html,
        )
        self.assertIn(
            '<button class="cds-btn cds-btn--tertiary" type="button" '
            'data-action="jd-verify">Verify credentials</button>',
            html,
        )

    def test_jdownloader_instance_placeholder_when_no_device_stored(self):
        html, _model = self._render(
            jdownloader={
                "connected": False,
                "user": "",
                "password": "",
                "device": "",
            }
        )
        self.assertIn(
            '<select class="cds-field__select" id="settings-jd-device" '
            'data-current=""><option value="">Verify credentials to list '
            "instances</option></select>",
            html,
        )
        self.assertIn('<span class="cds-status cds-status--error">', html)

    def test_link_protection_segmented_radio_b1(self):
        html, model = self._render()
        self.assertIn("Hold and retest", html)
        self.assertIn("Fail immediately", html)
        self.assertIn('name="settings-crypter-block-mode"', html)
        # mode="fail" -> the "fail" radio is checked, "defer" is not.
        self.assertRegex(
            html,
            r'id="settings-crypter-block-mode-fail" value="fail" checked',
        )
        self.assertNotRegex(
            html,
            r'id="settings-crypter-block-mode-defer" value="defer" checked',
        )

    def test_link_protection_block_mode_uses_the_shared_switcher(self):
        """The B1 radio group keeps its name/id/value contract but is
        styled by the same content switcher the theme row uses - there is
        no second segmented-control vocabulary left on the page.
        """
        html, _model = self._render()
        self.assertIn('<h3 class="cds-subheading">Linkcrypter access blocks</h3>', html)
        self.assertIn(
            '<fieldset class="cds-switcher"><legend class="cds-visually-hidden">'
            "When a linkcrypter blocks Quasarr</legend>",
            html,
        )
        self.assertEqual(html.count('class="cds-switcher__item"'), 5)
        self.assertNotIn("cds-segmented", html)

    def test_link_protection_has_one_save_for_both_endpoints(self):
        """Filecrypt decryption and the linkcrypter block policy live in one
        tile now, so they share one primary Save; carbon.js posts to both
        existing endpoints in sequence (see
        CarbonSettingsJsMergeBeforeSaveTests).
        """
        html, _model = self._render()
        self.assertEqual(html.count('data-action="link-protection-save"'), 1)
        self.assertNotIn('data-action="filecrypt-save"', html)
        self.assertNotIn('data-action="crypter-block-save"', html)
        self.assertIn('id="settings-link-protection-status"', html)
        self.assertNotIn('id="settings-filecrypt-status"', html)
        self.assertNotIn('id="settings-crypter-block-status"', html)

    def test_link_protection_number_fields_sit_side_by_side(self):
        html, _model = self._render()
        self.assertIn(
            '<div class="cds-grid--2"><div class="cds-field">'
            '<label class="cds-field__label" for="settings-crypter-cooldown-hours">',
            html,
        )

    def test_link_protection_sweep_source_tag(self):
        html, _model = self._render()
        self.assertIn(">Default</span>", html)

        html_env, _model = self._render(
            crypter_block={
                "filecrypt_enabled": True,
                "mode": "defer",
                "cooldown_hours": 24,
                "sweep_window_minutes": 30,
                "sweep_window_override": None,
                "sweep_window_source": "environment",
            }
        )
        self.assertIn(">Docker environment</span>", html_env)

        html_stored, _model = self._render(
            crypter_block={
                "filecrypt_enabled": True,
                "mode": "defer",
                "cooldown_hours": 24,
                "sweep_window_minutes": 45,
                "sweep_window_override": 45,
                "sweep_window_source": "stored",
            }
        )
        self.assertIn(">Web UI override</span>", html_stored)

    def test_sweep_window_default_checkbox_disables_input(self):
        html, _model = self._render()
        self.assertRegex(
            html,
            r'id="settings-filecrypt-sweep-window" type="number" '
            r'min="1" max="1440" step="1" value="15" disabled',
        )
        self.assertIn(
            'id="settings-filecrypt-sweep-window-default" class="cds-toggle__input" '
            'type="checkbox" role="switch" aria-checked="true" checked',
            html,
        )

    def test_sweep_window_enabled_when_override_set(self):
        html, _model = self._render(
            crypter_block={
                "filecrypt_enabled": True,
                "mode": "defer",
                "cooldown_hours": 24,
                "sweep_window_minutes": 45,
                "sweep_window_override": 45,
                "sweep_window_source": "stored",
            }
        )
        self.assertRegex(
            html,
            r'id="settings-filecrypt-sweep-window" type="number" '
            r'min="1" max="1440" step="1" value="45">',
        )
        self.assertIn(
            'id="settings-filecrypt-sweep-window-default" class="cds-toggle__input" '
            'type="checkbox" role="switch" aria-checked="false">',
            html,
        )

    def test_notifications_provider_cases_carried_for_js_merge(self):
        html, _model = self._render()
        self.assertIn('id="settings-notification-discord-cases"', html)
        self.assertIn(json.dumps(["captcha", "solved"]).replace('"', "&quot;"), html)

    def test_notification_credentials_render_configured_values(self):
        """Configured Discord/Telegram credentials DO render into their
        fields (unlike a since-corrected report claim) - real cached values
        flow straight into value=, matching every other Settings field.
        """
        html, model = self._render()
        settings = model["notifications"]["settings"]
        self.assertIn(
            f'id="settings-notification-discord-webhook" type="text" '
            f'value="{settings["discord_webhook"]}"',
            html,
        )
        self.assertIn(
            f'id="settings-notification-telegram-token" '
            f'type="text" value="{settings["telegram_bot_token"]}"',
            html,
        )
        self.assertIn(
            f'id="settings-notification-telegram-chat-id" '
            f'type="text" value="{settings["telegram_chat_id"]}"',
            html,
        )

    def test_notifications_has_one_unified_save_and_one_send_test(self):
        """The section has exactly one Save (Classic's single-save
        semantics restored) and, in the target design, exactly one
        tertiary Send test - carbon.js tests every provider whose
        credentials are filled in, so neither provider becomes untestable.
        """
        html, _model = self._render()
        self.assertEqual(html.count('data-action="notifications-save"'), 1)
        self.assertNotIn('data-action="notifications-save" data-provider', html)
        self.assertEqual(html.count('data-action="notifications-test"'), 1)
        self.assertNotIn('data-action="notifications-test" data-provider', html)
        self.assertIn(
            '<button class="cds-btn cds-btn--tertiary" type="button" '
            'data-action="notifications-test">Send test</button>',
            html,
        )
        self.assertEqual(html.count('id="settings-notifications-status"'), 1)
        self.assertNotIn('id="settings-notification-discord-status"', html)
        self.assertNotIn('id="settings-notification-telegram-status"', html)

    def test_notifications_telegram_is_collapsed_with_a_configured_count(self):
        html, _model = self._render()
        self.assertIn(
            '<details class="cds-details"><summary>Telegram '
            '<span class="cds-tile__count">(configured)</span></summary>',
            html,
        )
        html_empty, _model = self._render(
            notifications={
                "settings": {
                    "discord_webhook": "",
                    "telegram_bot_token": "",
                    "telegram_chat_id": "",
                    "toggles": {"discord": {}, "telegram": {}},
                    "silent": {"discord": {}, "telegram": {}},
                },
                "cases": [
                    ("captcha", "CAPTCHA Required"),
                    ("solved", "CAPTCHA Solved"),
                ],
            }
        )
        self.assertIn(
            '<details class="cds-details"><summary>Telegram '
            '<span class="cds-tile__count">(not configured)</span></summary>',
            html_empty,
        )

    def test_notifications_cases_render_as_a_compact_matrix(self):
        html, _model = self._render()
        self.assertEqual(html.count('<div class="cds-matrix__head">'), 2)
        self.assertIn(
            '<div class="cds-matrix__head"><span>Event</span>'
            "<span>Enabled</span><span>Silent</span></div>",
            html,
        )
        # Two providers x two cases.
        self.assertEqual(html.count('<div class="cds-matrix__row">'), 4)
        # Matrix switches stay compact (design 2.4); every other Settings
        # toggle is standard size.
        self.assertEqual(html.count("cds-toggle--compact"), 9)

    def test_arr_service_cards_present(self):
        html, _model = self._render()
        self.assertIn('id="settings-radarr-url"', html)
        self.assertIn('id="settings-radarr-api-key"', html)
        self.assertIn('id="settings-sonarr-url"', html)
        self.assertIn('id="settings-sonarr-api-key"', html)
        # One Save for the tile; clearing stays per service so a single
        # client can still be removed (a blank field alone cannot clear a
        # stored API key - saveArrSettings() falls back to it on purpose).
        self.assertEqual(html.count('data-action="arr-save"'), 1)
        self.assertNotIn('data-action="radarr-save"', html)
        self.assertNotIn('data-action="sonarr-save"', html)
        # Clear is destructive and wipes a configured client immediately,
        # so the button only opens a confirmation modal - it never fires
        # the clearing request straight off the click.
        self.assertIn(
            '<button class="cds-btn cds-btn--danger-ghost" type="button" '
            'data-action="radarr-clear-open">Clear</button>',
            html,
        )
        self.assertIn(
            '<button class="cds-btn cds-btn--danger-ghost" type="button" '
            'data-action="sonarr-clear-open">Clear</button>',
            html,
        )
        self.assertEqual(html.count('id="settings-arr-status"'), 1)
        self.assertNotIn('id="settings-radarr-status"', html)
        self.assertNotIn('id="settings-sonarr-status"', html)

    def test_flaresolverr_skip_warning_when_skipped(self):
        html, _model = self._render(flaresolverr={"url": "", "skipped": True})
        self.assertIn("flaresolverr-next setup was skipped", html)

    def test_no_inline_event_handlers(self):
        html, _model = self._render()
        self.assertNotRegex(html, r"\son[a-z]+\s*=")

    def test_structural_guards_pass(self):
        html, _model = self._render()
        self.templates._assert_structural_guards(html)

    def test_no_remote_resources(self):
        html, _model = self._render()
        # The only allowed absolute URL is the flaresolverr-next docs link,
        # which is a fixed, non-secret, target=_blank anchor with safe rel.
        for match in re.finditer(r'href="(https?://[^"]+)"', html):
            self.assertEqual(
                match.group(1), "https://github.com/rix1337/flaresolverr-next"
            )


class CarbonSettingsBooleanTogglesUseSwitchComponentTests(unittest.TestCase):
    """Every boolean Settings control must render through the existing
    toggle() component (role="switch" + aria-checked), not a raw
    `<input type="checkbox">`. The IDs below are
    the exact ones carbon.js reads via getElementById()/.checked - the
    contract those reads depend on is unchanged, only the markup around
    each id is.
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = importlib.import_module("quasarr.api.carbon")

    def _render(self):
        model = {
            "show_user": False,
            "captcha_count": 0,
            "jdownloader": {
                "connected": True,
                "user": "jd@quasarr.invalid",
                "password": "hunter2",
                "device": "MyJD",
            },
            "api_key": "test-api-key-value",
            "internal_address": "http://quasarr.invalid:8080",
            "timeout_slow_mode": {"search": True, "feed": False},
            "crypter_block": {
                "filecrypt_enabled": True,
                "mode": "fail",
                "cooldown_hours": 72,
                "sweep_window_minutes": 15,
                "sweep_window_override": None,
                "sweep_window_source": "default",
            },
            "flaresolverr": {
                "url": "http://flaresolverr.invalid:8191/v1",
                "skipped": False,
            },
            "notifications": {
                "settings": {
                    "discord_webhook": "https://discord.invalid/configured-hook",
                    "telegram_bot_token": "123456:configured-token",
                    "telegram_chat_id": "999888777",
                    "toggles": {"discord": {}, "telegram": {}},
                    "silent": {"discord": {}, "telegram": {}},
                },
                "cases": [
                    ("captcha", "CAPTCHA Required"),
                    ("solved", "CAPTCHA Solved"),
                ],
            },
            "radarr": {"url": "http://radarr.invalid:7878", "api_key": "radarr-key"},
            "sonarr": {"url": "http://sonarr.invalid:8989", "api_key": "sonarr-key"},
        }
        with mock.patch.object(self.mod, "build_settings_model", return_value=model):
            html = self.mod.render_settings(object())
        return html

    def test_every_boolean_setting_id_is_a_role_switch_input(self):
        html = self._render()
        boolean_setting_ids = [
            "settings-timeout-search",
            "settings-timeout-feed",
            "settings-timeout-download",
            "settings-timeout-session",
            "settings-filecrypt-enabled",
            "settings-filecrypt-sweep-window-default",
            "settings-notif-discord-captcha",
            "settings-notif-discord-captcha-silent",
            "settings-notif-discord-solved",
            "settings-notif-discord-solved-silent",
            "settings-notif-telegram-captcha",
            "settings-notif-telegram-captcha-silent",
            "settings-notif-telegram-solved",
            "settings-notif-telegram-solved-silent",
        ]
        for element_id in boolean_setting_ids:
            with self.subTest(element_id=element_id):
                self.assertRegex(
                    html,
                    rf'<input id="{re.escape(element_id)}" class="cds-toggle__input" '
                    r'type="checkbox" role="switch" aria-checked="(true|false)"',
                )

    def test_no_raw_checkbox_remains_on_settings_page(self):
        # A raw boolean checkbox would be <input type="checkbox" ...> with
        # no accompanying role="switch" on the same tag. The segmented
        # Link Protection mode control is type="radio", not "checkbox", so
        # it is unaffected by this guard.
        html = self._render()
        for match in re.finditer(r"<input[^>]*>", html):
            tag_html = match.group(0)
            if 'type="checkbox"' in tag_html:
                self.assertIn(
                    'role="switch"',
                    tag_html,
                    f"raw checkbox without role=switch found: {tag_html}",
                )

    def test_setting_checkbox_ids_unchanged_for_js_reads(self):
        # carbon.js's readCheckboxValue()/byId() reads depend only on these
        # ids existing on a real <input type="checkbox">, never on the
        # surrounding markup shape - pin that the id set itself survived
        # the toggle() conversion unchanged.
        html = self._render()
        self.assertIn('id="settings-filecrypt-enabled"', html)
        self.assertIn('id="settings-filecrypt-sweep-window-default"', html)
        for timeout_key in ("search", "feed", "download", "session"):
            self.assertIn(f'id="settings-timeout-{timeout_key}"', html)


class CarbonSettingsJsMergeBeforeSaveTests(unittest.TestCase):
    """Behavior-equivalent coverage of the shipped external-JS save flow:
    every save fetches the authenticated settings first. Notifications has
    one unified save that reads every rendered field for both providers
    unconditionally (restoring Classic's single-save semantics - a typed
    edit anywhere in the section is never silently discarded), while the
    pre-save fetch still protects a notification case this page's own
    rendered case list does not know about.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = (STATIC_ROOT / "carbon.js").read_text(encoding="utf-8")

    def _function_body(self, name):
        for prefix in ("async function ", "function "):
            marker = f"{prefix}{name}("
            if marker in self.js:
                start = self.js.index(marker)
                break
        else:
            raise AssertionError(f"No function named {name} found in carbon.js")
        depth = 0
        i = self.js.index("{", start)
        body_start = i
        for index in range(i, len(self.js)):
            char = self.js[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return self.js[body_start : index + 1]
        raise AssertionError(f"Unbalanced braces in {name}")

    def test_notifications_fetches_before_building_payload(self):
        body = self._function_body("buildNotificationsPayload")
        fetch_index = body.index("fetchJsonSettings('/api/notifications/settings')")
        return_index = body.index("return {")
        self.assertLess(fetch_index, return_index)

    def test_unified_save_reads_both_provider_credentials_unconditionally(self):
        """Regression coverage: a prior per-provider save read
        only the DOM field of the provider being saved (gated by an `if
        (provider === ...)` branch), so saving Discord silently discarded
        an edit typed into the Telegram field - the field kept displaying
        the typed value while the server never received it. The unified
        builder must read every rendered credential field unconditionally;
        no provider-branching may remain to gate which field reaches the
        payload.
        """
        body = self._function_body("buildNotificationsPayload")
        self.assertNotIn("provider ===", body)
        self.assertIn("readFieldValue('settings-notification-discord-webhook')", body)
        self.assertIn("readFieldValue('settings-notification-telegram-token')", body)
        self.assertIn("readFieldValue('settings-notification-telegram-chat-id')", body)

    def test_unified_save_merges_both_providers_preserving_unknown_case_keys(self):
        """Combined with test_toggle_merge_preserves_unknown_case_keys
        (which proves mergedCaseMap clones the fetched base before
        overlaying only the case keys this page renders), this proves that
        guarantee is exercised for *both* providers unconditionally in the
        unified payload: a notification case key outside this page's
        rendered set - e.g. one only a newer server knows about - survives
        the save for Discord and Telegram alike, not just for whichever
        provider used to be the "active" one under the old per-provider
        save.
        """
        body = self._function_body("buildNotificationsPayload")
        self.assertEqual(body.count("mergedCaseMap("), 4)
        self.assertIn(
            "mergedCaseMap((base.toggles || {}).discord, 'discord', '')", body
        )
        self.assertIn(
            "mergedCaseMap((base.toggles || {}).telegram, 'telegram', '')", body
        )
        self.assertIn(
            "mergedCaseMap((base.silent || {}).discord, 'discord', '-silent')", body
        )
        self.assertIn(
            "mergedCaseMap((base.silent || {}).telegram, 'telegram', '-silent')",
            body,
        )

    def test_test_button_saves_via_unified_save(self):
        """Send test still saves-then-tests through the one unified save
        action instead of a removed per-provider save. With a single
        button, every provider whose credentials are filled in is tested,
        so dropping the second button removes no reachable behavior.
        """
        body = self._function_body("testConfiguredNotificationProviders")
        self.assertIn("saveNotifications()", body)
        self.assertNotIn("saveNotificationProvider(", body)
        self.assertIn("configuredNotificationProviders()", body)
        self.assertIn("settings-notifications-status", body)
        # saveNotifications() writes the real reason into the same status
        # line; overwriting it with a generic sentence would throw away the
        # only clue the user has.
        self.assertNotIn("Save failed. Fix settings and retry.", self.js)
        self.assertIn(
            "'/api/notifications/test'",
            self._function_body("testNotificationProvider"),
        )

    def test_configured_providers_are_derived_from_the_rendered_fields(self):
        body = self._function_body("configuredNotificationProviders")
        self.assertIn("readFieldValue('settings-notification-discord-webhook')", body)
        self.assertIn("readFieldValue('settings-notification-telegram-token')", body)
        self.assertIn("readFieldValue('settings-notification-telegram-chat-id')", body)

    def test_send_test_reports_every_provider_outcome_separately(self):
        """A failure of the first provider must neither hide itself nor
        stop the second provider from being tested: every outcome is
        collected into one list and rendered as one combined status line.
        """
        body = self._function_body("testConfiguredNotificationProviders")
        self.assertIn("var results = [];", body)
        self.assertIn("for (var index = 0; index < providers.length; index += 1)", body)
        self.assertIn("results.push({", body)
        self.assertIn("await testNotificationProvider(providers[index].id)", body)
        self.assertIn("combineResults(results, 'Test message sent', 'sent')", body)
        # Per-provider failures resolve instead of throwing, so one bad
        # provider can never abort the loop before the other is tested.
        provider_body = self._function_body("testNotificationProvider")
        self.assertIn("return { ok: true", provider_body)
        self.assertIn("return { ok: false", provider_body)

    def test_toggle_merge_preserves_unknown_case_keys(self):
        body = self._function_body("mergedCaseMap")
        # Base object is cloned first (Object.assign), then only case keys
        # this page knows about are overwritten - any key present in the
        # fetched base but absent from the page's case list survives.
        assign_index = body.index("Object.assign({}, baseMap || {})")
        overlay_index = body.index("notificationCaseKeys(provider).forEach")
        self.assertLess(assign_index, overlay_index)

    def test_arr_save_fetches_before_merging_api_key(self):
        body = self._function_body("saveArrSettings")
        fetch_index = body.index("fetchJsonSettings('/api/' + service + '/settings')")
        merge_index = body.index("typedApiKey || base.api_key")
        self.assertLess(fetch_index, merge_index)

    def test_arr_clear_sends_explicit_empty_pair_without_merge(self):
        body = self._function_body("clearArrSettings")
        self.assertNotIn("fetchJsonSettings", body)
        self.assertIn("url: '',", body)
        self.assertIn("api_key: ''", body)

    def test_arr_clear_button_opens_confirmation_modal_before_clearing(self):
        """Clear wipes a configured client immediately once confirmed, so
        the button must never reach clearArrSettings() directly from a
        click - matching the confirm-then-act anatomy "Restart Quasarr"
        and "Delete package" already use elsewhere in this UI.
        """
        dispatch_body = self._function_body("onSettingsDashboardClick")
        self.assertIn("case 'radarr-clear-open':", dispatch_body)
        self.assertIn("case 'sonarr-clear-open':", dispatch_body)
        open_index = dispatch_body.index("case 'radarr-clear-open':")
        self.assertLess(
            open_index,
            dispatch_body.index("openArrClearModal('radarr')", open_index),
        )

        confirm_index = dispatch_body.index("case 'radarr-clear-confirm':")
        close_index = dispatch_body.index("window.closeModal();", confirm_index)
        clear_index = dispatch_body.index("clearArrSettings('radarr')", confirm_index)
        self.assertLess(confirm_index, close_index)
        self.assertLess(close_index, clear_index)

    def test_arr_clear_modal_matches_confirm_anatomy(self):
        """Eyebrow present, secondary Cancel on the left, danger Clear on
        the right - the same shape as openRestartModal()'s Cancel/Restart
        footer.
        """
        body = self._function_body("openArrClearModal")
        self.assertIn("eyebrow: '*arr clients'", body)
        cancel_index = body.index(
            '<button class="cds-btn cds-btn--secondary" type="button" data-action="modal-close">Cancel</button>'
        )
        danger_index = body.index('cds-btn cds-btn--danger"', cancel_index)
        self.assertLess(cancel_index, danger_index)
        self.assertIn('-clear-confirm">Clear</button>', body)
        self.assertIn("This cannot be undone.", body)

    def test_flaresolverr_save_has_no_merge_blank_is_intentional_clear(self):
        body = self._function_body("saveFlareSolverrSettings")
        self.assertNotIn("fetchJsonSettings", body)
        self.assertIn("readFieldValue('settings-flaresolverr-url')", body)

    def test_timeouts_merge_preserves_unknown_keys(self):
        body = self._function_body("saveTimeoutSettings")
        assign_index = body.index("Object.assign({}, base)")
        overlay_index = body.index("timeoutSlowModeKeys().forEach")
        self.assertLess(assign_index, overlay_index)

    def test_timeouts_save_on_change_and_have_no_save_action_left(self):
        """The Save button is gone, so the change handler is the only thing
        that can still persist a timeout toggle - it must call the very
        same saveTimeoutSettings() the button used to call, and refresh the
        row's "Current: n s" helper text from the server-rendered strings.
        """
        body = self._function_body("onSettingsDashboardChange")
        self.assertIn("settings-timeout-", body)
        self.assertIn("saveTimeoutSettings(previousSettings);", body)
        self.assertIn("updateTimeoutHelpText(", body)
        self.assertNotIn("case 'timeouts-save':", self.js)

        help_body = self._function_body("updateTimeoutHelpText")
        self.assertIn("data-timeout-help-slow", help_body)
        self.assertIn("data-timeout-help-normal", help_body)

    def test_failed_timeout_save_restores_the_stored_value_in_the_ui(self):
        """Autosave has no Save button, so the switch itself is the state
        indicator: after a failed POST it must not keep showing a value the
        server never stored (which would silently revert on the next page
        load). saveTimeoutSettings() re-syncs the controls from an
        authoritative settings object on BOTH paths, exactly the way its
        two siblings re-sync theirs from the response.
        """
        body = self._function_body("saveTimeoutSettings")
        success_index = body.index("applyTimeoutSettings(result.data.settings")
        catch_index = body.index("catch (error)")
        self.assertLess(success_index, catch_index)

        failure_branch = body[catch_index:]
        # The freshly fetched stored settings when the GET got through,
        # otherwise the pre-flip state the change handler captured - never
        # a bare status message with the lying switch left alone.
        self.assertIn(
            "applyTimeoutSettings(stored || previousSettings || {});", failure_branch
        )
        self.assertLess(
            failure_branch.index("applyTimeoutSettings("),
            failure_branch.index("setFieldStatus("),
        )

        apply_body = self._function_body("applyTimeoutSettings")
        self.assertIn("input.checked = !!settings[key];", apply_body)
        self.assertIn(
            "input.setAttribute('aria-checked', String(input.checked));", apply_body
        )
        self.assertIn("updateTimeoutHelpText(input);", apply_body)

    def test_change_handler_captures_the_pre_flip_state_for_the_revert(self):
        """The browser flips the checkbox before the change event fires, so
        the only place that still knows the previous state is the handler.
        Without this capture a total outage (GET and POST both failing)
        would leave the switch stuck on the value the user clicked.
        """
        body = self._function_body("onSettingsDashboardChange")
        self.assertIn("var previousSettings = {};", body)
        self.assertIn(
            "previousSettings[target.id.replace('settings-timeout-', '')] = "
            "!target.checked;",
            body,
        )

    def test_sequential_saves_report_each_outcome_without_swallowing_one(self):
        """Two tiles now save through two existing endpoints behind one
        button. Neither orchestrator may stop at the first failure, and the
        combined status line must name what failed AND what still saved -
        a bare "Save failed" would hide a half-applied change.
        """
        combine = self._function_body("combineResults")
        self.assertIn("return successMessage;", combine)
        self.assertIn("part.label", combine)
        self.assertIn("part.result.message", combine)
        self.assertIn("saved.join(", combine)
        self.assertIn("doneLabel", combine)

        for name, first, second in (
            (
                "saveLinkProtectionSettings",
                "saveFilecryptSetting()",
                "saveCrypterBlockSettings()",
            ),
            (
                "saveAllArrSettings",
                "saveArrSettings('radarr')",
                "saveArrSettings('sonarr')",
            ),
        ):
            with self.subTest(orchestrator=name):
                body = self._function_body(name)
                self.assertLess(body.index(first), body.index(second))
                # Both calls are awaited unconditionally, before any status
                # is written - no early return between them.
                between = body[body.index(first) : body.index(second)]
                self.assertNotIn("return", between)
                self.assertIn("combineResults(", body)

    def test_partial_save_helpers_return_a_result_instead_of_throwing(self):
        """The three functions the two orchestrators drive must resolve to
        {ok, message} for both outcomes; if one still threw, the second
        save would never run.
        """
        for name in (
            "saveFilecryptSetting",
            "saveCrypterBlockSettings",
            "saveArrSettings",
        ):
            with self.subTest(function=name):
                body = self._function_body(name)
                self.assertIn("return { ok: true", body)
                self.assertIn("return { ok: false", body)
                self.assertIn("catch (error)", body)

    def test_arr_clear_and_link_protection_write_to_the_shared_status_line(self):
        self.assertIn("'settings-arr-status'", self._function_body("clearArrSettings"))
        self.assertIn(
            "'settings-link-protection-status'",
            self._function_body("saveLinkProtectionSettings"),
        )
        self.assertNotIn("settings-radarr-status", self.js)
        self.assertNotIn("settings-crypter-block-status", self.js)
        self.assertNotIn("settings-filecrypt-status", self.js)

    def test_timeout_slow_mode_keys_selector_is_scoped_to_inputs_only(self):
        """A bare `[id^="settings-timeout-"]` attribute selector would also
        match toggle()'s own help-text `<p
        id="settings-timeout-<key>-help">` (unused today, but a real,
        supported toggle() option) and emit a phantom "<key>-help" entry
        into the POST /api/timeouts/settings payload. Scoped to
        `input[id^=...]` so only the toggle's own checkbox can ever match.
        """
        body = self._function_body("timeoutSlowModeKeys")
        self.assertIn('input[id^="settings-timeout-"]', body)
        self.assertNotIn("querySelectorAll('[id^=\"settings-timeout-\"]')", body)

    def test_dashboard_queue_loader_has_isolated_error_state(self):
        js = self.js
        self.assertIn("function loadDashboardQueue()", js)
        self.assertIn("dashboard-queue-content", js)
        self.assertIn("Queue is unavailable right now.", js)
        self.assertIn("JDownloader is not connected.", js)

    def test_carbon_js_still_has_no_remote_urls(self):
        self.assertNotIn("http://", self.js)
        self.assertNotIn("https://", self.js)


class CarbonSettingsCssContractTests(unittest.TestCase):
    def test_switcher_matrix_and_details_styles_present(self):
        css = (STATIC_ROOT / "carbon.css").read_text(encoding="utf-8")
        self.assertIn(".cds-switcher", css)
        self.assertIn(".cds-switcher__item input:checked + span", css)
        self.assertIn(".cds-visually-hidden", css)
        self.assertIn(".cds-matrix__head", css)
        self.assertIn(".cds-matrix__row", css)
        self.assertIn(".cds-details", css)
        self.assertIn(".cds-field-row__label", css)
        self.assertNotIn("http://", css)
        self.assertNotIn("https://", css)

    def test_rules_left_with_their_last_consumer(self):
        # Two blocks lost their last renderer in the same change and are
        # therefore dead weight rather than merely unused: `.cds-segmented`
        # (Link Protection's B1 radio group, which now renders through the
        # shared `.cds-switcher`) and `.cds-toggle-row` (the notification
        # case rows, which now render as `.cds-matrix__row`). Both go, on
        # the same rule as the earlier `.cds-checkbox-field` removal.
        css = (STATIC_ROOT / "carbon.css").read_text(encoding="utf-8")
        self.assertNotIn(".cds-segmented", css)
        self.assertNotIn(".cds-toggle-row", css)

    def test_raw_checkbox_field_style_was_removed_with_the_last_consumer(self):
        # Every boolean Settings control moved to the accessible toggle()
        # component; .cds-checkbox-field had no other
        # consumer anywhere in the codebase, so it is dead weight if left
        # behind - confirm it is actually gone rather than merely unused.
        css = (STATIC_ROOT / "carbon.css").read_text(encoding="utf-8")
        self.assertNotIn(".cds-checkbox-field", css)


class FlareSolverrJsonBranchTests(unittest.TestCase):
    """The additive JSON response branch on save_flaresolverr_url: the exact
    existing form-encoded branch (Classic UI, first-run setup) is untouched.
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = importlib.import_module("quasarr.storage.setup.flaresolverr")

    class _FakeConfig:
        def __init__(self):
            self.saved = {}

        def get(self, key):
            return self.saved.get(key)

        def save(self, key, value):
            self.saved[key] = value

    class _FakeSkipDb:
        def __init__(self):
            self.store = {}

        def update_store(self, key, value):
            self.store[key] = value

        def delete(self, key):
            self.store.pop(key, None)

        def retrieve(self, key):
            return self.store.get(key)

    def _fake_shared_state(self):
        state = mock.Mock()
        state.values = {"user_agent": "UA"}
        return state

    def _request_json(self, payload, content_type="application/json"):
        request_mock = mock.Mock()
        request_mock.content_type = content_type
        request_mock.json = payload
        return request_mock

    def _request_form(self, url):
        request_mock = mock.Mock()
        request_mock.content_type = "application/x-www-form-urlencoded"
        request_mock.forms = {"url": url}
        return request_mock

    def test_json_branch_returns_json_on_success(self):
        fake_config = self._FakeConfig()
        request_mock = self._request_json({"url": "http://flaresolverr.invalid/v1"})
        with (
            mock.patch.object(self.mod, "request", request_mock),
            mock.patch.object(self.mod, "response") as response_mock,
            mock.patch.object(self.mod, "Config", return_value=fake_config),
            mock.patch.object(self.mod, "DataBase", return_value=self._FakeSkipDb()),
            mock.patch.object(self.mod, "check_flaresolverr", return_value=True),
        ):
            result = self.mod.save_flaresolverr_url(self._fake_shared_state())

        self.assertEqual(response_mock.content_type, "application/json")
        self.assertEqual(
            result,
            {"success": True, "message": "flaresolverr-next URL saved successfully!"},
        )
        self.assertEqual(fake_config.saved["url"], "http://flaresolverr.invalid/v1")

    def test_json_branch_returns_json_on_failure(self):
        fake_config = self._FakeConfig()
        request_mock = self._request_json({"url": "http://flaresolverr.invalid/v1"})
        with (
            mock.patch.object(self.mod, "request", request_mock),
            mock.patch.object(self.mod, "response") as response_mock,
            mock.patch.object(self.mod, "Config", return_value=fake_config),
            mock.patch.object(self.mod, "DataBase", return_value=self._FakeSkipDb()),
            mock.patch.object(self.mod, "check_flaresolverr", return_value=False),
        ):
            result = self.mod.save_flaresolverr_url(self._fake_shared_state())

        self.assertEqual(response_mock.content_type, "application/json")
        self.assertEqual(result["success"], False)
        self.assertIn("Could not reach", result["message"])

    def test_json_branch_invalid_payload_returns_400_shape(self):
        request_mock = self._request_json(None)
        with (
            mock.patch.object(self.mod, "request", request_mock),
            mock.patch.object(self.mod, "response") as response_mock,
        ):
            result = self.mod.save_flaresolverr_url(self._fake_shared_state())

        self.assertEqual(response_mock.content_type, "application/json")
        self.assertEqual(result, {"success": False, "message": "Invalid JSON payload"})

    def test_json_branch_clear_returns_json(self):
        fake_config = self._FakeConfig()
        fake_skip_db = self._FakeSkipDb()
        request_mock = self._request_json({"url": ""})
        shared_state = self._fake_shared_state()
        with (
            mock.patch.object(self.mod, "request", request_mock),
            mock.patch.object(self.mod, "response") as response_mock,
            mock.patch.object(self.mod, "Config", return_value=fake_config),
            mock.patch.object(self.mod, "DataBase", return_value=fake_skip_db),
        ):
            result = self.mod.save_flaresolverr_url(shared_state)

        self.assertEqual(response_mock.content_type, "application/json")
        self.assertEqual(
            result, {"success": True, "message": "flaresolverr-next URL cleared."}
        )
        self.assertEqual(fake_skip_db.store.get("skipped"), "true")
        self.assertEqual(shared_state.update.call_args[0], ("user_agent", mock.ANY))

    def test_form_branch_unchanged_returns_html(self):
        """The exact existing form-encoded path: HTML reconnect/fail
        response, untouched by the additive JSON branch."""
        fake_config = self._FakeConfig()
        request_mock = self._request_form("http://flaresolverr.invalid/v1")
        with (
            mock.patch.object(self.mod, "request", request_mock),
            mock.patch.object(self.mod, "Config", return_value=fake_config),
            mock.patch.object(self.mod, "DataBase", return_value=self._FakeSkipDb()),
            mock.patch.object(self.mod, "check_flaresolverr", return_value=True),
            mock.patch.object(
                self.mod,
                "render_reconnect_success",
                side_effect=lambda message: f"<html>{message}</html>",
            ) as render_mock,
        ):
            result = self.mod.save_flaresolverr_url(self._fake_shared_state())

        render_mock.assert_called_once_with("flaresolverr-next URL saved successfully!")
        self.assertEqual(
            result, "<html>flaresolverr-next URL saved successfully!</html>"
        )

    def test_form_branch_failure_still_uses_render_fail(self):
        fake_config = self._FakeConfig()
        request_mock = self._request_form("http://flaresolverr.invalid/v1")
        with (
            mock.patch.object(self.mod, "request", request_mock),
            mock.patch.object(self.mod, "Config", return_value=fake_config),
            mock.patch.object(self.mod, "DataBase", return_value=self._FakeSkipDb()),
            mock.patch.object(self.mod, "check_flaresolverr", return_value=False),
            mock.patch.object(
                self.mod,
                "render_fail",
                side_effect=lambda message: f"<html>{message}</html>",
            ) as render_mock,
        ):
            result = self.mod.save_flaresolverr_url(self._fake_shared_state())

        render_mock.assert_called_once_with("Could not reach flaresolverr-next!")
        self.assertEqual(result, "<html>Could not reach flaresolverr-next!</html>")


if __name__ == "__main__":
    unittest.main()
