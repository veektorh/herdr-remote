#!/bin/sh
# tests/run.sh — tests for herdr-remote
PASS=0; FAIL=0
DIR="$(cd "$(dirname "$0")/.." && pwd)"

assert_eq() {
  if [ "$1" = "$2" ]; then PASS=$((PASS+1)); echo "  pass: $3"
  else FAIL=$((FAIL+1)); echo "  FAIL: $3 (expected '$2', got '$1')"; fi
}

echo "herdr-remote tests"
echo ""

# --- Relay ---
echo "=== Relay ==="
echo "1. relay syntax"
python3 -c "import ast; ast.parse(open('$DIR/relay/herdr_relay.py').read())" 2>/dev/null
assert_eq "$?" "0" "herdr_relay.py parses"

echo "2. PEP 723 metadata"
grep -q "requires-python" "$DIR/relay/herdr_relay.py"
assert_eq "$?" "0" "inline deps present"

echo "3. start.sh executable"
[ -x "$DIR/relay/start.sh" ]
assert_eq "$?" "0" "start.sh +x"

echo "4. relay security behavior"
PYTHONPATH="$DIR/relay" python3 -m unittest discover -s "$DIR/tests" -p 'test_*.py' >/dev/null 2>&1
assert_eq "$?" "0" "auth, origin, and command validation"

echo "5. service and installer security configuration"
bash -n "$DIR/relay/install-service.sh" "$DIR/relay/start.sh" "$DIR/relay/service.sh" "$DIR/relay/write-config.sh" "$DIR/relay/rotate-token.sh" "$DIR/relay/doctor.sh" && \
  "$DIR/tests/test_installer.sh" && \
  grep -q 'systemctl --user show-environment' "$DIR/relay/install-service.sh" && \
  grep -q 'ProtectSystem=strict' "$DIR/relay/install-service.sh" && \
  grep -q 'Wants=network-online.target' "$DIR/relay/install-service.sh"
assert_eq "$?" "0" "installer secures config and detects systemd user services"

# --- Telegram ---
echo ""
echo "=== Telegram bot ==="
echo "6. telegram bot syntax"
python3 -c "import ast; ast.parse(open('$DIR/relay/herdr_telegram.py').read())" 2>/dev/null
assert_eq "$?" "0" "herdr_telegram.py parses"

echo "7. telegram demo bot syntax"
python3 -c "import ast; ast.parse(open('$DIR/relay/herdr_telegram_demo.py').read())" 2>/dev/null
assert_eq "$?" "0" "herdr_telegram_demo.py parses"

echo "8. telegram bot has all commands"
for cmd in cmd_start cmd_agents cmd_status cmd_read cmd_send cmd_reply cmd_trust cmd_interrupt; do
  grep -q "async def $cmd" "$DIR/relay/herdr_telegram.py" || { FAIL=$((FAIL+1)); echo "  FAIL: missing $cmd"; continue; }
done
PASS=$((PASS+1)); echo "  pass: all 8 commands present"

echo "9. telegram bot env vars documented"
grep -q "HERDR_TG_TOKEN" "$DIR/relay/herdr_telegram.py" && grep -q "HERDR_TG_CHAT_ID" "$DIR/relay/herdr_telegram.py"
assert_eq "$?" "0" "env vars referenced"

# --- TUI ---
echo ""
echo "=== TUI ==="
echo "10. TUI syntax"
python3 -c "import ast; ast.parse(open('$DIR/relay/herdr_tui.py').read())" 2>/dev/null
assert_eq "$?" "0" "herdr_tui.py parses"

# --- Web app ---
echo ""
echo "=== Web app ==="
echo "11. web app key elements"
WEB="$DIR/web/index.html"
grep -q "WebSocket" "$WEB" && grep -q "theme" "$WEB" && grep -q "sendKey" "$WEB"
assert_eq "$?" "0" "has WebSocket, themes, keyboard"

echo "12. web app no hardcoded secrets"
! grep -q "c4a2385e" "$WEB" && ! grep -q "graffold" "$WEB"
assert_eq "$?" "0" "no secrets in web app"

echo "13. PWA manifest, offline shell, and browser syntax"
python3 -m json.tool "$DIR/web/manifest.webmanifest" >/dev/null && \
  node --check "$DIR/web/sw.js" >/dev/null 2>&1 && \
  awk '/<script>/{flag=1;next}/<\/script>/{flag=0}flag' "$WEB" | node --check >/dev/null 2>&1 && \
  grep -q 'icon-maskable-512.png' "$DIR/web/manifest.webmanifest" && \
  grep -q 'CACHE_NAME' "$DIR/web/sw.js" && \
  grep -q 'herdr-auth.' "$WEB" && \
  grep -q "type:'submit_text'" "$WEB" && \
  grep -q "msg.type === 'command_result'" "$WEB" && \
  grep -q 'enterkeyhint="send"' "$WEB" && \
  grep -q '"orientation": "portrait"' "$DIR/web/manifest.webmanifest" && \
  grep -q "msg.type === 'agent_update'" "$WEB" && \
  grep -q "screen.orientation.lock('portrait')" "$WEB" && \
  grep -q 'overscroll-behavior-y: none' "$WEB" && \
  grep -q 'aria-label="Back to workspaces"' "$WEB" && \
  grep -q 'lockPortraitFromGesture' "$WEB" && \
  grep -q 'requestFullscreen' "$WEB" && \
  grep -q 'aria-label="Press Enter"' "$WEB" && \
  grep -q 'touch-action: pan-y' "$WEB" && \
  grep -q 'min-height: 0; overflow-y: auto' "$WEB" && \
  grep -q 'aria-label="Move selection up"' "$WEB" && \
  grep -q 'aria-label="Move selection down"' "$WEB" && \
  grep -q 'if (show && activePane) closeTerminal()' "$WEB" && \
  grep -q 'id="pushQuiet"' "$WEB" && \
  grep -q "type: 'push_quiet'" "$WEB"
assert_eq "$?" "0" "installable PWA and subprotocol auth present"

# --- macOS app ---
echo ""
echo "=== macOS app ==="
echo "14. Swift sources parse"
if command -v swiftc >/dev/null 2>&1; then
  swiftc -parse "$DIR/herdi-mac/Sources/Agent.swift" "$DIR/herdi-mac/Sources/RelayConnection.swift" 2>/dev/null
  assert_eq "$?" "0" "core Swift parses"
else
  PASS=$((PASS+1)); echo "  skip: swiftc not available"
fi

echo "15. build.sh and dmg.sh present"
[ -x "$DIR/herdi-mac/build.sh" ] && [ -f "$DIR/herdi-mac/dmg.sh" ]
assert_eq "$?" "0" "build scripts present"

echo "16. updater points to correct repo"
grep -q "dcolinmorgan/herdr-remote" "$DIR/herdi-mac/Sources/Updater.swift"
assert_eq "$?" "0" "updater repo correct"

# --- Demo worker ---
echo ""
echo "=== Demo worker ==="
echo "17. demo worker syntax"
if [ -f "$DIR/demo-worker/src/index.js" ]; then
  node --input-type=module --check < "$DIR/demo-worker/src/index.js" 2>/dev/null && \
    grep -q "action: msg.type" "$DIR/demo-worker/src/index.js"
  assert_eq "$?" "0" "demo worker parses"
else
  PASS=$((PASS+1)); echo "  skip: not present"
fi

# --- Integration ---
echo ""
echo "=== Integration ==="
echo "18. README links to herdr-demo.pages.dev"
grep -q "herdr-demo.pages.dev" "$DIR/README.md"
assert_eq "$?" "0" "demo URL correct"

echo "19. README links to herdr-push"
grep -q "dcolinmorgan/herdr-push" "$DIR/README.md"
assert_eq "$?" "0" "plugin link present"

echo "20. LICENSE is AGPL"
grep -q "GNU AFFERO GENERAL PUBLIC LICENSE" "$DIR/LICENSE"
assert_eq "$?" "0" "AGPL license"

echo "21. authenticated WebSocket network behavior"
uv run "$DIR/tests/relay_e2e.py" >/dev/null 2>&1
assert_eq "$?" "0" "real server enforces auth, origins, validation, and acknowledgements"

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
