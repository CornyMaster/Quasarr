# -*- coding: utf-8 -*-

"""One navigation chain per resolution, never one per solver session.

The chain lives in ``window.name`` because that survives cross-origin
navigation - but it also survives every other navigation in the same tab, and
a FlareSolverr session reuses one tab for everything sent through it. The
original recorder appended unconditionally, so the chain grew across
resolutions and ``first_crypter_in_chain()`` kept answering the first crypter
the session had ever walked.

Measured on a live instance before the fix: one release offered four
link-protection redirects leading to four distinct containers (two FileCrypt,
two Hide), and all four resolved to the first one. Every alternative container
was lost, and the stored link did not even belong to the mirror the whitelist
had selected.
"""

import json
import unittest

from quasarr.providers.utils import (
    first_crypter_in_chain,
    navigation_chain_recorder_js,
    new_navigation_chain_token,
)

CRYPTER_A = "https://filecrypt.invalid/Container/AAAA.html"
CRYPTER_B = "https://filecrypt.invalid/Container/BBBB.html"
SOURCE_HOP = "https://source.invalid/mirror/1/direct"
AD_HOP = "https://ad.invalid/p/landing.html"


def envelope(token, chain):
    """What the recorder leaves in window.name."""
    return json.dumps({"t": token, "c": chain})


class ChainTokenTests(unittest.TestCase):
    def test_every_token_is_unique(self):
        tokens = {new_navigation_chain_token() for _ in range(50)}
        self.assertEqual(50, len(tokens))
        self.assertTrue(all(isinstance(t, str) and t for t in tokens))

    def test_the_recorder_carries_its_token_and_can_restart_a_chain(self):
        js = navigation_chain_recorder_js("tok-1")
        self.assertIn('"tok-1"', js)
        # The reset branch is what stops a foreign chain being extended.
        self.assertIn("s.t!==t", js)
        self.assertIn("s={t:t,c:[]}", js)
        self.assertIn("s.c.push(location.href)", js)

    def test_a_token_is_embedded_as_an_escaped_string_literal(self):
        # The token reaches the page as JS source, so it is embedded through
        # json.dumps rather than concatenated raw - a token containing a quote
        # cannot close the literal and start running.
        hostile = '"; alert(1); var x="'
        js = navigation_chain_recorder_js(hostile)
        self.assertIn(json.dumps(hostile), js)
        self.assertNotIn('var t="";', js)


class FirstCrypterInChainTests(unittest.TestCase):
    def test_a_matching_token_reads_the_chain(self):
        chain = envelope("tok", [SOURCE_HOP, CRYPTER_A, AD_HOP])
        self.assertEqual(CRYPTER_A, first_crypter_in_chain(chain, token="tok"))

    def test_a_foreign_token_is_refused(self):
        # THE BUG: this chain belongs to the previous resolution.
        chain = envelope("previous", [SOURCE_HOP, CRYPTER_A])
        self.assertIsNone(first_crypter_in_chain(chain, token="current"))

    def test_an_unstamped_chain_is_refused_while_a_token_is_expected(self):
        # No identity, so no owner can be established - a page may have written
        # window.name itself.
        chain = json.dumps([SOURCE_HOP, CRYPTER_A])
        self.assertIsNone(first_crypter_in_chain(chain, token="tok"))

    def test_the_ad_after_the_crypter_is_still_ignored(self):
        # Quasarr#419 must keep working through the envelope.
        chain = envelope("tok", [SOURCE_HOP, CRYPTER_A, AD_HOP])
        self.assertEqual(CRYPTER_A, first_crypter_in_chain(chain, token="tok"))

    def test_a_chain_without_a_crypter_answers_none(self):
        chain = envelope("tok", [SOURCE_HOP, AD_HOP])
        self.assertIsNone(first_crypter_in_chain(chain, token="tok"))

    def test_missing_or_malformed_input_answers_none(self):
        for value in (None, "", "not-json", json.dumps({"t": "tok"}), json.dumps(5)):
            with self.subTest(value=value):
                self.assertIsNone(first_crypter_in_chain(value, token="tok"))

    def test_without_a_token_the_bare_list_shape_is_still_read(self):
        # A caller that records no chain of its own keeps its old behaviour.
        chain = json.dumps([SOURCE_HOP, CRYPTER_A])
        self.assertEqual(CRYPTER_A, first_crypter_in_chain(chain))


class SessionReuseRegressionTests(unittest.TestCase):
    """Two resolutions through one solver tab must not share an answer."""

    class Tab:
        """A solver tab: window.name persists, exactly as the real one does."""

        def __init__(self):
            self.window_name = ""

        def visit(self, recorder_js, hops):
            """Apply the recorder's logic once per document, then read it back."""
            token = json.loads(recorder_js.split("var t=", 1)[1].split(";", 1)[0])
            for hop in hops:
                try:
                    state = json.loads(self.window_name or "null")
                except ValueError:
                    state = None
                if (
                    not isinstance(state, dict)
                    or state.get("t") != token
                    or not isinstance(state.get("c"), list)
                ):
                    state = {"t": token, "c": []}
                state["c"].append(hop)
                self.window_name = json.dumps(state)
            return self.window_name

    def test_the_second_resolution_never_inherits_the_first_crypter(self):
        tab = self.Tab()

        token_one = new_navigation_chain_token()
        read_one = tab.visit(
            navigation_chain_recorder_js(token_one), [SOURCE_HOP, CRYPTER_A]
        )
        self.assertEqual(CRYPTER_A, first_crypter_in_chain(read_one, token=token_one))

        # Same tab, so window.name still holds the first chain.
        token_two = new_navigation_chain_token()
        read_two = tab.visit(
            navigation_chain_recorder_js(token_two), [SOURCE_HOP, CRYPTER_B]
        )

        self.assertEqual(CRYPTER_B, first_crypter_in_chain(read_two, token=token_two))
        self.assertNotIn(CRYPTER_A, read_two)

    def test_a_resolution_that_died_before_being_read_cannot_leak(self):
        # The self-healing property: no cleanup has to have run.
        tab = self.Tab()
        tab.visit(
            navigation_chain_recorder_js(new_navigation_chain_token()), [CRYPTER_A]
        )

        token = new_navigation_chain_token()
        read = tab.visit(navigation_chain_recorder_js(token), [SOURCE_HOP, CRYPTER_B])

        self.assertEqual(CRYPTER_B, first_crypter_in_chain(read, token=token))

    def test_hops_of_one_resolution_still_share_a_chain(self):
        # resolve_crypter_redirect() may drive the browser several times for a
        # single resolution; those hops must accumulate, not reset each other.
        tab = self.Tab()
        token = new_navigation_chain_token()
        recorder = navigation_chain_recorder_js(token)

        tab.visit(recorder, [SOURCE_HOP])
        read = tab.visit(recorder, [CRYPTER_A])

        self.assertEqual(CRYPTER_A, first_crypter_in_chain(read, token=token))
        self.assertEqual(2, len(json.loads(read)["c"]))


if __name__ == "__main__":
    unittest.main()
