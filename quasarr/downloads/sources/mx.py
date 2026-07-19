# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337
#
# MX: download twin.
# Original contribution by Riourik (https://github.com/riourik), PR #360.

from quasarr.downloads.mirror_filters import normalize_mirror_token
from quasarr.downloads.sources.helpers.abstract_source import AbstractDownloadSource
from quasarr.providers.hostname_issues import clear_hostname_issue
from quasarr.providers.log import debug


class Source(AbstractDownloadSource):
    initials = "mx"

    def get_download_links(self, shared_state, url, mirrors, title, password):
        # The search side already decoded the final hoster URL (1Fichier, Send,
        # ...) into the payload, so there is nothing to fetch here.
        if not url:
            return {"links": []}

        # Match the mirror whitelist on the canonical hoster token (the same
        # normalization the final filter uses), so aliased hoster domains are
        # not dropped before they reach it.
        token = normalize_mirror_token(url)
        if mirrors and token not in {normalize_mirror_token(m) for m in mirrors}:
            debug(f"[mx] {token or 'unknown'} not in requested mirrors for {title}")
            return {"links": []}

        debug(f"[mx] download link: {url}")
        clear_hostname_issue(self.initials)
        return {"links": [[url, token or "unknown"]]}
