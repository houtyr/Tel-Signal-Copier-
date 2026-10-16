@echo off
cd /d "%~dp0"
:: Prefer the py launcher; fall back to python on PATH
where py >nul 2>&1 && set PYTHON=py || set PYTHON=python
if not exist ".venv" (
  echo First run - setting up...
  %PYTHON% -m venv .venv || (echo Python 3.11+ not found. Install from python.org and tick "Add to PATH". & pause & exit /b)
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\pip install -r requirements.txt || (pause & exit /b)
)
.venv\Scripts\python app.py
pause
