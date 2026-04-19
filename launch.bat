@echo off
setlocal ENABLEDELAYEDEXPANSION

:: --- DEFAULT CONFIGURATION ---
set PORT=8000
set VENV_DIR=venv
set HOST=127.0.0.1

:: --- ARGUMENT PARSING ---
:parse_args
if "%~1"=="" goto :args_done
if /i "%~1"=="--port" (
    set PORT=%~2
    shift
    shift
    goto :parse_args
)
shift
goto :parse_args
:args_done

echo ============================================================
echo        RLM PIPELINE INITIALIZATION SEQUENCE
echo ============================================================

:: --- ENVIRONMENT BOOTSTRAPPING ---
echo [INFO] Probing for virtual environment...
if exist "%~dp0.venv\Scripts\activate.bat" (
    set VENV_DIR=.venv
) else if exist "%~dp0venv\Scripts\activate.bat" (
    set VENV_DIR=venv
) else if exist "%~dp0env\Scripts\activate.bat" (
    set VENV_DIR=env
) else (
    echo [WARN] Virtual environment not detected.
    echo [INFO] Provisioning hermetic execution environment ^(this may take a moment^)...
    python -m venv --copies --prompt gguficator "%~dp0venv"
    if !ERRORLEVEL! NEQ 0 (
        echo [ERROR] Failed to provision virtual environment. Ensure Python is installed and added to PATH.
        pause
        exit /b !ERRORLEVEL!
    )
    set VENV_DIR=venv
    echo [SUCCESS] Virtual environment provisioned successfully.
)

echo [INFO] Activating isolated environment: %VENV_DIR%
call "%~dp0%VENV_DIR%\Scripts\activate.bat"

:: --- DEPENDENCY HYDRATION ---
echo [INFO] Executing automated hydration protocols...
if exist "%~dp0tools\setup_*.py" (
    for %%f in ("%~dp0tools\setup_*.py") do (
        python "%%f"
        if !ERRORLEVEL! NEQ 0 (
            echo [ERROR] Critical failure in hydration protocol: %%~nxf
            echo [ERROR] Initialization aborted.
            pause
            exit /b !ERRORLEVEL!
        )
    )
) else (
    echo [WARN] No hydration protocols detected in tools directory.
)

:: --- SERVER EXECUTION & CLIENT INVOCATION ---
echo [INFO] Booting ASGI execution environment on %HOST%:%PORT%...

:: Spawns a background process to delay browser invocation, ensuring ASGI port is listening
start /b powershell -NoProfile -Command "$port = %PORT%; $hostIp = '%HOST%'; while ((Test-NetConnection -ComputerName $hostIp -Port $port -WarningAction SilentlyContinue).TcpTestSucceeded -eq $false) { Start-Sleep -Milliseconds 500 }; Start-Process ('http://' + $hostIp + ':' + $port)"

:: Executes the Uvicorn server synchronously in the foreground terminal
python -m uvicorn main:app --host %HOST% --port %PORT%

if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Server execution terminated unexpectedly.
    pause
    exit /b %ERRORLEVEL%
)