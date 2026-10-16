"""The setup window: the saved-feed link, the archive folder and the username.

Two modes:
  "first"  -- opened by app.py on a first run (needs_setup()), in-process,
              before the UI starts. Offers Save / Skip for now / Cancel and two
              extras: a Start-menu (or app-menu) entry and a one-time download
              of the AI tagging model.
  "change" -- `python setup_window.py --change`, spawned from Settings -> App.
              Its cwd is the data root (the parent passes it); it loads config
              itself. Save / Cancel only, no extras.

Everything the window decides is in plain functions above the window class
(validators, needs_setup, the feed test, the shortcut builders, apply_save /
apply_skip), so it is testable without a display. tkinter is imported only when
a window is actually built.

Secrets: the feed link is shown masked, never logged, never echoed in a
message. A setting that comes from the environment or a .env file is shown as
read-only ("Set in .env") without its value, and is never written.
"""
import base64
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import webbrowser
from collections import namedtuple
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

import config
import datahome
import settings

FEED_KEY = "REDDIT_SAVED_FEED_URL"
ARCHIVE_KEY = "ARCHIVE_DIR"
USER_KEY = "REDDIT_USERNAME"
REQUIRED_KEYS = (FEED_KEY, ARCHIVE_KEY)
EDITABLE_KEYS = (FEED_KEY, ARCHIVE_KEY, USER_KEY)

FEEDS_PAGE = "https://old.reddit.com/prefs/feeds/"
REDDIT_HOSTS = ("reddit.com", "www.reddit.com", "old.reddit.com")
FEED_HOST = "www.reddit.com"
FEED_PATH = "/saved.rss"
TEST_TIMEOUT_S = 15  # ingest.fetch_feed's own timeout

RESULT_SAVED = "saved"
RESULT_SKIPPED = "skipped"
RESULT_CANCELLED = "cancelled"
RESULT_UNAVAILABLE = "unavailable"  # no tkinter / no display

COOKIE_LINE = ("Gallery posts need a Reddit login cookie -- that's an advanced "
               "setting, see docs/SETUP.md.")


# --------------------------------------------------------------------------- #
# Does the first-run window need to open?
# --------------------------------------------------------------------------- #
def needs_setup():
    """True when the feed link or the archive folder is missing from the merged
    config AND the window was never answered (data/settings.json has no
    "setup_done": true)."""
    missing = any(not config.get(key).strip() for key in REQUIRED_KEYS)
    return missing and not settings.setup_done()


# --------------------------------------------------------------------------- #
# Validators -- messages never echo the link or its token
# --------------------------------------------------------------------------- #
_USERNAME_RE = re.compile(r"^/?(?:u/|user/)?[A-Za-z0-9_-]{3,20}/?$")

HINT_EMPTY = "Paste the saved-feed link."
HINT_USERNAME = ("That looks like a username. Put it in the Username box below; the "
                 "feed link is a long address from the feeds page.")
HINT_SCHEME = "Copy the whole link -- it starts with https://"
HINT_HOST = "That isn't a reddit.com link. Copy the RSS link from the feeds page."
HINT_FEEDS_PAGE = ("That's the feeds page itself. On that page, copy the RSS link "
                   "next to \"your saved links\".")
HINT_USER_PAGE = ("That's a profile page, not the feed. Copy the RSS link next to "
                  "\"your saved links\" on the feeds page.")
HINT_OTHER_FEED = ("That isn't your saved-posts feed. Copy the RSS link next to "
                   "\"your saved links\".")
HINT_PARAMS = "The link is missing part of itself. Copy it again in full."
MSG_FEED_OK = "Looks like a saved-feed link."


def validate_feed_url(text):
    """(ok, normalized, message). `normalized` is the link with its host set to
    www.reddit.com (old.reddit.com walls logged-out requests) and no fragment;
    '' when not ok. The query -- feed= and user=, in any order -- is kept as
    pasted. `message` never contains the link or its token."""
    value = (text or "").strip().strip('"').strip("'").strip()
    if not value:
        return (False, "", HINT_EMPTY)
    if "://" not in value and _USERNAME_RE.match(value):
        return (False, "", HINT_USERNAME)
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
    except ValueError:
        return (False, "", HINT_SCHEME)
    if parts.scheme.lower() != "https" or not host:
        # "www.reddit.com/saved.rss?..." with no scheme parses as a path.
        return (False, "", HINT_SCHEME)
    if host not in REDDIT_HOSTS:
        return (False, "", HINT_HOST)
    path = parts.path.rstrip("/") or "/"
    lower = path.lower()
    if lower.startswith("/prefs/feeds"):
        return (False, "", HINT_FEEDS_PAGE)
    if lower.startswith("/user/") or lower.startswith("/u/"):
        return (False, "", HINT_USER_PAGE)
    if lower != FEED_PATH:
        return (False, "", HINT_OTHER_FEED)
    query = parse_qs(parts.query, keep_blank_values=True)
    if not (query.get("feed") or [""])[0] or not (query.get("user") or [""])[0]:
        return (False, "", HINT_PARAMS)
    normalized = urlunsplit(("https", FEED_HOST, FEED_PATH, parts.query, ""))
    return (True, normalized, MSG_FEED_OK)


def feed_user(url):
    """The user= value of a feed link ('' if none) -- offered to pre-fill the
    Username box. A username is not a secret."""
    try:
        return (parse_qs(urlsplit(url).query).get("user") or [""])[0].strip()
    except ValueError:
        return ""


ArchiveCheck = namedtuple("ArchiveCheck", "ok message will_create")

MSG_ARCHIVE_EMPTY = "Choose the folder your archive lives in."
MSG_ARCHIVE_RELATIVE = "Use the folder's full path (for example C:\\Users\\you\\Pictures\\Art)."
MSG_ARCHIVE_INSIDE_APP = ("That folder holds Oshiire itself. Choose the folder where your "
                          "pictures are kept instead.")
MSG_ARCHIVE_INSIDE_DATA = ("That folder holds Oshiire's settings. Choose the folder where "
                           "your pictures are kept instead.")
MSG_ARCHIVE_FILE = "That's a file, not a folder."
MSG_ARCHIVE_READONLY = "Oshiire can't write to that folder. Choose another one."
MSG_ARCHIVE_OK = "Folder found."
MSG_ARCHIVE_CREATE = "This folder doesn't exist yet. Oshiire will create it when you save."


def _norm(path):
    """A path for comparison: absolute, resolved where possible, case-folded on
    Windows (normcase), no trailing separator."""
    p = Path(path)
    try:
        p = p.resolve(strict=False)
    except OSError:
        p = Path(os.path.abspath(p))
    return os.path.normcase(os.path.normpath(str(p)))


def _contains(outer, inner):
    """True when `inner` is `outer` or anywhere inside it."""
    outer, inner = _norm(outer), _norm(inner)
    try:
        return os.path.commonpath([outer, inner]) == outer
    except ValueError:  # different drives
        return False


def clean_archive_path(text):
    """The archive path as typed, minus surrounding quotes (Explorer's "Copy as
    path" adds them) and whitespace."""
    return (text or "").strip().strip('"').strip()


def validate_archive_dir(path, app_dir, data_root):
    """ArchiveCheck(ok, message, will_create). Refuses a folder that IS or
    CONTAINS the app folder or the data root: the archive is passed to gradio as
    an allowed path, and a folder holding .env or data/config.env would make
    them servable. An existing folder must be writable; a missing one is ok with
    will_create=True (Save asks before creating it). Cloud-synced folders are
    fine -- they are ordinary folders here."""
    value = clean_archive_path(path)
    if not value:
        return ArchiveCheck(False, MSG_ARCHIVE_EMPTY, False)
    target = Path(value)
    if not target.is_absolute():
        return ArchiveCheck(False, MSG_ARCHIVE_RELATIVE, False)
    if _contains(target, app_dir):
        return ArchiveCheck(False, MSG_ARCHIVE_INSIDE_APP, False)
    if _contains(target, data_root):
        return ArchiveCheck(False, MSG_ARCHIVE_INSIDE_DATA, False)
    try:
        exists = target.exists()
    except OSError:
        exists = False
    if not exists:
        return ArchiveCheck(True, MSG_ARCHIVE_CREATE, True)
    if not target.is_dir():
        return ArchiveCheck(False, MSG_ARCHIVE_FILE, False)
    try:
        fd, probe = tempfile.mkstemp(dir=str(target), prefix=".oshiire-write-test-")
        os.close(fd)
        os.unlink(probe)
    except OSError:
        return ArchiveCheck(False, MSG_ARCHIVE_READONLY, False)
    return ArchiveCheck(True, MSG_ARCHIVE_OK, False)


# --------------------------------------------------------------------------- #
# "Test it" -- one read-only fetch through ingest's own transport and parser
# --------------------------------------------------------------------------- #
MSG_TEST_REFUSED = "Reddit refused the link. Copy it again from the feeds page."
MSG_TEST_WALL = ("Reddit answered with its login page instead of your saves. Copy the "
                 "link again from the feeds page.")
MSG_TEST_TIMEOUT = f"Reddit didn't answer within {TEST_TIMEOUT_S} seconds. Try again."
MSG_TEST_OFFLINE = "Couldn't reach Reddit. Check your internet connection."
MSG_TEST_NOT_FEED = "That link didn't return a feed. Copy it again from the feeds page."


def test_feed(url):
    """(ok, message) for ONE read-only fetch of the feed, via ingest.fetch_feed
    (the same transport, host rewrite, login-wall check and parser ingest uses;
    15 s timeout). Blocking -- the window runs it in a worker thread. The
    message never contains the link: exception texts are not shown, because a
    requests error message carries the URL."""
    ok, normalized, message = validate_feed_url(url)
    if not ok:
        return (False, message)
    import requests
    import ingest
    try:
        parsed = ingest.fetch_feed(normalized)
    except ingest.FeedUnavailable:
        return (False, MSG_TEST_WALL)
    except requests.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (401, 403, 404):
            return (False, MSG_TEST_REFUSED)
        return (False, f"Reddit had a problem answering (error {status}). Try again in a minute.")
    except requests.Timeout:
        return (False, MSG_TEST_TIMEOUT)
    except requests.ConnectionError:
        return (False, MSG_TEST_OFFLINE)
    except Exception as exc:  # never show str(exc): it may carry the URL
        return (False, f"The test failed ({type(exc).__name__}).")
    entries = getattr(parsed, "entries", None) or []
    if not entries and getattr(parsed, "bozo", False):
        return (False, MSG_TEST_NOT_FEED)
    n = len(entries)
    return (True, f"Found {n} saved post{'' if n == 1 else 's'}. The link works.")


# --------------------------------------------------------------------------- #
# Start menu / app menu entry -- builders return what they would do
# --------------------------------------------------------------------------- #
def shortcut_supported(platform=None):
    platform = platform or sys.platform
    return platform == "win32" or platform.startswith("linux")


def shortcut_label(platform=None):
    platform = platform or sys.platform
    return ("Add Oshiire to the Start menu" if platform == "win32"
            else "Add Oshiire to the app menu")


def _app_icon(app_dir, suffixes):
    assets = Path(app_dir) / "assets"
    for suffix in suffixes:
        found = sorted(assets.glob(f"*{suffix}")) if assets.is_dir() else []
        if found:
            return found[0]
    return None


def windows_shortcut_path(environ=None):
    environ = os.environ if environ is None else environ
    appdata = environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Oshiire.lnk"


def gui_python(executable=None):
    """pythonw.exe next to the running interpreter when it exists (no console
    window), else the interpreter itself."""
    exe = Path(executable or sys.executable)
    sibling = exe.with_name("pythonw.exe")
    return sibling if sibling.exists() else exe


def _ps_quote(text):
    return "'" + str(text).replace("'", "''") + "'"


def build_windows_shortcut(app_dir=None, executable=None, environ=None):
    """{"path", "target", "arguments", "workdir", "icon", "script", "argv"} for
    the Start-menu shortcut. Nothing is created: `argv` is the PowerShell command
    (WScript.Shell, no extra dependency) that create_shortcut() would run, with
    the script passed -EncodedCommand so no path needs shell quoting."""
    app_dir = Path(app_dir or datahome.APP_DIR)
    lnk = windows_shortcut_path(environ)
    target = gui_python(executable)
    arguments = f'"{app_dir / "app.py"}"'
    icon = _app_icon(app_dir, (".ico",))
    lines = [
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(lnk)})",
        f"$s.TargetPath = {_ps_quote(target)}",
        f"$s.Arguments = {_ps_quote(arguments)}",
        f"$s.WorkingDirectory = {_ps_quote(app_dir)}",
        "$s.Description = 'Oshiire'",
    ]
    if icon is not None:
        lines.append(f"$s.IconLocation = {_ps_quote(icon)}")
    lines.append("$s.Save()")
    script = "\n".join(lines)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    argv = ["powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
    return {"path": lnk, "target": target, "arguments": arguments, "workdir": app_dir,
            "icon": icon, "script": script, "argv": argv}


def _desktop_exec_quote(path):
    # Desktop Entry spec: inside double quotes, escape " ` $ and \ with a backslash.
    escaped = re.sub(r'(["`$\\])', r"\\\1", str(path))
    return f'"{escaped}"'


def linux_desktop_path(environ=None, home=None):
    environ = os.environ if environ is None else environ
    xdg = (environ.get("XDG_DATA_HOME") or "").strip()
    base = Path(xdg) if xdg and os.path.isabs(xdg) else Path(home or Path.home()) / ".local" / "share"
    return base / "applications" / "oshiire.desktop"


def build_linux_desktop(app_dir=None, environ=None, home=None):
    """{"path", "content"} for ~/.local/share/applications/oshiire.desktop.
    Nothing is created."""
    app_dir = Path(app_dir or datahome.APP_DIR)
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=Oshiire",
        "Comment=Sort the art you saved on Reddit",
        f"Exec={_desktop_exec_quote(app_dir / 'oshiire.sh')}",
        f"Path={app_dir}",
        "Terminal=false",
        "Categories=Graphics;",
    ]
    icon = _app_icon(app_dir, (".svg", ".png"))
    if icon is not None:
        lines.append(f"Icon={icon}")
    return {"path": linux_desktop_path(environ, home), "content": "\n".join(lines) + "\n"}


def create_shortcut(platform=None, runner=None):
    """Create the Start-menu / app-menu entry. Returns (ok, message). `runner`
    (default subprocess.run) is how tests keep this from touching the machine."""
    platform = platform or sys.platform
    runner = runner or subprocess.run
    try:
        if platform == "win32":
            spec = build_windows_shortcut()
            spec["path"].parent.mkdir(parents=True, exist_ok=True)
            done = runner(spec["argv"], capture_output=True, timeout=30,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if getattr(done, "returncode", 1) != 0:
                return (False, "Couldn't add Oshiire to the Start menu.")
            return (True, "Added Oshiire to the Start menu.")
        if platform.startswith("linux"):
            spec = build_linux_desktop()
            spec["path"].parent.mkdir(parents=True, exist_ok=True)
            tmp = spec["path"].with_suffix(".tmp")
            tmp.write_text(spec["content"], encoding="utf-8")
            os.replace(tmp, spec["path"])
            return (True, "Added Oshiire to the app menu.")
    except (OSError, subprocess.SubprocessError):
        pass
    return (False, "Couldn't add Oshiire to the menu.")


# --------------------------------------------------------------------------- #
# Save / Skip -- the only two ways the window writes anything
# --------------------------------------------------------------------------- #
def editable_keys():
    """The setup keys the window may write: those not already set by the
    environment or a .env file (those are shown read-only)."""
    sources = config.key_sources()
    return [key for key in EDITABLE_KEYS if sources.get(key) not in (config.SOURCE_ENV,
                                                                      config.SOURCE_DOTENV)]


def apply_save(values):
    """Write the editable subset of `values` via config.save_setup, then mark
    setup done. Keys set in the environment or a .env are never written."""
    allowed = set(editable_keys())
    to_save = {key: value for key, value in values.items() if key in allowed}
    if to_save:
        config.save_setup(to_save)
    settings.save_setup_done(True)
    return to_save


def apply_skip():
    """Skip for now: remember the window was answered, write nothing else."""
    settings.save_setup_done(True)


# --------------------------------------------------------------------------- #
# Appearance + DPI
# --------------------------------------------------------------------------- #
PALETTE = {
    "light": {"paper": "#e9ecee", "door": "#f7f8f9", "ink": "#1e2a44", "ink2": "#4a5a7a",
              "btn": "#1e2a44", "on_btn": "#f7f8f9", "seal": "#c8361d", "ok": "#2e7d4f",
              "line": "#1e2a44"},
    "dark": {"paper": "#141925", "door": "#1d2436", "ink": "#e4e8f1", "ink2": "#a3adc2",
             "btn": "#c9d4ee", "on_btn": "#141925", "seal": "#ef6a4f", "ok": "#7fd1a0",
             "line": "#6677a3"},
}


def _system_is_dark():
    """Windows' "apps use dark mode" setting; light everywhere else (cheap)."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return value == 0
    except OSError:
        return False


def resolve_theme(theme_value=None):
    theme_value = theme_value or settings.theme()
    if theme_value in ("light", "dark"):
        return theme_value
    return "dark" if _system_is_dark() else "light"


def _enable_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except Exception:
        pass  # already set for this process, or an older Windows


# --------------------------------------------------------------------------- #
# The window
# --------------------------------------------------------------------------- #
SOURCE_LABELS = {config.SOURCE_DOTENV: "Set in .env", config.SOURCE_ENV: "Set in the environment"}


class SetupWindow:
    """The Tk window. `confirm(title, text) -> bool` asks before creating a
    missing archive folder (messagebox.askyesno by default; tests inject one).
    After run()/close, `result` is one of the RESULT_* values."""

    def __init__(self, mode="first", root=None, confirm=None):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.mode = mode
        self.result = RESULT_CANCELLED
        self._events = queue.Queue()  # worker threads -> Tk thread, plain data only
        self._closed = False
        self.root = root or tk.Tk()
        self._confirm = confirm or self._ask_yes_no
        self.colors = PALETTE[resolve_theme()]
        self.sources = config.key_sources()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.cancel)
        self.root.after(100, self._drain_events)

    # -- layout ---------------------------------------------------------------
    def _build(self):
        tk, ttk, c = self.tk, self.ttk, self.colors
        root = self.root
        root.title("Set up Oshiire")
        root.configure(bg=c["paper"])
        root.minsize(560, 0)
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=c["paper"], foreground=c["ink"])
        style.configure("TFrame", background=c["paper"])
        style.configure("Card.TFrame", background=c["door"], relief="flat")
        style.configure("TLabel", background=c["paper"], foreground=c["ink"])
        style.configure("Card.TLabel", background=c["door"], foreground=c["ink"])
        style.configure("Hint.TLabel", background=c["door"], foreground=c["ink2"])
        style.configure("Note.TLabel", background=c["paper"], foreground=c["ink2"])
        style.configure("Title.TLabel", background=c["paper"], foreground=c["ink"],
                        font=("Segoe UI", 16, "bold"))
        style.configure("Head.TLabel", background=c["door"], foreground=c["ink"],
                        font=("Segoe UI", 11, "bold"))
        style.configure("Bad.TLabel", background=c["door"], foreground=c["seal"])
        style.configure("Good.TLabel", background=c["door"], foreground=c["ok"])
        style.configure("TButton", background=c["door"], foreground=c["ink"],
                        bordercolor=c["line"], padding=(10, 4))
        style.map("TButton", background=[("active", c["paper"])])
        style.configure("Primary.TButton", background=c["btn"], foreground=c["on_btn"])
        style.map("Primary.TButton", background=[("active", c["btn"])],
                  foreground=[("active", c["on_btn"])])
        style.configure("TCheckbutton", background=c["door"], foreground=c["ink"])
        style.map("TCheckbutton", background=[("active", c["door"])])
        style.configure("Page.TCheckbutton", background=c["paper"], foreground=c["ink"])
        style.map("Page.TCheckbutton", background=[("active", c["paper"])])
        style.configure("TEntry", fieldbackground=c["door"], foreground=c["ink"],
                        insertcolor=c["ink"])

        outer = ttk.Frame(root, padding=18)
        outer.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        row = 0
        ttk.Label(outer, text="Set up Oshiire", style="Title.TLabel").grid(row=row, sticky="w")
        row += 1
        intro = ("Oshiire needs your saved-posts feed link and the folder your archive "
                 "lives in." if self.mode == "first" else
                 "Change Oshiire's setup. Changes take effect the next time you start Oshiire.")
        ttk.Label(outer, text=intro, style="Note.TLabel", wraplength=520).grid(
            row=row, sticky="w", pady=(2, 12))
        row += 1

        # -- feed link --
        self.feed_var = tk.StringVar(value=self._prefill(FEED_KEY))
        self.show_var = tk.BooleanVar(value=False)
        card = self._card(outer, row, "Saved-feed link")
        row += 1
        guide = ("1. Open the feeds page (log in to Reddit if asked).\n"
                 "2. Find \"your saved links\" and copy its RSS link.\n"
                 "3. Paste it here.")
        ttk.Label(card, text=guide, style="Hint.TLabel", justify="left").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(2, 6))
        if self._locked(FEED_KEY):
            self._locked_row(card, 2, FEED_KEY)
            self.feed_entry = None
        else:
            ttk.Button(card, text="Open the feeds page",
                       command=lambda: webbrowser.open(FEEDS_PAGE)).grid(
                row=2, column=0, sticky="w", pady=(0, 6))
            self.feed_entry = ttk.Entry(card, textvariable=self.feed_var, show="•", width=56)
            self.feed_entry.grid(row=3, column=0, sticky="ew")
            ttk.Checkbutton(card, text="Show", variable=self.show_var,
                            command=self._toggle_show).grid(row=3, column=1, padx=(8, 0))
            self.test_btn = ttk.Button(card, text="Test it", command=self.start_test)
            self.test_btn.grid(row=3, column=2, padx=(8, 0))
            self.feed_var.trace_add("write", lambda *_: self._feed_changed())
        self.feed_msg = ttk.Label(card, text="", style="Hint.TLabel", wraplength=500)
        self.feed_msg.grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # -- archive folder --
        self.archive_var = tk.StringVar(value=self._prefill(ARCHIVE_KEY))
        card = self._card(outer, row, "Archive folder")
        row += 1
        ttk.Label(card, text="Where your sorted pictures are kept. A cloud-synced "
                             "folder (Google Drive, OneDrive) is fine.",
                  style="Hint.TLabel", wraplength=500).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(2, 6))
        if self._locked(ARCHIVE_KEY):
            self._locked_row(card, 2, ARCHIVE_KEY)
        else:
            ttk.Entry(card, textvariable=self.archive_var, width=56).grid(
                row=2, column=0, sticky="ew")
            ttk.Button(card, text="Browse...", command=self._browse).grid(
                row=2, column=1, padx=(8, 0))
        self.archive_msg = ttk.Label(card, text="", style="Hint.TLabel", wraplength=500)
        self.archive_msg.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # -- username --
        self.user_var = tk.StringVar(value=self._prefill(USER_KEY))
        card = self._card(outer, row, "Reddit username (optional)")
        row += 1
        if self._locked(USER_KEY):
            self._locked_row(card, 1, USER_KEY)
        else:
            ttk.Entry(card, textvariable=self.user_var, width=30).grid(
                row=1, column=0, sticky="w", pady=(4, 0))

        ttk.Label(outer, text=COOKIE_LINE, style="Note.TLabel", wraplength=520).grid(
            row=row, sticky="w", pady=(4, 8))
        row += 1

        # -- first-run extras --
        self.shortcut_var = tk.BooleanVar(value=datahome.is_release())
        if self.mode == "first":
            extras = ttk.Frame(outer)
            extras.grid(row=row, sticky="ew", pady=(0, 8))
            row += 1
            if shortcut_supported():
                ttk.Checkbutton(extras, text=shortcut_label(), variable=self.shortcut_var,
                                style="Page.TCheckbutton").grid(row=0, column=0, sticky="w")
            self.model_btn = ttk.Button(
                extras, text="Download the AI model now (one time, about 450 MB)",
                command=self.start_model_download)
            self.model_btn.grid(row=1, column=0, sticky="w", pady=(6, 0))
            self.model_bar = ttk.Progressbar(extras, mode="indeterminate", length=200)
            self.model_msg = ttk.Label(extras, text="", style="Note.TLabel")
            self.model_msg.grid(row=2, column=0, sticky="w")

        # -- buttons --
        buttons = ttk.Frame(outer)
        buttons.grid(row=row, sticky="e", pady=(8, 0))
        col = 0
        ttk.Button(buttons, text="Save", style="Primary.TButton", command=self.save).grid(
            row=0, column=col, padx=(0, 8))
        col += 1
        if self.mode == "first":
            ttk.Button(buttons, text="Skip for now", command=self.skip).grid(
                row=0, column=col, padx=(0, 8))
            col += 1
        ttk.Button(buttons, text="Cancel", command=self.cancel).grid(row=0, column=col)

    def _card(self, parent, row, title):
        frame = self.ttk.Frame(parent, style="Card.TFrame", padding=12)
        frame.grid(row=row, sticky="ew", pady=(0, 10))
        frame.columnconfigure(0, weight=1)
        self.ttk.Label(frame, text=title, style="Head.TLabel").grid(row=0, column=0, sticky="w")
        return frame

    def _locked(self, key):
        return self.sources.get(key) in SOURCE_LABELS

    def _locked_row(self, card, row, key):
        self.ttk.Label(card, text=SOURCE_LABELS[self.sources[key]] + " (change it there).",
                       style="Card.TLabel").grid(row=row, column=0, sticky="w")

    def _prefill(self, key):
        """Only a value Oshiire itself stored (data/config.env) is shown back."""
        if self.sources.get(key) == config.SOURCE_CONFIG:
            return config.get(key)
        return ""

    def _ask_yes_no(self, title, text):
        from tkinter import messagebox
        return messagebox.askyesno(title, text, parent=self.root)

    # -- small actions ----------------------------------------------------------
    def _toggle_show(self):
        if self.feed_entry is not None:
            self.feed_entry.configure(show="" if self.show_var.get() else "•")

    def _feed_changed(self):
        self.feed_msg.configure(text="", style="Hint.TLabel")
        ok, normalized, _ = validate_feed_url(self.feed_var.get())
        if ok and not self.user_var.get().strip() and not self._locked(USER_KEY):
            self.user_var.set(feed_user(normalized))

    def _browse(self):
        from tkinter import filedialog
        chosen = filedialog.askdirectory(parent=self.root, mustexist=False,
                                         title="Choose your archive folder")
        if chosen:
            self.archive_var.set(str(Path(chosen)))
            self.archive_msg.configure(text="", style="Hint.TLabel")

    def _say(self, label, text, good=None):
        style = "Hint.TLabel" if good is None else ("Good.TLabel" if good else "Bad.TLabel")
        label.configure(text=text, style=style)

    # -- worker threads (they only ever put plain tuples on the queue) ----------
    def start_test(self):
        url = self.feed_var.get()
        ok, _, message = validate_feed_url(url)
        if not ok:
            self._say(self.feed_msg, message, good=False)
            return
        self.test_btn.state(["disabled"])
        self._say(self.feed_msg, "Testing...")
        events = self._events

        def work():
            try:
                result = test_feed(url)
            except Exception as exc:
                result = (False, f"The test failed ({type(exc).__name__}).")
            events.put(("test", result))
        threading.Thread(target=work, daemon=True).start()

    def start_model_download(self):
        self.model_btn.state(["disabled"])
        self.model_bar.grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.model_bar.start(12)
        self.model_msg.configure(text="Downloading... you can save and carry on meanwhile.")
        events = self._events

        def work():
            events.put(("model", download_model_in_child()))
        # Daemon: if the app carries on to Home mid-download this thread keeps
        # waiting on the child; the child itself is not tied to this thread.
        threading.Thread(target=work, daemon=True).start()

    def _drain_events(self):
        if self._closed:
            return
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "test":
                    ok, message = payload
                    self.test_btn.state(["!disabled"])
                    self._say(self.feed_msg, message, good=ok)
                elif kind == "model":
                    ok, message = payload
                    self.model_bar.stop()
                    self.model_bar.grid_remove()
                    self.model_msg.configure(text=message)
                    if not ok:
                        self.model_btn.state(["!disabled"])
        except queue.Empty:
            pass
        self.root.after(150, self._drain_events)

    # -- Save / Skip / Cancel -----------------------------------------------------
    def collect(self):
        """Validate the fields. Returns ({key: value}, ok); messages are shown
        on the fields. A missing archive folder is created only after `confirm`."""
        values, ok = {}, True
        if not self._locked(FEED_KEY):
            text = self.feed_var.get().strip()
            if text:
                valid, normalized, message = validate_feed_url(text)
                if valid:
                    values[FEED_KEY] = normalized
                else:
                    self._say(self.feed_msg, message, good=False)
                    ok = False
            else:
                values[FEED_KEY] = ""
        if not self._locked(ARCHIVE_KEY):
            text = clean_archive_path(self.archive_var.get())
            if text:
                check = validate_archive_dir(text, datahome.APP_DIR, Path.cwd())
                if not check.ok:
                    self._say(self.archive_msg, check.message, good=False)
                    ok = False
                elif check.will_create:
                    if self._confirm("Create the folder?",
                                     "That folder doesn't exist yet. Create it now?"):
                        try:
                            Path(text).mkdir(parents=True, exist_ok=True)
                            values[ARCHIVE_KEY] = str(Path(text))
                        except OSError:
                            self._say(self.archive_msg, "Couldn't create that folder.", good=False)
                            ok = False
                    else:
                        self._say(self.archive_msg, MSG_ARCHIVE_CREATE)
                        ok = False
                else:
                    values[ARCHIVE_KEY] = str(Path(text))
            else:
                values[ARCHIVE_KEY] = ""
        if not self._locked(USER_KEY):
            values[USER_KEY] = self.user_var.get().strip().lstrip("/").removeprefix("u/")
        return values, ok

    def save(self):
        values, ok = self.collect()
        if not ok:
            return False
        try:
            apply_save(values)
        except (OSError, ValueError) as exc:
            self._say(self.archive_msg, f"Couldn't save: {type(exc).__name__}.", good=False)
            return False
        if self.mode == "first" and shortcut_supported() and self.shortcut_var.get():
            ok_sc, message = create_shortcut()
            print(message)
        self.result = RESULT_SAVED
        self._close()
        return True

    def skip(self):
        apply_skip()
        self.result = RESULT_SKIPPED
        self._close()

    def cancel(self):
        self.result = RESULT_CANCELLED
        self._close()

    def _close(self):
        self._closed = True
        try:
            self.root.quit()
        except Exception:
            pass

    def run(self):
        self.root.lift()
        try:
            self.root.attributes("-topmost", True)
            self.root.after(800, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass
        self.root.focus_force()
        self.root.mainloop()
        return self.result

    def destroy(self):
        self._closed = True
        try:
            self.root.destroy()
        except Exception:
            pass


def download_model_in_child(executable=None, runner=None):
    """(ok, message). Runs `setup_window.py --download-model` in a separate
    process so the heavy imgutils/onnxruntime import never lands in the app's own
    process. The child is not killed if the app quits mid-download."""
    runner = runner or subprocess.run
    argv = [str(executable or sys.executable), str(datahome.APP_DIR / "setup_window.py"),
            "--download-model"]
    try:
        done = runner(argv, cwd=str(Path.cwd()), stdin=subprocess.DEVNULL,
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        done = None
    if done is not None and done.returncode == 0:
        return (True, "Done -- the AI model is ready.")
    return (False, "The download didn't finish (check your internet connection). "
                   "Oshiire will try again the first time it tags.")


def run_setup_window(mode="first", confirm=None):
    """Open the window, block until it closes, destroy Tk completely, and return
    the RESULT_* value. RESULT_UNAVAILABLE when tkinter or a display is missing."""
    _enable_dpi_awareness()
    try:
        import tkinter  # noqa: F401
        window = SetupWindow(mode=mode, confirm=confirm)
    except Exception as exc:
        print(f"The setup window couldn't open ({type(exc).__name__}).")
        return RESULT_UNAVAILABLE
    try:
        return window.run()
    finally:
        window.destroy()
        del window
        import gc
        gc.collect()  # drop Tk objects here, on this thread, before anything else runs


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--download-model" in argv:
        import ai_tagger
        ai_tagger.download_model()
        return 0
    if "--change" in argv:
        config.load()  # cwd is the data root, set by the parent
        result = run_setup_window("change")
        print(f"setup window: {result}")
        return 0
    print("usage: setup_window.py --change")
    return 2


if __name__ == "__main__":
    sys.exit(main())
