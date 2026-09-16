@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "ramb.exe" (
    ramb.exe
) else (
    python "ramb.py"
)
if errorlevel 1 pause
