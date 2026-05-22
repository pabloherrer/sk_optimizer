@echo off
:: ============================================================
:: S&K Route Optimizer — First-Time Setup (Windows)
:: Double-click this file (or run: setup.bat)
:: Creates .venv and installs dependencies.
:: ============================================================

cd /d "%~dp0"

echo.
echo ================================================
echo   S^&K Route Optimizer — Setup
echo ================================================
echo.

:: ── Find Python ───────────────────────────────────
set PYTHON_CMD=
for %%P in (python3.13 python3.12 python3.11 python3.10 python3 python) do (
    %%P --version >nul 2>&1
    if not errorlevel 1 (
        set PYTHON_CMD=%%P
        goto found_python
    )
)
:found_python
if "%PYTHON_CMD%"=="" (
    echo ERROR: Python is not installed.
    echo.
    echo Install Python 3.12 from: https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

%PYTHON_CMD% --version
echo.

:: ── Create .venv ──────────────────────────────────
if exist .venv (
    echo Virtual environment ^(.venv^) already exists — skipping creation.
) else (
    echo Creating virtual environment...
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 (
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
.venv\Scripts\python -m pip install --upgrade pip --quiet
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 (
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
