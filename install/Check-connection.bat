@echo off
REM Checks the mailbox connection and says exactly what is wrong if anything
REM is. Safe to run at any time — it only reads.

title EDM Zone Quoting - connection check
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
