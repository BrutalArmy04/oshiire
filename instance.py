"""Single-instance lock, shared by both entry points.

Oshiire has two ways in -- the native desktop window (app.py) and the plain
browser UI (`python review.py`) -- and only one may run at a time, because both
serve the same Gradio app and hold the same manifest open. Both acquire the lock
below before launching; whoever loses backs out instead of racing for a port.

Kept in its own module (no gradio, no review import) so review.py can take the
lock without importing app.py and app.py can take it without importing review's
heavy UI graph.
"""
import os
import sys
from pathlib import Path

DEFAULT_LOCK_PATH = Path("data/oshiire.lock")

# Lock a single byte far past anything we ever store in the file. The bound
# server port is written at the START of the file (see record_port); keeping the
# locked byte out at this offset means that prefix stays in an UNLOCKED region
# that a second launch can read even while this process holds the lock. On
# Windows msvcrt byte-range locks are mandatory -- they block another process's
# reads of the locked range -- so the port must live outside it; on POSIX flock
# is advisory and never blocks a plain read, so the offset is simply harmless.
_LOCK_OFFSET = 1 << 30


def acquire_single_instance(path=DEFAULT_LOCK_PATH):
    """Take an OS-level exclusive, non-blocking lock on `path`, returning the
    open handle (keep a reference for the life of the process) or None if another
    Oshiire already holds it. The lock releases when the handle is closed or the
    process exits."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def record_port(port, path=DEFAULT_LOCK_PATH, pid=None):
    """Record the bound server port AND the holder's PID at the start of the lock
    file, as "port pid", so a second launch (which can't take the lock) can both
    open the running instance and -- if the user asks to stop it -- find the
    exact process to terminate without ever guessing by image name. `pid`
    defaults to THIS process's id (record_port runs in the lock holder). Uses its
    own handle -- the write lands outside the locked byte, so the holder writes it
    and a second process reads it concurrently."""
    if pid is None:
        pid = os.getpid()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    with open(path, "r+", encoding="utf-8") as handle:
        handle.seek(0)
        handle.write(f"{int(port)} {int(pid)}")
        handle.truncate()
        handle.flush()


def clear_recorded_port(path=DEFAULT_LOCK_PATH):
    """Erase any recorded port. Called by a fresh launch right after it takes the
    lock, so a stale port left by a previous session can never be read back
    before this session records its own. A no-op if the file doesn't exist."""
    path = Path(path)
    if not path.exists():
        return
    with open(path, "r+", encoding="utf-8") as handle:
        handle.seek(0)
        handle.truncate()
        handle.flush()


def read_lock_info(path=DEFAULT_LOCK_PATH):
    """(port, pid) recorded by a running instance -- each None when it is absent
    or unparseable. Reads only the start of the file, never the locked byte, so
    it works while another process still holds the lock. Understands the current
    "port pid" format and, for a lock file left by an older version, the "port"-
    only and empty formats (pid comes back None then)."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read().strip()
    except OSError:
        return (None, None)
    if not text:
        return (None, None)
    parts = text.split()
    try:
        port = int(parts[0])
    except (ValueError, IndexError):
        return (None, None)
    pid = None
    if len(parts) >= 2:
        try:
            pid = int(parts[1])
        except ValueError:
            pid = None
    return (port, pid)


def read_recorded_port(path=DEFAULT_LOCK_PATH):
    """The port a running instance recorded, or None when the file is missing,
    empty or doesn't hold a port. A thin wrapper over read_lock_info for callers
    that only need the port."""
    return read_lock_info(path)[0]
