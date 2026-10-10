@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title openai-gateway

echo ============================================================
echo   openai-gateway
echo   dir : %CD%
echo ============================================================
echo.

set "PORT=%~1"
set "HOST=%~2"

rem ============================================================
rem  [1/3] Pick the first interpreter that ALREADY has the deps.
rem        This skips the network install entirely, which matters
rem        because pypi mirrors (aliyun / tuna) sometimes miss
rem        brand-new package versions. Set PYTHON_EXE=... to force.
rem ============================================================
echo [1/3] Looking for a Python that already has dependencies ...

call :probe ".venv\Scripts\python.exe"
if defined PY goto :deps_ok
call :probe "%USERPROFILE%\miniconda3\python.exe"
if defined PY goto :deps_ok
call :probe "%USERPROFILE%\anaconda3\python.exe"
if defined PY goto :deps_ok
call :probe "%USERPROFILE%\miniforge3\python.exe"
if defined PY goto :deps_ok
call :probe "C:\ProgramData\miniconda3\python.exe"
if defined PY goto :deps_ok
call :probe "C:\ProgramData\anaconda3\python.exe"
if defined PY goto :deps_ok
if defined PYTHON_EXE call :probe "%PYTHON_EXE%"
if defined PY goto :deps_ok
call :probe_cmd "python"
if defined PY goto :deps_ok
call :probe_cmd "py -3"
if defined PY goto :deps_ok

echo       none found - will bootstrap instead.
goto :bootstrap

:deps_ok
echo       OK  !PY!
echo.
echo [2/3] Dependencies already present - skipping install.
goto :ready

rem ============================================================
rem  [2/3] Bootstrap: reuse or create .venv, then install.
rem        Mirrors are tried in order. Official pypi is last
rem        because it is slower but always has latest versions.
rem ============================================================
:bootstrap
echo.
echo [2/3] Bootstrapping environment ...

set "BASE="
if defined PYTHON_EXE if exist "%PYTHON_EXE%" set "BASE=%PYTHON_EXE%"
if not defined BASE call :which BASE "python"
if not defined BASE call :which BASE "py -3"
if not defined BASE call :existing BASE "%USERPROFILE%\miniconda3\python.exe"
if not defined BASE call :existing BASE "%USERPROFILE%\anaconda3\python.exe"
if not defined BASE call :existing BASE "C:\ProgramData\miniconda3\python.exe"
if not defined BASE call :existing BASE "C:\ProgramData\anaconda3\python.exe"
if not defined BASE (
    echo.
    echo [ERROR] No Python found.
    echo         Install Python 3.10+ on PATH, or run:
    echo           set PYTHON_EXE=C:\path\to\python.exe
    echo           start.bat
    goto :fail
)
echo       base interpreter: !BASE!

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
    echo       reusing virtualenv: .venv
) else (
    echo       creating virtualenv .venv ...
    "!BASE!" -m venv .venv
    if errorlevel 1 (
        echo [ERROR] venv creation failed.
        goto :fail
    )
    set "PY=.venv\Scripts\python.exe"
)

echo       installing requirements.txt ...
call :try_install "https://mirrors.aliyun.com/pypi/simple" "mirrors.aliyun.com"
if not errorlevel 1 goto :ready
call :try_install "https://pypi.tuna.tsinghua.edu.cn/simple" "pypi.tuna.tsinghua.edu.cn"
if not errorlevel 1 goto :ready
call :try_install "https://pypi.org/simple" "pypi.org"
if not errorlevel 1 goto :ready
echo.
echo [ERROR] All pip mirrors failed. Install manually:
echo           "!PY!" -m pip install -r requirements.txt
goto :fail

rem ============================================================
rem  [3/3] Resolve the port, warn if occupied, then serve.
rem ============================================================
:ready
if not defined PY goto :fail

if not defined PORT (
    set "CFGP="
    for /f "usebackq delims=" %%i in (`"!PY!" -c "import yaml; print(yaml.safe_load(open('config.yaml', encoding='utf-8'))['server']['port'])"`) do set "CFGP=%%i"
    if defined CFGP set "PORT=!CFGP!"
)
if not defined PORT set "PORT=8800"

netstat -ano | findstr /C:":%PORT% " | findstr /I LISTENING >nul
if not errorlevel 1 (
    echo.
    echo [WARN] Port %PORT% is already in use:
    netstat -ano | findstr /C:":%PORT% " | findstr /I LISTENING
    echo        Stop that instance, or start elsewhere: start.bat 8801
    echo.
    set "CONT="
    set /p CONT="        Start anyway [y/N] "
    if /I not "!CONT!"=="y" goto :fail
)

echo.
echo [3/3] Starting gateway on port %PORT% - Ctrl+C to stop.
echo       health: http://127.0.0.1:%PORT%/health
echo.
if defined HOST (
    "!PY!" main.py --config config.yaml --host !HOST! --port !PORT!
) else (
    "!PY!" main.py --config config.yaml --port !PORT!
)
goto :eof

rem ============================================================
rem  helpers
rem ============================================================
:probe
rem  %~1 = full path to a python exe -> sets PY when it has all deps
if "%~1"=="" exit /b 1
if not exist "%~1" exit /b 1
"%~1" -c "import fastapi, uvicorn, httpx, yaml" >nul 2>&1
if errorlevel 1 exit /b 1
set "PY=%~1"
exit /b 0

:probe_cmd
rem  %~1 = command possibly with args, e.g. "py -3"
%~1 -c "import fastapi, uvicorn, httpx, yaml" >nul 2>&1
if errorlevel 1 exit /b 1
set "PY=%~1"
exit /b 0

:which
rem  %~1 = variable to set, %~2 = PATH command (quoted, may carry args)
%~2 -c "import sys" >nul 2>&1
if errorlevel 1 exit /b 1
if "%~1"=="" exit /b 1
set "%~1=%~2"
exit /b 0

:existing
rem  %~1 = variable to set, %~2 = absolute path that must exist
if "%~1"=="" exit /b 1
if not exist "%~2" exit /b 1
set "%~1=%~2"
exit /b 0

:try_install
rem  %~1 = index url, %~2 = trusted host
echo       trying %~1
"!PY!" -m pip install -r requirements.txt -i "%~1" --trusted-host "%~2"
exit /b %errorlevel%

:fail
echo.
echo [STOPPED] Gateway did not start.
exit /b 1
