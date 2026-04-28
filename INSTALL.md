# S&K Route Optimizer — Installation Guide

## What You Need
- A Windows or Mac computer
- Internet access (for the one-time setup only)

---

## Windows Setup

### Step 1 — Install Python
1. Go to **https://www.python.org/downloads/**
2. Click the big yellow "Download Python 3.12.x" button
3. Run the installer
4. **IMPORTANT:** On the first screen, check the box **"Add Python to PATH"**
5. Click "Install Now" and let it finish

### Step 2 — Install Git
1. Go to **https://git-scm.com/download/win**
2. Download and run the installer (all defaults are fine)

### Step 3 — Download the optimizer
1. Open Command Prompt (search "cmd" in Start menu)
2. Run this command:
```
git clone https://github.com/pabloherrer/sk_optimizer.git C:\sk_optimizer
```
This downloads everything into `C:\sk_optimizer`.

### Step 4 — Run Setup (one time only)
1. Open the `C:\sk_optimizer` folder
2. Double-click **`setup.bat`**
3. A black window will appear and install everything automatically
4. Takes about 2–3 minutes. When done it says "Setup complete!"

### Step 5 — Launch
Double-click **`Launch Optimizer.bat`** every time you want to run it.
Your browser will open automatically at `http://localhost:5050`.

---

## Mac Setup

### Step 1 — Install Python
Open Terminal and run:
```
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install python@3.12
```

### Step 2 — Install Git
Git comes with macOS by default. If prompted, install Xcode Command Line Tools when asked.

### Step 3 — Download the optimizer
In Terminal, run:
```
git clone https://github.com/pabloherrer/sk_optimizer.git ~/Desktop/sk_optimizer
```
This downloads everything to your Desktop.

### Step 4 — Run Setup (one time only)
1. Open the `sk_optimizer` folder on your Desktop
2. Double-click **`setup.command`**
3. If macOS blocks it: right-click → Open → Open
4. Takes about 2–3 minutes. When done it says "Setup complete!"

### Step 5 — Launch
Double-click **`Launch Optimizer.command`** every time you want to run it.
Your browser will open automatically at `http://localhost:5050`.

---

## Updating to the Latest Version

When there's a new version available:

**Windows:** Double-click **`Update Optimizer.bat`**
**Mac:** Double-click **`Update Optimizer.command`**

This pulls the latest code from GitHub and updates any dependencies automatically. Your local data files are preserved.

---

## Data File

The optimizer reads from **`data/SK_Delivery_System.xlsx`**.

**Option A — Local copy (default)**
The file lives inside `sk_optimizer/data/`. Update this file when client or delivery data changes.

**Option B — OneDrive / SharePoint**
If the Excel file is synced from OneDrive, set the path before launching:

*Windows* — edit `Launch Optimizer.bat` and add this line before `sk_venv\Scripts\python app.py`:
```
set SK_INPUT_FILE=C:\Users\[YourName]\OneDrive - DAO Trading LLC\SK_Delivery_System.xlsx
```

*Mac* — edit `Launch Optimizer.command` and add before `sk_venv/bin/python app.py`:
```
export SK_INPUT_FILE="/Users/[YourName]/Library/CloudStorage/OneDrive-DAOTradingLLC/SK_Delivery_System.xlsx"
```

---

## Quick Reference — Files in the Folder

| File | What to do with it |
|---|---|
| `setup.bat` / `setup.command` | Run once on a new computer |
| `Launch Optimizer.bat` / `.command` | Double-click to start the app |
| `Update Optimizer.bat` / `.command` | Double-click to get the latest version |
| `data/SK_Delivery_System.xlsx` | Your client & delivery data |
| Everything else | Don't move or rename — the app needs them |

---

## Troubleshooting

**"Python is not installed"**
→ Complete Step 1 above. On Windows, make sure "Add Python to PATH" was checked.

**Browser does not open**
→ Manually open your browser and go to `http://localhost:5050`

**"sk_venv not found"**
→ Run `setup.bat` (Windows) or `setup.command` (Mac) again.

**macOS says the file "can't be opened"**
→ Right-click the `.command` file → Open → click Open in the dialog.

**Solver fails with no routes**
→ Check the log box in the app for error details. Most common cause: the data file has changed format.

**App was working, now it's not after an OS update**
→ Run `setup.bat` / `setup.command` again to reinstall dependencies.

---

## Questions?
Contact Pablo — pabloherrerapinto@gmail.com
