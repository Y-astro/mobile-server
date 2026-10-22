#!/system/bin/sh
# Runs on the PHONE as Android shell UID, independently of Termux and USB.
# Boot recovery is handled separately by Termux:Boot and persisted JobScheduler.
PATH=/system/bin:/system/xbin
STATE=/data/local/tmp/phone-server-guardian
mkdir -p "$STATE"
if [ -f "$STATE/pid" ]; then
    old=$(cat "$STATE/pid")
    if kill -0 "$old" 2>/dev/null && tr '\000' ' ' < "/proc/$old/cmdline" | grep -q 'phone-server-guardian.sh'; then
        exit 0
    fi
fi
echo $$ > "$STATE/pid"
trap 'rm -f "$STATE/pid"; exit' TERM INT
failures=0
health_failures=0
last_restart=0
vpn_failures=0
last_vpn_restart=0
while true; do
    now=$(date +%s)
    if [ -z "$(pidof com.tailscale.ipn)" ]; then
        vpn_failures=$((vpn_failures + 1))
        if [ "$vpn_failures" -ge 2 ] && [ $((now - last_vpn_restart)) -ge 300 ]; then
            echo "$(date '+%F %T') Tailscale app absent; requesting VPN connection" >> "$STATE/events.log"
            # Android force-stop suppresses receivers until the app is launched.
            am start -n com.tailscale.ipn/.MainActivity >> "$STATE/events.log" 2>&1
            sleep 1
            am broadcast --include-stopped-packages -n com.tailscale.ipn/.IPNReceiver -a com.tailscale.ipn.CONNECT_VPN >> "$STATE/events.log" 2>&1
            last_vpn_restart=$now
            vpn_failures=0
        fi
    else
        vpn_failures=0
    fi
    # A deliberate server-down must never be undone by recovery.
    if [ -f /sdcard/.phone-server-paused ]; then
        failures=0
        health_failures=0
    elif [ -z "$(pidof com.termux)" ]; then
        failures=$((failures + 1))
    else
        failures=0
        # A surviving app can still have lost both its supervisor and gateway.
        # Give the service-level supervisor two minutes before cycling the app.
        response=$({ printf 'GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n'; sleep 1; } | timeout 4 toybox nc -w 2 -W 2 127.0.0.1 8000 2>/dev/null | head -n 1)
        case "$response" in
            *' 200 '*) health_failures=0 ;;
            *) health_failures=$((health_failures + 1)) ;;
        esac
    fi
    now=$(date +%s)
    if { [ "$failures" -ge 2 ] || [ "$health_failures" -ge 6 ]; } && [ $((now - last_restart)) -ge 300 ]; then
        if [ -f "$STATE/events.log" ] && [ "$(wc -c < "$STATE/events.log")" -gt 131072 ]; then
            mv "$STATE/events.log" "$STATE/events.log.1"
        fi
        if [ "$health_failures" -ge 6 ]; then
            echo "$(date '+%F %T') Gateway unresponsive for two minutes; cycling Termux" >> "$STATE/events.log"
            am force-stop com.termux >> "$STATE/events.log" 2>&1
            sleep 2
        else
            echo "$(date '+%F %T') Termux app absent; reopening" >> "$STATE/events.log"
        fi
        am start -n com.termux/.app.TermuxActivity >> "$STATE/events.log" 2>&1
        failures=0
        health_failures=0
        last_restart=$now
    fi
    sleep 20
done
