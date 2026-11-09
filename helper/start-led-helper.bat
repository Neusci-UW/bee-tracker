@echo off
rem Starts the Bee Tracker LED helper. Keep this window open while you use the page.
rem Add options after the file name, e.g.  --device Dev2  or  --code 123456  or  --simulated
cd /d "%~dp0"
py led_helper.py %*
if errorlevel 1 pause
