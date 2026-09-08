#!/usr/bin/env bash
cd "$(dirname "$(readlink -f "$0")")/.."
PY=".venv/bin/python"
[ -x "$PY" ] || { echo "No venv at $PY"; exit 1; }

read -rp "Apply these archive moves now? (y/n): " CONFIRM
if [ "${CONFIRM,,}" != "y" ]; then
  echo "Cancelled - no changes made."; exit 0
fi

if ! "$PY" archive.py --apply; then
  echo; echo "Archive apply FAILED - see error above."; exit 1
fi
echo; echo "Archive apply complete."
