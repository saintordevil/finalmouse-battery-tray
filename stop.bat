@echo off
set "PID_FILE=%LOCALAPPDATA%\finalmouse-tray\chrome.pids"
set "LOCK_FILE=%LOCALAPPDATA%\finalmouse-tray\tray.lock"

:: Kill the tray Python process
if exist "%LOCK_FILE%" (
    set /p TRAY_PID=<"%LOCK_FILE%"
    taskkill /f /pid %TRAY_PID% >nul 2>&1
    del "%LOCK_FILE%" >nul 2>&1
)

:: Kill tracked Chrome/chromedriver processes
if exist "%PID_FILE%" (
    for /f "delims=" %%p in (%PID_FILE%) do (
        taskkill /f /pid %%p >nul 2>&1
    )
    del "%PID_FILE%" >nul 2>&1
)

:: Fallback: kill any chromedriver left behind
taskkill /f /im chromedriver.exe >nul 2>&1

echo Finalmouse Battery Tray stopped.
if "%1"=="" pause
