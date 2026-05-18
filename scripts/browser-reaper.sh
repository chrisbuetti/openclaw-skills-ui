#!/bin/bash
# browser-reaper.sh — Kill idle OCPlatform browser sessions
# Runs via launchd every 15 minutes
# Kills Chrome profiles that have been running for more than IDLE_MINUTES
# with no recent CPU activity (< 1% total CPU across all their processes)

IDLE_MINUTES=${1:-45}  # Default: 45 minutes idle threshold
LOG="/tmp/browser-reaper.log"
BROWSER_BASE="$HOME/.openclaw/browser"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"
}

# Only keep last 200 lines of log
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 200 ]; then
  tail -100 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

log "=== Reaper run (idle threshold: ${IDLE_MINUTES}m) ==="

# Find main Chrome processes for OpenClaw browser profiles
while IFS= read -r line; do
  [ -z "$line" ] && continue
  
  PID=$(echo "$line" | awk '{print $2}')
  STARTED=$(echo "$line" | awk '{print $9}')
  
  # Extract profile name
  PROFILE=$(echo "$line" | grep -oE '\.openclaw/browser/[^/]+' | sed 's|.*/||')
  [ -z "$PROFILE" ] && continue
  
  # Skip the "openclaw" profile (Chris's interactive browser)
  if [ "$PROFILE" = "openclaw" ]; then
    log "  SKIP $PROFILE (interactive profile)"
    continue
  fi
  
  # Get elapsed time in minutes using ps -o etime
  ETIME=$(ps -o etime= -p "$PID" 2>/dev/null | tr -d ' ')
  [ -z "$ETIME" ] && continue
  
  # Parse etime (formats: MM:SS, HH:MM:SS, D-HH:MM:SS)
  MINS=0
  if echo "$ETIME" | grep -q '-'; then
    DAYS=$(echo "$ETIME" | cut -d'-' -f1)
    REST=$(echo "$ETIME" | cut -d'-' -f2)
    HOURS=$(echo "$REST" | cut -d: -f1)
    MINS_PART=$(echo "$REST" | cut -d: -f2)
    MINS=$((DAYS * 1440 + HOURS * 60 + MINS_PART))
  elif [ "$(echo "$ETIME" | tr -cd ':' | wc -c)" -eq 2 ]; then
    HOURS=$(echo "$ETIME" | cut -d: -f1)
    MINS_PART=$(echo "$ETIME" | cut -d: -f2)
    MINS=$((HOURS * 60 + MINS_PART))
  else
    MINS_PART=$(echo "$ETIME" | cut -d: -f1)
    MINS=$MINS_PART
  fi
  
  # Get total CPU for all processes in this profile
  TOTAL_CPU=$(ps aux | grep ".openclaw/browser/${PROFILE}/user-data" | grep -v grep | awk '{sum += $3} END {printf "%.1f", sum}')
  
  # Get total memory
  TOTAL_MEM=$(ps aux | grep ".openclaw/browser/${PROFILE}/user-data" | grep -v grep | awk '{sum += $6} END {printf "%d", sum/1024}')
  
  log "  $PROFILE: pid=$PID uptime=${MINS}m cpu=${TOTAL_CPU}% mem=${TOTAL_MEM}MB"
  
  if [ "$MINS" -ge "$IDLE_MINUTES" ] && [ "$(echo "$TOTAL_CPU < 1.0" | bc)" -eq 1 ]; then
    log "  KILLING $PROFILE (idle ${MINS}m, cpu ${TOTAL_CPU}%)"
    kill -15 "$PID" 2>/dev/null
    # Give it 3 seconds, then force kill if still alive
    sleep 3
    kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null
  fi
  
done < <(ps aux | grep "[G]oogle Chrome --remote-debugging-port" | grep ".openclaw/browser/")

log "=== Reaper done ==="
