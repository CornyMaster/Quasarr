# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List

import requests

from quasarr.constants import DOWNLOAD_REQUEST_TIMEOUT_SECONDS
from quasarr.providers.log import debug, info
from quasarr.providers.statistics import StatsHelper


def unhide_links(shared_state, url, session, outcome=None):
    """Decrypt one hide.cx container into its final hoster links.

    `outcome` is an optional dict the caller may pass to learn WHY an empty
    result came back. It is set to `{"gone": True}` only when hide.cx itself
    answers 404 - its explicit "container not found or invalid" - because
    that is the one failure a human cannot beat either: there is nothing left
    to solve. Every other failure (5xx, an unreadable body, an unexpected
    shape) stays unmarked, so the caller keeps demoting those to a manual
    CAPTCHA where a person may still succeed.
    """
    if outcome is None:
        outcome = {}
    try:
        links = []

        # Support both formats:
        # - https://hide.cx/container/{id}
        # - https://hide.cx/fc/Container/{id}.html
        match = re.search(
            r"/(?:fc/)?container/([a-z0-9A-Z\-]+)(?:\.html)?",
            url,
            re.IGNORECASE,
        )

        if not match:
            info(f"Invalid hide.cx URL: {url}")
            return []

        container_id = match.group(1)
        is_fc = "/fc/" in url.lower()
        # resolve fc foreign ID to canonical container ID
        if is_fc:
            headers = {
                "User-Agent": shared_state.values["user_agent"],
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            }

            info(f"Resolving hide.cx foreign container ID: {container_id}")
            resolve_url = f"https://api.hide.cx/fc/Container/{container_id}"
            resp = session.get(
                resolve_url,
                headers=headers,
                timeout=DOWNLOAD_REQUEST_TIMEOUT_SECONDS,
            )

            if resp.status_code == 404:
                # hide.cx's own answer, verbatim: "container not found or
                # invalid". Logged at INFO because the operator otherwise
                # only sees "Could not decrypt any links" and is left to
                # guess whether the link is dead or the solve merely failed.
                outcome["gone"] = True
                info(
                    f"hide.cx reports container {container_id} as gone "
                    f"(HTTP 404); it can no longer be solved by anyone"
                )
                return []

            try:
                resolved = resp.json()
            except Exception:
                info(
                    f"Failed to resolve foreign container {container_id} "
                    f"(HTTP {resp.status_code}, unreadable body)"
                )
                return []

            canonical_id = resolved.get("id")
            if not canonical_id:
                info(
                    f"No canonical container ID for {container_id} "
                    f"(HTTP {resp.status_code})"
                )
                return []

            container_id = canonical_id
            debug(f"Resolved to canonical container ID: {container_id}")

        headers = {"User-Agent": shared_state.values["user_agent"]}
        info(f"Fetching hide.cx container with ID: {container_id}")

        headers = {"User-Agent": shared_state.values["user_agent"]}

        container_url = f"https://api.hide.cx/containers/{container_id}"
        response = session.get(
            container_url,
            headers=headers,
            timeout=DOWNLOAD_REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code == 404:
            outcome["gone"] = True
            info(
                f"hide.cx reports container {container_id} as gone "
                f"(HTTP 404); it can no longer be solved by anyone"
            )
            return []

        data = response.json()

        link_ids = [link.get("id") for link in data.get("links", []) if link.get("id")]

        if not link_ids:
            debug(f"No link IDs found in container {container_id}")
            return []

        def fetch_link(link_id):
            debug(f"Fetching hide.cx link with ID: {link_id}")
            link_url = f"https://api.hide.cx/containers/{container_id}/links/{link_id}"
            link_data = session.get(
                link_url,
                headers=headers,
                timeout=DOWNLOAD_REQUEST_TIMEOUT_SECONDS,
            ).json()
            return link_data.get("url")

        # Process links in batches of 10
        batch_size = 10
        for i in range(0, len(link_ids), batch_size):
            batch = link_ids[i : i + batch_size]
            with ThreadPoolExecutor(max_workers=batch_size) as executor:
                futures = [executor.submit(fetch_link, link_id) for link_id in batch]
                for future in as_completed(futures):
                    try:
                        final_url = future.result()
                        if final_url and final_url not in links:
                            links.append(final_url)
                    except Exception as e:
                        info(f"Error fetching link: {e}")

        success = bool(links)
        if success:
            StatsHelper(shared_state).increment_captcha_decryptions_automatic()
        else:
            StatsHelper(shared_state).increment_failed_decryptions_automatic()

        return links
    except Exception as e:
        info(f"Error fetching hide.cx links: {e}")
        StatsHelper(shared_state).increment_failed_decryptions_automatic()
        return []


def decrypt_links_if_hide(shared_state: Any, items: List[List[str]]) -> Dict[str, Any]:
    """
    Resolve redirects and decrypt hide.cx links from a list of item lists.

    Each item list must include:
      - index 0: the URL to resolve
      - any additional metadata at subsequent indices (ignored here)

    :param shared_state: State object required by unhide_links function
    :param items: List of lists, where each inner list has the URL at index 0
    :return: Dict with 'status' and 'results' (flat list of decrypted link URLs)
    """
    if not items:
        info("No items provided to decrypt.")
        return {"status": "error", "results": []}

    session = requests.Session()
    session.max_redirects = 5

    hide_urls: List[str] = []
    for item in items:
        original_url = item[0]
        if not original_url:
            debug(f"Skipping item without URL: {item}")
            continue

        try:
            # Try HEAD first, fallback to GET
            try:
                resp = session.head(
                    original_url,
                    allow_redirects=True,
                    timeout=DOWNLOAD_REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException:
                resp = session.get(
                    original_url,
                    allow_redirects=True,
                    timeout=DOWNLOAD_REQUEST_TIMEOUT_SECONDS,
                )

            final_url = resp.url

            # accept hide.cx even if it did not redirect
            if "hide.cx" in final_url or "hide.cx" in original_url:
                debug(f"Identified hide.cx link: {final_url}")
                hide_urls.append(final_url)
            else:
                debug(f"Not a hide.cx link (skipped): {final_url}")

        except requests.RequestException as e:
            info(f"Error resolving URL {original_url}: {e}")
            continue

    if not hide_urls:
        debug(f"No hide.cx links found among {len(items)} items.")
        return {"status": "none", "results": []}

    info(f"Found {len(hide_urls)} hide.cx URLs; decrypting...")
    decrypted_links: List[str] = []
    gone_count = 0
    for url in hide_urls:
        try:
            outcome: Dict[str, Any] = {}
            links = unhide_links(shared_state, url, session, outcome=outcome)
            if not links:
                if outcome.get("gone"):
                    gone_count += 1
                else:
                    debug(f"No links decrypted for {url}")
                continue
            decrypted_links.extend(links)
        except Exception as e:
            info(f"Failed to decrypt {url}: {e}")
            continue

    if not decrypted_links:
        # "gone" only when EVERY container was reported missing. If even one
        # failed for another reason it may still be solvable by hand, and the
        # package must keep its manual CAPTCHA route.
        if gone_count and gone_count == len(hide_urls):
            info("Every hide.cx container is gone; nothing left to solve.")
            return {"status": "gone", "results": []}
        info("Could not decrypt any links from hide.cx URLs.")
        return {"status": "error", "results": []}

    return {"status": "success", "results": decrypted_links}
