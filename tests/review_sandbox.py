"""Test helper: import review (and the pages it builds) against a throwaway data root.

review.py, resolve.py and history.py bind their manifest, layout, queue and
archive dir at IMPORT time, from the cwd and the environment, and resolve.py
exits the process when there is no layout.json. So a test module that imports
review in the repo folder either dies in a fresh checkout or silently reads the
user's real files. ReviewSandbox gives each test module its OWN copy of those
three modules, imported inside its own sandbox:

  - cwd = the sandbox data root, seeded by build_sandbox() from the committed
    *.example templates (or any tree the caller built),
  - the four config.CONFIG_KEYS removed from os.environ and
    PYTHON_DOTENV_DISABLED=1, so the no-argument load_dotenv() calls in
    review/resolve/history never reach the app-folder .env,
  - during the import, config is pointed at the sandbox and an EMPTY app folder
    (config._state), so the real .env is unreachable through config too.
    datahome.APP_DIR itself is left alone: review anchors code assets to it
    (assets/fonts, setup_window.py), and those must stay the real ones.

Import order. `unittest discover` imports every test module before it runs any,
and a module import is cached in sys.modules, so whichever test imported review
first used to decide what every later `import review` got. Here no copy is
shared: each sandbox pops review/resolve/history from sys.modules, imports
fresh ones, and gives the slots back. While a sandbox is ACTIVE (a test module
or class is running) its copies sit in sys.modules, so a function-level
`import review` or a mock.patch("review....") string target reaches the copy
the running tests hold; deactivating restores whatever was there before. A
module-level copy and a class-level copy never see each other's state, whatever
order discover loads or runs them in.

Not a test module itself (the name doesn't match test*.py).
"""
import contextlib
import importlib
import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

import config  # noqa: E402

# The modules that bind data-root state at import. review imports the other two.
STATEFUL_MODULES = ("review", "resolve", "history")


def build_sandbox(tmp, manifest=None):
    """A minimal but REAL project tree: review.py reads all of this at import."""
    tmp = Path(tmp)
    shutil.copy(REPO / "examples" / "layout.example.json", tmp / "layout.json")
    shutil.copy(REPO / "examples" / "known_series_names.example.txt", tmp / "known_series_names.txt")
    # tagger.subreddit_is_mapped exits the process if this is absent, and
    # on_accept consults it.
    shutil.copy(REPO / "examples" / "subreddit_map.example.json", tmp / "subreddit_map.json")
    (tmp / "staging").mkdir(exist_ok=True)
    (tmp / "manifest.json").write_text(json.dumps(manifest or {}, indent=2), encoding="utf-8")


def scrubbed_environ(base=None):
    """A copy of `base` (default os.environ) without the four config keys and with
    python-dotenv disabled -- for a child process, or to install in-process."""
    env = dict(os.environ if base is None else base)
    for key in config.CONFIG_KEYS:
        env.pop(key, None)
    env[config.DISABLE_FLAG] = "1"
    return env


def child_environ():
    """The environment for a subprocess that imports review/resolve/history:
    scrubbed, with the repo on PYTHONPATH (its cwd is a sandbox, not the repo)."""
    env = scrubbed_environ()
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(REPO), env.get("PYTHONPATH")) if p)
    return env


def new_root(prefix="oshiire-review-"):
    """A fresh temp data root seeded by build_sandbox()."""
    root = Path(tempfile.mkdtemp(prefix=prefix))
    build_sandbox(root)
    return root


@contextlib.contextmanager
def _environment(root, env):
    """cwd = root and a scrubbed os.environ (plus `env`), both restored on exit."""
    prev_cwd = os.getcwd()
    with mock.patch.dict(os.environ):
        for key in config.CONFIG_KEYS:
            os.environ.pop(key, None)
        os.environ[config.DISABLE_FLAG] = "1"
        os.environ.update(env)
        os.chdir(root)
        try:
            yield
        finally:
            os.chdir(prev_cwd)


class _SandboxConfigState(dict):
    """config._state for an ACTIVE sandbox: exactly what config does with no
    state (root = the live cwd, environment = the live os.environ) except that
    the app folder is an empty one -- so a config.get() made while a test runs
    (review._reddit_username, jobs.run_steps, ...) can never read the real
    app-folder .env, not just the import."""

    def __getitem__(self, key):
        if key == "root":
            return Path.cwd()
        if key == "process_env":
            return os.environ
        return super().__getitem__(key)


@contextlib.contextmanager
def _config_without_app_env():
    app_dir = Path(tempfile.mkdtemp(prefix="oshiire-review-app-"))
    try:
        with mock.patch.object(config, "_state", _SandboxConfigState(app_dir=app_dir)):
            yield
    finally:
        shutil.rmtree(app_dir, ignore_errors=True)


def _swap_modules(modules):
    """Put `modules` in the STATEFUL_MODULES slots (empty slots for the rest) and
    return what was there, for _restore_modules."""
    prev = {name: sys.modules.pop(name, None) for name in STATEFUL_MODULES}
    sys.modules.update(modules)
    return prev


def _restore_modules(prev):
    for name in STATEFUL_MODULES:
        sys.modules.pop(name, None)
    sys.modules.update({name: m for name, m in prev.items() if m is not None})


class ReviewSandbox:
    """One data root and the private review/resolve/history copies bound to it.

    Class-level use:   setUpClass: cls.review = ReviewSandbox(tmp).activate()
                       tearDownClass: cls._sandbox.deactivate()
    Module-level use:  _sandbox = ReviewSandbox(); review = _sandbox.load()
                       setUpModule = _sandbox.activate; tearDownModule = _sandbox.close
    """

    def __init__(self, root=None, env=None, module="review"):
        self.owns_root = root is None
        self.root = Path(root if root is not None else new_root()).absolute()
        self.env = dict(env or {})
        self.module_name = module
        self.modules = None
        self._stack = None

    def _import(self):
        app_dir = Path(tempfile.mkdtemp(prefix="oshiire-review-app-"))
        state = {"root": self.root, "app_dir": app_dir, "process_env": dict(os.environ)}
        try:
            # Noisy (manifest load, hash warm-up) but side-effect-free here.
            with redirect_stdout(StringIO()), mock.patch.object(config, "_state", state):
                importlib.import_module(self.module_name)
        finally:
            shutil.rmtree(app_dir, ignore_errors=True)
        self.modules = {n: sys.modules[n] for n in STATEFUL_MODULES if n in sys.modules}

    def activate(self):
        """Enter the sandbox (cwd, env, module slots), importing on first use.
        Returns the sandbox's copy of the module."""
        if self._stack is not None:
            return self.modules[self.module_name]
        stack = contextlib.ExitStack()
        stack.enter_context(_environment(self.root, self.env))
        stack.enter_context(_config_without_app_env())
        stack.callback(_restore_modules, _swap_modules(self.modules or {}))
        self._stack = stack
        if self.modules is None:
            try:
                self._import()
            except BaseException:
                self.deactivate()
                raise
        return self.modules[self.module_name]

    def deactivate(self):
        if self._stack is not None:
            stack, self._stack = self._stack, None
            stack.close()

    def load(self):
        """Import (once) inside the sandbox and step back out -- for a module-level
        `review = ...` that class bodies need at load time."""
        module = self.activate()
        self.deactivate()
        return module

    def close(self):
        self.deactivate()
        if self.owns_root:
            shutil.rmtree(self.root, ignore_errors=True)
