#!/bin/bash
# JARVIS Startup Validation Checks

echo "🔍 Running startup checks..."

# Audio configuration check
if ! ~/jarvis/check_audio_config.sh > /dev/null 2>&1; then
    echo "⚠️  WARNING: Audio configuration issues detected!"
    echo "   Run: ~/jarvis/check_audio_config.sh for details"
    echo "   Run: ~/jarvis/fix_audio_config.sh to auto-fix"
fi
