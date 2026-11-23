"""One pipeline run at a time, and never alongside an open Oshiire window.

Every pipeline script (ingest, backfill, archive --apply, imagemeta warm,
ai_tag) loads the WHOLE manifest, changes it for minutes, and saves the whole
thing back. Two of them at once -- or one of them and the review UI -- are
last-writer-wins: the later save silently erases what the other wrote, and two
saves racing for one temp file crashed an ingest outright. Two guards, both
taken at the top of each script's main():

  1. data/run.lock -- an exclusive OS file lock held for the WHOLE run. A second
     run exits 1 with "<Job> is already running (started HH:MM, process N)."
     The lock belongs to the process, so a crash or a kill frees it; nothing
     is ever left locked by a dead run.
  2. A CLI run is refused while Oshiire is open (the app holds data/oshiire.lock):
     the review UI keeps its own copy of the manifest and saves the whole thing on
     every click, so a run that checkpoints the file underneath it loses, or
     overwrites, work. The app's own jobs are exempt: jobs.py hands each child its
     own pid in OSHIIRE_APP_PID, and the child is allowed only if that pid is the
     one the open app recorded in its lock file -- so a stray environment
     variable doesn't open a way round it, and there is no override flag.

Same technique as instance.py (a byte locked far past the end of the file, so
the bookkeeping text at the start stays readable while the lock is held). Stdlib
only; no gradio, no manifest imports.
"""
import os
import sys
import time
from pathlib import Path

import instance

RUN_LOCK_PATH = Path("data/run.lock")
APP_PID_ENV = "OSHIIRE_APP_PID"

MSG_APP_OPEN = "Oshiire is open. Close it, or run this from the app."

_LOCK_OFFSET = 1 << 30
_HELD = []  # the open lock handle(s); a reference keeps the lock for the process's life


class AlreadyRunning(Exception):
    """Another run holds data/run.lock. `job`, `pid` and `started` (epoch seconds)
    describe it, each None when the holder's note couldn't be read."""

    def __init__(self, job=None, pid=None, started=None):
        self.job, self.pid, self.started = job, pid, started
        super().__init__(describe(job, pid, started))


def describe(job, pid, started):
    """'Ingest is already running (started 20:01, process 4242).'"""
    name = job or "Another job"
    if pid is None or started is None:
        return f"{name} is already running."
    clock = time.strftime("%H:%M", time.localtime(started))
    return f"{name} is already running (started {clock}, process {pid})."


def _try_lock(path):
    """The open handle with the byte locked, or None when someone else holds it."""
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


def _read_note(path):
    """(job, pid, started) the holder wrote at the start of the file, each None if
    absent or unparseable. Never touches the locked byte."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.readline().strip()
    except OSError:
        return (None, None, None)
    parts = text.split("\t")
    if len(parts) != 3:
        return (None, None, None)
    try:
        return (parts[0] or None, int(parts[1]), float(parts[2]))
    except ValueError:
        return (None, None, None)


def _write_note(path, job):
    with open(path, "r+", encoding="utf-8") as handle:
        handle.seek(0)
        handle.write(f"{job}\t{os.getpid()}\t{time.time():.0f}\n")
        handle.truncate()
        handle.flush()


def acquire(job, path=None):
    """Take the run lock for `job` (a display name such as "Ingest"). Returns the
    handle, also kept alive for the life of the process. Raises AlreadyRunning."""
    path = Path(path if path is not None else RUN_LOCK_PATH)
    handle = _try_lock(path)
    if handle is None:
        raise AlreadyRunning(*_read_note(path))
    _HELD.append(handle)
    _write_note(path, job)
    return handle


def release(handle):
    """Give the lock back early (tests; a normal run just exits)."""
    if handle in _HELD:
        _HELD.remove(handle)
    try:
        handle.close()
    except OSError:
        pass


def holder(path=None):
    """None when the run lock is free, else (job, pid, started) of the holder. Takes
    and drops the lock to find out, so it is a snapshot, not a reservation."""
    path = Path(path if path is not None else RUN_LOCK_PATH)
    handle = _try_lock(path)
    if handle is not None:
        handle.close()
        return None
    return _read_note(path)


def busy_message(path=None):
    """'<Job> is already running (started HH:MM, process N).' when a run holds the
    lock, else None. For the app's menu, which greys the doors out with it."""
    info = holder(path)
    return None if info is None else describe(*info)


def app_is_open(instance_path=None):
    """True while an Oshiire window/server holds the single-instance lock."""
    kwargs = {} if instance_path is None else {"path": instance_path}
    handle = instance.acquire_single_instance(**kwargs)
    if handle is None:
        return True
    handle.close()
    return False


def started_by_app(instance_path=None):
    """True when this process was launched by the OPEN app's job runner: the pid in
    OSHIIRE_APP_PID is the one the app recorded in its lock file."""
    try:
        claimed = int(os.environ.get(APP_PID_ENV, ""))
    except ValueError:
        return False
    kwargs = {} if instance_path is None else {"path": instance_path}
    return claimed == instance.read_lock_info(**kwargs)[1]


def guard(job, path=None, instance_path=None):
    """The call at the top of a pipeline script's main(): refuse while Oshiire is
    open (unless the app started this run), then take the run lock. On a refusal
    prints one line to stderr and exits 1. Returns the lock handle."""
    if app_is_open(instance_path) and not started_by_app(instance_path):
        print(MSG_APP_OPEN, file=sys.stderr)
        sys.exit(1)
    try:
        return acquire(job, path)
    except AlreadyRunning as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
