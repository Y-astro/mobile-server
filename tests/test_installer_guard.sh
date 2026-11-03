#!/usr/bin/env bash
set -euo pipefail

# Verify scripts/install.sh fails fast BEFORE invoking any command (like caddy, mktemp, etc.)
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

SENTINEL="$TMP_DIR/sentinel.txt"

# Create mock commands in PATH that write to sentinel if executed
mkdir -p "$TMP_DIR/bin"
for cmd in caddy filebrowser tor tmux python3 mktemp; do
  cat << MOCK > "$TMP_DIR/bin/$cmd"
#!/bin/sh
echo "$cmd was executed" >> "$SENTINEL"
exit 0
MOCK
  chmod +x "$TMP_DIR/bin/$cmd"
done

# Run install.sh with mocked PATH
OUTPUT=$(PATH="$TMP_DIR/bin:$PATH" bash scripts/install.sh 2>&1 || true)

if [ -f "$SENTINEL" ]; then
  echo "FAIL: install.sh executed external commands before fail-fast guard:" >&2
  cat "$SENTINEL" >&2
  exit 1
fi

if ! echo "$OUTPUT" | grep -q "Direct source writes via legacy installer are disabled"; then
  echo "FAIL: Expected fail-fast guard message not found. Output was:" >&2
  echo "$OUTPUT" >&2
  exit 1
fi

echo "PASS: install.sh fail-fast guard executes immediately before any external commands."
