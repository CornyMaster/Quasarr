import ast
import pathlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from quasarr.constants import SEARCH_CAT_MOVIES
from quasarr.search.sources.helpers.budget import use_search_budget

SOURCES_DIR = (
    pathlib.Path(__file__).resolve().parents[1] / "quasarr" / "search" / "sources"
)

# Task 6 owns the direct-request sources: they call requests.get/post with a
# timeout constant and own no session, browser, or nested pool. Task 7 extends
# this tuple with the remaining sources instead of forking the contract.
BUDGETED_MODULES = (
    "by",
    "dj",
    "dt",
    "dw",
    "fx",
    "he",
    "hs",
    "mb",
    "nk",
    "nx",
    "sj",
    "wx",
)

REQUEST_FUNCTIONS = ("get", "post", "request")

BUDGET_HELPER_MODULE = "quasarr.search.sources.helpers.budget"

# Local names that already carry a budget-derived timeout and may therefore be
# passed straight to `timeout=`. Names assigned from `clamp_timeout(...)` inside
# the module are detected automatically; this map is for expressions the parser
# cannot follow (e.g. a timeout handed in as a parameter). Task 7 adds its own
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


if __name__ == "__main__":
    unittest.main()
