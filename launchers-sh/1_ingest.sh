#!/usr/bin/env bash
cd "$(dirname "$(readlink -f "$0")")/.."
PY=".venv/bin/python"
[ -x "$PY" ] || { echo "No venv at $PY -- create it: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"; exit 1; }

if ! "$PY" ingest.py; then
  echo; echo "Ingest FAILED - see error above."; exit 1
fi
echo; echo "Ingest complete."
