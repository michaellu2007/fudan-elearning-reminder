# fudan-elearning-reminder

复旦 eLearning（Canvas）作业自动提醒：纯本地脚本，无需服务器、无需大模型。
自动检测未提交/未完成的作业，按策略给你的学邮或其他邮箱发提醒邮件——
快截止没提交会被"疯狂轰炸"，交了立刻闭嘴。

## 功能特性

- **网上提交的作业**：通过 Canvas 待办接口检测，你一提交它就自动从待办消失，提醒自动停止；
- **线下提交的作业**（如概率论：网上发布、纸质上交）：Canvas 永远不知道你做没做完，
  也能被捕获并提醒（老师在作业发布时可能还没设截止时间，会先发一封"新作业"通知）；
- **无人值守自愈**：UIS 登录会话约 2 小时失效，脚本用 Playwright 无头浏览器自动重登；
  守护进程每 15 分钟巡检一次，同时也是会话保活；
- **静默名单**：做完了线下作业？一行规则让它闭嘴（`mute.txt`）；
- **故障告警**：登录失败等异常会发邮件通知（每天最多一封，不会轰炸）。

## 提醒策略

| 作业类型 | 距截止 3 天内 | 最后 5 小时 | 过截止后 |
|---|---|---|---|
| 需网上提交 | 每天最多 1 封 | **每 15 分钟一封，直到提交** | 持续每日提醒 |
| 线下提交 | 每天最多 1 封 | 只发 1 封"最后提醒" | 不再提醒 |
| 线下、暂无截止时间 | 发布时 1 封"新作业"通知 | — | — |

窗口大小可在 `config.json` 调整（`remind_days_before` / `urgent_last_hours`）。

## 快速开始

需要 Windows + Python 3.10+。

```bat
git clone https://github.com/michaellu2007/fudan-elearning-reminder.git
cd fudan-elearning-reminder
python install.py
```

首次运行会生成 `config.json` 并提示你填写，填好后再次运行 `python install.py`，
它会自动：安装 Playwright 与无头 Chromium → 注册开机自启（隐藏窗口）→ 启动守护进程。

## 配置说明（config.json）

所有私密配置都在这**一个文件**里，它已被 `.gitignore` 排除，永远不会进仓库。

| 字段 | 说明 |
|---|---|
| `uis.user` / `uis.password` | 复旦统一身份认证（UIS）学工号与密码，用于自动登录 |
| `smtp.host` / `port` | 发件邮箱 SMTP 服务器（163：`smtp.163.com:465`，QQ：`smtp.qq.com:465`） |
| `smtp.user` / `auth_code` | 发件邮箱账号与 **SMTP 授权码**（不是邮箱登录密码，163/QQ 需在邮箱设置里开启 SMTP 后生成） |
| `recipient` | 提醒邮件收件地址（学邮、QQ 邮箱均可） |
| `remind_days_before` | 提前几天开始每日提醒（默认 3） |
| `urgent_last_hours` | 距截止多少小时内进入"疯狂模式"（默认 5） |
| `canvas_token` | 可选。Canvas 访问令牌（API 直连模式）。注意部分学校部署强制令牌约 1 小时失效，留空则使用浏览器会话模式（推荐） |

## 安全设计

- `config.json`（UIS 密码、SMTP 授权码）、`browser_state.json`（登录 Cookie）、
  `_browser_raw.json`（个人课业数据）、`sent_log.json`、`reminder.log`、`daemon.pid`
  全部在 `.gitignore` 中，**仓库里不含任何令牌、密码或个人数据**；
- 密码只存在本机，只在自动登录填表瞬间进入内存，绝不写入日志与邮件；
- 推送前可用 `git check-ignore config.json` 自行验证（输出该路径即表示已被排除）。

## 日常操作

| 想做什么 | 怎么做 |
|---|---|
| 让某个作业别再提醒 | 在 `mute.txt` 加一行规则，如 `概率论 第4次`（空格分隔关键词，全部命中才静默；删行即恢复） |
| 手动跑一轮检查 | `python browser_fetch.py` |
| 停止守护进程 | `python install.py --stop`（或建一个 `daemon.stop` 文件） |
| 恢复运行 | 删除 `daemon.stop`，再 `python install.py` |
| 卸载开机自启 | `python install.py --uninstall` |
| 看运行记录 | `reminder.log`，每轮一行摘要 |

## 工作原理

```
daemon.pyw（隐藏常驻，每 15 分钟）
   └─ browser_fetch.py
        ├─ Playwright 无头 Chromium 打开 eLearning
        ├─ 会话失效 → 读 config.json 自动重登 UIS
        ├─ 页内 fetch Canvas API：待办列表 + 各课程作业
        └─ check_and_remind.py：解析 + 策略判断 + SMTP 发信
```

## FAQ

**为什么不直接用 Canvas 访问令牌？**
复旦 eLearning 把 API 令牌和 UIS 会话绑定，实测令牌约 1 小时就被服务端作废（设置页里名义有效期到 2028 年也没用），无法无人值守。浏览器会话可通过账密自动续期，是目前唯一可靠的方式。

**UIS 会话失效后自动重登失败怎么办？**
会收到一封告警邮件（每天最多一封）。常见原因是 UIS 弹了验证码或密码已修改——手动用浏览器登录一次 eLearning 没有直接帮助（会话由脚本自己管理），需检查 `config.json` 里的密码，或重跑 `python browser_fetch.py` 查看具体报错。

**电脑关机/睡眠会怎样？**
守护进程随之暂停，开机登录后自动恢复。漏掉的检查不会补跑，但下一次巡检会拿到最新状态，策略基于当前时刻计算，不影响正确性。

**非复旦用户能用吗？**
任何 Canvas LMS 实例（学校自建或 canvas.instructure.com）都适用，把 `canvas_base_url` 改成你们学校的地址即可；自动登录表单的定位逻辑可能需要按你们的 SSO 页面微调。
