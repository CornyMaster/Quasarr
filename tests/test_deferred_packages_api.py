# -*- coding: utf-8 -*-

import json
import os
import re
import unittest
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest import mock

from bottle import Bottle

import quasarr.api.packages as packages_api
from quasarr.providers.auth import audit_route_auth_modes

PACKAGE_A = "Quasarr_movies_" + "a" * 32
PACKAGE_B = "Quasarr_movies_" + "b" * 32
PACKAGE_C = "Quasarr_movies_" + "c" * 32
PACKAGE_D = "Quasarr_movies_" + "d" * 32
SELECTOR_HOSTILE_PACKAGE_ID = PACKAGE_A + '"][data-selected="unexpected'
RETRY_AFTER = 4_000_000_000
COHORT_DEADLINE = RETRY_AFTER + 5 * 86400
SWEEP_ID = "5e" * 16
LINK_FINGERPRINT = "7f" * 32


def deferred_block(
    *,
    state="observing",
    hold_type="provisional",
    evidence_count=1,
    probe_requested=False,
    crypter="filecrypt",
    reason_code="ip_block_suspected",
    active=True,
    generation=False,
    cohort_tested=0,
    cohort_total=0,
    cohort_deadline_epoch=0,
    cohort_retest_depth=0,
):
    block = {
        "crypter": crypter,
        "reason_code": reason_code,
        "since_epoch": 1_700_000_000,
        "retry_after_epoch": RETRY_AFTER,
        "probe_requested": probe_requested,
        "observation_holds": 1,
        "state": state,
        "evidence_count": evidence_count,
        "hold_type": hold_type,
        "active": active,
        "cohort_tested": cohort_tested,
        "cohort_total": cohort_total,
        "cohort_deadline_epoch": cohort_deadline_epoch,
        "cohort_retest_depth": cohort_retest_depth,
    }
    if generation:
        block.update(
            {
                "schema_version": 2,
                "sweep_id": SWEEP_ID,
                "link_fingerprints": [LINK_FINGERPRINT],
            }
        )
    return block


def queue_item(package_id, title, deferred=None, storage=""):
    item = {
        "nzo_id": package_id,
        "filename": title,
        "cat": "movies",
        "type": "protected",
        "percentage": 0,
        "timeleft": "23:59:59",
        "mb": 1024,
        "bytes": 0,
        "storage": storage,
    }
    if deferred is not None:
        item["deferred"] = deferred
    return item


def history_item(package_id, name):
    return {
        "nzo_id": package_id,
        "name": name,
        "category": "movies",
        "status": "Failed",
        "fail_message": "Synthetic failure",
        "bytes": 0,
        "storage": "",
    }


class MemoryDatabase:
    def __init__(self):
        self.rows = {}

    def retrieve(self, key):
        return self.rows.get(key)

    def mutate_value(self, key, mutator):
        value = mutator(self.rows.get(key))
        if value is None:
            self.rows.pop(key, None)
        else:
            self.rows[key] = value
        return value

    def delete_exact(self, key, value):
        if self.rows.get(key) != value:
            return False
        self.rows.pop(key)
        return True


class DeferredApiState:
    def __init__(self):
        self.values = {"crypter_cooldown_hours": 24}
        self.databases = {
            "protected": MemoryDatabase(),
            "failed": MemoryDatabase(),
        }
        self.device_calls = 0

    @property
    def protected(self):
        return self.databases["protected"]

    def get_db(self, table):
        return self.databases[table]

    def get_device(self):
        self.device_calls += 1
        raise AssertionError("deferred package commands must not access JDownloader")

    def add_package(self, package_id, *, deferred, generation=False):
        package = {
            "title": f"Synthetic {package_id[-4:]}",
            "links": [],
            "password": "",
            "size_mb": 1,
        }
        if deferred:
            package["deferred"] = {
                "crypter": "filecrypt",
                "reason_code": "ip_block_suspected",
                "since_epoch": 1_700_000_000,
                "retry_after_epoch": RETRY_AFTER,
                "probe_requested": False,
                "observation_holds": 1,
            }
            if generation:
                package["deferred"].update(
                    {
                        "schema_version": 2,
                        "sweep_id": SWEEP_ID,
                        "link_fingerprints": [LINK_FINGERPRINT],
                    }
                )
        self.protected.rows[package_id] = json.dumps(package)


def route_callback(app, method, rule):
    return next(
        route.callback
        for route in app.routes
        if route.method == method and route.rule == rule
    )


def render_packages_page(packages_content=""):
    """Render the Classic `/packages` page and its inline-script contract.

    Every caller of this helper pins Classic on purpose: they inspect
    Classic's own inline `<script>` functions (`refreshContent`,
    `restoreDeferredSelection`, `selectedDeferredPackageIds`, the countdown
    epoch helper, the selector-injection audit) - contracts that only exist
    in Classic's markup and are unrelated to which UI mode `DEFAULT_UI`
    happens to select. The `QUASARR_UI` env override forces that
    deterministically regardless of the default.
    """
    app = Bottle()
    packages_api.setup_packages_routes(app)
    page_route = route_callback(app, "GET", "/packages")

    with (
        mock.patch.dict(packages_api.shared_state.values, {"device": object()}),
        mock.patch.object(packages_api, "request", SimpleNamespace(query={})),
        mock.patch.object(
            packages_api,
            "_render_packages_content",
            return_value=packages_content,
        ),
        mock.patch.dict(os.environ, {"QUASARR_UI": "classic"}),
    ):
        return page_route()


def render_deferred_fragment(package_ids):
    downloads = {
        "queue": [
            queue_item(
                package_id,
                f"[Waiting for linkcrypter retry] Deferred {package_id[-4:]}",
                deferred_block(),
            )
            for package_id in package_ids
        ],
        "history": [],
    }

    with mock.patch.object(packages_api, "get_packages", return_value=downloads):
        return packages_api._render_packages_content()


def render_selector_audit_fragment():
    downloads = {
        "queue": [
            queue_item(
                SELECTOR_HOSTILE_PACKAGE_ID,
                "[Waiting for linkcrypter retry] Deferred audit package",
                deferred_block(),
            ),
            queue_item(
                PACKAGE_B,
                "[CAPTCHA not solved!] Ordinary audit package",
            ),
        ],
        "history": [history_item(PACKAGE_C, "History audit package")],
    }

    with mock.patch.object(packages_api, "get_packages", return_value=downloads):
        return packages_api._render_packages_content()


class DeferredSelectParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.checkboxes = []

    def handle_starttag(self, tag, attrs):
        if tag != "input":
            return
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if "deferred-package-select" not in classes:
            return
        self.checkboxes.append(
            (attributes.get("value", ""), "checked" in attributes),
        )


class RefreshDom:
    """Stand-in for the deferred checkboxes inside `#packages-content`.

    Only the operations the refresh cycle performs on them are modelled, so the
    order extracted from the shipped script can be replayed against real server
    output instead of matched as text.
    """

    def __init__(self, fragment):
        self.checkboxes = []
        self.replace(fragment)

    def replace(self, fragment):
        parser = DeferredSelectParser()
        parser.feed(fragment)
        self.checkboxes = [
            {"value": value, "checked": checked} for value, checked in parser.checkboxes
        ]

    def check(self, *values):
        wanted = set(values)
        for checkbox in self.checkboxes:
            if checkbox["value"] in wanted:
                checkbox["checked"] = True

    def restore_selection(self, package_ids):
        selected = set(package_ids)
        for checkbox in self.checkboxes:
            if checkbox["value"] in selected:
                checkbox["checked"] = True

    def selected_values(self):
        return [
            checkbox["value"] for checkbox in self.checkboxes if checkbox["checked"]
        ]


def javascript_function_body(source, name):
    start = source.index(f"function {name}(")
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : index]
    raise AssertionError(f"unbalanced braces in {name}")


def javascript_function_source(source, name):
    start = source.index(f"function {name}(")
    body = javascript_function_body(source, name)
    return source[start : source.index(body, start) + len(body) + 1]


REFRESH_SELECTION_STEPS = (
    ("snapshot", re.compile(r"const (\w+) = selectedDeferredPackageIds\(\);")),
    ("replace", re.compile(r"container\.innerHTML = (html);")),
    ("restore", re.compile(r"restoreDeferredSelection\((\w+)\);")),
)


def refresh_selection_steps(body):
    found = []
    for name, pattern in REFRESH_SELECTION_STEPS:
        match = pattern.search(body)
        if match is None:
            raise AssertionError(f"refreshContent performs no {name} step")
        found.append((match.start(), name, match.group(1)))
    found.sort()
    return [(name, argument) for _, name, argument in found]


JS_STRING_LITERAL = re.compile(r"""^('[^']*'|"[^"]*")$""")
QUERY_SELECTOR_CALL = re.compile(r"\bquerySelector(All)?\s*\(")


def _call_argument(source, opening):
    depth = 0
    quote = None
    index = opening
    while index < len(source):
        character = source[index]
        if quote is not None:
            if character == "\\":
                index += 1
            elif character == quote:
                quote = None
        elif character in "'\"`":
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : index]
        index += 1
    raise AssertionError("unbalanced call around offset {0}".format(opening))


def query_selector_calls(source):
    """Every `querySelector` and `querySelectorAll` argument in `source`.

    Arguments are read with quote and paren awareness so a literal such as
    `':not(#id)'` is captured whole instead of being truncated into a false
    positive.
    """
    calls = []
    for match in QUERY_SELECTOR_CALL.finditer(source):
        name = "querySelectorAll" if match.group(1) else "querySelector"
        calls.append((name, _call_argument(source, match.end() - 1).strip()))
    return calls


def built_selector_arguments(source):
    return [
        (name, argument)
        for name, argument in query_selector_calls(source)
        if not JS_STRING_LITERAL.match(argument)
    ]


RESTORE_SIGNATURE = re.compile(r"function restoreDeferredSelection\(([^)]*)\)")
SET_FROM_IDENTIFIER = re.compile(
    r"const\s+(\w+)\s*=\s*new\s+Set\(\s*([A-Za-z_$][\w$]*)\s*\)\s*;"
)
RESTORE_ITERATION = re.compile(
    r"document\.querySelector(?:All)?\((.*?)\)\.forEach\("
    r"\s*\(?\s*([A-Za-z_$][\w$]*)\s*\)?\s*=>"
)
GUARDED_CHECK = re.compile(
    r"if\s*\(\s*(\w+)\.has\(\s*([A-Za-z_$][\w$]*)\.(\w+)\s*\)\s*\)\s*"
    r"([A-Za-z_$][\w$]*)\.checked\s*=\s*(\w+)\s*;"
)
CHECKED_ASSIGNMENT = re.compile(r"\.checked\s*=")
SNAPSHOT_PROPERTY = re.compile(r"=>\s*([A-Za-z_$][\w$]*)\.(\w+)")


def selection_snapshot_property(page):
    body = javascript_function_body(page, "selectedDeferredPackageIds")
    match = SNAPSHOT_PROPERTY.search(body)
    if match is None:
        raise AssertionError("selectedDeferredPackageIds reads no checkbox property")
    return match.group(2)


def restore_selection_contract(source, package_id_property):
    """Structure of the shipped `restoreDeferredSelection` helper.

    Raises `AssertionError` for any helper that would not re-check exactly the
    snapshotted values, so a mutated copy of the shipped source can be asserted
    to be rejected. This is static analysis of the shipped text; it does not
    execute JavaScript.
    """
    signature = RESTORE_SIGNATURE.search(source)
    if signature is None:
        raise AssertionError("no restoreDeferredSelection(...) helper is shipped")
    parameters = [
        name.strip() for name in signature.group(1).split(",") if name.strip()
    ]
    if len(parameters) != 1:
        raise AssertionError(
            f"restoreDeferredSelection takes {parameters}, want exactly one parameter"
        )
    body = javascript_function_body(source, "restoreDeferredSelection")

    membership = SET_FROM_IDENTIFIER.search(body)
    if membership is None:
        raise AssertionError("the helper builds no `const <name> = new Set(<name>);`")
    if membership.group(2) != parameters[0]:
        raise AssertionError(
            f"the membership set is built from {membership.group(2)!r}, "
            f"not from the parameter {parameters[0]!r}"
        )

    iteration = RESTORE_ITERATION.search(body)
    if iteration is None:
        raise AssertionError(
            "the helper iterates no document.querySelector(All) result"
        )
    selector = iteration.group(1).strip()
    if not JS_STRING_LITERAL.match(selector):
        raise AssertionError(f"selector {selector!r} is not a quoted literal")

    guard = GUARDED_CHECK.search(body)
    if guard is None:
        raise AssertionError(
            "the helper has no `if (<set>.has(<checkbox>.<property>)) "
            "<checkbox>.checked = <value>;`"
        )
    set_name, tested, property_name, assigned, value = guard.groups()
    if set_name != membership.group(1):
        raise AssertionError(
            f"membership is tested on {set_name!r}, not on the set built from the "
            f"parameter ({membership.group(1)!r})"
        )
    if tested != iteration.group(2) or assigned != iteration.group(2):
        raise AssertionError(
            "the guard and the assignment must both use the iterated checkbox "
            f"({iteration.group(2)!r})"
        )
    if property_name != package_id_property:
        raise AssertionError(
            f"membership is tested on {property_name!r}, not on the property the "
            f"snapshot reads ({package_id_property!r})"
        )
    if value != "true":
        raise AssertionError(f"the restore assigns {value!r} instead of true")
    if len(CHECKED_ASSIGNMENT.findall(body)) != 1:
        raise AssertionError("the helper writes `checked` outside the membership guard")
    if not membership.start() < iteration.start() < guard.start():
        raise AssertionError("the set must be built before the checkboxes are visited")

    return SimpleNamespace(
        parameter=parameters[0],
        set_name=set_name,
        selector=selector[1:-1],
        checkbox=iteration.group(2),
        property_name=property_name,
    )


# Each entry mutates the *shipped* helper text, so the contract above is proven
# to reject the weakenings that the outcome replay alone stays green for.
RESTORE_MUTATIONS = (
    ("set ignores the parameter", "new Set(packageIds)", "new Set()"),
    ("set built from a constant", "new Set(packageIds)", "new Set(['x'])"),
    (
        "parameter renamed away from the set",
        "restoreDeferredSelection(packageIds)",
        "restoreDeferredSelection(ignored)",
    ),
    (
        "membership tested on the whole list",
        "selected.has(checkbox.value)",
        "selected.has(packageIds)",
    ),
    ("membership tested on the wrong property", "checkbox.value", "checkbox.name"),
    (
        "selector built from a package value",
        "'.deferred-package-select'",
        "'[value=\"' + packageIds[0] + '\"]'",
    ),
    (
        "restore no longer guarded",
        "if (selected.has(checkbox.value)) checkbox.checked = true;",
        "checkbox.checked = true;",
    ),
    (
        "extra unguarded restore",
        "checkbox.checked = true;",
        "checkbox.checked = true; if (checkbox) checkbox.checked = true;",
    ),
)

TARGETED_RESTORE_MUTATIONS = (
    (
        "extra parameter",
        "restoreDeferredSelection(packageIds)",
        "restoreDeferredSelection(packageIds, stalePackageIds)",
        r"want exactly one parameter",
    ),
    (
        "false checked assignment",
        "checkbox.checked = true;",
        "checkbox.checked = false;",
        r"restore assigns 'false' instead of true",
    ),
    (
        "set constructed after iteration starts",
        (
            "const selected = new Set(packageIds);\n"
            "                    if (!selected.size) return;\n\n"
            "                    document.querySelectorAll("
            "'.deferred-package-select').forEach(checkbox => {"
        ),
        (
            "document.querySelectorAll("
            "'.deferred-package-select').forEach(checkbox => {\n"
            "                        const selected = new Set(packageIds);\n"
            "                        if (!selected.size) return;"
        ),
        r"set must be built before the checkboxes are visited",
    ),
)

SELECTOR_AUDIT_FIXTURES = (
    ("singular literal", "document.querySelector('.status-message');", True),
    ("plural literal", 'document.querySelectorAll(".deferred-package-select");', True),
    ("literal with parentheses", "form.querySelectorAll('input:not(#url)');", True),
    (
        "singular concatenation",
        "document.querySelector('[value=\"' + packageId + '\"]');",
        False,
    ),
    (
        "plural concatenation",
        "document.querySelectorAll('[value=' + packageId + ']');",
        False,
    ),
    (
        "singular template literal",
        'document.querySelector(`[value="${packageId}"]`);',
        False,
    ),
    ("plural variable", "document.querySelectorAll(selector);", False),
)

FUNCTION_SIGNATURE = re.compile(r"function (\w+)\(([^)]*)\)")
DATASET_READ = re.compile(r"\.dataset\.(\w+)")
COUNTDOWN_EPOCH_FALLBACK = re.compile(
    r"([A-Za-z_$][\w$]*)\.dataset\.cohortDeadlineEpoch\s*\?\?\s*"
    r"\1\.dataset\.retryAfterEpoch"
)


def _function_owning(source, fragment):
    """The innermost shipped function whose body contains `fragment`."""
    owner = None
    for match in FUNCTION_SIGNATURE.finditer(source):
        name = match.group(1)
        try:
            body = javascript_function_body(source, name)
        except ValueError:
            continue
        if fragment not in body:
            continue
        parameters = [
            parameter.strip()
            for parameter in match.group(2).split(",")
            if parameter.strip()
        ]
        if owner is None or len(body) < len(owner[1]):
            owner = (name, body, parameters)
    return owner


def countdown_epoch_contract(page):
    """Structure of the shipped deferred countdown epoch read.

    Raises `AssertionError` for any page whose countdown would stop following
    the deadline the server rendered onto the element it updates, so a mutated
    copy of the shipped source can be asserted to be rejected.
    """
    fallbacks = list(COUNTDOWN_EPOCH_FALLBACK.finditer(page))
    if len(fallbacks) != 1:
        raise AssertionError(
            f"{len(fallbacks)} `<element>.dataset.cohortDeadlineEpoch ?? "
            "<element>.dataset.retryAfterEpoch` reads are shipped, want exactly one"
        )
    binding = fallbacks[0].group(1)

    epochs = sorted(
        name for name in DATASET_READ.findall(page) if name.endswith("Epoch")
    )
    if epochs != ["cohortDeadlineEpoch", "retryAfterEpoch"]:
        raise AssertionError(
            f"the page reads countdown epochs {epochs}, want exactly the shared "
            "cohort and retry pair"
        )

    owner = _function_owning(page, fallbacks[0].group(0))
    if owner is None:
        raise AssertionError("the epoch fallback belongs to no shipped helper")
    name, _body, parameters = owner
    if parameters != [binding]:
        raise AssertionError(
            f"{name} takes {parameters} but the fallback reads {binding!r}"
        )

    updater = javascript_function_body(page, "updateDeferredCountdowns")
    if f"{name}(" not in updater:
        raise AssertionError(f"updateDeferredCountdowns never calls {name}")
    if ".dataset." in updater:
        raise AssertionError("updateDeferredCountdowns resolves an epoch of its own")
    return SimpleNamespace(helper=name, parameter=binding)


COUNTDOWN_MUTATIONS = (
    (
        "cohort deadline ignored",
        "element.dataset.cohortDeadlineEpoch ?? element.dataset.retryAfterEpoch",
        "element.dataset.retryAfterEpoch",
    ),
    (
        "fallback order reversed",
        "element.dataset.cohortDeadlineEpoch ?? element.dataset.retryAfterEpoch",
        "element.dataset.retryAfterEpoch ?? element.dataset.cohortDeadlineEpoch",
    ),
    (
        "fallback reads a foreign element",
        "element.dataset.cohortDeadlineEpoch ?? element.dataset.retryAfterEpoch",
        "element.dataset.cohortDeadlineEpoch ?? other.dataset.retryAfterEpoch",
    ),
    (
        "cohort deadline renamed",
        "element.dataset.cohortDeadlineEpoch",
        "element.dataset.cohortDeadline",
    ),
    (
        "helper reads something it was not given",
        "function deferredCountdownEpoch(element)",
        "function deferredCountdownEpoch(node)",
    ),
)


def dataset_key(attribute):
    parts = attribute[len("data-") :].split("-")
    return parts[0] + "".join(part.capitalize() for part in parts[1:])


class CountdownParser(HTMLParser):
    """Every rendered `.deferred-countdown` element as dataset plus text."""

    def __init__(self):
        super().__init__()
        self.elements = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if "deferred-countdown" not in (attributes.get("class") or "").split():
            self._current = None
            return
        self._current = {
            "dataset": {
                dataset_key(name): value
                for name, value in attributes.items()
                if name.startswith("data-")
            },
            "text": "",
        }
        self.elements.append(self._current)

    def handle_data(self, data):
        if self._current is not None:
            self._current["text"] += data

    def handle_endtag(self, tag):
        self._current = None


def countdown_elements(fragment):
    parser = CountdownParser()
    parser.feed(fragment)
    return parser.elements


def replay_countdown_epoch(dataset):
    """The Python twin of the shipped `??` epoch read."""
    value = dataset.get("cohortDeadlineEpoch")
    if value is None:
        value = dataset.get("retryAfterEpoch")
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def replay_countdown_text(dataset, now):
    remaining = max(0, replay_countdown_epoch(dataset) - now)
    days, rest = divmod(remaining, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    clock = f"{hours:02}:{minutes:02}:{seconds:02}"
    return f"{days}d {clock}" if days else clock


class DeferredPackagesRenderingTests(unittest.TestCase):
    def test_active_deferred_cards_render_separately_without_sensitive_links(self):
        downloads = {
            "queue": [
                queue_item(
                    PACKAGE_A,
                    "[Waiting for linkcrypter retry] Deferred <script>alert(1)</script>",
                    deferred_block(evidence_count=2),
                    storage="https://secret-source.invalid/private",
                ),
                queue_item(
                    PACKAGE_B,
                    "[Waiting for linkcrypter retry] Cooldown package",
                    deferred_block(
                        state="cooldown",
                        hold_type="crypter_cooldown",
                        evidence_count=3,
                        probe_requested=True,
                        crypter='<img src=x onerror="alert(2)">',
                        reason_code="blocked<script>alert(3)</script>",
                    ),
                ),
                queue_item(
                    PACKAGE_D,
                    "[CAPTCHA not solved!] Ordinary package",
                ),
            ],
            "history": [],
        }

        with mock.patch.object(packages_api, "get_packages", return_value=downloads):
            rendered = packages_api._render_packages_content()

        self.assertIn("Deferred linkcrypter checks", rendered)
        self.assertEqual(
            2, rendered.count('class="package-card deferred-package-card"')
        )
        self.assertIn("Observing", rendered)
        self.assertIn("Cooldown", rendered)
        self.assertIn("<strong>Evidence:</strong> 2", rendered)
        self.assertIn("<strong>Evidence:</strong> 3", rendered)
        self.assertIn("Retry in", rendered)
        self.assertIn(f'data-retry-after-epoch="{RETRY_AFTER}"', rendered)
        self.assertIn("Probe not queued", rendered)
        self.assertIn("Probe queued", rendered)
        self.assertIn("Linkcrypter:", rendered)
        self.assertIn("Reason:", rendered)
        self.assertIn("IP access block suspected", rendered)
        self.assertIn("Check selected", rendered)
        self.assertIn("Delete selected packages", rendered)
        self.assertEqual(2, rendered.count('class="deferred-package-select"'))
        self.assertIn(f'href="/captcha?package_id={PACKAGE_A}"', rendered)
        self.assertIn(f'href="/captcha?package_id={PACKAGE_B}"', rendered)
        self.assertIn("Solve CAPTCHA", rendered)
        self.assertIn("Deferred &lt;script&gt;alert(1)&lt;/script&gt;", rendered)
        self.assertIn("&lt;img src=x onerror=&quot;alert(2)&quot;&gt;", rendered)
        self.assertNotIn("<script>alert", rendered)
        self.assertNotIn("<img src=x", rendered)
        self.assertNotIn("secret-source.invalid", rendered)
        self.assertLess(
            rendered.index("Deferred linkcrypter checks"),
            rendered.index("⬇️ Downloading"),
        )

    def test_generation_bound_cards_never_expose_the_sweep_or_link_fingerprint(self):
        downloads = {
            "queue": [
                queue_item(
                    PACKAGE_A,
                    "[Waiting for linkcrypter retry] Generation package",
                    deferred_block(generation=True),
                ),
            ],
            "history": [],
        }

        with mock.patch.object(packages_api, "get_packages", return_value=downloads):
            rendered = packages_api._render_packages_content()

        self.assertIn("Deferred linkcrypter checks", rendered)
        self.assertEqual(
            1, rendered.count('class="package-card deferred-package-card"')
        )
        self.assertIn("Generation package", rendered)
        self.assertNotIn(SWEEP_ID, rendered)
        self.assertNotIn(LINK_FINGERPRINT, rendered)

    def test_cohort_cards_show_progress_and_retest_depth_but_no_identifier(self):
        downloads = {
            "queue": [
                queue_item(
                    PACKAGE_A,
                    "[Waiting for linkcrypter retry] Cohort package",
                    deferred_block(
                        generation=True,
                        cohort_tested=3,
                        cohort_total=7,
                        cohort_deadline_epoch=RETRY_AFTER,
                        cohort_retest_depth=2,
                    ),
                ),
            ],
            "history": [],
        }

        with mock.patch.object(packages_api, "get_packages", return_value=downloads):
            rendered = packages_api._render_packages_content()

        self.assertIn("<strong>Tested:</strong> 3 / 7", rendered)
        self.assertIn("<strong>Retest queue:</strong> 2", rendered)
        self.assertIn(f'data-cohort-deadline-epoch="{RETRY_AFTER}"', rendered)
        self.assertNotIn(SWEEP_ID, rendered)
        self.assertNotIn(LINK_FINGERPRINT, rendered)

    def test_a_package_without_cohort_progress_renders_no_progress_row(self):
        downloads = {
            "queue": [
                queue_item(
                    PACKAGE_A,
                    "[Waiting for linkcrypter retry] Legacy package",
                    deferred_block(),
                ),
            ],
            "history": [],
        }

        with mock.patch.object(packages_api, "get_packages", return_value=downloads):
            rendered = packages_api._render_packages_content()

        self.assertIn("Deferred linkcrypter checks", rendered)
        self.assertNotIn("<strong>Tested:</strong>", rendered)
        self.assertNotIn("data-cohort-deadline-epoch", rendered)

    def test_hostile_cohort_progress_values_never_reach_the_page(self):
        downloads = {
            "queue": [
                queue_item(
                    PACKAGE_A,
                    "[Waiting for linkcrypter retry] Hostile package",
                    deferred_block(
                        cohort_tested="<script>alert(4)</script>",
                        cohort_total='"><img src=x>',
                        cohort_deadline_epoch='" onload="alert(5)',
                        cohort_retest_depth=None,
                    ),
                ),
            ],
            "history": [],
        }

        with mock.patch.object(packages_api, "get_packages", return_value=downloads):
            rendered = packages_api._render_packages_content()

        self.assertNotIn("<script>alert(4)", rendered)
        self.assertNotIn("<img src=x", rendered)
        self.assertNotIn('onload="alert(5)', rendered)

    def test_inactive_or_unprojected_packages_use_the_normal_queue(self):
        downloads = {
            "queue": [
                queue_item(
                    PACKAGE_A,
                    "[CAPTCHA not solved!] Expired hold",
                    deferred_block(active=False, hold_type="none"),
                ),
                queue_item(
                    PACKAGE_B,
                    "[CAPTCHA not solved!] Fail mode package",
                ),
            ],
            "history": [],
        }

        with mock.patch.object(packages_api, "get_packages", return_value=downloads):
            rendered = packages_api._render_packages_content()

        self.assertNotIn("Deferred linkcrypter checks", rendered)
        self.assertIn("⬇️ Downloading", rendered)
        self.assertIn("Expired hold", rendered)
        self.assertIn("Fail mode package", rendered)
        self.assertEqual(2, rendered.count("Solve CAPTCHA"))
        self.assertNotIn("deferred-package-select", rendered)

    def test_ordinary_queue_and_history_keep_actions_and_escape_dynamic_text(self):
        malicious_package_id = '"><svg/onload=alert(9)>'
        downloads = {
            "queue": [
                queue_item(
                    malicious_package_id,
                    '[CAPTCHA not solved!] Queue <img src=x onerror="alert(1)">',
                )
            ],
            "history": [
                history_item(
                    PACKAGE_B,
                    'History </p><script>alert("history")</script>',
                )
            ],
        }

        with mock.patch.object(packages_api, "get_packages", return_value=downloads):
            rendered = packages_api._render_packages_content()

        self.assertIn("⬇️ Downloading", rendered)
        self.assertIn("📜 Recent History", rendered)
        self.assertIn("Solve CAPTCHA", rendered)
        self.assertIn("confirmDelete", rendered)
        self.assertIn("showPackageDetails", rendered)
        self.assertIn("Queue &lt;img src=x onerror=&quot;alert(1)&quot;&gt;", rendered)
        self.assertIn("History &lt;/p&gt;&lt;script&gt;alert(", rendered)
        self.assertIn("&quot;history&quot;", rendered)
        self.assertNotIn("<img src=x", rendered)
        self.assertNotIn("<script>alert", rendered)
        self.assertNotIn(malicious_package_id, rendered)
        self.assertNotIn("<svg", rendered)
        self.assertIn(
            "%22%3E%3Csvg%2Fonload%3Dalert%289%29%3E",
            rendered,
        )

    def test_ajax_content_endpoint_uses_the_same_deferred_section(self):
        app = Bottle()
        packages_api.setup_packages_routes(app)
        content_route = route_callback(app, "GET", "/api/packages/content")
        downloads = {
            "queue": [
                queue_item(
                    PACKAGE_A,
                    "[Waiting for linkcrypter retry] AJAX package",
                    deferred_block(),
                )
            ],
            "history": [],
        }

        with (
            mock.patch.dict(packages_api.shared_state.values, {"device": object()}),
            mock.patch.object(packages_api, "get_packages", return_value=downloads),
        ):
            rendered = content_route()

        self.assertIn("Deferred linkcrypter checks", rendered)
        self.assertIn("AJAX package", rendered)

    def test_packages_page_runs_individual_and_bulk_commands_through_api_fetch(self):
        rendered = render_packages_page('<div id="deferred-action-status"></div>')

        self.assertIn("quasarrApiFetch(endpoint", rendered)
        self.assertIn("'/api/packages/deferred/probe'", rendered)
        self.assertIn("'/api/packages/deferred'", rendered)
        self.assertIn("'DELETE'", rendered)
        self.assertIn("checkDeferredPackage", rendered)
        self.assertIn("deleteDeferredPackage", rendered)
        self.assertIn("checkSelectedDeferred", rendered)
        self.assertIn("deleteSelectedDeferred", rendered)
        self.assertIn(".deferred-package-select:checked", rendered)
        self.assertIn("button.disabled = true", rendered)
        self.assertIn("button.disabled = false", rendered)
        self.assertIn("statusElement.textContent", rendered)
        self.assertIn("await refreshContent()", rendered)


class DeferredSelectionRefreshTests(unittest.TestCase):
    def test_background_refresh_keeps_selection_of_still_rendered_packages(self):
        page = render_packages_page()
        body = javascript_function_body(page, "refreshContent")
        steps = refresh_selection_steps(body)

        self.assertEqual(
            ["snapshot", "replace", "restore"], [name for name, _ in steps]
        )
        captured_name = steps[0][1]
        # The restore must consume exactly what the snapshot captured, and that
        # binding must not outlive one refresh, or a cleared package would keep
        # re-selecting itself on every later cycle.
        self.assertEqual(captured_name, steps[2][1])
        self.assertEqual(page.count(captured_name), body.count(captured_name))

        before = render_deferred_fragment([PACKAGE_A, PACKAGE_B])
        after = render_deferred_fragment([PACKAGE_A, PACKAGE_C])
        self.assertEqual([], RefreshDom(after).selected_values())

        dom = RefreshDom(before)
        dom.check(PACKAGE_A, PACKAGE_B)

        captured = None
        for name, _ in steps:
            if name == "snapshot":
                captured = dom.selected_values()
            elif name == "replace":
                dom.replace(after)
            else:
                dom.restore_selection(captured)

        self.assertEqual([PACKAGE_A, PACKAGE_B], captured)
        self.assertEqual([PACKAGE_A], dom.selected_values())

    def test_selection_restore_matches_values_without_building_selectors(self):
        page = render_packages_page()
        snapshot_property = selection_snapshot_property(page)
        contract = restore_selection_contract(page, snapshot_property)

        self.assertEqual("value", snapshot_property)
        self.assertEqual(".deferred-package-select", contract.selector)
        # The pinned literal is the class the server actually renders, so the
        # restore cannot silently drift away from the markup it must match.
        self.assertIn(
            f'class="{contract.selector.lstrip(".")}"',
            render_deferred_fragment([PACKAGE_A]),
        )

        calls = query_selector_calls(page)
        self.assertEqual([], built_selector_arguments(page))
        # A quoted literal cannot carry a concatenation or a template
        # placeholder, so no package value can reach a query — and the audit
        # must cover the singular call form too, which this page also ships.
        self.assertIn("querySelector", {name for name, _ in calls})
        self.assertIn("querySelectorAll", {name for name, _ in calls})

    def test_selector_audit_includes_value_bearing_packages_content(self):
        fragment = render_selector_audit_fragment()
        static_page = render_packages_page()
        page = render_packages_page(fragment)

        self.assertIn("Deferred audit package", fragment)
        self.assertIn("Ordinary audit package", fragment)
        self.assertIn("History audit package", fragment)
        self.assertIn(
            f'value="{PACKAGE_A}&quot;][data-selected=&quot;unexpected"',
            fragment,
        )

        selector_arguments = []
        for surface, source in (
            ("static page", static_page),
            ("packages fragment", fragment),
            ("complete page", page),
        ):
            with self.subTest(surface=surface):
                self.assertEqual([], built_selector_arguments(source))
                selector_arguments.extend(
                    argument for _, argument in query_selector_calls(source)
                )

        self.assertTrue(
            fragment in page,
            "the selector audit page omits the real packages fragment",
        )
        self.assertNotIn("data-selected", "\n".join(selector_arguments))

    def test_restore_helper_contract_rejects_weakened_variants(self):
        page = render_packages_page()
        snapshot_property = selection_snapshot_property(page)
        helper = javascript_function_source(page, "restoreDeferredSelection")
        restore_selection_contract(helper, snapshot_property)

        for label, original, replacement in RESTORE_MUTATIONS:
            with self.subTest(mutation=label):
                self.assertIn(original, helper)
                with self.assertRaises(AssertionError):
                    restore_selection_contract(
                        helper.replace(original, replacement), snapshot_property
                    )

    def test_restore_helper_contract_reports_targeted_rule_failures(self):
        page = render_packages_page()
        snapshot_property = selection_snapshot_property(page)
        helper = javascript_function_source(page, "restoreDeferredSelection")

        for label, original, replacement, error_pattern in TARGETED_RESTORE_MUTATIONS:
            with self.subTest(mutation=label):
                self.assertEqual(1, helper.count(original))
                mutant = helper.replace(original, replacement, 1)
                with self.assertRaisesRegex(AssertionError, error_pattern):
                    restore_selection_contract(mutant, snapshot_property)

    def test_selector_audit_reads_both_call_forms_and_rejects_built_selectors(self):
        for label, snippet, is_literal in SELECTOR_AUDIT_FIXTURES:
            with self.subTest(fixture=label):
                self.assertEqual(1, len(query_selector_calls(snippet)))
                self.assertEqual(is_literal, not built_selector_arguments(snippet))

        self.assertEqual(
            {"querySelector", "querySelectorAll"},
            {
                name
                for _, snippet, _ in SELECTOR_AUDIT_FIXTURES
                for name, _ in query_selector_calls(snippet)
            },
        )


class DeferredCountdownTests(unittest.TestCase):
    """The ticking countdown must follow the deadline the server rendered."""

    def cohort_fragment(self):
        downloads = {
            "queue": [
                queue_item(
                    PACKAGE_A,
                    "[Waiting for linkcrypter retry] Cohort package",
                    deferred_block(
                        generation=True,
                        cohort_tested=3,
                        cohort_total=7,
                        cohort_deadline_epoch=COHORT_DEADLINE,
                        cohort_retest_depth=2,
                    ),
                ),
                queue_item(
                    PACKAGE_B,
                    "[Waiting for linkcrypter retry] Ordinary package",
                    deferred_block(),
                ),
            ],
            "history": [],
        }

        with mock.patch.object(packages_api, "get_packages", return_value=downloads):
            return packages_api._render_packages_content()

    def test_the_countdown_resolves_one_shared_epoch_per_element(self):
        page = render_packages_page()

        contract = countdown_epoch_contract(page)

        self.assertEqual("deferredCountdownEpoch", contract.helper)
        # No package value may become part of a query while this is added.
        self.assertEqual([], built_selector_arguments(page))

    def test_the_countdown_contract_rejects_weakened_variants(self):
        page = render_packages_page()

        for label, original, replacement in COUNTDOWN_MUTATIONS:
            with self.subTest(mutation=label):
                self.assertIn(original, page)
                with self.assertRaises(AssertionError):
                    countdown_epoch_contract(page.replace(original, replacement))

    def test_a_cohort_card_carries_only_its_own_deadline(self):
        elements = countdown_elements(self.cohort_fragment())

        datasets = [element["dataset"] for element in elements]
        self.assertEqual(3, len(datasets))
        cohort = [data for data in datasets if "cohortDeadlineEpoch" in data]
        retry = [data for data in datasets if "cohortDeadlineEpoch" not in data]
        self.assertEqual(1, len(cohort))
        self.assertEqual(str(COHORT_DEADLINE), cohort[0]["cohortDeadlineEpoch"])
        # A retry-only read would resolve `undefined` here and collapse the
        # cohort deadline to zero on the very first tick.
        self.assertNotIn("retryAfterEpoch", cohort[0])
        self.assertEqual(2, len(retry))
        for data in retry:
            self.assertEqual(str(RETRY_AFTER), data["retryAfterEpoch"])

    def test_every_tick_keeps_the_server_rendered_deadlines_apart(self):
        elements = countdown_elements(self.cohort_fragment())
        cohort = next(
            element
            for element in elements
            if "cohortDeadlineEpoch" in element["dataset"]
        )
        retry = next(
            element
            for element in elements
            if "cohortDeadlineEpoch" not in element["dataset"]
        )

        self.assertEqual(COHORT_DEADLINE, replay_countdown_epoch(cohort["dataset"]))
        self.assertEqual(RETRY_AFTER, replay_countdown_epoch(retry["dataset"]))

        rendered_days = [
            int(element["text"].strip().split("d ")[0]) for element in (cohort, retry)
        ]
        self.assertEqual(5, rendered_days[0] - rendered_days[1])

        for tick in (0, 1, 59, 3600, 86400):
            with self.subTest(tick=tick):
                now = RETRY_AFTER - 2 * 86400 + tick
                cohort_text = replay_countdown_text(cohort["dataset"], now)
                retry_text = replay_countdown_text(retry["dataset"], now)
                self.assertNotEqual("00:00:00", cohort_text)
                self.assertNotEqual(cohort_text, retry_text)
                self.assertEqual(
                    5,
                    int(cohort_text.split("d ")[0]) - int(retry_text.split("d ")[0]),
                )
                self.assertEqual(cohort_text.split("d ")[1], retry_text.split("d ")[1])


class DeferredPackagesRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Bottle()
        packages_api.setup_packages_routes(self.app)

    def test_command_routes_use_exact_methods_and_api_key_authentication(self):
        methods = {
            route.rule: route.method
            for route in self.app.routes
            if route.rule.startswith("/api/packages/deferred")
        }

        self.assertEqual(
            {
                "/api/packages/deferred/probe": "POST",
                "/api/packages/deferred": "DELETE",
            },
            methods,
        )
        audit_route_auth_modes(
            self.app,
            api_key_prefixes=("/api",),
            public_whitelist=(),
        )

    def test_structurally_malformed_payloads_are_rejected_before_delegation(self):
        probe_route = route_callback(self.app, "POST", "/api/packages/deferred/probe")
        delete_route = route_callback(self.app, "DELETE", "/api/packages/deferred")

        for payload in (None, [], {}, {"package_ids": None}, {"package_ids": "x"}):
            with self.subTest(payload=payload):
                with (
                    mock.patch.object(
                        packages_api, "request", SimpleNamespace(json=payload)
                    ),
                    mock.patch.object(
                        packages_api, "CrypterCooldownService"
                    ) as service,
                    mock.patch.object(
                        packages_api, "delete_database_packages"
                    ) as delete,
                ):
                    self.assertEqual(
                        {
                            "success": False,
                            "message": "package_ids must be a list",
                        },
                        probe_route(),
                    )
                    self.assertEqual(
                        {
                            "success": False,
                            "message": "package_ids must be a list",
                        },
                        delete_route(),
                    )

                service.assert_not_called()
                delete.assert_not_called()

    def test_routes_delegate_valid_lists_to_the_atomic_service_and_batch_helper(self):
        package_ids = [PACKAGE_A, PACKAGE_B]
        probe_result = {"requested": package_ids, "rejected": []}
        delete_result = {"deleted": package_ids, "rejected": []}
        probe_route = route_callback(self.app, "POST", "/api/packages/deferred/probe")
        delete_route = route_callback(self.app, "DELETE", "/api/packages/deferred")

        with (
            mock.patch.dict(
                packages_api.shared_state.values, {"crypter_block_mode": "defer"}
            ),
            mock.patch.object(
                packages_api,
                "request",
                SimpleNamespace(json={"package_ids": package_ids}),
            ),
            mock.patch.object(packages_api, "CrypterCooldownService") as service,
            mock.patch.object(
                packages_api,
                "delete_database_packages",
                return_value=delete_result,
            ) as delete,
        ):
            service.return_value.request_probe.return_value = probe_result

            self.assertEqual(probe_result, probe_route())
            self.assertEqual(delete_result, delete_route())

        service.assert_called_once_with(packages_api.shared_state)
        service.return_value.request_probe.assert_called_once_with(package_ids)
        delete.assert_called_once_with(
            packages_api.shared_state,
            package_ids,
            expected_type="protected",
        )

    def test_probe_returns_mixed_per_id_results_and_changes_only_selected_deferred_ids(
        self,
    ):
        state = DeferredApiState()
        state.add_package(PACKAGE_A, deferred=True)
        state.add_package(PACKAGE_B, deferred=False)
        state.add_package(PACKAGE_D, deferred=True)
        probe_route = route_callback(self.app, "POST", "/api/packages/deferred/probe")
        package_ids = [
            PACKAGE_A,
            PACKAGE_B,
            PACKAGE_C,
            "malformed",
            None,
            PACKAGE_A,
        ]

        with (
            mock.patch.object(packages_api, "shared_state", state),
            mock.patch.object(
                packages_api,
                "request",
                SimpleNamespace(json={"package_ids": package_ids}),
            ),
        ):
            result = probe_route()

        self.assertEqual(
            {
                "requested": [PACKAGE_A],
                "rejected": [
                    {"package_id": PACKAGE_B, "reason": "not_deferred"},
                    {"package_id": PACKAGE_C, "reason": "not_found"},
                    {
                        "package_id": "malformed",
                        "reason": "invalid_package_id",
                    },
                    {"package_id": None, "reason": "invalid_package_id"},
                    {"package_id": PACKAGE_A, "reason": "duplicate"},
                ],
            },
            result,
        )
        self.assertTrue(
            json.loads(state.protected.rows[PACKAGE_A])["deferred"]["probe_requested"]
        )
        self.assertFalse(
            json.loads(state.protected.rows[PACKAGE_D])["deferred"]["probe_requested"]
        )
        self.assertNotIn("deferred", json.loads(state.protected.rows[PACKAGE_B]))
        self.assertEqual(0, state.device_calls)

    def test_delete_returns_mixed_per_id_results_and_keeps_unselected_deferred_ids(
        self,
    ):
        state = DeferredApiState()
        state.add_package(PACKAGE_A, deferred=True)
        state.add_package(PACKAGE_B, deferred=False)
        state.add_package(PACKAGE_D, deferred=True)
        delete_route = route_callback(self.app, "DELETE", "/api/packages/deferred")
        package_ids = [
            PACKAGE_A,
            PACKAGE_B,
            PACKAGE_C,
            "malformed",
            None,
            PACKAGE_A,
        ]

        with (
            mock.patch.object(packages_api, "shared_state", state),
            mock.patch.object(
                packages_api,
                "request",
                SimpleNamespace(json={"package_ids": package_ids}),
            ),
        ):
            result = delete_route()

        self.assertEqual(
            {
                "deleted": [PACKAGE_A],
                "rejected": [
                    {"package_id": PACKAGE_B, "reason": "not_deferred"},
                    {"package_id": PACKAGE_C, "reason": "not_found"},
                    {
                        "package_id": "malformed",
                        "reason": "invalid_package_id",
                    },
                    {"package_id": None, "reason": "invalid_package_id"},
                    {"package_id": PACKAGE_A, "reason": "duplicate"},
                ],
            },
            result,
        )
        self.assertNotIn(PACKAGE_A, state.protected.rows)
        self.assertIn(PACKAGE_B, state.protected.rows)
        self.assertIn(PACKAGE_D, state.protected.rows)
        self.assertEqual(0, state.device_calls)

    def test_generation_bound_holds_stay_probeable_and_deletable(self):
        state = DeferredApiState()
        state.add_package(PACKAGE_A, deferred=True, generation=True)
        state.add_package(PACKAGE_B, deferred=True, generation=True)
        probe_route = route_callback(self.app, "POST", "/api/packages/deferred/probe")
        delete_route = route_callback(self.app, "DELETE", "/api/packages/deferred")

        with (
            mock.patch.dict(state.values, {"crypter_block_mode": "defer"}),
            mock.patch.object(packages_api, "shared_state", state),
            mock.patch.object(
                packages_api,
                "request",
                SimpleNamespace(json={"package_ids": [PACKAGE_A]}),
            ),
        ):
            probe_result = probe_route()

        self.assertEqual({"requested": [PACKAGE_A], "rejected": []}, probe_result)
        self.assertEqual(
            {
                "crypter": "filecrypt",
                "reason_code": "ip_block_suspected",
                "since_epoch": 1_700_000_000,
                "retry_after_epoch": RETRY_AFTER,
                "probe_requested": True,
                "observation_holds": 1,
                "schema_version": 2,
                "sweep_id": SWEEP_ID,
                "link_fingerprints": [LINK_FINGERPRINT],
            },
            json.loads(state.protected.rows[PACKAGE_A])["deferred"],
        )

        with (
            mock.patch.object(packages_api, "shared_state", state),
            mock.patch.object(
                packages_api,
                "request",
                SimpleNamespace(json={"package_ids": [PACKAGE_B]}),
            ),
        ):
            delete_result = delete_route()

        self.assertEqual({"deleted": [PACKAGE_B], "rejected": []}, delete_result)
        self.assertNotIn(PACKAGE_B, state.protected.rows)
        self.assertIn(PACKAGE_A, state.protected.rows)
        self.assertEqual(0, state.device_calls)

    def test_fail_block_mode_makes_probe_inert_but_keeps_delete_working(self):
        state = DeferredApiState()
        state.values["crypter_block_mode"] = "fail"
        state.add_package(PACKAGE_A, deferred=True)
        state.add_package(PACKAGE_D, deferred=True)
        probe_route = route_callback(self.app, "POST", "/api/packages/deferred/probe")
        delete_route = route_callback(self.app, "DELETE", "/api/packages/deferred")

        with (
            mock.patch.object(packages_api, "shared_state", state),
            mock.patch.object(packages_api, "CrypterCooldownService") as service,
            mock.patch.object(
                packages_api,
                "request",
                SimpleNamespace(json={"package_ids": [PACKAGE_A]}),
            ),
        ):
            probe_result = probe_route()

        self.assertEqual(
            {
                "success": False,
                "message": "Linkcrypter blocks are in fail mode",
            },
            probe_result,
        )
        service.assert_not_called()
        # `fail` is a read bypass: no hold exists to probe, and the persisted
        # defer metadata must survive a flip back to `defer` untouched.
        self.assertEqual(
            {
                "crypter": "filecrypt",
                "reason_code": "ip_block_suspected",
                "since_epoch": 1_700_000_000,
                "retry_after_epoch": RETRY_AFTER,
                "probe_requested": False,
                "observation_holds": 1,
            },
            json.loads(state.protected.rows[PACKAGE_A])["deferred"],
        )

        with (
            mock.patch.object(packages_api, "shared_state", state),
            mock.patch.object(
                packages_api,
                "request",
                SimpleNamespace(json={"package_ids": [PACKAGE_A]}),
            ),
        ):
            delete_result = delete_route()

        self.assertEqual({"deleted": [PACKAGE_A], "rejected": []}, delete_result)
        self.assertNotIn(PACKAGE_A, state.protected.rows)
        self.assertIn(PACKAGE_D, state.protected.rows)
        self.assertEqual(0, state.device_calls)


if __name__ == "__main__":
    unittest.main()
