@echo off
rem learn-coach study dashboard launcher (read-only, local only)
cd /d %~dp0
where python >nul 2>nul
if %errorlevel%==0 (
  python dashboard\server.py
  goto :end
)
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 dashboard\server.py
  goto :end
)
echo [ERROR] Python 3 not found. Install it from https://www.python.org/downloads/ first.
pause
:end
