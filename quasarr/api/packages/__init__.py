# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import html
import json
import time
from urllib.parse import quote

from bottle import redirect, request

import quasarr.providers.html_images as images
from quasarr.api.jdownloader import get_jdownloader_disconnected_page
from quasarr.downloads.packages import (
    DEFERRED_STATUS_PREFIX,
    PROTECTED_STATUS_PREFIX,
    delete_database_packages,
    delete_package,
    get_packages,
)
from quasarr.providers import shared_state
from quasarr.providers.auth import require_api_key
from quasarr.providers.crypter_cooldowns import (
    CrypterCooldownService,
    crypter_blocks_deferred,
)
from quasarr.providers.html_templates import render_button, render_centered_html
from quasarr.storage.categories import get_download_category_emoji


def _get_category_emoji(cat):
    if cat == "not_quasarr":
        return "❓"
    return get_download_category_emoji(cat)


def _format_size(mb=None, bytes_val=None):
    if bytes_val is not None:
        if bytes_val == 0:
            return "? MB"
        if bytes_val < 1024:
            return f"{bytes_val} B"
        if bytes_val < 1024 * 1024:
            return f"{bytes_val // 1024} KB"
        mb = bytes_val / (1024 * 1024)
    if mb is None or mb == 0:
        return "? MB"
    if mb < 1024:
        return f"{mb:.0f} MB"
    return f"{mb / 1024:.1f} GB"


def _javascript_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    encoded = json.dumps(str(value), ensure_ascii=True)
    return (
        encoded.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    )


def _javascript_call(function_name, *args):
    arguments = ", ".join(_javascript_value(value) for value in args)
    return html.escape(f"{function_name}({arguments})", quote=True)


def _html(value):
    return html.escape(str(value), quote=True)


def _label(value, labels):
    text = str(value or "unknown")
    return labels.get(text, text.replace("_", " ").strip().capitalize())


def _format_deferred_countdown(retry_after_epoch):
    try:
        remaining = max(0, int(retry_after_epoch) - int(time.time()))
    except (TypeError, ValueError):
        remaining = 0
    days, remainder = divmod(remaining, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes, seconds = divmod(remainder, 60)
    clock = f"{hours:02}:{minutes:02}:{seconds:02}"
    return f"{days}d {clock}" if days else clock


def _deferred_countdown(attribute, epoch):
    """One ticking countdown element carrying the deadline it counts down to."""
    return (
        f'<strong class="deferred-countdown" {attribute}="{epoch}">'
        f"{_format_deferred_countdown(epoch)}</strong>"
    )


def _render_deferred_queue_item(item):
    deferred = item.get("deferred", {})
    filename = str(item.get("filename", "Unknown"))
    for prefix in (DEFERRED_STATUS_PREFIX, PROTECTED_STATUS_PREFIX):
        if filename.startswith(prefix):
            filename = filename[len(prefix) :]
            break

    package_id = str(item.get("nzo_id", ""))
    package_id_attr = _html(package_id)
    package_url_id = quote(package_id, safe="")
    crypter_label = _label(
        deferred.get("crypter"),
        {"filecrypt": "Filecrypt", "junkies": "Junkies"},
    )
    reason_label = _label(
        deferred.get("reason_code"),
        {"ip_block_suspected": "IP access block suspected"},
    )
    hold_type = deferred.get("hold_type")
    if hold_type == "crypter_cooldown":
        state_label = "Cooldown"
    elif hold_type == "provisional":
        state_label = "Observing"
    else:
        state_label = _label(deferred.get("state"), {})
    try:
        evidence_count = max(0, int(deferred.get("evidence_count", 0)))
    except (TypeError, ValueError):
        evidence_count = 0
    try:
        retry_after_epoch = max(0, int(deferred.get("retry_after_epoch", 0)))
    except (TypeError, ValueError):
        retry_after_epoch = 0
    cohort_values = []
    for field in (
        "cohort_tested",
        "cohort_total",
        "cohort_deadline_epoch",
        "cohort_retest_depth",
    ):
        value = deferred.get(field, 0)
        cohort_values.append(value if type(value) is int and value >= 0 else 0)
    cohort_tested, cohort_total, cohort_deadline_epoch, cohort_retest_depth = (
        cohort_values
    )
    cohort_progress = ""
    cohort_deadline = ""
    if cohort_total > 0:
        cohort_progress = f"""
                <span><strong>Tested:</strong> {cohort_tested} / {cohort_total}</span>
                <span><strong>Retest queue:</strong> {cohort_retest_depth}</span>"""
        cohort_deadline = f"""
                <span>Cohort deadline {_deferred_countdown("data-cohort-deadline-epoch", cohort_deadline_epoch)}</span>"""
    probe_label = (
        "Probe queued"
        if deferred.get("probe_requested") is True
        else "Probe not queued"
    )

    return f'''
        <div class="package-card deferred-package-card" data-package-id="{package_id_attr}">
            <div class="package-header deferred-package-header">
                <input class="deferred-package-select" type="checkbox" value="{package_id_attr}" aria-label="Select {_html(filename)}">
                <span class="status-emoji">⏳</span>
                <span class="package-name">{_html(filename)}</span>
                <span class="deferred-state">{_html(state_label)}</span>
            </div>
            <div class="deferred-details">
                <span><strong>Linkcrypter:</strong> {_html(crypter_label)}</span>
                <span><strong>Reason:</strong> {_html(reason_label)}</span>
                <span><strong>Evidence:</strong> {evidence_count}</span>
                <span><strong>Package ID:</strong> <code>{package_id_attr}</code></span>
                {cohort_progress}
            </div>
            <div class="deferred-retry">
                <span>Retry in {_deferred_countdown("data-retry-after-epoch", retry_after_epoch)}</span>
                <span class="deferred-probe-state">{probe_label}</span>
                {cohort_deadline}
            </div>
            <div class="package-actions">
                <a class="btn-small primary-thin" href="/captcha?package_id={package_url_id}">🔓 Solve CAPTCHA</a>
                <button class="btn-small info" type="button" onclick="checkDeferredPackage(this)">↻ Check now</button>
                <span class="spacer"></span>
                <button class="btn-small danger" type="button" title="Delete package" aria-label="Delete package" onclick="deleteDeferredPackage(this)">🗑️</button>
            </div>
        </div>
    '''


def _requested_package_ids():
    try:
        payload = request.json
    except Exception:
        payload = None
    if not isinstance(payload, dict) or not isinstance(
        payload.get("package_ids"), list
    ):
        return None
    return payload["package_ids"]


def _render_queue_item(item):
    filename = str(item.get("filename", "Unknown"))
    try:
        percentage = max(0, min(100, int(item.get("percentage", 0))))
    except (TypeError, ValueError):
        percentage = 0
    timeleft = str(item.get("timeleft", "??:??:??"))
    bytes_val = item.get("bytes", 0)
    mb = item.get("mb", 0)
    cat = str(item.get("cat", "not_quasarr"))
    is_archive = item.get("is_archive", False)
    nzo_id = str(item.get("nzo_id", ""))
    storage = str(item.get("storage", ""))

    is_captcha = "[CAPTCHA" in filename
    status_text = "Downloading"
    if is_captcha:
        status_emoji = "🔒"
        status_text = "Waiting for CAPTCHA Solution!"
    elif "[Extracting]" in filename:
        status_emoji = "📦"
        status_text = "Extracting"
    elif "[Paused]" in filename:
        status_emoji = "⏸️"
        status_text = "Paused"
    elif "[Linkgrabber]" in filename:
        status_emoji = "🔗"
        status_text = "Linkgrabber"
    else:
        status_emoji = "▶️"

    display_name = filename
    for prefix in [
        "[Downloading] ",
        "[Extracting] ",
        "[Paused] ",
        "[Linkgrabber] ",
        "[CAPTCHA not solved!] ",
    ]:
        display_name = display_name.replace(prefix, "")

    archive_badge = "📦" if is_archive else ""
    cat_emoji = _get_category_emoji(cat)
    size_str = _format_size(bytes_val=bytes_val) if bytes_val else _format_size(mb=mb)

    # Progress bar - show "waiting..." for 0%
    if percentage == 0:
        progress_html = '<span class="progress-waiting"></span>'
    else:
        progress_html = f'<div class="progress-track"><div class="progress-fill" style="width: {percentage}%"></div></div>'

    # Interactive info
    info_onclick = _javascript_call(
        "showPackageDetails",
        nzo_id,
        display_name,
        cat,
        "Yes" if is_archive else "No",
        "",
        timeleft,
        size_str,
        percentage,
        status_text,
        storage,
        is_captcha,
    )
    info_btn = f'<button class="btn-small info" onclick="{info_onclick}">ℹ️</button>'

    # Action buttons - Info left, CAPTCHA/Delete right
    if is_captcha and nzo_id:
        captcha_url = f"/captcha?package_id={quote(nzo_id, safe='')}"
        actions = f"""
            <div class="package-actions">
                {info_btn}
                <button class="btn-small primary-thin" onclick="{_javascript_call("location.assign", captcha_url)}">🔓 Solve CAPTCHA</button>
                <span class="spacer"></span>
                <button class="btn-small danger" onclick="{_javascript_call("confirmDelete", nzo_id, display_name)}">🗑️</button>
            </div>
        """
    elif nzo_id:
        actions = f"""
            <div class="package-actions">
                {info_btn}
                <span class="spacer"></span>
                <button class="btn-small danger" onclick="{_javascript_call("confirmDelete", nzo_id, display_name)}">🗑️</button>
            </div>
        """
    else:
        actions = f"""
            <div class="package-actions">
                {info_btn}
                <span class="spacer"></span>
            </div>
        """

    cat_html = f'<span title="Category: {_html(cat)}">{cat_emoji}</span>'
    archive_html = (
        f'<span title="Archive: {_html(is_archive)}">{archive_badge}</span>'
        if is_archive
        else ""
    )

    return f"""
        <div class="package-card">
            <div class="package-header">
                <span class="status-emoji">{status_emoji}</span>
                <span class="package-name">{_html(display_name)}</span>
            </div>
            <div class="package-progress">
                {progress_html}
                <span class="progress-percent">{percentage}%</span>
            </div>
            <div class="package-details">
                <span>⏱️ {_html(timeleft)}</span>
                <span>💾 {_html(size_str)}</span>
                {cat_html}
                {archive_html}
            </div>
            {actions}
        </div>
    """


def _render_history_item(item):
    name = str(item.get("name", "Unknown"))
    status = str(item.get("status", "Unknown"))
    bytes_val = item.get("bytes", 0)
    category = str(item.get("category", "not_quasarr"))
    is_archive = item.get("is_archive", False)
    extraction_status = str(item.get("extraction_status", ""))
    fail_message = str(item.get("fail_message", ""))
    nzo_id = str(item.get("nzo_id", ""))
    storage = str(item.get("storage", ""))

    is_error = status.lower() in ["failed", "error"] or fail_message
    card_class = "package-card error" if is_error else "package-card"

    cat_emoji = _get_category_emoji(category)
    size_str = _format_size(bytes_val=bytes_val)

    archive_emoji = ""
    if is_archive:
        if extraction_status == "SUCCESSFUL":
            archive_emoji = "✅"
        elif extraction_status == "RUNNING":
            archive_emoji = "⏳"
        else:
            archive_emoji = "📦"

    status_emoji = "❌" if is_error else "✅"
    error_html = (
        f'<div class="package-error">⚠️ {_html(fail_message)}</div>'
        if fail_message
        else ""
    )

    # Interactive info
    info_onclick = _javascript_call(
        "showPackageDetails",
        nzo_id,
        name,
        category,
        "Yes" if is_archive else "No",
        extraction_status,
        "",
        size_str,
        "",
        status,
        storage,
        False,
    )
    info_btn = f'<button class="btn-small info" onclick="{info_onclick}">ℹ️</button>'

    # Delete button for history items
    if nzo_id:
        actions = f"""
            <div class="package-actions">
                {info_btn}
                <span class="spacer"></span>
                <button class="btn-small danger" onclick="{_javascript_call("confirmDelete", nzo_id, name)}">🗑️</button>
            </div>
        """
    else:
        actions = f"""
            <div class="package-actions">
                {info_btn}
                <span class="spacer"></span>
            </div>
        """

    cat_html = f'<span title="Category: {_html(category)}">{cat_emoji}</span>'
    archive_html = (
        f'<span title="Archive Status: {_html(extraction_status)}">{archive_emoji}</span>'
        if is_archive
        else ""
    )

    return f'''
        <div class="{card_class}">
            <div class="package-header">
                <span class="status-emoji">{status_emoji}</span>
                <span class="package-name">{_html(name)}</span>
            </div>
            <div class="package-details">
                <span>💾 {_html(size_str)}</span>
                {cat_html}
                {archive_html}
            </div>
            {error_html}
            {actions}
        </div>
    '''


def _render_packages_content():
    """Render just the packages content (used for both full page and AJAX refresh)."""
    downloads = get_packages(shared_state)
    queue = downloads.get("queue", [])
    history = downloads.get("history", [])

    # Separate Quasarr packages from others
    deferred_queue = []
    quasarr_queue = []
    for package in queue:
        if package.get("cat") == "not_quasarr":
            continue
        deferred = package.get("deferred")
        if isinstance(deferred, dict) and deferred.get("active") is True:
            deferred_queue.append(package)
        else:
            quasarr_queue.append(package)
    other_queue = [p for p in queue if p.get("cat") == "not_quasarr"]
    quasarr_history = [p for p in history if p.get("category") != "not_quasarr"]
    other_history = [p for p in history if p.get("category") == "not_quasarr"]

    # Check if there's anything at all
    has_quasarr_content = deferred_queue or quasarr_queue or quasarr_history
    has_other_content = other_queue or other_history
    has_any_content = has_quasarr_content or has_other_content

    deferred_html = ""
    if deferred_queue:
        deferred_items = "".join(
            _render_deferred_queue_item(item) for item in deferred_queue
        )
        deferred_html = f"""
            <div class="section deferred-section">
                <h3>⏳ Deferred linkcrypter checks</h3>
                <div class="deferred-toolbar">
                    <button class="btn-small info" type="button" onclick="checkSelectedDeferred(this)">↻ Check selected</button>
                    <button class="btn-small danger" type="button" onclick="deleteSelectedDeferred(this)">🗑️ Delete selected packages</button>
                </div>
                <div class="packages-list">{deferred_items}</div>
            </div>
        """

    # Build queue section (only if has items)
    queue_html = ""
    if quasarr_queue:
        queue_items = "".join(_render_queue_item(item) for item in quasarr_queue)
        queue_html = f"""
            <div class="section">
                <h3>⬇️ Downloading</h3>
                <div class="packages-list">{queue_items}</div>
            </div>
        """

    # Build history section (only if has items)
    history_html = ""
    if quasarr_history:
        history_items = "".join(
            _render_history_item(item) for item in quasarr_history[:10]
        )
        history_html = f"""
            <div class="section">
                <h3>📜 Recent History</h3>
                <div class="packages-list">{history_items}</div>
            </div>
        """

    # Build "other packages" section (non-Quasarr)
    other_html = ""
    other_count = len(other_queue) + len(other_history)
    if other_count > 0:
        other_items = ""
        if other_queue:
            other_items += f"<h4>Queue ({len(other_queue)})</h4>"
            other_items += "".join(_render_queue_item(item) for item in other_queue)
        if other_history:
            other_items += f"<h4>History ({len(other_history)})</h4>"
            other_items += "".join(
                _render_history_item(item) for item in other_history[:5]
            )

        plural = "s" if other_count != 1 else ""
        # Only add separator class if there's Quasarr content above
        section_class = (
            "other-packages-section"
            if has_quasarr_content
            else "other-packages-section no-separator"
        )
        other_html = f'''
            <div class="{section_class}">
                <details id="otherPackagesDetails">
                    <summary id="otherPackagesSummary">Show {other_count} other package{plural}</summary>
                    <div class="other-packages-content">{other_items}</div>
                </details>
            </div>
        '''

    # Only show "no downloads" if there's literally nothing
    empty_html = ""
    if not has_any_content:
        empty_html = '<p class="empty-message">No packages</p>'

    return f"""
        <div class="packages-container">
            {deferred_html}
            {queue_html}
            {history_html}
            {other_html}
            {empty_html}
        </div>
    """


def setup_packages_routes(app):
    @app.get("/packages/delete/<package_id>")
    def delete_package_route(package_id):

        # Get optional title parameter
        package_title = request.query.get("title")

        success = delete_package(shared_state, package_id, package_title)

        # Redirect back to packages page with status message via query param
        if success:
            redirect("/packages?deleted=1")
        else:
            redirect("/packages?deleted=0")

    @app.get("/api/packages/content")
    @require_api_key
    def packages_content_api():
        """AJAX endpoint - returns just the packages content HTML for background refresh."""
        try:
            device = shared_state.values["device"]
        except KeyError:
            device = None

        if not device:
            return """
                <div class="status-bar">
                    <span class="status-pill error">
                        ❌ JDownloader disconnected
                    </span>
                </div>
            """

        return _render_packages_content()

    @app.post("/api/packages/deferred/probe")
    @require_api_key
    def deferred_packages_probe_api():
        package_ids = _requested_package_ids()
        if package_ids is None:
            return {
                "success": False,
                "message": "package_ids must be a list",
            }
        if not crypter_blocks_deferred(shared_state):
            return {
                "success": False,
                "message": "Linkcrypter blocks are in fail mode",
            }
        return CrypterCooldownService(shared_state).request_probe(package_ids)

    @app.delete("/api/packages/deferred")
    @require_api_key
    def deferred_packages_delete_api():
        package_ids = _requested_package_ids()
        if package_ids is None:
            return {
                "success": False,
                "message": "package_ids must be a list",
            }
        return delete_database_packages(
            shared_state,
            package_ids,
            expected_type="protected",
        )

    @app.get("/api/packages/status")
    @require_api_key
    def packages_status_api():
        try:
            device = shared_state.values["device"]
        except KeyError:
            device = None

        if not device:
            return {
                "connected": False,
                "linkgrabber": {"is_collecting": False, "is_stopped": True},
                "queue_count": 0,
                "history_count": 0,
            }

        downloads = get_packages(shared_state)
        return {
            "connected": True,
            "linkgrabber": downloads.get(
                "linkgrabber", {"is_collecting": False, "is_stopped": True}
            ),
            "queue_count": len(downloads.get("queue", [])),
            "history_count": len(downloads.get("history", [])),
        }

    @app.get("/packages")
    def packages_status():
        try:
            device = shared_state.values["device"]
        except KeyError:
            device = None

        if not device:
            return get_jdownloader_disconnected_page(shared_state)

        # Check for delete status from redirect
        deleted = request.query.get("deleted")
        status_message = ""
        if deleted == "1":
            status_message = '<div class="status-message success">✅ Package deleted successfully.</div>'
        elif deleted == "0":
            status_message = (
                '<div class="status-message error">❌ Failed to delete package.</div>'
            )

        # Get rendered packages content using shared helper
        packages_content = _render_packages_content()

        back_btn = render_button("Back", "secondary", {"onclick": "location.href='/'"})

        packages_html = f'''
            <h1><img src="{images.logo}" type="image/webp" alt="Quasarr logo" class="logo"/>Quasarr</h1>
            <h2>Packages</h2>

            {status_message}

            <div id="slow-warning" class="slow-warning" style="display:none;">⚠️ Slow connection detected</div>

            <div id="deferred-action-status" class="deferred-action-status" aria-live="polite"></div>

            <div id="packages-content">
                {packages_content}
            </div>

            <p>{back_btn}</p>

            <style>
                .packages-container {{ max-width: 600px; margin: 0 auto; }}
                .section {{ margin: 20px 0; }}
                .section h3 {{ margin-bottom: 15px; padding-bottom: 8px; border-bottom: 1px solid var(--border-color, #ddd); }}
                .packages-list {{ display: flex; flex-direction: column; gap: 10px; }}

                .deferred-toolbar {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }}
                .deferred-package-header {{ align-items: center; }}
                .deferred-package-select {{ width: 18px; height: 18px; margin: 0; flex: 0 0 18px; }}
                .deferred-state {{ padding: 3px 7px; border: 1px solid var(--btn-info-bg, #17a2b8); border-radius: 4px; color: var(--btn-info-bg, #17a2b8); font-size: 0.75em; font-weight: 600; }}
                .deferred-details {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 5px 12px; color: var(--text-muted, #666); font-size: 0.82em; text-align: left; }}
                .deferred-details code {{ overflow-wrap: anywhere; }}
                .deferred-retry {{ display: flex; justify-content: space-between; gap: 10px; margin-top: 9px; font-size: 0.85em; }}
                .deferred-probe-state {{ color: var(--text-muted, #666); }}
                .deferred-action-status {{ min-height: 1.4em; margin-bottom: 8px; color: var(--text-muted, #666); font-size: 0.85em; }}
                a.btn-small {{ display: inline-flex; align-items: center; text-decoration: none; }}

                .package-card {{
                    background: var(--card-bg, #f8f9fa);
                    border: 1px solid var(--card-border, #dee2e6);
                    border-radius: 8px;
                    padding: 12px 15px;
                    transition: transform 0.2s, box-shadow 0.2s;
                }}
                .package-card:hover {{ transform: translateY(-1px); box-shadow: 0 2px 8px var(--card-shadow, rgba(0,0,0,0.1)); }}
                .package-card.error {{ border-color: var(--error-border, #dc3545); background: var(--error-bg, #fff5f5); }}

                .package-header {{ display: flex; align-items: flex-start; gap: 8px; margin-bottom: 8px; }}
                .status-emoji {{ font-size: 1.2em; flex-shrink: 0; }}
                .package-name {{ flex: 1; font-weight: 500; word-break: break-word; line-height: 1.3; }}
                .package-progress {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }}
                .progress-track {{ flex: 1; height: 8px; background: var(--progress-track, #e0e0e0); border-radius: 4px; overflow: hidden; }}
                .progress-fill {{ height: 100%; background: var(--progress-fill, #4caf50); border-radius: 4px; min-width: 4px; }}
                .progress-waiting {{ flex: 1; color: var(--text-muted, #888); font-style: italic; font-size: 0.85em; }}
                .progress-percent {{ font-weight: bold; min-width: 40px; text-align: right; font-size: 0.9em; }}

                .package-details {{ display: flex; flex-wrap: wrap; gap: 15px; font-size: 0.85em; color: var(--text-muted, #666); }}
                .package-error {{ margin-top: 8px; padding: 8px; background: var(--error-msg-bg, #ffebee); border-radius: 4px; font-size: 0.85em; color: var(--error-msg-color, #c62828); }}

                .package-actions {{ margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border-color, #eee); display: flex; gap: 8px; align-items: center; }}
                .package-actions .spacer {{ flex: 1; }}
                .package-actions.right-only {{ justify-content: flex-end; }}
                .btn-small {{ line-height:1; padding: 5px 12px; font-size: 0.8em; border-radius: 4px; cursor: pointer; transition: all 0.2s; }}
                .btn-small.primary {{ background: var(--btn-primary-bg, #007bff); color: white; border: none; }}
                .btn-small.primary:hover {{ background: var(--btn-primary-hover, #0056b3); }}
                .btn-small.danger {{ background: transparent; color: var(--btn-danger-text, #dc3545); border: 1px solid var(--btn-danger-border, #dc3545); }}
                .btn-small.danger:hover {{ background: var(--btn-danger-hover-bg, #dc3545); color: white; }}
                .btn-small.info {{ background: transparent; color: var(--btn-info-bg, #17a2b8); border: 1px solid var(--btn-info-bg, #17a2b8); }}
                .btn-small.info:hover {{ background: var(--btn-info-bg, #17a2b8); color: white; }}
                .btn-small.primary-thin {{ background: transparent; color: var(--btn-primary-bg, #007bff); border: 1px solid var(--btn-primary-bg, #007bff); }}
                .btn-small.primary-thin:hover {{ background: var(--btn-primary-bg, #007bff); color: white; }}

                @media (max-width: 600px) {{
                    .deferred-details {{ grid-template-columns: 1fr; }}
                    .deferred-retry {{ flex-direction: column; }}
                }}

                .empty-message {{ color: var(--text-muted, #888); font-style: italic; text-align: center; padding: 20px; }}

                .slow-warning {{
                    text-align: center;
                    font-size: 0.85em;
                    color: #856404;
                    background: #fff3cd;
                    border: 1px solid #ffc107;
                    padding: 8px 12px;
                    border-radius: 6px;
                    margin-bottom: 15px;
                }}
                @media (prefers-color-scheme: dark) {{
                    .slow-warning {{
                        color: #ffc107;
                        background: #3d3510;
                        border-color: #665c00;
                    }}
                }}

                .other-packages-section {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid var(--border-color, #ddd); }}
                .other-packages-section.no-separator {{ margin-top: 0; padding-top: 0; border-top: none; }}
                .other-packages-section summary {{ cursor: pointer; padding: 8px 0; color: var(--text-muted, #666); }}
                .other-packages-section summary:hover {{ color: var(--link-color, #0066cc); }}
                .other-packages-content {{ margin-top: 15px; }}
                .other-packages-content h4 {{ margin: 15px 0 10px 0; font-size: 0.95em; color: var(--text-muted, #666); }}

                /* Status message styling */
                .status-message {{
                    padding: 10px 15px;
                    border-radius: 6px;
                    margin-bottom: 15px;
                    font-weight: 500;
                }}
                .status-message.success {{
                    background: var(--success-bg, #d1e7dd);
                    color: var(--success-color, #198754);
                    border: 1px solid var(--success-border, #a3cfbb);
                }}
                .status-message.error {{
                    background: var(--error-bg, #f8d7da);
                    color: var(--error-color, #dc3545);
                    border: 1px solid var(--error-border, #f1aeb5);
                }}

                .btn-danger {{ background: transparent; color: var(--btn-danger-bg, #dc3545); border: 1px solid var(--btn-danger-bg, #dc3545); padding: 10px 20px; border-radius: 6px; cursor: pointer; font-weight: 500; }}
                .btn-danger:hover {{ background: var(--btn-danger-bg, #dc3545); color: white; }}

                /* Dark mode */
                @media (prefers-color-scheme: dark) {{
                    :root {{
                        --card-bg: #2d3748; --card-border: #4a5568; --card-shadow: rgba(0,0,0,0.3);
                        --border-color: #4a5568; --text-muted: #a0aec0;
                        --progress-track: #4a5568; --progress-fill: #68d391;
                        --error-border: #fc8181; --error-bg: #3d2d2d; --error-msg-bg: #3d2d2d; --error-msg-color: #fc8181;
                        --link-color: #63b3ed; --modal-bg: #2d3748; --code-bg: #1a202c;
                        --btn-primary-bg: #3182ce; --btn-primary-hover: #2c5282;
                        --btn-danger-text: #fc8181; --btn-danger-border: #fc8181; --btn-danger-hover-bg: #e53e3e;
                        --success-bg: #1c4532; --success-color: #68d391; --success-border: #276749;
                        --btn-info-bg: #38b2ac; --btn-info-hover: #319795;
                    }}
                }}
            </style>

            <script>
                // Background refresh - fetches content via AJAX, waits 5s between refresh cycles
                let refreshPaused = false;
                let slowConnection = false;
                let refreshTimer = null;
                let isFetching = false;
                const SCROLL_STORAGE_KEY = 'quasarr_packages_scroll_y';

                function deferredPackageId(button) {{
                    return button.closest('.deferred-package-card')?.dataset.packageId || '';
                }}

                function selectedDeferredPackageIds() {{
                    return Array.from(
                        document.querySelectorAll('.deferred-package-select:checked'),
                        checkbox => checkbox.value
                    );
                }}

                // Matches by value instead of a built selector, so a package ID can never
                // reach a query, and IDs that stopped being rendered stay dropped.
                function restoreDeferredSelection(packageIds) {{
                    const selected = new Set(packageIds);
                    if (!selected.size) return;

                    document.querySelectorAll('.deferred-package-select').forEach(checkbox => {{
                        if (selected.has(checkbox.value)) checkbox.checked = true;
                    }});
                }}

                function showDeferredActionResult(result, successKey) {{
                    const statusElement = document.getElementById('deferred-action-status');
                    if (!statusElement) return;

                    const rejected = Array.isArray(result?.rejected) ? result.rejected : [];
                    if (rejected.length) {{
                        statusElement.textContent = rejected
                            .map(item => String(item.package_id) + ': ' + String(item.reason))
                            .join('; ');
                    }} else if (result?.success === false) {{
                        statusElement.textContent = String(result.message || 'Request failed');
                    }} else {{
                        const completed = Array.isArray(result?.[successKey])
                            ? result[successKey].length
                            : 0;
                        statusElement.textContent = completed + ' package' + (completed === 1 ? '' : 's') + ' updated';
                    }}
                }}

                async function runDeferredAction(button, endpoint, packageIds, method = 'POST', successKey = 'requested') {{
                    const statusElement = document.getElementById('deferred-action-status');
                    if (!packageIds.length) {{
                        if (statusElement) statusElement.textContent = 'Select at least one deferred package';
                        return;
                    }}

                    button.disabled = true;
                    try {{
                        const response = await quasarrApiFetch(endpoint, {{
                            method,
                            headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify({{ package_ids: packageIds }})
                        }});
                        const result = await response.json();
                        showDeferredActionResult(result, successKey);
                        await refreshContent();
                    }} catch (error) {{
                        if (statusElement) statusElement.textContent = 'Deferred package request failed';
                    }} finally {{
                        button.disabled = false;
                    }}
                }}

                function checkDeferredPackage(button) {{
                    const packageId = deferredPackageId(button);
                    return runDeferredAction(button, '/api/packages/deferred/probe', [packageId]);
                }}

                function deleteDeferredPackage(button) {{
                    const packageId = deferredPackageId(button);
                    if (!window.confirm('Delete this deferred package?')) return;
                    return runDeferredAction(button, '/api/packages/deferred', [packageId], 'DELETE', 'deleted');
                }}

                function checkSelectedDeferred(button) {{
                    return runDeferredAction(button, '/api/packages/deferred/probe', selectedDeferredPackageIds());
                }}

                function deleteSelectedDeferred(button) {{
                    const packageIds = selectedDeferredPackageIds();
                    if (packageIds.length && !window.confirm('Delete the selected deferred packages?')) return;
                    return runDeferredAction(button, '/api/packages/deferred', packageIds, 'DELETE', 'deleted');
                }}

                function formatDeferredCountdown(remaining) {{
                    const days = Math.floor(remaining / 86400);
                    const hours = Math.floor((remaining % 86400) / 3600);
                    const minutes = Math.floor((remaining % 3600) / 60);
                    const seconds = remaining % 60;
                    const clock = [hours, minutes, seconds]
                        .map(value => String(value).padStart(2, '0'))
                        .join(':');
                    return days ? days + 'd ' + clock : clock;
                }}

                function deferredCountdownEpoch(element) {{
                    const epoch = Number.parseInt(element.dataset.cohortDeadlineEpoch ?? element.dataset.retryAfterEpoch ?? '0', 10);
                    return Number.isFinite(epoch) ? epoch : 0;
                }}

                function updateDeferredCountdowns() {{
                    const now = Math.floor(Date.now() / 1000);
                    document.querySelectorAll('.deferred-countdown').forEach(element => {{
                        const remaining = Math.max(0, deferredCountdownEpoch(element) - now);
                        element.textContent = formatDeferredCountdown(remaining);
                    }});
                }}

                function saveScrollPosition() {{
                    sessionStorage.setItem(SCROLL_STORAGE_KEY, String(window.scrollY || 0));
                }}

                function restoreScrollPosition() {{
                    const saved = Number(sessionStorage.getItem(SCROLL_STORAGE_KEY) || '0');
                    if (!Number.isFinite(saved)) return;

                    window.scrollTo(0, saved);
                }}

                async function refreshContent() {{
                    if (refreshPaused) return;
                    if (isFetching) return;

                    isFetching = true;
                    const startTime = Date.now();
                    const warningEl = document.getElementById('slow-warning');

                    // Save scroll position before refresh
                    saveScrollPosition();

                    // Show warning after 5s if still loading
                    const slowTimer = setTimeout(() => {{
                        slowConnection = true;
                        if (warningEl) warningEl.style.display = 'block';
                    }}, 5000);

                    try {{
                        const response = await quasarrApiFetch('/api/packages/content');
                        const elapsed = Date.now() - startTime;

                        clearTimeout(slowTimer);

                        // Update slow connection state
                        if (elapsed < 5000) {{
                            slowConnection = false;
                            if (warningEl) warningEl.style.display = 'none';
                        }} else {{
                            slowConnection = true;
                            if (warningEl) warningEl.style.display = 'block';
                        }}

                        if (response.ok) {{
                            const html = await response.text();
                            const container = document.getElementById('packages-content');
                            if (container && html) {{
                                const selectedDeferredIds = selectedDeferredPackageIds();
                                container.innerHTML = html;
                                restoreDeferredSelection(selectedDeferredIds);
                                restoreCollapseState();
                                updateDeferredCountdowns();
                                // Restore scroll position after content update
                                restoreScrollPosition();
                                requestAnimationFrame(restoreScrollPosition);
                            }}
                        }}
                    }} catch (e) {{
                        clearTimeout(slowTimer);
                    }} finally {{
                        isFetching = false;
                    }}
                    
                    // Only schedule next refresh if not paused
                    if (!refreshPaused) {{
                        if (refreshTimer) clearTimeout(refreshTimer);
                        refreshTimer = setTimeout(refreshContent, 5000);
                    }}
                }}

                function restoreCollapseState() {{
                    const otherDetails = document.getElementById('otherPackagesDetails');
                    const otherSummary = document.getElementById('otherPackagesSummary');
                    if (otherDetails && otherSummary) {{
                        const count = otherSummary.textContent.match(/\\d+/)?.[0] || '0';
                        const plural = count !== '1' ? 's' : '';
                        if (localStorage.getItem('otherPackagesOpen') === 'true') {{
                            otherDetails.open = true;
                            otherSummary.textContent = 'Hide ' + count + ' other package' + plural;
                        }}
                        // Re-attach event listener
                        otherDetails.onclick = null;
                        otherDetails.addEventListener('toggle', function() {{
                            localStorage.setItem('otherPackagesOpen', this.open);
                            const summaryEl = document.getElementById('otherPackagesSummary');
                            if (summaryEl) {{
                                summaryEl.textContent = (this.open ? 'Hide ' : 'Show ') + count + ' other package' + plural;
                            }}
                        }});
                    }}
                }}

                // Initial collapse state setup
                restoreCollapseState();
                updateDeferredCountdowns();
                window.setInterval(updateDeferredCountdowns, 1000);
                restoreScrollPosition();
                window.addEventListener('scroll', saveScrollPosition, {{ passive: true }});

                // Clear status message from URL after display and auto-hide after 5s
                if (window.location.search.includes('deleted=')) {{
                    const url = new URL(window.location);
                    url.searchParams.delete('deleted');
                    window.history.replaceState({{}}, '', url);

                    // Hide the status message after 5 seconds
                    const statusMsg = document.querySelector('.status-message');
                    if (statusMsg) {{
                        setTimeout(() => {{
                            statusMsg.style.transition = 'opacity 0.3s';
                            statusMsg.style.opacity = '0';
                            setTimeout(() => statusMsg.remove(), 300);
                        }}, 5000);
                    }}

                    // Reset refresh - start fresh 5s countdown after delete
                    setTimeout(refreshContent, 5000);
                }} else {{
                    // Normal start - 5s delay
                    setTimeout(refreshContent, 5000);
                }}

                // Delete modal
                let deletePackageId = null;
                let deletePackageName = null;

                function escapePackageHtml(value) {{
                    const element = document.createElement('div');
                    element.textContent = String(value ?? '');
                    return element.innerHTML;
                }}
                
                function confirmDelete(packageId, packageName) {{
                    // Stop any pending refresh
                    if (refreshTimer) clearTimeout(refreshTimer);
                    refreshPaused = true;
                    
                    deletePackageId = packageId;
                    deletePackageName = packageName;
                    const safePackageName = escapePackageHtml(packageName);
                    
                    const content = `
                        <p class="modal-package-name" style="font-weight: 500; word-break: break-word; padding: 10px; background: var(--code-bg, #f5f5f5); border-radius: 6px; margin: 10px 0;">${{safePackageName}}</p>
                        <div class="modal-warning" style="background: var(--error-msg-bg, #ffebee); color: var(--error-msg-color, #c62828); padding: 12px; border-radius: 6px; margin: 15px 0; font-size: 0.9em; text-align: left;">
                            <strong>⛔ Warning:</strong> This will permanently delete the package AND all associated files from disk. This action cannot be undone!
                        </div>
                    `;
                    
                    const buttons = `
                        <button class="btn-secondary" onclick="closeModal()">Back</button>
                        <button class="btn-danger" onclick="performDelete()">🗑️ Delete Package & Files</button>
                    `;
                    
                    showModal('🗑️ Delete Package?', content, buttons);
                }}
                
                function performDelete() {{
                    if (deletePackageId) {{
                        let url = '/packages/delete/' + encodeURIComponent(deletePackageId);
                        if (deletePackageName) {{
                            url += '?title=' + encodeURIComponent(deletePackageName);
                        }}
                        location.href = url;
                    }}
                }}
                
                // Show package details modal
                function showPackageDetails(id, name, category, isArchive, extractionStatus, eta, size, percentage, status, storage, isCaptcha) {{
                    // Stop any pending refresh
                    if (refreshTimer) clearTimeout(refreshTimer);
                    refreshPaused = true;
                    
                    let captchaBtn = '';
                    if (isCaptcha) {{
                        const captchaUrl = '/captcha?package_id=' + encodeURIComponent(String(id || ''));
                        captchaBtn = `<button class="btn-small primary-thin" type="button" data-captcha-url="${{escapePackageHtml(captchaUrl)}}" onclick="location.href=this.dataset.captchaUrl">🔓 Solve CAPTCHA</button>`;
                    }}

                    const safeId = escapePackageHtml(id);
                    const safeName = escapePackageHtml(name);
                    const safeCategory = escapePackageHtml(category);
                    const safeArchive = escapePackageHtml(isArchive);
                    const safeExtractionStatus = escapePackageHtml(extractionStatus);
                    const safeEta = escapePackageHtml(eta);
                    const safeSize = escapePackageHtml(size);
                    const safePercentage = escapePackageHtml(percentage);
                    const safeStatus = escapePackageHtml(status);
                    const safeStorage = escapePackageHtml(storage);
                    
                    const content = `
                        <div style="text-align: left; padding: 10px;">
                            <p style="margin-bottom: 8px;"><strong>Name:</strong></p><p style="font-family: monospace; text-align: center; background: var(--code-bg, #eee); padding: 4px; border-radius: 4px; word-break: break-word;">${{safeName}}</p>
                            ${{storage ? `<p style="margin-bottom: 4px;"><strong>Storage:</strong></p><p style="margin-bottom: 8px; font-family: monospace; text-align: center; background: var(--code-bg, #eee); padding: 4px; border-radius: 4px; word-break: break-all;">${{safeStorage}}</p>` : ''}}
                            <p style="margin-bottom: 4px;"><strong>ID:</strong></p><p style="margin-bottom: 8px; font-family: monospace; text-align: center; background: var(--code-bg, #eee); padding: 4px; border-radius: 4px; overflow-wrap: anywhere;">${{safeId}}</p>
                            <p style="margin-bottom: 8px;"><strong>Status:</strong> ${{safeStatus}}</p>
                            ${{percentage ? `<p style="margin-bottom: 8px;"><strong>Percentage:</strong> ${{safePercentage}}%</p>` : ''}}
                            ${{size ? `<p style="margin-bottom: 8px;"><strong>Size:</strong> ${{safeSize}}</p>` : ''}}
                            ${{eta ? `<p style="margin-bottom: 8px;"><strong>ETA:</strong> ${{safeEta}}</p>` : ''}}
                            <p style="margin-bottom: 8px;"><strong>Category:</strong> ${{safeCategory}}</p>
                            <p style="margin-bottom: 8px;"><strong>Archive:</strong> ${{safeArchive}}</p>
                            ${{extractionStatus ? `<p style="margin-bottom: 8px;"><strong>Extraction Status:</strong> ${{safeExtractionStatus}}</p>` : ''}}
                        </div>
                    `;
                    
                    const buttons = `
                        <button class="btn-secondary" onclick="closeModal()">Back</button>
                        ${{captchaBtn}}
                    `;
                    
                    showModal('ℹ️ Package Details', content, buttons);
                }}
                
                // Hook into modal closing to resume refresh
                document.addEventListener('DOMContentLoaded', function() {{
                    const baseCloseModal = window.closeModal;
                    window.closeModal = function() {{
                        if (baseCloseModal) baseCloseModal();
                        
                        // Clear any existing timer to prevent duplicates
                        if (refreshTimer) clearTimeout(refreshTimer);
                        
                        refreshPaused = false;
                        refreshContent();
                    }};
                }});
            </script>
        '''

        return render_centered_html(packages_html)
