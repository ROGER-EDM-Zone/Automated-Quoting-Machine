@echo off
REM Checks the mailbox connection and says exactly what is wrong if anything
REM is. Safe to run at any time — it only reads.

title EDM Zone Quoting - connection check
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

.venv\Scripts\python.exe -m scripts.check_graph
echo.
echo   Send anything above in red to Claude if it does not make sense.
pause
