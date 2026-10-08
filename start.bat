@echo off
setlocal DisableDelayedExpansion
title ULX Finalmouse Battery Tray
cscript.exe //nologo "%~dp0finalmouse_tray_silent.vbs" %*
set "launchCode=%ERRORLEVEL%"
if not "%launchCode%"=="0" pause
exit /b %launchCode%
