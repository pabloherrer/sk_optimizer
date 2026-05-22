#!/bin/bash
# ============================================================
# S&K Route Optimizer — Mac Launcher
# Double-click to start. Browser opens automatically.
# Runs the FINAL dashboard (sk_solver_final + Flask UI).
# ============================================================

cd "$(dirname "$0")"

echo ""
echo "================================================"
echo "  S&K Route Optimizer"
echo "================================================"
echo ""

# ── Check setup has been run ──────────────────────
if [ ! -d ".venv" ]; then
    echo "Setup has not been run yet."
    echo "Please double-click \"setup.command\" first."
    echo ""
    read -p "Press Enter to close..."
    exit 1
fi

# ── Check the FINAL app module exists ─────────────
if [ ! -f "final/app/server.py" ]; then
    echo "ERROR: final/app/server.py not found."
    echo "Either the repository is incomplete or out of date."
    echo "Run 'Update Optimizer.command' to repair."
    read -p "Press Enter to close..."
    exit 1
fi

echo "Starting S&K Route Dispatch..."
echo "  http://127.0.0.1:5050"
echo ""
echo "Close this window to stop the app."
echo ""

# Open the browser in 2 seconds (after Flask binds the port).
(sleep 2 && open "http://127.0.0.1:5050") &

# Run the dashboard (blocks until Ctrl-C / window close).
.venv/bin/python -m final.app

echo ""
read -p "Server stopped. Press Enter to close..."
