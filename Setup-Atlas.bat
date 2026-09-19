@echo off
REM Explicit setup only. Normal CMS.bat launch never downloads packages.
setlocal
if /i "%~1"=="--help" goto :help
if not "%~1"=="" if /i not "%~1"=="--no-pause" goto :help
set "ATLAS_SETUP_PY="
set "ATLAS_SETUP_ARGS="
if defined CMS_PYTHON call :probe "%CMS_PYTHON%" ""
if not defined ATLAS_SETUP_PY call :probe "py" "-3"
if not defined ATLAS_SETUP_PY call :probe "python" ""
if not defined ATLAS_SETUP_PY goto :no_python
echo Installing Atlas's local runtime in .venv-atlas.
echo This downloads the required packages. Existing project data is preserved.
"%ATLAS_SETUP_PY%" %ATLAS_SETUP_ARGS% -m venv "%~dp0.venv-atlas"
if errorlevel 1 goto :failed
"%~dp0.venv-atlas\Scripts\python.exe" -m pip install -e "%~dp0.[dev,anthropic]"
if errorlevel 1 goto :failed
echo.
echo Atlas is ready. Double-click CMS.bat to open this project.
set "RC=0"
goto :finish

:no_python
echo Python 3.11 or newer is required. Install Python, then run Setup-Atlas.bat again.
echo If Python is already installed, set CMS_PYTHON to its full executable path.
set "RC=9009"
goto :finish

:failed
echo.
echo Setup did not finish. Check the error above, then run Setup-Atlas.bat again.
set "RC=1"

:finish
if /i not "%~1"=="--no-pause" pause
exit /b %RC%

:help
echo Usage: Setup-Atlas.bat [--no-pause]
echo Installs Atlas's local runtime and downloads its required packages.
echo Use --no-pause for unattended setup. CMS.bat starts the app after setup.
exit /b 0

:probe
"%~1" %~2 -c "import sys, venv; assert sys.version_info >= (3, 11)" >nul 2>nul
if not errorlevel 1 (
    set "ATLAS_SETUP_PY=%~1"
    set "ATLAS_SETUP_ARGS=%~2"
)
exit /b 0
