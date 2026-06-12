@echo off
echo ============================================================
echo  MLB Fantasy Results Tracker
echo  %date% %time%
echo ============================================================
echo.

cd /d "%~dp0"

echo Grading yesterday's games...
python tracker.py
if %errorlevel% neq 0 (
    echo ERROR: tracker.py failed.
    exit /b 1
)

echo.
echo Done.
