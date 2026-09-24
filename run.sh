#!/usr/bin/env bash
# Sets up (or repairs) a virtual environment and launches the desktop app.
# Safe to run every time; it only does real work the first run, or after
# something about the environment changed underneath it (e.g. a SteamOS
# update swapping the system Python version out from under an existing venv).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

VENV=.venv

# A venv's own interpreter can go stale if the system Python that created it
# no longer exists: its site-packages sit under that old version's
# lib/pythonX.Y/ directory, invisible to whatever python3 the venv actually
# runs now. Rebuilding it is cheap and beats diagnosing that by hand.
if [ -d "$VENV" ] && ! "$VENV/bin/python3" -c "import webview" >/dev/null 2>&1; then
    echo "Existing .venv doesn't have this project's dependencies importable, rebuilding it."
    rm -rf "$VENV"
fi

if command -v uv >/dev/null 2>&1; then
    if [ ! -d "$VENV" ]; then
        echo "Setting up a virtual environment with uv (first run only)..."
        uv venv "$VENV"
    fi
    uv pip install -q -r requirements.txt --python "$VENV/bin/python3"
else
    if [ ! -d "$VENV" ]; then
        echo "Setting up a virtual environment (first run only)..."
        python3 -m venv "$VENV"
    fi
    "$VENV/bin/python3" -m pip install -q --upgrade pip
    "$VENV/bin/python3" -m pip install -q -r requirements.txt
fi

exec "$VENV/bin/python3" loader/app.py
