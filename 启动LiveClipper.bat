@echo off
chcp 65001 >nul
title LiveClipper Web
cd /d "%~dp0"
echo 正在启动 LiveClipper（Web 版）...
"C:\Program Files\Python312\python.exe" web_client\desktop.py
if errorlevel 1 (
  echo.
  echo 启动失败。请确认已安装 Python 3.12 及依赖，或改用「LiveClipperWeb.exe」。
  pause
)