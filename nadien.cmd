@echo off
rem Double-click: start NADIEN. From a terminal: nadien start ^| restart ^| stop ^| status ^| reset-password [user] [-Mode auto^|docker^|native]
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-nadien.ps1" %*
set EXITCODE=%ERRORLEVEL%
if "%~1"=="" pause
exit /b %EXITCODE%
