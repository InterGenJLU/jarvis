#!/bin/bash
#
# Restart Jarvis Service
#

echo "🔄 Restarting Jarvis..."
systemctl --user restart jarvis.service

# Wait a moment for restart
sleep 2

# Show status
systemctl --user status jarvis.service --no-pager -l
