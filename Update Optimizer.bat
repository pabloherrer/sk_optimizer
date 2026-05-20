@echo off
:: ============================================================
:: S&K Route Optimizer — Update (Windows)
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

:: ── Clean up any in-progress merge/rebase from a previous failure ──
git merge --abort >nul 2>&1
git rebase --abort >nul 2>&1

:: ── Save local data changes ───────────────────────
echo Saving any local data changes...
git stash push --include-untracked --quiet --message "auto-update-stash" >nul 2>&1
:: We don't reliably detect whether a stash was created on Windows;
:: just try to pop later — it's a no-op if there's nothing to pop.

:: ── Pull latest version ───────────────────────────
echo Downloading latest version from GitHub...
echo   https://github.com/pabloherrer/sk_optimizer
echo.
git pull origin main > pull_output.tmp 2>&1
set PULL_RC=%errorlevel%
type pull_output.tmp

if %PULL_RC% neq 0 (
    echo.
    echo ================================================
    echo   Update failed — see git message above
    echo ================================================

    :: Diagnose error type
    findstr /i /c:"could not resolve host" /c:"timed out" /c:"network" /c:"failed to connect" pull_output.tmp >nul
    if not errorlevel 1 (
        echo   Reason: NETWORK problem — check your internet connection.
        del pull_output.tmp >nul 2>&1
        git stash pop --quiet >nul 2>&1
        pause
        exit /b 1
    )

    findstr /i /c:"authentication" /c:"permission denied" /c:"403" /c:"401" pull_output.tmp >nul
    if not errorlevel 1 (
        echo   Reason: AUTHENTICATION failed — GitHub credentials may have expired.
        del pull_output.tmp >nul 2>&1
        git stash pop --quiet >nul 2>&1
        pause
        exit /b 1
    )

    findstr /i /c:"unmerged" /c:"conflict" /c:"unresolved conflict" pull_output.tmp >nul
    if not errorlevel 1 (
        echo   Reason: MERGE CONFLICT from a previous interrupted update.
        echo.
        echo   --^> Attempting automatic recovery...
        git stash pop --quiet >nul 2>&1

        :: Back up data files
        if not exist "%TEMP%\sk_recovery" mkdir "%TEMP%\sk_recovery"
        if exist "data\SK_Delivery_System.xlsx" copy /Y "data\SK_Delivery_System.xlsx" "%TEMP%\sk_recovery\" >nul
        if exist "data\inventory_state.json" copy /Y "data\inventory_state.json" "%TEMP%\sk_recovery\" >nul
        if exist "data\plan.json" copy /Y "data\plan.json" "%TEMP%\sk_recovery\" >nul
        if exist "local_config.json" copy /Y "local_config.json" "%TEMP%\sk_recovery\" >nul

        echo   Resetting to clean state...
        git reset --hard HEAD >nul 2>&1
        git clean -fd >nul 2>&1

        echo   Pulling latest...
        git pull origin main
        set RECOVERY_RC=%errorlevel%

        echo   Restoring data files...
        if exist "%TEMP%\sk_recovery\SK_Delivery_System.xlsx" copy /Y "%TEMP%\sk_recovery\SK_Delivery_System.xlsx" "data\" >nul
        if exist "%TEMP%\sk_recovery\inventory_state.json" copy /Y "%TEMP%\sk_recovery\inventory_state.json" "data\" >nul
        if exist "%TEMP%\sk_recovery\plan.json" copy /Y "%TEMP%\sk_recovery\plan.json" "data\" >nul
        if exist "%TEMP%\sk_recovery\local_config.json" copy /Y "%TEMP%\sk_recovery\local_config.json" "." >nul
        rmdir /S /Q "%TEMP%\sk_recovery" >nul 2>&1

        del pull_output.tmp >nul 2>&1

        if %RECOVERY_RC% equ 0 (
            echo.
            echo   [OK] Recovery succeeded — update complete.
        ) else (
            echo   [FAIL] Recovery failed. Contact support.
            pause
            exit /b 1
        )
    ) else (
        findstr /i /c:"local changes" /c:"please commit" pull_output.tmp >nul
        if not errorlevel 1 (
            echo   Reason: UNCOMMITTED CHANGES that the stash didn't catch.
            echo   Run:  git status  to see what's blocking, then re-try.
        ) else (
            echo   Reason: UNKNOWN — see the git message above.
        )
        del pull_output.tmp >nul 2>&1
        git stash pop --quiet >nul 2>&1
        pause
        exit /b 1
    )
)

del pull_output.tmp >nul 2>&1

:: ── Restore local data changes ───────────────────
git stash pop --quiet >nul 2>&1

:: ── Update dependencies if requirements changed ───
echo.
echo Checking dependencies...
sk_venv\Scripts\pip install -r requirements.txt --quiet
if %errorlevel% neq 0 (
    echo.
    echo   [WARN] Dependency update failed.
    echo   Run setup.bat to repair the virtual environment.
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
