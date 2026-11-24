@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m portfolio_bot >> "run.log" 2>&1
