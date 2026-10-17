"""Oshiire desktop entry point.

Opens the Gradio review UI inside a native OS window (pywebview). The window is
just a chrome around the local Gradio server -- review.py still does all the
work, and `python src/review.py` still runs the UI in the browser exactly as before.

Launch paths, in order of preference:
  1. native window  -- pywebview is installed AND a GUI backend exists
  2. browser        -- pywebview missing, or no GUI backend / no WebView2

The heavy import (review.py runs its startup hashing at import time) happens in
pywebview's worker thread via _boot(), so the window paints the LOADING_HTML
placeholder immediately instead of a blank frame.

It never prints a secret: config.load() merges .env/config.env into the
environment, and config.summary() logs key names only.

Developer use only: set the environment variable OSHIIRE_DEBUG=1 to launch with
WebView2's developer tools (devtools) enabled -- this passes debug=True to
webview.start(). Leave it unset for normal use.
"""
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import config
import datahome
import settings
import setup_window
import starters
from instance import (
    acquire_single_instance,
    clear_recorded_port,
    read_lock_info,
    read_recorded_port,
)

# This file, resolved once. Used to verify that a PID we are about to terminate
# really runs THIS repo's app.py -- we never stop a process by image name alone.
APP_PY = (datahome.CODE_DIR / "app.py").resolve()

# Second-launch port poll: a quiet quick poll first, then up to the full timeout
# behind a "Starting…" window. Ports to probe when none was recorded (the
# instance may predate the PID/port record): 7860 plus the next few.
_QUICK_POLL_S = 2.0
_PORT_POLL_S = 30.0
_PROBE_PORTS = range(7860, 7865)
_STOP_LOCK_WAIT_S = 5.0

# Set by _boot()/_browser_fallback() once review.py is imported, so the closing
# handler and shutdown can reach the live server and job lock. None while the
# app is still booting (the window is up but review hasn't finished importing).
_review = None

# True once a close has been deferred to wait for a running job: the window
# stays open and a single daemon waiter will destroy it when the job ends.
# Guards against spawning a second waiter if the user keeps clicking the X.
_closing_pending = False

# The single-instance lock handle, held for the life of the process so the OS
# keeps the lock. Set in main() once acquired.
_lock = None


LOADING_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Oshiire</title>
<style>
  :root { color-scheme: light dark; }
  html, body { height: 100%; margin: 0; }
  body {
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    gap: 0.6rem;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: #ffffff; color: #1a1a1a;
    text-align: center; padding: 0 16px;
  }
  .title { font-size: 1.4rem; font-weight: 600; }
  .sub { font-size: 0.9rem; opacity: 0.65; }
  @media (prefers-color-scheme: dark) {
    body { background: #1a1a1a; color: #ededed; }
  }
</style>
</head>
<body>
  <div class="title">Starting Oshiire&hellip;</div>
  <div class="sub">The first start after new saves can take a minute.</div>
</body>
</html>
"""


def _error_html(message: str) -> str:
    """The page shown in the window when review.py fails to start. `message` is
    the exception's one-line summary; the full traceback is in the log."""
    # Escape the message so an exception containing <, > or & renders as text.
    safe = (message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return f"""\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Oshiire</title>
<style>
  :root {{ color-scheme: light dark; }}
  html, body {{ height: 100%; margin: 0; }}
  body {{
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    gap: 0.6rem;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: #ffffff; color: #1a1a1a;
    text-align: center; padding: 0 24px;
  }}
  .title {{ font-size: 1.3rem; font-weight: 600; }}
  .msg {{ font-size: 0.95rem; }}
  .hint {{ font-size: 0.85rem; opacity: 0.65; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #1a1a1a; color: #ededed; }}
  }}
</style>
</head>
<body>
  <div class="title">Oshiire couldn't start.</div>
  <div class="msg">{safe}</div>
  <div class="hint">Details: data/oshiire.log</div>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# stdio under pythonw (no console)
# --------------------------------------------------------------------------- #
def _setup_stdio(log_dir=Path("data")):
    """Under pythonw.exe there is no console, so sys.stdout/sys.stderr are None
    and any print() (or a child's inherited stream) would raise. Redirect both
    to data/oshiire.log, rotating the previous run's log to data/oshiire.prev.log
    first. When a console exists (normal `python src/app.py`), change nothing."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / "oshiire.log"
    prev = log_dir / "oshiire.prev.log"
    if log.exists():
        try:
            if prev.exists():
                prev.unlink()
            log.replace(prev)
        except OSError:
            pass
    stream = open(log, "w", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = stream
    sys.stderr = stream


# --------------------------------------------------------------------------- #
# Single instance
# --------------------------------------------------------------------------- #
# acquire_single_instance now lives in instance.py (imported at the top) so that
# `python src/review.py` can take the same lock without importing this module.


# --------------------------------------------------------------------------- #
# "Already running" -- a second launch while an instance holds the lock
# --------------------------------------------------------------------------- #
# Closing the browser tab leaves the server running invisibly. A second launch
# can't take the lock; instead of a dead-end "already open" message it offers a
# small window with two ways forward: open the running instance (a tab at its
# port), or stop it and start fresh in THIS process. See _handle_already_running.

_ALREADY_RUNNING_CSS = """
  :root { color-scheme: light dark; --bg:#fff; --fg:#1a1a1a; --sub:#555;
          --btn:#1e2a44; --on:#f7f8f9; --line:#1e2a44; --seal:#c8361d; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#1a1a1a; --fg:#ededed; --sub:#b8b8b8; --btn:#c9d4ee; --on:#141925;
            --line:#6677a3; --seal:#ef6a4f; }
  }
  html,body { height:100%; margin:0; }
  body { display:flex; flex-direction:column; align-items:center;
         justify-content:center; gap:16px; padding:0 24px; text-align:center;
         font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         background:var(--bg); color:var(--fg); }
  .title { font-size:1.25rem; font-weight:700; }
  .row { display:flex; gap:12px; flex-wrap:wrap; justify-content:center; }
  button { font:inherit; font-weight:700; min-height:44px; padding:0 18px;
           border:2px solid var(--line); border-radius:2px; cursor:pointer; }
  button.primary { background:var(--btn); color:var(--on); }
  button.secondary { background:transparent; color:var(--fg); }
  button.stop { background:var(--seal); border-color:var(--seal); color:#fff; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .note, .warn { font-size:.9rem; color:var(--sub); max-width:34rem; }
  .confirm { display:flex; flex-direction:column; gap:12px; align-items:center; }
  [hidden] { display:none !important; }
"""

_STARTING_HTML = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Oshiire</title><style>{_ALREADY_RUNNING_CSS}</style></head>
<body><div class="title">Starting…</div>
<div class="note">Oshiire is already running — reconnecting.</div></body></html>"""


def _already_running_html(can_open: bool) -> str:
    """The window body: a title, an 'Open it' button (disabled with a note when
    no running port could be found), a 'Stop it and start fresh' button that
    reveals a job warning + Stop/Cancel confirm."""
    if can_open:
        open_btn = ('<button class="primary" onclick="pywebview.api.open_it()">'
                    'Open it</button>')
        open_note = ""
    else:
        open_btn = '<button class="primary" disabled>Open it</button>'
        open_note = ('<div class="note">Couldn\'t find the running window to '
                     'open. You can stop it and start fresh instead.</div>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Oshiire</title><style>{_ALREADY_RUNNING_CSS}</style></head>
<body>
  <div class="title">Oshiire is already running.</div>
  <div class="row">
    {open_btn}
    <button class="secondary" onclick="showConfirm()">Stop it and start fresh</button>
  </div>
  {open_note}
  <div id="confirm" class="confirm" hidden>
    <div class="warn">If a job is running it will be stopped part-way; nothing is
      lost that was already saved.</div>
    <div class="row">
      <button class="stop" onclick="pywebview.api.stop_it()">Stop</button>
      <button class="secondary" onclick="hideConfirm()">Cancel</button>
    </div>
  </div>
  <script>
    function showConfirm(){{document.getElementById('confirm').hidden=false;}}
    function hideConfirm(){{document.getElementById('confirm').hidden=true;}}
  </script>
</body></html>"""


class _AlreadyRunningApi:
    """pywebview js_api: the window's buttons call these. Each records the choice
    and closes the window; _show_already_running reads `result` after start()."""

    def __init__(self):
        self.window = None     # set to the created window right after creation
        self.result = None     # "open" | "open_handled" | "stop" | None

    def open_it(self):
        self.result = "open"
        self._close()

    def stop_it(self):
        self.result = "stop"
        self._close()

    def _close(self):
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass


def _show_already_running(mode, port, remaining_poll):
    """Show the one 'already running' window and return (action, port):
      ("open", port)   -- open a tab at `port`
      ("open_handled", None) -- a tab was already opened (from the poll thread)
      ("stop", None)   -- stop the running instance and start fresh
      ("close", None)  -- nothing to do (dismissed, or no pywebview)

    When `port` is still None the window shows 'Starting…' and finishes the port
    poll in a background thread, then swaps to the buttons -- or, in browser mode
    once a port appears, opens a tab and closes. One webview.start()."""
    try:
        import webview
    except ImportError:
        print("Oshiire is already running.")
        return ("close", None)

    api = _AlreadyRunningApi()
    state = {"port": port}
    html = _already_running_html(can_open=True) if port is not None else _STARTING_HTML
    window = webview.create_window("Oshiire", html=html, width=480, height=260, js_api=api)
    api.window = window

    def finish_poll():
        found = _wait_for_recorded_port(timeout=remaining_poll, interval=0.5)
        open_port = _discover_oshiire_port(found if found is not None else state["port"])
        state["port"] = open_port
        if open_port is not None and mode == "browser":
            import webbrowser
            webbrowser.open(_themed_url(open_port))
            api.result = "open_handled"
            try:
                window.destroy()
            except Exception:
                pass
            return
        try:
            window.load_html(_already_running_html(can_open=open_port is not None))
        except Exception:
            pass

    if port is None:
        threading.Thread(target=finish_poll, daemon=True).start()

    webview.start()

    if api.result == "open_handled":
        return ("open_handled", None)
    if api.result == "open":
        return ("open", state["port"])
    if api.result == "stop":
        return ("stop", None)
    return ("close", None)


# --------------------------------------------------------------------------- #
# Finding and stopping the running instance (for "Stop it and start fresh")
# --------------------------------------------------------------------------- #
def _psutil():
    """psutil if it's installed, else None. It is an optional convenience: the
    PID scan/verify fall back to wmic on Windows when it's absent."""
    try:
        import psutil
        return psutil
    except ImportError:
        return None


def _cmdline_runs_our_app(cmdline, cwd=None):
    """True when a process command line (a list of args) runs THIS repo's app.py.
    An absolute app.py path must resolve to APP_PY; a relative 'app.py' is judged
    against the process's cwd (or this repo root as a fallback). This is the
    whole safety test -- a PID is only ever terminated when this returns True, so
    we never stop a process just because it is python/pythonw."""
    for token in cmdline or ():
        # wmic/PowerShell command lines are re-split with shlex(posix=False),
        # which KEEPS the quotes around a spaced path (this repo's path has a
        # space), so strip them before testing the basename. psutil's argv is
        # already unquoted, so this is a no-op there.
        try:
            p = Path(str(token).strip().strip('"').strip("'"))
        except Exception:
            continue
        if p.name.lower() != "app.py":
            continue
        try:
            if p.is_absolute():
                if p.resolve() == APP_PY:
                    return True
                continue
            base = Path(cwd) if cwd else datahome.APP_DIR
            if (base / p).resolve() == APP_PY:
                return True
        except OSError:
            continue
    return False


def _no_window_flags():
    """CREATE_NO_WINDOW on Windows, so a subprocess (PowerShell/wmic/taskkill) run
    from the console-less pythonw second launch never flashes a window; 0
    elsewhere."""
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _run_capture(args, timeout):
    """Run `args` and return its stdout, or None on any launch/timeout failure."""
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            creationflags=_no_window_flags(),
        ).stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _powershell_cmdline(pid):
    """One process's command line (a token list) via PowerShell's CIM, or None.
    The Windows path when wmic is gone -- current Windows 11 ships no wmic."""
    out = _run_capture(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
        timeout=15,
    )
    if not out or not out.strip():
        return None
    value = out.strip()
    try:
        return shlex.split(value, posix=False)
    except ValueError:
        return value.split()


def _powershell_scan():
    """Scan python/pythonw processes via PowerShell's CIM, returning the PID of
    the first whose command line runs this repo's app.py, or None. Each process
    prints one `pid|commandline` line (a PID can't contain '|', so split once)."""
    out = _run_capture(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "Get-CimInstance Win32_Process -Filter "
         "\"Name='python.exe' or Name='pythonw.exe'\" | "
         "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }"],
        timeout=20,
    )
    if not out:
        return None
    for line in out.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        pid_text, _, value = line.partition("|")
        try:
            pid = int(pid_text.strip())
        except ValueError:
            continue
        try:
            cmdline = shlex.split(value, posix=False) if value else []
        except ValueError:
            cmdline = value.split()
        if _cmdline_runs_our_app(cmdline):
            return pid
    return None


def _wmic_cmdline(pid):
    """One process's command line (as a token list) via `wmic`, or None. Legacy
    Windows fallback -- wmic is absent on current Windows 11, so PowerShell is
    tried first (see _pid_cmdline)."""
    try:
        where = f"processid={int(pid)}"
    except (TypeError, ValueError):
        return None
    out = _run_capture(
        ["wmic", "process", "where", where, "get", "commandline", "/format:list"],
        timeout=10,
    )
    if not out:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.lower().startswith("commandline="):
            value = line.split("=", 1)[1].strip()
            if not value:
                return None
            try:
                return shlex.split(value, posix=False)
            except ValueError:
                return value.split()
    return None


def _pid_cmdline(pid):
    """(cmdline_list, cwd_or_None) for `pid`, or (None, None) if it can't be
    read. psutil when available (the pinned primary path), else PowerShell's CIM
    on Windows, then wmic as a last resort (no cwd from either)."""
    ps = _psutil()
    if ps is not None:
        try:
            proc = ps.Process(pid)
            try:
                cwd = proc.cwd()
            except Exception:
                cwd = None
            return (proc.cmdline(), cwd)
        except Exception:
            return (None, None)
    if sys.platform == "win32":
        cmdline = _powershell_cmdline(pid)
        if cmdline is None:
            cmdline = _wmic_cmdline(pid)
        return (cmdline, None)
    return (None, None)


def _pid_runs_our_app(pid):
    """Verify, before ever terminating it, that `pid`'s command line runs this
    repo's app.py."""
    cmdline, cwd = _pid_cmdline(pid)
    if not cmdline:
        return False
    return _cmdline_runs_our_app(cmdline, cwd)


_PY_NAMES = {"python.exe", "pythonw.exe", "python", "pythonw", "python3"}


def _wmic_scan():
    """Scan python/pythonw processes via `wmic`, returning the PID of the first
    whose command line runs this repo's app.py, or None. Legacy Windows fallback
    (wmic is absent on current Windows 11, so PowerShell is tried first)."""
    out = _run_capture(
        ["wmic", "process", "where",
         "name='python.exe' or name='pythonw.exe'",
         "get", "processid,commandline", "/format:list"],
        timeout=15,
    )
    if not out:
        return None
    cmdline = None
    for line in out.splitlines():
        line = line.strip()
        if line.lower().startswith("commandline="):
            value = line.split("=", 1)[1].strip()
            try:
                cmdline = shlex.split(value, posix=False) if value else []
            except ValueError:
                cmdline = value.split()
        elif line.lower().startswith("processid="):
            pid_text = line.split("=", 1)[1].strip()
            try:
                pid = int(pid_text)
            except ValueError:
                cmdline = None
                continue
            if cmdline is not None and _cmdline_runs_our_app(cmdline):
                return pid
            cmdline = None
    return None


def _scan_for_holder_pid():
    """Find the PID of a running app.py for THIS repo by scanning python/pythonw
    processes and matching the command line -- used when the lock had no PID (an
    older instance) or its PID didn't verify. psutil when available (the pinned
    primary path), else PowerShell's CIM on Windows, then wmic as a last resort.
    None if nothing matches (or no backend is available)."""
    ps = _psutil()
    if ps is not None:
        for proc in ps.process_iter(["name", "cmdline", "cwd"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if name not in _PY_NAMES:
                    continue
                if _cmdline_runs_our_app(proc.info.get("cmdline"), proc.info.get("cwd")):
                    return proc.pid
            except Exception:
                continue
        return None
    if sys.platform == "win32":
        pid = _powershell_scan()
        if pid is None:
            pid = _wmic_scan()
        return pid
    return None


def _find_holder_pid(lock_pid):
    """The PID to terminate for 'Stop it and start fresh'. Prefer the lock file's
    recorded PID, but only after verifying its command line really runs this
    repo's app.py (a bare PID can be recycled). If the lock had no PID (old
    format) or it didn't verify, scan for the process instead."""
    if lock_pid is not None and _pid_runs_our_app(lock_pid):
        return lock_pid
    return _scan_for_holder_pid()


def _terminate_pid(pid):
    """Terminate `pid` -- already verified to run this repo's app.py. psutil when
    available, else taskkill on Windows, else SIGTERM. True on a clean request."""
    ps = _psutil()
    if ps is not None:
        try:
            ps.Process(pid).terminate()
            return True
        except Exception:
            return False
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/F"],
                           capture_output=True, timeout=10,
                           creationflags=_no_window_flags())
        else:
            os.kill(int(pid), signal.SIGTERM)
        return True
    except Exception:
        return False


def _wait_for_lock_free(timeout=_STOP_LOCK_WAIT_S, interval=0.2):
    """After terminating the holder, poll to re-acquire the single-instance lock,
    up to `timeout` seconds. Returns the acquired handle, or None if the lock
    never freed in time."""
    deadline = time.monotonic() + timeout
    while True:
        handle = acquire_single_instance()
        if handle is not None:
            return handle
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def _relaunch_interpreter():
    """The interpreter to relaunch Oshiire with: pythonw.exe on Windows (so the
    fresh instance has no console window), else this same interpreter. If we are
    already running under pythonw.exe, that's used as-is."""
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.name.lower() == "python.exe":
        sibling = exe.with_name("pythonw.exe")
        if sibling.exists():
            return str(sibling)
    return sys.executable


def _spawn_fresh_app():
    """Launch a brand-new, detached Oshiire (a fresh app.py) in the same cwd with
    no console, and return without waiting for it. The lock must already be free
    (we released it) so the new instance can take it."""
    # cwd is the data root; env is the pre-config environment, so the fresh
    # instance re-reads .env/config.env itself and reports their sources truly.
    kwargs = {"cwd": os.getcwd(), "close_fds": True, "env": config.launch_environ()}
    if sys.platform == "win32":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([_relaunch_interpreter(), str(APP_PY)], **kwargs)


def _stop_and_restart(lock_pid):
    """'Stop it and start fresh': find, verify and terminate the running
    instance; once its lock is free, release it and spawn a fresh, detached
    app.py, then let THIS process exit (both modes -- we never continue in-process
    or call webview.start() twice). Nothing is terminated unless its command line
    runs this repo's app.py. Returns True when a fresh instance was launched."""
    pid = _find_holder_pid(lock_pid)
    if pid is None:
        print("Couldn't find the running Oshiire process to stop.")
        return False
    if not _pid_runs_our_app(pid):  # final guard; never kill by image name
        print("Refusing to stop a process that doesn't look like this Oshiire.")
        return False
    _terminate_pid(pid)
    handle = _wait_for_lock_free()
    if handle is None:
        print("The running Oshiire didn't stop in time; please try again.")
        return False
    # _wait_for_lock_free took the lock to prove it's free; release it so the
    # fresh instance can take it, then launch that instance and bow out.
    try:
        handle.close()
    except Exception:
        pass
    _spawn_fresh_app()
    return True


def _page_is_oshiire(port, timeout=1.0):
    """True when 127.0.0.1:`port` answers with Oshiire's page -- checked by the
    <title>, so a different local server on the port is not mistaken for ours."""
    try:
        import requests
        resp = requests.get(f"http://127.0.0.1:{int(port)}/", timeout=timeout)
    except Exception:
        return False
    import re
    match = re.search(r"<title[^>]*>([^<]*)</title>", resp.text, re.IGNORECASE)
    title = match.group(1) if match else resp.text
    return "oshiire" in title.lower()


def _discover_oshiire_port(known_port):
    """The port to open a tab at. A port recorded by the live instance is trusted
    as-is; when none is known, probe 7860 + the next few and return the first that
    answers with Oshiire's page, or None."""
    if known_port is not None:
        return known_port
    for port in _PROBE_PORTS:
        if _page_is_oshiire(port):
            return port
    return None


def _handle_already_running(mode):
    """Second launch while an instance holds the lock. Opens a tab at the running
    instance, or shows the Open/Stop window; 'Stop it and start fresh' terminates
    the old instance and spawns a fresh, detached one. Returns nothing -- this
    process always exits afterwards (the fresh instance, if any, carries on). The
    open/dismiss paths never touch stdio, so the running instance's log is never
    rotated out from under it."""
    lock_pid = read_lock_info()[1]
    # Quick, windowless poll: the common case is the instance is already up and
    # its port is recorded, so a browser launch just opens a tab.
    port = _wait_for_recorded_port(timeout=_QUICK_POLL_S, interval=0.25)
    if port is not None and mode == "browser":
        import webbrowser
        webbrowser.open(_themed_url(port))
        return
    # Native mode, or the port is still unknown: show the window (with 'Starting…'
    # while the rest of the poll runs if the port isn't known yet).
    remaining = max(0.0, _PORT_POLL_S - _QUICK_POLL_S)
    action, open_port = _show_already_running(mode, port, remaining)
    if action == "open" and open_port is not None:
        import webbrowser
        webbrowser.open(_themed_url(open_port))
        return
    if action == "stop":
        _stop_and_restart(lock_pid)


# --------------------------------------------------------------------------- #
# Close handling
# --------------------------------------------------------------------------- #
def close_decision(running_name, cancellable):
    """Pure decision for what the window's close button should do:
      - no job running            -> "close"   (allow the close)
      - a cancellable job (Backfill) -> "ask_stop"
      - a non-cancellable job     -> "ask_wait"
    """
    if running_name is None:
        return "close"
    if cancellable:
        return "ask_stop"
    return "ask_wait"


def _start_waiter(window):
    """Daemon thread: wait for the running job to finish, then destroy the
    window. Keeps the GUI thread free -- the closing handler never blocks on a
    job. At most one waiter ever runs, so repeated close clicks don't pile up
    threads."""
    global _closing_pending
    if _closing_pending:
        return
    _closing_pending = True

    def wait():
        while _review.jobs.running() is not None:
            time.sleep(0.5)
        window.destroy()
    threading.Thread(target=wait, daemon=True).start()


def _on_closing(window):
    """Build the pywebview `closing` handler. Returning False cancels the close;
    True allows it. Never blocks the GUI thread: a job we wait on is polled from
    a daemon thread that destroys the window itself when the job ends."""
    def handler():
        # pywebview swallows an exception raised in a closing handler and lets
        # the close proceed; os._exit would then kill the Gradio server mid-job
        # and orphan the child process. So fail CLOSED: on any error, keep the
        # window open while a job is running, but still allow the close when
        # idle -- a bug must never make the window un-closable.
        try:
            if _review is None:
                return True  # still booting -- nothing to protect, let it close
            name = _review.jobs.running()
            decision = close_decision(name, name == "Backfill")
            if decision == "close":
                return True
            if decision == "ask_stop":
                if window.create_confirmation_dialog(
                    "Backfill is running",
                    "Stop Backfill at its last checkpoint and quit?",
                ):
                    _review._backfill_cancel.set()
                    _start_waiter(window)
                return False
            # ask_wait: a non-cancellable job must run to the end.
            if window.create_confirmation_dialog(
                f"{name} is running",
                "Oshiire will close by itself when it finishes. Close then?",
            ):
                _start_waiter(window)
            return False
        except Exception:
            traceback.print_exc()
            try:
                busy = _review is not None and _review.jobs.running() is not None
            except Exception:
                busy = True
            return not busy
    return handler


# --------------------------------------------------------------------------- #
# Boot + fallback
# --------------------------------------------------------------------------- #
def _boot(window):
    """Runs in pywebview's worker thread: do the heavy review import and start
    the Gradio server, then point the window at it. Any failure is logged and
    shown in the window rather than left as a blank frame."""
    global _review
    try:
        import review
        _review = review
        review.set_launch_mode("native")  # hide the browser-only Quit button
        url = review.launch_app(inbrowser=False, block=False)
        window.load_url(url)
    except Exception as exc:
        traceback.print_exc()
        window.load_html(_error_html(f"{type(exc).__name__}: {exc}"))


def _shutdown():
    """Close the Gradio server (if it started) and flush the log."""
    if _review is not None:
        try:
            _review.demo.close()
        except Exception:
            traceback.print_exc()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass


def _themed_url(port, theme=None):
    """The loopback URL for a running instance, carrying the saved appearance:
    no __theme param for "system" (gradio follows the OS), else
    ?__theme=light|dark. `theme` defaults to the saved setting."""
    theme = theme if theme is not None else settings.theme()
    query = settings.theme_param(theme)
    return f"http://127.0.0.1:{port}/" + (f"?{query}" if query else "")


def _wait_for_recorded_port(timeout=30.0, interval=0.5):
    """Poll for the running instance's recorded port, up to `timeout` seconds.
    A just-started first instance clears its port on acquiring the lock and only
    records it once its server is up, so a second launch can briefly see nothing;
    this bridges that gap. Returns the port, or None if none appears in time."""
    deadline = time.monotonic() + timeout
    port = read_recorded_port()
    while port is None and time.monotonic() < deadline:
        time.sleep(interval)
        port = read_recorded_port()
    return port


def _run_browser(lock):
    """Browser mode: start the server, open the default browser at its URL, and
    keep the process alive until the Home 'Quit' button stops the server.
    `lock` is the single-instance handle, held for the life of the process."""
    global _review
    import review
    _review = review
    review.set_launch_mode("browser")  # show the Quit button
    # launch_app records the bound port in the lock file, opens the browser, then
    # blocks here until the server is closed (by Quit).
    review.launch_app(inbrowser=True, block=True)
    _shutdown()


def _run_native(lock):
    """Native mode: the pywebview window + close guard. If pywebview is missing
    or its WebView backend can't start, fall back to browser mode for THIS
    launch (logged), so a missing GUI backend never leaves the user with nothing."""
    try:
        import webview
    except ImportError:
        print("pywebview isn't available; opening Oshiire in the browser instead.")
        _run_browser(lock)
        return

    window = webview.create_window(
        "Oshiire", html=LOADING_HTML,
        width=1400, height=900, min_size=(960, 640),
    )
    window.events.closing += _on_closing(window)

    # OSHIIRE_DEBUG=1 enables WebView2's devtools (developer use only).
    debug = os.environ.get("OSHIIRE_DEBUG") == "1"
    try:
        if debug:
            webview.start(_boot, (window,), debug=True)
        else:
            webview.start(_boot, (window,))
    except Exception:
        # No GUI backend / no WebView2: log why and fall back to the browser.
        traceback.print_exc()
        print("The native window couldn't start; opening Oshiire in the browser instead.")
        _run_browser(lock)
        return

    # start() returned -> the window is gone. Tear the server down and hard-exit
    # so lingering Gradio/server threads can't keep the process alive.
    _shutdown()
    os._exit(0)


# --------------------------------------------------------------------------- #
# Startup robustness: numpy check + visible error window
# --------------------------------------------------------------------------- #
# Re-running `pip install -r requirements.txt` downgrades numpy, because
# dghs-imgutils pins numpy<2; on Python 3.14 that pin has no wheel and pip
# builds the experimental MinGW numpy, which crashes review.py's import. Under
# pythonw there is no console, so that crash was invisible. The check below runs
# BEFORE review is imported and surfaces any startup failure in a visible
# window, so the app never just fails silently. The fix is to upgrade numpy.
_MIN_NUMPY_MAJOR = 2
if datahome.is_release():
    # A release copy ships its packages pre-installed; there is no pip to run.
    _NUMPY_FIX = ("Download Oshiire again and replace this copy's files "
                  "(your data is kept separately).")
    _NUMPY_FIX_IS_COMMAND = False
else:
    _NUMPY_FIX = r".venv-win\Scripts\pip install --upgrade numpy"
    _NUMPY_FIX_IS_COMMAND = True
_LOG_PATH = Path("data") / "oshiire.log"


def _parse_major(version):
    """The leading integer of a version string ('2.5.1' -> 2), or None when it
    doesn't begin with a number -- an unparseable version is never treated as
    'too old', only as 'can't tell'."""
    head = str(version).strip().split(".", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def _numpy_fault(version, mingw):
    """Pure decision: given numpy's __version__ string and whether it looks like
    the experimental MinGW build, return a one-line reason it is unusable, or
    None when it's fine. The two faults both come from the numpy<2 pin building
    the experimental MinGW numpy on Python 3.13+ (no wheel there): a too-old
    major version, or that MinGW build (which crashes on import)."""
    major = _parse_major(version)
    if major is not None and major < _MIN_NUMPY_MAJOR:
        return (f"numpy {version} is installed, but Oshiire needs numpy "
                f"{_MIN_NUMPY_MAJOR} or newer.")
    if mingw:
        shown = version or "(unknown version)"
        return (f"numpy {shown} is the experimental MinGW build, which crashes "
                f"on import.")
    return None


def _numpy_looks_mingw(module, warnings_caught):
    """True when numpy looks like the experimental MinGW-W64 Windows build. That
    build warns on import ('Numpy built with MINGW-W64 ... is experimental'), so
    a captured import warning naming mingw is the primary signal; numpy's
    exposed build config is a secondary one where available. Deliberately
    conservative: it must never flag an ordinary numpy (a gcc toolchain on Linux
    is not a fault)."""
    for warning in warnings_caught or ():
        message = getattr(warning, "message", warning)
        if "mingw" in str(message).lower():
            return True
    try:
        config = module.show_config(mode="dicts")
    except Exception:
        config = None
    if isinstance(config, dict) and "mingw" in repr(config).lower():
        return True
    return False


def _check_numpy():
    """Import numpy and verify it is a usable build, returning a one-line reason
    string when it isn't (for the error window) or None when it's fine. Runs
    before review is imported -- review pulls numpy in transitively, so a broken
    numpy would otherwise crash that import silently under pythonw."""
    import warnings
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            import numpy
        version = getattr(numpy, "__version__", "")
        mingw = _numpy_looks_mingw(numpy, caught)
    except Exception as exc:
        return f"numpy could not be imported: {type(exc).__name__}: {exc}"
    return _numpy_fault(version, mingw)


def _show_error_window(reason, fix=None, log_path=_LOG_PATH, fix_is_command=True):
    """Pop up a visible, blocking error dialog so a startup failure is never
    silent -- including under pythonw, where there is no console. Uses stdlib
    tkinter only (no extra dependency, always importable), and never raises: if
    even tkinter is unavailable it falls back to a Windows message box, then to
    printing. `reason` is the
    one-line cause; `fix` an exact command the user can run to repair it."""
    parts = ["Oshiire couldn't start.", "", reason]
    if fix and fix_is_command:
        parts += ["", "To fix it, run this from the Oshiire folder:", f"    {fix}"]
    elif fix:
        parts += ["", f"To fix it: {fix}"]
    # Absolute: in a release the log lives in the per-user data folder, which a
    # relative "data\oshiire.log" would not lead anyone to.
    parts += ["", f"Full log: {Path(log_path).absolute()}"]
    message = "\n".join(parts)
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        messagebox.showerror("Oshiire", message)
        root.destroy()
    except Exception:
        # No tkinter (or no display): on Windows the plain system message box
        # still works, so a startup failure is never silent under pythonw.
        if _message_box_w(message):
            return
        try:
            print(message)
        except Exception:
            pass


def _message_box_w(message):
    """Windows-only fallback dialog via user32.MessageBoxW (ctypes, no Tk).
    Returns True when it was shown."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        MB_ICONERROR, MB_TOPMOST = 0x10, 0x40000
        ctypes.windll.user32.MessageBoxW(None, message, "Oshiire", MB_ICONERROR | MB_TOPMOST)
        return True
    except Exception:
        return False


def main():
    # A desktop shortcut may launch us with a different "Start in" folder, so
    # anchor the cwd on the data root (datahome.py) -- manifest.json, data/ and
    # data/settings.json resolve relative to it; app files go through APP_DIR.
    # The data root (datahome.py) IS the cwd from here on: the app folder in a
    # git checkout, the per-user data folder in a release, or OSHIIRE_HOME.
    root = datahome.data_root()
    root.mkdir(parents=True, exist_ok=True)
    os.chdir(root)
    # Settings from env/.env/data/config.env, before anything reads them; this
    # also stops python-dotenv's own upward .env search (see config.py).
    config.load()

    mode = settings.window_mode()  # "browser" (default) or "native"

    # Take the single-instance lock BEFORE touching stdio: a second launch must
    # not rotate or truncate the log the running instance holds open (on Windows
    # the rotate fails and open("w") would truncate it out from under it). Only
    # a failure of the lock itself needs the log, so set stdio up for that case.
    try:
        lock = acquire_single_instance()
    except Exception:
        _setup_stdio()
        raise
    if lock is None:
        # Already running. _handle_already_running opens a tab at the running
        # instance (browser fast path), or shows the Open/Stop window; 'Stop it
        # and start fresh' terminates the old instance and spawns a fresh,
        # detached one. Either way this process is done afterwards -- and it never
        # touches stdio, so the running instance's log is never rotated out from
        # under it.
        _handle_already_running(mode)
        sys.exit(0)
    global _lock
    _lock = lock
    # Drop any stale port left by a previous session, so a second launch racing
    # us can never read an old port before launch_app records the real one.
    clear_recorded_port()
    _setup_stdio()
    print(config.summary())  # key names and sources only, never a value

    # A first run has no configs, and `import review` exits without layout.json.
    # Missing files only, never an overwrite; a no-op once they exist.
    created = starters.ensure_starters()
    if created:
        print("Created starter files: " + ", ".join(str(p) for p in created))

    # Before importing review (which pulls in numpy): a bad numpy -- the numpy<2
    # MinGW build the pin can produce on 3.14 -- crashes that import. Catch it
    # here and show a visible window with the fix, rather than dying silently.
    reason = _check_numpy()
    if reason is not None:
        print(f"Oshiire startup check failed: {reason}")
        _show_error_window(reason, fix=_NUMPY_FIX, fix_is_command=_NUMPY_FIX_IS_COMMAND)
        sys.exit(1)

    # First run: no feed link / archive folder yet and the setup window was
    # never answered. It runs in-process and tears Tk down completely before
    # returning, so pywebview (native mode) starts on a clean GUI thread.
    if setup_window.needs_setup():
        outcome = setup_window.run_setup_window("first")
        if outcome == setup_window.RESULT_CANCELLED:
            print("Setup was cancelled; Oshiire is not starting.")
            sys.exit(0)
        # saved / skipped: save_setup already refreshed os.environ, so Home
        # sees the new settings with no restart. unavailable (no tkinter or no
        # display): carry on to Home, which says what isn't set up.
        print(f"Setup window: {outcome}")

    if mode == "native":
        _run_native(lock)
    else:
        _run_browser(lock)


def _run_guarded():
    """Run main(), but never die silently. Any exception that escapes startup
    before the UI is up is logged to the run log, surfaced in a visible error
    window (even under pythonw, where there is no console), and the process
    exits non-zero. A deliberate sys.exit (SystemExit) passes straight through
    unchanged -- that is a clean exit, not a failure."""
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:
        # We may have failed before _setup_stdio ran (e.g. reading settings);
        # ensure the log exists so the traceback lands somewhere under pythonw.
        try:
            _setup_stdio()
        except Exception:
            pass
        try:
            traceback.print_exc()
        except Exception:
            pass
        _show_error_window(f"{type(exc).__name__}: {exc}")
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        sys.exit(1)


if __name__ == "__main__":
    _run_guarded()
