@echo off
setlocal EnableExtensions
title Refund Intelligence - Demo
cd /d "%~dp0"

rem ---- pick Python: project .venv first, else system python ----
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=%CD%\.venv\Scripts\python.exe"
"%PY%" --version >nul 2>&1
if errorlevel 1 (
  echo Python was not found. Install Python 3.10+ and tick "Add to PATH", then run this again.
  pause & exit /b 1
)

rem Demo + tests use the code default (1.0): the seeded stories and the 53 tests are written for it.
rem (0.1 is the recommended production-style setting, but it changes the seeded outcomes, e.g. ORD-1005 goes to manual review.)
set "COST_SCALE=1.0"
set "API_URL=http://127.0.0.1:8000"

:menu
cls
echo ==============================================================
echo    REFUND INTELLIGENCE  -  DEMO LAUNCHER
echo ==============================================================
echo.
echo   1  LIVE DEMO  : start backend + UI and open the browser   (best for showing people)
echo   2  Terminal story : 6 seeded claims through the full pipeline (no browser)
echo   3  Run all tests (about 1-2 min)
echo   4  Open API docs (backend must be running: option 1)
echo   5  Open architecture diagram
echo   6  Install / update requirements (first time on a new PC)
echo   7  STOP the demo servers
echo   0  Exit
echo.
set "C="
set /p C=Choose a number: 
if "%C%"=="1" goto live
if "%C%"=="2" goto story
if "%C%"=="3" goto tests
if "%C%"=="4" goto docs
if "%C%"=="5" goto arch
if "%C%"=="6" goto install
if "%C%"=="7" goto stop
if "%C%"=="0" exit /b 0
goto menu

:live
call :stopquiet
rem fresh demo database every time so the story is always the same
if exist "refund_intel.db" del /q "refund_intel.db" >nul 2>&1
echo Starting backend ...
start "Refund backend (close to stop)" cmd /k ""%PY%" -m uvicorn app:app --port 8000"
echo Waiting for the backend ...
set /a N=0
:waitapi
set /a N+=1
"%PY%" -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/docs',timeout=2)" >nul 2>&1
if not errorlevel 1 goto apiok
if %N% GEQ 40 (
  echo Backend did not start. Choose option 6 to install requirements, then option 1 again. Look at the Refund backend window for the error.
  pause & goto menu
)
timeout /t 1 /nobreak >nul
goto waitapi
:apiok
"%PY%" -c "import streamlit,requests" >nul 2>&1
if errorlevel 1 (
  echo Streamlit is not installed. Choose option 6 first.
  pause & goto menu
)
echo Starting UI ...
start "Refund UI (close to stop)" cmd /k ""%PY%" -m streamlit run streamlit_app.py --server.port 8501 --server.headless true --browser.gatherUsageStats false"
set /a M=0
:waitui
set /a M+=1
"%PY%" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501',timeout=2)" >nul 2>&1
if not errorlevel 1 goto uiok
if %M% GEQ 40 (
  echo UI did not start - look at the "Refund UI" window for the error.
  pause & goto menu
)
timeout /t 1 /nobreak >nul
goto waitui
:uiok
start "" "http://localhost:8501"
echo.
echo   UI       : http://localhost:8501
echo   API docs : http://localhost:8000/docs
echo.
echo   TRY THESE ORDERS in Customer Portal (seeded):  ORD-1001 ... ORD-1006
echo     - normal claim          -> auto-approve
echo     - repeat refunder       -> manual review
echo     - same photo reused     -> manual review (evidence guardrail)
echo     - outside window        -> policy decline
echo   Then open Operations Console to resolve a case, and Management View for insights.
echo.
pause
goto menu

:story
cls
"%PY%" demo.py
echo.
pause
goto menu

:tests
cls
"%PY%" -m pytest -q
echo.
pause
goto menu

:docs
start "" "http://localhost:8000/docs"
goto menu

:arch
if exist "docs\architecture.png" (start "" "docs\architecture.png") else echo docs\architecture.png not found.
pause
goto menu

:install
"%PY%" -m pip install -r requirements.txt
echo.
pause
goto menu

:stop
call :stopquiet
echo Demo servers stopped.
timeout /t 2 /nobreak >nul
goto menu

:stopquiet
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000 " ^| findstr LISTENING') do taskkill /f /pid %%p >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8501 " ^| findstr LISTENING') do taskkill /f /pid %%p >nul 2>&1
exit /b 0
