#

<img src="https://raw.githubusercontent.com/rix1337/Quasarr/main/Quasarr.png" data-canonical-src="https://raw.githubusercontent.com/rix1337/Quasarr/main/Quasarr.png" width="64" height="64" />

Quasarr connects JDownloader with Radarr, Sonarr, Lidarr and Magazarr. It also handles links protected by
CAPTCHAs.

[![PyPI version](https://badge.fury.io/py/quasarr.svg)](https://badge.fury.io/py/quasarr)
[![Discord](https://img.shields.io/discord/1075348594225315891)](https://discord.gg/eM4zA2wWQb)
[![GitHub Sponsorship](https://img.shields.io/badge/support-me-red.svg)](https://github.com/users/rix1337/sponsorship)

Quasarr pretends to be both `Newznab Indexer` and `SABnzbd client`. Therefore, do not try to use it with real usenet
indexers. It simply does not know what NZB files are.

Quasarr includes a solution to quickly and easily decrypt protected links.
[Active monthly Sponsors get access to SponsorsHelper to do so automatically.](https://github.com/rix1337/Quasarr?tab=readme-ov-file#sponsorshelper)
Alternatively, follow the link from the console output (or notification) to solve CAPTCHAs in your browser.
This requires [Tampermonkey](https://www.tampermonkey.net/) and the matching Quasarr userscript. The CAPTCHA page
guides you through installing both once. Solve the CAPTCHA on the crypter's own page and the userscript sends the
links back, so Quasarr can confidently handle the rest.

If a link crypter's CAPTCHAs become temporarily unsolvable, you can disable it under **Web UI → Link Protection**.
Affected releases then fail so your *arr app grabs an alternative instead of stalling.

# Instructions

1. Set up and run [JDownloader 2](https://jdownloader.org/download/index)
2. Configure the integrations below
3. (Optional) Set up [flaresolverr-next](https://github.com/rix1337/flaresolverr-next) for sites that require it

> **Finding your Quasarr URL and API Key**  
> Both values are shown in the console output under **API Information**, or in the Quasarr web UI.

---

## Quasarr

> ⚠️ Quasarr requires at least one valid hostname to start. It does not provide or endorse any specific sources, but
> community-maintained lists are available:

🔗 **[https://quasarr-hostnames.pages.dev](https://quasarr-hostnames.pages.dev)**: community guide for finding hostnames

📋 Alternatively, browse community suggestions via [pastebin search](https://pastebin.com/search?q=hostnames+quasarr) (
login required).

> Authentication is optional but strongly recommended.
>
> - 🔐 Set `USER` and `PASS` to enable form-based login (30-day session)
> - 🔑 Set `AUTH=basic` to use HTTP Basic Authentication instead

---

## Web UI

Quasarr's Web UI is built on the [Carbon Design System](https://carbondesignsystem.com/) and is the default UI. It supports light and dark themes (toggle in the header, remembered per device) and works down to mobile viewport widths.

<details>
<summary>Screenshots</summary>

Dashboard, light and dark:

<img src="https://raw.githubusercontent.com/rix1337/Quasarr/main/readme-assets/carbon-dashboard-desktop-light.png" width="49%" alt="Dashboard, light theme" /> <img src="https://raw.githubusercontent.com/rix1337/Quasarr/main/readme-assets/carbon-dashboard-desktop-dark.png" width="49%" alt="Dashboard, dark theme" />

Downloads and Settings:

<img src="https://raw.githubusercontent.com/rix1337/Quasarr/main/readme-assets/carbon-downloads-desktop-light.png" width="49%" alt="Downloads" /> <img src="https://raw.githubusercontent.com/rix1337/Quasarr/main/readme-assets/carbon-settings-desktop-light.png" width="49%" alt="Settings" />

Mobile, light and dark:

<img src="https://raw.githubusercontent.com/rix1337/Quasarr/main/readme-assets/carbon-dashboard-mobile-light.png" width="30%" alt="Dashboard, mobile, light theme" /> <img src="https://raw.githubusercontent.com/rix1337/Quasarr/main/readme-assets/carbon-dashboard-mobile-dark.png" width="30%" alt="Dashboard, mobile, dark theme" />

</details>

### Classic UI

The previous UI is still fully supported and easy to reach:

- **For one browser:** click **Switch to Classic UI** in the footer (`/ui/classic`), or **Carbon UI** in Classic's footer to switch back (`/ui/carbon`). Both remember your choice via cookie.
- **For one page load:** append `?ui=classic` (or `?ui=carbon`) to any URL.
- **For the whole instance:** set the `QUASARR_UI=classic` environment variable. This takes precedence over every other setting, including the URL and cookie - useful for Docker deployments or anyone who prefers Classic permanently.
- **For every user, persisted:** `POST /api/ui-preference` with `{"mode": "classic"}` and your API key (`X-Api-Key` header) sets the stored default for everyone without a more specific override.

Carbon falls back to Classic automatically if its static assets are ever missing or fail to render, so a broken deployment never loses access to the Web UI.

### Third-party notices

The Carbon UI bundles:

- [IBM Plex](https://github.com/IBM/plex) fonts, © IBM Corp., licensed under the [SIL Open Font License 1.1](quasarr/static/fonts/LICENSE-IBM-PLEX.txt).
- Icon path data from the [Carbon Design System icons](https://github.com/carbon-design-system/carbon/tree/main/packages/icons), © IBM Corp., licensed under [Apache License 2.0](quasarr/static/icons/LICENSE-APACHE-2.0.txt).

---

## JDownloader

> ⚠️ If using Docker:
> JDownloader's download path must be available to Radarr/Sonarr/Lidarr/Magazarr with **identical internal and
external
path mappings**!
> Matching only the external path is not sufficient.

1. Start and connect JDownloader to [My JDownloader](https://my.jdownloader.org)
2. Provide your My JDownloader credentials during Quasarr setup

<details>
<summary>Fresh install recommended</summary>

Consider setting up a fresh JDownloader instance. Quasarr will modify JDownloader's settings to enable
Radarr/Sonarr/Lidarr/Magazarr integration.

</details>

---

## Categories & Mirrors

You can manage categories in the Quasarr Web UI.

* **Setup:** Add or edit categories to organize your downloads.
* **Download Mirror Whitelist:**
    * Inside a **download category**, you can whitelist specific mirrors.
    * If specific mirrors are set, downloads will fail unless the release is available from them.
    * This does not affect search results.
    * This affects the **Quasarr Download Client** in Radarr/Sonarr/Lidarr and Magazarr.
* **Search Hostname Whitelist:**
    * Inside a **search category**, you can whitelist specific hostnames.
    * If specific hostnames are set, only these will be searched by the given search category.
    * This affects search results.
    * This affects the **Quasarr Newznab Indexer** in Radarr/Sonarr/Lidarr and Magazarr.

---

## Radarr / Sonarr / Lidarr
Add Quasarr as both a **Newznab Indexer** and **SABnzbd Download Client** using your Quasarr URL and API Key.

Be sure to set a category in the **SABnzbd Download client** (default: `movies` for Radarr, `tv` for Sonarr and `music`
for Lidarr).

<details>
<summary>Show download status in Radarr/Sonarr/Lidarr</summary>

**Activity → Queue → Options** → Enable `Release Title`

</details>

---

## Prowlarr

Add Quasarr as a **Generic Newznab Indexer**.

* **Url:** Your Quasarr URL
* **ApiKey:** Your Quasarr API Key

<details>
<summary>Allowed search parameters and categories</summary>

#### Movies / TV:

* Use IMDb ID, Syntax: `{ImdbId:tt0133093}` and pick category `2000` (Movies) or `5000` (TV)
* Simple text search is **not** supported.

#### Music / Books / Magazines:

* Use simple text search and pick category `3000` (Music) or `7000` (Books/Magazines).

</details>

---

## Magazarr

[Magazarr](https://github.com/rix1337/Magazarr) is the magazine companion for Quasarr. It keeps your magazine list,
searches Quasarr, sends selected releases to Quasarr for download, imports completed PDFs from JDownloader folders, and
serves your library through OPDS.

<details>
<summary>Configure Magazarr</summary>

Run Magazarr and open `http://127.0.0.1:8090`:

```bash
docker run -d \
  --name="Magazarr" \
  -p 8090:8090 \
  -v /path/to/magazarr-config:/config:rw \
  -v /path/to/magazine-library:/library:rw \
  -v /path/to/jdownloader-output:/output:rw \
  -e 'TZ'='Europe/Berlin' \
  ghcr.io/rix1337/magazarr:latest
```

Set these values in Magazarr:

| Setting                    | Value                                                                                           |
|----------------------------|-------------------------------------------------------------------------------------------------|
| Quasarr URL                | Your Quasarr URL                                                                                |
| Quasarr API Key            | Your Quasarr API Key                                                                            |
| Search category            | `7000`                                                                                          |
| Download category          | `docs`                                                                                          |
| JDownloader import root    | `/output`, or the same internal path where completed JDownloader packages are visible            |
| Library directory          | `/library`, or wherever Magazarr should store imported PDFs                                      |

Magazarr will import the PDF and clean up the completed package folder.

</details>

---

# Docker

It is highly recommended to run the latest docker image with all optional variables set.

```
docker run -d \
  --name="Quasarr" \
  -p port:8080 \
  -v /path/to/config/:/config:rw \
  -e 'INTERNAL_ADDRESS'='http://192.168.0.1:8080' \
  -e 'EXTERNAL_ADDRESS'='https://foo.bar/' \
  -e 'USER'='admin' \
  -e 'PASS'='change-me' \
  -e 'AUTH'='form' \
  -e 'TZ'='Europe/Berlin' \
  ghcr.io/rix1337/quasarr:latest
  ```

> 🪟 **Windows users:** the single quotes above are for Linux/macOS shells. In Windows `cmd` and PowerShell the quotes are **not** stripped, so they end up as part of the variable name (`'INTERNAL_ADDRESS'` instead of `INTERNAL_ADDRESS`) and Quasarr stops with `You must set the INTERNAL_ADDRESS variable...`. Drop the single quotes and put everything on one line, e.g. `-e INTERNAL_ADDRESS=http://192.168.0.1:8080`.

| Parameter          | Description                                                                                                |
|--------------------|------------------------------------------------------------------------------------------------------------|
| `INTERNAL_ADDRESS` | **Required.** Internal URL so Radarr/Sonarr/Lidarr/Magazarr can reach Quasarr. **Must include port.** |
| `EXTERNAL_ADDRESS` | Optional. External URL (e.g. reverse proxy). Always protect external access with authentication.           |
| `USER` / `PASS`    | Optional, but recommended! Username / Password to protect the web UI.                                      |
| `AUTH`             | Authentication mode. Supported values: `form` or `basic`.                                                  |
| `TZ`               | Optional. Timezone. Incorrect values may cause HTTPS/SSL issues.                                           |
| `FILECRYPT_SWEEP_WINDOW_MINUTES` | Optional. Filecrypt sweep window in minutes (1–1440, default 15). A WebGUI override under **Link Protection** takes precedence over this value; clearing the WebGUI override reverts to this ENV or the default. |

## Filecrypt Link Lifecycle

When SponsorsHelper (v1 protocol) reports a Filecrypt link unavailable (IP block), Quasarr applies a **per-link 24-hour hold** rather than immediately failing the release. A full sweep of at least five links that are all blocked closes with a **global cooldown**; a single reachable link (CLEAR) prevents the global cooldown. Key operator behaviors:

- **Per-link 24 h hold**: each blocked link is held individually. No release fails during its first blocked result.
- **Global cooldown minimum**: requires at least 5 members all blocked. Fewer members get individual holds without a global cooldown.
- **Unlimited denominator**: sweeps are not capped at 100 links. Quasarr handles any number of Filecrypt packages without truncation.
- **CLEAR prevents global cooldown**: one accessible link in a sweep marks the whole linkcrypter as healthy.
- **Second-blocked terminal blacklist**: after a 24 h hold the link is rechecked; a second blocked result permanently blacklists the URL. The release fails exactly once; other releases sharing the URL have their link scrubbed (with alternatives) or fail exactly once (sole link).
- **Arr blocklist matching**: Quasarr publishes stable Newznab publication dates so Radarr/Sonarr/Lidarr can match failed releases in their blocklist. Do not modify the publication date of a failed release manually.
- **Sweep window**: configurable via `FILECRYPT_SWEEP_WINDOW_MINUTES` (ENV) or **Link Protection → Sweep window override** in the WebGUI. Stored WebGUI value takes precedence over ENV; clearing the WebGUI override reverts to ENV or the 15-minute default.

# Manual setup

> Use this only in case you can't run the docker image.

> ⚠️ Requires Python 3.12 (or later) and [uv](https://docs.astral.sh/uv/#installation)!

`uv tool install quasarr`

```
export INTERNAL_ADDRESS=http://192.168.0.1:8080
export EXTERNAL_ADDRESS=https://foo.bar/
quasarr
  ```

* `EXTERNAL_ADDRESS` see `EXTERNAL_ADDRESS` docker variable
  
# Notifications
Configure notifications in **Web UI → Notifications Configuration**:
- Set provider credentials
- Choose notification types per provider
- *Silent* notifications won't play any sound

## Discord

<details>
<summary>Configure Discord</summary>

1. Open your Discord server and go to **Server Settings → Integrations → Webhooks**.
2. Click **New Webhook**, choose the target channel, and copy the **Webhook URL**.
3. Open Quasarr UI and go to **Notifications**.
4. Paste the webhook URL into **Discord → Webhook URL**.
5. Click **Save Notification Settings** and then **Send Discord Test**.

</details>

## Telegram

<details>
<summary>Configure Telegram Bot</summary>

1. **Create a bot**: Open Telegram and search for [@BotFather](https://t.me/BotFather). Send `/newbot` and follow the prompts to choose a name and username for your bot.
2. **Copy the token**: BotFather will reply with an HTTP API token (e.g. `123456789:ABCdefGHI...`). This is your `TELEGRAM_BOT_TOKEN`.
3. **Start a chat with the bot**: Open a chat with your new bot and send any message (e.g. `/start`). This is required so the bot can send messages back to you.
4. **Get your chat ID**: Open the following URL in a browser (replace `<TOKEN>` with your bot token):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
   Look for `"chat":{"id":` in the JSON response. That number is your `TELEGRAM_CHAT_ID`.
   > **Tip:** For a group chat, add the bot to the group first, send a message in the group, then call `getUpdates`.
5. **Configure Quasarr**: Open **Notifications** in Quasarr UI.
6. Paste both values into **Telegram → Bot Token / Chat ID**.
7. Click **Save Notification Settings** and then **Send Telegram Test**.
  
</details>
<br>

# Philosophy

Complexity is the killer of small projects like this one. It must be fought at all cost!

We will not waste precious time on features that will slow future development cycles down.
Most feature requests can be satisfied by:

- Existing settings in Radarr/Sonarr/Lidarr/Magazarr
- Existing settings in JDownloader
- Existing tools from the *arr ecosystem that integrate directly with Radarr/Sonarr/Lidarr/Magazarr

# Roadmap

- Assume there are zero known
  issues [unless you find one or more open issues in this repository](https://github.com/rix1337/Quasarr/issues).
- Still having an issue? Provide a detailed report [here](https://github.com/rix1337/Quasarr/issues/new/choose)!
- There are no hostname integrations in active development unless you see an open pull request
  [here](https://github.com/rix1337/Quasarr/pulls).
- **Pull requests are welcome!** Especially for popular hostnames.
    - Start with [AGENTS.md](AGENTS.md) for repository guidelines: development setup,
      local run commands, tests, linting, and commit/PR conventions.
      Development environment setup for pull requests also lives in [CONTRIBUTING.md](CONTRIBUTING.md).
    - Always reach out on Discord before starting work on a new feature to prevent waste of time.
    - Please follow the existing code style and project structure.
    - CAPTCHA solving for new link crypters is done via Tampermonkey userscripts. You will need to provide a working
      userscript that integrates with the Quasarr Web UI's CAPTCHA flow.
    - Please provide proof of functionality (screenshots/examples) when submitting your pull request.

# SponsorsHelper

<img src="https://imgur.com/iHBqLwT.png" width="64" height="64" />

SponsorsHelper is a Docker image that solves CAPTCHAs and decrypts links for Quasarr.  
The image is public, but requires a valid [active monthly GitHub sponsor](https://github.com/users/rix1337/sponsorship) check at runtime.

[![Github Sponsorship](https://img.shields.io/badge/support-me-red.svg)](https://github.com/users/rix1337/sponsorship)

---

## 🔐 Quasarr API Key Setup

1. Open your Quasarr web UI in a browser
2. On the main page, expand **"Show API Settings"**
3. Copy the **API Key** value
4. Use this value for the `QUASARR_API_KEY` environment variable

> **Note:** The API key is required for SponsorsHelper to communicate securely with Quasarr. Without it, all requests
> will be rejected with a 401/403 error.

---

## 🔑 GitHub Auth

> ⚠️ Mount `/config` to persistent storage. Otherwise the auth token is lost on container recreation/update.

1. Start your [sponsorship](https://github.com/users/rix1337/sponsorship).
2. Start SponsorsHelper.
3. In container logs, open the GitHub authorization URL shown by SponsorsHelper.
4. Confirm the authorization in browser.
5. Done. SponsorsHelper caches the auth token in `/config/github_oauth_token.json`.

---

## ▶️ Run SponsorsHelper

```bash
docker run -d \
  --name='SponsorsHelper' \
  -v '/path/to/sponsorshelper-config:/config' \
  -e 'QUASARR_URL'='http://192.168.0.1:8080' \
  -e 'QUASARR_API_KEY'='your_quasarr_api_key_here' \
  -e 'FLARESOLVERR_URL'='http://192.168.0.1:8191/v1' \
  -e 'APIKEY_2CAPTCHA'='your_2captcha_api_key_here' \
  -e 'TZ'='Europe/Berlin' \
  ghcr.io/rix1337/sponsors-helper:latest
```

| Parameter              | Description                                                                           |
|------------------------|---------------------------------------------------------------------------------------|
| `QUASARR_URL`          | Local URL of Quasarr (e.g., `http://192.168.0.1:8080`)                                |
| `QUASARR_API_KEY`      | Your Quasarr API key (found in Quasarr web UI under "API Settings")                   |
| `FLARESOLVERR_URL`     | Local URL of [flaresolverr-next](https://github.com/rix1337/flaresolverr-next)        |
| `APIKEY_2CAPTCHA`      | [2Captcha](https://2captcha.com/?from=27506687) account API key                       |
| `TZ`                   | Optional. Timezone for SponsorsHelper (e.g., `Europe/Berlin`)                           |
