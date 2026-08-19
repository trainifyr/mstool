@echo off
:: Batch script wrapper to run the PowerShell installation script in bypass mode
title Keystroke Monitor Setup
cls
echo ==================================================
echo Starting Installer... Please wait.
echo ==================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
exit
