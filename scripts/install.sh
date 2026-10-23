#!/data/data/com.termux/files/usr/bin/bash
# Update an already provisioned K20 Pro. Run inside Termux, from this checkout.
set -euo pipefail
cd "$(dirname "$0")/.."
PREFIX=/data/data/com.termux/files/usr
export PREFIX
lib="$HOME/.local/lib/mobile-server"
state="$HOME/.config/server_recovery"
for tool in python3 caddy filebrowser tor tmux; do command -v "$tool" >/dev/null; done
test -f "$HOME/.config/filebrowser/filebrowser.db"
test -x .work/mobile-gateway || { echo 'Build gateway first: cd gateway && GOOS=linux GOARCH=arm64 CGO_ENABLED=0 go build -o ../.work/mobile-gateway .' >&2; exit 1; }
caddy validate --config config/Caddyfile --adapter caddyfile
mkdir -p "$lib" "$HOME/bin" "$state" "$HOME/.config/dashboard/static" "$HOME/.config/ttyd" "$HOME/.config/tor" "$HOME/.local/share/mobile-server-tor" "$HOME/.config/services_disabled"
chmod 700 "$HOME/.local/share/mobile-server-tor" "$HOME/.config/tor"
install -m 600 app/auth.py "$lib/auth.py"
if [ ! -f "$HOME/.config/dashboard/users.json" ]; then
    python3 scripts/migrate-auth.py --source "$PREFIX/bin/dashboard"
fi
# Preserve existing Tor intent on subsequent updates. Default off on first setup.
if [ ! -f "$HOME/.config/tor/torrc" ]; then touch "$HOME/.config/services_disabled/tor"; fi
# Revisions contain only sanitized code: never back up the old plaintext dashboard.
rev="$state/releases/$(date +%Y%m%dT%H%M%S)"
mkdir -p "$rev"
chmod 700 "$rev"
cp app/*.py "$rev/"
for source in app/*.py; do install -m 600 "$source" "$lib/$(basename "$source")"; done
install -m 700 app/dashboard.py "$PREFIX/bin/dashboard.new"
mv "$PREFIX/bin/dashboard.new" "$PREFIX/bin/dashboard"
install -m 700 .work/mobile-gateway "$HOME/bin/mobile-gateway.new"
mv "$HOME/bin/mobile-gateway.new" "$HOME/bin/mobile-gateway"
for script in server_supervisor.py agy_session.sh battery_guard.sh laptop_session.py; do install -m 700 "scripts/$script" "$HOME/bin/$script.new"; mv "$HOME/bin/$script.new" "$HOME/bin/$script"; done
for command in server-user server-status server-health server-up server-down server-restart agy-resume; do
    install -m 700 "scripts/$command" "$PREFIX/bin/$command"
done
install -m 600 web/agy_index.html "$HOME/.config/ttyd/agy_index.html"
install -m 600 web/filebrowser.js "$HOME/.config/dashboard/static/patched_index.js"
install -m 600 web/laptop.js "$HOME/.config/dashboard/static/laptop.js"
install -m 600 web/interactive_player.js "$HOME/.config/dashboard/static/interactive_player.js"
install -m 600 config/torrc "$HOME/.config/tor/torrc"
install -m 600 config/Caddyfile "$HOME/.config/caddy/Caddyfile"
# Even a failed reload must leave the service supervisor running.
recover_services() {
    result=$?
    trap - EXIT
    python3 "$HOME/bin/server_supervisor.py" --ensure || true
    exit "$result"
}
trap recover_services EXIT
python3 - <<'PY'
import os,signal,sys,time
sys.path.insert(0,os.path.expanduser('~/bin'))
import server_supervisor as s
s.initialize()
# Retire the old daemon by exact script identity so it loads the new service set.
records = []
for entry in s.Path('/proc').iterdir():
    if not entry.name.isdigit(): continue
    try:
        args=(entry/'cmdline').read_bytes().split(b'\0')
        if str(s.HOME/'bin/server_supervisor.py').encode() in args and b'--daemon' in args and entry.stat().st_uid==os.getuid():
            records.append((int(entry.name), s.process_identity(int(entry.name))))
    except (OSError,ValueError): pass
s.terminate([(pid, identity) for pid, identity in records if identity is not None])
s.terminate(s.matching_processes('dashboard'))
s.terminate(s.matching_processes('transfer_gateway'))
s.terminate(s.matching_processes('battery_guard'))
PY
if ! caddy reload --config "$HOME/.config/caddy/Caddyfile" --adapter caddyfile; then
    # A stopped/broken admin API cannot reload: retire only our Caddy instance.
    python3 - <<'PY'
import os,sys
sys.path.insert(0,os.path.expanduser('~/bin'))
import server_supervisor as s
s.terminate(s.matching_processes('caddy'))
PY
fi
# ensure() initializes services immediately and reuses any existing daemon lock.
python3 "$HOME/bin/server_supervisor.py" --ensure
sleep 12
python3 "$HOME/bin/server_supervisor.py" --ensure
python3 "$HOME/bin/server_supervisor.py" --status
