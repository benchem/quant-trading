"""昨夜美股简报邮件推送：读当日 data/news/us_market/*.md → HTML 邮件 → SMTP。

- 数据源：与 AI 分析共享的每日美股简报（analyzer.overseas.load_daily_brief
  已做严格当日有效性校验：状态有效 / 日期一致 / 分数在界 / 有核心要闻，
  周末或校验失败一律不推旧内容）。
- 调度：守护线程（随 analyzer serve 存活），每个工作日 PUSH_HOUR:PUSH_MINUTE
  （默认 08:40 CST）触发；简报还没生成/无效时按 RETRY_INTERVALS 重试，
  当日成功或重试耗尽后不再重发；进程启动时若当日时刻已过且未推过则补推一次。
- 渠道：analyzer.mailer.send_mail（QQ SMTP 587 STARTTLS，借 iam 服务器
  github-trending-push 的 email 通道做法；账号由本系统 MAIL_*/SMTP_* 配置）。
- 失败只推站内信告警 + 日志，绝不中断 analyzer 主服务。

环境变量（.env，缺省功能停用）:
    SMTP_HOST=smtp.qq.com
    SMTP_PORT=587              # 465 走 SSL，其余走 STARTTLS
    SMTP_USER=xxx@qq.com
    SMTP_PASS=<SMTP 授权码>
    MAIL_FROM=xxx@qq.com       # 缺省同 SMTP_USER
    MAIL_TO=a@x.com,b@y.com    # 逗号分隔多收件人
    US_BRIEF_PUSH_HOUR=8       # 推送时刻（CST），默认 8
    US_BRIEF_PUSH_MINUTE=40    # 默认 40（简报通常 08:30~08:40 落盘）
    US_BRIEF_PUSH_RETRY=30     # 简报未就绪时重试间隔分钟数，逗号分隔，默认 30,30,30
    MAIL_DRY_RUN=false         # true 只记日志不发信
"""
from __future__ import annotations

import datetime as dt
import html as html_mod
import re
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

from .mailer import MailConfig, MailError, send_mail
from .overseas import load_daily_brief

CST = ZoneInfo("Asia/Shanghai")

DEFAULT_PUSH_HOUR = 8
DEFAULT_PUSH_MINUTE = 40
DEFAULT_RETRY_MINUTES = (30, 30, 30)
CHECK_INTERVAL = 60.0   # 调度线程基础轮询间隔（秒）

SUBJECT_PREFIX = "🇺🇸 昨夜美股"
MAX_SECTIONS_IN_MAIL = 8   # 邮件正文最多渲染的小节数（防异常大文件）


class PushState:
    """推送结果记录（进程内防重发；持久化到 data/news/us_market/.push_state.json）。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict = {"sent_days": {}, "last_error": None}
        self._load()

    def _load(self) -> None:
        try:
            import json

            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                self._data = data
        except (FileNotFoundError, OSError, ValueError):
            pass

    def _save(self) -> None:
        try:
            import json

            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 只保留最近 60 天记录，防文件无限增长
            days = self._data.get("sent_days") or {}
            cut = (dt.datetime.now(CST) - dt.timedelta(days=60)).date().isoformat()
            self._data["sent_days"] = {k: v for k, v in days.items() if k >= cut}
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        except (OSError, ValueError):
            pass

    def sent_day(self, day: str) -> bool:
        with self._lock:
            return bool((self._data.get("sent_days") or {}).get(day, {}).get("ok"))

    def mark_sent(self, day: str, subject: str) -> None:
        with self._lock:
            rec = self._data.setdefault("sent_days", {}).setdefault(day, {})
            rec["ok"] = True
            rec["subject"] = subject
            rec["time"] = dt.datetime.now(CST).isoformat(timespec="seconds")
            self._data["last_error"] = None
            self._save()

    def mark_failed(self, day: str, error: str) -> None:
        with self._lock:
            rec = self._data.setdefault("sent_days", {}).setdefault(day, {})
            rec["ok"] = False
            rec["error"] = error[:300]
            rec.setdefault("time", dt.datetime.now(CST).isoformat(timespec="seconds"))
            self._data["last_error"] = f"{day}: {error[:300]}"
            self._save()

    def attempts(self, day: str) -> int:
        with self._lock:
            return int((self._data.get("sent_days") or {}).get(day, {}).get("attempts", 0))

    def last_attempt_time(self, day: str) -> str | None:
        with self._lock:
            return (self._data.get("sent_days") or {}).get(day, {}).get(
                "last_attempt_time")

    def bump_attempts(self, day: str, when: dt.datetime | None = None) -> int:
        """记一次尝试（含实际时刻，重试节奏锚定它而非计划时刻）。"""
        with self._lock:
            rec = self._data.setdefault("sent_days", {}).setdefault(day, {})
            rec["attempts"] = int(rec.get("attempts", 0)) + 1
            rec["last_attempt_time"] = (when or dt.datetime.now(CST)).isoformat(
                timespec="seconds")
            self._save()
            return rec["attempts"]


# ------------------------------------------------------------------ 正文渲染


def _esc(s: str) -> str:
    return html_mod.escape(s, quote=True)


def _inline(s: str) -> str:
    """极简 Markdown 行内渲染：**粗体** / [text](url) / `code`。"""
    s = _esc(s)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
               r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def render_brief_html(text: str, day: str) -> str:
    """把简报 Markdown 渲染为邮件 HTML（标题层级映射 + 列表 + 邮件内联样式）。

    不引入 markdown 依赖（服务器 py3.10 无该包），覆盖简报实际用到的语法：
    #/##/### 标题、- 列表、**粗体**、[链接](url)、`代码`、--- 分隔线、普通段落。
    """
    lines = text.splitlines()
    body: list[str] = []
    in_list = False
    h_level = 0
    n_sections = 0

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            body.append("</ul>")
            in_list = False

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            close_list()
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            close_list()
            n_sections += 1
            if n_sections > MAX_SECTIONS_IN_MAIL:
                break
            level = min(len(m.group(1)) + 1, 5)   # # -> h2，## -> h3 …
            h_level = len(m.group(1))
            title = _inline(m.group(2).strip())
            style = ('style="margin:18px 0 8px;font-size:16px;color:#1a2b4a;'
                     'border-bottom:2px solid #e8ecf3;padding-bottom:6px"'
                     if h_level == 1 else 'style="margin:16px 0 6px;font-size:14px;color:#2b3d5c"')
            body.append(f"<h{level} {style}>{title}</h{level}>")
            continue
        if re.match(r"^-{3,}\s*$", line):
            close_list()
            body.append('<hr style="border:none;border-top:1px solid #e8ecf3;margin:14px 0">')
            continue
        m = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if m:
            if not in_list:
                body.append('<ul style="margin:6px 0 6px 0;padding-left:22px">')
                in_list = True
            body.append(f'<li style="margin:4px 0;line-height:1.7;font-size:13px;'
                        f'color:#333">{_inline(m.group(1).strip())}</li>')
            continue
        m = re.match(r"^\s*(\d+)[.)、]\s+(.*)$", line)
        if m:
            if not in_list:
                body.append('<ul style="margin:6px 0;padding-left:22px">')
                in_list = True
            body.append(f'<li style="margin:4px 0;line-height:1.7;font-size:13px;'
                        f'color:#333">{_inline(m.group(2).strip())}</li>')
            continue
        # 字段行（状态：有效 等）渲染成小标签样式
        m = re.match(r"^([^\s：:]{2,8})[：:]\s*(.*)$", line)
        if m and h_level == 0:
            body.append(
                f'<div style="margin:2px 0"><span style="display:inline-block;'
                f'background:#eef3fb;color:#2b4a8a;border-radius:4px;padding:1px 8px;'
                f'font-size:12px;font-weight:600">{_esc(m.group(1))}</span>'
                f'<span style="font-size:13px;color:#333;margin-left:6px">'
                f"{_inline(m.group(2))}</span></div>")
            continue
        close_list()
        body.append(f'<p style="margin:6px 0;line-height:1.7;font-size:13px;'
                    f'color:#333">{_inline(line.strip())}</p>')
    close_list()

    footer = (
        '<hr style="border:none;border-top:1px solid #e8ecf3;margin:18px 0 10px">'
        '<p style="font-size:11px;color:#999;line-height:1.6">'
        f"本邮件由 quant-trading 系统自动发送（{dt.datetime.now(CST).strftime('%Y-%m-%d %H:%M')} CST）"
        " · 仅供研究参考，不构成投资建议 · "
        '<a href="https://github.com/" target="_blank" rel="noopener" '
        'style="color:#888">数据见仪表盘</a></p>')
    return (
        '<div style="max-width:680px;margin:0 auto;font-family:'
        '-apple-system,BlinkMacSystemFont,\'PingFang SC\',\'Microsoft YaHei\','
        'Segoe UI,Arial,sans-serif;padding:4px 2px">'
        + "".join(body) + footer + "</div>")


def render_brief_text(text: str) -> str:
    """纯文本回退：去 Markdown 记号（HTML 客户端不可用时的 set_content）。"""
    out = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    out = out.replace("**", "").replace("`", "")
    return out


def build_subject(brief: dict) -> str:
    score = brief.get("risk_score")
    view = brief.get("risk_view") or ""
    date_str = brief.get("date", "")
    if isinstance(score, (int, float)):
        return f"{SUBJECT_PREFIX} {date_str} · 风险偏好 {score:+.0f}（{view}）"
    return f"{SUBJECT_PREFIX} {date_str} · {view}".rstrip(" ·")


# ------------------------------------------------------------------ 推送逻辑


def push_daily_brief(data_dir: str | Path, cfg: MailConfig,
                     on_date: dt.date | None = None) -> dict:
    """读当日简报并发邮件。返回 {"ok": bool, "reason": str, ...}，不抛异常。"""
    day = on_date or dt.datetime.now(CST).date()
    brief = load_daily_brief(data_dir, code=None, on_date=day)
    if brief["status"] != "valid":
        return {"ok": False, "reason": brief["message"],
                "brief_status": "unavailable", "day": day.isoformat()}

    path = Path(data_dir) / "news" / "us_market" / f"{day.isoformat()}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        return {"ok": False, "reason": f"简报文件读取失败: {e}", "day": day.isoformat()}

    subject = build_subject(brief)
    html_body = render_brief_html(text, day.isoformat())
    text_body = render_brief_text(text)
    try:
        send_mail(cfg, subject, html_body, text_body=text_body)
    except MailError as e:
        return {"ok": False, "reason": str(e), "brief_status": "valid",
                "day": day.isoformat(), "subject": subject}
    return {"ok": True, "day": day.isoformat(), "subject": subject,
            "brief_status": "valid", "risk_score": brief.get("risk_score")}


class OverseasBriefPusher:
    """美股简报邮件推送调度器（守护线程，随 analyzer serve 存活）。"""

    def __init__(self, data_dir: str | Path, cfg: MailConfig,
                 notifier=None, push_hour: int = DEFAULT_PUSH_HOUR,
                 push_minute: int = DEFAULT_PUSH_MINUTE,
                 retry_minutes: tuple[int, ...] = DEFAULT_RETRY_MINUTES,
                 check_interval: float = CHECK_INTERVAL):
        self.data_dir = Path(data_dir)
        self.cfg = cfg
        self.notifier = notifier
        self.push_hour = int(push_hour)
        self.push_minute = int(push_minute)
        self.retry_minutes = tuple(int(x) for x in retry_minutes) or DEFAULT_RETRY_MINUTES
        self.check_interval = float(check_interval)
        self.state = PushState(self.data_dir / "news" / "us_market" / ".push_state.json")
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.status: dict = {
            "enabled": cfg.enabled, "trigger": f"{self.push_hour:02d}:{self.push_minute:02d}",
            "retries": list(self.retry_minutes), "last_result": None,
            "last_attempt_time": None, "sent_days": 0,
        }

    # ------------------------------------------------------ 调度

    def start(self) -> None:
        if not self.cfg.enabled:
            print(f"美股简报邮件推送: 未启用（配置 SMTP_USER/SMTP_PASS/MAIL_TO 后可用，"
                  f"计划每工作日 {self.status['trigger']} CST 推送）")
            return
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="us-brief-push",
                                        daemon=True)
        self._thread.start()
        print(f"美股简报邮件推送: 已启用（每工作日 {self.status['trigger']} CST，"
              f"收件 {len(self.cfg.mail_to)} 人；简报未就绪时按 "
              f"{self.retry_minutes} 分钟重试）")

    def stop(self) -> None:
        self._stop.set()

    def _push_time_today(self, now: dt.datetime) -> dt.datetime:
        return now.replace(hour=self.push_hour, minute=self.push_minute,
                           second=0, microsecond=0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001 调度线程绝不退出
                print(f"[us-brief-push] 调度异常: {e}")
            self._stop.wait(self.check_interval)

    def _tick(self) -> None:
        self._tick_at(dt.datetime.now(CST))

    def _tick_at(self, now: dt.datetime) -> None:
        day = now.date()
        day_str = day.isoformat()
        if day.weekday() >= 5:          # 周末美股无收盘，不推
            return
        if self.state.sent_day(day_str):
            return                      # 当日已成功，防重发
        fire = self._push_time_today(now)
        if now < fire:
            return                      # 未到推送时刻
        n = self.state.attempts(day_str)
        if n > 0:
            if n > len(self.retry_minutes):
                return                  # 当日重试已耗尽
            # 重试锚定「上次实际尝试时刻 + 当前档位间隔」（手动补推也算一次）
            last = self.state.last_attempt_time(day_str)
            try:
                last_dt = dt.datetime.fromisoformat(last) if last else None
            except ValueError:
                last_dt = None
            base = last_dt if (last_dt and last_dt.tzinfo) else fire
            next_at = base + dt.timedelta(minutes=self.retry_minutes[n - 1])
            if now < next_at:
                return                  # 未到下一次重试时刻
        self.push_once(source="schedule", on_date=day, now=now)

    def push_once(self, source: str = "manual", on_date: dt.date | None = None,
                  now: dt.datetime | None = None) -> dict:
        """执行一次推送（手动 / 调度共用）；当日已成功时幂等跳过。

        now 供调度器注入触发时刻（重试锚点）；缺省取当前时间。
        """
        now = now or dt.datetime.now(CST)
        day = on_date or now.date()
        day_str = day.isoformat()
        if self.state.sent_day(day_str):
            return {"ok": True, "skipped": True, "reason": "当日已推送",
                    "day": day_str}
        result = push_daily_brief(self.data_dir, self.cfg, on_date=day)
        self.state.bump_attempts(day_str, when=now)
        self.status["last_result"] = result
        self.status["last_attempt_time"] = now.isoformat(timespec="seconds")

        if result["ok"]:
            self.state.mark_sent(day_str, result.get("subject", ""))
            self.status["sent_days"] = self.status.get("sent_days", 0) + 1
            self._notify(f"美股简报邮件已推送（{result.get('subject', '')}）",
                         level="ok")
            print(f"[us-brief-push] 已推送: {result.get('subject', day_str)}")
        else:
            self.state.mark_failed(day_str, result.get("reason", "unknown"))
            self._notify(f"美股简报邮件推送失败：{result.get('reason', '')}",
                         level="warn")
            print(f"[us-brief-push] 失败（{source}）: {result.get('reason', '')}")
        return result

    def _notify(self, text: str, level: str = "info") -> None:
        """推送结果同时写站内信（Web 消息中心可见）。

        key 带结果后缀，避免「失败告警」与「稍后重试成功」被同日去重。
        """
        if self.notifier is None:
            return
        try:
            self.notifier.push(
                key=f"us_brief_push:{dt.datetime.now(CST).date().isoformat()}:{level}",
                text=text, level=level, desktop=False, source="system")
        except Exception:  # noqa: BLE001 站内信失败不影响推送
            pass

    def get_status(self) -> dict:
        return dict(self.status)


def parse_retry_minutes(raw: str) -> tuple[int, ...]:
    """解析 US_BRIEF_PUSH_RETRY（逗号分隔分钟数），非法值回落默认。"""
    try:
        vals = tuple(int(x) for x in raw.split(",") if x.strip())
        return vals if vals else DEFAULT_RETRY_MINUTES
    except ValueError:
        return DEFAULT_RETRY_MINUTES


def pusher_from_env(data_dir: str | Path, notifier=None) -> OverseasBriefPusher:
    """按环境变量构建调度器（未配置邮件时返回 enabled=False 的实例，start() 为空操作）。"""
    import os

    cfg = MailConfig.from_env()
    return OverseasBriefPusher(
        data_dir, cfg, notifier=notifier,
        push_hour=int(os.environ.get("US_BRIEF_PUSH_HOUR", DEFAULT_PUSH_HOUR)),
        push_minute=int(os.environ.get("US_BRIEF_PUSH_MINUTE", DEFAULT_PUSH_MINUTE)),
        retry_minutes=parse_retry_minutes(os.environ.get("US_BRIEF_PUSH_RETRY", "")),
    )
