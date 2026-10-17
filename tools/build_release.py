"""Build a downloadable Oshiire: app files + a standalone Python + every package.

    python tools/build_release.py --platform windows|linux \\
        --python <extracted python-build-standalone install_only "python" dir> \\
        --wheelhouse <dir with prebuilt wheels for SOURCE_ONLY> --out <dir>

    python tools/build_release.py --source-only-reqs
        prints the SOURCE_ONLY pins from requirements-lock.txt, one per line
        (the release workflow builds those wheels first)

Stdlib only. Steps:
  1. App files: the repo's files (tracked, plus untracked-but-not-ignored, so a
     local build of uncommitted work matches what CI will build once it is
     committed), filtered through an explicit ALLOWLIST: the Python modules the
     app's entry points import (walked with ast, never listed by hand), plus
     STATIC_FILES and the platform launcher.
  2. The standalone Python is copied to Oshiire/runtime and requirements-lock.txt
     is installed into it with --no-deps --only-binary=:all:; oshiire_release.json
     (the release marker datahome.py looks for) is written next to app.py.
  3. Everything is precompiled (checked-hash .pyc, so the 2-second timestamps of
     a zip can never make them look stale). pip stays in the runtime.
  4. Checks that fail the build: a personal file anywhere in the app tree, a
     Windows path that is too long, a marker version that differs from
     version.py.
  5. Oshiire-Windows.zip or Oshiire-Linux.tar.gz, top folder "Oshiire/".
"""
import argparse
import ast
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parent.parent
TOP = "Oshiire"
LOCK_FILE = "requirements-lock.txt"

# No Python 3.14 wheel on PyPI: built from source by the workflow into the
# wheelhouse. bchlib is a C extension (wheels stop at cp312); proxy_tools and
# sgmllib3k are pure Python with sdists only.
SOURCE_ONLY = ("bchlib", "proxy_tools", "sgmllib3k")

# Where the import walk starts: the desktop entry point, the setup window (also
# run as its own process), and every script jobs.py runs as a pipeline step
# (found by reading jobs.py's _script("...") calls).
ENTRY_MODULES = ("app", "setup_window")

# Non-module files a release needs. Globs are matched against repo-relative
# POSIX paths.
STATIC_FILES = (
    "assets/**",
    "data/tombstones.json",
    "*.example*",
    "**/*.example*",
    ".env.example",
    "oshiire_unsave.user.js",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "docs/SETUP.md",
    "docs/BACKFILL.md",
)
LAUNCHERS = {"windows": "Oshiire.bat", "linux": "oshiire.sh"}
# Fixed: the README links /releases/latest/download/<name>.
ARCHIVE_NAMES = {"windows": "Oshiire-Windows.zip", "linux": "Oshiire-Linux.tar.gz"}

# Never shipped even if an allowlist pattern would match (belt and braces).
EXCLUDED_PREFIXES = ("tests/", "scripts/", "screenshots/", "tools/", ".github/")
EXCLUDED_FILES = ("CLAUDE.md", "blueprint.md", "install.bat", "install.sh")
EXCLUDED_GLOBS = ("requirements*.txt",)

# Personal data -- the build fails if any of these is in the app tree.
# Patterns are relative to Oshiire/; "**/" means "at any depth". runtime/ is the
# standalone Python plus PyPI wheels (never repo content), and some packages
# legitimately ship e.g. a web-app manifest.json, so only the names that can
# never be a package file are checked inside it (DENY_EVERYWHERE).
DENY_EVERYWHERE = (
    "**/.env", ".env",
    "**/000___*", "000___*",
    "**/known_series_names.txt", "known_series_names.txt",
    "**/config.env",
)
DENY_APP_TREE = (
    "manifest.json", "layout.json", "subreddit_map.json",
    "staging/**",
    "data/settings.json", "data/config.env",
    "data/archive_index.db*", "data/oshiire.*",
    "data/thumbs/**",
    "data/backfill/**",  # *.example.* excepted below
    "data/saved_posts.csv", "data/unsave_list.json",
)
DENY_EXCEPT = ("data/backfill/*.example.*",)

MAX_WINDOWS_PATH = 150  # relative to Oshiire/, so an extract path has room
# onnxruntime's model-conversion tools are never imported by Oshiire and hold
# the deepest file names in the tree; precompiling them would add a
# __pycache__ level that breaks the Windows path budget.
NO_PRECOMPILE = r"[/\\]onnxruntime[/\\]tools[/\\]"


def log(msg):
    print(msg, flush=True)


# Every call to the runtime's interpreter runs with the user site-packages OFF
# (-s and PYTHONNOUSERSITE): a python-build-standalone interpreter honours a
# per-user site-packages for its version (%APPDATA%\Python\Python314 on
# Windows), and pip would treat packages found there as "already installed" --
# skipping them in the runtime and UNINSTALLING them from the user's own
# environment when versions differ.
RUNTIME_ENV = dict(os.environ, PYTHONNOUSERSITE="1")


def run(argv, **kwargs):
    log("$ " + " ".join(str(a) for a in argv))
    kwargs.setdefault("env", RUNTIME_ENV)
    return subprocess.run([str(a) for a in argv], check=True, **kwargs)


def run_py(py, *args, **kwargs):
    """The runtime interpreter, user site off."""
    return run([py, "-s", *args], **kwargs)


# --------------------------------------------------------------------------- #
# 1. App files
# --------------------------------------------------------------------------- #
def repo_files():
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO, check=True, capture_output=True).stdout.decode("utf-8")
    return sorted(p for p in out.split("\0") if p and (REPO / p).is_file())


def _local_imports(path, local):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found & local


def _job_scripts():
    tree = ast.parse((REPO / "jobs.py").read_text(encoding="utf-8"))
    return {node.args[0].value[:-3] for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_script"
            and node.args and isinstance(node.args[0], ast.Constant)
            and str(node.args[0].value).endswith(".py")}


def import_closure(files):
    """Root-level modules reachable from ENTRY_MODULES + the job scripts."""
    local = {f[:-3] for f in files if "/" not in f and f.endswith(".py")}
    todo = list(ENTRY_MODULES) + sorted(_job_scripts())
    seen = set()
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        if mod not in local:
            raise SystemExit(f"BUILD FAILED: {mod}.py is imported but not in the repo")
        seen.add(mod)
        todo.extend(_local_imports(REPO / f"{mod}.py", local))
    return sorted(seen)


def _match(path, patterns):
    return any(fnmatch.fnmatchcase(path, p) or PurePosixPath(path).match(p) for p in patterns)


def select_app_files(platform, files):
    modules = import_closure(files)
    chosen = {f"{m}.py" for m in modules}
    for f in files:
        if _match(f, STATIC_FILES):
            chosen.add(f)
    chosen.add(LAUNCHERS[platform])
    chosen = {f for f in chosen
              if not f.startswith(EXCLUDED_PREFIXES) and f not in EXCLUDED_FILES
              and not _match(f, EXCLUDED_GLOBS)}
    missing = sorted(f for f in chosen if f not in files)
    if missing:
        raise SystemExit(f"BUILD FAILED: allowlisted but not in the repo: {missing}")
    left_out = sorted(f for f in files if f.endswith(".py") and "/" not in f
                      and f not in chosen)
    return sorted(chosen), modules, left_out


def copy_app(platform, app_dir):
    files = repo_files()
    chosen, modules, left_out = select_app_files(platform, files)
    for rel in chosen:
        dest = app_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, dest)
    launcher = app_dir / LAUNCHERS[platform]
    if platform == "linux":
        launcher.chmod(0o755)
    log(f"app files: {len(chosen)} ({len(modules)} modules)")
    log("modules: " + " ".join(modules))
    log("root .py left out (not imported by the app): " + (" ".join(left_out) or "none"))
    return chosen


# --------------------------------------------------------------------------- #
# 2. Runtime + packages + marker
# --------------------------------------------------------------------------- #
def runtime_python(runtime, platform):
    exe = runtime / ("python.exe" if platform == "windows" else "bin/python3")
    if not exe.exists():
        raise SystemExit(f"BUILD FAILED: no interpreter at {exe}")
    return exe


def lock_pins():
    pins = {}
    for line in (REPO / LOCK_FILE).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name = re.split(r"[=;<> ]", line, maxsplit=1)[0]
            pins[re.sub(r"[-_.]+", "-", name).lower()] = line.split(";")[0].strip()
    return pins


def source_only_reqs():
    pins = lock_pins()
    return [pins[re.sub(r"[-_.]+", "-", n).lower()] for n in SOURCE_ONLY]


def install_packages(py, wheelhouse):
    probe = subprocess.run([str(py), "-s", "-m", "pip", "--version"], capture_output=True,
                           env=RUNTIME_ENV)
    if probe.returncode != 0:
        run_py(py, "-m", "ensurepip", "--upgrade")
    # --no-compile: step 3 compiles everything once, with the path exclusions.
    run_py(py, "-m", "pip", "install", "--disable-pip-version-check",
           "--no-warn-script-location", "--no-compile",
           "--no-deps", "--only-binary=:all:", "--find-links", wheelhouse,
           "-r", REPO / LOCK_FILE)


def isolate_runtime(runtime, platform, py):
    """Windows: a <pythonXY>._pth next to python.exe pins sys.path to the runtime
    and the app folder ("..") and, with no "import site" line, keeps every
    per-user site-packages out -- for every process of this python.exe (the
    launcher, a Start-menu shortcut, job children, the setup window). The only
    .pth file in site-packages is setuptools' distutils shim, which nothing
    needs at runtime. Linux has no ._pth; oshiire.sh exports PYTHONNOUSERSITE."""
    if platform != "windows":
        return None
    version = subprocess.run([str(py), "-s", "-c",
                              "import sys; print(f'{sys.version_info[0]}{sys.version_info[1]}')"],
                             check=True, capture_output=True, text=True,
                             env=RUNTIME_ENV).stdout.strip()
    pth = runtime / f"python{version}._pth"
    lines = [f"python{version}.zip", "DLLs", "Lib", "Lib\\site-packages", ".."]
    pth.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
    log(f"wrote {pth.name}: " + ", ".join(lines))
    return pth


def check_no_user_site(py, runtime, top):
    """Fail if the runtime would import from anywhere but itself and the app."""
    code = "import sys, json; print(json.dumps({'paths': sys.path}))"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONNOUSERSITE"}
    # Launched the way a user launches it: no -s, no PYTHONNOUSERSITE (Linux
    # relies on oshiire.sh for that, so it is set there).
    if (top / "oshiire.sh").exists():
        env["PYTHONNOUSERSITE"] = "1"
    out = subprocess.run([str(py), "-c", code], check=True, capture_output=True, text=True,
                         env=env, cwd=str(top)).stdout
    paths = json.loads(out)["paths"]
    outside = [p for p in paths if p and not Path(p).resolve().is_relative_to(top.resolve())]
    return outside


def app_version():
    tree = ast.parse((REPO / "version.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "__version__"
                                                for t in node.targets):
            return node.value.value
    raise SystemExit("BUILD FAILED: no __version__ in version.py")


def write_marker(app_dir, platform, py):
    python_version = subprocess.run(
        [str(py), "-s", "-c", "import platform; print(platform.python_version())"],
        check=True, capture_output=True, text=True, env=RUNTIME_ENV).stdout.strip()
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, check=True,
                            capture_output=True, text=True).stdout.strip()
    marker = {"version": app_version(), "platform": platform,
              "python": python_version, "commit": commit}
    (app_dir / "oshiire_release.json").write_text(json.dumps(marker, indent=2) + "\n",
                                                  encoding="utf-8")
    log(f"release marker: {marker}")
    return marker


# --------------------------------------------------------------------------- #
# 3. Precompile
# --------------------------------------------------------------------------- #
def precompile(py, top):
    # -f: also rebuild the runtime's own timestamp-based stdlib .pyc files.
    done = subprocess.run([str(py), "-s", "-m", "compileall", "-q", "-f", "-j0",
                           "--invalidation-mode", "checked-hash",
                           "-x", NO_PRECOMPILE, str(top)], env=RUNTIME_ENV)
    if done.returncode != 0:
        # Some wheels ship deliberately invalid test files; a .py that doesn't
        # compile here would not import either, so this is reported, not fatal.
        log("note: compileall reported files it could not compile (see above)")


# --------------------------------------------------------------------------- #
# 4. Checks
# --------------------------------------------------------------------------- #
def all_paths(top):
    for path in sorted(top.rglob("*")):
        yield path, path.relative_to(top).as_posix()


def scan_tree(top, platform):
    """Problems found by path alone: personal files, Windows path lengths."""
    problems = []
    for path, rel in all_paths(top):
        in_runtime = rel == "runtime" or rel.startswith("runtime/")
        patterns = DENY_EVERYWHERE if in_runtime else DENY_EVERYWHERE + DENY_APP_TREE
        if _match(rel, patterns) and not _match(rel, DENY_EXCEPT):
            problems.append(f"personal file in the release: {rel}")
        if platform == "windows" and len(rel) > MAX_WINDOWS_PATH:
            problems.append(f"path longer than {MAX_WINDOWS_PATH} chars ({len(rel)}): {rel}")
    return problems


def check_tree(top, platform, marker, py):
    problems = []
    for outside in check_no_user_site(py, top / "runtime", top):
        problems.append(f"the runtime imports from outside the release: {outside}")
    problems += scan_tree(top, platform)
    if marker["version"] != app_version():
        problems.append(f"marker version {marker['version']} != version.py {app_version()}")
    if problems:
        for p in problems:
            log("CHECK FAILED: " + p)
        raise SystemExit("BUILD FAILED: release checks")
    log("checks passed: no personal files, path lengths ok, marker version ok")


# --------------------------------------------------------------------------- #
# 5. Archive
# --------------------------------------------------------------------------- #
def archive(out, top, platform):
    dest = out / ARCHIVE_NAMES[platform]
    if platform == "windows":
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path, rel in all_paths(top):
                arc = f"{TOP}/{rel}"
                if path.is_dir():
                    if not any(path.iterdir()):
                        zf.write(path, arc + "/")
                    continue
                zf.write(path, arc)
    else:
        with tarfile.open(dest, "w:gz", compresslevel=6) as tf:
            # dereference=False (the default) stores symlinks as symlinks, and
            # each member keeps its file mode, so exec bits survive.
            tf.add(top, arcname=TOP, recursive=True)
    return dest


def tree_stats(top):
    files = [p for p in top.rglob("*") if p.is_file() and not p.is_symlink()]
    longest = max((p.relative_to(top).as_posix() for p in top.rglob("*")), key=len)
    return len(files), sum(p.stat().st_size for p in files), longest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-only-reqs", action="store_true",
                        help="print the SOURCE_ONLY pins and exit")
    parser.add_argument("--platform", choices=sorted(LAUNCHERS))
    parser.add_argument("--python", type=Path, help="extracted install_only 'python' dir")
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.source_only_reqs:
        print("\n".join(source_only_reqs()))
        return 0
    if not (args.platform and args.python and args.wheelhouse and args.out):
        parser.error("--platform, --python, --wheelhouse and --out are required")

    started = time.monotonic()
    out = args.out.resolve()
    top = out / TOP
    if top.exists():
        raise SystemExit(f"BUILD FAILED: {top} already exists -- use an empty --out")
    out.mkdir(parents=True, exist_ok=True)

    log("== 1. app files")
    copy_app(args.platform, top)
    log("== 2. runtime + packages")
    runtime = top / "runtime"
    shutil.copytree(args.python, runtime, symlinks=True)
    py = runtime_python(runtime, args.platform)
    install_packages(py, args.wheelhouse.resolve())
    isolate_runtime(runtime, args.platform, py)
    marker = write_marker(top, args.platform, py)
    log("== 3. precompile")
    precompile(py, top)
    log("== 4. checks")
    check_tree(top, args.platform, marker, py)
    log("== 5. archive")
    count, size, longest = tree_stats(top)
    dest = archive(out, top, args.platform)
    log(f"unpacked: {size / 2**20:,.1f} MiB in {count:,} files")
    log(f"longest path ({len(longest)} chars): {longest}")
    log(f"archive: {dest.name} {dest.stat().st_size / 2**20:,.1f} MiB")
    log(f"built in {time.monotonic() - started:,.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
