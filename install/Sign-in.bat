@echo off
REM Signs the app in to the sales mailbox. Run this once, and again only if
REM it ever says the sign-in has expired.

title EDM Zone Quoting - sign in
cd /d "%~dp0..\backend"

if not exist ".venv\Scripts\python.exe" (
    echo   The app is not set up yet. Run Setup.ps1 first.
    pause
    exit /b 1
)

.venv\Scripts\python.exe -m scripts.sign_in
pause
