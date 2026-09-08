#!/usr/bin/env bash
cd "$(dirname "$(readlink -f "$0")")/.."
PY=".venv/bin/python"
[ -x "$PY" ] || { echo "No venv at $PY"; exit 1; }

if ! "$PY" ai_tag.py; then
  echo; echo "AI tagging FAILED - see error above."; exit 1
fi
echo; echo "AI tagging complete."
