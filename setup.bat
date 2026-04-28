@echo off
:: ============================================================
:: S&K Route Optimizer — First-Time Setup (Windows)
:: Run this ONCE when installing on a new computer.
:: ============================================================

cd /d "%~dp0"

echo.
echo ================================================
echo   S^&K Route Optimizer — Setup
echo ================================================
echo.

:: ── Check Python ─────────────────────────────────
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python is not installed or not in PATH.
    echo.
    echo Please install Python 3.12 from:
    echo   https://www.python.org/downloads/release/python-31211/
    echo.
    echo IMPORTANT: During install, check the box:
    echo   [x] Add Python to PATH
    echo.
    echo Then run this setup again.
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo Found: %%v

:: ── Check Python version is compatible (3.10–3.13) ──
for /f %%m in ('python -c "import sys; print(sys.version_info.minor)"') do set PY_MINOR=%%m
for /f %%M in ('python -c "import sys; print(sys.version_info.major)"') do set PY_MAJOR=%%M

if %PY_MAJOR% neq 3 (
    echo.
    echo ERROR: Python 3 is required, but found Python %PY_MAJOR%.
    pause
    exit /b 1
)

if %PY_MINOR% gtr 13 (
    echo.
    echo ERROR: Python 3.%PY_MINOR% is too new — our packages don't support it yet.
    echo.
    echo Please install Python 3.12 specifically from:
    echo   https://www.python.org/downloads/release/python-31211/
    echo.
    echo IMPORTANT: During install, check the box:
    echo   [x] Add Python to PATH
    echo.
    echo After installing 3.12, delete the "sk_venv" folder and run this again.
    pause
    exit /b 1
)

if %PY_MINOR% lss 10 (
    echo.
    echo ERROR: Python 3.%PY_MINOR% is too old. Please install Python 3.12.
    echo   https://www.python.org/downloads/release/python-31211/
    pause
    exit /b 1
)

echo   (Python 3.%PY_MINOR% — compatible)
echo.

:: ── Create virtual environment ────────────────────
if exist sk_venv (
    echo Virtual environment already exists — skipping creation.
) else (
    echo Creating virtual environment...
    python -m venv sk_venv
    if %errorlevel% neq 0 (
        echo ERROR: Could not create virtual environment.
        pause
        exit /b 1
    )
    echo Done.
)
echo.

:: ── Install dependencies ──────────────────────────
echo Installing dependencies (this may take 2-3 minutes)...
echo.
sk_venv\Scripts\pip install --upgrade pip --quiet
sk_venv\Scripts\pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Dependency installation failed.
    echo Check your internet connection and try again.
    pause
    exit /b 1
)

echo.
echo ================================================
echo   Setup complete!
echo ================================================
echo.
echo To start the optimizer:
echo   Double-click "Launch Optimizer.bat"
echo.
pause
