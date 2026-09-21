"""创建/更新桌面快捷方式 studytool（指向 run.bat，最小化启动，用 assets/studytool.ico）。

用法：python assets/setup_desktop.py
项目移动位置或换机器后重跑一次即可。仅 Windows；macOS/Linux 请用 run.sh。

路径经环境变量传给 PowerShell（不做任何字符串插值）——项目路径含撇号
（如用户名 O'Brien）或其它特殊字符时既不会解析失败，也不存在注入面。
"""
import os
import subprocess
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# $env:ST_PROJ 由 subprocess 的 env 注入，PowerShell 侧只读取、不拼接
PS_SCRIPT = r"""
$desktop = [Environment]::GetFolderPath('Desktop')
$proj = $env:ST_PROJ
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

r = subprocess.run(
    ["powershell", "-NoProfile", "-Command", PS_SCRIPT],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
    env={**os.environ, "ST_PROJ": PROJ},
)
print((r.stdout or r.stderr or "").strip())
