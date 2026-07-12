@echo off
setlocal
chcp 65001 >nul

set "TARGET=%APPDATA%\LiveClipper"
set "SOURCE=%~dp0payload"

if not exist "%SOURCE%\app\version.json" (
  echo Recovery payload is incomplete.
  pause
  exit /b 1
)

echo Closing LiveClipper...
taskkill /F /T /IM LiveClipperWeb.exe >nul 2>&1
timeout /t 2 /nobreak >nul

if not exist "%TARGET%\app" mkdir "%TARGET%\app"
if not exist "%TARGET%\web_client" mkdir "%TARGET%\web_client"

echo Restoring AI and preview files...
xcopy "%SOURCE%\app\*" "%TARGET%\app\" /E /I /Y >nul
if errorlevel 1 goto :failed
xcopy "%SOURCE%\web_client\*" "%TARGET%\web_client\" /E /I /Y >nul
if errorlevel 1 goto :failed

if exist "%TARGET%\app\__pycache__" rmdir /S /Q "%TARGET%\app\__pycache__"
if exist "%TARGET%\web_client\__pycache__" rmdir /S /Q "%TARGET%\web_client\__pycache__"

echo.
echo Recovery completed. Reopen LiveClipperWeb.exe now.
pause
exit /b 0

:failed
echo.
echo Recovery failed. Close LiveClipper completely and run this file as administrator.
pause
exit /b 1
