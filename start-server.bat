@echo off
title Enternly Server
color 0A
echo ========================================
echo   ENTERNLY - One Click Hire
echo   Starting server on port 8000...
echo ========================================
echo.
cd /d "%~dp0"

:: Activate virtual environment if present
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
) else if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

echo Server: http://localhost:8000  (also accessible via network IP)
echo Press Ctrl+C to stop.
echo.

py -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload
pause
