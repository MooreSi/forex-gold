#!/bin/bash
# FOREX Trader — Stop
cd "$(dirname "$0")"

# Pause the auto-restart watchdog before killing anything, or it would start
# the app straight back up and Stop would not stop. Removing the flag leaves
# the LaunchAgent installed but makes every tick a no-op; the app re-arms
# itself on next startup when the Settings toggle is on. See
# forex_trader/core/core_autostart.py.
rm -f "$HOME/Library/Application Support/ForexTrader/data/watchdog.armed" 2>/dev/null

PIDS=$(lsof -ti:8888 2>/dev/null)
if [ -z "$PIDS" ]; then
    echo "FOREX Trader is not running."
else
    echo "Stopping FOREX Trader (PID $PIDS)..."
    kill $PIDS 2>/dev/null
    sleep 1
    echo "Stopped."
fi
