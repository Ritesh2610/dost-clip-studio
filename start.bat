@echo off
rem Starts Dost Clip Studio on Windows. The first run installs the video tools
rem (yt-dlp, Deno and ffmpeg) into a private folder (.venv) next to this file.
setlocal
cd /d "%~dp0"
title Dost Clip Studio
set "CODE=1"

set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% --version >nul 2>nul || goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul || goto oldpython

if not exist ".venv\Scripts\python.exe" goto setup
if exist ".venv\.ready" goto run

:setup
echo Setting up the video tools. This happens once, needs internet and can take a few minutes.
%PY% -m venv --clear .venv || goto failed
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto failed
echo ready> ".venv\.ready"

:run
".venv\Scripts\python.exe" -X faulthandler -m clipstudio %*
set "CODE=%ERRORLEVEL%"
echo.
echo Dost Clip Studio has stopped (code %CODE%).
echo If you did not stop it yourself, the reason is in clip-log.txt in this folder.
pause
goto end

:nopython
echo Python 3.11 or newer is needed.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH", then run this again.
pause
goto end

:oldpython
echo Python 3.11 or newer is needed. This computer has an older one:
%PY% --version
echo Install a newer one from https://www.python.org/downloads/ and tick "Add python.exe to PATH", then run this again.
pause
goto end

:failed
echo The video tools could not be installed. Check your internet connection and run this again.
pause

:end
endlocal & exit /b %CODE%
