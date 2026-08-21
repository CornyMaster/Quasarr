# -*- coding: utf-8 -*-
# Quasarr
# Project by https://github.com/rix1337

"""Contracts for `quasarr.providers.jd_cache.JDPackageCache`.

The cache is read while rendering the Downloads page, the dashboard queue
tile and `GET /api/packages/list`, which the Carbon UI polls every five
seconds. A JDownloader that is missing, stale, or answering the wrong shape
therefore may not propagate out of this class - it has to degrade to
"nothing known" so the page still renders.

Two failure shapes were observed in production as HTTP 500s and are pinned
here: `shared_state` holding a configured device NAME where a connected
client is expected (`AttributeError: 'str' object has no attribute
'linkgrabber'`), and `query_packages()` answering `False` rather than a list
(`TypeError: object of type 'bool' has no len()`).
"""

import unittest

from quasarr.providers.jd_cache import JDPackageCache
from quasarr.providers.myjd_api import (
    MYJDException,
    RequestTimeoutException,
    TokenExpiredException,
)

LIST_PROPERTIES = (
    "linkgrabber_packages",
    "linkgrabber_links",
    "downloader_packages",
    "downloader_links",
)


class _Endpoint:
    """One JDownloader namespace whose calls all answer the same way."""

    def __init__(self, answer):
        self._answer = answer

    def _respond(self, *_args, **_kwargs):
        if isinstance(self._answer, BaseException):
            raise self._answer
        return self._answer

    query_packages = _respond
    query_links = _respond
    is_collecting = _respond


class _Device:
    def __init__(self, answer):
        self.linkgrabber = _Endpoint(answer)
        self.downloads = _Endpoint(answer)


class _CountingEndpoint(_Endpoint):
    def __init__(self, answer):
        super().__init__(answer)
        self.calls = 0

    def _respond(self, *_args, **_kwargs):
        self.calls += 1
        return super()._respond()

    query_packages = _respond
    query_links = _respond
    is_collecting = _respond


class DegradedDeviceTests(unittest.TestCase):
    def test_a_device_name_instead_of_a_device_answers_empty(self):
        """The exact production failure: `shared_state` held a string."""
        cache = JDPackageCache("JDownloader@unRaid")

        for name in LIST_PROPERTIES:
            with self.subTest(property=name):
                self.assertEqual([], getattr(cache, name))

    def test_a_device_name_instead_of_a_device_is_not_collecting(self):
        cache = JDPackageCache("JDownloader@unRaid")

        self.assertFalse(cache.is_collecting)

    def test_a_query_answering_false_is_treated_as_no_data(self):
        """The second production failure: `len(False)` raised TypeError."""
        cache = JDPackageCache(_Device(False))

        for name in LIST_PROPERTIES:
            with self.subTest(property=name):
                self.assertEqual([], getattr(cache, name))

    def test_a_query_answering_none_is_treated_as_no_data(self):
        cache = JDPackageCache(_Device(None))

        for name in LIST_PROPERTIES:
            with self.subTest(property=name):
                self.assertEqual([], getattr(cache, name))

    def test_myjd_failures_still_degrade_to_no_data(self):
        """Pre-existing behaviour, kept: the named MyJD errors are handled."""
        for failure in (
            MYJDException("device offline"),
            TokenExpiredException("token expired"),
            RequestTimeoutException("timed out"),
        ):
            for name in LIST_PROPERTIES:
                with self.subTest(failure=type(failure).__name__, property=name):
                    cache = JDPackageCache(_Device(failure))
                    self.assertEqual([], getattr(cache, name))

    def test_stats_render_after_a_degraded_query(self):
        """`get_stats()` is logged on the same paths that read the cache."""
        cache = JDPackageCache("JDownloader@unRaid")
        for name in LIST_PROPERTIES:
            getattr(cache, name)

        self.assertIn("0 packages, 0 links cached", cache.get_stats())


class HealthyDeviceTests(unittest.TestCase):
    def test_a_real_answer_is_returned_unchanged(self):
        packages = [{"uuid": 1, "name": "Synthetic.Release.2024.German.1080p-GRP"}]
        cache = JDPackageCache(_Device(packages))

        self.assertEqual(packages, cache.linkgrabber_packages)

    def test_is_collecting_is_returned_unchanged(self):
        cache = JDPackageCache(_Device(True))

        self.assertTrue(cache.is_collecting)

    def test_one_query_per_property_within_one_cache_lifetime(self):
        """The whole point of the cache: a second read must not call out."""
        device = _Device([])
        device.linkgrabber = _CountingEndpoint([])
        cache = JDPackageCache(device)

        first = cache.linkgrabber_packages
        second = cache.linkgrabber_packages

        self.assertEqual(1, device.linkgrabber.calls)
        self.assertIs(first, second)

    def test_a_degraded_query_still_counts_as_one_api_call(self):
        cache = JDPackageCache("JDownloader@unRaid")

        self.assertEqual([], cache.linkgrabber_packages)

        self.assertIn("1 API calls", cache.get_stats())


if __name__ == "__main__":
    unittest.main()
