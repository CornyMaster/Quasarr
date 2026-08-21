import ast
import pathlib
import unittest
from threading import get_ident
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from quasarr.constants import SEARCH_CAT_MOVIES
from quasarr.search.sources.helpers.budget import (
    SearchBudgetExhausted,
    use_search_budget,
)

SOURCES_DIR = (
    pathlib.Path(__file__).resolve().parents[1] / "quasarr" / "search" / "sources"
)

# Direct-request sources: they call requests.get/post with a
# timeout constant and own no session, browser, or nested pool. Any source
# added later should extend this tuple instead of forking the contract.
BUDGETED_MODULES = (
    "al",
    "at",
    "by",
    "dd",
    "dj",
    "dl",
    "dt",
    "dw",
    "ff",
    "fx",
    "he",
    "hs",
    "mb",
    "mx",
    "nk",
    "nx",
    "rm",
    "sf",
    "sj",
    "sl",
    "wd",
    "wx",
)

REQUEST_FUNCTIONS = ("get", "post", "request")

BUDGET_HELPER_MODULE = "quasarr.search.sources.helpers.budget"

# Local names that already carry a budget-derived timeout and may therefore be
# passed straight to `timeout=`. Names assigned from `clamp_timeout(...)` inside
# the module are detected automatically; this map is for expressions the parser
# cannot follow (e.g. a timeout handed in as a parameter). Add new
# entries here rather than relaxing the rule.
ALLOWED_TIMEOUT_NAMES: dict[str, set[str]] = {}


def _module_tree(module_name):
    """Parse the module text - the contract must not import a source module."""
    source = (SOURCES_DIR / f"{module_name}.py").read_text(encoding="utf-8")
    return ast.parse(source)


def _names_assigned_from_clamp(tree):
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if not (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "clamp_timeout"
        ):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _request_calls(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in REQUEST_FUNCTIONS:
            continue
        if isinstance(func.value, ast.Name) and func.value.id == "requests":
            yield node


def _timeout_violations(module_name):
    tree = _module_tree(module_name)
    budget_derived = _names_assigned_from_clamp(tree) | ALLOWED_TIMEOUT_NAMES.get(
        module_name, set()
    )

    violations = []
    for call in _request_calls(tree):
        where = f"{module_name}.py:{call.lineno} requests.{call.func.attr}()"
        keyword = next((kw for kw in call.keywords if kw.arg == "timeout"), None)
        if keyword is None:
            violations.append(f"{where} passes no timeout=")
            continue

        value = keyword.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "clamp_timeout"
        ):
            continue
        if isinstance(value, ast.Name) and value.id in budget_derived:
            continue
        if isinstance(value, ast.Name) and value.id.endswith("_TIMEOUT_SECONDS"):
            violations.append(f"{where} passes the unclamped constant {value.id}")
            continue
        violations.append(f"{where} passes timeout={ast.unparse(value)}")
    return violations


def _budget_imports(module_name):
    imported = set()
    for node in ast.walk(_module_tree(module_name)):
        if isinstance(node, ast.ImportFrom) and node.module == BUDGET_HELPER_MODULE:
            imported.update(alias.name for alias in node.names)
    return imported


class SourceBudgetContractTests(unittest.TestCase):
    """Static contract: no direct request may outlive the request deadline."""

    def test_every_direct_request_clamps_its_timeout_to_the_budget(self):
        violations = []
        for module_name in BUDGETED_MODULES:
            violations.extend(_timeout_violations(module_name))

        self.assertEqual([], violations, "\n".join(violations))

    def test_every_budgeted_module_reads_the_budget_helper(self):
        for module_name in BUDGETED_MODULES:
            with self.subTest(module=module_name):
                imported = _budget_imports(module_name)
                self.assertIn("checkpoint", imported)
                self.assertIn("clamp_timeout", imported)


class ManualClock:
    """Wall clock a test moves by hand, so no budget test needs to sleep."""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class ThreadScopedClock:
    def __init__(self, main_times, worker_time):
        self._main_thread = get_ident()
        self._main_times = list(main_times)
        self._worker_time = worker_time
        self._main_index = 0

    def __call__(self):
        if get_ident() != self._main_thread:
            return self._worker_time
        index = min(self._main_index, len(self._main_times) - 1)
        self._main_index += 1
        return self._main_times[index]


class FakeResponse:
    status_code = 200

    def __init__(self, payload=None, content=b""):
        self._payload = payload
        self.content = content
        self.text = content.decode("utf-8")

    def raise_for_status(self):
        return None

    def json(self):
        return {} if self._payload is None else self._payload


class RecordingRequests:
    """Stands in for the `requests` name bound inside ONE source module."""

    def __init__(self, responses=(), on_call=None):
        self._responses = list(responses)
        self._on_call = on_call
        self.calls = []

    def _record(self, method, url, kwargs):
        self.calls.append(
            SimpleNamespace(method=method, url=url, timeout=kwargs.get("timeout"))
        )
        if self._on_call is not None:
            self._on_call(len(self.calls))
        if self._responses:
            return self._responses.pop(0)
        return FakeResponse(content=b"")

    def get(self, url, *_args, **kwargs):
        return self._record("get", url, kwargs)

    def post(self, url, *_args, **kwargs):
        return self._record("post", url, kwargs)

    @property
    def timeouts(self):
        return [call.timeout for call in self.calls]


def shared_state_for(initials):
    return SimpleNamespace(
        values={
            "config": lambda _section: {initials: f"{initials}.invalid"},
            "user_agent": "test-agent",
            "internal_address": "http://127.0.0.1:8080",
        }
    )


class SourceBudgetBehaviorTests(unittest.TestCase):
    def test_at_nested_workers_receive_the_parent_budget(self):
        from quasarr.search.sources import at

        fake = RecordingRequests()
        clock = ThreadScopedClock([0.0], worker_time=10.0)
        with (
            patch.object(at, "requests", fake),
            use_search_budget(10.0, clock=clock),
        ):
            with self.assertRaises(SearchBudgetExhausted):
                at._load_entries(
                    shared_state_for("at"),
                    "https://at.invalid/listing",
                    "https://at.invalid/attachments",
                    15,
                )

        self.assertEqual([], fake.calls)

    def test_at_stops_before_submitting_the_next_nested_request(self):
        from quasarr.search.sources import at

        fake = RecordingRequests([FakeResponse(content=b"<html></html>")])
        clock = ThreadScopedClock([0.0, 10.0], worker_time=0.0)
        with (
            patch.object(at, "requests", fake),
            use_search_budget(10.0, clock=clock),
        ):
            entries = at._load_entries(
                shared_state_for("at"),
                "https://at.invalid/listing",
                "https://at.invalid/attachments",
                15,
            )

        self.assertEqual([], entries)
        self.assertEqual(
            ["https://at.invalid/listing"], [call.url for call in fake.calls]
        )

    def test_sl_nested_workers_receive_the_parent_budget(self):
        from quasarr.search.sources import sl

        sessions = []

        def make_session():
            session = MagicMock()
            sessions.append(session)
            return session

        clock = ThreadScopedClock([0.0], worker_time=10.0)
        with (
            patch.object(sl.requests, "Session", side_effect=make_session),
            patch.object(
                sl,
                "ensure_session_cf_bypassed",
                side_effect=lambda _info, _state, session, _url, headers, timeout=None: (
                    session,
                    headers,
                    FakeResponse(content=b"<html></html>"),
                ),
            ) as ensure,
            patch.object(sl, "mark_hostname_issue") as marked,
            patch.object(sl, "clear_hostname_issue"),
            use_search_budget(10.0, clock=clock),
        ):
            releases = sl.Source().search(
                shared_state_for("sl"),
                0.0,
                5000,
                "Synthetic Show",
            )

        self.assertEqual([], releases)
        self.assertEqual([], sessions)
        ensure.assert_not_called()
        marked.assert_not_called()

    def test_sl_stops_before_submitting_the_next_nested_request(self):
        from quasarr.search.sources import sl

        sessions = []
        requested_urls = []

        def make_session():
            session = MagicMock()
            sessions.append(session)
            return session

        def ensure(_info, _state, session, url, headers, timeout=None):
            requested_urls.append(url)
            return session, headers, FakeResponse(content=b"<html></html>")

        clock = ThreadScopedClock([0.0, 10.0], worker_time=0.0)
        with (
            patch.object(sl.requests, "Session", side_effect=make_session),
            patch.object(sl, "ensure_session_cf_bypassed", side_effect=ensure),
            patch.object(sl, "mark_hostname_issue") as marked,
            patch.object(sl, "clear_hostname_issue"),
            use_search_budget(10.0, clock=clock),
        ):
            releases = sl.Source().search(
                shared_state_for("sl"),
                0.0,
                5000,
                "Synthetic Show",
            )

        self.assertEqual([], releases)
        self.assertEqual(1, len(requested_urls))
        self.assertEqual(1, len(sessions))
        sessions[0].close.assert_called_once()
        marked.assert_not_called()

    def test_single_page_source_starts_no_request_after_exhaustion(self):
        from quasarr.search.sources import hs

        fake = RecordingRequests()
        with (
            patch.object(hs, "requests", fake),
            patch.object(hs, "mark_hostname_issue") as marked,
            patch.object(hs, "clear_hostname_issue"),
            use_search_budget(10.0, clock=ManualClock(10.0)),
        ):
            releases = hs.Source().search(
                shared_state_for("hs"), 0.0, SEARCH_CAT_MOVIES, "tt1234567"
            )

        self.assertEqual([], releases)
        self.assertEqual([], fake.calls)
        # A spent budget is not the host's fault, so it must not be recorded as one.
        marked.assert_not_called()

    def test_single_page_source_clamps_timeout_to_remaining_budget(self):
        from quasarr.search.sources import hs

        fake = RecordingRequests([FakeResponse(content=b"<html></html>")])
        with (
            patch.object(hs, "requests", fake),
            patch.object(hs, "mark_hostname_issue"),
            patch.object(hs, "clear_hostname_issue"),
            use_search_budget(0.5, clock=ManualClock(0.0)),
        ):
            hs.Source().search(
                shared_state_for("hs"), 0.0, SEARCH_CAT_MOVIES, "tt1234567"
            )

        self.assertEqual([0.5], fake.timeouts)

    def test_multi_detail_source_answers_with_what_it_already_collected(self):
        from quasarr.search.sources import wx

        clock = ManualClock(0.0)
        listing = FakeResponse({"items": {"data": [{"uid": "u1"}, {"uid": "u2"}]}})
        detail = FakeResponse({"item": {"fulltitle": "Synthetic Movie 2031 1080p"}})

        def spend_budget_during_first_detail(call_count):
            if call_count == 2:
                clock.now = 10.0

        fake = RecordingRequests(
            [listing, detail], on_call=spend_budget_during_first_detail
        )
        with (
            patch.object(wx, "requests", fake),
            patch.object(wx, "get_year", return_value=None),
            patch.object(wx, "is_valid_release", return_value=True),
            patch.object(wx, "generate_download_link", return_value="download"),
            patch.object(wx, "mark_hostname_issue") as marked,
            patch.object(wx, "clear_hostname_issue"),
            use_search_budget(10.0, clock=clock),
        ):
            releases = wx.Source().search(
                shared_state_for("wx"), 0.0, SEARCH_CAT_MOVIES, "Synthetic Movie"
            )

        # Listing plus the first detail page - the second detail never starts.
        self.assertEqual(2, len(fake.calls))
        self.assertEqual(1, len(releases))
        marked.assert_not_called()

    def test_dual_path_source_clamps_the_constant_of_the_path_it_took(self):
        from quasarr.search.sources import he

        # 20s left sits between the search (15s) and feed (30s) constants, so the
        # clamped value proves which constant the call site read.
        feed_requests = RecordingRequests([FakeResponse(content=b"<html></html>")])
        with (
            patch.object(he, "requests", feed_requests),
            patch.object(he, "mark_hostname_issue"),
            patch.object(he, "clear_hostname_issue"),
            use_search_budget(20.0, clock=ManualClock(0.0)),
        ):
            he.Source().feed(shared_state_for("he"), 0.0, SEARCH_CAT_MOVIES)

        self.assertEqual([20.0], feed_requests.timeouts)

        search_requests = RecordingRequests([FakeResponse(content=b"<html></html>")])
        with (
            patch.object(he, "requests", search_requests),
            patch.object(he, "get_localized_title", return_value="Synthetic Movie"),
            patch.object(he, "get_year", return_value=None),
            patch.object(he, "mark_hostname_issue"),
            patch.object(he, "clear_hostname_issue"),
            use_search_budget(20.0, clock=ManualClock(0.0)),
        ):
            he.Source().search(
                shared_state_for("he"), 0.0, SEARCH_CAT_MOVIES, "tt1234567"
            )

        self.assertEqual([15], search_requests.timeouts)

    def test_dual_path_source_starts_no_request_after_exhaustion(self):
        from quasarr.search.sources import he

        feed_requests = RecordingRequests()
        with (
            patch.object(he, "requests", feed_requests),
            patch.object(he, "mark_hostname_issue") as feed_marked,
            patch.object(he, "clear_hostname_issue"),
            use_search_budget(5.0, clock=ManualClock(5.0)),
        ):
            self.assertEqual(
                [], he.Source().feed(shared_state_for("he"), 0.0, SEARCH_CAT_MOVIES)
            )

        self.assertEqual([], feed_requests.calls)
        feed_marked.assert_not_called()

        search_requests = RecordingRequests()
        with (
            patch.object(he, "requests", search_requests),
            patch.object(he, "get_localized_title", return_value="Synthetic Movie"),
            patch.object(he, "get_year", return_value=None),
            patch.object(he, "mark_hostname_issue") as search_marked,
            patch.object(he, "clear_hostname_issue"),
            use_search_budget(5.0, clock=ManualClock(5.0)),
        ):
            self.assertEqual(
                [],
                he.Source().search(
                    shared_state_for("he"), 0.0, SEARCH_CAT_MOVIES, "tt1234567"
                ),
            )

        self.assertEqual([], search_requests.calls)
        search_marked.assert_not_called()


class FakeReleaseBudgetTests(unittest.TestCase):
    """A response that serves fakes is never an answer, budget or no budget."""

    # DD's API answers `{"results": [...], "nextCursor": ...}` and names the
    # title field `releaseName`; the bare list and `release` key these
    # fixtures used belong to the pre-4.6.17 endpoint. The budget contract
    # itself is unchanged - only the transport shape moved.
    def _page_session(self, first_page):
        session = MagicMock()
        session.get.side_effect = [first_page] + [
            FakeResponse({"results": [], "nextCursor": None}) for _ in range(4)
        ]
        return session

    @staticmethod
    def _fake_serving_page():
        return FakeResponse(
            {
                "results": [
                    {
                        "releaseName": "Synthetic.Movie.2031.1080p.WEB.h264-GROUP",
                        "size": 1073741824,
                        "when": 1700000000,
                    },
                    {"releaseName": "Synthetic.Movie.2031.Fake", "fake": True},
                ],
                "nextCursor": None,
            }
        )

    def test_fake_release_answers_empty_when_the_budget_runs_out_first(self):
        from quasarr.search.sources import dd

        clock = ManualClock(0.0)

        def spend_budget(*_args):
            # The last release parsed before the fake one uses up the budget.
            clock.now = 10.0
            return "download://synthetic"

        state = shared_state_for("dd")
        with (
            patch.object(
                dd,
                "retrieve_and_validate_session",
                return_value=self._page_session(self._fake_serving_page()),
            ),
            patch.object(dd, "create_and_persist_session") as invalidated,
            patch.object(dd, "is_valid_release", return_value=True),
            patch.object(dd, "generate_download_link", side_effect=spend_budget),
            patch.object(dd, "mark_hostname_issue") as marked,
            patch.object(dd, "clear_hostname_issue"),
            use_search_budget(10.0, clock=clock),
        ):
            releases = dd.Source().search(
                state, 0.0, SEARCH_CAT_MOVIES, "Synthetic Movie"
            )

        self.assertEqual([], releases)
        # No time left to log back in, but the fakes must not be answered with.
        invalidated.assert_not_called()
        marked.assert_not_called()

    def test_fake_release_still_invalidates_the_session_while_time_remains(self):
        from quasarr.search.sources import dd

        state = shared_state_for("dd")
        with (
            patch.object(
                dd,
                "retrieve_and_validate_session",
                return_value=self._page_session(self._fake_serving_page()),
            ),
            patch.object(dd, "create_and_persist_session") as invalidated,
            patch.object(dd, "is_valid_release", return_value=True),
            patch.object(
                dd, "generate_download_link", return_value="download://synthetic"
            ),
            patch.object(dd, "mark_hostname_issue") as marked,
            patch.object(dd, "clear_hostname_issue"),
            use_search_budget(10.0, clock=ManualClock(0.0)),
        ):
            releases = dd.Source().search(
                state, 0.0, SEARCH_CAT_MOVIES, "Synthetic Movie"
            )

        self.assertEqual([], releases)
        invalidated.assert_called_once_with(state)
        marked.assert_not_called()


class SharedProviderBudgetTests(unittest.TestCase):
    def test_arr_clients_clamp_requests_to_the_worker_budget(self):
        from quasarr.providers import radarr_api, sonarr_api

        for module, client_type in (
            (radarr_api, radarr_api.RadarrAPIClient),
            (sonarr_api, sonarr_api.SonarrAPIClient),
        ):
            with self.subTest(module=module.__name__):
                fake = RecordingRequests([FakeResponse(payload={})])
                with (
                    patch.object(module, "requests", fake),
                    use_search_budget(0.25, clock=ManualClock()),
                ):
                    client_type("https://arr.invalid", "api-key")._get("/test")

                self.assertEqual([0.25], fake.timeouts)

    def test_arr_wanted_pagination_stops_before_the_next_page(self):
        from quasarr.providers import radarr_api, sonarr_api

        cases = (
            (radarr_api, "radarr_client", radarr_api.get_wanted_imdb_ids),
            (sonarr_api, "sonarr_client", sonarr_api.get_wanted_episodes),
        )
        for _module, state_key, wanted in cases:
            with self.subTest(state_key=state_key):
                clock = ManualClock()
                client = MagicMock()

                def first_page(*_args, page_clock=clock, **_kwargs):
                    page_clock.now = 10.0
                    return {"records": []}

                client.wanted.side_effect = first_page
                state = SimpleNamespace(values={state_key: client})
                status = {}
                with use_search_budget(10.0, clock=clock):
                    self.assertEqual([], wanted(state, status=status))

                self.assertEqual(1, client.wanted.call_count)
                self.assertEqual({"complete": False}, status)


if __name__ == "__main__":
    unittest.main()
