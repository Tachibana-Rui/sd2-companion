@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0dist\sd2-companion\SD2-Companion.exe" (
  start "" "%~dp0dist\sd2-companion\SD2-Companion.exe" desktop --config "%~dp0private-reports\companion.json"
  goto finish
)
where pythonw >nul 2>nul
if errorlevel 1 goto console
start "" pythonw -B "%~dp0player_companion.py" desktop
goto finish

:console
python -B "%~dp0player_companion.py" desktop
if errorlevel 1 pause

:finish
endlocal
