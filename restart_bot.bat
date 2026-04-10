@echo off
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"
set "BITKUB_NO_PAUSE=1"

:start
call "%ROOT%run_bot.bat"
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Bot exited with code %EXIT_CODE%. Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto start
