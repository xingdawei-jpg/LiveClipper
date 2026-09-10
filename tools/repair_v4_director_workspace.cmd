@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0repair_v4_director_workspace.ps1" %*
pause
