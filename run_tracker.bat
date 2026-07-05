@echo off
echo ============================================================
echo  MLB Fantasy Results Tracker
echo  %date% %time%
echo ============================================================
echo.

cd /d "%~dp0"
set MLB_HEADLESS=1

echo Grading yesterday's games...
python tracker.py
if %errorlevel% neq 0 (
    echo WARNING: tracker.py failed, retrying in 60s...
    timeout /t 60 /nobreak >nul
    python tracker.py
    if %errorlevel% neq 0 (
        echo ERROR: tracker.py failed twice. Check pipeline.log for details.
        python write_status.py tracker FAILED tracker.py
        exit /b 1
    )
)

python write_status.py tracker OK
echo.
echo Done.
