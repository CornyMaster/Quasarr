# -*- coding: utf-8 -*-

import multiprocessing
import os
import tempfile
import time
import unittest

from filelock import Timeout

from quasarr.storage.lock import ProcessLocalFileLock, get_lock, with_lock


def _acquire_in_child(lock, queue):
    """Acquire the inherited handle in a forked child and report the outcome."""
    try:
        with lock.acquire(timeout=10):
            queue.put(("ok", os.getpid()))
    except Exception as e:  # noqa: BLE001 - the failure message is the assertion
        queue.put(("error", f"{type(e).__name__}: {e}"))


def _try_acquire_held_lock_in_child(lock, queue):
    """Fail fast on a lock the parent still holds, then exit."""
    try:
        with lock.acquire(timeout=0):
            queue.put("acquired")
    except Timeout:
        queue.put("blocked")
    except Exception as e:  # noqa: BLE001 - the failure message is the assertion
        queue.put(f"{type(e).__name__}: {e}")


def _increment_under_lock(lock_name, counter_file, order_file, iterations):
    """Read-modify-write a shared file from a separate process.

    Deliberately racy without a working lock: the sleep between read and write
    widens the window so lost updates show up if mutual exclusion is broken.
    """
    from quasarr.storage.lock import get_lock

    lock = get_lock(lock_name)
    pid = os.getpid()
    for _ in range(iterations):
        with lock.acquire(timeout=60):
            with open(order_file, "a") as f:
                f.write(f"IN {pid}\n")
            with open(counter_file) as f:
                value = int(f.read())
            time.sleep(0.003)
            with open(counter_file, "w") as f:
                f.write(str(value + 1))
            with open(order_file, "a") as f:
                f.write(f"OUT {pid}\n")


class StorageLockForkSafetyTests(unittest.TestCase):
    def setUp(self):
        self.lock_file = os.path.join(
            tempfile.gettempdir(), f"quasarr_test_{os.getpid()}.lock"
        )
        self.addCleanup(
            lambda: os.path.exists(self.lock_file) and os.remove(self.lock_file)
        )

    def test_same_process_reuses_one_filelock_instance(self):
        # Reentrancy relies on the process reusing its own FileLock instance;
        # rebuilding it per call would self-deadlock nested @with_lock methods.
        lock = ProcessLocalFileLock(self.lock_file)

        first = lock._instance()
        second = lock._instance()

        self.assertIs(first, second)

    def test_nested_acquire_does_not_deadlock(self):
        lock = ProcessLocalFileLock(self.lock_file)

        @with_lock(lock, timeout=5)
        def outer():
            return inner()

        @with_lock(lock, timeout=5)
        def inner():
            return "done"

        self.assertEqual("done", outer())

    @unittest.skipUnless(
        "fork" in multiprocessing.get_all_start_methods(),
        "fork start method unavailable on this platform",
    )
    def test_inherited_handle_is_usable_in_forked_child(self):
        # Regression: filelock >= 3.30 raises RuntimeError when a FileLock
        # created in the parent is acquired in a forked child. Quasarr's storage
        # modules bind their locks at import time, before run() forks its
        # workers, so the handle must rebuild the instance per process.
        lock = ProcessLocalFileLock(self.lock_file)
        lock._instance()  # bind the parent's instance before forking

        ctx = multiprocessing.get_context("fork")
        queue = ctx.Queue()
        child = ctx.Process(target=_acquire_in_child, args=(lock, queue))
        child.start()
        child.join(timeout=30)

        status, detail = queue.get(timeout=5)
        self.assertEqual("ok", status, msg=f"child failed to acquire lock: {detail}")
        self.assertNotEqual(os.getpid(), detail)

    @unittest.skipUnless(
        "fork" in multiprocessing.get_all_start_methods(),
        "fork start method unavailable on this platform",
    )
    def test_child_neither_steals_nor_releases_a_lock_held_at_fork(self):
        # The dangerous case: a worker is forked while the parent holds the
        # lock. The child must not enter the critical section, and dropping its
        # inherited instance must not release the parent's lock (filelock's
        # __del__ no-ops on a PID mismatch, which is what keeps this safe).
        lock = ProcessLocalFileLock(self.lock_file)
        ctx = multiprocessing.get_context("fork")

        with lock.acquire(timeout=10):
            queue = ctx.Queue()
            child = ctx.Process(
                target=_try_acquire_held_lock_in_child, args=(lock, queue)
            )
            child.start()
            child.join(timeout=30)

            self.assertEqual("blocked", queue.get(timeout=5))
            self.assertTrue(lock.is_locked, msg="child exit released the parent's lock")

    def test_separate_processes_still_exclude_each_other(self):
        # The handle resolves a FileLock per PID, so every process ends up with
        # its own instance. Mutual exclusion does not come from sharing that
        # object: it comes from the OS lock on the shared lock FILE, which all
        # processes open by the same path. This asserts the guarantee that
        # matters - concurrent processes never overlap inside the critical
        # section and no update is lost - so a change that reduces the lock to
        # a per-process no-op fails here.
        workers, iterations = 3, 8
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        counter_file = os.path.join(tmpdir.name, "counter")
        order_file = os.path.join(tmpdir.name, "order")
        with open(counter_file, "w") as f:
            f.write("0")
        open(order_file, "w").close()
        lock_name = f"test_mutex_{os.getpid()}"
        self.addCleanup(
            lambda: (
                os.path.exists(
                    os.path.join(tempfile.gettempdir(), f"quasarr_{lock_name}.lock")
                )
                and os.remove(
                    os.path.join(tempfile.gettempdir(), f"quasarr_{lock_name}.lock")
                )
            )
        )

        ctx = multiprocessing.get_context("spawn")
        procs = [
            ctx.Process(
                target=_increment_under_lock,
                args=(lock_name, counter_file, order_file, iterations),
            )
            for _ in range(workers)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=180)

        self.assertEqual([], [p.exitcode for p in procs if p.exitcode != 0])
        with open(counter_file) as f:
            self.assertEqual(workers * iterations, int(f.read()))

        with open(order_file) as f:
            events = f.read().splitlines()
        # Every IN must be followed by the OUT of the same pid; anything else
        # means two processes were inside the critical section at once.
        pairs = list(zip(events[::2], events[1::2], strict=True))
        self.assertEqual(workers * iterations, len(pairs))
        for enter, leave in pairs:
            self.assertTrue(enter.startswith("IN "), msg=f"overlap at {enter}")
            self.assertTrue(leave.startswith("OUT "), msg=f"overlap at {leave}")
            self.assertEqual(enter.split()[1], leave.split()[1])

    def test_get_lock_returns_stable_handle_per_name(self):
        self.assertIs(get_lock("database"), get_lock("database"))
        self.assertIsNot(get_lock("database"), get_lock("config"))


if __name__ == "__main__":
    unittest.main()
