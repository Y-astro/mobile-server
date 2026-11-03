#!/data/data/com.termux/files/usr/bin/bash
set -eu
export PREFIX="/data/data/com.termux/files/usr"
export HOME="/data/data/com.termux/files/home"
export PATH="$PREFIX/bin:$PREFIX/bin/applets:$PATH"
export TMPDIR="$PREFIX/tmp"
export TERM=xterm-256color
export USER="u0_a505"
unset container PD_CONTAINER PD_IMAGE PD_IMAGE_ID PROOT_L2S_DIR ANTIGRAVITY_AGENT
cd "$HOME"

# Locate the active tmux socket robustly
TMUX_SOCK="$PREFIX/var/run/tmux-10505/default"
if [ ! -S "$TMUX_SOCK" ]; then
    for s in "$PREFIX"/var/run/tmux-*/default; do
        if [ -S "$s" ]; then TMUX_SOCK="$s"; break; fi
    done
fi
export TMUX_TMPDIR="$PREFIX/var/run/tmux-10505"
TMUX_CMD="tmux -S $TMUX_SOCK"

# If an instance ID argument is passed (from ttyd ?arg=<id>), sanitize and use it
ARG="${1:-default}"
ARG=$(echo "$ARG" | tr -cd 'a-zA-Z0-9_-' | cut -c1-32)
if [ -z "$ARG" ] || [ "$ARG" = "default" ]; then
    SESSION="agy"
else
    SESSION="agy-${ARG}"
fi

mkdir -p "$HOME/.config/server_recovery"
# Serialize creation across simultaneous websocket clients. Release before attach.
exec 9>"$HOME/.config/server_recovery/${SESSION}.lock"
flock 9
if ! $TMUX_CMD has-session -t "=$SESSION" 2>/dev/null; then
    $TMUX_CMD new-session -d -s "$SESSION" -n agy 'sleep 86400'
    $TMUX_CMD set-window-option -t "$SESSION:" history-limit 50000
    $TMUX_CMD respawn-window -k -t "$SESSION:" \
        'proot-distro login debian -- /usr/local/bin/agy-runner-loop'
fi
# Keep options local; the admin terminal has independent preferences.
$TMUX_CMD set-option -t "$SESSION" mouse off
$TMUX_CMD set-option -t "$SESSION" status off
$TMUX_CMD set-window-option -t "$SESSION:" window-size latest
$TMUX_CMD set-window-option -t "$SESSION:" aggressive-resize on
flock -u 9
exec 9>&-
# Closing a browser disconnects its own client; other clients stay attached.
exec $TMUX_CMD attach-session -t "=$SESSION"
