#!/bin/bash
#
# Jarvis Emergency Kill Switch
# Stops Jarvis completely and prevents restart
#

echo "🛑 JARVIS EMERGENCY KILL SWITCH ACTIVATED"
echo ""

# Stop the systemd service
echo "Stopping systemd service..."
systemctl --user stop jarvis.service

# Disable auto-restart
echo "Disabling service..."
systemctl --user disable jarvis.service

# Kill any running Python processes running jarvis
echo "Killing any remaining Jarvis processes..."
pkill -9 -f "jarvis_continuous.py"
pkill -9 -f "jarvis/main.py"

# Kill TTS/STT processes
pkill -9 -f "piper"
pkill -9 -f "aplay"
pkill -9 -f "whisper-cli"

echo ""
echo "✅ Jarvis has been shut down completely"
echo ""
echo "To restart Jarvis:"
echo "  systemctl --user start jarvis.service"
echo "  systemctl --user enable jarvis.service"
echo ""
echo "Or use the 'startjarvis' alias"
