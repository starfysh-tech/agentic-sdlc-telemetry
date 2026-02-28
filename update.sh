#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="$HOME/.claude/usage-data/sdlc-analytics"
SCRIPT_NAME="sdlc_extract.py"
REMOTE_URL="https://raw.githubusercontent.com/starfysh-tech/agentic-sdlc-telemetry/main/$SCRIPT_NAME"

if [ ! -f "$INSTALL_DIR/$SCRIPT_NAME" ]; then
    echo "ERROR: Not installed. Run install.sh first." >&2
    exit 1
fi

echo "Updating $SCRIPT_NAME from GitHub..."

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

if ! curl -fsSL "$REMOTE_URL" -o "$TMP"; then
    echo "ERROR: Download failed from $REMOTE_URL" >&2
    exit 1
fi

# Sanity check: downloaded file should look like Python
if ! head -1 "$TMP" | grep -q "python"; then
    echo "ERROR: Downloaded file does not look like a Python script" >&2
    exit 1
fi

mv "$TMP" "$INSTALL_DIR/$SCRIPT_NAME"
chmod +x "$INSTALL_DIR/$SCRIPT_NAME"

echo "  Updated: $INSTALL_DIR/$SCRIPT_NAME"
