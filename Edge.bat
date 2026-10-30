@echo off
REM === Edge v2.0 - double-click launcher ===
REM Starts the Streamlit server and opens the dashboard in your default browser.

cd /d "%~dp0"

REM Ensure pip packages are installed on first run.
python -m pip install -q -r requirements.txt

REM Launch Streamlit in a visible terminal window so the user can see server logs
REM and close it with Ctrl+C.
start "Edge v2.0 server" cmd /k python -m streamlit run app.py --server.port=8601 --server.headless=true --browser.gatherUsageStats=false

REM Wait a moment for the server to come up, then open the browser.
timeout /t 4 /nobreak >nul
start "" "http://localhost:8601"
exit /b 0
