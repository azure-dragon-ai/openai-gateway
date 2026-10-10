@echo off
REM ============================================================
REM  openai-gateway launcher (Windows)
REM
REM  Usage:
REM    start.bat                 start on 0.0.0.0:8800 (from config.yaml)
REM    start.bat 9000            override port
REM    start.bat 9000 127.0.0.1  override port and host
REM
REM  The script reuses .venv when present, otherwise creates it,
REM  installs missing dependencies, then boots uvicorn.
REM ============================================================
setlocal EnableDelayedExpansion

cd /d "%~dp0"

set "PORT=%~1"
set "HOST=%~2"

echo ============================================
echo  openai-gateway
echo  dir : %CD%
if defined HOST (echo  bind: %HOST%:%PORT%) else (echo  bind: from config.yaml)
echo ============================================

REM ---------- locate python ----------
set "PY="
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY=%~dp0.venv\Scripts\python.exe"
    echo [info] using existing venv: .venv
)

if not defined PY (
    where py >nul 2>nul
    if !errorlevel! equ 0 (
        set "PY=py"
    )
)

if not defined PY (
    where python >nul 2>nul
    if !errorlevel! equ 0 (
        set "PY=python"
    )
)

if not defined PY (
    echo [ERROR] Python not found. Install Python 3.10+ and ensure it is on PATH.
    exit /b 1
)

echo [info] interpreter: %PY%

REM ---------- create venv if needed ----------
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo [info] creating virtualenv .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] failed to create virtualenv.
        exit /b 1
    )
    set "PY=%~dp0.venv\Scripts\python.exe"
)

set "PY=%~dp0.venv\Scripts\python.exe"

REM ---------- ensure dependencies ----------
"%PY%" -c "import fastapi, uvicorn, httpx, yaml" >nul 2>nul
if errorlevel 1 (
    echo [info] installing dependencies from requirements.txt ...
    "%PY%" -m pip install --upgrade pip
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] dependency installation failed.
        exit /b 1
    )
) else (
    echo [info] dependencies already satisfied.
)

REM ---------- port check ----------
if defined PORT (
    netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul 2>nul
    if !errorlevel! equ 0 (
        echo [WARN] port %PORT% seems to be in use already.
    )
)

REM ---------- run ----------
echo [info] starting gateway... (Ctrl+C to stop)
if defined HOST (
    "%PY%" main.py --config config.yaml --host %HOST% --port %PORT%
) else (
    "%PY%" main.py --config config.yaml
)

endlocal
