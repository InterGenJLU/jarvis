#!/bin/bash
#
# Stop Jarvis Service (graceful)
#

echo "🟡 Stopping Jarvis..."
systemctl --user stop jarvis.service

echo "✅ Jarvis stopped"
echo ""
echo "Service is still enabled and will restart on boot."
echo "To disable: systemctl --user disable jarvis.service"
