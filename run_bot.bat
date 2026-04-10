@echo off
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"

set "PYTHON_EXE="
if exist "%ROOT%.venv-3\Scripts\python.exe" set "PYTHON_EXE=%ROOT%.venv-3\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%ROOT%.venv\Scripts\python.exe" set "PYTHON_EXE=%ROOT%.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%ROOT%venv\Scripts\python.exe" set "PYTHON_EXE=%ROOT%venv\Scripts\python.exe"

if not defined PYTHON_EXE (
    where python >nul 2>nul
    if not errorlevel 1 set "PYTHON_EXE=python"
)

if not defined PYTHON_EXE goto :python_missing

set "PYTHONUNBUFFERED=1"
echo Starting standalone bot from "%ROOT%"
"%PYTHON_EXE%" "%ROOT%main.py"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo Bot exited with code %EXIT_CODE%.
    if /I not "%BITKUB_NO_PAUSE%"=="1" pause
)

exit /b %EXIT_CODE%

:python_missing
echo.
echo [ERR] Could not find a Python interpreter for this project.
echo [ERR] Looked for:
echo        %ROOT%.venv-3\Scripts\python.exe
echo        %ROOT%.venv\Scripts\python.exe
echo        %ROOT%venv\Scripts\python.exe
echo [ERR] Please create the virtual environment or add python to PATH.
if /I not "%BITKUB_NO_PAUSE%"=="1" pause
exit /b 1