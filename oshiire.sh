#!/usr/bin/env bash
# Launch Oshiire as a native window where a GUI backend exists, else the browser.
cd "$(dirname "$0")" || exit 1
PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
    echo "Oshiire isn't set up yet... Follow docs/SETUP.md"
    exit 1
fi
exec "$PY" app.py
