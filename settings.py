"""App-level preferences, kept in data/settings.json.

Separate from .env on purpose: .env holds SECRETS and the app must never write
it. This file holds only non-secret UI preferences the app itself owns and
rewrites:

    {"window": "browser" | "native",       how Oshiire opens
     "theme": "system" | "light" | "dark",  its appearance
     "setup_done": true | false}            the first-run setup window was answered

Anything unreadable, missing or out of range reads back as the safe default:
"browser" (open in the default web browser), "system", and false. No gradio, no review import, so
both entry points can read the mode without pulling in the heavy UI graph.
"""
import json
import os
import tempfile
from pathlib import Path

import fsutil

SETTINGS_PATH = Path("data/settings.json")

WINDOW_KEY = "window"
DEFAULT_WINDOW = "browser"
VALID_WINDOWS = ("browser", "native")

THEME_KEY = "theme"
DEFAULT_THEME = "system"
VALID_THEMES = ("system", "light", "dark")


def load_settings(path=SETTINGS_PATH):
    """The settings dict, or {} when the file is missing or unreadable. Never
    raises: a corrupt or absent file is a return of {}, not a crash."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def window_mode(path=SETTINGS_PATH):
    """"browser" or "native". Any missing file, missing key, wrong type or
    unknown value falls back to DEFAULT_WINDOW ("browser")."""
    value = load_settings(path).get(WINDOW_KEY)
    return value if value in VALID_WINDOWS else DEFAULT_WINDOW


def theme(path=SETTINGS_PATH):
    """"system" | "light" | "dark". Missing file/key, wrong type or unknown
    value falls back to DEFAULT_THEME ("system" -- follow the OS)."""
    value = load_settings(path).get(THEME_KEY)
    return value if value in VALID_THEMES else DEFAULT_THEME


def theme_param(theme_value):
    """The URL query fragment that forces a theme: "" for "system" (let gradio
    follow prefers-color-scheme), else "__theme=light"/"__theme=dark". An
    unknown value is treated as system (no param)."""
    return f"__theme={theme_value}" if theme_value in ("light", "dark") else ""


def _save_key(key, value, valid, path):
    """Merge {key: value} into the settings file and write it atomically (temp
    file + os.replace, the manifest's discipline). Rejects an out-of-range value
    so a typo can never be written; the UI only ever submits a valid one. Other
    keys already in the file are preserved."""
    if value not in valid:
        raise ValueError(f"unknown {key} {value!r}")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load_settings(path)
    data[key] = value
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        fsutil.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_window_mode(mode, path=SETTINGS_PATH):
    """Persist the launch mode atomically (see _save_key)."""
    _save_key(WINDOW_KEY, mode, VALID_WINDOWS, path)


def save_theme(theme_value, path=SETTINGS_PATH):
    """Persist the appearance theme atomically (see _save_key)."""
    _save_key(THEME_KEY, theme_value, VALID_THEMES, path)


# Set once the first-run setup window was answered (Save or Skip), so it stops
# opening by itself even when the feed URL or archive folder is still missing.
SETUP_DONE_KEY = "setup_done"


def setup_done(path=SETTINGS_PATH):
    """True only when the file holds "setup_done": true (a real bool)."""
    return load_settings(path).get(SETUP_DONE_KEY) is True


def save_setup_done(done=True, path=SETTINGS_PATH):
    """Persist the setup_done flag atomically (see _save_key)."""
    _save_key(SETUP_DONE_KEY, bool(done), (True, False), path)
