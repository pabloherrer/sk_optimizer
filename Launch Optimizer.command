#!/bin/bash
# S&K Route Optimizer — Mac Launcher
# Double-click this file to start the app.
# Uses the clean sk_routes conda env (not broken Anaconda base).

cd "$(dirname "$0")"

echo "================================================"
echo "  S&K Route Optimizer"
echo "================================================"
echo ""

# Find sk_routes conda env python — try common install locations.
SK_PYTHON=""
for base in "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/opt/anaconda3" "$HOME/opt/miniconda3" "/opt/homebrew/Caskroom/miniconda/base" "/opt/anaconda3"; do
    candidate="$base/envs/sk_routes/bin/python"
    if [ -f "$candidate" ]; then
        SK_PYTHON="$candidate"
        break
    fi
done

if [ -z "$SK_PYTHON" ]; then
    echo "ERROR: sk_routes conda env not found."
    echo ""
    echo "Create it with:"
    echo "  conda create -n sk_routes python=3.10 -y"
    echo "  conda activate sk_routes"
    echo "  pip install flask openpyxl pandas numpy folium ortools"
    echo ""
    read -p "Press Enter to close..."
    exit 1
fi

echo "Using Python: $SK_PYTHON"
echo ""
echo "Starting server..."
echo "Browser will open at http://localhost:5050"
echo ""
echo "(Close this window to stop the server)"
echo ""

# Belt-and-suspenders: prevent any stray MKL load attempt from crashing us.
export MKL_SERVICE_FORCE_INTEL=0
export KMP_DUPLICATE_LIB_OK=TRUE

"$SK_PYTHON" app.py

# If we get here, the server stopped.
echo ""
read -p "Server stopped. Press Enter to close..."
