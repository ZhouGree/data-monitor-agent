@echo off
rem D18 daily monitor entry: run manually or register in Task Scheduler
rem output appends to monitor.log, daily report saves to reports\
cd /d %~dp0
echo ===== %date% %time% monitor start ===== >> monitor.log
"D:\7python\Fir\venv\Scripts\python.exe" analyze.py >> monitor.log 2>&1
echo ===== %date% %time% monitor end ===== >> monitor.log
