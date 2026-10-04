@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
if "%PORT%"=="" set PORT=8766
set "PYVER=3.11.9"

if not exist ".env" copy ".env.example" ".env" >nul

rem --- API key check (before the long package install) ---
set "HASKEY="
for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
    if /i "%%a"=="DEEPSEEK_API_KEY" if not "%%b"=="" set "HASKEY=1"
    if /i "%%a"=="ANTHROPIC_API_KEY" if not "%%b"=="" set "HASKEY=1"
    if /i "%%a"=="OPENAI_API_KEY" if not "%%b"=="" set "HASKEY=1"
    if /i "%%a"=="GEMINI_API_KEY" if not "%%b"=="" set "HASKEY=1"
)
if defined HASKEY goto :start
echo.
echo  ============================================================
echo   No LLM API key in .env
echo.
echo   You can still open the screens, but to request a
echo   district report you need these keys in .env:
echo     an LLM key ^(DeepSeek / Claude / OpenAI / Gemini^),
echo     NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, SEMAS_API_KEY
echo  ============================================================
echo.
choice /c YN /n /m "Open .env now? (Y = open and quit / N = run without keys) "
if errorlevel 2 goto :start
start "" notepad ".env"
echo.
echo  Save .env, close this window, then run run.bat again.
pause
exit /b 0

:start
if exist ".venv\Scripts\python.exe" goto :have_venv

call :find_python
if not defined PY call :install_python
if not defined PY goto :no_python

echo [Sangkwon AI Team] Python: !PY!
echo [Sangkwon AI Team] Creating virtual environment... ^(first run takes a few minutes^)
"!PY!" -m venv .venv || goto :no_python
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :pip_failed
goto :run

:have_venv
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :pip_failed

:run
start "" /b cmd /c "timeout /t 3 /nobreak >nul & start http://localhost:%PORT%"
echo [Sangkwon AI Team] http://localhost:%PORT%   (Ctrl+C to stop)
".venv\Scripts\python.exe" -m uvicorn app.main:app --port %PORT%
goto :eof

rem ------------------------------------------------------------------
rem Find Python 3.10 - 3.12 (skips the Microsoft Store stub "python")
:find_python
set "PY="
for %%v in (311 312 310) do (
    if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python%%v\python.exe"
    if not defined PY if exist "%ProgramFiles%\Python%%v\python.exe" set "PY=%ProgramFiles%\Python%%v\python.exe"
)
if defined PY exit /b 0
for /f "delims=" %%p in ('py -3.11 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%p"
if defined PY exit /b 0
for /f "delims=" %%p in ('python -c "import sys;print(sys.executable if (3,10)<=sys.version_info[:2]<=(3,12) else '')" 2^>nul') do set "PY=%%p"
exit /b 0

rem ------------------------------------------------------------------
rem Install Python %PYVER% for the current user only (no admin rights needed)
:install_python
echo.
echo [Sangkwon AI Team] Python 3.10 - 3.12 not found. Installing Python %PYVER% for this user...
where winget >nul 2>nul
if not errorlevel 1 (
    winget install -e --id Python.Python.3.11 --scope user --silent --accept-package-agreements --accept-source-agreements
    call :find_python
    if defined PY exit /b 0
)
set "PYINST=%TEMP%\python-%PYVER%-amd64.exe"
echo [Sangkwon AI Team] Downloading the installer from python.org ...
curl.exe -L -f -o "%PYINST%" "https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe"
if errorlevel 1 powershell -NoProfile -Command "Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe' -OutFile '%PYINST%'"
if not exist "%PYINST%" exit /b 0
echo [Sangkwon AI Team] Installing... ^(about 1 minute^)
"%PYINST%" /quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0 Include_doc=0
call :find_python
exit /b 0

:no_python
echo [Sangkwon AI Team] Could not find or install Python 3.11.
echo   Install it from https://www.python.org/downloads/ ^(check "Add Python to PATH"^) and run again.
pause
exit /b 1

:pip_failed
echo [Sangkwon AI Team] Package install failed. Delete the .venv folder and run again.
pause
exit /b 1
