#!/usr/bin/env bash
# Sets up a launchd job to run sieve-run daily.
# Usage: bash setup_launchd.sh [--hour H] [--minute M] [--uninstall]
#   --hour H      Hour to run (24h, default: 6)
#   --minute M    Minute to run (default: 0)
#   --uninstall   Remove the launchd job

set -euo pipefail

LABEL="com.sieve.run"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
SIEVE_DIR="$(cd "$(dirname "$0")" && pwd)"
SIEVE_BIN="${SIEVE_DIR}/.venv/bin/sieve-run"
LOG_DIR="${SIEVE_DIR}/data/logs"
HOUR=6
MINUTE=0

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --hour)    HOUR="$2";    shift 2 ;;
        --minute)  MINUTE="$2";  shift 2 ;;
        --uninstall)
            if launchctl list | grep -q "$LABEL"; then
                launchctl unload "$PLIST"
                echo "Unloaded $LABEL"
            fi
            rm -f "$PLIST"
            echo "Removed $PLIST"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Validate
if [[ ! -x "$SIEVE_BIN" ]]; then
    echo "Error: sieve-run not found at $SIEVE_BIN"
    echo "Run 'uv sync' first."
    exit 1
fi

mkdir -p "$LOG_DIR"

CURRENT_PATH="$(bash -lc 'echo $PATH')"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${SIEVE_BIN}</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${SIEVE_DIR}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${CURRENT_PATH}</string>
    </dict>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>${HOUR}</integer>
        <key>Minute</key>
        <integer>${MINUTE}</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/launchd.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/launchd.log</string>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

# Unload existing job if present, then load
if launchctl list | grep -q "$LABEL"; then
    launchctl unload "$PLIST"
fi
launchctl load "$PLIST"

echo "Installed: $LABEL"
echo "Runs daily at ${HOUR}:$(printf '%02d' "$MINUTE")"
echo "Logs: ${LOG_DIR}/launchd.log"
echo ""
echo "To run immediately:  launchctl start $LABEL"
echo "To uninstall:        bash setup_launchd.sh --uninstall"
