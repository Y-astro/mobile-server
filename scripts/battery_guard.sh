#!/data/data/com.termux/files/usr/bin/bash
# ==============================================================================
# Battery Guard & Hardware Protection Daemon for Android Server (Redmi K20 Pro)
# Monitors battery readings and alerts the owner. Cannot control charging hardware.
# ==============================================================================

if [ -d "/data/data/com.termux/files/home" ]; then
    BASE_HOME="/data/data/com.termux/files/home"
else
    BASE_HOME="$HOME"
fi

CONFIG_DIR="$BASE_HOME/.config/battery_guard"
CONFIG_FILE="$CONFIG_DIR/config.env"
PID_FILE="$CONFIG_DIR/battery_guard.pid"
LOG_FILE="$CONFIG_DIR/battery_guard.log"
STATUS_FILE="$CONFIG_DIR/status.json"

mkdir -p "$CONFIG_DIR"

# Load default configurations
MAX_BATTERY_PERCENT=80
MAX_BATTERY_VOLTAGE_MV=4300
MIN_BATTERY_PERCENT=30
MAX_TEMP_CELSIUS=40.0
TTS_ALERT_INTERVAL=900
NOTIFICATION_ID="battery_guard_monitor"
SMART_PLUG_OFF_URL=""
SMART_PLUG_ON_URL=""
ENABLE_TTS=true
POLL_INTERVAL=120

if [ -f "$CONFIG_FILE" ]; then
    # shellcheck source=/dev/null
    source "$CONFIG_FILE"
fi

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

LAST_TTS_TIME=0

speak_alert() {
    local message="$1"
    local now
    now=$(date +%s)
    if [ "$ENABLE_TTS" = "true" ] && command -v termux-tts-speak >/dev/null 2>&1; then
        if [ $(( now - LAST_TTS_TIME )) -ge "$TTS_ALERT_INTERVAL" ]; then
            timeout -k 2 12 termux-tts-speak "$message" 2>/dev/null &
            LAST_TTS_TIME=$now
            log "[TTS] Spoke: $message"
        fi
    fi
}

send_notification() {
    local title="$1"
    local content="$2"
    local priority="$3"
    if command -v termux-notification >/dev/null 2>&1; then
        timeout -k 2 8 termux-notification \
            --id "$NOTIFICATION_ID" \
            --title "$title" \
            --content "$content" \
            --priority "$priority" 2>/dev/null || true
    fi
}

trigger_webhook() {
    local url="$1"
    local name="$2"
    if [ -n "$url" ]; then
        log "[WEBHOOK] Triggering $name webhook"
        curl -s -m 5 -X POST "$url" >/dev/null 2>&1 || curl -s -m 5 "$url" >/dev/null 2>&1 || log "[WEBHOOK] Warning: $name webhook request failed."
    fi
}

check_battery() {
    local raw_json
    raw_json=$(timeout 3 termux-battery-status 2>/dev/null)
    if ! printf '%s' "$raw_json" | jq -e '(.percentage | type == "number") and (.temperature | type == "number") and (.voltage | type == "number")' >/dev/null 2>&1; then
        log "[WARN] Failed to query battery status (timed out or empty)."
        return 1
    fi

    local pct temp plugged voltage status health
    pct=$(echo "$raw_json" | jq -r '.percentage')
    temp=$(echo "$raw_json" | jq -r '.temperature')
    plugged=$(echo "$raw_json" | jq -r '.plugged // "UNKNOWN"')
    voltage=$(echo "$raw_json" | jq -r '.voltage')
    status=$(echo "$raw_json" | jq -r '.status // "UNKNOWN"')
    health=$(echo "$raw_json" | jq -r '.health // "UNKNOWN"')

    local is_plugged=false
    if [[ "$plugged" == "PLUGGED_AC" || "$plugged" == "PLUGGED_USB" || "$plugged" == "PLUGGED_WIRELESS" ]]; then
        is_plugged=true
    fi

    local alert_state="NORMAL"
    local alert_msg="Battery levels operating normally."
    local recommendation="Safe operating range."

    # 1. Thermal check (Highest priority)
    local temp_int=${temp%.*}
    local max_temp_int=${MAX_TEMP_CELSIUS%.*}
    if [ "$temp_int" -ge "$max_temp_int" ]; then
        alert_state="CRITICAL_OVERHEAT"
        alert_msg="High battery temperature: ${temp}°C! Reduce load and cool the phone."
        recommendation="Cool device immediately. Ensure airflow and remove protective case."
        log "[ALERT] Thermal limit exceeded: ${temp}°C (limit: ${MAX_TEMP_CELSIUS}°C)"
        send_notification "⚠️ Server Overheating (${temp}°C)" "$alert_msg" "high"
        speak_alert "Warning. Server battery temperature is high at ${temp_int} degrees."

    # 2. Overcharge / High Voltage Swelling Prevention
    elif [ "$is_plugged" = true ] && { [ "$pct" -ge "$MAX_BATTERY_PERCENT" ] || [ "$voltage" -ge "$MAX_BATTERY_VOLTAGE_MV" ]; }; then
        alert_state="OVERCHARGE_WARNING"
        alert_msg="Battery at ${pct}% (${voltage}mV). Overcharge limit is ${MAX_BATTERY_PERCENT}%."
        recommendation="Disconnect USB charger or enable smart plug automation to prevent battery pillowing."
        log "[ALERT] Overcharge limit reached: ${pct}% at ${voltage}mV (threshold: ${MAX_BATTERY_PERCENT}%)"
        send_notification "🔋 Server Battery Full (${pct}%)" "Unplug charger or toggle smart plug to protect battery health." "high"
        speak_alert "Server battery is full at ${pct} percent. Please unplug charger."
        trigger_webhook "$SMART_PLUG_OFF_URL" "CUT_CHARGER_POWER"

    # 3. Low Battery Alert
    elif [ "$is_plugged" = false ] && [ "$pct" -le "$MIN_BATTERY_PERCENT" ]; then
        alert_state="LOW_BATTERY_WARNING"
        alert_msg="Battery down to ${pct}% on battery power."
        recommendation="Connect charger to avoid server shutdown."
        log "[ALERT] Low battery warning: ${pct}% unplugged."
        send_notification "🪫 Server Battery Low (${pct}%)" "Connect charger now to keep services online." "high"
        speak_alert "Server battery is low at ${pct} percent. Please connect charger."
        trigger_webhook "$SMART_PLUG_ON_URL" "RESUME_CHARGER_POWER"
    fi

    # Write persistent status.json for Dashboard telemetry
    cat <<EOF > "$STATUS_FILE.tmp"
{
  "timestamp": $(date +%s),
  "updated_at": "$(date '+%Y-%m-%d %H:%M:%S')",
  "percentage": $pct,
  "temperature": $temp,
  "voltage_mv": $voltage,
  "plugged": "$plugged",
  "is_plugged": $is_plugged,
  "status": "$status",
  "health": "$health",
  "alert_state": "$alert_state",
  "alert_message": "$alert_msg",
  "recommendation": "$recommendation",
  "max_percent_threshold": $MAX_BATTERY_PERCENT,
  "min_percent_threshold": $MIN_BATTERY_PERCENT
}
EOF
    mv "$STATUS_FILE.tmp" "$STATUS_FILE"
}

run_daemon() {
    if [ -f "$PID_FILE" ]; then
        local old_pid
        old_pid=$(cat "$PID_FILE" 2>/dev/null)
        if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null && grep -qa "battery_guard" "/proc/$old_pid/cmdline" 2>/dev/null; then
            echo "Battery Guard daemon is already running (PID $old_pid)."
            exit 0
        fi
        rm -f "$PID_FILE"
    fi

    echo $$ > "$PID_FILE"
    trap 'rm -f "$PID_FILE"; exit 0' SIGTERM SIGINT EXIT

    log "Battery Guard daemon started (PID $$)"
    echo "Battery Guard daemon started (PID $$)."

    while true; do
        check_battery
        sleep "$POLL_INTERVAL"
    done
}

stop_daemon() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            sleep 1
            kill -9 "$pid" 2>/dev/null
            rm -f "$PID_FILE"
            echo "Battery Guard daemon (PID $pid) stopped."
            return 0
        fi
        rm -f "$PID_FILE"
    fi
    pkill -f "battery_guard.sh --daemon" 2>/dev/null || true
    echo "Battery Guard daemon is not running."
}

show_status() {
    if [ -f "$STATUS_FILE" ]; then
        cat "$STATUS_FILE"
    else
        check_battery
        cat "$STATUS_FILE"
    fi
}

case "$1" in
    --daemon)
        run_daemon
        ;;
    --stop)
        stop_daemon
        ;;
    --status)
        show_status
        ;;
    --once)
        check_battery
        ;;
    *)
        echo "Usage: $0 {--daemon|--stop|--status|--once}"
        exit 1
        ;;
esac
