@echo off
set "ROOT=%~dp0"

set "ACTIVATE_SCRIPT="
if exist "%ROOT%.venv-3\Scripts\activate.bat" set "ACTIVATE_SCRIPT=%ROOT%.venv-3\Scripts\activate.bat"
if not defined ACTIVATE_SCRIPT if exist "%ROOT%.venv\Scripts\activate.bat" set "ACTIVATE_SCRIPT=%ROOT%.venv\Scripts\activate.bat"
if not defined ACTIVATE_SCRIPT if exist "%ROOT%venv\Scripts\activate.bat" set "ACTIVATE_SCRIPT=%ROOT%venv\Scripts\activate.bat"

if not defined ACTIVATE_SCRIPT (
    echo [ERR] Could not find a local virtual environment activate script.
    echo [ERR] Looked for .venv-3, .venv, and venv under "%ROOT%".
    exit /b 1
)

call "%ACTIVATE_SCRIPT%"

call "%ACTIVATE_SCRIPT%"
