#!/data/data/com.termux/files/usr/bin/bash
export HOME=/data/data/com.termux/files/home
export PREFIX=/data/data/com.termux/files/usr
export PATH="$PREFIX/bin:$PREFIX/bin/applets:$PATH"
export TMPDIR="$PREFIX/tmp"
timeout 5 termux-wake-lock 2>/dev/null || true
exec python3 "$HOME/bin/server_supervisor.py" --ensure
