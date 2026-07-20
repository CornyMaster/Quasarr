# -*- coding: utf-8 -*-

import configparser
import multiprocessing
import os
import tempfile
import unittest

from quasarr.providers import shared_state
from quasarr.storage.config import Config

SECTIONS = [
    "Hostnames",
    "JUNKIES",
    "API",
    "FlareSolverr",
    "Radarr",
    "Sonarr",
    "JDownloader",
]


def _save_repeatedly(dbfile, configfile, key, rounds):
    """Rewrite the ini from a separate process via the real Config path."""
    shared_state.values = {"dbfile": dbfile, "configfile": configfile}
    shared_state.lock = None
    for i in range(rounds):
        Config("Hostnames").save(key, f"{key}{i}.example.invalid")


class ConfigAtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.dbfile = os.path.join(self.tmpdir.name, "Quasarr.db")
        self.configfile = os.path.join(self.tmpdir.name, "Quasarr.ini")
        shared_state.values = {"dbfile": self.dbfile, "configfile": self.configfile}
        shared_state.lock = None
        # Seed every section so the file is large enough that a torn write
        # would be observable.
        for section in SECTIONS:
            Config(section)

    def test_no_reader_ever_observes_a_torn_ini(self):
        # Regression: writes used to truncate Quasarr.ini in place, so any
        # observer outside the file lock (a raw reader, a backup tool, or the
        # frozen state after a mid-write SIGKILL) could catch an empty or
        # partial file. Atomic replace guarantees a complete old or complete
        # new file, so raw reads here are the assertion, not a cheat.
        ctx = multiprocessing.get_context("spawn")
        writers = [
            ctx.Process(
                target=_save_repeatedly,
                args=(self.dbfile, self.configfile, key, 200),
            )
            for key in ("al", "by")
        ]
        for w in writers:
            w.start()

        reads = 0
        torn = []
        while any(w.is_alive() for w in writers):
            with open(self.configfile, encoding="utf-8") as f:
                text = f.read()
            parser = configparser.RawConfigParser()
            try:
                parser.read_string(text)
                if len(parser.sections()) < len(SECTIONS):
                    torn.append(f"{len(parser.sections())} sections")
            except configparser.Error as e:
                torn.append(f"unparseable: {e}")
            reads += 1

        for w in writers:
            w.join(timeout=120)
        self.assertEqual([], [w.exitcode for w in writers if w.exitcode != 0])
        # Enough samples that a truncate-in-place write could not slip through.
        # Kept deliberately low: on a heavily loaded machine the reader thread
        # can be starved, and this guard only proves concurrency happened (the
        # old code produced dozens of torn reads out of a few thousand).
        self.assertGreater(reads, 50)
        self.assertEqual([], torn[:5], msg=f"{len(torn)} torn reads out of {reads}")

    def test_writes_still_roundtrip_and_leave_no_temp_files(self):
        Config("Hostnames").save("al", "roundtrip.example.invalid")

        self.assertEqual("roundtrip.example.invalid", Config("Hostnames").get("al"))
        leftovers = [
            name
            for name in os.listdir(self.tmpdir.name)
            if name not in ("Quasarr.db", "Quasarr.ini")
        ]
        self.assertEqual([], leftovers)


if __name__ == "__main__":
    unittest.main()
