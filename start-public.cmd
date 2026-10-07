@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File "%~dp0Start-Panel.ps1" -Public
if errorlevel 1 pause
endlocal
