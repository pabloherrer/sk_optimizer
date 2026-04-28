#!/bin/bash
# ============================================================
# S&K Route Optimizer — Update (Mac / Linux)
# Double-click to pull the latest version from GitHub.
# ============================================================

cd "$(dirname "$0")"

echo ""
echo "================================================"
echo "  S&K Route Optimizer — Update"
echo "================================================"
echo ""

# ── Check git is available ────────────────────────
if ! command -v git &>/dev/null; then
    echo "ERROR: Git is not installed."
    echo ""
    echo "Install Git with:  brew install git"
    echo "  or download from: https://git-scm.com/download/mac"
    echo ""
    echo "Then run this update again."
    read -p "Press Enter to close..."
    exit 1
fi

# ── Stash any local changes to data files ─────────
echo "Saving any local data changes..."
git stash --include-untracked --quiet 2>/dev/null

# ── Pull latest version ───────────────────────────
echo "Downloading latest version from GitHub..."
echo "  https://github.com/pabloherrer/sk_optimizer"
echo ""
git pull origin main
if [ $? -ne 0 ]; then
    echo ""
    echo "Update failed. Restoring your local changes..."
    git stash pop --quiet 2>/dev/null
    echo "Check your internet connection and try again."
    read -p "Press Enter to close..."
    exit 1
fi

# ── Restore local changes ─────────────────────────
git stash pop --quiet 2>/dev/null

# ── Update dependencies if requirements changed ───
if [ -d "sk_venv" ]; then
    echo ""
    echo "Checking dependencies..."
    sk_venv/bin/pip install -r requirements.txt --quiet
    if [ $? -ne 0 ]; then
        echo ""
        echo "Dependency update failed. Try running setup.command again."
        read -p "Press Enter to close..."
        exit 1
    fi
else
    echo ""
    echo "WARNING: Virtual environment not found."
    echo "Run setup.command to complete installation."
fi

echo ""
echo "================================================"
echo "  Update complete!"
echo "================================================"
echo ""
echo "Changes installed. Launch the optimizer as normal."
echo ""
read -p "Press Enter to close..."
