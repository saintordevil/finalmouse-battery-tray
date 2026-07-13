@echo off
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_finalmouse.ps1"
set "stopCode=%ERRORLEVEL%"

if "%stopCode%"=="0" (
    echo Finalmouse Battery Tray stopped.
) else (
    echo Finalmouse Battery Tray could not be stopped safely. Exit code: %stopCode%
)
if "%1"=="" pause
exit /b %stopCode%
