import os
import tempfile
import threading
import time
from functools import wraps

from filelock import FileLock

from quasarr.providers.log import trace

_locks = {}
_locks_guard = threading.Lock()


class ProcessLocalFileLock:
    """FileLock handle that rebuilds its instance after a fork.

    `quasarr/__init__.py` forks its worker processes after the storage modules
    are imported, so the `FileLock` created at import time is inherited by every
    child. filelock refuses to acquire an inherited instance
    ("was inherited across fork; construct a new instance"), which killed the
    forked workers. Resolving the instance per PID keeps the module-level
    `lock = get_lock(...)` bindings valid in parent and children alike, while a
    process that keeps reusing its own instance keeps filelock's reentrancy.
    """

    def __init__(self, lock_file):
        self._lock_file = lock_file
        self._guard = threading.Lock()
        self._pid = None
        self._lock = None

    def _instance(self) -> FileLock:
        pid = os.getpid()
        # Fast path: no locking once this process built its own instance.
        if self._lock is not None and self._pid == pid:
            return self._lock
        with self._guard:
            if self._lock is None or self._pid != pid:
                self._lock = FileLock(self._lock_file)
                self._pid = pid
            return self._lock

    @property
    def lock_file(self):
        return self._lock_file

    @property
    def is_locked(self):
        return self._instance().is_locked

    def acquire(self, timeout=-1, **kwargs):
        return self._instance().acquire(timeout=timeout, **kwargs)

    def release(self, force=False):
        return self._instance().release(force=force)

    def __enter__(self):
        self._instance().acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._instance().release()


def get_lock(name) -> ProcessLocalFileLock:
    with _locks_guard:
        if name not in _locks:
            _locks[name] = ProcessLocalFileLock(
                os.path.join(tempfile.gettempdir(), f"quasarr_{name}.lock")
            )
        return _locks[name]


def with_lock(lock: ProcessLocalFileLock, timeout=-1):
    """Serialize calls to the decorated function across processes.

    lock:    lock handle acquired via get_lock(name)
    timeout: seconds to wait; -1 = wait forever, 0 = fail immediately
    """

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.monotonic()
            with lock.acquire(timeout=timeout):
                delta = time.monotonic() - start
                trace(
                    f"acquiring lock for '{os.path.basename(lock.lock_file)}' took {delta * 1000:.3f}ms"
                )
                return fn(*args, **kwargs)

        return wrapper

    return decorator
