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

:: ── Clean up any in-progress merge/rebase ─────────
git merge --abort >nul 2>&1
git rebase --abort >nul 2>&1

:: ── Save local changes ────────────────────────────
echo Saving any local changes...
git stash push --include-untracked --quiet --message "auto-update-stash" >nul 2>&1

:: ── Pull latest ───────────────────────────────────
echo Downloading latest version from GitHub...
echo.
git pull origin main > pull_output.tmp 2>&1
set PULL_RC=%errorlevel%
type pull_output.tmp

if %PULL_RC% neq 0 (
    echo.
    echo ================================================
    echo   Update failed — see git message above
    echo ================================================

    findstr /i /c:"could not resolve host" /c:"timed out" /c:"network" pull_output.tmp >nul
    if not errorlevel 1 (
        echo   Reason: NETWORK — check your internet connection.
        del pull_output.tmp >nul 2>&1
        git stash pop --quiet >nul 2>&1
        pause
        exit /b 1
    )

    findstr /i /c:"unmerged" /c:"conflict" /c:"merge.*aborted" pull_output.tmp >nul
    if not errorlevel 1 (
        echo   Reason: MERGE CONFLICT from a previous interrupted update.
        echo   Attempting automatic recovery...
        del pull_output.tmp >nul 2>&1
        git stash pop --quiet >nul 2>&1

        :: Save data aside
        if not exist %TEMP%\sk_recovery mkdir %TEMP%\sk_recovery
        for %%F in (local_config.json data\inventory_state_final.json data\user_overrides.json data\route_geom_cache.json data\osrm_full_matrix_with_ids.npz) do (
            if exist %%F copy /Y %%F %TEMP%\sk_recovery\ >nul 2>&1
        )
        git reset --hard HEAD >nul 2>&1
        git clean -fd >nul 2>&1
        git pull origin main
        if errorlevel 1 (
            echo   ^✗ Recovery failed. Contact support.
            pause
            exit /b 1
        )
        :: Restore
        if not exist data mkdir data
        for %%F in (%TEMP%\sk_recovery\*) do (
            copy /Y %%F data\ >nul 2>&1
        )
        if exist %TEMP%\sk_recovery\local_config.json copy /Y %TEMP%\sk_recovery\local_config.json .\ >nul
        rmdir /S /Q %TEMP%\sk_recovery
        echo   ^✓ Recovery succeeded — update complete.
    ) else (
        echo   Reason: UNKNOWN — see the git message above.
        del pull_output.tmp >nul 2>&1
        git stash pop --quiet >nul 2>&1
        pause
        exit /b 1
    )
)
del pull_output.tmp >nul 2>&1

:: ── Restore stashed changes ───────────────────────
git stash pop --quiet >nul 2>&1

:: ── Update dependencies ───────────────────────────
if exist .venv (
    echo.
    echo Checking dependencies...
    .venv\Scripts\python -m pip install --upgrade pip --quiet
    .venv\Scripts\python -m pip install -r requirements.txt --quiet
    if errorlevel 1 (
        echo.
        echo   ^⚠ Dependency update failed.
        echo   Run setup.bat to repair the virtual environment.
        pause
        exit /b 1
    )
) else (
    echo.
    echo   ^⚠ Virtual environment ^(.venv^) not found.
    echo   Run setup.bat to complete installation.
)

echo.
echo ================================================
echo   Update complete!
echo ================================================
echo.
echo Changes installed. Launch the optimizer as normal.
echo.
pause
