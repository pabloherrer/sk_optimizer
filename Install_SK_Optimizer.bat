@echo off
:: ============================================================
::  S&K Route Optimizer — One-Click Installer (Windows)
::
::  Send this file to anyone. They double-click it and
::  everything installs automatically.
::
::  What it does:
::    1. Checks for Python 3 (tells you how to install if missing)
::    2. Checks for Git       (tells you how to install if missing)
::    3. Clones the repo from GitHub
::    4. Creates a virtual environment
::    5. Installs all dependencies
::    6. Launches the app
:: ============================================================

set REPO_URL=https://github.com/pabloherrer/sk_optimizer.git
set INSTALL_DIR=C:\sk_optimizer

cls
echo.
echo ========================================================
echo   S^&K Route Optimizer — Installer
echo ========================================================
echo.

:: ── Step 1: Check Python ──────────────────────────
echo [1/5] Checking Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo   Python is not installed.
    echo.
    echo   To install it:
    echo     1. Go to https://www.python.org/downloads/
    echo     2. Click the big yellow "Download Python 3.12" button
    echo     3. Run the installer
    echo     4. IMPORTANT: Check the box "Add Python to PATH"
    echo     5. Click "Install Now"
    echo.
    echo   Then double-click this file again.
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('python --version 2^>^&1') do (
    echo   Found: %%v
)
echo.

:: ── Step 2: Check Git ─────────────────────────────
echo [2/5] Checking Git...
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo   Git is not installed.
    echo.
    echo   To install it:
    echo     1. Go to https://git-scm.com/download/win
    echo     2. Download and run the installer (all defaults are fine)
    echo.
    echo   Then double-click this file again.
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('git --version 2^>^&1') do (
    echo   Found: %%v
)
echo.

:: ── Step 3: Clone the repo ────────────────────────
echo [3/5] Downloading the optimizer...
if exist "%INSTALL_DIR%" (
    echo   Folder already exists at: %INSTALL_DIR%
    echo   Pulling latest version...
    cd /d "%INSTALL_DIR%"
    git pull origin main >nul 2>&1
) else (
    git clone %REPO_URL% "%INSTALL_DIR%"
    cd /d "%INSTALL_DIR%"
)
echo   Downloaded to: %INSTALL_DIR%
echo.

:: ── Step 4: Create virtual environment ────────────
echo [4/5] Setting up Python environment...
cd /d "%INSTALL_DIR%"

if exist sk_venv (
    echo   Virtual environment already exists.
) else (
    python -m venv sk_venv
    if %errorlevel% neq 0 (
        echo   ERROR: Could not create virtual environment.
        pause
        exit /b 1
    )
    echo   Virtual environment created.
)
echo.

:: ── Step 5: Install dependencies ──────────────────
echo [5/5] Installing dependencies (this takes 2-3 minutes)...
echo.
sk_venv\Scripts\pip install --upgrade pip --quiet
sk_venv\Scripts\pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo   ERROR: Dependency installation failed.
    echo   Check your internet connection and try again.
    pause
    exit /b 1
)
echo   All dependencies installed.
echo.

:: ── Done! ─────────────────────────────────────────
echo ========================================================
echo   Installation complete!
echo ========================================================
echo.
echo   The optimizer is at: %INSTALL_DIR%
echo.
echo   To run it now and in the future:
echo     Double-click "Launch Optimizer.bat"
echo.
echo   To update to the latest version:
echo     Double-click "Update Optimizer.bat"
echo.

set /p LAUNCH="Press Enter to launch the optimizer now (or close this window to skip)..."

echo.
echo Starting the optimizer...
echo Your browser will open at http://localhost:5050
echo.
sk_venv\Scripts\python app.py
