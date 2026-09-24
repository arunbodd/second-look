#!/bin/bash
# Double-click me to start Second Look. The script:
#   1. checks for Python 3.11 or newer,
#   2. creates the virtualenv on first run and reinstalls packages only when requirements.txt changes,
#   3. creates .env from .env.example if there is none (add an API key there for live AI reviews),
#   4. says which AI review you will get: live models, the recorded run, or rules only,
#   5. starts the app and opens the browser.
# The app listens on http://127.0.0.1:8000 and https://127.0.0.1:8443 (self-signed certificate:
# accept the one-time browser warning), or the next free ports if those are taken.
# Ctrl+C in this window stops it.
cd "$(dirname "$0")" || exit 1
fail() { echo "$1"; read -r -p "Press Enter to close"; exit 1; }

PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then PY="$c"; break; fi
done
[ -n "$PY" ] || fail "Second Look needs Python 3.11 or newer. Install it from python.org and double-click this file again."

if [ ! -x .venv/bin/python ]; then
  echo "Creating the virtualenv (first run only)..."
  "$PY" -m venv .venv || fail "Could not create a virtualenv."
fi
. .venv/bin/activate

STAMP=.venv/.requirements.sha
NEW=$(shasum -a 256 requirements.txt 2>/dev/null || sha256sum requirements.txt)
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$NEW" ]; then
  echo "Installing packages..."
  pip install -q --upgrade pip >/dev/null 2>&1
  pip install -q -r requirements.txt || fail "Package install failed."
  echo "$NEW" > "$STAMP"
fi

[ -f .env ] || { [ -f .env.example ] && cp .env.example .env && echo "Created .env from .env.example."; }
[ -f .env ] && chmod 600 .env  # API keys: readable by you only

has_key() { grep -Eq "^[[:space:]]*$1[[:space:]]*=[[:space:]]*['\"]?[^'\"[:space:]#]+" .env 2>/dev/null; }
if has_key OPENAI_API_KEY || has_key ANTHROPIC_API_KEY || has_key GEMINI_API_KEY || has_key PERPLEXITY_API_KEY || has_key LLM_API_KEY; then
  echo "AI review: live models. Pick the writer and judge in the Models menu; nothing is sent until you press Run."
elif [ -f data/recorded_review.json ]; then
  echo "AI review: the recorded run (no API key found in .env). Add a key to .env to run new reviews."
else
  echo "AI review: rules only (no API key in .env and no recorded run). The Rules review tab works fully."
fi

echo "Starting Second Look on this computer only; it is not reachable from your network or Wi-Fi (Ctrl+C stops it)"
exec python serve.py --open
