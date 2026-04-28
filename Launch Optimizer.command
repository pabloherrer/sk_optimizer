#!/bin/bash
# ============================================================
# S&K Route Optimizer — Mac Launcher
# Double-click to start. Browser opens automatically.
# ============================================================

cd "$(dirname "$0")"

echo ""
echo "================================================"
echo "  S&K Route Optimizer"
echo "================================================"
echo ""

# ── Check setup has been run ──────────────────────
if [ ! -d "sk_venv" ]; then
    echo "Setup has not been run yet."
    echo "Please double-click \"setup.command\" first."
    echo ""
    read -p "Press Enter to close..."
    exit 1
fi

echo "Starting... browser will open at http://localhost:5050"
echo ""
echo "Close this window to stop the app."
echo ""

sk_venv/bin/python app.py

echo ""
read -p "Server stopped. Press Enter to close..."
