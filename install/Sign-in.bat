@echo off
REM Signs the app in to the sales mailbox. Run this once, and again only if
REM it ever says the sign-in has expired.

title EDM Zone Quoting - sign in
REM A work PC usually keeps the Desktop and Documents on a server. Command
REM Prompt cannot work inside a network folder at all, so say so plainly
REM rather than let the next line fail with a message nobody can act on.
set "HERE=%~dp0"
if "%HERE:~0,2%"=="\\" (
    echo.
    echo   The app is in a network folder, and it cannot run from there:
    echo     %HERE%
    echo.
    echo   Move the whole folder to  C:\EDMZone\Quoting  and run the setup
    echo   again from its install folder.
    echo.
    pause
    exit /b 1
)

cd /d "%~dp0..\backend"

if not exist ".venv\Scripts\python.exe" (
    echo   The app is not set up yet. Run Setup.ps1 first.
    pause
    exit /b 1
)

.venv\Scripts\python.exe -m scripts.sign_in
pause
