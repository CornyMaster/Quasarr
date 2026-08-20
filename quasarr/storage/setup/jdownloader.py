# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

from bottle import Bottle, request, response

import quasarr.providers.web_server
from quasarr.providers.html_templates import (
    render_button,
    render_fail,
    render_form,
)
from quasarr.providers.log import info
from quasarr.providers.page_dispatch import render_page
from quasarr.providers.web_server import Server
from quasarr.storage.config import Config
from quasarr.storage.setup.common import (
    add_no_cache_headers,
    render_reconnect_success,
    setup_auth,
)


def verify_jdownloader_credentials(shared_state):
    """Verify JDownloader credentials and return devices."""
    response.content_type = "application/json"
    try:
        data = request.json
        username = data.get("user")
        password = data.get("pass")

        devices = shared_state.get_devices(username, password)
        device_names = []
        if devices:
            for device in devices:
                device_names.append(device["name"])

        if device_names:
            return {"success": True, "devices": device_names}
        return {
            "success": False,
            "message": "No devices found or invalid credentials",
        }
    except Exception as e:
        return {"success": False, "message": str(e)}


def save_jdownloader_settings(shared_state, is_setup=False):
    """Save JDownloader settings."""
    if request.json:
        username = request.json.get("user")
        password = request.json.get("pass")
        device = request.json.get("device")
    else:
        username = request.forms.get("user")
        password = request.forms.get("pass")
        device = request.forms.get("device")

    if username and password and device:
        if shared_state.set_device(username, password, device):
            config = Config("JDownloader")
            config.save("user", username)
            config.save("password", password)
            config.save("device", device)

            if is_setup:
                quasarr.providers.web_server.temp_server_success = True
                return render_reconnect_success("Credentials set")

            response.content_type = "application/json"
            return {
                "success": True,
                "message": "JDownloader configured successfully",
            }

        if is_setup:
            return render_fail("Could not connect to selected device!")

        response.content_type = "application/json"
        return {
            "success": False,
            "message": "Could not connect to selected device",
        }

    if is_setup:
        return render_fail("Could not set credentials!")

    response.content_type = "application/json"
    return {"success": False, "message": "Missing required fields"}


def jdownloader_config(shared_state):
    app = Bottle()
    add_no_cache_headers(app)
    setup_auth(app)

    def _classic_jd_form():
        verify_form_html = f"""
        <span>If required register account at: <a href="https://my.jdownloader.org/login.html#register" target="_blank">
        my.jdownloader.org</a>!</span><br>

        <p><strong>JDownloader must be running and connected to My JDownloader!</strong></p><br>

        <form id="verifyForm" action="/api/verify_jdownloader" method="post">
            <label for="user">E-Mail</label>
            <input type="text" id="user" name="user" placeholder="user@example.org" autocorrect="off"><br>
            <label for="pass">Password</label>
            <input type="password" id="pass" name="pass" placeholder="Password"><br>
            {
            render_button(
                "Verify Credentials",
                "secondary",
                {
                    "id": "verifyButton",
                    "type": "button",
                    "onclick": "verifyCredentials()",
                },
            )
        }
        </form>

        <p>Some JDownloader settings will be enforced by Quasarr on startup.</p>

        <form action="/api/store_jdownloader" method="post" id="deviceForm" style="display: none;" onsubmit="return handleStoreSubmit(this)">
            <input type="hidden" id="hiddenUser" name="user">
            <input type="hidden" id="hiddenPass" name="pass">
            <label for="device">JDownloader</label>
            <select id="device" name="device"></select><br>
            {render_button("Save", "primary", {"type": "submit", "id": "storeBtn"})}
        </form>
        <p><strong>Saving may take a while!</strong></p><br>
        """

        verify_script = """
        <script>
        var verifyInProgress = false;
        var storeSubmitted = false;
        function verifyCredentials() {
            if (verifyInProgress) return;
            verifyInProgress = true;
            var btn = document.getElementById('verifyButton');
            if (btn) { btn.disabled = true; btn.textContent = 'Verifying...'; }

            var user = document.getElementById('user').value;
            var pass = document.getElementById('pass').value;
            quasarrApiFetch('/api/verify_jdownloader', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({user: user, pass: pass}),
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    var select = document.getElementById('device');
                    data.devices.forEach(device => {
                        var opt = document.createElement('option');
                        opt.value = device;
                        opt.innerHTML = device;
                        select.appendChild(opt);
                    });
                    document.getElementById('hiddenUser').value = document.getElementById('user').value;
                    document.getElementById('hiddenPass').value = document.getElementById('pass').value;
                    document.getElementById("verifyButton").style.display = "none";
                    document.getElementById('deviceForm').style.display = 'block';
                } else {
                    showModal('Error', 'Error! Please check your Credentials.');
                    verifyInProgress = false;
                    if (btn) { btn.disabled = false; btn.textContent = 'Verify Credentials'; }
                }
            })
            .catch((error) => {
                console.error('Error:', error);
                verifyInProgress = false;
                if (btn) { btn.disabled = false; btn.textContent = 'Verify Credentials'; }
            });
        }
        function handleStoreSubmit(form) {
            if (storeSubmitted) return false;
            storeSubmitted = true;
            var btn = document.getElementById('storeBtn');
            if (btn) { btn.disabled = true; btn.textContent = 'Saving...'; }
            return true;
        }
        </script>
        """
        return render_form(
            "Set your credentials for My JDownloader", verify_form_html, verify_script
        )

    @app.get("/")
    def jd_form():
        def carbon():
            from quasarr.storage.setup.carbon import render_setup_jdownloader

            return render_setup_jdownloader()

        return render_page(
            "setup-jdownloader", carbon, _classic_jd_form, shared_state=shared_state
        )

    @app.post("/api/verify_jdownloader")
    def verify_jdownloader():
        return verify_jdownloader_credentials(shared_state)

    @app.post("/api/store_jdownloader")
    def store_jdownloader():
        return save_jdownloader_settings(shared_state, is_setup=True)

    info(
        f"My-JDownloader-Credentials not set. "
        f'Starting web server for config at: "{shared_state.values["external_address"]}".'
    )
    info("If needed register here: 'https://my.jdownloader.org/login.html#register'")
    info("Please set your credentials now, to allow Quasarr to launch!")
    quasarr.providers.web_server.temp_server_success = False
    return Server(
        app, listen="0.0.0.0", port=shared_state.values["port"]
    ).serve_temporarily()
