@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0repair_liveclipper_update.ps1"
set "RESULT=%ERRORLEVEL%"
echo.
if "%RESULT%"=="0" (
  echo Recovery completed and verified. Reopen LiveClipper now.
) else (
  echo Recovery failed. See repair_result.txt for details.
)
pause
exit /b %RESULT%
