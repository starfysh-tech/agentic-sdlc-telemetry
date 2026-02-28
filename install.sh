#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="$HOME/.claude/usage-data/sdlc-analytics"
SCRIPT_NAME="sdlc_extract.py"
SOURCE="$(cd "$(dirname "$0")" && pwd)/$SCRIPT_NAME"

echo "Installing agentic-sdlc-telemetry..."

# Check Python 3
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found in PATH" >&2
    exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
PYTHON_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")

if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 9 ]; }; then
    echo "ERROR: Python 3.9+ required (found $PYTHON_VERSION)" >&2
    exit 1
fi

echo "  Python: $PYTHON_VERSION"

# Verify source exists
if [ ! -f "$SOURCE" ]; then
    echo "ERROR: $SOURCE not found" >&2
    exit 1
fi

# Create install dir and copy
mkdir -p "$INSTALL_DIR"
cp "$SOURCE" "$INSTALL_DIR/$SCRIPT_NAME"
chmod +x "$INSTALL_DIR/$SCRIPT_NAME"

echo "  Installed: $INSTALL_DIR/$SCRIPT_NAME"
echo ""
echo "Run:"
echo "  python3 $INSTALL_DIR/$SCRIPT_NAME --help"
echo "  python3 $INSTALL_DIR/$SCRIPT_NAME -v"
