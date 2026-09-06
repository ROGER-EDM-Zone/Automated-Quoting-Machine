@echo off
REM Starts the quoting app and opens it in the browser.
REM Leave this window open — closing it stops the app.

title EDM Zone Quoting
cd /d "%~dp0..\backend"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   The app is not set up yet.
    echo   Right-click Setup.ps1 in the install folder and choose
    echo   "Run with PowerShell", then try again.
    echo.
    pause
    exit /b 1
)

echo.
echo   Starting the quoting app...
echo   Leave this window open. Closing it stops the app.
echo.

REM Give the server a moment, then open the browser.
start "" /b cmd /c "timeout /t 4 >nul & start http://localhost:8000"

.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

echo.
echo   The app has stopped.
pause
