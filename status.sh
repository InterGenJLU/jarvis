#!/bin/bash
#
# Check Jarvis Service Status
#

echo "📊 Jarvis Status:"
echo ""
systemctl --user status jarvis.service --no-pager -l
echo ""
echo "📋 Recent Logs:"
journalctl --user -u jarvis.service -n 20 --no-pager
