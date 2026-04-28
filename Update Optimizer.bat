@echo off
:: ============================================================
:: S&K Route Optimizer — Update
:: Double-click to pull the latest version from GitHub.
:: ============================================================

cd /d "%~dp0"

echo.
echo ================================================
echo   S^&K Route Optimizer — Update
echo ================================================
echo.

:: ── Check git is available ────────────────────────
git --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Git is not installed.
    echo.
    echo Download Git from: https://git-scm.com/download/win
    echo Install it, then run this update again.
    echo.
    pause
    exit /b 1
)

:: ── Save local data changes ───────────────────────
echo Saving any local data changes...
git stash --include-untracked >nul 2>&1

:: ── Pull latest version ───────────────────────────
echo Downloading latest version from GitHub...
echo   https://github.com/pabloherrer/sk_optimizer
echo.
git pull origin main
if %errorlevel% neq 0 (
    echo.
    echo Restoring your local changes...
    git stash pop >nul 2>&1
    echo Update failed. Check your internet connection.
    pause
    exit /b 1
)

:: ── Restore local data changes ───────────────────
git stash pop >nul 2>&1

:: ── Update dependencies if requirements changed ───
echo.
echo Checking dependencies...
sk_venv\Scripts\pip install -r requirements.txt --quiet
if %errorlevel% neq 0 (
    echo.
    echo Dependency update failed. Try running setup.bat again.
    pause
    exit /b 1
)

echo.
echo ================================================
echo   Update complete!
echo ================================================
echo.
echo Changes installed. Launch the optimizer as normal.
echo.
pause
