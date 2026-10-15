@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\telemetry.ps1" %*
exit /b %ERRORLEVEL%
