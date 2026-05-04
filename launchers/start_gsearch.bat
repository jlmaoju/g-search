@echo off
setlocal
cd /d "%~dp0\.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_gsearch.ps1" %*
exit /b %ERRORLEVEL%
