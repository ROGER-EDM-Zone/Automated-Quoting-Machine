@echo off
REM ===================================================================
REM  Double-click this file to set up the quoting app.
REM
REM  This exists so you do not have to fight Windows. Windows blocks
REM  PowerShell scripts that came from the internet; a .bat file is not
REM  blocked, so this one starts the setup for you with that block
REM  lifted for this one script only. Nothing about your PC is changed.
REM ===================================================================

title EDM Zone Quoting - setup
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Setup.ps1"

if errorlevel 1 (
    echo.
    echo   Setup did not finish. The message above says why.
    echo.
    pause
)
