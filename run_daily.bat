@echo off
echo ============================================================
echo  MLB Fantasy Baseball Daily Pipeline
echo  %date% %time%
echo ============================================================
echo.

cd /d "%~dp0"

echo [1/2] Running data pipeline...
python pipeline.py
if %errorlevel% neq 0 (
    echo ERROR: pipeline.py failed. Check pipeline.log for details.
    pause
    exit /b 1
)

echo.
echo [2/3] Generating projections leaderboard...
python projections.py
if %errorlevel% neq 0 (
    echo ERROR: projections.py failed.
    pause
    exit /b 1
)

echo.
echo [3/3] Building Excel report and dashboard...
python report.py
if %errorlevel% neq 0 (
    echo ERROR: report.py failed.
    pause
    exit /b 1
)

echo.
echo Done! Data saved to the data\ folder.
echo ============================================================
pause
