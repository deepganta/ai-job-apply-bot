#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# launch_chrome_bot.sh
# Launches the bot Chrome with CDP on port 9222.
#   • Uses the bot profile (your LinkedIn session is copied into it)
#   • Your personal Chrome keeps running untouched — this is a SECOND instance
#   • The MCP Bridge extension is auto-loaded
#
# First time only:
#   1. Quit Chrome
#   2. python scripts/import_linkedin_session.py   (copies your LinkedIn login)
#   3. bash launch_chrome_bot.sh
#
# After that, just:  bash launch_chrome_bot.sh
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXTENSION_DIR="$SCRIPT_DIR/chrome_mcp/extension"
PROFILE_DIR="$SCRIPT_DIR/runs/indeed-chrome-profile"
CDP_PORT=9222
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

if [ ! -f "$CHROME" ]; then
  echo "❌  Google Chrome not found."
  exit 1
fi

# ── Already running? ──────────────────────────────────────────────────────
if curl -s --max-time 1 "http://127.0.0.1:$CDP_PORT/json/version" &>/dev/null; then
  BROWSER=$(curl -s http://127.0.0.1:$CDP_PORT/json/version | python3 -c "import json,sys; print(json.load(sys.stdin).get('Browser','?'))" 2>/dev/null)
  echo "✅  Bot Chrome already running ($BROWSER) — nothing to do."
  echo "    Run:  python -m job_apply_bot linkedin-scan"
  exit 0
fi

# ── Warn if LinkedIn session hasn't been imported yet ─────────────────────
COOKIES="$PROFILE_DIR/Default/Cookies"
if [ ! -f "$COOKIES" ]; then
  echo "⚠️  LinkedIn session not found in bot profile."
  echo ""
  echo "   First time setup (one-time only):"
  echo "   1. Quit Chrome:  osascript -e 'quit app \"Google Chrome\"'"
  echo "   2. Import your session:  python scripts/import_linkedin_session.py"
  echo "   3. Re-run this script"
  echo ""
  read -p "   Continue anyway? (you'll need to log in manually) [y/N] " yn
  [[ "$yn" =~ ^[Yy]$ ]] || exit 0
fi

# ── Launch bot Chrome ─────────────────────────────────────────────────────
mkdir -p "$PROFILE_DIR"
echo "🚀  Launching bot Chrome..."
echo "    Profile : $PROFILE_DIR  (separate from your personal Chrome)"
echo "    CDP     : http://127.0.0.1:$CDP_PORT"
echo "    Extension loaded: Chrome MCP Bridge v0.2.0"

"$CHROME" \
  --remote-debugging-port=$CDP_PORT \
  --user-data-dir="$PROFILE_DIR" \
  --load-extension="$EXTENSION_DIR" \
  --no-first-run \
  --no-default-browser-check \
  --disable-popup-blocking \
  --disable-translate \
  --window-size=1400,900 \
  "https://www.linkedin.com/jobs/" \
  &>/dev/null &

echo "    Waiting for CDP..."
for i in $(seq 1 20); do
  sleep 1
  if curl -s --max-time 1 "http://127.0.0.1:$CDP_PORT/json/version" &>/dev/null; then
    BROWSER=$(curl -s http://127.0.0.1:$CDP_PORT/json/version | python3 -c "import json,sys; print(json.load(sys.stdin).get('Browser','?'))" 2>/dev/null)
    echo ""
    echo "✅  Bot Chrome ready! ($BROWSER)"
    echo ""
    echo "    Run the bot:"
    echo "      source .venv/bin/activate"
    echo "      python -m job_apply_bot linkedin-scan"
    echo "      python -m job_apply_bot apply --mode auto"
    exit 0
  fi
  printf "."
done

echo ""
echo "⚠️  Chrome didn't respond on port $CDP_PORT within 20s."
echo "    Try:  curl http://127.0.0.1:$CDP_PORT/json/version"
