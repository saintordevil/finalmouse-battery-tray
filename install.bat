@echo off
setlocal DisableDelayedExpansion
title Install ULX Finalmouse Battery Tray
cd /d "%~dp0" || goto folderError
if not exist "requirements.txt" goto filesError
if exist ".venv" goto validateEnvironment

rem Only consider registered, installed runtimes. Do not ask py to install Python.
set "PYLAUNCHER_ALLOW_INSTALL="
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=0"
set "selectedPython="
call py -0p 2>nul | findstr /c:"3.11" >nul
if not errorlevel 1 call :tryPython py -3.11
if defined selectedPython goto createEnvironment
call py -0p 2>nul | findstr /r /c:"-V:3\." /c:"-3\." >nul
if not errorlevel 1 call :tryPython py -3
if defined selectedPython goto createEnvironment
call :tryPython python
if not defined selectedPython goto pythonError

:createEnvironment
echo Creating the project-local Python environment...
call %selectedPython% -m venv "%~dp0.venv"
if errorlevel 1 goto createError

:validateEnvironment
if not exist "%~dp0.venv\Scripts\python.exe" goto environmentError
if not exist "%~dp0.venv\Scripts\pythonw.exe" goto environmentError
"%~dp0.venv\Scripts\python.exe" -I -c "import struct, sys; sys.exit(not (sys.version_info >= (3, 10) and struct.calcsize('P') == 8 and sys.prefix != sys.base_prefix))"
if errorlevel 1 goto environmentError
echo Installing dependencies into .venv...
"%~dp0.venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r "%~dp0requirements.txt"
if errorlevel 1 goto dependenciesError
echo.
echo Installation complete. Run start.bat or finalmouse_tray_silent.vbs.
pause
exit /b 0

:tryPython
call %* -I -c "import struct, sys; sys.exit(not (sys.version_info >= (3, 10) and struct.calcsize('P') == 8))" >nul 2>&1
if errorlevel 1 exit /b 1
set "selectedPython=%*"
exit /b 0

:pythonError
echo A 64-bit Python 3.10 or newer installation is required.
echo Install Python from python.org, then run install.bat again. Python 3.11 is recommended.
goto failed
:environmentError
echo The existing .venv is incomplete or is not a compatible 64-bit Python 3.10+ environment.
echo It has been preserved. Repair that environment or use a new project folder, then run install.bat again.
goto failed
:createError
echo Could not create .venv. Any existing files have been preserved.
echo Check the selected Python installation and folder permissions, then run install.bat again.
goto failed
:dependenciesError
echo Dependency installation failed. Review the error above and run install.bat again.
goto failed
:filesError
echo requirements.txt is missing. Restore the complete downloaded project folder.
goto failed
:folderError
echo Could not access the project folder. Check its location and permissions.
:failed
pause
exit /b 1
