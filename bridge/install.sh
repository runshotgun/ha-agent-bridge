#!/bin/bash
# Install or update the Agent Bridge service for the current macOS user.
# Creates a venv, a config from the example (first run only), the bridge
# token, and a LaunchAgent. Run it from your login shell: the LaunchAgent
# gets this shell's PATH, because MCP servers often start through npx or uvx.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Library/Application Support/AgentBridge"
LABEL="io.github.runshotgun.agent-bridge"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }
CLAUDE="$(command -v claude || true)"
CODEX="$(command -v codex || true)"

umask 077
mkdir -p "$APP/secrets" "$APP/logs" "$APP/state" "$APP/workspace"
chmod 700 "$APP" "$APP/secrets"

uv venv --quiet --allow-existing --python 3.12 "$APP/venv"
VIRTUAL_ENV="$APP/venv" uv pip install --quiet --reinstall-package agent-bridge "$REPO"

if [ ! -f "$APP/config.toml" ]; then
  sed -e "s|@APP@|$APP|g" -e "s|@CLAUDE@|${CLAUDE:-claude}|g" -e "s|@CODEX@|${CODEX:-codex}|g" \
    "$REPO/config.example.toml" > "$APP/config.toml"
  echo "Created $APP/config.toml"
fi
if [ ! -s "$APP/secrets/bridge.token" ]; then
  openssl rand -hex 32 > "$APP/secrets/bridge.token"
  echo "Created bridge token: $APP/secrets/bridge.token (enter it in Home Assistant)"
fi
for secret in proxy.key; do
  [ -s "$APP/secrets/$secret" ] || echo "Missing: $APP/secrets/$secret (one line, mode 0600)" >&2
done

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP/venv/bin/agent-bridge</string>
    <string>--config</string><string>$APP/config.toml</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$PATH</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>WorkingDirectory</key><string>$APP/workspace</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$APP/logs/bridge.log</string>
  <key>StandardErrorPath</key><string>$APP/logs/bridge.log</string>
</dict>
</plist>
EOF

# bootout returns before the old process is gone; bootstrap fails (error 5) until it is.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
for _ in $(seq 1 20); do
  launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || break
  sleep 0.5
done
if [ -s "$APP/secrets/proxy.key" ]; then
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "Started $LABEL. Log: $APP/logs/bridge.log"
else
  echo "Add the missing secrets, then run this script again to start the service." >&2
fi
