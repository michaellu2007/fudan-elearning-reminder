# -*- coding: utf-8 -*-
"""复旦 eLearning 作业提醒：检查未完成作业，按策略发送邮件。

数据来源（两种模式）：
1. API 令牌模式（默认）：用 config.json 里的访问令牌调 Canvas API；
   - todo 接口：需要网上提交且未提交的作业（已提交自动消失）；
   - 逐门课程拉取已发布作业：捕获不需要网上提交的作业
     （如概率论：网上发布、纸质/线下提交，Canvas 上永远不会显示"已提交"）。
2. JSON 模式（--from-json 文件）：读取已抓好的原始 API 数据再走提醒逻辑，
   供浏览器会话等无法长期持令牌的场景使用。

提醒策略（按 Canvas 截止时间）：
- 需网上提交的作业：
  · 距截止 3 天以内（且超过 5 小时）：每天最多提醒一次；
  · 距截止 5 小时以内：每次运行都提醒（配合 15 分钟一次的定时任务，约 15 分钟一封）；
  · 已过截止仍未提交：并入每日提醒，标注"已过截止"。
- 无需网上提交、已设截止时间的作业（无法检测是否完成，避免误轰炸）：
  · 距截止 3 天以内：每天最多提醒一次；
  · 进入最后 5 小时：只发一封"最后提醒"；
  · 过截止后不再提醒。
- 无需网上提交、暂未设截止时间的作业：发布后发一封"新作业"通知（仅一次），
  待老师设置截止时间后转入常规提醒。
- Canvas 认证失败（令牌失效等）：走 SMTP 发系统故障告警，每天最多一封。

用法：
    python check_and_remind.py                    # API 令牌模式，正常检查
    python check_and_remind.py --from-json d.json # 用已抓取的 JSON 数据检查
    python check_and_remind.py --test-email       # 只发一封测试邮件，验证 SMTP 配置

不再提醒某个作业：编辑同目录 mute.txt（格式见文件内注释），或直接让助手处理。
"""
import json
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
STATE_FILE = BASE / "sent_log.json"
MUTE_FILE = BASE / "mute.txt"  # 静默名单，见文件内注释
# 中国没有夏令时，固定 UTC+8 即可，避免 Windows 上 zoneinfo 缺 tzdata 的问题
CST = timezone(timedelta(hours=8))
# 有这些提交方式的作业需要网上提交，"是否已提交"以 Canvas 为准
ONLINE_TYPES = {
    "online_text_entry", "online_upload", "online_url", "online_quiz",
    "external_tool", "media_recording", "student_annotation",
}


class AuthError(RuntimeError):
    """Canvas 认证失败（令牌失效等），需要用户更换凭据。"""


def now():
    return datetime.now(CST)


def parse_due(iso):
    # Canvas 返回 "2026-11-01T13:00:00Z"（可能带毫秒）
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(CST)


# ---------- 数据获取：Canvas API ----------

def api_get(path, params=None):
    token = CONFIG["canvas_token"]
    if not token:
        raise AuthError("config.json 里的 canvas_token 还没有填写")
    url = CONFIG["canvas_base_url"].rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError(f"Canvas 认证失败(HTTP {e.code})，访问令牌可能已失效") from e
        raise


def fetch_raw():
    """拉取原始数据：todo + 活跃课程 + 每门课的已发布作业。"""
    raw = {"todo": [], "courses": [], "course_assignments": {}}

    page = 1
    while True:
        data = api_get(
            "/api/v1/users/self/todo",
            {"page": page, "per_page": 50, "include[]": "unsubmitted_quizzes"},
        )
        if not isinstance(data, list):
            raise RuntimeError("todo 接口返回异常: " + str(data)[:300])
        raw["todo"].extend(data)
        if len(data) < 50:
            break
        page += 1

    try:
        raw["courses"] = api_get(
            "/api/v1/courses", {"enrollment_state": "active", "per_page": 100}
        )
    except Exception as e:
        print("拉取课程列表失败，本轮跳过线下作业:", e)

    for c in raw["courses"]:
        cid = c.get("id")
        data, page = [], 1
        while True:
            try:
                page_data = api_get(
                    f"/api/v1/courses/{cid}/assignments",
                    {"page": page, "per_page": 100},
                )
            except Exception as e:
                print(f"拉取课程 {c.get('name') or cid} 作业失败，跳过:", e)
                break
            data.extend(page_data)
            if len(page_data) < 100:
                break
            page += 1
        raw["course_assignments"][str(cid)] = data
    return raw


# ---------- 数据解析：纯函数，API 与 JSON 两种模式共用 ----------

def make_item(assignment, course, needs_online):
    return {
        "id": assignment.get("id"),
        "name": assignment.get("name", "(未命名作业)"),
        "course": course,
        "due": parse_due(assignment["due_at"]) if assignment.get("due_at") else None,
        "html_url": assignment.get("html_url", ""),
        "needs_online": needs_online,
    }


def parse_todo(raw):
    """todo 只含未提交项；没有截止时间的不在提醒范围。"""
    result = []
    for it in raw["todo"]:
        a = it.get("assignment") or {}
        if not a.get("due_at"):
            continue
        if it.get("needs_submitting") is False:
            continue
        course = it.get("context_name") or it.get("course_id", "")
        result.append(make_item(a, course, needs_online=True))
    return result


def parse_offline(raw):
    """不需要网上提交、已发布的作业（含暂无截止时间的）。"""
    merged = {}
    for c in raw["courses"]:
        cid, cname = c.get("id"), c.get("name") or c.get("id", "")
        for a in raw["course_assignments"].get(str(cid), []):
            stypes = set(a.get("submission_types") or [])
            if stypes & ONLINE_TYPES:
                continue  # 需网上提交的作业由 todo 负责（含已提交检测）
            if "not_graded" in stypes or not a.get("published", True):
                continue
            if a.get("due_at") and parse_due(a["due_at"]) < now():
                continue  # 线下作业过截止后不再提醒
            merged[a["id"]] = make_item(a, cname, needs_online=False)
    return list(merged.values())


def parse_raw(raw):
    by_id = {}
    for a in parse_todo(raw) + parse_offline(raw):
        by_id.setdefault(a["id"], a)  # todo 优先（含提交状态）
    return list(by_id.values())


def fetch_all():
    return parse_raw(fetch_raw())


def load_raw(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ---------- 发邮件 / 状态 / 静默名单 ----------

def send_email(subject, body):
    smtp = CONFIG["smtp"]
    msg = EmailMessage()
    msg["From"] = formataddr(("作业提醒", smtp["user"]))
    msg["To"] = CONFIG["recipient"]
    msg["Subject"] = subject
    msg.set_content(body)
    last_err = None
    for attempt in range(3):  # 163 偶尔限流断连，重试 3 次
        try:
            with smtplib.SMTP_SSL(smtp["host"], smtp.get("port", 465), timeout=30) as s:
                s.login(smtp["user"], smtp["auth_code"])
                s.send_message(msg)
            return
        except (smtplib.SMTPException, ConnectionError, OSError) as e:
            last_err = e
            time.sleep(5 * (attempt + 1))
    raise last_err


def load_mutes():
    """每行一条规则：行内空格分隔多个关键词，需全部命中（课程名+作业名）才静默。"""
    if not MUTE_FILE.exists():
        return []
    rules = []
    for line in MUTE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rules.append([t.lower() for t in line.split()])
    return rules


def is_muted(a, rules):
    key = f"{a['course']} {a['name']}".lower()
    return any(all(t in key for t in rule) for rule in rules)


def load_state():
    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    for rec in state.values():
        rec.setdefault("dates", [])
        rec.setdefault("final_dates", [])
        rec.setdefault("announced", False)
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- 展示辅助 ----------

def fmt_left(delta):
    total_min = max(0, int(delta.total_seconds() // 60))
    h, m = total_min // 60, total_min % 60
    if h >= 24:
        return f"{h // 24} 天 {h % 24} 小时"
    if h > 0:
        return f"{h} 小时 {m} 分钟"
    return f"{m} 分钟"


def fmt_due(due):
    return due.strftime("%m月%d日 %H:%M")


def offline_note(a):
    return "" if a["needs_online"] else "\n（此作业无需网上提交，Canvas 无法检测是否完成）\n"


# ---------- 主逻辑 ----------

def send_auth_alert(detail):
    """凭据/会话故障告警：每天最多一封。"""
    state = load_state()
    rec = state.setdefault(
        "_auth_alert", {"dates": [], "final_dates": [], "announced": False}
    )
    today = now().date().isoformat()
    if today in rec["dates"]:
        return False
    send_email(
        "[作业提醒] 系统故障：eLearning 登录会话失效且自动登录未成功",
        (
            f"{now():%Y-%m-%d %H:%M} {detail}\n\n"
            "在会话恢复前不会收到任何作业提醒。\n"
            "请在 ZCode 内置浏览器中手动登录 elearning.fudan.edu.cn，"
            "登录后提醒会自动恢复；若反复失败请检查 UIS 密码是否已修改。\n"
        ),
    )
    rec["dates"].append(today)
    save_state(state)
    return True


def check(raw=None):
    mutes = load_mutes()
    try:
        fetched = parse_raw(raw) if raw is not None else fetch_all()
    except AuthError as e:
        # Canvas 挂了也要能通知到用户：走 SMTP 每天最多告警一次
        send_auth_alert(str(e))
        raise
    assignments = [a for a in fetched if not is_muted(a, mutes)]
    t = now()
    undated = [a for a in assignments if a["due"] is None]
    urgent, final_call, daily = [], [], []

    for a in (x for x in assignments if x["due"] is not None):
        remain = a["due"] - t
        a["remain"] = remain
        if remain.total_seconds() < 0:
            if a["needs_online"]:
                daily.append(a)  # 需网上提交却过期未交：持续每日提醒
            # 线下作业过截止后不再提醒
        elif remain <= timedelta(hours=CONFIG["urgent_last_hours"]):
            if a["needs_online"]:
                urgent.append(a)
            else:
                final_call.append(a)
        elif remain <= timedelta(days=CONFIG["remind_days_before"]):
            daily.append(a)

    state = load_state()
    today = t.date().isoformat()
    sent = 0

    # 新作业通知（暂无截止时间的线下作业）：每个只发一次
    for a in sorted(undated, key=lambda x: str(x["id"])):
        rec = state.setdefault(
            str(a["id"]), {"dates": [], "final_dates": [], "announced": False}
        )
        if rec.get("announced"):
            continue
        send_email(
            f"[新作业] {a['course']} 发布了《{a['name']}》",
            (
                f"课程：{a['course']}\n"
                f"作业：{a['name']}\n"
                f"老师还没有设置截止时间，注意课上安排。\n"
                f"{offline_note(a)}"
                f"设置截止时间后会自动转入常规提醒。此通知只发一次。\n"
                f"链接：{a['html_url']}\n"
            ),
        )
        rec["announced"] = True
        sent += 1

    # 紧急（需网上提交）：最后 N 小时，每次运行都发
    for a in sorted(urgent, key=lambda x: x["due"]):
        left = fmt_left(a["remain"])
        send_email(
            f"[紧急] 《{a['name']}》还有 {left} 截止，快去提交！",
            (
                f"课程：{a['course']}\n"
                f"作业：{a['name']}\n"
                f"截止：{fmt_due(a['due'])}（剩余 {left}）\n"
                f"{offline_note(a)}"
                f"Canvas 上显示还未提交。每 15 分钟会再提醒一次，直到提交为止。\n"
                f"链接：{a['html_url']}\n"
            ),
        )
        sent += 1

    # 最后提醒（无需网上提交）：进入最后 N 小时只发一次
    for a in sorted(final_call, key=lambda x: x["due"]):
        rec = state.setdefault(
            str(a["id"]), {"dates": [], "final_dates": [], "announced": False}
        )
        if today in rec["final_dates"]:
            continue
        left = fmt_left(a["remain"])
        send_email(
            f"[最后提醒] 《{a['name']}》还有 {left} 截止",
            (
                f"课程：{a['course']}\n"
                f"作业：{a['name']}\n"
                f"截止：{fmt_due(a['due'])}（剩余 {left}）\n"
                f"{offline_note(a)}"
                f"链接：{a['html_url']}\n"
            ),
        )
        rec["final_dates"].append(today)
        sent += 1

    # 每日提醒：3 天内 / 需网上提交且已过截止，每天最多一次
    pending = [
        a for a in sorted(daily, key=lambda x: x["due"])
        if today not in state.setdefault(
            str(a["id"]), {"dates": [], "final_dates": [], "announced": False}
        )["dates"]
    ]
    if pending:
        lines = []
        for a in pending:
            if a["remain"].total_seconds() < 0:
                tag = "已过截止 {}（仍未提交！）".format(fmt_due(a["due"]))
            else:
                tag = "截止 {}（剩 {}）".format(fmt_due(a["due"]), fmt_left(a["remain"]))
            lines.append(
                f"· [{a['course']}] {a['name']} — {tag}{offline_note(a).strip()}\n  {a['html_url']}"
            )
        send_email(
            f"[作业提醒] {len(pending)} 个作业待处理",
            "以下作业还未完成：\n\n" + "\n".join(lines) + "\n",
        )
        for a in pending:
            state[str(a["id"])]["dates"].append(today)
        sent += 1

    save_state(state)
    print(
        f"{t:%Y-%m-%d %H:%M} 检查完成：未完成 {len(assignments)} 个"
        f"（需提交 {sum(1 for a in assignments if a['needs_online'])}，"
        f"线下 {sum(1 for a in assignments if not a['needs_online'])}，"
        f"其中暂无截止 {len(undated)} 个），"
        f"静默 {len(fetched) - len(assignments)} 个，紧急 {len(urgent)} 个，发送邮件 {sent} 封"
    )


def main():
    args = sys.argv[1:]
    if "--test-email" in args:
        t = now()
        send_email(
            "[作业提醒] 配置测试邮件",
            f"这是一封测试邮件（{t:%Y-%m-%d %H:%M}）。\n"
            "收到说明发件邮箱配置正确。作业提醒系统就绪。",
        )
        print("测试邮件已发送到", CONFIG["recipient"])
        return
    if "--from-json" in args:
        check(raw=load_raw(args[args.index("--from-json") + 1]))
        return
    if "--auth-alert" in args:
        sent = send_auth_alert("浏览器会话抓取失败（登录会话失效且自动重登未成功）")
        print("告警邮件已发送" if sent else "今日已发过告警，跳过")
        return
    check()


if __name__ == "__main__":
    main()
