@echo off
rem Double-click to start the chat. Keys come from .env (if filled in) or your saved Windows variables.
cd /d "%~dp0"
if exist .env for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do if not "%%b"=="" set "%%a=%%b"
if "%ANTHROPIC_API_KEY%"=="" (
    echo ANTHROPIC_API_KEY is missing. Open .env in Notepad, paste your keys after the = signs, save, and run this again.
    pause
    exit /b 1
)
start "" /b cmd /c "timeout /t 5 >nul & start http://localhost:8501"
.venv\Scripts\streamlit.exe run chat.py --server.headless true --browser.gatherUsageStats false
pause
