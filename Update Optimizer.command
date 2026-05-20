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
    read -p "Press Enter to close..."
    exit 1
fi

# ── Clean up any in-progress merge/rebase from a previous failure ──
# This is the #1 reason updates fail: a prior pull was interrupted
# (network drop, Ctrl-C, conflict) and left the repo in an unmerged
# state. Abort silently — if nothing was in progress these are no-ops.
git merge --abort 2>/dev/null
git rebase --abort 2>/dev/null

# ── Stash any local changes to data files ─────────
echo "Saving any local data changes..."
STASH_BEFORE=$(git stash list 2>/dev/null | wc -l)
git stash push --include-untracked --quiet --message "auto-update-stash" 2>/dev/null
STASH_AFTER=$(git stash list 2>/dev/null | wc -l)
STASHED=0
if [ "$STASH_AFTER" -gt "$STASH_BEFORE" ]; then
    STASHED=1
fi

# ── Pull latest version ───────────────────────────
echo "Downloading latest version from GitHub..."
echo "  https://github.com/pabloherrer/sk_optimizer"
echo ""
PULL_OUT=$(git pull origin main 2>&1)
PULL_RC=$?
echo "$PULL_OUT"

if [ $PULL_RC -ne 0 ]; then
    echo ""
    echo "================================================"
    echo "  Update failed — see git message above"
    echo "================================================"

    # Diagnose: what kind of error?
    if echo "$PULL_OUT" | grep -qiE "could not resolve host|name resolution|timed? ?out|network is unreachable|failed to connect"; then
        echo "  Reason: NETWORK problem — check your internet connection."
    elif echo "$PULL_OUT" | grep -qiE "authentication|permission denied|403|401"; then
        echo "  Reason: AUTHENTICATION failed — your GitHub credentials may have expired."
    elif echo "$PULL_OUT" | grep -qiE "unmerged|conflict|merge.*aborted|exiting because of an unresolved conflict"; then
        echo "  Reason: MERGE CONFLICT from a previous interrupted update."
        echo ""
        echo "  → Attempting automatic recovery..."
        # Restore stash first so we don't lose data
        [ $STASHED -eq 1 ] && git stash pop --quiet 2>/dev/null
        # Hard reset to origin/main (discards local CODE edits, keeps data via stash flow)
        echo "  Saving data files aside..."
        mkdir -p /tmp/sk_recovery_$$
        for f in data/SK_Delivery_System.xlsx data/inventory_state.json data/plan.json local_config.json; do
            [ -f "$f" ] && cp "$f" /tmp/sk_recovery_$$/$(basename "$f")
        done
        echo "  Resetting to clean state..."
        git reset --hard HEAD 2>/dev/null
        git clean -fd 2>/dev/null
        echo "  Pulling latest..."
        git pull origin main
        RC=$?
        echo "  Restoring data files..."
        for f in /tmp/sk_recovery_$$/*; do
            [ -f "$f" ] && cp "$f" data/$(basename "$f") 2>/dev/null
            [ -f "$f" ] && [ "$(basename $f)" = "local_config.json" ] && cp "$f" ./
        done
        rm -rf /tmp/sk_recovery_$$
        if [ $RC -eq 0 ]; then
            echo ""
            echo "  ✓ Recovery succeeded — update complete."
        else
            echo "  ✗ Recovery failed. Contact support."
            read -p "Press Enter to close..."
            exit 1
        fi
    elif echo "$PULL_OUT" | grep -qiE "local changes.*would be overwritten|please commit your changes"; then
        echo "  Reason: UNCOMMITTED CHANGES that the stash didn't catch."
        echo "  Run this command then re-try the update:"
        echo "    git status"
        echo ""
        [ $STASHED -eq 1 ] && git stash pop --quiet 2>/dev/null
        read -p "Press Enter to close..."
        exit 1
    else
        echo "  Reason: UNKNOWN — see the git message above for details."
        [ $STASHED -eq 1 ] && git stash pop --quiet 2>/dev/null
        read -p "Press Enter to close..."
        exit 1
    fi
fi

# ── Restore local changes ─────────────────────────
if [ $STASHED -eq 1 ]; then
    POP_OUT=$(git stash pop 2>&1)
    if echo "$POP_OUT" | grep -qiE "conflict|merge"; then
        echo ""
        echo "  ⚠ Some local data files conflicted with the update."
        echo "  Your data is preserved in the stash. To resolve:"
        echo "    git status   (see conflicted files)"
        echo "    git checkout --theirs <file>   (keep upstream version)"
        echo "    git stash drop"
    fi
fi

# ── Update dependencies if requirements changed ───
if [ -d "sk_venv" ]; then
    echo ""
    echo "Checking dependencies..."
    sk_venv/bin/pip install -r requirements.txt --quiet
    if [ $? -ne 0 ]; then
        echo ""
        echo "  ⚠ Dependency update failed."
        echo "  Run setup.command to repair the virtual environment."
        read -p "Press Enter to close..."
        exit 1
    fi
else
    echo ""
    echo "  ⚠ Virtual environment not found."
    echo "  Run setup.command to complete installation."
fi

echo ""
echo "================================================"
echo "  Update complete!"
echo "================================================"
echo ""
echo "Changes installed. Launch the optimizer as normal."
echo ""
read -p "Press Enter to close..."
