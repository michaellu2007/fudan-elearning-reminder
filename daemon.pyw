# -*- coding: utf-8 -*-
"""保活守护进程：每 15 分钟调用一次 browser_fetch.py，直到出现 daemon.stop。

- 用 pythonw 运行则全程无窗口；
- daemon.stop 文件出现即退出（删除本文件旁的 daemon.stop 可恢复）；
- daemon.pid 防止多实例；
- 抓取子进程超时 10 分钟自动放弃，等待下一轮。

由 Startup 文件夹里的 vbs 在登录时静默启动。
"""
import os
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
STOP = BASE / "daemon.stop"
LOCK = BASE / "daemon.pid"
SCRIPT = BASE / "browser_fetch.py"
INTERVAL_SEC = 15 * 60
CHILD_TIMEOUT = 600


def already_running():
    if not LOCK.exists():
        return False
    pid = LOCK.read_text(encoding="utf-8").strip()
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True, text=True, timeout=30,
        ).stdout
        return pid in out
    except Exception:
        return False


def main():
    if already_running():
        return
    LOCK.write_text(str(os.getpid()), encoding="utf-8")
    try:
        while not STOP.exists():
            try:
                subprocess.run(
                    [sys.executable, str(SCRIPT)],
                    timeout=CHILD_TIMEOUT, cwd=str(BASE),
                )
            except Exception:
                pass  # browser_fetch 自己会记日志/发告警；守护循环永不退出
            for _ in range(INTERVAL_SEC):
                if STOP.exists():
                    break
                time.sleep(1)
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
