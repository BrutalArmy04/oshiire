#!/usr/bin/env bash
cd "$(dirname "$(readlink -f "$0")")/.."
PY=".venv/bin/python"
[ -x "$PY" ] || { echo "No venv at $PY"; exit 1; }

# Read-only history browser. Looks things up; changes nothing.
if ! "$PY" history.py; then
  echo; echo "History viewer exited with an error - see above."; exit 1
fi
