# -*- coding: utf-8 -*-

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import MagicMock, patch

from quasarr.providers.cloudflare import (
    LazyFlareSolverrSession,
    _clear_cloudflare_gate_cache,
)
from quasarr.search.sources.helpers.budget import (
    SearchBudgetExhausted,
    use_search_budget,
)


class FakeResponse:
    def __init__(self, url, text="", status_code=200):
        self.url = url
        self.text = text
        self.status_code = status_code
        self.headers = {}


def _build_shared_state():
    shared_state = MagicMock()
    shared_state.values = {"user_agent": "UnitTestAgent/1.0"}
    return shared_state


class ManualClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class CloudflareGateCacheTests(unittest.TestCase):
    def setUp(self):
        _clear_cloudflare_gate_cache()

    def tearDown(self):
        _clear_cloudflare_gate_cache()

    def test_cached_host_skips_plain_until_ttl_expires(self):
        url = "https://cache-source.invalid/page"
        challenged = FakeResponse(
            url, text="<title>Just a moment...</title>", status_code=403
        )
        plain_after_expiry = FakeResponse(url, text="<html>plain again</html>")
        solved = FakeResponse(url, text="<html>solved</html>")
        plain_get = MagicMock(side_effect=[challenged, plain_after_expiry])
        now = [100.0]
        shared_state = _build_shared_state()

        with (
            patch(
                "quasarr.providers.cloudflare.time.monotonic",
                side_effect=lambda: now[0],
            ),
            patch(
                "quasarr.providers.cloudflare.is_flaresolverr_available",
                return_value=True,
            ),
            patch(
                "quasarr.providers.cloudflare.flaresolverr_create_session",
                return_value="cache-session",
            ) as create_session,
            patch(
                "quasarr.providers.cloudflare.flaresolverr_get",
                return_value=solved,
            ) as flaresolverr_get,
            patch(
                "quasarr.providers.cloudflare.flaresolverr_destroy_session"
            ) as destroy_session,
            patch("quasarr.providers.cloudflare.debug") as debug_log,
        ):
            first = LazyFlareSolverrSession(shared_state)
            try:
                first.get(url, {"User-Agent": "UnitTestAgent/1.0"}, 10, plain_get)
            finally:
                first.close()

            now[0] = 200.0
            second = LazyFlareSolverrSession(shared_state)
            try:
                second.get(url, {"User-Agent": "UnitTestAgent/1.0"}, 10, plain_get)
            finally:
                second.close()

            now[0] = 100.0 + (24 * 60 * 60)
            third = LazyFlareSolverrSession(shared_state)
            try:
                response = third.get(
                    url, {"User-Agent": "UnitTestAgent/1.0"}, 10, plain_get
                )
            finally:
                third.close()

        self.assertIs(plain_after_expiry, response)
        self.assertEqual(2, plain_get.call_count)
        self.assertEqual(2, create_session.call_count)
        self.assertEqual(2, flaresolverr_get.call_count)
        self.assertEqual(2, destroy_session.call_count)
        debug_log.assert_called_once()

    def test_cache_is_isolated_by_netloc(self):
        gated_url = "https://gated-source.invalid/page"
        other_url = "https://plain-source.invalid/page"
        gated_api_url = "https://gated-source.invalid/api"
        challenged = FakeResponse(gated_url, status_code=403)
        plain = FakeResponse(other_url, text="<html>plain</html>")
        requested_urls = []

        def plain_get(url, headers=None, timeout=None):
            requested_urls.append(url)
            if url == gated_url:
                return challenged
            if url == other_url:
                return plain
            raise AssertionError(f"Cached host unexpectedly used plain HTTP: {url}")

        with (
            patch(
                "quasarr.providers.cloudflare.is_flaresolverr_available",
                return_value=True,
            ),
            patch(
                "quasarr.providers.cloudflare.flaresolverr_create_session",
                return_value="isolated-session",
            ),
            patch(
                "quasarr.providers.cloudflare.flaresolverr_get",
                side_effect=lambda shared_state, url, timeout, session_id: FakeResponse(
                    url, text="<html>solved</html>"
                ),
            ) as flaresolverr_get,
            patch("quasarr.providers.cloudflare.flaresolverr_destroy_session"),
        ):
            session = LazyFlareSolverrSession(_build_shared_state())
            try:
                session.get(
                    gated_url, {"User-Agent": "UnitTestAgent/1.0"}, 10, plain_get
                )
                other_response = session.get(
                    other_url, {"User-Agent": "UnitTestAgent/1.0"}, 10, plain_get
                )
                session.get(
                    gated_api_url,
                    {"User-Agent": "UnitTestAgent/1.0"},
                    10,
                    plain_get,
                )
            finally:
                session.close()

        self.assertIs(plain, other_response)
        self.assertEqual([gated_url, other_url], requested_urls)
        self.assertEqual(
            [gated_url, gated_api_url],
            [call.args[1] for call in flaresolverr_get.call_args_list],
        )

    def test_same_operation_skips_plain_api_and_external_requests(self):
        base_url = "https://operation-source.invalid"
        urls = [f"{base_url}/page", f"{base_url}/api", f"{base_url}/external/link"]
        plain_get = MagicMock(return_value=FakeResponse(urls[0], status_code=403))

        with (
            patch(
                "quasarr.providers.cloudflare.is_flaresolverr_available",
                return_value=True,
            ),
            patch(
                "quasarr.providers.cloudflare.flaresolverr_create_session",
                return_value="operation-session",
            ) as create_session,
            patch(
                "quasarr.providers.cloudflare.flaresolverr_get",
                side_effect=lambda shared_state, url, timeout, session_id: FakeResponse(
                    url, text="<html>solved</html>"
                ),
            ) as flaresolverr_get,
            patch(
                "quasarr.providers.cloudflare.flaresolverr_destroy_session"
            ) as destroy_session,
            patch("quasarr.providers.cloudflare.debug") as debug_log,
        ):
            session = LazyFlareSolverrSession(_build_shared_state())
            try:
                for url in urls:
                    session.get(url, {"User-Agent": "UnitTestAgent/1.0"}, 10, plain_get)
            finally:
                session.close()

        plain_get.assert_called_once()
        create_session.assert_called_once()
        self.assertEqual(
            urls, [call.args[1] for call in flaresolverr_get.call_args_list]
        )
        self.assertEqual(
            ["operation-session"] * 3,
            [call.kwargs["session_id"] for call in flaresolverr_get.call_args_list],
        )
        destroy_session.assert_called_once()
        debug_log.assert_called_once()

    def test_solver_uses_at_least_the_session_timeout_budget(self):
        url = "https://timeout-source.invalid/page"
        plain_get = MagicMock(return_value=FakeResponse(url, status_code=403))

        with (
            patch(
                "quasarr.providers.cloudflare.is_flaresolverr_available",
                return_value=True,
            ),
            patch(
                "quasarr.providers.cloudflare.flaresolverr_create_session",
                return_value="timeout-session",
            ),
            patch(
                "quasarr.providers.cloudflare.flaresolverr_get",
                return_value=FakeResponse(url, text="<html>solved</html>"),
            ) as flaresolverr_get,
            patch("quasarr.providers.cloudflare.flaresolverr_destroy_session"),
        ):
            session = LazyFlareSolverrSession(_build_shared_state())
            try:
                session.get(
                    url,
                    {"User-Agent": "UnitTestAgent/1.0"},
                    10,
                    plain_get,
                )
            finally:
                session.close()

        self.assertEqual(30, flaresolverr_get.call_args.kwargs["timeout"])

    def test_solver_timeout_is_clamped_to_the_worker_budget(self):
        url = "https://worker-budget-source.invalid/page"
        plain_get = MagicMock(return_value=FakeResponse(url, status_code=403))

        with (
            patch(
                "quasarr.providers.cloudflare.is_flaresolverr_available",
                return_value=True,
            ),
            patch(
                "quasarr.providers.cloudflare.flaresolverr_create_session",
                return_value="worker-budget-session",
            ),
            patch(
                "quasarr.providers.cloudflare.flaresolverr_get",
                return_value=FakeResponse(url, text="<html>solved</html>"),
            ) as flaresolverr_get,
            patch("quasarr.providers.cloudflare.flaresolverr_destroy_session"),
            use_search_budget(5.0, clock=ManualClock()),
        ):
            session = LazyFlareSolverrSession(_build_shared_state())
            try:
                session.get(
                    url,
                    {"User-Agent": "UnitTestAgent/1.0"},
                    10,
                    plain_get,
                )
            finally:
                session.close()

        self.assertEqual(5.0, flaresolverr_get.call_args.kwargs["timeout"])

    def test_browser_is_not_created_after_plain_request_spends_the_budget(self):
        url = "https://late-browser-source.invalid/page"
        clock = ManualClock()

        def spend_budget(_url, headers=None, timeout=None):
            clock.now = 10.0
            return FakeResponse(url, status_code=403)

        with (
            patch(
                "quasarr.providers.cloudflare.is_flaresolverr_available",
                return_value=True,
            ),
            patch(
                "quasarr.providers.cloudflare.flaresolverr_create_session",
                return_value="too-late-session",
            ) as create_session,
            patch(
                "quasarr.providers.cloudflare.flaresolverr_get",
                return_value=FakeResponse(url, text="<html>solved</html>"),
            ),
            patch(
                "quasarr.providers.cloudflare.flaresolverr_destroy_session"
            ) as destroy_session,
            use_search_budget(10.0, clock=clock),
        ):
            session = LazyFlareSolverrSession(_build_shared_state())
            try:
                with self.assertRaises(SearchBudgetExhausted):
                    session.get(
                        url,
                        {"User-Agent": "UnitTestAgent/1.0"},
                        10,
                        spend_budget,
                    )
            finally:
                session.close()

        create_session.assert_not_called()
        destroy_session.assert_not_called()

    def test_an_existing_browser_is_closed_after_the_budget_is_spent(self):
        session = LazyFlareSolverrSession(_build_shared_state())
        session.session_id = "existing-session"

        with (
            patch(
                "quasarr.providers.cloudflare.flaresolverr_destroy_session"
            ) as destroy_session,
            use_search_budget(1.0, clock=ManualClock(1.0)),
        ):
            session.close()

        destroy_session.assert_called_once_with(
            session.shared_state, "existing-session"
        )
        self.assertIsNone(session.session_id)

    def test_concurrent_detection_logs_once_and_leaves_host_gated(self):
        url = "https://threaded-source.invalid/page"
        barrier = Barrier(2)
        plain_get = MagicMock()

        def blocked_get(url, headers=None, timeout=None):
            barrier.wait()
            return FakeResponse(url, status_code=403)

        plain_get.side_effect = blocked_get
        shared_state = _build_shared_state()

        def run_operation():
            session = LazyFlareSolverrSession(shared_state)
            try:
                return session.get(
                    url, {"User-Agent": "UnitTestAgent/1.0"}, 10, plain_get
                )
            finally:
                session.close()

        with (
            patch(
                "quasarr.providers.cloudflare.is_flaresolverr_available",
                return_value=True,
            ),
            patch(
                "quasarr.providers.cloudflare.flaresolverr_create_session",
                side_effect=lambda shared_state, requested_id: requested_id,
            ) as create_session,
            patch(
                "quasarr.providers.cloudflare.flaresolverr_get",
                side_effect=lambda shared_state, url, timeout, session_id: FakeResponse(
                    url, text="<html>solved</html>"
                ),
            ) as flaresolverr_get,
            patch("quasarr.providers.cloudflare.flaresolverr_destroy_session"),
            patch("quasarr.providers.cloudflare.debug") as debug_log,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                responses = list(executor.map(lambda _: run_operation(), range(2)))

            cached_plain_get = MagicMock(
                side_effect=AssertionError("Gated host retried plain HTTP")
            )
            cached = LazyFlareSolverrSession(shared_state)
            try:
                cached_response = cached.get(
                    url,
                    {"User-Agent": "UnitTestAgent/1.0"},
                    10,
                    cached_plain_get,
                )
            finally:
                cached.close()

        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(200, cached_response.status_code)
        self.assertEqual(2, plain_get.call_count)
        cached_plain_get.assert_not_called()
        self.assertEqual(3, create_session.call_count)
        self.assertEqual(3, flaresolverr_get.call_count)
        debug_log.assert_called_once()


if __name__ == "__main__":
    unittest.main()
