"""The setup window: the Reddit username and login cookie, and the archive folder.

Two modes:
  "first"  -- opened by app.py on a first run (needs_setup()), in-process,
              before the UI starts. Offers Save / Skip for now / Cancel and two
              extras: a Start-menu (or app-menu) entry and a one-time download
              of the AI tagging model.
  "change" -- `python src/setup_window.py --change`, spawned from Settings -> App.
              Its cwd is the data root (the parent passes it); it loads config
              itself. Save / Cancel only, no extras.

Everything the window decides is in plain functions above the window class
(validators, needs_setup, the login test, the shortcut builders, apply_save /
apply_skip), so it is testable without a display. tkinter is imported only when
a window is actually built.

Secrets: the login cookie is shown masked, never logged, never echoed in a
message. A setting that comes from the environment or a .env file is shown as
read-only ("Set in .env") without its value, and is never written.
"""
import base64
import itertools
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

import config
import datahome
import fsutil
import settings

ARCHIVE_KEY = "ARCHIVE_DIR"
USER_KEY = "REDDIT_USERNAME"
COOKIE_KEY = "REDDIT_SESSION_COOKIE"
REQUIRED_KEYS = (ARCHIVE_KEY, USER_KEY, COOKIE_KEY)
EDITABLE_KEYS = (COOKIE_KEY, ARCHIVE_KEY, USER_KEY)

TEST_TIMEOUT_S = 15  # redditclient.DEFAULT_TIMEOUT

RESULT_SAVED = "saved"
RESULT_SKIPPED = "skipped"
RESULT_CANCELLED = "cancelled"
RESULT_UNAVAILABLE = "unavailable"  # no tkinter / no display

GITHUB_REPO_URL = "https://github.com/BrutalArmy04/oshiire"
COOKIE_DOCS_ANCHOR = "reddit-login-cookie"  # docs/SETUP.md's heading
COOKIE_DOCS_URL = f"{GITHUB_REPO_URL}/blob/main/docs/SETUP.md#{COOKIE_DOCS_ANCHOR}"
COOKIE_LINK = "More help copying it"


# --------------------------------------------------------------------------- #
# Does the first-run window need to open?
# --------------------------------------------------------------------------- #
FEED_KEY = "REDDIT_SAVED_FEED_URL"
FEED_NOTICE = ("Reddit ended RSS feeds on 13 November 2026. Oshiire now reads your saved "
               "list while logged in, so it needs your username and login cookie. Your old "
               "feed link is no longer used.")


def coming_from_feed():
    """True for someone upgrading from the RSS-feed versions: the old feed link is
    still in the merged config and the username or the login cookie is missing."""
    if not config.get(FEED_KEY).strip():
        return False
    return not config.get(USER_KEY).strip() or not config.get(COOKIE_KEY).strip()


def needs_setup():
    """True when the username, the login cookie or the archive folder is missing
    from the merged config AND the window was never answered (data/settings.json has no
    "setup_done": true). Also true, once, for an upgrader from the feed versions
    (coming_from_feed) who has setup_done but never saw the window since."""
    missing = any(not config.get(key).strip() for key in REQUIRED_KEYS)
    if missing and not settings.setup_done():
        return True
    return coming_from_feed() and not settings.feed_migration_done()


def _mark_answered():
    settings.save_setup_done(True)
    if coming_from_feed():
        settings.save_feed_migration_done(True)


# --------------------------------------------------------------------------- #
# Validators -- messages never echo the cookie
# --------------------------------------------------------------------------- #
_USERNAME_RE = re.compile(r"^/?(?:u/|user/)?[A-Za-z0-9_-]{3,20}/?$")
_USERNAME_PREFIX_RE = re.compile(r"^/?(?:u/|user/)?")

HINT_USER_EMPTY = "Enter your Reddit username."
HINT_USER_BAD = ("That doesn't look like a Reddit username (3-20 letters, numbers, "
                 "dashes or underscores).")
HINT_COOKIE_EMPTY = "Paste your Reddit login cookie."
HINT_COOKIE_LINES = "That has line breaks in it. Copy the Cookie value again as one line."
HINT_COOKIE_BAD = ("That doesn't look like a Reddit login cookie -- it should contain "
                   "reddit_session=. Copy the whole Cookie value again from the request "
                   "headers.")
_COOKIE_PREFIX_RE = re.compile(r"^cookie\s*:\s*", re.IGNORECASE)
_COOKIE_SESSION_RE = re.compile(r"(?:^|;\s*)reddit_session=[^;\s]+", re.IGNORECASE)


def clean_username(text):
    """(ok, name, message). Accepts `name`, `u/name` and `/user/name/`; `name`
    is the bare username, '' when not ok."""
    value = (text or "").strip()
    if not value:
        return (False, "", HINT_USER_EMPTY)
    if not _USERNAME_RE.match(value):
        return (False, "", HINT_USER_BAD)
    return (True, _USERNAME_PREFIX_RE.sub("", value).rstrip("/"), "")


def _strip_quotes(value):
    value = value.strip()
    while len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def clean_cookie(text):
    """(ok, cookie, message). Strips surrounding quotes and a leading "Cookie:"
    (what DevTools' "copy" and a pasted header line carry), then requires a
    reddit_session= value. `cookie` is '' when not ok. `message` never contains
    any part of the value."""
    value = _strip_quotes(text or "")
    value = _strip_quotes(_COOKIE_PREFIX_RE.sub("", value))
    if not value:
        return (False, "", HINT_COOKIE_EMPTY)
    if any(ch in value for ch in "\r\n\0"):
        return (False, "", HINT_COOKIE_LINES)
    if not _COOKIE_SESSION_RE.search(value):
        return (False, "", HINT_COOKIE_BAD)
    return (True, value, "")


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
# "Test it" -- ONE read-only fetch of saved page 1, through savedlisting
# --------------------------------------------------------------------------- #
MSG_TEST_TIMEOUT = f"Reddit didn't answer within {TEST_TIMEOUT_S} seconds. Try again."
MSG_TEST_OFFLINE = "Couldn't reach Reddit. Check your internet connection."
MSG_TEST_OK = "Logged in. Your saved list loads."
MSG_TEST_BUSY = "Reddit is rate limiting right now. Try again in a minute."
MSG_TEST_COPY_AGAIN = "Copy the cookie again from your browser."
MSG_TEST_FIX_ONE = "Copy the cookie from that account, or correct the username."


def test_login(username, cookie):
    """(ok, message) for ONE read-only fetch of saved page 1 with the given
    username and cookie (not necessarily saved yet): logged in and how many saves
    are visible, cookie expired or wrong, or a login for a different account than
    the username. Blocking -- the window runs it in a worker thread. The message
    never contains the cookie: exception texts are not shown."""
    ok, name, message = clean_username(username)
    if not ok:
        return (False, message)
    ok, cleaned, message = clean_cookie(cookie)
    if not ok:
        return (False, message)
    import redditclient
    import savedlisting
    try:
        savedlisting.fetch_page(savedlisting.listing_url(name), cookie=cleaned)
    except savedlisting.SavedAuthError as exc:
        tail = MSG_TEST_FIX_ONE if exc.logged_in else MSG_TEST_COPY_AGAIN
        return (False, f"{savedlisting.auth_message(exc, name)} {tail}")
    except savedlisting.SavedRateLimited:
        return (False, MSG_TEST_BUSY)
    except redditclient.RedditFetchError as exc:
        if "timeout" in (exc.kind or "").lower():
            return (False, MSG_TEST_TIMEOUT)
        return (False, MSG_TEST_OFFLINE)
    except Exception as exc:  # never show str(exc)
        return (False, f"The test failed ({type(exc).__name__}).")
    return (True, MSG_TEST_OK)


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
    arguments = f'"{app_dir / "src" / "app.py"}"'
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
            fsutil.replace(tmp, spec["path"])
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
    _mark_answered()
    return to_save


def apply_skip():
    """Skip for now: remember the window was answered, write nothing else."""
    _mark_answered()


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


GSETTINGS_TIMEOUT_S = 1


def _gsettings(key, runner=None):
    """`gsettings get org.gnome.desktop.interface <key>` unquoted, or '' when
    gsettings is missing, fails, or takes longer than a second."""
    runner = runner or subprocess.run
    try:
        done = runner(["gsettings", "get", "org.gnome.desktop.interface", key],
                      capture_output=True, text=True, timeout=GSETTINGS_TIMEOUT_S,
                      stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    if getattr(done, "returncode", 1) != 0:
        return ""
    return (getattr(done, "stdout", "") or "").strip().strip("'\"").strip()


def _linux_is_dark(runner=None):
    """GNOME's color-scheme ('prefer-dark'), else a gtk-theme named *-dark.
    Light when neither says dark or gsettings can't answer."""
    if _gsettings("color-scheme", runner) == "prefer-dark":
        return True
    return _gsettings("gtk-theme", runner).lower().endswith("-dark")


def resolve_theme(theme_value=None, platform=None):
    theme_value = theme_value or settings.theme()
    if theme_value in ("light", "dark"):
        return theme_value
    platform = platform or sys.platform
    if platform.startswith("linux"):
        return "dark" if _linux_is_dark() else "light"
    return "dark" if _system_is_dark() else "light"


# Body text family per platform; headings are the same family, bold and larger.
WINDOWS_FONT = "Segoe UI"
LINUX_FONTS = ("Noto Sans", "Ubuntu", "Cantarell", "DejaVu Sans")
TITLE_SIZE = 16
HEAD_SIZE = 11
# Tk's named fonts every ttk widget draws with (TkFixedFont: the window shows no
# monospace text, so it is left alone).
TK_NAMED_FONTS = ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
                  "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont")


def choose_font_family(available, platform=None):
    """The body family to use, from the families Tk actually has, or None to
    keep TkDefaultFont's own family. Windows: Segoe UI (today's look). Linux:
    the first of LINUX_FONTS installed -- a family Tk doesn't have would fall
    back to a bitmap X11 font."""
    platform = platform or sys.platform
    available = set(available)
    if platform == "win32":
        return WINDOWS_FONT if WINDOWS_FONT in available else None
    if platform.startswith("linux"):
        return next((name for name in LINUX_FONTS if name in available), None)
    return None


def _enable_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except Exception:
        pass  # already set for this process, or an older Windows


CHECK_MARK = ((0.22, 0.52), (0.42, 0.72), (0.78, 0.30))  # fractions of the box
_ELEMENT_IDS = itertools.count(1)


def check_images(tk, master, size, box, edge, accent, mark):
    """(off, on) PhotoImages for a checkbox: an outlined box, and an accent-
    filled box with a check mark. Each is `size` square plus a transparent gap
    on the right that spaces the label."""
    gap = max(4, size // 3)
    border = max(1, size // 14)
    images = []
    for fill, outline in ((box, edge), (accent, accent)):
        image = tk.PhotoImage(master=master, width=size + gap, height=size)
        image.put(outline, to=(0, 0, size, size))
        image.put(fill, to=(border, border, size - border, size - border))
        images.append(image)
    stroke = max(2, round(size / 7))
    points = [(x * size, y * size) for x, y in CHECK_MARK]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        steps = int(max(abs(x1 - x0), abs(y1 - y0)) * 2) + 1
        for i in range(steps + 1):
            x = int(x0 + (x1 - x0) * i / steps - stroke / 2)
            y = int(y0 + (y1 - y0) * i / steps - stroke / 2)
            images[1].put(mark, to=(x, y, x + stroke, y + stroke))
    return tuple(images)


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
        self._setup_fonts()
        style.configure(".", background=c["paper"], foreground=c["ink"])
        style.configure("TFrame", background=c["paper"])
        style.configure("Card.TFrame", background=c["door"], relief="flat")
        style.configure("TLabel", background=c["paper"], foreground=c["ink"])
        style.configure("Card.TLabel", background=c["door"], foreground=c["ink"])
        style.configure("Hint.TLabel", background=c["door"], foreground=c["ink2"])
        style.configure("Note.TLabel", background=c["paper"], foreground=c["ink2"])
        style.configure("Title.TLabel", background=c["paper"], foreground=c["ink"],
                        font=self.title_font)
        style.configure("Head.TLabel", background=c["door"], foreground=c["ink"],
                        font=self.head_font)
        style.configure("Bad.TLabel", background=c["door"], foreground=c["seal"])
        style.configure("Good.TLabel", background=c["door"], foreground=c["ok"])
        style.configure("TButton", background=c["door"], foreground=c["ink"],
                        bordercolor=c["line"], padding=(10, 4))
        style.map("TButton", background=[("active", c["paper"])])
        style.configure("Primary.TButton", background=c["btn"], foreground=c["on_btn"])
        style.map("Primary.TButton", background=[("active", c["btn"])],
                  foreground=[("active", c["on_btn"])])
        # Link: a button drawn as underlined text, for "How to set it up".
        style.configure("Link.TButton", background=c["paper"], foreground=c["btn"],
                        bordercolor=c["paper"], lightcolor=c["paper"], darkcolor=c["paper"],
                        borderwidth=0, relief="flat", padding=(0, 0), font=self.link_font)
        style.map("Link.TButton", background=[("active", c["paper"]), ("pressed", c["paper"])],
                  foreground=[("active", c["ink"])],
                  lightcolor=[("pressed", c["paper"])], darkcolor=[("pressed", c["paper"])])
        self._style_checkbuttons(style)
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
        intro = ("Oshiire needs your Reddit username and login cookie, and the folder "
                 "your archive lives in." if self.mode == "first" else
                 "Change Oshiire's setup. Changes take effect the next time you start Oshiire.")
        ttk.Label(outer, text=intro, style="Note.TLabel", wraplength=520).grid(
            row=row, sticky="w", pady=(2, 12))
        row += 1
        if coming_from_feed():
            ttk.Label(outer, text=FEED_NOTICE, style="Note.TLabel", wraplength=520).grid(
                row=row, sticky="w", pady=(0, 12))
            row += 1

        # -- username --
        self.user_var = tk.StringVar(value=self._prefill(USER_KEY))
        card = self._card(outer, row, "Reddit username")
        row += 1
        if self._locked(USER_KEY):
            self._locked_row(card, 1, USER_KEY)
        else:
            ttk.Entry(card, textvariable=self.user_var, width=30).grid(
                row=1, column=0, sticky="w", pady=(4, 0))
        self.user_msg = ttk.Label(card, text="", style="Hint.TLabel", wraplength=500)
        self.user_msg.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.user_msg.grid_remove()

        # -- login cookie --
        self.cookie_var = tk.StringVar(value=self._prefill(COOKIE_KEY))
        self.show_var = tk.BooleanVar(value=False)
        card = self._card(outer, row, "Reddit login cookie")
        row += 1
        guide = ("Oshiire reads your saved posts while logged in as you. To copy the cookie:\n"
                 "1. Open old.reddit.com in your browser, logged in.\n"
                 "2. Press F12 (DevTools), open the Network tab and reload the page.\n"
                 "3. Click the first request (type \"document\"), find Request Headers, and\n"
                 "    copy the whole Cookie value.\n"
                 "4. Paste it here. It expires; when Oshiire says so, copy a fresh one.")
        ttk.Label(card, text=guide, style="Hint.TLabel", justify="left").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(2, 6))
        if self._locked(COOKIE_KEY):
            self._locked_row(card, 2, COOKIE_KEY)
            self.cookie_entry = None
        else:
            self.cookie_entry = ttk.Entry(card, textvariable=self.cookie_var, show="\u2022", width=56)
            self.cookie_entry.grid(row=2, column=0, sticky="ew")
            ttk.Checkbutton(card, text="Show", variable=self.show_var,
                            command=self._toggle_show).grid(row=2, column=1, padx=(8, 0))
            self.cookie_var.trace_add("write", lambda *_: self._say(self.cookie_msg, ""))
        self.test_btn = ttk.Button(card, text="Test it", command=self.start_test)
        self.test_btn.grid(row=2, column=2, padx=(8, 0))
        self.cookie_link = ttk.Button(card, text=COOKIE_LINK, style="Link.TButton",
                                      cursor="hand2", command=self._open_cookie_docs)
        self.cookie_link.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.cookie_msg = ttk.Label(card, text="", style="Hint.TLabel", wraplength=500)
        self.cookie_msg.grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.cookie_msg.grid_remove()  # shown by _say only while it has text

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
        self.archive_msg.grid_remove()

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

    def _setup_fonts(self):
        """Title/heading/link fonts, from a family Tk really has (see
        choose_font_family). On Linux the chosen family also replaces Tk's named
        fonts, so entries, buttons and labels never fall back to a bitmap font;
        on Windows those are left exactly as they were."""
        from tkinter import font as tkfont
        root = self.root
        family = choose_font_family(tkfont.families(root))
        if family and sys.platform != "win32":
            for name in TK_NAMED_FONTS:
                try:
                    tkfont.nametofont(name, root=root).configure(family=family)
                except self.tk.TclError:
                    pass
        default = tkfont.nametofont("TkDefaultFont", root=root)
        base = family or default.actual("family")
        self.title_font = tkfont.Font(root=root, family=base, size=TITLE_SIZE, weight="bold")
        self.head_font = tkfont.Font(root=root, family=base, size=HEAD_SIZE, weight="bold")
        self.link_font = default.copy()
        self.link_font.configure(underline=True)

    def _style_checkbuttons(self, style):
        """A check mark for "on" (clam's own indicator draws an X): two images,
        sized from the default font, as the indicator element of TCheckbutton
        (and so of Page.TCheckbutton too)."""
        from tkinter import font as tkfont
        c = self.colors
        linespace = tkfont.nametofont("TkDefaultFont", root=self.root).metrics("linespace")
        off, on = check_images(self.tk, self.root, max(13, int(linespace * 0.85)),
                               box=c["door"], edge=c["line"], accent=c["btn"], mark=c["on_btn"])
        self._check_images = (off, on)  # Tk keeps no reference of its own
        element = f"oshiire_check{next(_ELEMENT_IDS)}.indicator"
        style.element_create(element, "image", off, ("selected", on), sticky="w")
        style.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
            (element, {"side": "left", "sticky": ""}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Checkbutton.label", {"sticky": "nswe"})]})]})])

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
        if self.cookie_entry is not None:
            self.cookie_entry.configure(show="" if self.show_var.get() else "\u2022")

    def _open_cookie_docs(self):
        webbrowser.open(COOKIE_DOCS_URL)

    def _browse(self):
        from tkinter import filedialog
        chosen = filedialog.askdirectory(parent=self.root, mustexist=False,
                                         title="Choose your archive folder")
        if chosen:
            self.archive_var.set(str(Path(chosen)))
            self._say(self.archive_msg, "")

    def _say(self, label, text, good=None):
        style = "Hint.TLabel" if good is None else ("Good.TLabel" if good else "Bad.TLabel")
        label.configure(text=text, style=style)
        if text:
            label.grid()
        else:
            label.grid_remove()  # an empty line would leave a gap under the card

    # -- worker threads (they only ever put plain tuples on the queue) ----------
    def _current(self, key, var):
        """The value to test with: a key set elsewhere (env/.env) is read from the
        merged config, anything else from the field."""
        return config.get(key) if self._locked(key) else var.get()

    def start_test(self):
        username = self._current(USER_KEY, self.user_var)
        ok, _, message = clean_username(username)
        if not ok:
            self._say(self.user_msg, message, good=False)
            return
        self._say(self.user_msg, "")
        cookie = self._current(COOKIE_KEY, self.cookie_var)
        ok, _, message = clean_cookie(cookie)
        if not ok:
            self._say(self.cookie_msg, message, good=False)
            return
        self.test_btn.state(["disabled"])
        self._say(self.cookie_msg, "Testing...")
        events = self._events

        def work():
            try:
                result = test_login(username, cookie)
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
                    self._say(self.cookie_msg, message, good=ok)
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
            valid, name, message = clean_username(self.user_var.get())
            if valid:
                values[USER_KEY] = name
            else:
                self._say(self.user_msg, message, good=False)
                ok = False
        if not self._locked(COOKIE_KEY):
            valid, cookie, message = clean_cookie(self.cookie_var.get())
            if valid:
                values[COOKIE_KEY] = cookie
            else:
                self._say(self.cookie_msg, message, good=False)
                ok = False
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
    argv = [str(executable or sys.executable), str(datahome.CODE_DIR / "setup_window.py"),
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
