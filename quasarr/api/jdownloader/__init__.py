# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

from quasarr.providers.html_templates import render_button
from quasarr.storage.config import Config


def get_jdownloader_status(shared_state):
    """Get JDownloader connection status and device name."""
    try:
        device = shared_state.values.get("device")
        jd_connected = device is not None and device is not False
    except:
        jd_connected = False

    jd_config = Config("JDownloader")
    jd_device = jd_config.get("device") or ""

    dev_name = jd_device if jd_device else "JDownloader"
    dev_name_safe = (
        dev_name.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )

    if jd_connected:
        status_text = f"✅ {dev_name_safe} connected"
        status_class = "success"
    elif jd_device:
        status_text = f"❌ {dev_name_safe} disconnected"
        status_class = "error"
    else:
        status_text = "❌ JDownloader disconnected"
        status_class = "error"

    return {
        "connected": jd_connected,
        "device_name": jd_device,
        "status_text": status_text,
        "status_class": status_class,
    }


def get_jdownloader_status_pill(shared_state):
    """Return the HTML for the JDownloader status pill."""
    status = get_jdownloader_status(shared_state)

    return f"""
        <span class="status-pill {status["status_class"]}" 
              title="JDownloader Status">
            {status["status_text"]}
        </span>
    """


def get_jdownloader_disconnected_page(shared_state, back_url="/"):
    """Return a full error page when JDownloader is disconnected."""

    def classic():
        import quasarr.providers.html_images as images
        from quasarr.providers.html_templates import render_centered_html

        status_pill = get_jdownloader_status_pill(shared_state)

        back_btn = render_button(
            "Back", "secondary", {"onclick": f"location.href='{back_url}'"}
        )

        content = f'''
        <h1><img src="{images.logo}" type="image/webp" alt="Quasarr logo" class="logo"/>Quasarr</h1>
        <div class="status-bar">
            {status_pill}
        </div>
        <p>{back_btn}</p>
        <style>
            .status-pill {{
                font-size: 0.9em;
                padding: 8px 16px;
                border-radius: 0.5rem;
                font-weight: 500;
            }}
            .status-pill.success {{
                background: var(--status-success-bg, #e8f5e9);
                color: var(--status-success-color, #2e7d32);
                border: 1px solid var(--status-success-border, #a5d6a7);
            }}
            .status-pill.error {{
                background: var(--status-error-bg, #ffebee);
                color: var(--status-error-color, #c62828);
                border: 1px solid var(--status-error-border, #ef9a9a);
            }}
            /* Dark mode */
            @media (prefers-color-scheme: dark) {{
                :root {{
                    --status-success-bg: #1c4532;
                    --status-success-color: #68d391;
                    --status-success-border: #276749;
                    --status-error-bg: #3d2d2d;
                    --status-error-color: #fc8181;
                    --status-error-border: #c53030;
                }}
            }}
        </style>
    '''

        return render_centered_html(content)

    def carbon():
        from html import escape

        from quasarr.providers.carbon_templates import (
            page_header,
            render_carbon_simple_page,
            tag,
            tile,
        )

        status = get_jdownloader_status(shared_state)
        tone = "green" if status["connected"] else "red"
        text = "Connected" if status["connected"] else "Disconnected"
        device_html = (
            f"<p>{escape(status['device_name'], quote=True)}</p>"
            if status["device_name"]
            else ""
        )
        safe_back_url = escape(back_url, quote=True)
        back_btn = (
            f'<a class="cds-btn cds-btn--primary" href="{safe_back_url}">Back</a>'
        )
        body = f"{tag(text, tone=tone)}{device_html}<p>{back_btn}</p>"
        content = page_header("JDownloader", "Connection Required") + tile(body)
        return render_carbon_simple_page(content, title="JDownloader")

    from quasarr.providers.page_dispatch import render_page

    return render_page("jd-disconnected", carbon, classic, shared_state=shared_state)
