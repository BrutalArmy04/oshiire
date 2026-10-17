"""Where Oshiire keeps its data -- the DATA ROOT -- versus where its code lives.

The invariant the rest of the app relies on: the data root is the process cwd.
app.py chdirs here at startup, so every existing cwd-relative path (manifest.json,
staging/..., data/..., layout.json, and the relative local_path values already
stored in a manifest) keeps meaning the same thing. App files -- the scripts,
assets/fonts, the unsave userscript, data/tombstones.json -- are anchored to
APP_DIR instead, so they are found whatever the cwd is.

    OSHIIRE_HOME set           -> that folder (expanded, made absolute)
    release copy (marker file) -> the per-user data folder for this OS
    git checkout (no marker)   -> APP_DIR, i.e. exactly as before

Stdlib only, and no heavy imports: app.py reads this before anything else.
"""
import os
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
APP_DIR = CODE_DIR.parent

# Written by the release build, never by the app and never present in a git
# checkout. Its presence is what makes a downloaded copy keep its data per user.
RELEASE_MARKER = APP_DIR / "oshiire_release.json"

HOME_ENV = "OSHIIRE_HOME"
APP_NAME = "Oshiire"


def is_release(marker=None):
    """True in a downloaded release copy (the marker file exists)."""
    return Path(marker if marker is not None else RELEASE_MARKER).exists()


def _user_data_dir(environ, platform, home):
    """The per-user data folder for a release copy on this OS."""
    if platform == "win32":
        local = environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else home / "AppData" / "Local"
        return base / APP_NAME
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    xdg = environ.get("XDG_DATA_HOME", "").strip()
    # The XDG spec says a relative XDG_DATA_HOME is invalid and must be ignored.
    if xdg and os.path.isabs(xdg):
        return Path(xdg) / APP_NAME.lower()
    return home / ".local" / "share" / APP_NAME.lower()


def data_root(environ=None, platform=None, marker=None, home=None):
    """The data root as an absolute Path. Does not create it (app.py does).

    The parameters exist for tests; normal callers pass none of them."""
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    override = (environ.get(HOME_ENV) or "").strip()
    if override:
        return Path(os.path.abspath(os.path.expanduser(override)))
    if is_release(marker):
        home = Path.home() if home is None else Path(home)
        return Path(os.path.abspath(_user_data_dir(environ, platform, home)))
    return APP_DIR
