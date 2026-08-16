# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import json
import time
from contextlib import ExitStack, contextmanager
from functools import wraps

from bottle import HTTPError, HTTPResponse, abort, request

from quasarr.api.sponsors_helper.cohort_protocol import (
    COHORT_CRYPTER,
    COHORT_REPORT,
    LIFECYCLE_REPORT,
    MALFORMED_REPORT,
    classify_access_report,
    classify_blocked_report,
    helper_supports_cohort,
    helper_supports_defer,
    helper_supports_lifecycle,
    lifecycle_stale_access_response,
    lifecycle_stale_blocked_response,
    render_access_response,
    render_crypter_offer,
    render_defer_response,
    terminal_operation_id,
)
from quasarr.api.sponsors_helper.cohort_protocol import (
    FILECRYPT_LINK_LIFECYCLE_CAPABILITY as FILECRYPT_LINK_LIFECYCLE_CAPABILITY,
)
from quasarr.constants import PACKAGE_ID_PATTERN
from quasarr.downloads import (
    SUBMIT_PHASE_SUBMIT,
    commit_terminal_failure,
    fail,
    failed_package_reason,
    failed_package_records_operation,
    finalize_protected_removal,
    jdownloader_holds_operation,
    project_final_download_urls,
    resolve_protected_crypter_key,
    submit_final_download_urls,
)
from quasarr.providers import shared_state
from quasarr.providers.auth import require_api_key
from quasarr.providers.crypter_candidates import (
    enumerate_filecrypt_candidates,
    enumerate_filecrypt_lifecycle_candidates,
    link_fingerprint,
)
from quasarr.providers.crypter_cooldowns import (
    CrypterCooldownService,
    crypter_blocks_deferred,
    decode_package_defer,
    normalize_crypter_key,
    package_defer_covers_fingerprint,
)
from quasarr.providers.crypter_sweeps import (
    bypass_decision,
    helper_package_is_candidate,
)
from quasarr.providers.filecrypt_lifecycle_service import FilecryptLifecycleService
from quasarr.providers.log import debug, info, warn
from quasarr.providers.notifications import update_release_notification
from quasarr.providers.notifications.helpers.notification_types import NotificationType
from quasarr.providers.statistics import StatsHelper
from quasarr.providers.terminal_operations import (
    CAPACITY,
    CONFLICT,
    EFFECT_ATTEMPTING,
    NOTIFICATION_ATTEMPTING,
    NOTIFICATION_NOT_STARTED,
    NOTIFICATION_RECORDED,
    TERMINAL_OPERATION_MARKER,
    UNREADABLE,
    TerminalOperationService,
    operation_evidence,
)
from quasarr.storage.categories import (
    get_download_category_from_package_id,
    get_download_category_mirrors,
)
from quasarr.storage.config import Config

MAXIMUM_EXCLUDED_PACKAGE_IDS = 100


def require_helper_active(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if shared_state.values.get("helper_active", False):
            last_seen = shared_state.values.get("helper_last_seen", 0)
            if last_seen > 0 and time.time() - last_seen > 300:
                warn(
                    "SponsorsHelper last seen more than 5 minutes ago. Deactivating..."
                )
                shared_state.update("helper_active", False)

        if not shared_state.values.get("helper_active"):
            abort(402, "Sponsors Payment Required")
        return func(*args, **kwargs)

    return wrapper


def normalize_helper_supported_urls(url_patterns):
    if not isinstance(url_patterns, (list, tuple, set)):
        return []

    normalized_patterns = []
    seen_patterns = set()

    for pattern in url_patterns:
        if pattern is None:
            continue

        normalized_pattern = str(pattern).strip().lower()
        if not normalized_pattern or normalized_pattern in seen_patterns:
            continue

        normalized_patterns.append(normalized_pattern)
        seen_patterns.add(normalized_pattern)

    return normalized_patterns


def normalize_helper_supported_mirrors(mirrors):
    return normalize_helper_supported_urls(mirrors)


def extract_helper_candidate_url(link):
    if isinstance(link, (list, tuple)) and link:
        candidate = link[0]
    else:
        candidate = link

    if not isinstance(candidate, str):
        return ""

    return candidate.strip()


def extract_helper_candidate_mirror(link):
    if isinstance(link, (list, tuple)) and len(link) > 1 and isinstance(link[1], str):
        return link[1].strip().lower()
    return ""


def is_rapidgator_link(link):
    if isinstance(link, (list, tuple)) and len(link) > 1:
        mirror_name = link[1]
        if isinstance(mirror_name, str) and "rapidgator" in mirror_name.lower():
            return True

    return "rapidgator" in extract_helper_candidate_url(link).lower()


def prioritize_helper_supported_links(
    links, supported_url_patterns, supported_mirrors=None
):
    if not isinstance(links, list):
        return [], []

    normalized_patterns = normalize_helper_supported_urls(supported_url_patterns)
    normalized_mirrors = normalize_helper_supported_mirrors(supported_mirrors)
    if not normalized_patterns and not normalized_mirrors:
        return list(links), list(links)

    supported_links = []
    unsupported_links = []

    for link in links:
        candidate_url = extract_helper_candidate_url(link).lower()
        if (
            candidate_url
            and any(pattern in candidate_url for pattern in normalized_patterns)
        ) or extract_helper_candidate_mirror(link) in normalized_mirrors:
            supported_links.append(link)
        else:
            unsupported_links.append(link)

    return supported_links + unsupported_links, supported_links


def normalize_excluded_package_ids(package_ids):
    if not isinstance(package_ids, (list, tuple, set)):
        return frozenset()

    normalized_ids = []
    seen_ids = set()
    for package_id in package_ids:
        if (
            not isinstance(package_id, str)
            or not PACKAGE_ID_PATTERN.fullmatch(package_id)
            or package_id in seen_ids
        ):
            continue
        normalized_ids.append(package_id)
        seen_ids.add(package_id)
        if len(normalized_ids) == MAXIMUM_EXCLUDED_PACKAGE_IDS:
            break
    return frozenset(normalized_ids)


def _log_safe_package_id(package_id):
    """Persisted keys are untrusted input; keep them out of the log grammar."""
    sanitized = "".join(
        character if character.isalnum() or character in "_-" else "?"
        for character in package_id[:64]
    )
    return sanitized or "<empty>"


def _link_fingerprint(crypter, link):
    return link_fingerprint(crypter, extract_helper_candidate_url(link))


def _cohort_cooldown(decision):
    """Whether a decision is the linkcrypter-wide cooldown a cohort proved."""
    return bool(
        decision
        and decision["state"] == "cooldown"
        and decision.get("legacy_cooldown") is not True
    )


def _bind_offered_occurrence(links, occurrence):
    """The package's links reduced to exactly the offered Filecrypt occurrence.

    Two raw URLs of one package can normalize to the same fingerprint, so the
    handout is bound to the stored link index rather than to the digest: the
    occurrence the offer was mapped to is the only Filecrypt link that survives,
    and a package whose stored index no longer carries that link is refused so
    the caller can fall back to ordinary work.
    """
    index = occurrence.link_index
    if not isinstance(links, list) or not 0 <= index < len(links):
        return None
    offered = links[index]
    if resolve_protected_crypter_key(offered) != COHORT_CRYPTER:
        return None
    if _link_fingerprint(COHORT_CRYPTER, offered) != occurrence.fingerprint:
        return None
    return [
        link
        for position, link in enumerate(links)
        if position == index or resolve_protected_crypter_key(link) != COHORT_CRYPTER
    ]


def select_helper_package(
    protected_packages,
    supported_url_patterns,
    supported_mirrors=None,
    cooldown_service=None,
    excluded_package_ids=None,
    enforce_package_contract=False,
    offered_occurrence=None,
    lifecycle_service=None,
):
    """Pick the next protected package to hand out.

    `enforce_package_contract` follows the helper's advertised capability, not
    the cooldown service: a capable helper reports and excludes packages by
    canonical ID, so a non-canonical row it can never name must never be
    selected - in any block mode - or it starves every later package.

    `offered_occurrence` is the live occurrence one leased cohort offer was
    mapped to. It restricts the handout to that package and to that exact stored
    Filecrypt link, so the offered container is first and no second Filecrypt
    URL travels with it; the package is skipped entirely when its offered link
    is not eligible, which lets the caller fall back to ordinary work rather
    than claim an offer it did not hand out.

    Spending a queued probe and handing the package out is one decision: the
    probe is counted by the transaction that spends it, and nothing between
    that transaction and this return may reject the package. Under a cohort
    cooldown only the typed offer may spend one, because an untyped handout can
    only ever produce a version-one report the cohort cannot use.
    """
    excluded_package_ids = normalize_excluded_package_ids(excluded_package_ids)
    crypter_projections = {}

    for package in protected_packages:
        if not isinstance(package, (list, tuple)) or len(package) < 2:
            continue
        package_id = package[0]
        if not isinstance(package_id, str):
            continue
        if (
            offered_occurrence is not None
            and package_id != offered_occurrence.package_id
        ):
            continue
        if enforce_package_contract and not PACKAGE_ID_PATTERN.fullmatch(package_id):
            debug(
                "Skipping protected package outside the package ID contract: "
                f"{_log_safe_package_id(package_id)}"
            )
            continue
        if package_id in excluded_package_ids:
            continue

        try:
            data = json.loads(package[1])
        except (TypeError, json.JSONDecodeError):
            continue
        if not helper_package_is_candidate(data):
            continue

        raw_links = data["links"]
        if offered_occurrence is not None:
            raw_links = _bind_offered_occurrence(raw_links, offered_occurrence)
            if raw_links is None:
                continue
        elif lifecycle_service is not None:
            # No typed offer but lifecycle owns Filecrypt: strip all Filecrypt links
            raw_links = [
                link
                for link in raw_links
                if resolve_protected_crypter_key(link) != COHORT_CRYPTER
            ]
            if not raw_links:
                continue

        # Order links by the category's mirror-whitelist: the whitelist order is
        # the priority ranking. Without an explicit whitelist, fall back to the
        # legacy rapidgator-first default.
        mirror_priority = []
        try:
            category = get_download_category_from_package_id(package[0])
            mirror_priority = get_download_category_mirrors(category, lowercase=True)
        except Exception:
            mirror_priority = []

        if mirror_priority:

            def mirror_rank(link, mirror_priority=mirror_priority):
                if isinstance(link, (list, tuple)) and len(link) > 1 and link[1]:
                    haystack = str(link[1]).lower()
                elif isinstance(link, (list, tuple)) and link:
                    haystack = str(link[0]).lower()
                else:
                    haystack = str(link).lower()
                for index, mirror in enumerate(mirror_priority):
                    if mirror and mirror in haystack:
                        return index
                return len(mirror_priority)

            prioritized_links = sorted(raw_links, key=mirror_rank)
        else:
            rapid = [ln for ln in raw_links if is_rapidgator_link(ln)]
            others = [ln for ln in raw_links if not is_rapidgator_link(ln)]
            prioritized_links = rapid + others

        prioritized_links, supported_links = prioritize_helper_supported_links(
            prioritized_links,
            supported_url_patterns,
            supported_mirrors,
        )
        if (supported_url_patterns or supported_mirrors) and not supported_links:
            continue

        if cooldown_service is None:
            return package_id, data, prioritized_links

        package_defer = cooldown_service.get_package_defer(package_id)
        eligible_supported_links = []
        probe_dependent_links = []
        probe_crypter = None

        for link in supported_links:
            crypter = resolve_protected_crypter_key(link)
            if crypter is None:
                eligible_supported_links.append(link)
                continue

            # Lifecycle-offered Filecrypt link bypasses legacy cooldown projection
            if (
                lifecycle_service is not None
                and crypter == COHORT_CRYPTER
                and offered_occurrence is not None
                and _link_fingerprint(crypter, link) == offered_occurrence.fingerprint
            ):
                eligible_supported_links.append(link)
                continue

            if crypter not in crypter_projections:
                crypter_projections[crypter] = cooldown_service.crypter_projection(
                    crypter
                )
            snapshot, decision = crypter_projections[crypter]
            matching_defer = (
                package_defer
                if package_defer and package_defer["crypter"] == crypter
                else None
            )
            projected_defer = (
                cooldown_service.project_package_defer(
                    matching_defer, snapshot, decision
                )
                if matching_defer
                else None
            )
            probe_requested = bool(matching_defer and matching_defer["probe_requested"])
            link_digest = _link_fingerprint(crypter, link)
            # A hold speaks only for the links a report already tested, so a
            # never tested container of the same package is still eligible.
            held = bool(
                projected_defer
                and projected_defer["active"]
                and package_defer_covers_fingerprint(matching_defer, link_digest)
            )
            blocked = snapshot["state"] == "cooldown" or held
            if probe_requested and _cohort_cooldown(decision):
                # A cohort cooldown is only re-tested by the typed offer that
                # feeds it; an ordinary handout would spend the operator's probe
                # on a report the cohort decision cannot use.
                probe_requested = (
                    offered_occurrence is not None
                    and offered_occurrence.package_id == package_id
                    and offered_occurrence.fingerprint == link_digest
                )

            if blocked and not probe_requested:
                continue
            if blocked and probe_requested:
                probe_crypter = crypter
                probe_dependent_links.append(link)
            eligible_supported_links.append(link)

        if not eligible_supported_links:
            continue
        if probe_crypter and not cooldown_service.consume_probe(
            package_id, probe_crypter
        ):
            eligible_supported_links = [
                link
                for link in eligible_supported_links
                if link not in probe_dependent_links
            ]
            if not eligible_supported_links:
                continue

        if offered_occurrence is not None:
            offered = [
                link
                for link in eligible_supported_links
                if _link_fingerprint(COHORT_CRYPTER, link)
                == offered_occurrence.fingerprint
            ]
            if not offered:
                continue
            eligible_supported_links = offered + [
                link for link in eligible_supported_links if link not in offered
            ]

        unsupported_links = prioritized_links[len(supported_links) :]
        return package_id, data, eligible_supported_links + unsupported_links

    return None


def setup_sponsors_helper_routes(app):
    @contextmanager
    def terminal_operation(data, terminal_state):
        """Yield the admitted version-two context, or None for a legacy body.

        The context is yielded while this operation's lock is held, and the
        caller must decide and apply its whole transition inside the block: an
        answer that was lost on the way to the helper is retried, and two
        retries that arrive together would otherwise both read the same phase
        and both submit, fail or disable the package.
        """
        carries_version = "protocol_version" in data
        carries_operation = "terminal_operation_id" in data
        if not carries_version and not carries_operation:
            yield None
            return
        if type(data.get("protocol_version")) is not int:
            return abort(400, "Invalid terminal protocol version")
        if data["protocol_version"] != 2:
            return abort(400, "Invalid terminal protocol version")

        package_id = data.get("package_id")
        if not isinstance(package_id, str) or not package_id:
            return abort(400, "Missing or invalid 'package_id'")

        operation_id = data.get("terminal_operation_id")
        service = TerminalOperationService(shared_state)
        with ExitStack() as admission:
            try:
                opened = admission.enter_context(
                    service.exclusive(operation_id, package_id, terminal_state)
                )
            except ValueError:
                return abort(400, "Invalid terminal operation identity")
            if opened["outcome"] == CONFLICT:
                return abort(409, "Terminal operation identity conflict")
            if opened["outcome"] == CAPACITY:
                return abort(503, "Terminal operation capacity exhausted")
            if opened["outcome"] == UNREADABLE:
                # The stored record may describe an effect that already
                # happened, so nothing may be decided from it and nothing may
                # replace it; the operator repairs that one identity.
                return abort(503, "Terminal operation state unavailable")
            yield {
                "service": service,
                "operation_id": operation_id,
                "record": opened["record"],
            }

    def filecrypt_inventory(protected_rows=None):
        """The bounded Filecrypt inventory, or None when it cannot be proven.

        Always resolved before a mutation opens, so no transaction callback ever
        enumerates storage, and an inventory this cannot read is passed on as an
        explicit failure that the transition layer refuses to cool on.
        """
        try:
            if protected_rows is None:
                protected_rows = shared_state.get_db("protected").retrieve_all_titles()
            return enumerate_filecrypt_candidates(protected_rows)
        except Exception:
            warn("Filecrypt candidate inventory unavailable; cohort work is suspended")
            return None

    def json_response(body, status):
        if status == 200:
            return body
        return HTTPResponse(
            body=json.dumps(body),
            status=status,
            content_type="application/json",
        )

    def internal_failure(event, error):
        """Fail one request without telling the helper anything about why.

        An exception text can carry a storage path, a package title, or a URL,
        and the helper has no use for any of it. The class is logged on this
        side and the answer is the fixed one.
        """
        warn(f"{event} failed ({type(error).__name__})")
        return abort(500, "Internal server error")

    def scrub_blacklisted_owners(lifecycle_service, protected_rows):
        """Scrub blacklisted Filecrypt fingerprints from all owning packages.

        Packages with alternative links have the blacklisted link removed.
        Packages that would be left with no usable links are terminally failed
        through the existing terminal-operation service.  CAPACITY, UNREADABLE,
        and CONFLICT outcomes leave the last link intact for the next scrub pass.
        The reporting package's absence is idempotent.
        """
        terminal_service = TerminalOperationService(shared_state)
        for fp_val in lifecycle_service.active_blacklisted_fingerprints():
            for pkg_id in lifecycle_service.blacklisted_owners(protected_rows, fp_val):
                try:
                    removal = lifecycle_service.remove_blacklisted_link(pkg_id, fp_val)
                    if removal["package_absent"]:
                        continue
                    if removal["link_removed"]:
                        # Alternatives remain; the package row is already updated.
                        continue
                    if removal["usable_links_remaining"] > 0:
                        # Fingerprint not found in this package (already scrubbed).
                        continue
                    # Package has no usable links after scrub: run terminal failure.
                    op_id = terminal_operation_id(pkg_id)
                    with terminal_service.exclusive(op_id, pkg_id, "failed") as result:
                        outcome = result["outcome"]
                        if outcome in (CAPACITY, UNREADABLE, CONFLICT):
                            # Preserve the link/package for the next scrub pass.
                            continue
                        context = {
                            "service": terminal_service,
                            "operation_id": op_id,
                            "record": result["record"],
                        }
                        _present, package_data = read_protected_package(pkg_id)
                        title = (
                            package_data.get("title", "Unknown")
                            if isinstance(package_data, dict)
                            else "Unknown"
                        )
                        reason = (
                            "Filecrypt URL permanently blacklisted; "
                            "no remaining links available."
                        )
                        confirm_terminal_failure(context, pkg_id, title, reason)
                except HTTPError as _http_err:
                    if _http_err.status_code == 409:
                        # Downstream identity conflict: preserve link, retry next pass.
                        pass
                    else:
                        warn(
                            f"Error scrubbing blacklisted Filecrypt owner "
                            f"{_log_safe_package_id(pkg_id)}: "
                            f"{type(_http_err).__name__}"
                        )
                except Exception as error:
                    warn(
                        f"Error scrubbing blacklisted Filecrypt owner "
                        f"{_log_safe_package_id(pkg_id)}: "
                        f"{type(error).__name__}"
                    )

    def filecrypt_probe_occurrence(inventory, protected_rows):
        """The exact occurrence one queued `Check now` authorizes, or None.

        A probe is granted per package, so the member it can test has to come
        from that package: the lowest package ID carrying a queued Filecrypt
        probe, and inside it the lowest stored link index its hold still covers.
        Read from the rows this request already enumerated, so proving it costs
        no extra storage access.
        """
        if inventory is None:
            return None
        occurrences = {}
        for candidate in inventory.candidates:
            for occurrence in candidate.occurrences:
                occurrences.setdefault(occurrence.package_id, []).append(occurrence)

        queued = []
        for row in protected_rows or ():
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            package_id = row[0]
            if not isinstance(package_id, str):
                continue
            try:
                deferred = decode_package_defer(json.loads(row[1]))
            except (TypeError, ValueError, RecursionError):
                continue
            if (
                deferred
                and deferred["crypter"] == COHORT_CRYPTER
                and deferred["probe_requested"]
            ):
                queued.append((package_id, deferred))

        for package_id, deferred in sorted(queued, key=lambda entry: entry[0]):
            for occurrence in sorted(
                occurrences.get(package_id, ()), key=lambda entry: entry.link_index
            ):
                if package_defer_covers_fingerprint(deferred, occurrence.fingerprint):
                    return occurrence
        return None

    def lease_cohort_offer(cooldown_service, inventory, protected_rows):
        """Advance the decision and lease at most one Filecrypt offer.

        A manual probe is only requested while a cohort cooldown could issue
        one; asking for it in any other state would suppress the ordinary offer
        that state does issue. Returns the leased offer and the occurrence a
        probe named, so the handout can be bound to the package the operator
        actually authorized.
        """
        decision = cooldown_service.crypter_decision(COHORT_CRYPTER)
        probe = (
            filecrypt_probe_occurrence(inventory, protected_rows)
            if _cohort_cooldown(decision)
            else None
        )
        offer = cooldown_service.prepare_offer(
            COHORT_CRYPTER,
            inventory,
            mode=None if probe is None else "probe",
            preferred_fingerprint=None if probe is None else probe.fingerprint,
        )
        return offer, probe

    def cohort_occurrence(inventory, offer, excluded_package_ids, probe=None):
        """Map a leased fingerprint onto its deterministic live occurrence.

        A probe already names the exact occurrence its package authorized. Any
        other offer resolves through the inventory, which is ordered by package
        ID and stored link index, so the first occurrence the helper can still
        be handed is the one this returns.
        """
        if inventory is None or not offer:
            return None
        excluded = normalize_excluded_package_ids(excluded_package_ids)
        if probe is not None:
            if probe.fingerprint != offer["link_fingerprint"]:
                return None
            return None if probe.package_id in excluded else probe
        for candidate in inventory.candidates:
            if candidate.fingerprint != offer["link_fingerprint"]:
                continue
            for occurrence in candidate.occurrences:
                if occurrence.package_id not in excluded:
                    return occurrence
            return None
        return None

    def get_protected_release(package_id):
        try:
            raw_data = shared_state.get_db("protected").retrieve(package_id)
            data = json.loads(raw_data) if raw_data else None
        except Exception as e:
            info(
                f'Error reading protected package "{package_id}" for notification: {e}'
            )
            return None
        return data if isinstance(data, dict) else None

    def extract_failure_reason(data, default_reason=None):
        if not isinstance(data, dict):
            return default_reason

        reason = data.get("reason") or data.get("error")
        if reason:
            return str(reason)
        return default_reason

    def mark_helper_package_failed(package_id, title, reason):
        protected_release = get_protected_release(package_id)
        if protected_release and protected_release.get("title"):
            title = protected_release["title"]
        fail(title, package_id, shared_state, reason=reason)
        try:
            shared_state.get_db("protected").delete(package_id)
        except Exception as e:
            info(
                f'Error deleting protected package "{package_id}" after helper failure: {e}'
            )
        update_release_notification(
            shared_state,
            protected_release or {"title": title},
            NotificationType.FAILED,
            details={"reason": reason},
        )
        return {
            "success": False,
            "failed": True,
            "reason": reason,
        }

    def terminal_response(record):
        terminal_state = record["terminal_state"]
        package_removed = record["package_removed"]
        package_terminal = record["package_terminal"]
        return {
            "success": package_terminal
            and (package_removed or terminal_state == "disabled"),
            "terminal_state": terminal_state,
            "package_removed": package_removed,
            "package_terminal": package_terminal,
            "package_id": record["package_id"],
        }

    def unconfirmed_terminal_response(context):
        return terminal_response(
            {
                **context["record"],
                "package_removed": False,
                "package_terminal": False,
            }
        )

    def mark_terminal_submitted(context):
        result = context["service"].mark_submitted(
            context["operation_id"],
            context["record"]["package_id"],
            context["record"]["terminal_state"],
        )
        if result["outcome"] == CONFLICT:
            return abort(409, "Terminal operation identity conflict")
        context["record"] = result["record"]

    def begin_terminal_effect(context):
        """Persist that this operation is about to touch the world.

        Written before the side effect and before any external call, so a crash
        can no longer look like a crash that never started: an operation still
        marked as never started may - and must - apply its transition, whatever
        an earlier life of the same release left lying around.
        """
        result = context["service"].mark_effect_attempting(
            context["operation_id"],
            context["record"]["package_id"],
            context["record"]["terminal_state"],
        )
        if result["outcome"] == CONFLICT:
            return abort(409, "Terminal operation identity conflict")
        context["record"] = result["record"]

    def complete_terminal(context, *, package_removed, package_terminal):
        result = context["service"].mark_complete(
            context["operation_id"],
            context["record"]["package_id"],
            context["record"]["terminal_state"],
            package_removed=package_removed,
            package_terminal=package_terminal,
        )
        if result["outcome"] == CONFLICT:
            return abort(409, "Terminal operation identity conflict")
        context["record"] = result["record"]
        return terminal_response(context["record"])

    def record_helper_terminal_failure(context, package_id, title, reason):
        """Persist this operation's failure once, resuming an interrupted one.

        The history row is the commit point of the whole failure: it is written
        with this operation's evidence and with both counters in one
        transaction, and nothing a helper or an operator can observe happens
        before it lands. A write that never landed therefore leaves the
        operation exactly where it was, with no counter, no notification and a
        protected package the next attempt can still act on.

        Failed history only answers for the operation that wrote it: package
        IDs are derived from the release, so a package that failed once and was
        added again still carries that row while it is protected, and the
        automatic download path and the legacy fail route write one too. An
        operation that never began its attempt therefore reads no history at
        all, and one that did may only recognize its own marker.
        """
        record = context["record"]
        evidence = operation_evidence(record)
        if not record["failure_persisted"]:
            persisted = False
            if record["effect_state"] == EFFECT_ATTEMPTING:
                persisted = failed_package_records_operation(
                    shared_state, package_id, evidence
                )
                if persisted is None:
                    return False
            if persisted:
                # An interrupted attempt of an older shape already committed
                # the row. Nothing that followed it left a trace, so neither
                # its count nor its notification may be invented here: the
                # notification is closed unsent rather than risking a second
                # one the operator would see.
                result = context["service"].mark_failure_persisted(
                    context["operation_id"],
                    package_id,
                    record["terminal_state"],
                )
                if result["outcome"] == CONFLICT:
                    return abort(409, "Terminal operation identity conflict")
                context["record"] = result["record"]
                if not context["record"]["failure_persisted"]:
                    return False
                return mark_terminal_notification(context, NOTIFICATION_RECORDED)

            begin_terminal_effect(context)
            protected_release = get_protected_release(package_id)
            if protected_release and protected_release.get("title"):
                title = protected_release["title"]
            try:
                result = commit_terminal_failure(
                    context["service"],
                    context["operation_id"],
                    package_id,
                    record["terminal_state"],
                    title,
                    reason,
                    evidence,
                )
            except Exception as e:
                info(
                    "Error recording the terminal failure of "
                    f'"{_log_safe_package_id(package_id)}": {e}'
                )
                return False
            if result["outcome"] == CONFLICT:
                return abort(409, "Terminal operation identity conflict")
            context["record"] = result["record"]
            if not context["record"]["failure_persisted"]:
                return False
        return notify_terminal_failure(context, package_id, title, reason)

    def notify_terminal_failure(context, package_id, title, reason):
        """Tell the operator about this failure at most once, ever.

        A message that was already dispatched cannot be recalled and cannot be
        proven to have arrived, so the pending phase is persisted before it is
        sent: a retry that finds one never sends a second, and a retry that
        finds none knows the first was never dispatched at all.
        """
        phase = context["record"]["notification_state"]
        if phase == NOTIFICATION_RECORDED:
            return True
        if phase == NOTIFICATION_NOT_STARTED:
            release = get_protected_release(package_id) or {"title": title}
            if not mark_terminal_notification(context, NOTIFICATION_ATTEMPTING):
                return False
            update_release_notification(
                shared_state,
                release,
                NotificationType.FAILED,
                details={"reason": reason},
            )
        return mark_terminal_notification(context, NOTIFICATION_RECORDED)

    def mark_terminal_notification(context, phase):
        result = context["service"].mark_notification(
            context["operation_id"],
            context["record"]["package_id"],
            context["record"]["terminal_state"],
            phase,
        )
        if result["outcome"] == CONFLICT:
            return abort(409, "Terminal operation identity conflict")
        context["record"] = result["record"]
        return True

    def unprovable_legacy_terminal(context, package_id):
        """Answer a migrated record that can account for nothing it applied.

        The row shape it was written in marked none of its artifacts, so a
        failed row, a JDownloader package or a disabled flag found now proves
        nothing about it - and neither does their absence. The only thing still
        worth reading is whether the protected package is gone, which is
        terminal on its own and invents no outcome. Anything else waits for the
        operator, or for retention to allow a fresh lifecycle.
        """
        present, _package_data = read_protected_package(package_id)
        if present is False:
            return complete_terminal(
                context, package_removed=True, package_terminal=True
            )
        return unconfirmed_terminal_response(context)

    def projected_final_download_links(download_links, package_id):
        return project_final_download_urls(download_links, package_id)[2]["urls"]

    def confirm_terminal_download(
        context, title, package_id, download_links, password, notification
    ):
        if context["record"]["state"] == "complete":
            return terminal_response(context["record"])
        if context["record"]["legacy_unproven"]:
            return unprovable_legacy_terminal(context, package_id)

        evidence = operation_evidence(context["record"])
        final_links = None
        # None until this request knows whether the operation ends in a failure.
        # An operation that never began its attempt has provably changed
        # nothing, so no artifact of an earlier lifecycle is an answer for it.
        failed_now = None
        if context["record"]["state"] == "prepared":
            failed_now = False
            submit = True
            if context["record"]["effect_state"] == EFFECT_ATTEMPTING:
                # The interrupted attempt could have ended in either side
                # effect, so both are asked - each only about what this
                # operation named.
                failed_now = context["record"]["failure_persisted"]
                if not failed_now:
                    failed_now = failed_package_records_operation(
                        shared_state, package_id, evidence
                    )
                    if failed_now is None:
                        return unconfirmed_terminal_response(context)
                if failed_now:
                    submit = False
                else:
                    already_submitted = jdownloader_holds_operation(
                        shared_state, package_id, evidence
                    )
                    if already_submitted is None:
                        return unconfirmed_terminal_response(context)
                    submit = not already_submitted

            if not submit:
                # An interrupted attempt of this operation already wrote the
                # failure; the reason it recorded is what finishes telling the
                # operator about it, so a lost answer costs no notification.
                recorded_reason = (
                    failed_package_reason(shared_state, package_id, evidence)
                    if failed_now
                    else None
                )
                if recorded_reason is not None and not record_helper_terminal_failure(
                    context, package_id, title, recorded_reason
                ):
                    return unconfirmed_terminal_response(context)
                mark_terminal_submitted(context)
            elif not isinstance(download_links, list) or not download_links:
                reason = (
                    "SponsorsHelper returned an invalid download payload."
                    if not isinstance(download_links, list)
                    else "SponsorsHelper returned no final download links."
                )
                if not record_helper_terminal_failure(
                    context, package_id, title, reason
                ):
                    return unconfirmed_terminal_response(context)
                failed_now = True
                mark_terminal_submitted(context)
            else:
                begin_terminal_effect(context)
                submit_result = submit_final_download_urls(
                    shared_state,
                    download_links,
                    title,
                    password,
                    package_id,
                    remove_protected=True,
                    notification_details=notification,
                    phase=SUBMIT_PHASE_SUBMIT,
                    terminal_operation=evidence,
                )
                if submit_result["success"]:
                    final_links = submit_result["links"]
                    mark_terminal_submitted(context)
                elif submit_result.get("mirror_rejected"):
                    if not record_helper_terminal_failure(
                        context, package_id, title, submit_result["reason"]
                    ):
                        return unconfirmed_terminal_response(context)
                    failed_now = True
                    mark_terminal_submitted(context)
                else:
                    return unconfirmed_terminal_response(context)

        if failed_now is None:
            # Resumed after the submitted phase: the durable marker, and then
            # history, are the only records left of how that phase ended.
            failed_now = context["record"]["failure_persisted"]
            if not failed_now:
                failed_now = failed_package_records_operation(
                    shared_state, package_id, evidence
                )
                if failed_now is None:
                    return unconfirmed_terminal_response(context)
        removal = finalize_protected_removal(
            shared_state,
            package_id,
            notification,
            notify_solved=not failed_now,
        )
        if not removal["package_removed"]:
            return unconfirmed_terminal_response(context)

        if not failed_now and removal["removed_now"]:
            if final_links is None:
                final_links = projected_final_download_links(download_links, package_id)
            StatsHelper(shared_state).increment_package_with_links(final_links)
            StatsHelper(shared_state).increment_captcha_decryptions_automatic()
        return complete_terminal(context, package_removed=True, package_terminal=True)

    def confirm_terminal_failure(context, package_id, title, reason):
        if context["record"]["state"] == "complete":
            return terminal_response(context["record"])
        if context["record"]["legacy_unproven"]:
            return unprovable_legacy_terminal(context, package_id)

        if context["record"]["state"] == "prepared":
            if not record_helper_terminal_failure(context, package_id, title, reason):
                return unconfirmed_terminal_response(context)
            mark_terminal_submitted(context)

        removal = finalize_protected_removal(
            shared_state, package_id, notify_solved=False
        )
        if not removal["package_removed"]:
            return unconfirmed_terminal_response(context)
        return complete_terminal(context, package_removed=True, package_terminal=True)

    def read_protected_package(package_id):
        try:
            raw = shared_state.get_db("protected").retrieve(package_id)
        except Exception:
            return None, None
        if raw is None:
            return False, None
        try:
            package_data = json.loads(raw)
        except (TypeError, ValueError, RecursionError):
            package_data = None
        return True, package_data

    def disabled_by_operation(package_data, evidence):
        """Whether the package carries the disable this operation applied.

        A package can already be disabled by an earlier life of the release or
        by hand, which says nothing about the operation asking, so the marker
        written with the flag is what answers.
        """
        return (
            isinstance(package_data, dict)
            and package_data.get("disabled") is True
            and package_data.get(TERMINAL_OPERATION_MARKER) == evidence
        )

    def confirm_terminal_disable(context, package_id, reason):
        if context["record"]["state"] == "complete":
            return terminal_response(context["record"])
        if context["record"]["legacy_unproven"]:
            return unprovable_legacy_terminal(context, package_id)

        if context["record"]["state"] == "prepared":
            evidence = operation_evidence(context["record"])
            present, package_data = read_protected_package(package_id)
            if present is None:
                return unconfirmed_terminal_response(context)
            if present and not disabled_by_operation(package_data, evidence):
                if not isinstance(package_data, dict):
                    return unconfirmed_terminal_response(context)
                begin_terminal_effect(context)
                package_data["disabled"] = True
                package_data[TERMINAL_OPERATION_MARKER] = evidence
                shared_state.get_db("protected").update_store(
                    package_id, json.dumps(package_data)
                )
                StatsHelper(shared_state).increment_failed_decryptions_automatic()
                StatsHelper(shared_state).increment_captcha_decryptions_automatic()
                update_release_notification(
                    shared_state,
                    package_data,
                    NotificationType.DISABLED,
                    details={"reason": reason} if reason else None,
                )
            mark_terminal_submitted(context)

        present, package_data = read_protected_package(package_id)
        if present is None:
            return unconfirmed_terminal_response(context)
        if present is False:
            return complete_terminal(
                context, package_removed=True, package_terminal=True
            )
        if isinstance(package_data, dict) and package_data.get("disabled") is True:
            return complete_terminal(
                context, package_removed=False, package_terminal=True
            )
        return unconfirmed_terminal_response(context)

    @app.get("/sponsors_helper/api/ping/")
    @require_api_key
    def ping_api():
        """Health check endpoint for SponsorsHelper to verify connectivity."""
        return "pong"

    @app.get("/sponsors_helper/api/credentials/<hostname>/")
    @require_api_key
    @require_helper_active
    def credentials_api(hostname):
        section = hostname.upper()
        if section not in ["AL", "DD", "DL", "NX", "JUNKIES"]:
            return abort(404, f"No credentials for {hostname}")

        config = Config(section)
        user = config.get("user")
        password = config.get("password")

        if not user or not password:
            return abort(404, f"Credentials not set for {hostname}")

        return {"user": user, "pass": password}

    @app.get("/sponsors_helper/api/mirrors/<package_id>/")
    @require_api_key
    @require_helper_active
    def mirrors_api(package_id):
        category = get_download_category_from_package_id(package_id)
        mirrors = get_download_category_mirrors(category)
        return {"mirrors": mirrors}

    @app.post("/sponsors_helper/api/defer/")
    @require_api_key
    def defer_api():
        data = request.json
        if not isinstance(data, dict):
            return abort(400, "Missing or invalid JSON object")
        required_fields = {
            "package_id",
            "crypter",
            "reason_code",
            "link_fingerprint",
        }
        if not required_fields.issubset(data):
            return abort(400, "Missing defer report fields")

        # A report is a cohort report only when it names a strictly valid offer
        # identity. Cohort intent it cannot spell is rejected outright: falling
        # back to the state-changing version-one route would let a typo record
        # an observation and write a hold the helper never asked for. A body
        # naming no offer at all is ordinary version-one work.
        kind, cohort_report = classify_blocked_report(data)
        if kind == MALFORMED_REPORT:
            return abort(400, "Invalid Filecrypt cohort offer identity")
        if kind == LIFECYCLE_REPORT:
            if not crypter_blocks_deferred(shared_state):
                return lifecycle_stale_blocked_response()
            try:
                protected_rows = shared_state.get_db("protected").retrieve_all_titles()
                lifecycle = FilecryptLifecycleService(shared_state)
                result = lifecycle.record_blocked(cohort_report, protected_rows)
            except HTTPResponse:
                raise
            except Exception as error:
                return internal_failure("A Filecrypt lifecycle block report", error)
            if result is None:
                return lifecycle_stale_blocked_response()
            if not result.get("terminal_required"):
                return render_defer_response(result)
            # Terminal blacklist workflow
            fp = result["fingerprint"]
            pkg_id = result["package_id"]
            top_id = result["terminal_operation_id"]
            offer_id = result["offer_id"]
            try:
                protected_release = get_protected_release(pkg_id)
                title = (
                    protected_release.get("title", "Unknown")
                    if protected_release
                    else "Unknown"
                )
                with terminal_operation(data, "failed") as context:
                    if context is None:
                        return abort(500, "Internal server error")
                    reason = (
                        "Filecrypt URL remained unavailable after its 24-hour recheck."
                    )
                    terminal_result = confirm_terminal_failure(
                        context, pkg_id, title, reason
                    )
                    if not terminal_result.get("package_terminal"):
                        return abort(500, "Internal server error")
                    blacklist = lifecycle.confirm_blacklist(fp, offer_id, top_id)
                    if blacklist is None:
                        return abort(500, "Internal server error")
                    return render_defer_response(blacklist)
            except HTTPResponse:
                raise
            except Exception as error:
                return internal_failure(
                    "A Filecrypt lifecycle terminal blacklist", error
                )
        if kind == COHORT_REPORT:
            if not crypter_blocks_deferred(shared_state):
                return render_defer_response(bypass_decision())
            service = CrypterCooldownService(shared_state)
            inventory = filecrypt_inventory()
            try:
                decision = service.record_cohort_blocked(
                    cohort_report["crypter"],
                    cohort_report["package_id"],
                    cohort_report["link_fingerprint"],
                    cohort_report["sweep_id"],
                    cohort_report["offer_id"],
                    cohort_report["reason_code"],
                    inventory,
                )
            except ValueError as error:
                return abort(400, str(error))
            except HTTPResponse:
                raise
            except Exception as error:
                return internal_failure("A Filecrypt cohort block report", error)
            return render_defer_response(decision)

        if not crypter_blocks_deferred(shared_state):
            return {
                "success": True,
                "instruction": "legacy_failure",
                "state": "available",
                "hold_type": "none",
                "evidence_count": 0,
                "retry_after_epoch": 0,
            }

        package_id = data.get("package_id")
        crypter = data.get("crypter")
        reason_code = data.get("reason_code")
        link_fingerprint = data.get("link_fingerprint")
        service = CrypterCooldownService(shared_state)

        try:
            # One transaction decides the observation, the version-two
            # precedence, and the package hold together, so a cohort decision
            # opening meanwhile can never be answered as a version-one hold.
            answer = service.record_version_one_report(
                crypter,
                package_id,
                link_fingerprint,
                reason_code,
            )
            if answer["package_missing"]:
                return abort(404, "Protected package not found")
        except ValueError as error:
            return abort(400, str(error))
        except HTTPResponse:
            raise
        except Exception as error:
            return internal_failure("A version-one linkcrypter block report", error)

        return {
            "success": True,
            "instruction": answer["instruction"],
            "state": answer["state"],
            "evidence_count": answer["evidence_count"],
            "retry_after_epoch": answer["retry_after_epoch"],
            "hold_type": answer["hold_type"],
        }

    @app.post("/sponsors_helper/api/crypter-access/")
    @require_api_key
    def crypter_access_api():
        data = request.json
        if not isinstance(data, dict):
            return abort(400, "Missing or invalid JSON object")
        if not {"package_id", "crypter", "access"}.issubset(data):
            return abort(400, "Missing linkcrypter access report fields")

        kind, cohort_report = classify_access_report(data)
        if kind == MALFORMED_REPORT:
            return abort(400, "Invalid Filecrypt cohort offer identity")
        if kind == LIFECYCLE_REPORT:
            offer_id = cohort_report["offer_id"]
            if not crypter_blocks_deferred(shared_state):
                return json_response(*lifecycle_stale_access_response(offer_id))
            try:
                protected_rows = shared_state.get_db("protected").retrieve_all_titles()
                lifecycle = FilecryptLifecycleService(shared_state)
                result = lifecycle.record_access(cohort_report, protected_rows)
            except HTTPResponse:
                raise
            except Exception as error:
                return internal_failure("A Filecrypt lifecycle access report", error)
            if result is None:
                return json_response(*lifecycle_stale_access_response(offer_id))
            return json_response(*render_access_response(result, offer_id=offer_id))
        if kind == COHORT_REPORT:
            offer_id = cohort_report["offer_id"]
            if not crypter_blocks_deferred(shared_state):
                # A pure bypass still has to answer, and the only answer that
                # implies nothing about a decision is the stale one.
                return json_response(
                    *render_access_response(bypass_decision(), offer_id=offer_id)
                )
            service = CrypterCooldownService(shared_state)
            inventory = filecrypt_inventory()
            try:
                decision = service.record_cohort_access(
                    cohort_report["crypter"],
                    cohort_report["package_id"],
                    cohort_report["link_fingerprint"],
                    cohort_report["sweep_id"],
                    offer_id,
                    cohort_report["access"],
                    inventory,
                )
            except ValueError as error:
                return abort(400, str(error))
            except HTTPResponse:
                raise
            except Exception as error:
                return internal_failure("A Filecrypt cohort access report", error)
            return json_response(*render_access_response(decision, offer_id=offer_id))

        if data["access"] != "clear":
            return abort(400, "Unsupported linkcrypter access value")

        if not crypter_blocks_deferred(shared_state):
            # `fail` mode is a pure bypass on this route too: the compatibility
            # body is returned before any package or linkcrypter state is read.
            return {"success": True, "state": "available", "cleared": True}

        package_id = data["package_id"]
        if not isinstance(package_id, str) or not PACKAGE_ID_PATTERN.fullmatch(
            package_id
        ):
            return abort(400, "Invalid package_id")
        try:
            crypter = normalize_crypter_key(data["crypter"])
        except ValueError as error:
            return abort(400, str(error))

        protected_release = get_protected_release(package_id)
        if protected_release is None:
            return abort(404, "Protected package not found")
        links = protected_release.get("links")
        if not isinstance(links, list) or not any(
            resolve_protected_crypter_key(link) == crypter for link in links
        ):
            return abort(400, "Package does not contain the reported linkcrypter")

        service = CrypterCooldownService(shared_state)
        try:
            # Health is proven first and alone. Every physical release runs
            # inside the service after that commit and is best effort, because
            # the committed window already invalidated those holds logically.
            service.record_legacy_success(crypter, package_id=package_id)
        except ValueError as error:
            return abort(400, str(error))
        except Exception as error:
            return internal_failure("A version-one linkcrypter access report", error)

        return {"success": True, "state": "available", "cleared": True}

    @app.post("/sponsors_helper/api/to_decrypt/")
    @require_api_key
    def to_decrypt_api():
        shared_state.update("helper_active", True)
        shared_state.update("helper_last_seen", int(time.time()))
        try:
            protected = shared_state.get_db("protected").retrieve_all_titles()
            if not protected:
                return abort(404, "No encrypted packages found")

            payload = request.json
            if not isinstance(payload, dict) or "supported_urls" not in payload:
                return abort(400, "Missing supported_urls")

            supported_url_patterns = normalize_helper_supported_urls(
                payload["supported_urls"]
            )
            if not supported_url_patterns:
                return abort(400, "Missing supported_urls")
            supported_mirrors = normalize_helper_supported_mirrors(
                payload.get("supported_mirrors")
            )

            # Issue #350: only hand SponsorsHelper packages where at least one URL
            # matches the helper's advertised support, and move that URL to the front.
            defer_capable = helper_supports_defer(payload)
            cohort_capable = helper_supports_cohort(payload)
            lifecycle_capable = helper_supports_lifecycle(payload)
            # Legacy block mode selects exactly like an incapable helper: no
            # cooldown service is built, so no hold can gate this handout. The
            # package ID contract still applies, because it is what makes the
            # helper's exclusions expressible.
            cooldown_service = (
                CrypterCooldownService(shared_state)
                if defer_capable and crypter_blocks_deferred(shared_state)
                else None
            )
            excluded_package_ids = payload.get("excluded_package_ids")
            offer = None
            occurrence = None
            lifecycle_service = None

            if lifecycle_capable and cooldown_service is not None:
                lifecycle_service = FilecryptLifecycleService(shared_state)
                migration = lifecycle_service.migrate_legacy(protected_rows=protected)
                if migration["status"] in ("unavailable", "conflict"):
                    return HTTPResponse(
                        status=503,
                        body="Filecrypt lifecycle migration unavailable",
                    )
                scrub_blacklisted_owners(lifecycle_service, protected)
                protected = shared_state.get_db("protected").retrieve_all_titles()
                preferred_fp = None
                probe_occurrence = filecrypt_probe_occurrence(
                    enumerate_filecrypt_lifecycle_candidates(protected), protected
                )
                if probe_occurrence is not None:
                    preferred_fp = probe_occurrence.fingerprint
                offer = lifecycle_service.prepare_offer(
                    protected,
                    excluded_package_ids=normalize_excluded_package_ids(
                        excluded_package_ids
                    ),
                    preferred_fingerprint=preferred_fp,
                    probe_package_id=(
                        probe_occurrence.package_id
                        if probe_occurrence is not None
                        else None
                    ),
                )
                occurrence = offer.get("occurrence") if offer else None
            elif cohort_capable and cooldown_service is not None:
                inventory = filecrypt_inventory(protected)
                offer, probe = lease_cohort_offer(
                    cooldown_service, inventory, protected
                )
                occurrence = cohort_occurrence(
                    inventory, offer, excluded_package_ids, probe
                )

            if defer_capable:
                selected_package = select_helper_package(
                    protected,
                    supported_url_patterns,
                    supported_mirrors,
                    cooldown_service=cooldown_service,
                    excluded_package_ids=excluded_package_ids,
                    enforce_package_contract=True,
                    offered_occurrence=occurrence,
                    lifecycle_service=lifecycle_service,
                )
                if selected_package is None and occurrence is not None:
                    # The offered occurrence is not handable right now, so this
                    # request falls back to ordinary work and the unanswered
                    # lease simply expires.
                    occurrence = None
                    offer = None
                    selected_package = select_helper_package(
                        protected,
                        supported_url_patterns,
                        supported_mirrors,
                        cooldown_service=cooldown_service,
                        excluded_package_ids=excluded_package_ids,
                        enforce_package_contract=True,
                        lifecycle_service=lifecycle_service,
                    )
            else:
                selected_package = select_helper_package(
                    protected, supported_url_patterns, supported_mirrors
                )

            if not selected_package:
                return abort(404, "No valid packages found")

            package_id, data, prioritized_links = selected_package
            title = data["title"]
            mirror = data.get("mirror")
            if mirror in (None, "None") and prioritized_links:
                first_link = prioritized_links[0]
                if isinstance(first_link, (list, tuple)) and len(first_link) > 1:
                    mirror = first_link[1]
            mirror = None if mirror == "None" else mirror
            password = data["password"]

            to_decrypt = {
                "name": title,
                "id": package_id,
                "url": prioritized_links,
                "mirror": mirror,
                "password": password,
                "max_attempts": 3,
            }
            if cohort_capable or lifecycle_capable:
                to_decrypt["terminal_operation_id"] = terminal_operation_id(package_id)
                crypter_offer = render_crypter_offer(offer, occurrence)
                if crypter_offer is not None:
                    to_decrypt["crypter_offer"] = crypter_offer
            return {"to_decrypt": to_decrypt}
        except HTTPResponse:
            raise
        except Exception as e:
            return internal_failure("A SponsorsHelper handout", e)

    @app.post("/sponsors_helper/api/download/")
    @require_api_key
    @require_helper_active
    def download_api():
        terminal = None
        try:
            data = request.json or {}
            if not isinstance(data, dict):
                return abort(400, "Missing or invalid JSON object")
            title = data.get("name")
            package_id = data.get("package_id")
            download_links = data.get("urls")
            password = data.get("password")
            notification = data.get("notification")

            if not isinstance(notification, dict):
                return abort(400, "Missing or invalid 'notification' object")
            if not isinstance(notification.get("solvers"), list):
                return abort(400, "Missing or invalid 'notification.solvers' list")
            if not package_id:
                return abort(400, "Missing or invalid 'package_id'")
            with terminal_operation(data, "downloaded") as terminal:
                if not title:
                    title = "Unknown"
                if terminal is not None:
                    return confirm_terminal_download(
                        terminal,
                        title,
                        package_id,
                        download_links,
                        password,
                        notification,
                    )
            if not isinstance(download_links, list):
                StatsHelper(shared_state).increment_failed_decryptions_automatic()
                return mark_helper_package_failed(
                    package_id,
                    title,
                    "SponsorsHelper returned an invalid download payload.",
                )

            info(
                f"Received <green>{len(download_links)}</green> download links for <y>{title}</y>"
            )

            if download_links:
                submit_result = submit_final_download_urls(
                    shared_state,
                    download_links,
                    title,
                    password,
                    package_id,
                    remove_protected=True,
                    notification_details=notification,
                )
                if submit_result["success"]:
                    final_links = submit_result["links"]
                    StatsHelper(shared_state).increment_package_with_links(final_links)
                    StatsHelper(shared_state).increment_captcha_decryptions_automatic()

                    log_msg = f"Download successfully started for <y>{title}</y>"
                    providers = notification.get("solvers")
                    used_providers = []
                    if isinstance(providers, list) and providers:
                        for provider in providers:
                            if not isinstance(provider, dict):
                                continue
                            provider_name = provider.get("name")
                            if provider_name:
                                used_providers.append(str(provider_name))
                    if used_providers:
                        unique_providers = sorted(set(used_providers))
                        log_msg += f" | Providers: {', '.join(unique_providers)}"
                    if notification.get("duration_seconds") is not None:
                        log_msg += (
                            f" | Duration: {notification.get('duration_seconds')}s"
                        )
                    info(log_msg)
                    return f"Downloaded {len(final_links)} download links for {title}"
                elif submit_result.get("persisted_failure"):
                    StatsHelper(shared_state).increment_failed_decryptions_automatic()
                    return {
                        "success": False,
                        "failed": True,
                        "reason": submit_result["reason"],
                    }
                else:
                    info(f"Download failed for <y>{title}</y>")
            else:
                StatsHelper(shared_state).increment_failed_decryptions_automatic()
                return mark_helper_package_failed(
                    package_id,
                    title,
                    "SponsorsHelper returned no final download links.",
                )

        except HTTPResponse:
            raise
        except Exception as e:
            info(f"Error decrypting: {e}")
            if terminal is not None:
                return abort(500, "Failed")

        StatsHelper(shared_state).increment_failed_decryptions_automatic()
        return abort(500, "Failed")

    @app.post("/sponsors_helper/api/disable/")
    @require_api_key
    @require_helper_active
    def disable_api():
        terminal = None
        try:
            data = request.json or {}
            if not isinstance(data, dict):
                return {"error": "Missing or invalid JSON object"}, 400
            package_id = data.get("package_id")
            reason = extract_failure_reason(data)

            if not package_id:
                return {"error": "Missing package_id"}, 400

            with terminal_operation(data, "disabled") as terminal:
                if terminal is not None:
                    return confirm_terminal_disable(terminal, package_id, reason)
            StatsHelper(shared_state).increment_failed_decryptions_automatic()

            blob = shared_state.get_db("protected").retrieve(package_id)
            package_data = json.loads(blob)
            title = package_data.get("title")

            package_data["disabled"] = True
            shared_state.get_db("protected").update_store(
                package_id, json.dumps(package_data)
            )
            info(f"Disabled package {title}")

            StatsHelper(shared_state).increment_captcha_decryptions_automatic()

            update_release_notification(
                shared_state,
                package_data,
                NotificationType.DISABLED,
                details={"reason": reason} if reason else None,
            )
            shared_state.get_db("protected").update_store(
                package_id, json.dumps(package_data)
            )

            return f"Package <y>{title}</y> disabled"

        except HTTPResponse:
            raise
        except Exception as e:
            info(f"Error handling disable: {e}")
            if terminal is not None:
                return abort(500, "Failed")
            return {"error": str(e)}, 500

    @app.delete("/sponsors_helper/api/fail/")
    @require_api_key
    @require_helper_active
    def fail_api():
        terminal = None
        try:
            data = request.json or {}
            if not isinstance(data, dict):
                return abort(400, "Missing or invalid JSON object")
            with terminal_operation(data, "failed") as terminal:
                package_id = data.get("package_id")
                # SponsorsHelper might send 'name' or 'title'
                title = data.get("name") or data.get("title")
                reason = extract_failure_reason(
                    data,
                    default_reason="Too many failed attempts by SponsorsHelper",
                )
                if terminal is not None:
                    return confirm_terminal_failure(
                        terminal, package_id, title or "Unknown", reason
                    )

            StatsHelper(shared_state).increment_failed_decryptions_automatic()

            # 1. Try to find package in Protected DB if ID is missing but Title exists
            if not package_id and title:
                try:
                    protected_packages = shared_state.get_db(
                        "protected"
                    ).retrieve_all_titles()
                    for pkg in protected_packages:
                        # pkg is (id, json_str)
                        try:
                            pkg_data = json.loads(pkg[1])
                            if pkg_data.get("title") == title:
                                package_id = pkg[0]
                                info(
                                    f"Found package ID <y>{package_id}</y> for title <y>{title}</y>"
                                )
                                break
                        except Exception:
                            pass
                except Exception as e:
                    info(f"Error searching protected DB by title: {e}")

            # 2. If we have an ID, try to get canonical title from DB (if not provided or to verify)
            if package_id:
                protected_release = get_protected_release(package_id)
                try:
                    db_entry = shared_state.get_db("protected").retrieve(package_id)
                    if db_entry:
                        db_data = json.loads(db_entry)
                        # Prefer DB title if available
                        if db_data.get("title"):
                            title = db_data.get("title")
                except Exception:
                    # If retrieval fails, we stick with the title we have (or "Unknown")
                    pass

            if not title:
                title = "Unknown"

            if package_id:
                info(
                    f"Marking package <y>{title}</y> with ID <y>{package_id}</y> as failed"
                )
                failed = fail(
                    title,
                    package_id,
                    shared_state,
                    reason=reason,
                )

                # Always try to delete from protected, even if fail() returns False
                try:
                    shared_state.get_db("protected").delete(package_id)
                except Exception as e:
                    info(f"Error deleting from protected DB: {e}")

                # Verify deletion
                try:
                    if shared_state.get_db("protected").retrieve(package_id):
                        info(
                            f"Verification failed: Package {package_id} still exists in protected DB"
                        )
                except Exception:
                    pass

                if failed:
                    update_release_notification(
                        shared_state,
                        protected_release or {"title": title},
                        NotificationType.FAILED,
                        details={"reason": reason},
                    )
                    return f'Package <y>{title}</y> with ID <y>{package_id}</y> marked as failed!"'
                else:
                    return f"Package <y>{title}</y> processed."
            else:
                return abort(400, "Missing package_id")
        except HTTPResponse:
            raise
        except Exception as e:
            info(f"Error moving to failed: {e}")
            if terminal is not None:
                return abort(500, "Failed")

        return abort(500, "Failed")

    @app.put("/sponsors_helper/api/set_sponsor_status/")
    @require_api_key
    def set_sponsor_status_api():
        try:
            data = request.body.read().decode("utf-8")
            payload = json.loads(data)
            if payload["activate"]:
                shared_state.update("helper_active", True)
                shared_state.update("helper_last_seen", int(time.time()))
                info("Sponsor status activated successfully")
                return "Sponsor status activated successfully!"
        except:
            pass
        return abort(500, "Failed")
