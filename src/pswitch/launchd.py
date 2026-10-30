"""macOS launchd 开机自启支持。"""

import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import LOG_PATH

LABEL = f"com.{getpass.getuser()}.pswitch"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def installed():
    return sys.platform == "darwin" and PLIST.exists()


def launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def program_args():
    """优先使用 uv/pip 安装的 console script，其次退回 python -m。"""
    exe = shutil.which("pswitch")
    if exe:
        return [exe, "serve"]
    return [sys.executable, "-m", "pswitch", "serve"]


def fd_soft_limit():
    """与守护进程启动时一致：fd 硬上限的一半。"""
    from .proxy import desired_fd_limit
    return desired_fd_limit() or 46080


def plist_text():
    args = "".join(f"\n    <string>{a}</string>" for a in program_args())
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>{args}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>SoftResourceLimits</key>
  <dict>
    <key>NumberOfFiles</key><integer>{fd_soft_limit()}</integer>
  </dict>
  <key>StandardOutPath</key><string>{LOG_PATH}</string>
  <key>StandardErrorPath</key><string>{LOG_PATH}</string>
</dict>
</plist>
"""


def bootstrap():
    return launchctl("bootstrap", f"gui/{os.getuid()}", str(PLIST))


def bootout():
    return launchctl("bootout", f"gui/{os.getuid()}", str(PLIST))


def write_plist():
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(plist_text(), encoding="utf-8")


def remove_plist():
    try:
        PLIST.unlink()
    except OSError:
        pass
