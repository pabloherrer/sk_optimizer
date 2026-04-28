#!/bin/bash
# ============================================================
#  S&K Route Optimizer — One-Click Installer (Mac)
#
#  Send this file to anyone. They double-click it and
#  everything installs automatically.
#
#  What it does:
#    1. Checks for Python 3 (tells you how to install if missing)
#    2. Checks for Git       (tells you how to install if missing)
#    3. Clones the repo from GitHub
#    4. Creates a virtual environment
#    5. Installs all dependencies
#    6. Launches the app
# ============================================================

set -e

REPO_URL="https://github.com/pabloherrer/sk_optimizer.git"
INSTALL_DIR="$HOME/Desktop/sk_optimizer"

clear
echo ""
echo "========================================================"
echo "  S&K Route Optimizer — Installer"
echo "========================================================"
echo ""

# ── Step 1: Check Python ──────────────────────────
echo "[1/5] Checking Python..."
PYTHON_CMD=""
if command -v python3 &>/dev/null; then
    PYTHON_CMD="python3"
elif command -v python &>/dev/null; then
    PYTHON_CMD="python"
fi

if [ -z "$PYTHON_CMD" ]; then
    echo ""
    echo "  Python is not installed."
    echo ""
    echo "  To install it, open Terminal and paste this:"
    echo ""
    echo "    /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    echo "    brew install python@3.12"
    echo ""
    echo "  Then double-click this file again."
    echo ""
    read -p "Press Enter to close..."
    exit 1
fi

PY_VERSION=$($PYTHON_CMD --version 2>&1)
PY_MAJOR=$($PYTHON_CMD -c "import sys; print(sys.version_info.major)")
PY_MINOR=$($PYTHON_CMD -c "import sys; print(sys.version_info.minor)")

if [ "$PY_MAJOR" -lt 3 ] || [ "$PY_MINOR" -lt 10 ]; then
    echo ""
    echo "  Found $PY_VERSION — but Python 3.10+ is needed."
    echo ""
    echo "  To upgrade, open Terminal and paste:"
    echo "    brew install python@3.12"
    echo ""
    echo "  Then double-click this file again."
    echo ""
    read -p "Press Enter to close..."
    exit 1
fi

echo "  Found $PY_VERSION  ✓"
echo ""

# ── Step 2: Check Git ─────────────────────────────
echo "[2/5] Checking Git..."
if ! command -v git &>/dev/null; then
    echo ""
    echo "  Git is not installed."
    echo ""
    echo "  To install it, open Terminal and paste:"
    echo "    xcode-select --install"
    echo ""
    echo "  (Click 'Install' on the popup that appears)"
    echo "  Then double-click this file again."
    echo ""
    read -p "Press Enter to close..."
    exit 1
fi

GIT_VERSION=$(git --version 2>&1)
echo "  Found $GIT_VERSION  ✓"
echo ""

# ── Step 3: Clone the repo ────────────────────────
echo "[3/5] Downloading the optimizer..."
if [ -d "$INSTALL_DIR" ]; then
    echo "  Folder already exists at: $INSTALL_DIR"
    echo "  Pulling latest version..."
    cd "$INSTALL_DIR"
    git pull origin main --quiet 2>/dev/null || true
else
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi
echo "  Downloaded to: $INSTALL_DIR  ✓"
echo ""

# ── Step 4: Create virtual environment ────────────
echo "[4/5] Setting up Python environment..."
cd "$INSTALL_DIR"

if [ ! -d "sk_venv" ]; then
    $PYTHON_CMD -m venv sk_venv
    echo "  Virtual environment created  ✓"
else
    echo "  Virtual environment already exists  ✓"
fi
echo ""

# ── Step 5: Install dependencies ──────────────────
echo "[5/5] Installing dependencies (this takes 2-3 minutes)..."
echo ""
sk_venv/bin/pip install --upgrade pip --quiet 2>/dev/null
sk_venv/bin/pip install -r requirements.txt --quiet
echo "  All dependencies installed  ✓"
echo ""

# ── Make scripts executable ───────────────────────
chmod +x "Launch Optimizer.command" 2>/dev/null
chmod +x "Update Optimizer.command" 2>/dev/null
chmod +x "setup.command" 2>/dev/null

# ── Done! ─────────────────────────────────────────
echo "========================================================"
echo "  Installation complete!"
echo "========================================================"
echo ""
echo "  The optimizer is on your Desktop in: sk_optimizer/"
echo ""
echo "  To run it now and in the future:"
echo "    Double-click \"Launch Optimizer.command\""
echo ""
echo "  To update to the latest version:"
echo "    Double-click \"Update Optimizer.command\""
echo ""

read -p "Press Enter to launch the optimizer now (or close this window to skip)..."

echo ""
echo "Starting the optimizer..."
echo "Your browser will open at http://localhost:5050"
echo ""
sk_venv/bin/python app.py
