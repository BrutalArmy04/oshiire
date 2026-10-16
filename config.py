"""Oshiire's settings: where each one comes from, and the one file the app writes.

Sources, highest first -- a key is taken from the first source that has it:

    1. the process environment
    2. <data root>/.env
    3. <app folder>/.env          (skipped when it is the same file as 2)
    4. <data root>/data/config.env

The two .env files are the user's own and load every key in them, as
load_dotenv() did. data/config.env is the app's file (written by save_setup) and
contributes ONLY the four CONFIG_KEYS. In a FILE, an empty value for one of those
four does not claim the key -- an empty REDDIT_SAVED_FEED_URL= copied from
.env.example must not hide the value the user later entered in the app.

The data root is the cwd (see datahome.py). app.py calls load() right after it
chdirs there; load() copies the merged values into os.environ, remembers which
source each one came from (names only -- never values), then sets
PYTHON_DOTENV_DISABLED=1. That flag turns every no-argument load_dotenv() in the
modules and in each job child (they inherit os.environ) into a no-op, so nothing
walks up the directory tree to a stray .env above the app folder. It does not
affect dotenv_values() with an explicit path, which is all this module uses.

Values are never printed or logged here; summary() names keys and sources only.
"""
import io
import os
import re
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values

import datahome

CONFIG_KEYS = (
    "REDDIT_SAVED_FEED_URL",
    "ARCHIVE_DIR",
    "REDDIT_USERNAME",
    "REDDIT_SESSION_COOKIE",
)
CONFIG_ENV = Path("data") / "config.env"  # relative to the data root
DISABLE_FLAG = "PYTHON_DOTENV_DISABLED"

SOURCE_ENV = "env"
SOURCE_DOTENV = ".env"
SOURCE_CONFIG = "config"

# Values shorter than this are too generic to blanket-replace across a log line.
_MIN_SECRET_LEN = 8

# Set by load(): the data root and app folder it resolved files against, and a
# snapshot of the process environment taken BEFORE any file values were copied
# in -- so later reads can still tell a real environment variable from a value
# load() put there. None until load() runs (e.g. `python review.py`, tests);
# then the cwd, APP_DIR and the live os.environ are used.
_state = None


def _same_file(a, b):
    try:
        if a.exists() and b.exists():
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def source_files(root=None, app_dir=None):
    """[(source, Path), ...] in precedence order, files that may not exist
    included. The app-folder .env is dropped when it IS the data-root .env (a
    git checkout, where the data root is the app folder)."""
    root = Path(root) if root is not None else _root()
    app_dir = Path(app_dir) if app_dir is not None else _app_dir()
    files = [(SOURCE_DOTENV, root / ".env")]
    if not _same_file(app_dir / ".env", root / ".env"):
        files.append((SOURCE_DOTENV, app_dir / ".env"))
    files.append((SOURCE_CONFIG, root / CONFIG_ENV))
    return files


def _root():
    return _state["root"] if _state else Path.cwd()


def _app_dir():
    return _state["app_dir"] if _state else datahome.APP_DIR


def _read_file(source, path):
    """One source file's {key: value}. config.env is whitelisted to CONFIG_KEYS
    and read without ${VAR} interpolation -- the app wrote it, so its values are
    literal. A bare `KEY` line (value None) is skipped, as load_dotenv skips it."""
    if source == SOURCE_CONFIG:
        raw = dotenv_values(path, interpolate=False)
        raw = {k: v for k, v in raw.items() if k in CONFIG_KEYS}
    else:
        raw = dotenv_values(path)
    return {k: v for k, v in raw.items() if v is not None}


def _resolve(root=None, app_dir=None, environ=None):
    """(values, sources) for every key, files read fresh on each call."""
    if environ is None:
        environ = _state["process_env"] if _state else os.environ
    values = dict(environ)
    sources = {k: SOURCE_ENV for k in values}
    for source, path in source_files(root, app_dir):
        if not path.is_file():
            continue
        for key, value in _read_file(source, path).items():
            if key in values:
                continue  # a higher source already has it -- never override
            if key in CONFIG_KEYS and value == "":
                continue  # an empty file value never claims a setting
            values[key] = value
            sources[key] = source
    return values, sources


def load(root=None, app_dir=None):
    """Merge every source into os.environ (never overriding a key the process
    environment already has), remember where each key came from, then disable
    python-dotenv's own discovery. Call once, after chdir to the data root."""
    global _state
    _state = {
        "root": Path(root if root is not None else Path.cwd()).absolute(),
        "app_dir": Path(app_dir if app_dir is not None else datahome.APP_DIR),
        "process_env": dict(os.environ),
    }
    values, sources = _resolve()
    for key, value in values.items():
        if sources[key] != SOURCE_ENV:
            os.environ[key] = value
    os.environ[DISABLE_FLAG] = "1"


def launch_environ():
    """The environment to start a fresh Oshiire with: the process environment as
    it was BEFORE load() copied file values into it, so the new instance reads
    its own .env/config.env and reports their sources truthfully. None (inherit
    os.environ as-is) when load() never ran."""
    return dict(_state["process_env"]) if _state else None


def get(key, default=""):
    """The merged value of `key` (files read fresh), or `default` if unset."""
    return _resolve()[0].get(key, default)


def child_overrides():
    """{key: value} of the four CONFIG_KEYS as they stand NOW, for a job child's
    environment, plus the keys that are no longer set at all (value None), so an
    edit to .env or a save_setup reaches the next job without a restart."""
    values, _ = _resolve()
    return {key: values.get(key) for key in CONFIG_KEYS}


def key_sources():
    """{key: "env" | ".env" | "config" | None} for the four CONFIG_KEYS -- for a
    UI that says where a setting comes from. Names and sources only."""
    values, sources = _resolve()
    return {key: (sources[key] if key in values else None) for key in CONFIG_KEYS}


def secret_values():
    """The literal secret values to scrub from job output, from the MERGED
    config: the feed= query token of REDDIT_SAVED_FEED_URL and every cookie value
    in REDDIT_SESSION_COOKIE. Only values >= 8 chars, longest first (so a secret
    that contains another is masked whole). For redaction only -- never print,
    log or yield this list."""
    values, _ = _resolve()
    secrets = []
    feed_url = (values.get("REDDIT_SAVED_FEED_URL") or "").strip()
    match = re.search(r"feed=([^&\s]+)", feed_url)
    if match:
        secrets.append(match.group(1))
    cookie = (values.get("REDDIT_SESSION_COOKIE") or "").strip()
    for part in cookie.split(";"):
        if "=" in part:
            value = part.split("=", 1)[1].strip()
            if value:
                secrets.append(value)
    return sorted({s for s in secrets if len(s) >= _MIN_SECRET_LEN}, key=len, reverse=True)


def summary():
    """One log line: which source files exist and which CONFIG_KEYS are set, and
    from where. Key NAMES only -- never a value."""
    files = []
    for source, path in source_files():
        files.append(f"{path} ({'found' if path.is_file() else 'none'})")
    sources = key_sources()
    set_keys = [f"{k} [{s}]" for k, s in sources.items() if s]
    unset = [k for k, s in sources.items() if not s]
    return ("config: data root " + str(_root()) + "; files: " + ", ".join(files)
            + "; set: " + (", ".join(set_keys) or "none")
            + "; unset: " + (", ".join(unset) or "none"))


# --------------------------------------------------------------------------- #
# data/config.env -- the only settings file the app writes (never .env)
# --------------------------------------------------------------------------- #
def _quote(value):
    """A value in dotenv syntax that dotenv_values (interpolate=False) reads back
    unchanged.

    Unquoted is preferred: python-dotenv takes an unquoted value literally (no
    escape processing), so Windows paths keep their backslashes as typed --
    including a TRAILING one, which no quoted form can carry: the quoted-value
    regex reads "\\\\'" at the end as an escaped quote and runs on into the next
    line. Unquoted loses only leading/trailing whitespace, a leading quote
    character and anything after whitespace + "#", so those values are
    single-quoted instead, escaping the only two sequences single quotes decode
    (backslash-backslash and backslash-quote). save_setup re-parses the whole
    file before writing it, so a value neither form can carry is refused rather
    than silently stored wrong."""
    if (value and value == value.strip() and value[0] not in "'\""
            and not re.search(r"\s#", value)):
        return value
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def save_setup(values):
    """Write `values` (a subset of CONFIG_KEYS -> str) into data/config.env under
    the data root, keeping the file's other CONFIG_KEYS. Atomic (temp file +
    os.replace); 0600 on POSIX. An empty string is written as an empty value.
    Raises ValueError on an unknown key or a value with a line break / NUL.
    Never writes .env. When load() has run, os.environ is refreshed so jobs and
    readers see the new values (a key the process environment set is left as is)."""
    unknown = sorted(set(values) - set(CONFIG_KEYS))
    if unknown:
        raise ValueError(f"not a setup key: {', '.join(unknown)}")
    for key, value in values.items():
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        if any(ch in value for ch in ("\r", "\n", "\0")):
            raise ValueError(f"{key} must be a single line")

    path = _root() / CONFIG_ENV
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = _read_file(SOURCE_CONFIG, path) if path.is_file() else {}
    merged.update(values)
    lines = ["# Written by Oshiire's setup. Settings in .env or the environment win over these."]
    lines += [f"{key}={_quote(merged[key])}" for key in CONFIG_KEYS if key in merged]
    text = "\n".join(lines) + "\n"
    # Refuse to write anything that would not read back exactly.
    if dotenv_values(stream=io.StringIO(text), interpolate=False) != merged:
        raise ValueError("a value can't be stored in data/config.env as typed "
                         "(e.g. a quoted value ending in a backslash)")

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        if sys.platform != "win32":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    if _state:
        current, sources = _resolve()
        for key in CONFIG_KEYS:
            if key in _state["process_env"]:
                continue
            if key in current:
                os.environ[key] = current[key]
            else:
                os.environ.pop(key, None)
