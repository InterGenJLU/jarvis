#!/usr/bin/env bash
# Install the JARVIS Desktop Bridge GNOME Shell extension.
#
# Usage:  ./scripts/install_desktop_extension.sh
#
# After install, you MUST logout/login (Wayland) for the extension to load.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
EXT_UUID="jarvis-desktop@jarvis"
EXT_SRC="$REPO_ROOT/extensions/$EXT_UUID"
EXT_DEST="$HOME/.local/share/gnome-shell/extensions/$EXT_UUID"

echo "=== JARVIS Desktop Extension Installer ==="

# 1. Verify source exists
if [ ! -f "$EXT_SRC/extension.js" ]; then
    echo "ERROR: extension source not found at $EXT_SRC"
    exit 1
fi

# 2. Create destination and copy
mkdir -p "$EXT_DEST"
cp -v "$EXT_SRC/metadata.json" "$EXT_DEST/"
cp -v "$EXT_SRC/extension.js"  "$EXT_DEST/"

echo ""
echo "Extension installed to: $EXT_DEST"

# 3. Enable the extension
echo ""
echo "Enabling extension..."
gnome-extensions enable "$EXT_UUID" 2>/dev/null || true

# 4. Check if it's recognized
echo ""
if gnome-extensions info "$EXT_UUID" 2>/dev/null | grep -q "State"; then
    echo "Extension recognized by GNOME Shell."
    gnome-extensions info "$EXT_UUID"
else
    echo "Extension installed but not yet loaded."
    echo "You need to logout and login for Wayland to pick it up."
fi

# 5. Test D-Bus (will only work after logout/login on first install)
echo ""
echo "Testing D-Bus connection..."
if gdbus call --session \
    --dest org.jarvis.Desktop \
    --object-path /org/jarvis/Desktop \
    --method org.jarvis.Desktop.Ping 2>/dev/null; then
    echo "D-Bus bridge is LIVE!"
else
    echo "D-Bus not responding yet — logout/login required for first install."
fi

echo ""
echo "=== Done ==="
