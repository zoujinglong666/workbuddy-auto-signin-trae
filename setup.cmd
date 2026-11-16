@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-windows.ps1" %*
echo.
echo ============================================================
echo  Done. You can close this window.
echo  Re-run this file any time to check / install / uninstall.
echo ============================================================
pause >nul
