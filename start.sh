#!/bin/bash
#
# Start Jarvis Service
#

echo "🟢 Starting Jarvis..."
systemctl --user start jarvis.service
systemctl --user enable jarvis.service

# Wait a moment for startup
sleep 2

# Show status
systemctl --user status jarvis.service --no-pager -l
