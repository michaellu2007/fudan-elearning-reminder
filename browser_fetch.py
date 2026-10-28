# -*- coding: utf-8 -*-
"""独立浏览器抓取脚本：零大模型参与，可直接交给 Windows 计划任务运行。

流程：
1. 用保存的登录状态(browser_state.json)打开 eLearning；会话失效时自动用
   本地凭据文件(C:\\Users\\Administrator\\.homework_reminder_uis.json)重登 UIS；
2. 页内 fetch 拉取 todo/课程/作业原始数据 -> _browser_raw.json；
3. 调 check_and_remind.check(raw=...) 完成全部提醒逻辑（邮件策略见该脚本）；
4. 摘要追加到 reminder.log；登录/抓取失败时发告警邮件（每天最多一封）。

安全：凭据文件在工作区之外，绝不写入日志或邮件。
"""
import contextlib
import io
import json
import sys
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import check_and_remind as c  # noqa: E402

CREDS = Path(r"C:\Users\Administrator\.homework_reminder_uis.json")  # 旧版外置凭据，兼容保留
STATE = BASE / "browser_state.json"
RAW = BASE / "_browser_raw.json"
LOG = BASE / "reminder.log"
DASH = "https://elearning.fudan.edu.cn/dash"
LOGIN = "https://elearning.fudan.edu.cn/login"


def load_creds():
    """UIS 账号密码：优先 config.json 的 uis 字段，其次旧版外置凭据文件。"""
    uis = c.CONFIG.get("uis") or {}
    if uis.get("user") and uis.get("password"):
        return uis["user"], uis["password"]
    data = json.loads(CREDS.read_text(encoding="utf-8"))
    return data["user"], data["password"]

FETCH_JS = """
async () => {
  const get = async (path) => {
    const r = await fetch(path, { headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error(path + " -> HTTP " + r.status);
    return r.json();
  };
  const out = { todo: [], courses: [], course_assignments: {} };
  let page = 1;
  while (true) {
    const d = await get(`/api/v1/users/self/todo?per_page=50&page=${page}&include%5B%5D=unsubmitted_quizzes`);
    if (!Array.isArray(d)) throw new Error("todo 返回异常");
    out.todo.push(...d);
    if (d.length < 50) break;
    page++;
  }
  out.courses = await get("/api/v1/courses?enrollment_state=active&per_page=100");
  for (const c of out.courses) {
    let data = [], p = 1;
    while (true) {
      const d = await get(`/api/v1/courses/${c.id}/assignments?per_page=100&page=${p}`);
      if (!Array.isArray(d)) break;
      data.push(...d);
      if (d.length < 100) break;
      p++;
    }
    out.course_assignments[String(c.id)] = data;
  }
  return out;
}
"""


def log(msg):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def do_login(page, user, pwd):
    """从任意状态走到 UIS 登录并回到 eLearning 控制面板。"""
    page.goto(LOGIN, wait_until="domcontentloaded", timeout=40000)
    page.wait_for_timeout(2500)
    if "id.fudan.edu.cn" not in page.url:  # eLearning 登录页，点登录跳 UIS
        page.get_by_role("link", name="登录").first.click()
        page.wait_for_url("**id.fudan.edu.cn**", timeout=40000)
    page.wait_for_timeout(2000)
    page.get_by_placeholder("用户名").fill(user)
    page.get_by_placeholder("密码").fill(pwd)
    page.get_by_role("button", name="登录").click()
    page.wait_for_url("**elearning.fudan.edu.cn/dash**", timeout=40000)
    page.wait_for_timeout(2000)


def main():
    user, pwd = load_creds()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            storage_state=str(STATE) if STATE.exists() else None,
            locale="zh-CN",
        )
        page = ctx.new_page()
        relogged = False
        try:
            page.goto(DASH, wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(3000)
            if "id.fudan.edu.cn" in page.url or "/login" in page.url:
                do_login(page, user, pwd)
                relogged = True
            raw = page.evaluate(FETCH_JS)
            RAW.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
            ctx.storage_state(path=str(STATE))
        finally:
            browser.close()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        c.check(raw=json.loads(RAW.read_text(encoding="utf-8")))
    summary = buf.getvalue().strip()
    log(summary)
    if sys.stdout:  # pythonw 下 stdout 为 None
        print(summary)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        log(f"FAILED {c.now():%Y-%m-%d %H:%M} {err}")
        if sys.stderr:
            traceback.print_exc()
        try:
            c.send_auth_alert("浏览器会话抓取失败（自动登录未成功）：" + err[:300])
        except Exception:
            pass
        sys.exit(1)
