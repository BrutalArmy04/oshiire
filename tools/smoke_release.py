"""Smoke-test a BUILT release with its own runtime:

    <Oshiire/runtime python> tools/smoke_release.py --app <dir holding app.py>

Uses a throwaway OSHIIRE_HOME and refuses to run with a .env anywhere in reach.
Does what app.py does up to the UI (chdir to the data root, config.load,
ensure_starters), checks the heavy native pieces import, then starts the real
server on 127.0.0.1 and checks what it serves and what it refuses. One line per
check; exits non-zero if any check fails.
"""
import argparse
import json
import os
import platform
import re
import sys
import tempfile
import traceback
import urllib.error
import urllib.request
from pathlib import Path

RESULTS = []


def check(name, fn):
    try:
        detail = fn()
    except SkipCheck as skip:
        print(f"SKIP  {name}: {skip}", flush=True)
        return
    except Exception as exc:
        RESULTS.append(False)
        print(f"FAIL  {name}: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()
        return
    RESULTS.append(True)
    print(f"PASS  {name}" + (f" ({detail})" if detail else ""), flush=True)


class SkipCheck(Exception):
    pass


def info(name, text):
    """A fact worth printing that is neither a pass nor a failure."""
    print(f"INFO  {name}: {text}", flush=True)


def _importable(module):
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _dist_version(name):
    from importlib import metadata
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "?"


def _dotenv_in_reach(start):
    for folder in [start, *start.parents]:
        if (folder / ".env").exists():
            return folder / ".env"
    return None


def _status(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, required=True)
    args = parser.parse_args()
    app = args.app.resolve()
    # pywebview resolves sys.argv[0] against the cwd while it imports (a
    # default argument in webview/window.py); after the chdir to the data root
    # below, a relative "tools/smoke_release.py" no longer exists. The
    # launchers pass app.py by absolute path, so match them.
    sys.argv[0] = os.path.abspath(sys.argv[0])
    home = Path(tempfile.mkdtemp(prefix="oshiire-smoke-")).resolve()

    for key in ("REDDIT_SAVED_FEED_URL", "ARCHIVE_DIR", "REDDIT_USERNAME",
                "REDDIT_SESSION_COOKIE", "PYTHON_DOTENV_DISABLED"):
        os.environ.pop(key, None)
    os.environ["OSHIIRE_HOME"] = str(home)
    sys.path.insert(0, str(app))

    def no_dotenv():
        for start in (app, home):
            found = _dotenv_in_reach(start)
            if found:
                raise AssertionError(f"a .env is in reach: {found}")
        return f"app {app}, home {home}"
    check("no .env in reach", no_dotenv)
    if not all(RESULTS):
        return 1

    state = {}

    def release_marker():
        import datahome
        import version
        assert datahome.APP_DIR == app, (datahome.APP_DIR, app)
        assert datahome.is_release(), "oshiire_release.json missing"
        marker = json.loads(datahome.RELEASE_MARKER.read_text(encoding="utf-8"))
        assert marker["version"] == version.__version__, (marker, version.__version__)
        assert marker["python"] == ".".join(map(str, sys.version_info[:3])), marker
        return f"{marker['version']}, python {marker['python']}, {marker['platform']}"
    check("release marker", release_marker)

    def startup_like_app():
        import config
        import datahome
        import starters
        root = datahome.data_root()
        assert root == home, (root, home)
        root.mkdir(parents=True, exist_ok=True)
        os.chdir(root)
        config.load()
        written = starters.ensure_starters()
        assert {str(p).replace("\\", "/") for p in written} >= {
            "manifest.json", "layout.json", "subreddit_map.json"}, written
        state["config"] = config
        return f"{len(written)} starter files"
    check("data root + config + starters", startup_like_app)

    def numpy_two():
        import numpy
        major = int(numpy.__version__.split(".")[0])
        assert major == 2, numpy.__version__
        return numpy.__version__
    check("numpy is 2.x", numpy_two)

    def onnx():
        import onnxruntime
        return onnxruntime.__version__
    check("onnxruntime imports", onnx)

    def from_runtime():
        # Nothing may come from a per-user site-packages or any other Python.
        import gradio
        import numpy
        import onnxruntime
        runtime = (app / "runtime").resolve()
        outside = [m.__name__ for m in (gradio, numpy, onnxruntime)
                   if not Path(m.__file__).resolve().is_relative_to(runtime)]
        assert not outside, f"loaded from outside {runtime}: {outside}"
        stray = [p for p in sys.path if p and "site-packages" in p
                 and not Path(p).resolve().is_relative_to(runtime)]
        assert not stray, f"site-packages outside the runtime on sys.path: {stray}"
        return "gradio, numpy, onnxruntime from runtime/"
    check("packages load from the bundled runtime", from_runtime)

    def tagger():
        import ai_tagger
        return f"model {ai_tagger.model_files()[0][1]}"
    check("ai_tagger (imgutils) imports", tagger)

    def tcl():
        import tkinter
        interp = tkinter.Tcl()
        return f"Tcl {interp.eval('info patchlevel')}"
    check("tkinter + Tcl()", tcl)

    def tk_window():
        has_display = sys.platform in ("win32", "darwin") or os.environ.get("DISPLAY") \
            or os.environ.get("WAYLAND_DISPLAY")
        if not has_display:
            raise SkipCheck("no display")
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        root.update_idletasks()
        root.destroy()
        return "Tk() opened and closed"
    check("Tk()", tk_window)

    def native_window():
        # Everything app.py needs for window mode, short of opening a window:
        # pywebview's GUI is only touched by setup_app()/create_window(), which
        # nothing here calls.
        import webview
        version = getattr(webview, "__version__", None) or _dist_version("pywebview")
        if sys.platform != "win32":
            backends = [m for m in ("gi", "qtpy") if _importable(m)]
            if not backends:
                info("native window", "no GUI backend (browser mode)")
            return f"pywebview {version} imports"
        import clr  # noqa: F401  pythonnet, via clr_loader: loads .NET in-process
        import pythonnet
        import System
        runtime = f"{pythonnet.get_runtime_info().kind} {System.Environment.Version}"
        from webview.util import interop_dll_path
        arch = {"AMD64": "win-x64", "ARM64": "win-arm64", "x86": "win-x86"}.get(
            platform.machine(), "win-x64")
        loader = Path(interop_dll_path(arch)) / "WebView2Loader.dll"
        assert loader.is_file(), f"missing {loader}"
        for dll in ("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll"):
            assert Path(interop_dll_path(dll)).is_file(), f"missing {dll}"
        # The winforms module picks its renderer at import: with no WebView2
        # runtime registered it would take the MSHTML path, which writes an IE
        # emulation key to the registry. WEBVIEW2_RUNTIME_PATH (pywebview's own
        # setting, this process only) keeps it on the Edge path, so the import
        # loads WinForms and both WebView2 assemblies and has no side effects.
        saved = webview.settings["WEBVIEW2_RUNTIME_PATH"]
        webview.settings["WEBVIEW2_RUNTIME_PATH"] = str(loader.parent)
        try:
            from webview.platforms import winforms
        finally:
            webview.settings["WEBVIEW2_RUNTIME_PATH"] = saved
        assert winforms.renderer == "edgechromium", winforms.renderer
        installed = winforms._is_chromium()
        info("WebView2 runtime", "installed" if installed else
             "not installed (window mode falls back to the browser)")
        return (f"pywebview {version}, pythonnet on {runtime}, "
                f"{loader.relative_to(Path(webview.__file__).parent)}")
    check("native window readiness (no window opened)", native_window)

    def serve():
        config = state["config"]
        config.save_setup({"REDDIT_USERNAME": "smoke-test"})  # creates data/config.env
        (home / ".env").write_text("SMOKE_DUMMY=1\n", encoding="utf-8")
        import review
        review.launch_app(inbrowser=False, block=False)
        state["review"] = review
        base = f"http://127.0.0.1:{review.demo.server_port}"
        font = re.findall(r"url\('([^']+\.woff2)'\)", review.CUSTOM_CSS)[0]
        codes = {
            "/": _status(base, "/"),
            "font": _status(base, font),
            "config.env": _status(base, review._gradio_file_url(home / "data" / "config.env")),
            ".env": _status(base, review._gradio_file_url(home / ".env")),
        }
        expected = {"/": 200, "font": 200, "config.env": 403, ".env": 403}
        assert codes == expected, codes
        return ", ".join(f"{k} {v}" for k, v in codes.items())
    check("server: / and font 200, config.env and .env 403", serve)

    if "review" in state:
        try:
            state["review"].demo.close()
            print("server closed", flush=True)
        except Exception as exc:
            print(f"note: closing the server failed: {exc}", flush=True)

    passed = sum(RESULTS)
    print(f"{passed}/{len(RESULTS)} checks passed", flush=True)
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)  # gradio/uvicorn threads must not keep the process alive
