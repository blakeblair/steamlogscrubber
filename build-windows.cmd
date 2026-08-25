@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
title Steam Log Scrubber Windows Builder

echo Steam Log Scrubber Windows Builder
echo.
echo The finished app will be placed in the dist folder.
echo.

set "PYTHON_EXE="

for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
if not defined PYTHON_EXE for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"

call :validate_python

if not defined PYTHON_EXE (
    where winget >nul 2>nul
    if errorlevel 1 goto :missing_python

    echo Python 3.10 or newer was not found. Installing Python 3.13 for this user...
    winget install --exact --id Python.Python.3.13 --source winget --scope user --accept-package-agreements --accept-source-agreements --disable-interactivity
    if errorlevel 1 goto :failed

    for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
    if not defined PYTHON_EXE if exist "%LocalAppData%\Programs\Python\Python313\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python313\python.exe"
    call :validate_python
)

if not defined PYTHON_EXE goto :missing_python

echo Using "%PYTHON_EXE%"
echo Creating an isolated build environment...

"%PYTHON_EXE%" -m venv --clear ".windows-build-venv"
if errorlevel 1 goto :failed

set "BUILD_PYTHON=%CD%\.windows-build-venv\Scripts\python.exe"

echo Installing the build tools...
"%BUILD_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 goto :failed
"%BUILD_PYTHON%" -m pip install --upgrade ".[windows-build]"
if errorlevel 1 goto :failed

echo Building SteamLogScrubber.exe...
"%BUILD_PYTHON%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --name SteamLogScrubber ^
    --paths "%CD%\src" ^
    --collect-data steamlogscrubber ^
    --distpath "%CD%\dist" ^
    --workpath "%CD%\.windows-build\work" ^
    --specpath "%CD%\.windows-build" ^
    "%CD%\packaging\windows\steamlogscrubber_windows_entry.py"
if errorlevel 1 goto :failed

if not exist "%CD%\dist\SteamLogScrubber.exe" goto :failed

echo.
echo Build complete:
echo %CD%\dist\SteamLogScrubber.exe

if /i "%CI%"=="true" exit /b 0

explorer /select,"%CD%\dist\SteamLogScrubber.exe"
pause
exit /b 0

:validate_python
if not defined PYTHON_EXE exit /b 0
"%PYTHON_EXE%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 set "PYTHON_EXE="
exit /b 0

:missing_python
echo.
echo Python 3.10 or newer could not be found or installed.
echo Install App Installer from Microsoft, then double-click this file again.
goto :failed_end

:failed
echo.
echo The Windows build failed. The error is shown above.

:failed_end
if /i "%CI%"=="true" exit /b 1
pause
exit /b 1
