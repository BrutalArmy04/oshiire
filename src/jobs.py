"""Phase B job runner: runs the existing CLI pipelines (ingest, backfill,
archive) as child processes and streams their output back to the review UI.

Pure Python -- deliberately NO gradio import -- so it stays unit-testable on its
own. review.py drives it; the pipelines themselves (ingest.py, backfill.py,
archive.py, ai_tag.py, imagemeta.py, hash_index.py, export_unsave_list.py) are
run AS THEY ARE, via [sys.executable, *argv], never imported for execution.

Secrets never reach a log: every child line passes through redact() before it is
yielded, and the only log written to disk, data/jobs.log, is made from those same
redacted lines. (data/oshiire.log, the app's own log, keeps the child's verbatim
stderr and is a separate file.)
"""
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue

import config
import datahome
import fsutil
import runlock

# Imported for its path/parse helpers only (CSV_PATH, STATE_PATH, load_rows,
# load_state) -- the single source of truth for the back-catalogue cursor. The
# backfill SCRIPT is still run as a child process, never through these.
import backfill
from hash_index import DEFAULT_DB

def _script(name):
    """A pipeline script's ABSOLUTE path in the src folder. Children run in the
    data root (the cwd), which is not the app folder in a release."""
    return str(datahome.CODE_DIR / name)


# --------------------------------------------------------------------------- #
# Step model + pipelines
# --------------------------------------------------------------------------- #
@dataclass
class Step:
    """One pipeline step. A script step has an argv (run as [sys.executable,
    *argv]); a step with argv=None and cooldown_s>0 is a cooldown pause."""
    label: str
    argv: "list[str] | None"
    on_fail: str  # "stop" | "continue"
    cooldown_s: int = 0


def ingest_steps():
    """The ingest pipeline plus the hash/tag tail: fetch, hash, tag."""
    return [
        Step("Ingest", [_script("ingest.py")], "stop"),
        Step("Hash new images", [_script("imagemeta.py"), "warm"], "continue"),
        Step("AI tagging", [_script("ai_tag.py")], "continue"),
    ]


def backfill_steps(limit, cooldown=120):
    """The full maintenance cycle -- backfill sweep, cooldown, ingest,
    hash, index, AI tag."""
    return [
        Step("Backfill", [_script("backfill.py"), "--limit", str(limit)], "stop"),
        Step("Export unsave whitelist", [_script("export_unsave_list.py")], "stop"),
        Step(f"Cooling down {cooldown}s", None, "stop", cooldown_s=cooldown),
        Step("Ingest", [_script("ingest.py")], "stop"),
        Step("Hash new images", [_script("imagemeta.py"), "warm"], "continue"),
        Step("Index archive", [_script("hash_index.py"), "build"], "continue"),
        Step("AI tagging", [_script("ai_tag.py")], "continue"),
    ]


def export_unsave_steps():
    """Build the safe-to-unsave whitelist (the Free-up-space page's step 1).
    Read-only: export_unsave_list.py reads the manifest + backfill log and writes
    only data/unsave_list.json -- no network, no .env, no manifest write."""
    return [Step("Make the unsave list", [_script("export_unsave_list.py")], "stop")]


def archive_dryrun_steps():
    # --json so the Preview filing page can render the plan as a real table
    # (parse_plan_row); the human text table still prints for the log.
    return [Step("Archive dry run", [_script("archive.py"), "--json"], "stop")]


def archive_apply_steps():
    """Archive apply, then the two maintenance tails it has to own. These two
    also run at the end of Backfill, but Backfill greys out for good once the
    sweep is complete, and the unsave list goes stale exactly when apply
    archives new posts -- so they belong here too."""
    return [
        Step("Archive apply", [_script("archive.py"), "--apply"], "stop"),
        Step("Refresh duplicate check", [_script("hash_index.py"), "build"], "continue"),
        Step("Update unsave list", [_script("export_unsave_list.py")], "continue"),
    ]


# --------------------------------------------------------------------------- #
# Redaction -- every child line goes through this before it is yielded
# --------------------------------------------------------------------------- #
_REDACTED = "[redacted]"
_FEED_RE = re.compile(r"(feed=)[^&\s]+")
_SESSION_RE = re.compile(r"(reddit_session=)[^&\s]+")
_TOKEN_RE = re.compile(r"(token_v2=)[^&\s]+")
_COOKIE_RE = re.compile(r"(Cookie:).*", re.IGNORECASE)


def _secrets_from_env():
    """The literal secret VALUES to scrub from child output, read fresh from
    the merged config (env > .env > data/config.env): the feed= query token of REDDIT_SAVED_FEED_URL and every cookie value
    in REDDIT_SESSION_COOKIE. Only values >= 8 chars are kept. This list is for
    redact() only -- it is never printed, logged, or yielded."""
    # Longest first, so a secret that contains another is masked whole.
    return config.secret_values()


def redact(line, secrets=()):
    """Mask the per-account feed token, the two session secrets, any Cookie:
    header value, and every literal `secrets` value that appears anywhere in the
    line. Ordinary lines come back byte-identical."""
    line = _FEED_RE.sub(r"\1" + _REDACTED, line)
    line = _SESSION_RE.sub(r"\1" + _REDACTED, line)
    line = _TOKEN_RE.sub(r"\1" + _REDACTED, line)
    line = _COOKIE_RE.sub(r"\1 " + _REDACTED, line)
    for secret in secrets:
        line = line.replace(secret, _REDACTED)
    return line


# --------------------------------------------------------------------------- #
# App relabelling -- a few child lines are written for someone at a terminal and
# read wrong inside the app (where there is no "--apply" to type). This maps such
# a line to its in-app wording. It is applied to the DISPLAYED job log only,
# after redaction -- never to data/oshiire.log, which keeps the child's verbatim
# output. Add future command-line-only hints as new {cli_line: app_line} pairs.
# --------------------------------------------------------------------------- #
_CLI_TO_APP = {
    "Dry-run only -- nothing was moved. Re-run with --apply to perform these moves.":
        "Dry run only — nothing was moved. To do these moves, open Archive apply "
        "from the home menu.",
}


def relabel_for_app(line):
    """Swap a known command-line-only hint for its in-app wording. Whole-line
    exact match; every other line comes back unchanged."""
    return _CLI_TO_APP.get(line, line)


# --------------------------------------------------------------------------- #
# Structured summary -- archive.py's dry run ends with one machine-readable line,
# "OSHIIRE_SUMMARY moves=<n> flagged=<k>". The job page reads its counts from
# here (never by parsing the free-text table) and hides the line from the log.
# --------------------------------------------------------------------------- #
_SUMMARY_RE = re.compile(r"^OSHIIRE_SUMMARY moves=(\d+) flagged=(\d+)$")


def parse_summary(line):
    """{"moves": n, "flagged": k} for an OSHIIRE_SUMMARY line, else None."""
    match = _SUMMARY_RE.match(line.strip())
    if not match:
        return None
    return {"moves": int(match.group(1)), "flagged": int(match.group(2))}


# archive.py --json also prints one "OSHIIRE_PLAN <json>" line per planned row,
# which the Preview filing page renders as a table. Like the summary line, these
# feed the UI and are hidden from the log; a malformed one is ignored, never
# shown raw.
_PLAN_PREFIX = "OSHIIRE_PLAN "


def parse_plan_row(line):
    """The decoded dict for an "OSHIIRE_PLAN <json>" line, else None. The keys
    are archive.plan_display_rows' (image/franchise/character/destination/status/
    needs_resolve)."""
    stripped = line.strip()
    if not stripped.startswith(_PLAN_PREFIX):
        return None
    try:
        row = json.loads(stripped[len(_PLAN_PREFIX):])
    except (ValueError, TypeError):
        return None
    return row if isinstance(row, dict) else None


# export_unsave_list.py ends with one "OSHIIRE_UNSAVE total=<n>" line: the
# count of posts provably captured and therefore safe to unsave. The
# Free-up-space page reads its result count from here (never by parsing the
# human summary) and, like the two lines above, hides it from the log.
_UNSAVE_RE = re.compile(r"^OSHIIRE_UNSAVE total=(\d+)$")


def parse_unsave_count(line):
    """{"safe": n} for an OSHIIRE_UNSAVE line, else None."""
    match = _UNSAVE_RE.match(line.strip())
    if not match:
        return None
    return {"safe": int(match.group(1))}


# --------------------------------------------------------------------------- #
# Single-job lock
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_current = None  # name of the running job, or None


def try_start(name):
    """Atomically take the lock for `name`. False if a job is already running."""
    global _current
    with _lock:
        if _current is not None:
            return False
        _current = name
        return True


def finish():
    """Release the lock. Idempotent -- safe even if nothing was held."""
    global _current
    with _lock:
        _current = None


def running():
    """The running job's name, or None."""
    with _lock:
        return _current


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
JOBS_LOG_PATH = Path("data") / "jobs.log"  # relative -> the data root (the cwd)
JOBS_LOG_MAX_BYTES = 2_000_000  # past this the next run rotates it to jobs.prev.log


def _stamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class _JobLog:
    """data/jobs.log: one block per run -- when it started, each step's start, its
    REDACTED output lines and its exit code, and how the run ended -- so a run's
    timing can be reconstructed later (two runs that overlapped, a crash, ...).

    Only ever fed the lines run_steps already redacted for the UI, plus labels and
    numbers; never the environment, the argv or a raw line. A log that can't be
    written must not stop a job, so every failure just turns logging off."""

    def __init__(self, path):
        self._fh = None
        try:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > JOBS_LOG_MAX_BYTES:
                fsutil.replace(path, path.with_name("jobs.prev.log"))
            self._fh = open(path, "a", encoding="utf-8", buffering=1)
        except OSError:
            self._fh = None

    def _write(self, text):
        if self._fh is None:
            return
        try:
            self._fh.write(text + "\n")
        except (OSError, ValueError):
            self._fh = None

    def begin(self, steps):
        self._write(f"=== start {_stamp()}  steps: " + " | ".join(s.label for s in steps))

    def step(self, label):
        self._write(f"--- {label}: start {_stamp()}")

    def line(self, text):
        if not text.startswith("OSHIIRE_PLAN"):  # table rows: bulky and in the UI already
            self._write(text)

    def exit(self, label, rc):
        self._write(f"--- {label}: exit {rc} at {_stamp()}")

    def end(self, outcome):
        self._write(f"=== end {_stamp()}  {outcome}")
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


def _child_env():
    env = dict(os.environ)
    # The four settings as they stand NOW (files re-read), so an edit to .env or
    # data/config.env reaches the next job: children inherit
    # PYTHON_DOTENV_DISABLED and no longer read .env themselves.
    for key, value in config.child_overrides().items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Tells the child it was started by THIS app, so runlock lets it run although
    # the app is open (a CLI run is refused then). The child checks the pid
    # against the one the app recorded in data/oshiire.lock.
    env[runlock.APP_PID_ENV] = str(os.getpid())
    return env


def _child_python(executable=sys.executable):
    """The interpreter to launch child steps with. When the app runs under
    pythonw.exe (app.py, no console), its sibling python.exe exists but has a
    console; we still want the children to be consoleless, which
    CREATE_NO_WINDOW handles -- but pythonw swallows child stdout on some
    setups, so prefer the sibling python.exe when present. Falls back to the
    given executable unchanged (non-Windows, or no sibling)."""
    exe = Path(executable)
    if exe.name.lower() == "pythonw.exe":
        sibling = exe.with_name("python.exe")
        if sibling.exists():
            return str(sibling)
    return executable


_SENTINEL = object()


def _pump(proc, q):
    """Reader thread: push child lines (newline-stripped) into the queue, then a
    sentinel so the main loop knows the stream ended. Owns closing the pipe (it
    is the only thing reading it), so no other thread races it closed."""
    try:
        for line in proc.stdout:
            q.put(line.rstrip("\n"))
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        q.put(_SENTINEL)


def _terminate(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _failure_reason(last_line, rc):
    """One short, already-redacted line saying why a step failed: the last thing it
    printed (a script's own error message, or a traceback's final line), else its
    exit code. Collapsed to one line, capped, and without a trailing full stop so
    it can sit at the end of a sentence."""
    text = " ".join((last_line or "").split())
    if not text:
        return f"exit {rc}"
    text = text.rstrip(".")
    if len(text) > 200:
        text = text[:199].rstrip() + "…"
    return text


def run_steps(steps, cancel, cancellable):
    """Run the steps, yielding ("line", text) / ("step", label) /
    ("done", ok, summary). When a step that must not fail does, a
    ("failed", finished_labels, failed_label, reason) event comes first, so the
    result panel can say which steps had finished (see `_failure_reason`). Never
    touches the lock; the caller owns it (and so
    keeps it held until after any post-job manifest reload).

    `cancel` is a threading.Event. When `cancellable`, setting it terminates the
    current child and ends with ("done", False, "Cancelled"); when not, the
    cancel is ignored and the step runs to the end.
    """
    secrets = _secrets_from_env()  # once per run; passed to every redact, never yielded
    proc = None
    reader = None
    finished = []  # labels of the script steps that exited 0, in order
    log = _JobLog(JOBS_LOG_PATH)
    log.begin(steps)
    outcome = "interrupted"  # what the log says if the generator is closed mid-run
    try:
        for step in steps:
            if cancellable and cancel.is_set():
                outcome = "cancelled"
                yield ("done", False, "Cancelled")
                return
            yield ("step", step.label)
            log.step(step.label)

            if step.argv is None:  # cooldown pause
                remaining = step.cooldown_s
                while remaining > 0:
                    if cancellable and cancel.is_set():
                        outcome = "cancelled"
                        yield ("done", False, "Cancelled")
                        return
                    if remaining == step.cooldown_s or remaining % 10 == 0:
                        yield ("line", f"cooling down: {remaining}s remaining")
                    time.sleep(1)
                    remaining -= 1
                continue

            proc = subprocess.Popen(
                [_child_python(), *step.argv],
                cwd=str(Path.cwd()),  # the data root (see datahome.py)
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_child_env(),
                creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
            )
            q = Queue()
            reader = threading.Thread(target=_pump, args=(proc, q), daemon=True)
            reader.start()

            cancelled = False
            last_line = ""
            while True:
                # Checked between polls (not only on an idle timeout) so a chatty
                # child is still cancelled promptly, right after its latest line.
                if cancellable and cancel.is_set():
                    _terminate(proc)
                    cancelled = True
                    break
                try:
                    line = q.get(timeout=0.5)
                except Empty:
                    continue
                if line is _SENTINEL:
                    break
                safe_line = redact(line, secrets)
                if safe_line.strip() and not safe_line.startswith("OSHIIRE_"):
                    last_line = safe_line
                log.line(safe_line)
                yield ("line", safe_line)

            reader.join(timeout=1)
            reader = None  # pump closed the pipe in its finally
            if cancelled:
                outcome = "cancelled"
                yield ("done", False, "Cancelled")
                return

            rc = proc.wait()
            proc = None
            log.exit(step.label, rc)
            if rc != 0:
                if step.on_fail == "stop":
                    outcome = "failed"
                    yield ("failed", list(finished), step.label, _failure_reason(last_line, rc))
                    yield ("done", False, f"{step.label} failed (exit {rc}) -- see the log")
                    return
                warning = f"warning: {step.label} failed (exit {rc}); continuing."
                log.line(warning)
                yield ("line", warning)
            else:
                finished.append(step.label)

        outcome = "ok"
        yield ("done", True, "")
    finally:
        log.end(outcome)
        # Generator closed early (browser gone) with a child still alive, or a
        # mid-run exception: honour cancellability and clean up the child. The
        # lock is deliberately NOT released here -- the caller holds it across
        # its post-job manifest reload and frees it itself.
        if proc is not None:
            if proc.poll() is None:
                if cancellable:
                    _terminate(proc)
                else:
                    proc.wait()
            # The child has now exited, so the pump hits EOF; join it so it closes
            # the pipe in its own finally rather than leaving it to GC.
            if reader is not None:
                reader.join(timeout=10)


# --------------------------------------------------------------------------- #
# Availability -- read settings fresh every call; reasons never carry a secret value
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _backfill_at_data_root():
    """Point backfill's relative CSV/STATE paths at the data root (the cwd) so its own
    load_rows/load_state (the single source of truth for the cursor) read the
    right files."""
    old_csv, old_state = backfill.CSV_PATH, backfill.STATE_PATH
    backfill.CSV_PATH = Path.cwd() / old_csv  # the data root
    backfill.STATE_PATH = Path.cwd() / old_state
    try:
        yield
    finally:
        backfill.CSV_PATH, backfill.STATE_PATH = old_csv, old_state


def run_lock_busy():
    """'<Job> is already running (started HH:MM, process N).' while a pipeline run
    outside this app's own job (a command-line run) holds data/run.lock, else None.
    The menu greys Ingest / Backfill / File images out with it."""
    return runlock.busy_message()


def ingest_available():
    """(ok, reason). Ingest reads the logged-in saved listing, so it needs the
    Reddit username and login cookie. Reasons never include a secret value."""
    if not config.get("REDDIT_USERNAME").strip():
        return (False, "Your Reddit username isn't set up. Add it in Settings -> App.")
    if not config.get("REDDIT_SESSION_COOKIE").strip():
        return (False, "Your Reddit login cookie isn't set up. Add it in Settings -> App.")
    busy = run_lock_busy()
    if busy:
        return (False, busy)
    return (True, "")


def backfill_available():
    """(ok, reason), checked in dependency order so the first missing piece is
    the one reported. Reasons never include a secret value."""
    if not Path(backfill.CSV_PATH).exists():  # relative -> the data root
        return (False, "No Reddit data export at data/saved_posts.csv.")
    if not Path(DEFAULT_DB).exists():
        return (False, "The duplicate index hasn't been built yet.")
    if not config.get("REDDIT_SESSION_COOKIE").strip():
        return (False, "Reddit login isn't set up.")
    with _backfill_at_data_root():
        total = len(backfill.load_rows())
        next_row = backfill.load_state().get("next_row", 0)
    if next_row >= total:
        if total == 1:
            return (False, "Backfill complete: the 1 saved post was swept.")
        return (False, f"Backfill complete: all {total} saved posts swept.")
    busy = run_lock_busy()
    if busy:
        return (False, busy)
    return (True, "")
