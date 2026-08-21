# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import hashlib
import json

from quasarr.constants import (
    AUTO_DECRYPT_PATTERNS,
    CLIENT_DOWNLOAD_CATEGORY_FALLBACK_MAP,
    PROTECTED_PATTERNS,
)
from quasarr.downloads.linkcrypters.hide import decrypt_links_if_hide
from quasarr.downloads.mirror_filters import filter_final_download_urls
from quasarr.downloads.packages import get_packages
from quasarr.downloads.sources import get_sources as get_download_sources
from quasarr.providers.hostname_issues import clear_hostname_issue, mark_hostname_issue
from quasarr.providers.log import error, info, warn
from quasarr.providers.notifications import (
    send_notification,
    send_tracked_notification,
    update_release_notification,
)
from quasarr.providers.notifications.helpers.notification_types import NotificationType
from quasarr.providers.package_origin import record_package_origin
from quasarr.providers.statistics import StatsHelper
from quasarr.providers.terminal_operations import (
    TERMINAL_OPERATION_MARKER,
    submission_comment,
)
from quasarr.providers.utils import (
    download_package,
    extract_client_type,
    filter_offline_links,
    normalize_download_title,
)
from quasarr.storage.categories import (
    download_category_exists,
    get_download_category_from_package_id,
    get_download_category_mirrors,
)

_PROTECTED_MIRROR_KEYS = frozenset({"junkies"})

# The whole funnel, or only the half that ends at the JDownloader submission.
# Splitting it lets a caller that has to confirm a terminal package state put a
# durable marker between the one irreversible step and the cleanup after it.
SUBMIT_PHASE_ALL = "all"
SUBMIT_PHASE_SUBMIT = "submit"

# The rows one terminal failure commits together. They are named here because
# this module owns the shape of a failed row and the meaning of the counters;
# the operation service owns the transaction that writes them.
FAILED_TABLE = "failed"
STATISTICS_TABLE = "statistics"
TERMINAL_FAILURE_COUNTERS = ("failed_downloads", "failed_decryptions_automatic")
# Told apart from "there is no row": a store that cannot be read proves nothing
# either way and may never authorize a second terminal side effect.
_UNREADABLE = object()

# =============================================================================
# DETERMINISTIC PACKAGE ID GENERATION
# =============================================================================


def generate_deterministic_package_id(
    title, source_key, client_type, download_category
):
    """
    Generate a deterministic package ID from title, source, and client type.

    The same combination of (title, source_key, client_type) will ALWAYS produce
    the same package_id, allowing clients to reliably blocklist erroneous releases.

    Args:
        title: Release title (e.g., "Movie.Name.2024.1080p.BluRay")
        source_key: Source identifier/hostname shorthand
        client_type: Client type without version (e.g., "radarr", "sonarr", "magazarr")
        download_category: Optional download category override
            (e.g., "movies", "tv", "docs")

    Returns:
        Deterministic package ID in format: Quasarr_{download_category}_{hash32}
    """
    # Normalize inputs for consistency
    normalized_title = title.strip()
    normalized_source = source_key.lower().strip() if source_key else "unknown"
    normalized_client = client_type.lower().strip() if client_type else "unknown"

    # Determine download category
    if download_category and download_category_exists(download_category):
        final_download_category = download_category
    else:
        # Fallback to client type mapping
        final_download_category = CLIENT_DOWNLOAD_CATEGORY_FALLBACK_MAP.get(
            normalized_client, "tv"
        )

    # Create deterministic hash from combination using SHA256
    hash_input = f"{normalized_title}|{normalized_source}|{normalized_client}"
    hash_bytes = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()

    # Use first 32 characters for good collision resistance (128-bit)
    return f"Quasarr_{final_download_category}_{hash_bytes[:32]}"


# =============================================================================
# LINK CLASSIFICATION
# =============================================================================


def detect_crypter(url):
    """Returns (crypter_name, 'auto'|'protected') or (None, None)."""
    for name, pattern in AUTO_DECRYPT_PATTERNS.items():
        if pattern.search(url):
            return name, "auto"
    for name, pattern in PROTECTED_PATTERNS.items():
        if pattern.search(url):
            return name, "protected"
    return None, None


def protected_crypter_keys():
    return frozenset(PROTECTED_PATTERNS) | _PROTECTED_MIRROR_KEYS


def resolve_protected_crypter_key(link):
    if not isinstance(link, (list, tuple)) or not link:
        return None

    url = link[0]
    if not isinstance(url, str):
        return None

    mirror = link[1] if len(link) > 1 else ""
    if isinstance(mirror, str) and mirror.lower() in _PROTECTED_MIRROR_KEYS:
        return mirror.lower()

    crypter, crypter_type = detect_crypter(url)
    return crypter if crypter_type == "protected" else None


def _drop_filecrypt_if_disabled(shared_state, classified, title):
    """Drop filecrypt links from the protected bucket when the kill switch is off."""
    if shared_state.values.get("filecrypt_enabled", True):
        return classified

    filecrypt_re = PROTECTED_PATTERNS["filecrypt"]
    kept, dropped = [], 0
    for link in classified["protected"]:
        if filecrypt_re.search(link[0]):
            dropped += 1
        else:
            kept.append(link)

    if dropped:
        info(
            f"Filecrypt disabled - dropped <r>{dropped}</r> filecrypt link(s) for {title}"
        )
    classified["protected"] = kept
    return classified


def classify_links(links):
    """
    Classify links into direct/auto/protected categories.
    Direct = anything that's not a known crypter or junkies link.
    Mirror names from source are preserved.
    """
    classified = {"direct": [], "auto": [], "protected": []}

    for link in links:
        url = link[0]
        if resolve_protected_crypter_key(link):
            classified["protected"].append(link)
            continue

        crypter, crypter_type = detect_crypter(url)
        if crypter_type == "auto":
            classified["auto"].append(link)
        elif crypter_type == "protected":
            classified["protected"].append(link)
        else:
            # Not a known crypter = direct hoster link
            classified["direct"].append(link)

    return classified


# =============================================================================
# LINK PROCESSING
# =============================================================================


def _persist_failed_package(
    shared_state,
    title,
    package_id,
    reason,
    remove_protected=False,
):
    if remove_protected:
        try:
            shared_state.get_db("protected").delete(package_id)
        except Exception as e:
            info(f'Error removing protected package "{package_id}" before fail: {e}')
    fail(title, package_id, shared_state, reason=reason)
    return {"success": False, "persisted_failure": True, "reason": reason}


def _get_protected_release(shared_state, package_id):
    try:
        raw_data = shared_state.get_db("protected").retrieve(package_id)
        data = json.loads(raw_data) if raw_data else None
    except Exception as e:
        info(f'Error reading protected package "{package_id}" for notification: {e}')
        return None
    return data if isinstance(data, dict) else None


def _delete_protected_package(shared_state, package_id):
    try:
        shared_state.get_db("protected").delete(package_id)
    except Exception as e:
        info(f'Error removing protected package "{package_id}": {e}')


def _format_mirror_token_list(tokens):
    cleaned = [str(token) for token in sorted(tokens) if token]
    return ", ".join(cleaned) if cleaned else "unknown"


def project_final_download_urls(urls, package_id):
    """Apply the package category's final mirror policy without side effects."""
    category = get_download_category_from_package_id(package_id)
    mirrors = get_download_category_mirrors(category, lowercase=True)
    return category, mirrors, filter_final_download_urls(urls, mirrors)


def _protected_package_present(shared_state, package_id):
    """Whether the protected row exists, or None when that cannot be read."""
    try:
        return shared_state.get_db("protected").retrieve(package_id) is not None
    except Exception as e:
        info(f'Error reading protected package "{package_id}": {e}')
        return None


def failed_package_records_operation(shared_state, package_id, terminal_operation):
    """Whether failed history proves THIS operation wrote it, or None if unreadable.

    The mere presence of a row proves nothing: package IDs are derived from the
    release, and the automatic download path, the legacy fail route and every
    earlier life of the same release write one too. Only the operation marker
    the row was stored with answers for the operation asking.
    """
    blob = _failed_package_blob(shared_state, package_id)
    if blob is None or blob is _UNREADABLE:
        return None if blob is _UNREADABLE else False
    return blob.get(TERMINAL_OPERATION_MARKER) == terminal_operation


def failed_package_reason(shared_state, package_id, terminal_operation):
    """The reason THIS operation recorded, or None when it recorded none.

    An interrupted failure has to be able to finish telling the operator what
    it already wrote down, and the row it wrote is the only place that reason
    still exists - the operation record itself stays bounded and secret-free.
    """
    blob = _failed_package_blob(shared_state, package_id)
    if not isinstance(blob, dict):
        return None
    if blob.get(TERMINAL_OPERATION_MARKER) != terminal_operation:
        return None
    reason = blob.get("error")
    return reason if isinstance(reason, str) else None


def _failed_package_blob(shared_state, package_id):
    """The decoded failed row, None when there is none, `_UNREADABLE` on error."""
    try:
        raw = shared_state.get_db(FAILED_TABLE).retrieve(package_id)
    except Exception as e:
        info(f'Error reading failed package "{package_id}": {e}')
        return _UNREADABLE
    if raw is None:
        return None
    try:
        blob = json.loads(raw)
        if isinstance(blob, str):
            blob = json.loads(blob)
    except (TypeError, ValueError, RecursionError):
        return None
    return blob if isinstance(blob, dict) else None


def failed_row_value(title, reason, terminal_operation=None):
    """The exact stored value of one failed-history row.

    Kept as a pure projection so the same bytes can be written by the legacy
    path and inside the transaction that commits a terminal failure with its
    counters, and so nothing but the marker ever changes between the two.
    """
    entry = {"title": title, "error": reason}
    if terminal_operation:
        # Written with the row itself, so a retry of the operation that
        # recorded this failure can recognize its own work.
        entry[TERMINAL_OPERATION_MARKER] = terminal_operation
    return json.dumps(json.dumps(entry))


def commit_terminal_failure(
    service, operation_id, package_id, terminal_state, title, reason, evidence
):
    """Persist one operation's failure, its counters and its marker as one commit.

    Everything a helper can observe about a failure - the history row, the two
    counters, the notification, the removal of the protected package - waits
    behind this, because a failure that was never written down must never be
    reported, counted or cleaned up after.
    """
    return service.record_failure(
        operation_id,
        package_id,
        terminal_state,
        failed_target=(FAILED_TABLE, package_id),
        failed_value=failed_row_value(title, reason, evidence),
        counter_targets=tuple(
            (STATISTICS_TABLE, counter) for counter in TERMINAL_FAILURE_COUNTERS
        ),
    )


def finalize_protected_removal(
    shared_state, package_id, notification_details=None, *, notify_solved=True
):
    """Remove and verify one protected row, reporting whether this call removed it."""
    present = _protected_package_present(shared_state, package_id)
    if present is None:
        return {"package_removed": False, "removed_now": False}
    if not present:
        return {"package_removed": True, "removed_now": False}

    protected_release = _get_protected_release(shared_state, package_id)
    _delete_protected_package(shared_state, package_id)
    if _protected_package_present(shared_state, package_id) is not False:
        return {"package_removed": False, "removed_now": False}
    if notify_solved and protected_release:
        update_release_notification(
            shared_state,
            protected_release,
            NotificationType.SOLVED,
            details=notification_details,
        )
    return {"package_removed": True, "removed_now": True}


def confirm_protected_removal(shared_state, package_id, notification_details=None):
    """Prove the protected row of a submitted package is gone, removing it once.

    A delete call is not a proof, so the row is read back and an unreadable
    store never reports success. Safe to repeat: a package that is already
    absent counts as removed and sends no second solved notification, because
    only the call that actually removed the row notifies.
    """
    return finalize_protected_removal(shared_state, package_id, notification_details)[
        "package_removed"
    ]


def jdownloader_holds_operation(shared_state, package_id, terminal_operation):
    """Whether JDownloader carries what THIS operation submitted, or None.

    An earlier HTTP 200 never proved that a submission was recorded here, so an
    operation that already began its attempt asks JDownloader itself before it
    may submit again. The bare package ID in a comment only shows that some
    submission happened - a legacy one, a manual one, or an earlier life of the
    same release - so the operation marker the submission travelled with is
    what is matched. Both JDownloader lists are consulted because a package can
    already have left the linkgrabber. `None` means the answer could not be
    obtained and never authorizes a second submission.
    """
    unknown = object()
    comment = submission_comment(package_id, terminal_operation)

    def carries(entries):
        for entry in entries or ():
            if isinstance(entry, dict) and entry.get("comment") == comment:
                return True
        return False

    def query(device):
        if carries(device.linkgrabber.query_packages()):
            return True
        if carries(device.linkgrabber.query_links()):
            return True
        if carries(device.downloads.query_packages()):
            return True
        return carries(device.downloads.query_links())

    held = shared_state.run_device_request(
        "check whether a package was already submitted",
        query,
        default=unknown,
    )
    return None if held is unknown else bool(held)


def submit_final_download_urls(
    shared_state,
    urls,
    title,
    password,
    package_id,
    remove_protected=False,
    notification_details=None,
    phase=SUBMIT_PHASE_ALL,
    terminal_operation=None,
):
    """
    Final mirror whitelist check before sending direct HTTP links to JDownloader.
    """
    protected_release = None
    if remove_protected:
        protected_release = _get_protected_release(shared_state, package_id) or {
            "title": title
        }
    category, mirrors, filtered = project_final_download_urls(urls, package_id)
    final_urls = filtered["urls"]
    dropped = filtered["dropped"]

    if mirrors and dropped:
        info(
            f"Final mirror-whitelist check kept <g>{len(final_urls)}</g> of <y>{len(urls)}</y> links "
            f'for "{title}" in category "{category}" '
            f"(allowed: {_format_mirror_token_list(filtered['allowed_tokens'])}, "
            f"dropped: {_format_mirror_token_list({item['token'] for item in dropped})})"
        )

    if mirrors and not final_urls:
        reason = (
            f'All final download links were rejected by the mirror-whitelist for category "{category}". '
            f"Allowed mirrors: {_format_mirror_token_list(filtered['allowed_tokens'])}. "
            f"Received mirrors: {_format_mirror_token_list({item['token'] for item in dropped})}."
        )
        if phase == SUBMIT_PHASE_SUBMIT:
            # A terminal caller owns the whole failure: it has to persist it
            # against its operation before anything is counted, announced or
            # removed, so this half only reports that nothing was submitted.
            return {"success": False, "mirror_rejected": True, "reason": reason}
        result = _persist_failed_package(
            shared_state,
            title,
            package_id,
            reason,
            remove_protected=remove_protected,
        )
        if protected_release:
            update_release_notification(
                shared_state,
                protected_release,
                NotificationType.FAILED,
                details={"reason": reason},
            )
        return result

    info(f"Sending {len(final_urls)} direct download links for {title}")
    if download_package(
        final_urls,
        title,
        password,
        package_id,
        shared_state,
        comment=submission_comment(package_id, terminal_operation),
    ):
        if remove_protected and phase != SUBMIT_PHASE_SUBMIT:
            _delete_protected_package(shared_state, package_id)
            if protected_release:
                update_release_notification(
                    shared_state,
                    protected_release,
                    NotificationType.SOLVED,
                    details=notification_details,
                )
        return {"success": True, "links": final_urls}
    return {
        "success": False,
        "reason": f"Failed to add {len(final_urls)} links to linkgrabber",
    }


def handle_direct_links(shared_state, links, title, password, package_id):
    """Send direct hoster links to JDownloader."""
    urls = [link[0] for link in links]
    result = submit_final_download_urls(shared_state, urls, title, password, package_id)
    if result["success"]:
        StatsHelper(shared_state).increment_package_with_links(result["links"])
        return {"success": True}
    return result


def handle_auto_decrypt_links(shared_state, links, title, password, package_id):
    """Decrypt hide.cx links and send to JDownloader."""
    result = decrypt_links_if_hide(shared_state, links)

    if result.get("status") == "gone":
        # The crypter itself reports the container as missing, so no manual
        # CAPTCHA can rescue it either. Told apart from an ordinary failure
        # so process_links() can end the package instead of parking it.
        return {
            "success": False,
            "container_gone": True,
            "reason": "Linkcrypter reports the container as no longer available",
        }

    if result.get("status") != "success":
        return {"success": False, "reason": "Auto-decrypt failed"}

    decrypted_urls = result.get("results", [])
    if not decrypted_urls:
        return {"success": False, "reason": "No links decrypted"}

    info(f"Decrypted <g>{len(decrypted_urls)}</g> download links for {title}")

    submit_result = submit_final_download_urls(
        shared_state, decrypted_urls, title, password, package_id
    )
    if submit_result["success"]:
        StatsHelper(shared_state).increment_package_with_links(submit_result["links"])
        return {"success": True}
    return submit_result


def store_protected_links(
    shared_state,
    links,
    title,
    password,
    package_id,
    size_mb=None,
    original_url=None,
    imdb_id=None,
    notifications=None,
):
    """Store protected links for CAPTCHA UI."""
    blob_data = {
        "title": title,
        "links": links,
        "password": password,
        "size_mb": size_mb,
    }
    if original_url:
        blob_data["original_url"] = original_url
    if imdb_id:
        blob_data["imdb_id"] = imdb_id
    if notifications:
        blob_data["notifications"] = notifications

    def create_or_merge(current_value):
        """Never let a stale duplicate grab overwrite the live protected row.

        Duplicate detection happens before this call, so a concurrent request
        can create and defer the package in between. Existing fields therefore
        win and only genuinely missing ones are added.
        """
        try:
            existing = json.loads(current_value) if current_value is not None else None
        except (TypeError, json.JSONDecodeError):
            existing = None
        if not isinstance(existing, dict):
            return json.dumps(blob_data)
        merged = {**blob_data, **existing}
        return current_value if merged == existing else json.dumps(merged)

    shared_state.values["database"]("protected").mutate_value(
        package_id, create_or_merge
    )
    info(
        f'CAPTCHA-Solution required for <b>{title}</b> at: "{shared_state.values["external_address"]}/captcha"'
    )
    return {"success": True}


def _record_origin(shared_state, package_id, crypter, links):
    """Persist which linkcrypter and host this package came from, once.

    Called from each of process_links()'s three priority branches with that
    branch's own links, so the record always names the bucket that actually
    won - never the first link of a mixed result. Writing is create-only, so
    the auto-decrypt branch falling back into the protected one below leaves
    the first branch's record (and its `added_epoch`) untouched.
    """
    if not links:
        return
    first = links[0]
    url = first[0] if isinstance(first, (list, tuple)) and first else first
    record_package_origin(shared_state, package_id, crypter, url)


def process_links(
    shared_state,
    source_result,
    title,
    password,
    package_id,
    imdb_id,
    source_url,
    size_mb,
    label,
):
    """
    Central link processor with priority: direct → auto-decrypt → protected.
    If ANY direct links exist, use them and ignore crypted fallbacks.
    """
    if not source_result:
        return fail(
            title,
            package_id,
            shared_state,
            reason=f'Source returned no data for "{title}" on {label} - "{source_url}"',
        )

    links = source_result.get("links", [])
    password = source_result.get("password") or password
    imdb_id = imdb_id or source_result.get("imdb_id")
    title = normalize_download_title(source_result.get("title") or title)

    if not links:
        return fail(
            title,
            package_id,
            shared_state,
            reason=f'No links found for "{title}" on {label} - "{source_url}"',
        )

    # Filter out 404 links
    valid_links = [link for link in links if "/404.html" not in link[0]]
    if not valid_links:
        return fail(
            title,
            package_id,
            shared_state,
            reason=f'All links are offline or IP is banned for "{title}" on {label} - "{source_url}"',
        )
    links = valid_links

    # Filter out verifiably offline links
    links = filter_offline_links(links, shared_state=shared_state, log_func=info)
    if not links:
        return fail(
            title,
            package_id,
            shared_state,
            reason=f'All verifiable links are offline for "{title}" on {label} - "{source_url}"',
        )

    if source_result.get("protected") is True:
        classified = {"direct": [], "auto": [], "protected": links}
    else:
        classified = classify_links(links)
    classified = _drop_filecrypt_if_disabled(shared_state, classified, title)

    # PRIORITY 1: Direct hoster links
    if classified["direct"]:
        info(
            f"Found <g>{len(classified['direct'])}</g> direct hoster links for {title}"
        )
        _record_origin(shared_state, package_id, "direct", classified["direct"])
        result = handle_direct_links(
            shared_state, classified["direct"], title, password, package_id
        )
        if result["success"]:
            send_notification(
                shared_state,
                title=title,
                case=NotificationType.UNPROTECTED,
                imdb_id=imdb_id,
                source=source_url,
            )
            return {"success": True, "title": title}
        if result.get("persisted_failure"):
            return {"success": True, "title": title, "failed": True}
        return fail(title, package_id, shared_state, reason=result.get("reason"))

    # PRIORITY 2: Auto-decryptable (hide.cx)
    if classified["auto"]:
        info(
            f"Found <g>{len(classified['auto'])}</g> auto-decryptable links for {title}"
        )
        _record_origin(shared_state, package_id, "hide", classified["auto"])
        result = handle_auto_decrypt_links(
            shared_state, classified["auto"], title, password, package_id
        )
        if result["success"]:
            send_notification(
                shared_state,
                title=title,
                case=NotificationType.UNPROTECTED,
                imdb_id=imdb_id,
                source=source_url,
            )
            return {"success": True, "title": title}
        if result.get("persisted_failure"):
            return {"success": True, "title": title, "failed": True}
        if result.get("container_gone"):
            # Parking this would put a package in the CAPTCHA queue that
            # nobody - helper or human - can ever solve, where it waits
            # forever and has to be cleared by hand.
            return fail(
                title,
                package_id,
                shared_state,
                reason=(
                    f'Linkcrypter container for "{title}" no longer exists on '
                    f"{label} - nothing left to solve"
                ),
            )
        info(f"Auto-decrypt failed for {title}, falling back to manual CAPTCHA...")
        classified["protected"].extend(classified["auto"])

    # PRIORITY 3: Protected (filecrypt, tolink, keeplinks, junkies)
    if classified["protected"]:
        info(f"Found <g>{len(classified['protected'])}</g> protected links for {title}")
        _record_origin(
            shared_state,
            package_id,
            resolve_protected_crypter_key(classified["protected"][0]) or "unknown",
            classified["protected"],
        )
        notification_references = send_tracked_notification(
            shared_state,
            title=title,
            case=NotificationType.CAPTCHA,
            imdb_id=imdb_id,
            source=source_url,
        )
        store_protected_links(
            shared_state,
            classified["protected"],
            title,
            password,
            package_id,
            size_mb=size_mb,
            original_url=source_url,
            imdb_id=imdb_id,
            notifications=notification_references,
        )
        return {"success": True, "title": title}

    return fail(
        title,
        package_id,
        shared_state,
        reason=f'No usable links found for {title} on {label} - "{source_url}"',
    )


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================


def package_id_exists(shared_state, package_id):
    # DB checks
    if shared_state.get_db("protected").retrieve(package_id):
        return True
    if shared_state.get_db("failed").retrieve(package_id):
        return True

    data = (
        shared_state.run_device_request(
            "load package state for duplicate detection",
            lambda _device: get_packages(shared_state),
            default={},
        )
        or {}
    )

    for section in ("queue", "history"):
        for pkg in data.get(section, []) or []:
            if pkg.get("nzo_id") == package_id:
                return True

    return False


def download(
    shared_state,
    request_from,
    download_category,
    title,
    url,
    size_mb,
    password,
    imdb_id,
    source_key,
):
    """
    Main download entry point.

    Args:
        shared_state: Application shared state
        request_from: User-Agent string (e.g., "Radarr/6.0.4.10291")
        download_category: Download category (e.g., "movies", "tv", "docs")
        title: Release title
        url: Source URL
        size_mb: Size in MB
        password: Archive password
        imdb_id: IMDb ID (optional)
        source_key: Hostname shorthand from search. If not provided,
                    will be derived from URL matching against configured hostnames.
    """
    package_id = None
    try:
        if imdb_id and imdb_id.lower() == "none":
            imdb_id = None

        title = normalize_download_title(title)
        config = shared_state.values["config"]("Hostnames")

        # Extract client type (without version) for deterministic hashing
        client_type = extract_client_type(request_from)

        # Find matching source - all getters have unified signature
        source_result = None
        label = None
        detected_source_key = None

        mirrors = get_download_category_mirrors(download_category, lowercase=True)
        download_sources = get_download_sources()

        normalized_source_key = None
        if source_key and isinstance(source_key, str):
            normalized_source_key = source_key.lower().strip()

        source_candidates = []
        if normalized_source_key and normalized_source_key in download_sources:
            source_candidates.append(
                (normalized_source_key, download_sources[normalized_source_key], True)
            )

        for key, source in download_sources.items():
            if normalized_source_key and key == normalized_source_key:
                continue
            source_candidates.append((key, source, False))

        for key, source, from_source_key in source_candidates:
            hostname = config.get(key)
            if not from_source_key and not (
                hostname and hostname.lower() in url.lower()
            ):
                continue

            try:
                # Mirrors are download-category-driven and passed to each source getter.
                candidate_result = source.get_download_links(
                    shared_state, url, mirrors, title, password
                )
                if candidate_result and candidate_result.get("links"):
                    clear_hostname_issue(key)
                    source_result = candidate_result
                    label = key.upper()
                    detected_source_key = key
                    break
            except Exception as e:
                info(f"Error getting download links from {key.upper()}: {e}")
                if not from_source_key or (
                    hostname and hostname.lower() in url.lower()
                ):
                    mark_hostname_issue(key, "download", str(e))

        # No source matched - check if URL is a known crypter directly
        if source_result is None:
            crypter, crypter_type = detect_crypter(url)
            if crypter_type:
                # For direct crypter URLs, we only know the crypter type, not the hoster inside
                source_result = {"links": [[url, crypter]]}
                label = crypter.upper()
                detected_source_key = crypter

        # Use provided source_key if available, otherwise use detected one
        # This ensures we use the authoritative source from the search results
        final_source_key = source_key if source_key else detected_source_key

        # Generate DETERMINISTIC package_id
        package_id = generate_deterministic_package_id(
            title, final_source_key, client_type, download_category
        )

        # Skip Download if package_id already exists
        if package_id_exists(shared_state, package_id):
            warn(f"Package {package_id} already exists. Skipping download!")
            return {"success": True, "package_id": package_id, "title": title}

        if source_result is None:
            result = fail(
                title,
                package_id,
                shared_state,
                reason=f'Could not find matching source for "{title}" - "{url}"',
            )
            return {"package_id": package_id, **result}

        result = process_links(
            shared_state,
            source_result,
            title,
            password,
            package_id,
            imdb_id,
            url,
            size_mb,
            label,
        )
        return {"package_id": package_id, **result}

    except Exception as e:
        if not package_id:
            # Fallback generation if we crashed early
            try:
                client_type = extract_client_type(request_from)
            except Exception:
                client_type = "unknown"

            final_source_key = source_key if source_key else "unknown"

            package_id = generate_deterministic_package_id(
                title, final_source_key, client_type, download_category
            )

        result = fail(title, package_id, shared_state, reason=f"Unexpected error: {e}")
        return {"package_id": package_id, **result}


def fail(title, package_id, shared_state, reason="Unknown error"):
    """Mark download as failed.

    The row is stored before anything is counted or announced, so a store that
    never lands cannot leave a counter answering for history nobody has, and
    the result says whether the failure was really recorded.
    """
    persisted = False
    try:
        error(f"Reason for failure: {reason}")
        shared_state.get_db(FAILED_TABLE).store(
            package_id, failed_row_value(title, reason)
        )
        persisted = True
    except Exception as e:
        error(f'Error marking package "{package_id}" as failed: {e}')
    if persisted:
        try:
            StatsHelper(shared_state).increment_failed_downloads()
        except Exception as e:
            error(f'Error counting the failure of package "{package_id}": {e}')
        warn(f'Package "{title}" marked as failed!')
    return {"success": persisted, "title": title, "failed": True}
