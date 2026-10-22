@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\Install-Translator.ps1" %*
if errorlevel 1 echo Operation failed. See the message above.
pause
