@echo off
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_finalmouse.ps1" >nul 2>&1

echo Finalmouse Battery Tray stopped.
if "%1"=="" pause
