"""创建/更新桌面快捷方式 studytool（指向 run.bat，最小化启动，用 assets/studytool.ico）。

用法：python assets/setup_desktop.py
项目移动位置或换机器后重跑一次即可。仅 Windows；macOS/Linux 请用 run.sh。
"""
import os
import subprocess
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PS_TEMPLATE = r"""
$desktop = [Environment]::GetFolderPath('Desktop')
$proj = '{proj}'
$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut("$desktop\studytool.lnk")
$lnk.TargetPath = "$proj\run.bat"
$lnk.WorkingDirectory = $proj
$lnk.IconLocation = "$proj\assets\studytool.ico,0"
$lnk.WindowStyle = 7
$lnk.Description = 'study tools · 个人学习管理'
$lnk.Save()
Write-Output "OK: $desktop\studytool.lnk"
"""

if sys.platform != "win32":
    sys.exit("此脚本仅用于 Windows（macOS/Linux 请用 run.sh）")

ps = PS_TEMPLATE.format(proj=PROJ)
r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
print((r.stdout or r.stderr or "").strip())
