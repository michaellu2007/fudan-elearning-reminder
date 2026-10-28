# -*- coding: utf-8 -*-
"""一键安装：环境 + 配置 + 开机自启 + 启动守护进程（全程隐藏窗口）。

用法：
    python install.py            # 安装并启动
    python install.py --stop     # 停止守护进程
    python install.py --uninstall  # 停止并移除开机自启
"""
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
STOP = BASE / "daemon.stop"
PID = BASE / "daemon.pid"
STARTUP = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
VBS = STARTUP / "homework_reminder_daemon.vbs"
PYTHONW = Path(sys.executable).with_name("pythonw.exe")

VBS_CONTENT = (
    "' homework reminder daemon (hidden window)\n"
    f'CreateObject("WScript.Shell").Run """{PYTHONW}"" ""{BASE / "daemon.pyw"}""", 0, False\n'
)


def sh(cmd):
    print(">", " ".join(cmd))
    subprocess.run(cmd, check=True)


def install():
    if not (BASE / "config.json").exists():
        import shutil
        shutil.copy(BASE / "config.example.json", BASE / "config.json")
        print("已生成 config.json，请先填写 UIS 账号和 SMTP 授权码，然后重新运行本脚本。")
        print("文件位置：", BASE / "config.json")
        return
    sh([sys.executable, "-m", "pip", "install", "playwright"])
    sh([sys.executable, "-m", "playwright", "install", "chromium"])
    VBS.write_text(VBS_CONTENT, encoding="utf-8")
    print("开机自启已注册：", VBS)
    (BASE / "browser_state.json").unlink(missing_ok=True)  # 旧会话作废，强制新一轮登录
    stop_file = BASE / "daemon.stop"
    if stop_file.exists():
        stop_file.unlink()
    if not PYTHONW.exists():
        print("警告：未找到 pythonw.exe，守护进程将带窗口运行。")
        launcher = sys.executable
    else:
        launcher = str(PYTHONW)
    subprocess.Popen([launcher, str(BASE / "daemon.pyw")], cwd=str(BASE))
    print("守护进程已启动（隐藏窗口，无控制台）。日志：", BASE / "reminder.log")


def stop():
    STOP.write_text("stop\n", encoding="utf-8")
    print("已创建 daemon.stop，守护进程将在 1 分钟内退出。删除该文件即可恢复运行。")


def uninstall():
    stop()
    VBS.unlink(missing_ok=True)
    print("开机自启已移除。")


if __name__ == "__main__":
    if "--uninstall" in sys.argv:
        uninstall()
    elif "--stop" in sys.argv:
        stop()
    else:
        install()
