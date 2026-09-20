@echo off
rem 一键启动 study tools（Windows）
cd /d %~dp0
python app\main.py
rem 双击启动时依赖缺失/报错不能让黑窗口一闪而过——pause 让用户看到原因
pause
