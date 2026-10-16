"""Test helper: run config reads against a throwaway data root.

config.py merges the process environment, <data root>/.env, <app folder>/.env and
<data root>/data/config.env. In a test process the real app folder holds the
user's real .env, and an earlier `import review` may have copied its keys into
os.environ, so a test that reads config must hide all three. isolated_config()
does that for the duration of a `with` block:

  - chdir to `root` (the data root is the cwd),
  - point datahome.APP_DIR at an empty folder (no app-folder .env),
  - drop the four config keys and PYTHON_DOTENV_DISABLED from os.environ
    (restored afterwards, with everything else, by mock.patch.dict),
  - reset config's load() state.

Not a test module itself (the name doesn't match test*.py).
"""
import contextlib
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import config  # noqa: E402
import datahome  # noqa: E402


@contextlib.contextmanager
def isolated_config(root, app_dir=None):
    root = Path(root)
    app_dir = Path(app_dir) if app_dir is not None else Path(tempfile.mkdtemp(prefix="cfg-app-"))
    prev_cwd = os.getcwd()
    with mock.patch.dict(os.environ), \
         mock.patch.object(datahome, "APP_DIR", app_dir), \
         mock.patch.object(config, "_state", None):
        for key in (*config.CONFIG_KEYS, config.DISABLE_FLAG):
            os.environ.pop(key, None)
        os.chdir(root)
        try:
            yield app_dir
        finally:
            os.chdir(prev_cwd)
