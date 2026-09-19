"""Small cross-process transactions for local stores; corrupt data is never empty data."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from functools import wraps
import json
import os
from pathlib import Path
import threading
import time
import uuid

_registry_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_local = threading.local()


@contextmanager
def transaction(path: Path, timeout: float = 30):
    """Lock an entire read/modify/write, across threads and Atlas processes.

    OS locks release after process failure. The persistent sidecar is only a lock
    identity, never a stale lock requiring deletion. Same-thread nesting is safe.
    """
    path = Path(path).resolve()
    key = os.path.normcase(str(path))
    with _registry_guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        held = getattr(_local, "held", None)
        if held is None:
            held = _local.held = set()
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        sidecar = path.with_name(path.name + ".lock")
        with open(sidecar, "a+b") as handle:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + timeout
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise ValueError(f"Store is busy; retry after the current operation: {path}") from exc
                    time.sleep(0.025)
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def locked_store(path_attribute="path"):
    def decorate(fn):
        @wraps(fn)
        def run(self, *args, **kwargs):
            if getattr(self, "read_only", False):
                self._guard_writable()
            with transaction(getattr(self, path_attribute)):
                return fn(self, *args, **kwargs)
        return run
    return decorate


def locked_path(resolve):
    """Lock a module-level store whose configured directory is resolved at call time."""
    def decorate(fn):
        @wraps(fn)
        def run(*args, **kwargs):
            with transaction(resolve()):
                return fn(*args, **kwargs)
        return run
    return decorate


def read_json(path: Path, default, *, rows: str | None = None):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return deepcopy(default)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Store is unreadable; original preserved. Restore a known-good copy before editing: {path}") from exc
    if not isinstance(data, type(default)):
        raise ValueError(f"Invalid store structure; original preserved: {path}")
    if rows and (not isinstance(data.get(rows), list)
                 or any(not isinstance(row, dict) for row in data[rows])):
        raise ValueError(f"Invalid {rows} records; original preserved: {path}")
    return data


def atomic_text(path: Path, text: str):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.03 * (attempt + 1))
    finally:
        temp.unlink(missing_ok=True)


def atomic_write_json(path: Path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=1))
