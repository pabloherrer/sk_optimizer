#!/bin/bash
# ============================================================
# S&K Route Optimizer — First-Time Setup (Mac / Linux)
# Double-click this file (or run: bash setup.command)
# Creates .venv and installs dependencies.
# ============================================================

cd "$(dirname "$0")"

echo ""
echo "================================================"
echo "  S&K Route Optimizer — Setup"
echo "================================================"
echo ""

# ── Find Python 3.10–3.13 ────────────────────────
PYTHON_CMD=""
for try_cmd in python3.13 python3.12 python3.11 python3.10; do
    if command -v "$try_cmd" &>/dev/null; then
        PYTHON_CMD="$try_cmd"
        break
    fi
done
if [ -z "$PYTHON_CMD" ]; then
    for brew_py in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
                   /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10 \
                   /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
                   /usr/local/bin/python3.11 /usr/local/bin/python3.10; do
        if [ -x "$brew_py" ]; then
            PYTHON_CMD="$brew_py"
            break
        fi
    done
fi
if [ -z "$PYTHON_CMD" ]; then
    if command -v python3 &>/dev/null; then PYTHON_CMD="python3"
    elif command -v python &>/dev/null; then PYTHON_CMD="python"
    fi
fi
if [ -z "$PYTHON_CMD" ]; then
    echo "ERROR: Python is not installed."
    echo ""
    echo "Install Python 3.12 with:"
    echo "  brew install python@3.12"
    echo "Or download from:  https://www.python.org/downloads/"
    echo ""
    read -p "Press Enter to close..."
    exit 1
fi

PY_VERSION=$($PYTHON_CMD --version 2>&1)
echo "Found: $PY_VERSION"
echo ""

PY_MINOR=$($PYTHON_CMD -c "import sys; print(sys.version_info.minor)")
PY_MAJOR=$($PYTHON_CMD -c "import sys; print(sys.version_info.major)")
if [ "$PY_MAJOR" -lt 3 ] || [ "$PY_MINOR" -lt 10 ]; then
    echo "ERROR: Python 3.10+ is required (found $PY_VERSION)."
    read -p "Press Enter to close..."
    exit 1
fi
if [ "$PY_MINOR" -gt 13 ]; then
    echo "ERROR: Python 3.$PY_MINOR is too new — packages don't support it yet."
    echo "Please install Python 3.12:  brew install python@3.12"
    read -p "Press Enter to close..."
    exit 1
fi

# ── Git (for updates) ─────────────────────────────
if ! command -v git &>/dev/null; then
    echo "WARNING: Git is not installed. You won't be able to update."
    echo "Install Git with:  brew install git"
    echo "Continuing setup without Git..."
    echo ""
fi

# ── Create .venv ──────────────────────────────────
if [ -d ".venv" ]; then
    echo "Virtual environment (.venv) already exists — skipping creation."
else
    echo "Creating virtual environment..."
    $PYTHON_CMD -m venv .venv
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
.venv/bin/pip install --upgrade pip --quiet
.venv/bin/pip install -r requirements.txt
if [ $? -ne 0 ]; then
    echo ""
    echo "ERROR: Dependency installation failed."
    echo "Check your internet connection and try again."
    read -p "Press Enter to close..."
    exit 1
fi

# ── Make launchers executable ─────────────────────
chmod +x "Launch Optimizer.command" 2>/dev/null
chmod +x "Update Optimizer.command" 2>/dev/null
chmod +x "setup.command" 2>/dev/null

echo ""
echo "================================================"
echo "  Setup complete!"
echo "================================================"
echo ""
echo "To start the optimizer:"
echo "  Double-click \"Launch Optimizer.command\""
echo ""
echo "(If macOS blocks it: right-click → Open → Open)"
echo ""
read -p "Press Enter to close..."
