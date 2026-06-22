#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$SCRIPT_DIR/agent_startup.log"
AGENT="$SCRIPT_DIR/agent.py"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*" >> "$LOG_FILE"
}

log "=== Starting trading agent ==="
log "User: $(whoami), PID: $$"

if [ ! -f "$AGENT" ]; then
    log "ERROR: agent.py not found at $AGENT"
    exit 1
fi

# Kill any running instance
EXISTING=$(pgrep -f "python3 $AGENT" 2>/dev/null)
if [ -n "$EXISTING" ]; then
    log "Killing existing agent instance(s): $EXISTING"
    kill $EXISTING 2>/dev/null
    sleep 2
fi

cd "$SCRIPT_DIR"

log "Launching: python3 $AGENT"
python3 "$AGENT" >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

log "Agent exited with code $EXIT_CODE"
log "=== Done ==="
