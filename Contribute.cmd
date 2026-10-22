@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\Open-Workshop.ps1" %*
if errorlevel 1 pause
