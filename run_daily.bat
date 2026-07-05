@echo off
echo ============================================================
echo  MLB Fantasy Baseball - Noon Pipeline (Confirmed Lineups)
echo  %date% %time%
echo ============================================================
echo.

cd /d "%~dp0"
set MLB_HEADLESS=1

echo [1/3] Running data pipeline...
python pipeline.py
if %errorlevel% neq 0 (
    echo WARNING: pipeline.py failed, retrying in 60s...
    timeout /t 60 /nobreak >nul
    python pipeline.py
    if %errorlevel% neq 0 (
        echo ERROR: pipeline.py failed twice. Check pipeline.log for details.
        python write_status.py noon FAILED pipeline.py
        exit /b 1
    )
)

echo.
echo [2/3] Generating projections leaderboard...
python projections.py
if %errorlevel% neq 0 (
    echo WARNING: projections.py failed, retrying in 60s...
    timeout /t 60 /nobreak >nul
    python projections.py
    if %errorlevel% neq 0 (
        echo ERROR: projections.py failed twice.
        python write_status.py noon FAILED projections.py
        exit /b 1
    )
)

echo.
echo [3/3] Building Excel report and dashboard...
python report.py
if %errorlevel% neq 0 (
    echo WARNING: report.py failed, retrying in 60s...
    timeout /t 60 /nobreak >nul
    python report.py
    if %errorlevel% neq 0 (
        echo ERROR: report.py failed twice.
        python write_status.py noon FAILED report.py
        exit /b 1
    )
)

python write_status.py noon OK
echo.
echo Done. Dashboard pushed to GitHub Pages.
echo ============================================================
