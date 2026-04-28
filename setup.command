#!/bin/bash
# ============================================================
# S&K Route Optimizer — First-Time Setup (Mac / Linux)
# Double-click this file (or run: bash setup.command)
# ============================================================

cd "$(dirname "$0")"

echo ""
echo "================================================"
echo "  S&K Route Optimizer — Setup"
echo "================================================"
echo ""

# ── Check Python ─────────────────────────────────
# Try python3 first (standard on Mac), then python
PYTHON_CMD=""
if command -v python3 &>/dev/null; then
    PYTHON_CMD="python3"
elif command -v python &>/dev/null; then
    PYTHON_CMD="python"
fi

if [ -z "$PYTHON_CMD" ]; then
    echo "ERROR: Python is not installed."
    echo ""
    echo "Install Python 3.12 with one of these methods:"
    echo ""
    echo "  Option A — Homebrew (recommended):"
    echo "    /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    echo "    brew install python@3.12"
    echo ""
    echo "  Option B — Download from python.org:"
    echo "    https://www.python.org/downloads/"
    echo ""
    echo "Then run this setup again."
    read -p "Press Enter to close..."
    exit 1
fi

PY_VERSION=$($PYTHON_CMD --version 2>&1)
echo "Found: $PY_VERSION"
echo ""

# ── Check Python version is 3.10+ ────────────────
PY_MINOR=$($PYTHON_CMD -c "import sys; print(sys.version_info.minor)")
PY_MAJOR=$($PYTHON_CMD -c "import sys; print(sys.version_info.major)")
if [ "$PY_MAJOR" -lt 3 ] || [ "$PY_MINOR" -lt 10 ]; then
    echo "ERROR: Python 3.10 or newer is required (found $PY_VERSION)."
    echo "Please install Python 3.12 and try again."
    read -p "Press Enter to close..."
    exit 1
fi

# ── Check Git ─────────────────────────────────────
if ! command -v git &>/dev/null; then
    echo "WARNING: Git is not installed."
    echo "You won't be able to receive updates."
    echo ""
    echo "Install Git with:  brew install git"
    echo "  or download from: https://git-scm.com/download/mac"
    echo ""
    echo "Continuing setup without Git..."
    echo ""
fi

# ── Create virtual environment ────────────────────
if [ -d "sk_venv" ]; then
    echo "Virtual environment already exists — skipping creation."
else
    echo "Creating virtual environment..."
    $PYTHON_CMD -m venv sk_venv
    if [ $? -ne 0 ]; then
        echo "ERROR: Could not create virtual environment."
        read -p "Press Enter to close..."
        exit 1
    fi
    echo "Done."
fi
echo ""

# ── Install dependencies ──────────────────────────
echo "Installing dependencies (this may take 2-3 minutes)..."
echo ""
sk_venv/bin/pip install --upgrade pip --quiet
sk_venv/bin/pip install -r requirements.txt
if [ $? -ne 0 ]; then
    echo ""
    echo "ERROR: Dependency installation failed."
    echo "Check your internet connection and try again."
    read -p "Press Enter to close..."
    exit 1
fi

# ── Make launcher executable ──────────────────────
chmod +x "Launch Optimizer.command" 2>/dev/null
chmod +x "Update Optimizer.command" 2>/dev/null

echo ""
echo "================================================"
echo "  Setup complete!"
echo "================================================"
echo ""
echo "To start the optimizer:"
echo "  Double-click \"Launch Optimizer.command\""
echo ""
echo "(If macOS blocks it: right-click -> Open -> Open)"
echo ""
read -p "Press Enter to close..."
