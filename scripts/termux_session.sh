#!/data/data/com.termux/files/usr/bin/bash
export TERM=xterm-256color
export PREFIX="/data/data/com.termux/files/usr"
export HOME="/data/data/com.termux/files/home"
export PATH="$PREFIX/bin:$PREFIX/bin/applets:$PATH"
export LD_PRELOAD="$PREFIX/lib/libtermux-exec.so"
export TMPDIR="$PREFIX/tmp"
cd "$HOME"

SESSION="termux-admin"

# Apply tmux settings
tmux set-option -g default-terminal "xterm-256color" 2>/dev/null
tmux set-option -g history-limit 50000 2>/dev/null
tmux set-option -g mouse on 2>/dev/null
tmux set-option -g status off 2>/dev/null
tmux set-option -g window-size latest 2>/dev/null
tmux set-window-option -g aggressive-resize on 2>/dev/null

# Ensure persistent session exists
if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux new-session -d -s "$SESSION" -n "termux" "bash"
fi

exec tmux attach-session -d -t "$SESSION"
