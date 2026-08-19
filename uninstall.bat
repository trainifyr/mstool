@echo off
:: Batch script wrapper to run the PowerShell uninstaller script in bypass mode
title Keystroke Monitor Uninstaller
cls
echo ==================================================
echo Starting Uninstaller... Please wait.
echo ==================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1"
exit
