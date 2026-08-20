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
            "API &amp; Timeouts",
            "Link Protection",
            "FlareSolverr",
            "Notifications",
            "*arr",
        ):
            with self.subTest(heading=heading):
                self.assertIn(heading, html)

    def test_appearance_theme_select_present(self):
        html, _model = self._render()
        self.assertIn('id="settings-theme"', html)
        self.assertIn('data-action="theme-select"', html)

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

    def test_notifications_has_one_unified_save_and_per_provider_test(self):
        """The section has exactly one Save (Classic's single-save
        semantics restored) plus one Send Test per provider."""
        html, _model = self._render()
        self.assertEqual(html.count('data-action="notifications-save"'), 1)
        self.assertNotIn('data-action="notifications-save" data-provider', html)
        self.assertEqual(html.count('data-action="notifications-test"'), 2)
        self.assertIn('data-action="notifications-test" data-provider="discord"', html)
        self.assertIn('data-action="notifications-test" data-provider="telegram"', html)
        self.assertIn('id="settings-notifications-status"', html)

    def test_arr_service_cards_present(self):
        html, _model = self._render()
        self.assertIn('id="settings-radarr-url"', html)
        self.assertIn('id="settings-radarr-api-key"', html)
        self.assertIn('data-action="radarr-save"', html)
        self.assertIn('data-action="radarr-clear"', html)
        self.assertIn('id="settings-sonarr-url"', html)
        self.assertIn('data-action="sonarr-save"', html)
        self.assertIn('data-action="sonarr-clear"', html)

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
        """Per-provider Send Test still saves-then-tests, now through the
        one unified save action instead of a removed per-provider save."""
        body = self._function_body("testNotificationProvider")
        self.assertIn("saveNotifications()", body)
        self.assertNotIn("saveNotificationProvider(", body)

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

    def test_flaresolverr_save_has_no_merge_blank_is_intentional_clear(self):
        body = self._function_body("saveFlareSolverrSettings")
        self.assertNotIn("fetchJsonSettings", body)
        self.assertIn("readFieldValue('settings-flaresolverr-url')", body)

    def test_timeouts_merge_preserves_unknown_keys(self):
        body = self._function_body("saveTimeoutSettings")
        assign_index = body.index("Object.assign({}, base)")
        overlay_index = body.index("timeoutSlowModeKeys().forEach")
        self.assertLess(assign_index, overlay_index)

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
    def test_segmented_control_and_toggle_row_styles_present(self):
        css = (STATIC_ROOT / "carbon.css").read_text(encoding="utf-8")
        self.assertIn(".cds-segmented", css)
        self.assertIn(".cds-segmented__input:checked + .cds-segmented__label", css)
        self.assertIn(".cds-toggle-row", css)
        self.assertNotIn("http://", css)
        self.assertNotIn("https://", css)

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
