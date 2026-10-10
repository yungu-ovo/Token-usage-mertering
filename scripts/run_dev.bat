@echo off
chcp 65001 > nul
setlocal

rem ===================================================================
rem  Token Usage Metering - start the dashboard (Windows)
rem
rem    scripts\run_dev.bat                 real data  (data\usage.db)
rem    scripts\run_dev.bat --demo          demo data  (data\demo.db)
rem    scripts\run_dev.bat --port 9000     custom port
rem
rem  The service binds to 127.0.0.1 only. No remote API is ever called.
rem ===================================================================

cd /d "%~dp0.."

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\token-meter\Scripts\python.exe"
if not exist "%PY%" (
    echo [run] ERROR: no python environment found.
    echo [run] Run scripts\setup_env.bat first.
    exit /b 1
)

set "PORT=8787"
set "DB=data\usage.db"
set "SOURCE=real WorkBuddy data"

:parse
if "%~1"=="" goto start
if /i "%~1"=="--demo" (
    set "DB=data\demo.db"
    set "SOURCE=demo data"
    shift
    goto parse
)
if /i "%~1"=="--port" (
    set "PORT=%~2"
    shift
    shift
    goto parse
)
echo [run] unknown option: %~1
exit /b 1

:start
if not exist "%DB%" (
    echo [run] database %DB% not found.
    if /i "%DB%"=="data\demo.db" (
        echo [run] generating demo data ...
        "%PY%" scripts\seed_demo.py
        if errorlevel 1 exit /b 1
    ) else (
        echo [run] ingest your WorkBuddy data first:
        echo     "%PY%" scripts\ingest_workbuddy.py --ingest
        echo [run] or start with demo data:  scripts\run_dev.bat --demo
        exit /b 1
    )
)

echo [run] source : %SOURCE%
echo [run] db     : %DB%
echo [run] open   : http://127.0.0.1:%PORT%/
echo [run] press Ctrl+C to stop
echo.

set "TUM_DB_PATH=%DB%"
"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%

endlocal
