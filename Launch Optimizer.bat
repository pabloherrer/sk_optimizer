@echo off
:: ============================================================
:: S&K Route Optimizer — Windows Launcher
:: Double-click to start. Browser opens automatically.
:: ============================================================

cd /d "%~dp0"

:: ── Check setup has been run ──────────────────────
if not exist sk_venv (
    echo.
    echo Setup has not been run yet.
    echo Please double-click "setup.bat" first.
    echo.
    pause
    exit /b 1
)

:: ── Start app ─────────────────────────────────────
echo.
echo ================================================
echo   S^&K Route Optimizer
echo ================================================
echo.
echo Starting... browser will open at http://localhost:5050
echo.
echo Close this window to stop the app.
echo.

sk_venv\Scripts\python app.py

echo.
echo App stopped.
pause
