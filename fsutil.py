"""The one rename every atomic write ends with: fsutil.replace(src, dst).

Every file Oshiire writes "atomically" is written to a temp file next to the
target and then renamed over it. On POSIX that rename is os.replace and always
works. On Windows, os.replace fails with PermissionError while ANY process has
the target (or the temp file) open without FILE_SHARE_DELETE -- an antivirus
scan, the search indexer, a cloud-sync client, or Oshiire's own reader in
another thread. Those holds last milliseconds, so the rename is retried briefly
instead of failing the whole save.

Only the two "someone else has it open" errors are retried: winerror 5
(ERROR_ACCESS_DENIED, also what MoveFileEx reports for a target held open) and
32 (ERROR_SHARING_VIOLATION). Anything else -- a missing source, a real
permission problem with no winerror, a full disk -- is raised at once. After
the last attempt the ORIGINAL first error is re-raised.

Stdlib only, no heavy imports.
"""
import os
import sys
import time

RETRY_WINERRORS = (5, 32)
ATTEMPTS = 10
# Sleeps between attempts: 9 gaps summing to about 1 second.
BACKOFF_S = (0.01, 0.02, 0.04, 0.06, 0.1, 0.15, 0.2, 0.2, 0.22)


def _retryable(exc):
    return isinstance(exc, PermissionError) and getattr(exc, "winerror", None) in RETRY_WINERRORS


def replace(src, dst, *, platform=None, _replace=None, _sleep=None):
    """os.replace(src, dst), retried on Windows while the file is briefly held
    open elsewhere. `platform`, `_replace` and `_sleep` exist for tests."""
    platform = sys.platform if platform is None else platform
    do_replace = os.replace if _replace is None else _replace
    if platform != "win32":
        do_replace(src, dst)
        return
    sleep = time.sleep if _sleep is None else _sleep
    first = None
    for attempt in range(ATTEMPTS):
        try:
            do_replace(src, dst)
            return
        except PermissionError as exc:
            if not _retryable(exc):
                raise
            if first is None:
                first = exc
            if attempt < ATTEMPTS - 1:
                sleep(BACKOFF_S[attempt])
    raise first
