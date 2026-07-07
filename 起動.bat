@echo off
cd /d "%~dp0"
start http://localhost:5002
python app.py
