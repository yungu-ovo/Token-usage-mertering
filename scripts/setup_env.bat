@echo off
chcp 65001 > nul
setlocal

rem ===================================================================
rem  Token Usage Metering - one-shot environment setup (Windows)
rem  Creates a project-local .venv and installs dependencies.
rem  Nothing is installed globally.
rem ===================================================================

cd /d "%~dp0.."
echo [setup] project root: %CD%

rem ---- 1. locate a python interpreter ------------------------------
set "PY="
if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not defined PY (
    where py > nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    where python > nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [setup] ERROR: no python found. Install Python 3.11+ and retry.
    exit /b 1
)
echo [setup] python: %PY%

rem ---- 2. create the venv ------------------------------------------
if exist ".venv\Scripts\python.exe" (
    echo [setup] .venv already exists, skipping creation
) else (
    echo [setup] creating .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [setup] ERROR: failed to create .venv
        exit /b 1
    )
)

rem ---- 3. install dependencies -------------------------------------
echo [setup] installing requirements ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [setup] ERROR: pip install failed
    exit /b 1
)

rem ---- 4. initialise the database + run the fast checks ------------
echo [setup] initialising database ...
".venv\Scripts\python.exe" -c "from app import db; db.init_db()"

echo [setup] running unit tests ...
".venv\Scripts\python.exe" -m pytest -q
if errorlevel 1 (
    echo [setup] WARNING: some tests failed, see output above
)

echo.
echo [setup] done. Next step:
echo     scripts\run_dev.bat
echo.
endlocal
