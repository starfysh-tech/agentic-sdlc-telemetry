#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="$HOME/.claude/usage-data/sdlc-analytics"
SCRIPT_NAME="sdlc_extract.py"
DB_NAME="sdlc_analytics.db"

echo "Uninstalling agentic-sdlc-telemetry..."

# Remove script
if [ -f "$INSTALL_DIR/$SCRIPT_NAME" ]; then
    rm "$INSTALL_DIR/$SCRIPT_NAME"
    echo "  Removed: $INSTALL_DIR/$SCRIPT_NAME"
else
    echo "  Script not found at $INSTALL_DIR (already removed?)"
fi

# Prompt before touching the database
if [ -f "$INSTALL_DIR/$DB_NAME" ]; then
    echo ""
    read -r -p "Remove database at $INSTALL_DIR/$DB_NAME? [y/N] " response
    if [[ "${response:-N}" =~ ^[Yy]$ ]]; then
        rm -f "$INSTALL_DIR/$DB_NAME" \
              "$INSTALL_DIR/$DB_NAME-shm" \
              "$INSTALL_DIR/$DB_NAME-wal"
        echo "  Removed: $INSTALL_DIR/$DB_NAME"
    else
        echo "  Database preserved."
    fi
fi

# Remove directory only if empty
if [ -d "$INSTALL_DIR" ] && [ -z "$(ls -A "$INSTALL_DIR")" ]; then
    rmdir "$INSTALL_DIR"
    echo "  Removed empty dir: $INSTALL_DIR"
fi

echo ""
echo "Done."
