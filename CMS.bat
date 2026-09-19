@echo off
REM Start Atlas's local server, not cms\ui_assets\index.html.
setlocal
set "CMS_PY="
set "CMS_PY_ARGS="
set "ATLAS_VENV_PY=%~dp0.venv-atlas\Scripts\python.exe"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
REM -P plus this source path selects this checkout while preserving the caller's
REM directory, including the meaning of relative --root arguments.
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
if defined CMS_PYTHON call :probe "%CMS_PYTHON%" ""
if not defined CMS_PY if exist "%ATLAS_VENV_PY%" call :probe "%ATLAS_VENV_PY%" ""
if not defined CMS_PY if exist "%VENV_PY%" call :probe "%VENV_PY%" ""
if not defined CMS_PY call :probe "py" "-3"
if not defined CMS_PY call :probe "python" ""
if not defined CMS_PY goto :no_runtime
if "%~1"=="" goto :launch_app
"%CMS_PY%" %CMS_PY_ARGS% -P -m cms.cli %*
set "RC=%ERRORLEVEL%"
goto :finish

:launch_app
"%CMS_PY%" %CMS_PY_ARGS% -P -m cms.cli app --root "%~dp0."
set "RC=%ERRORLEVEL%"
goto :finish

:no_runtime
echo Atlas could not find a working Python runtime with its dependencies.
echo Double-click Setup-Atlas.bat in this folder to install the local runtime.
echo Setup needs Python 3.11 or newer and internet access to install packages.
echo You can also set CMS_PYTHON to a working Python executable with Atlas installed.
set "RC=9009"

:finish
REM Keep double-click failures readable. Commands with arguments never pause.
if "%~1"=="" if not "%RC%"=="0" pause
exit /b %RC%

:probe
"%~1" %~2 -P -c "import sys; assert sys.version_info >= (3, 11); import cms.cli" >nul 2>nul
if not errorlevel 1 (
    set "CMS_PY=%~1"
    set "CMS_PY_ARGS=%~2"
)
exit /b 0
