@echo off
:: ============================================================
:: S&K Route Optimizer — Windows Launcher
:: Double-click to start. Browser opens automatically.
:: Runs the FINAL dashboard (sk_solver_final + Flask UI).
:: ============================================================

cd /d "%~dp0"

:: ── Check setup has been run ──────────────────────
if not exist .venv (
    echo.
    echo Setup has not been run yet.
    echo Please double-click "setup.bat" first.
    echo.
    pause
    exit /b 1
)

:: ── Check FINAL app module exists ─────────────────
if not exist final\app\server.py (
    echo.
    echo ERROR: final\app\server.py not found.
    echo Either the repository is incomplete or out of date.
    echo Run "Update Optimizer.bat" to repair.
    pause
    exit /b 1
)

:: ── Force UTF-8 so Unicode characters don't crash ──
set PYTHONUTF8=1
chcp 65001 >nul 2>&1

echo.
echo ================================================
echo   S^&K Route Optimizer
echo ================================================
echo.
echo Starting S^&K Route Dispatch...
echo   http://127.0.0.1:5050
echo.
echo Close this window to stop the app.
echo.

:: Open the browser in 2 seconds (after Flask binds the port).
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:5050"

:: Run the dashboard (blocks until Ctrl-C / window close).
.venv\Scripts\python -m final.app

echo.
echo App stopped.
pause
