# -*- coding: utf-8 -*-

"""A hide.cx container that no longer exists must fail fast, not queue forever.

`hide.cx` answers a dead foreign container with HTTP 404 and
`{"error": "container not found or invalid"}`. Quasarr used to discard that
answer at DEBUG level and demote the links into the protected bucket, where
the package waited for a manual CAPTCHA that could never succeed - a human
cannot solve a container the crypter itself cannot find. These pin the
distinction between "gone" (terminal) and "failed" (a human may still win).
"""

import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from quasarr.downloads.linkcrypters.hide import decrypt_links_if_hide, unhide_links

PACKAGE_ID = "Quasarr_movies_" + "a" * 32
FOREIGN_URL = "https://hide.invalid/fc/Container/ABC123.html"
PLAIN_URL = "https://hide.invalid/container/ABC123"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, url=None, raises=False):
        self.status_code = status_code
        self._payload = payload
        self.url = url or FOREIGN_URL
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Answers the two hide.cx API calls `unhide_links()` makes."""

    def __init__(self, resolve=None, container=None):
        self.max_redirects = 5
        self._resolve = resolve
        self._container = container
        self.requested = []

    def head(self, url, **_kwargs):
        # No redirect: the resolved URL is the one that went in.
        return FakeResponse(url=url)

    def get(self, url, **_kwargs):
        self.requested.append(url)
        if "/fc/Container/" in url:
            return self._resolve
        return self._container


def _shared_state():
    shared_state = MagicMock()
    shared_state.values = {"user_agent": "Mozilla/5.0"}
    return shared_state


class UnhideLinksOutcomeTests(unittest.TestCase):
    def _run(self, session):
        outcome = {}
        with patch("quasarr.downloads.linkcrypters.hide.StatsHelper"):
            links = unhide_links(_shared_state(), FOREIGN_URL, session, outcome=outcome)
        return links, outcome

    def test_a_404_on_the_foreign_resolve_reports_the_container_as_gone(self):
        session = FakeSession(
            resolve=FakeResponse(404, {"error": "container not found or invalid"})
        )

        links, outcome = self._run(session)

        self.assertEqual([], links)
        self.assertTrue(outcome.get("gone"))

    def test_a_404_on_the_container_fetch_reports_the_container_as_gone(self):
        session = FakeSession(
            resolve=FakeResponse(200, {"id": "CANON1"}),
            container=FakeResponse(404, {"error": "container not found or invalid"}),
        )

        links, outcome = self._run(session)

        self.assertEqual([], links)
        self.assertTrue(outcome.get("gone"))

    def test_a_server_error_is_not_reported_as_gone(self):
        # A 5xx is the crypter having a bad day, not proof the container is
        # missing - demoting to a manual CAPTCHA stays the right answer.
        session = FakeSession(resolve=FakeResponse(503, {"error": "unavailable"}))

        links, outcome = self._run(session)

        self.assertEqual([], links)
        self.assertFalse(outcome.get("gone"))

    def test_an_unparsable_body_is_not_reported_as_gone(self):
        session = FakeSession(resolve=FakeResponse(200, None, raises=True))

        links, outcome = self._run(session)

        self.assertEqual([], links)
        self.assertFalse(outcome.get("gone"))

    def test_a_resolve_without_a_canonical_id_is_not_reported_as_gone(self):
        session = FakeSession(resolve=FakeResponse(200, {"unexpected": "shape"}))

        links, outcome = self._run(session)

        self.assertEqual([], links)
        self.assertFalse(outcome.get("gone"))


class DecryptStatusTests(unittest.TestCase):
    # decrypt_links_if_hide() recognises its own containers by the literal
    # host, which the shipped module hard-codes throughout (api.hide.cx), so
    # a test of THAT recognition cannot use a synthetic host. The crypter is
    # not one of the sources the repository rule protects.
    LIVE_HOST_URL = "https://hide.cx/fc/Container/ABC123.html"

    def _decrypt(self, session):
        with (
            patch(
                "quasarr.downloads.linkcrypters.hide.requests.Session", lambda: session
            ),
            patch("quasarr.downloads.linkcrypters.hide.StatsHelper"),
        ):
            return decrypt_links_if_hide(
                _shared_state(), [[self.LIVE_HOST_URL, "hoster"]]
            )

    def test_a_dead_container_yields_status_gone(self):
        session = FakeSession(
            resolve=FakeResponse(404, {"error": "container not found or invalid"})
        )

        self.assertEqual(
            {"status": "gone", "results": []},
            self._decrypt(session),
        )

    def test_a_transient_failure_still_yields_status_error(self):
        session = FakeSession(resolve=FakeResponse(503, {"error": "unavailable"}))

        self.assertEqual(
            {"status": "error", "results": []},
            self._decrypt(session),
        )


class ProcessLinksTerminalTests(unittest.TestCase):
    """The orchestrator must not park a package nobody can ever solve."""

    def _run(self, decrypt_status):
        from quasarr import downloads

        shared_state = MagicMock()
        shared_state.values = {
            "filecrypt_enabled": True,
            "external_address": "http://quasarr.invalid:8080",
        }
        calls = {}

        def fake_fail(title, package_id, state, reason="Unknown error"):
            calls["fail_reason"] = reason
            return {"success": False, "title": title, "failed": True}

        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "quasarr.downloads.filter_offline_links",
                    side_effect=lambda links, **_kwargs: links,
                )
            )
            stack.enter_context(patch("quasarr.downloads.record_package_origin"))
            stack.enter_context(
                patch(
                    "quasarr.downloads.decrypt_links_if_hide",
                    return_value={"status": decrypt_status, "results": []},
                )
            )
            store = stack.enter_context(
                patch(
                    "quasarr.downloads.store_protected_links",
                    return_value={"success": True},
                )
            )
            stack.enter_context(patch("quasarr.downloads.send_notification"))
            stack.enter_context(
                patch("quasarr.downloads.send_tracked_notification", return_value={})
            )
            stack.enter_context(
                patch(
                    "quasarr.downloads.normalize_download_title",
                    side_effect=lambda value: value,
                )
            )
            stack.enter_context(patch("quasarr.downloads.fail", side_effect=fake_fail))
            downloads.process_links(
                shared_state=shared_state,
                source_result={"links": [[PLAIN_URL, "hoster"]]},
                title="Synthetic.Release.2024",
                password="",
                package_id=PACKAGE_ID,
                imdb_id=None,
                source_url="https://source.invalid/release",
                size_mb=100,
                label="XX",
            )
        return calls, store

    def test_a_gone_container_fails_the_package_instead_of_queueing_it(self):
        calls, store = self._run("gone")

        store.assert_not_called()
        self.assertIn("container", calls.get("fail_reason", "").lower())

    def test_a_transient_failure_still_falls_back_to_a_manual_captcha(self):
        calls, store = self._run("error")

        store.assert_called_once()
        self.assertNotIn("fail_reason", calls)


if __name__ == "__main__":
    unittest.main()
