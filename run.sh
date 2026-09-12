#!/usr/bin/env bash
# Cross-platform (macOS/Linux/WSL/git-bash) launcher for ABPS.
# Windows cmd.exe/PowerShell users: see README.md for the direct python
# commands instead of this script.
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment in .venv ..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate 2>/dev/null || source .venv/Scripts/activate

echo "Installing dependencies ..."
pip install -q -r requirements.txt

DEMO_FLAG=""
for arg in "$@"; do
  if [ "$arg" = "--demo-data" ]; then
    export ABPS_DEMO_DATA=1
  fi
done

if [ -z "$ABPS_SECRET_KEY" ]; then
  echo ""
  echo "NOTE: ABPS_SECRET_KEY is not set — a random key will be generated for this"
  echo "run only, and every login will be invalidated the next time you restart."
  echo "Set 'export ABPS_SECRET_KEY=...' first for anything beyond a quick look."
fi

cd backend
echo ""
echo "Starting ABPS on http://localhost:8000  (browse to localhost, not 0.0.0.0)"
echo ""
python main.py
