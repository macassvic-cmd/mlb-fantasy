=====================================================================
  MLB FANTASY BASEBALL DATA PIPELINE — BEGINNER GUIDE
=====================================================================

WHAT THIS DOES
--------------
Every day before MLB games start, run this pipeline and it will:
  1. Pull today's confirmed lineups from the MLB Stats API
  2. Cross-check them with RotoWire
  3. Grab each batter's stats from Baseball Savant (Statcast) and FanGraphs
  4. Get weather forecasts for each stadium
  5. Save everything to a JSON file in the "data" folder
  6. Print a ranked leaderboard for both Underdog and PrizePicks

FIRST-TIME SETUP
----------------
1. Make sure Python 3.9+ is installed.
   Download from https://www.python.org/downloads/ if needed.

2. Open a Command Prompt in this folder (Shift+Right-click → Open PowerShell/CMD here)

3. Install all required packages by running:
      pip install -r requirements.txt

4. (Optional but recommended) Get a free OpenWeatherMap API key:
   - Go to https://openweathermap.org/api and sign up for free
   - Copy your API key
   - Open the ".env" file in this folder with Notepad
   - Replace "your_key_here" with your actual key
   - Save the file
   Without this key, weather data will show as N/A (everything else still works).

HOW TO RUN
----------
Easiest: double-click "run_daily.bat"
  This runs the pipeline AND prints the leaderboard automatically.

Or run manually in Command Prompt:

  Step 1 — Download today's data:
    python pipeline.py

  Step 2 — See the rankings:
    python projections.py

Other useful commands:
  # Run for a specific past date
  python pipeline.py --date 2025-06-01

  # Backfill the last 7 days (useful for tracking history)
  python pipeline.py --backfill 7

  # See rankings for a specific date
  python projections.py 2025-06-01

WHEN TO RUN
-----------
Run the pipeline about 2-3 hours before first pitch.
MLB lineups are usually posted by noon ET on game days.
If you run too early, you'll see a message: "lineups not yet posted."

WHERE IS MY DATA?
-----------------
All daily data is saved in the "data" folder as JSON files.
Example: data/2025-06-05.json
You can open these in any text editor to see the raw numbers.

UNDERSTANDING THE LEADERBOARD
------------------------------
Two leaderboards are printed: one for Underdog, one for PrizePicks.

Columns explained:
  Score    — Composite ranking score (higher = better play)
  7d/g     — Average fantasy points per game over last 7 days
  14d/g    — Average fantasy points per game over last 14 days
  30d/g    — Average fantasy points per game over last 30 days
  OppERA   — Today's opposing pitcher's ERA (lower = tougher matchup)
  wOBA     — Weighted On-Base Average (season, from FanGraphs)
  xwOBA    — Expected wOBA based on Statcast exit velocity data
  Park     — HR park factor (1.20 = 20% more HRs than average, like Coors Field)
  Weather  — Temperature and wind (outdoor parks only)

SCORING SYSTEMS
---------------
Underdog:    1B=+3  2B=+6  3B=+8  HR=+10  BB=+3  HBP=+3  RBI=+2  R=+2  SB=+4
PrizePicks:  1B=+3  2B=+5  3B=+8  HR=+10  BB=+2  HBP=+2  RBI=+2  R=+2  SB=+5

TROUBLESHOOTING
---------------
"No games found for this date"
  → Either there are no MLB games today, or you ran it too early (before schedules post).

"Lineups not yet posted"
  → Re-run the pipeline closer to game time (noon-3pm ET on game days).

"pybaseball errors"
  → Statcast data can occasionally time out. The pipeline will skip that player
    and continue. Re-run later if you want the missing data.

"pip install fails"
  → Make sure Python was installed with "Add Python to PATH" checked.
    Try: python -m pip install -r requirements.txt

FILES IN THIS FOLDER
--------------------
  pipeline.py       — Main data collector (run this first)
  projections.py    — Leaderboard printer (run this after pipeline)
  scrapers/         — Individual data source modules
    mlb_api.py      — MLB Stats API (lineups, stats, pitcher data)
    statcast.py     — Statcast data via pybaseball
    fangraphs.py    — FanGraphs data via pybaseball
    weather.py      — OpenWeatherMap weather forecast
    lineups.py      — RotoWire lineup confirmation scraper
  requirements.txt  — Python packages needed
  .env              — Your API key goes here
  run_daily.bat     — Double-click to run everything at once
  data/             — Output folder (JSON files saved here)
  pipeline.log      — Log file if something goes wrong

=====================================================================
