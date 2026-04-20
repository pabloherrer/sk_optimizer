# S&K Route Optimizer — Installation Guide

## What You Need
- A Windows or Mac computer
- Internet access (for the one-time setup only)
- The `sk_optimizer` folder (this folder)

---

## Windows Setup (First Time Only)

### Step 1 — Install Python
1. Go to **https://www.python.org/downloads/**
2. Click the big yellow "Download Python 3.12.x" button
3. Run the installer
4. **IMPORTANT:** On the first screen, check the box **"Add Python to PATH"**
5. Click "Install Now" and let it finish

### Step 2 — Copy the optimizer folder to this computer
Copy the entire `sk_optimizer` folder anywhere you like.  
Suggested location: `C:\Users\[YourName]\Desktop\sk_optimizer`

### Step 3 — Run Setup (one time only)
1. Open the `sk_optimizer` folder
2. Double-click **`setup.bat`**
3. A black window will appear and install everything automatically
4. Takes about 2–3 minutes. When done it says "Setup complete!"

### Step 4 — Launch
Double-click **`Launch Optimizer.bat`** every time you want to run it.  
Your browser will open automatically at `http://localhost:5050`.

---

## Mac Setup (First Time Only)

### Step 1 — Install Miniconda (if not already installed)
Open Terminal and run:
```
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install python@3.12
```

### Step 2 — Copy the optimizer folder to this computer
Copy the `sk_optimizer` folder to your Desktop or Documents.

### Step 3 — Create the environment (one time only)
Open Terminal, navigate to the folder, and run:
```
conda create -n sk_routes python=3.10 -y
conda activate sk_routes
pip install flask openpyxl pandas numpy folium ortools requests
```

### Step 4 — Launch
Double-click **`Launch Optimizer.command`**  
(If macOS blocks it: right-click → Open → Open anyway)

---

## Data File

The optimizer reads from **`SK_Delivery_System.xlsx`**.

**Option A — Local copy (default)**  
The file lives in `sk_optimizer/data/SK_Delivery_System.xlsx`.  
Update this file when client or delivery data changes.

**Option B — OneDrive / SharePoint (recommended)**  
If the Excel file is synced from OneDrive, set the environment variable before launching:

*Windows* — edit `Launch Optimizer.bat` and add this line before `sk_venv\Scripts\python app.py`:
```
set SK_INPUT_FILE=C:\Users\[YourName]\OneDrive - DAO Trading LLC\SK_Delivery_System.xlsx
```

*Mac* — edit `Launch Optimizer.command` and add before `"$SK_PYTHON" app.py`:
```
export SK_INPUT_FILE="/Users/[YourName]/Library/CloudStorage/OneDrive-DAOTradingLLC/SK_Delivery_System.xlsx"
```

---

## Files That Must Stay Together

| File / Folder | What It Is |
|---|---|
| `app.py` | The web interface |
| `run_unified.py` | The optimizer engine |
| `config.py` | Settings (trucks, shift times, etc.) |
| `data/SK_Delivery_System.xlsx` | Client + delivery data |
| `data/osrm_full_matrix_with_ids.npz` | Pre-built road distance matrix |
| `data/osrm_nodes_used_with_ids.csv` | Road network node list |
| All other `.py` files | Solver components — do not delete |

**Do not move or rename individual files.** Move the entire `sk_optimizer` folder as one unit.

---

## Updating the App

When there is a new version:
1. Copy the new `sk_optimizer` folder to this computer
2. **Keep** the existing `data/` folder (do not overwrite your data)
3. No need to re-run setup — the `sk_venv` environment stays

---

## Troubleshooting

**"Python is not installed"**  
→ Complete Step 1 above. Make sure "Add Python to PATH" was checked.

**Browser does not open**  
→ Manually open your browser and go to `http://localhost:5050`

**"sk_venv not found"**  
→ Run `setup.bat` again.

**Solver fails with no routes**  
→ Check the log box in the app for error details. Most common cause: the data file has changed format.

**App was working, now it's not after a Windows update**  
→ Run `setup.bat` again to reinstall dependencies.

---

## Questions?
Contact Pablo — pabloherrerapinto@gmail.com
