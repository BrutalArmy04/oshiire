#!/usr/bin/env bash
# Launch Oshiire as a native window where a GUI backend exists, else the browser.
# A release ships its own Python in runtime/; a git checkout uses .venv. app.py is
# passed by ABSOLUTE path so "Stop it and start fresh" can verify the instance.
DIR="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || printf '%s' "$0")")" && pwd)" || exit 1
cd "$DIR" || exit 1
if [ -x "$DIR/runtime/bin/python3" ]; then
    PY="$DIR/runtime/bin/python3"
    # The bundled Python must never pick up packages from a per-user
    # site-packages (~/.local/lib/python3.14); every job child inherits this.
    export PYTHONNOUSERSITE=1
else
    PY="$DIR/.venv/bin/python"
fi
if [ ! -x "$PY" ]; then
    echo "Oshiire isn't set up yet... Follow docs/SETUP.md"
    exit 1
fi
exec "$PY" "$DIR/app.py"
