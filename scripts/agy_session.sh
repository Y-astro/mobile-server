#!/data/data/com.termux/files/usr/bin/bash
set -eu
export TERM=xterm-256color
export PATH="/data/data/com.termux/files/usr/bin:$PATH"
SESSION=agy
mkdir -p "$HOME/.config/server_recovery"
# Serialize creation across simultaneous websocket clients. Release before attach.
exec 9>"$HOME/.config/server_recovery/agy-session.lock"
flock 9
if ! tmux has-session -t "=$SESSION" 2>/dev/null; then
    tmux new-session -d -s "$SESSION" -n agy 'sleep 86400'
    tmux set-window-option -t "$SESSION:" history-limit 50000
    tmux respawn-window -k -t "$SESSION:" \
        'proot-distro login debian -- /usr/local/bin/agy-runner-loop'
fi
# Keep options local; the admin terminal has independent preferences.
tmux set-option -t "$SESSION" mouse off
tmux set-option -t "$SESSION" status off
tmux set-window-option -t "$SESSION:" window-size latest
tmux set-window-option -t "$SESSION:" aggressive-resize on
flock -u 9
exec 9>&-
# Closing a browser disconnects its own client; other clients stay attached.
exec tmux attach-session -t "$SESSION"
