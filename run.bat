@echo off
rem 一键启动 study tools（Windows）
cd /d %~dp0
python app\main.py
rem 双击启动时依赖缺失/报错不能让黑窗口一闪而过——pause 让用户看到原因。
rem 注意：本文件必须是 CRLF 行尾——cmd 按 GBK 解析批处理，UTF-8 中文 + LF 会
rem 吞掉下一行首字符（曾把 python 吃成 ython、pause 吃成 ause，窗口闪退）。
pause
