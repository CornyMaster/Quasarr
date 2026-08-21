# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

from quasarr.constants import ARCHIVE_EXTENSIONS
from quasarr.providers.log import trace
from quasarr.providers.myjd_api import (
    MYJDException,
    RequestTimeoutException,
    TokenExpiredException,
)

# A JDownloader query either answers with data or fails, and two of its failure
# shapes used to escape this class as an HTTP 500 on every page that reads the
# cache. `shared_state` can hold a configured device NAME where a connected
# client is expected, so the first attribute lookup raises `AttributeError`
# rather than the MyJD errors below; and `query_packages()`/`query_links()`
# answer `False` instead of a list when the device refuses the call, which makes
# the `len()` in the success trace raise `TypeError`. Neither says anything
# except "nothing is known right now", so neither may take a page down.
_QUERY_FAILURES = (
    TokenExpiredException,
    RequestTimeoutException,
    MYJDException,
    AttributeError,
    TypeError,
)


class JDPackageCache:
    """
    Caches JDownloader package/link queries within a single request.

    IMPORTANT: This cache is ONLY valid for the duration of ONE get_packages()
    or delete_package() call. JDownloader state can be modified at any time by
    the user or third-party tools, so cached data must NEVER persist across
    separate requests.

    This reduces redundant API calls within a single operation where the same
    data (e.g., linkgrabber_links) is needed multiple times.
    """

    def __init__(self, device):
        trace("Initializing new cache instance")
        self._device = device
        self._linkgrabber_packages = None
        self._linkgrabber_links = None
        self._downloader_packages = None
        self._downloader_links = None
        self._archive_cache = {}  # package_uuid -> bool (is_archive)
        self._is_collecting = None
        # Stats tracking
        self._api_calls = 0
        self._cache_hits = 0

    def get_stats(self):
        """Return cache statistics string."""
        pkg_count = len(self._downloader_packages or []) + len(
            self._linkgrabber_packages or []
        )
        link_count = len(self._downloader_links or []) + len(
            self._linkgrabber_links or []
        )
        return f"{self._api_calls} API calls | {pkg_count} packages, {link_count} links cached"

    def _query(self, label, call, default):
        """Answer one JDownloader query, or `default` when it cannot be made.

        Every caller of this cache renders a page or an API response, so a
        device that is missing, stale, or answering the wrong shape has to
        degrade to "nothing known" instead of propagating.
        """
        self._api_calls += 1
        try:
            value = call()
        except _QUERY_FAILURES as e:
            trace(f"Failed to fetch {label}: {e}")
            return default
        if isinstance(default, list) and not isinstance(value, list):
            trace(f"{label} answered {type(value).__name__} instead of a list")
            return default
        return value

    @property
    def linkgrabber_packages(self):
        if self._linkgrabber_packages is None:
            trace("Fetching linkgrabber_packages from API")
            self._linkgrabber_packages = self._query(
                "linkgrabber_packages",
                lambda: self._device.linkgrabber.query_packages(),
                [],
            )
            trace(f"Retrieved {len(self._linkgrabber_packages)} linkgrabber packages")
        else:
            self._cache_hits += 1
            trace(
                f"Using cached linkgrabber_packages ({len(self._linkgrabber_packages)} packages)"
            )
        return self._linkgrabber_packages

    @property
    def linkgrabber_links(self):
        if self._linkgrabber_links is None:
            trace("Fetching linkgrabber_links from API")
            self._linkgrabber_links = self._query(
                "linkgrabber_links",
                lambda: self._device.linkgrabber.query_links(),
                [],
            )
            trace(f"Retrieved {len(self._linkgrabber_links)} linkgrabber links")
        else:
            self._cache_hits += 1
            trace(
                f"Using cached linkgrabber_links ({len(self._linkgrabber_links)} links)"
            )
        return self._linkgrabber_links

    @property
    def downloader_packages(self):
        if self._downloader_packages is None:
            trace("Fetching downloader_packages from API")
            self._downloader_packages = self._query(
                "downloader_packages",
                lambda: self._device.downloads.query_packages(),
                [],
            )
            trace(f"Retrieved {len(self._downloader_packages)} downloader packages")
        else:
            self._cache_hits += 1
            trace(
                f"Using cached downloader_packages ({len(self._downloader_packages)} packages)"
            )
        return self._downloader_packages

    @property
    def downloader_links(self):
        if self._downloader_links is None:
            trace("Fetching downloader_links from API")
            self._downloader_links = self._query(
                "downloader_links",
                lambda: self._device.downloads.query_links(),
                [],
            )
            trace(f"Retrieved {len(self._downloader_links)} downloader links")
        else:
            self._cache_hits += 1
            trace(
                f"Using cached downloader_links ({len(self._downloader_links)} links)"
            )
        return self._downloader_links

    @property
    def is_collecting(self):
        if self._is_collecting is None:
            trace("Checking is_collecting from API")
            self._is_collecting = self._query(
                "is_collecting",
                lambda: self._device.linkgrabber.is_collecting(),
                False,
            )
            trace(f"is_collecting = {self._is_collecting}")
        else:
            self._cache_hits += 1
            trace(f"Using cached is_collecting = {self._is_collecting}")
        return self._is_collecting

    def _has_archive_extension(self, package_uuid, links):
        """Check if any link in the package has an archive file extension."""
        for link in links:
            if link.get("packageUUID") != package_uuid:
                continue
            name = link.get("name", "")
            name_lower = name.lower()
            for ext in ARCHIVE_EXTENSIONS:
                if name_lower.endswith(ext):
                    trace(
                        f"Found archive extension '{ext}' in file '{name}' for package {package_uuid}"
                    )
                    return True
        return False

    def _bulk_detect_archives(self, package_uuids):
        """
        Detect archives for multiple packages in ONE API call.

        Returns:
            tuple: (confirmed_archives: set, api_succeeded: bool)
            - confirmed_archives: Package UUIDs confirmed as archives
            - api_succeeded: Whether the API call worked (for fallback decisions)
        """
        confirmed_archives = set()

        if not package_uuids:
            trace("_bulk_detect_archives called with empty package_uuids")
            return confirmed_archives, True

        package_list = list(package_uuids)
        trace(f"Bulk archive detection for {len(package_list)} packages")

        try:
            self._api_calls += 1
            archive_infos = self._device.extraction.get_archive_info([], package_list)
            trace(
                f"get_archive_info returned {len(archive_infos) if archive_infos else 0} results"
            )

            if archive_infos:
                for i, archive_info in enumerate(archive_infos):
                    if archive_info:
                        trace(f"archive_info[{i}] = {archive_info}")
                        # Try to get packageUUID from response
                        pkg_uuid = archive_info.get("packageUUID")
                        if pkg_uuid:
                            trace(f"Confirmed archive via packageUUID: {pkg_uuid}")
                            confirmed_archives.add(pkg_uuid)
                        else:
                            # Log what fields ARE available for debugging
                            trace(
                                f"archive_info has no packageUUID, available keys: {list(archive_info.keys())}"
                            )
                    else:
                        trace(f"archive_info[{i}] is empty/None")

            trace(
                f"Bulk detection confirmed {len(confirmed_archives)} archives: {confirmed_archives}"
            )
            return confirmed_archives, True

        except Exception as e:
            trace(f"Bulk archive detection API FAILED: {type(e).__name__}: {e}")
            return confirmed_archives, False

    def detect_all_archives(self, packages, links):
        """
        Detect archives for all packages efficiently.

        Uses ONE bulk API call, then applies safety fallbacks for packages
        where detection was uncertain.

        Args:
            packages: List of downloader packages
            links: List of downloader links (for extension fallback)

        Returns:
            Set of package UUIDs that should be treated as archives
        """
        if not packages:
            trace("detect_all_archives called with no packages")
            return set()

        all_package_uuids = {p.get("uuid") for p in packages if p.get("uuid")}
        trace(f"detect_all_archives for {len(all_package_uuids)} packages")

        # ONE bulk API call for all packages
        confirmed_archives, api_succeeded = self._bulk_detect_archives(
            all_package_uuids
        )
        trace(
            f"Bulk API succeeded={api_succeeded}, confirmed={len(confirmed_archives)} archives"
        )

        # For packages NOT confirmed as archives, apply safety fallbacks
        unconfirmed = all_package_uuids - confirmed_archives
        trace(f"{len(unconfirmed)} packages need fallback checking")

        for pkg_uuid in unconfirmed:
            # Fallback 1: Check file extensions
            if self._has_archive_extension(pkg_uuid, links):
                trace(f"Package {pkg_uuid} confirmed as archive via extension fallback")
                confirmed_archives.add(pkg_uuid)
            # Fallback 2: If bulk API failed completely, assume archive (safe)
            elif not api_succeeded:
                trace(
                    f"SAFETY - Bulk API failed, assuming package {pkg_uuid} is archive"
                )
                confirmed_archives.add(pkg_uuid)
            else:
                trace(
                    f"Package {pkg_uuid} confirmed as NON-archive (API worked, no extension match)"
                )

        # Cache results for is_package_archive() lookups
        for pkg_uuid in all_package_uuids:
            self._archive_cache[pkg_uuid] = pkg_uuid in confirmed_archives

        trace(
            f"Final archive detection: {len(confirmed_archives)}/{len(all_package_uuids)} packages are archives"
        )
        return confirmed_archives

    def is_package_archive(self, package_uuid, links=None):
        """
        Check if a package contains archive files.

        Prefer calling detect_all_archives() first for efficiency.
        This method is for single lookups or cache hits.

        SAFETY: On API error, defaults to True (assume archive) to prevent
        premature "finished" status.
        """
        if package_uuid is None:
            trace("is_package_archive called with None UUID")
            return False

        if package_uuid in self._archive_cache:
            self._cache_hits += 1
            cached = self._archive_cache[package_uuid]
            trace(f"is_package_archive({package_uuid}) = {cached} (cached)")
            return cached

        trace(f"is_package_archive({package_uuid}) - cache miss, querying API")

        # Single package lookup (fallback if detect_all_archives wasn't called)
        is_archive = None
        api_failed = False

        try:
            self._api_calls += 1
            archive_info = self._device.extraction.get_archive_info([], [package_uuid])
            trace(f"Single get_archive_info returned: {archive_info}")
            # Original logic: is_archive = True if archive_info and archive_info[0] else False
            is_archive = True if archive_info and archive_info[0] else False
            trace(f"API says is_archive = {is_archive}")
        except Exception as e:
            api_failed = True
            trace(
                f"Single archive detection API FAILED for {package_uuid}: {type(e).__name__}: {e}"
            )

        # Fallback: check file extensions if API failed or returned False
        if (api_failed or not is_archive) and links:
            if self._has_archive_extension(package_uuid, links):
                trace(
                    f"Package {package_uuid} confirmed as archive via extension fallback"
                )
                is_archive = True

        # SAFETY: If API failed and no extension detected, assume archive (conservative)
        if is_archive is None:
            trace(f"SAFETY - Detection uncertain for {package_uuid}, assuming archive")
            is_archive = True

        self._archive_cache[package_uuid] = is_archive
        trace(f"is_package_archive({package_uuid}) = {is_archive} (final)")
        return is_archive
