#!/usr/bin/env bash
# Oshiire one-shot installer (Linux/macOS). Builds .venv (the path oshiire.sh
# invokes), installs requirements, THEN upgrades numpy past the numpy<2 pin
# dghs-imgutils carries -- on Python 3.13+ that pin has no wheel and pip builds
# an experimental numpy that crashes Oshiire on startup. The numpy upgrade is
# NOT optional; this script does it for you. See docs/SETUP.md.
set -e
cd "$(dirname "$0")" || exit 1

VENV=".venv"
PY="${PYTHON:-python3}"

if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating virtualenv $VENV ..."
    "$PY" -m venv "$VENV"
fi

echo
echo "[1/2] Installing requirements ..."
"$VENV/bin/python" -m pip install -r requirements.txt

echo
echo "[2/2] Upgrading numpy past the numpy<2 pin ..."
"$VENV/bin/python" -m pip install --upgrade numpy

echo
echo "Installed numpy version:"
"$VENV/bin/python" -c "import numpy; print('  numpy', numpy.__version__)"

echo
echo "Done. Run ./oshiire.sh to start."
