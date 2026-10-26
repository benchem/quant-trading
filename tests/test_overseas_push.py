"""昨夜美股简报邮件推送单测：正文渲染 / 配置解析 / 推送与防重发 / 调度重试。

全部离线（tempfile 目录 + fake mailer），不触网、不发真邮件、不污染真实 data/。

运行: .venv/bin/python tests/test_overseas_push.py
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, ".")

from analyzer.mailer import MailConfig, MailError, send_mail  # noqa: E402
from analyzer.overseas_push import (  # noqa: E402
    CST, MailConfig as _MC, OverseasBriefPusher, build_subject, parse_retry_minutes,
    push_daily_brief, pusher_from_env, render_brief_html, render_brief_text)

PASSED = 0
FAILED = 0


def check(name, fn) -> bool:
    global PASSED, FAILED
    print(f"\n===== {name} =====", flush=True)
    try:
        fn()
        print("PASS")
        PASSED += 1
        return True
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        FAILED += 1
        return False


BRIEF_MD = """状态：有效
简报日期：2026-10-26
风险偏好分数：-42 / 100
情绪判断：偏空
置信度：中

# 每日美股要闻与交易情绪简报

生成时间：2026-10-26 08:36（北京时间）

## 核心要闻

- **三大指数集体收跌，道指弱于科技股。** 道指报 52,215.16 点，跌 1.23%。来源：[Reuters](https://www.reuters.com/)。
- **油价和高收益率共同压制估值。** Brent 原油升至 116.72 美元/桶。

## 自选股映射

- 300687.SZ（赛意信息）：**间接偏负面**，非公司基本面信号。
- 300170.SZ（汉得信息）：映射逻辑与赛意信息相似。

---

仅供研究参考，不构成投资建议。
"""


def _write_brief(root, day="2026-10-26", text=BRIEF_MD):
    path = Path(root) / "news" / "us_market" / f"{day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class _FakeNotifier:
    def __init__(self):
        self.items = []

    def push(self, key, text, level="info", **kw):
        self.items.append({"key": key, "text": text, "level": level})
        return {"ok": True}


class _Sentinel(Exception):
    pass


def _fake_send_ok(cfg, subject, html_body, text_body=None):
    return {"ok": True, "to": cfg.mail_to, "dry_run": cfg.dry_run}


def _fake_send_fail(cfg, subject, html_body, text_body=None):
    raise MailError("模拟 SMTP 失败")


# ------------------------------------------------------------------ 用例


def t_render_html():
    html = render_brief_html(BRIEF_MD, "2026-10-26")
    # 标题映射：# -> h2，## -> h3
    assert "<h2" in html and "<h3" in html
    # 行内渲染：粗体 / 链接
    assert "<b>三大指数集体收跌" in html
    assert '<a href="https://www.reuters.com/"' in html
    # 列表
    assert "<ul" in html and "<li" in html
    # 转义保护：无裸 <script>
    assert "<script>" not in html
    # 字段行标签样式（状态：有效）
    assert "状态" in html and "有效" in html
    # 分隔线
    assert "<hr" in html
    # 免责声明保留
    assert "不构成投资建议" in html
    # 纯文本回退去掉 markdown 记号（链接只留文字）
    plain = render_brief_text(BRIEF_MD)
    assert "**" not in plain and "`" not in plain
    assert "Reuters" in plain and "[Reuters]" not in plain
    assert "https://www.reuters.com" not in plain


def t_render_escapes_html():
    evil = "# 标题\n- 项目 <img src=x onerror=alert(1)> **粗体**"
    html = render_brief_html(evil, "2026-10-26")
    assert "<img" not in html and "&lt;img" in html


def t_mail_config():
    import os

    old = {k: os.environ.get(k) for k in
           ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS",
            "MAIL_FROM", "MAIL_TO", "MAIL_DRY_RUN")}
    try:
        for k in old:
            os.environ.pop(k, None)
        # 缺省 -> 停用
        cfg = MailConfig.from_env()
        assert not cfg.enabled
        # 配齐 -> 启用，from 缺省取 user
        os.environ.update({
            "SMTP_USER": "a@qq.com", "SMTP_PASS": "code",
            "MAIL_TO": "b@x.com, c@y.com",
        })
        cfg = MailConfig.from_env()
        assert cfg.enabled and cfg.from_addr == "a@qq.com"
        assert cfg.mail_to == ["b@x.com", "c@y.com"]
        assert cfg.smtp_port == 587 and cfg.smtp_host == "smtp.qq.com"
        # dry_run
        os.environ["MAIL_DRY_RUN"] = "true"
        assert MailConfig.from_env().dry_run
        # 未配置收件人 -> 停用
        os.environ["MAIL_TO"] = ""
        assert not MailConfig.from_env().enabled
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def t_send_mail_dry_run_and_missing():
    cfg = MailConfig(smtp_user="a@qq.com", smtp_pass="x",
                     mail_to=["b@x.com"], dry_run=True)
    r = send_mail(cfg, "测试主题", "<p>hi</p>", text_body="hi")
    assert r["ok"] and r["dry_run"]
    # 未配置 -> MailError
    try:
        send_mail(MailConfig(), "s", "<p>x</p>")
        raise AssertionError("未配置应报错")
    except MailError as e:
        assert "未配置" in str(e)


def t_push_daily_brief():
    import analyzer.overseas_push as op

    with tempfile.TemporaryDirectory() as tmp:
        day = dt.date(2026, 10, 26)   # 周一
        _write_brief(tmp)
        cfg = MailConfig(smtp_user="a@qq.com", smtp_pass="x", mail_to=["b@x.com"])

        # 简报无效（缺失）-> 不发
        empty_day = dt.date(2026, 10, 19)
        r = push_daily_brief(tmp, cfg, on_date=empty_day)
        assert not r["ok"] and "简报缺失" in r["reason"]

        # 状态非有效 -> 不发
        _write_brief(tmp, day="2026-10-26",
                     text=BRIEF_MD.replace("状态：有效", "状态：休市"))
        r = push_daily_brief(tmp, cfg, on_date=day)
        assert not r["ok"] and "休市" in r["reason"]

        # 正常 -> 发送成功，subject 带分数与情绪
        _write_brief(tmp)
        orig = op.send_mail
        op.send_mail = _fake_send_ok
        try:
            r = push_daily_brief(tmp, cfg, on_date=day)
        finally:
            op.send_mail = orig
        assert r["ok"] and r["risk_score"] == -42
        assert "风险偏好 -42" in r["subject"] and "偏空" in r["subject"]

        # SMTP 失败 -> ok=False 且 reason 带原因，不抛异常
        op.send_mail = _fake_send_fail
        try:
            r = push_daily_brief(tmp, cfg, on_date=day)
        finally:
            op.send_mail = orig
        assert not r["ok"] and "模拟 SMTP 失败" in r["reason"]


def t_pusher_idempotent_and_retry():
    import analyzer.overseas_push as op

    with tempfile.TemporaryDirectory() as tmp:
        _write_brief(tmp)
        cfg = MailConfig(smtp_user="a@qq.com", smtp_pass="x",
                         mail_to=["b@x.com"])
        notifier = _FakeNotifier()
        p = OverseasBriefPusher(tmp, cfg, notifier=notifier,
                                push_hour=8, push_minute=40,
                                retry_minutes=(30, 30))

        # 1) 首次推送成功
        sent = []
        op.send_mail = lambda c, s, h, text_body=None: sent.append(s) or \
            {"ok": True, "to": c.mail_to, "dry_run": False}
        r = p.push_once(source="manual")
        assert r["ok"] and len(sent) == 1
        assert notifier.items and notifier.items[-1]["level"] == "ok"

        # 2) 当日重复推送 -> 幂等跳过
        r = p.push_once(source="manual")
        assert r.get("skipped") and len(sent) == 1

        # 3) 状态文件持久化：新实例（模拟重启）也不会重发
        p2 = OverseasBriefPusher(tmp, cfg, push_hour=8, push_minute=40)
        r = p2.push_once(source="schedule")
        assert r.get("skipped") and len(sent) == 1
        day = "2026-10-26"
        # attempts 保持首次推送的计数（跳过不额外计数）
        assert p2.state.attempts(day) == 1

        # 4) 失败重试节奏（锚定上次实际尝试时刻）：
        #    8:40 首推失败 -> 9:10 前不重试 -> 9:11 第二次 -> 9:45 重试耗尽不再推
        p3 = OverseasBriefPusher(tmp, cfg, push_hour=8, push_minute=40,
                                 retry_minutes=(30, 30))
        p3.state._data["sent_days"] = {}   # 清掉当日记录
        p3.state._save()
        fails = []
        op.send_mail = lambda c, s, h, text_body=None: fails.append(1) or \
            _fake_send_fail(c, s, h)
        # 8:39 未到时刻 -> 不推
        p3._tick_at(dt.datetime(2026, 10, 26, 8, 39, tzinfo=CST))
        assert len(fails) == 0
        # 8:41 首推（失败，记录实际尝试时刻 8:41）
        p3._tick_at(dt.datetime(2026, 10, 26, 8, 41, tzinfo=CST))
        assert len(fails) == 1
        # 9:10（8:41+30min 之前）-> 不推
        p3._tick_at(dt.datetime(2026, 10, 26, 9, 10, tzinfo=CST))
        assert len(fails) == 1
        # 9:12 第二次（失败，锚定 9:12）
        p3._tick_at(dt.datetime(2026, 10, 26, 9, 12, tzinfo=CST))
        assert len(fails) == 2
        # 9:41（9:12+30min 之前）-> 不推
        p3._tick_at(dt.datetime(2026, 10, 26, 9, 41, tzinfo=CST))
        assert len(fails) == 2
        # 9:43 第三次（失败，2 次重试用完）
        p3._tick_at(dt.datetime(2026, 10, 26, 9, 43, tzinfo=CST))
        assert len(fails) == 3
        # 之后任意时刻 -> 重试耗尽，不再推
        p3._tick_at(dt.datetime(2026, 10, 26, 12, 0, tzinfo=CST))
        assert len(fails) == 3
        # 周末不推（即使简报存在、未到重试上限）
        p4 = OverseasBriefPusher(tmp, cfg, push_hour=8, push_minute=40)
        p4.state._data["sent_days"] = {}
        p4.state._save()
        p4._tick_at(dt.datetime(2026, 10, 24, 9, 0, tzinfo=CST))   # 周六
        assert len(fails) == 3


def t_parse_retry():
    assert parse_retry_minutes("30,30,30") == (30, 30, 30)
    assert parse_retry_minutes("") == (30, 30, 30)
    assert parse_retry_minutes("bad") == (30, 30, 30)
    assert parse_retry_minutes("10") == (10,)


def t_pusher_from_env_disabled():
    import os

    old = {k: os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASS", "MAIL_TO")}
    try:
        for k in old:
            os.environ.pop(k, None)
        with tempfile.TemporaryDirectory() as tmp:
            p = pusher_from_env(tmp)
            assert not p.cfg.enabled
            p.start()   # 未启用时 start 为空操作（不建线程）
            assert p._thread is None
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def t_build_subject():
    assert build_subject({"risk_score": -42, "risk_view": "偏空",
                          "date": "2026-10-26"}).startswith("🇺🇸 昨夜美股")
    assert "+35" in build_subject({"risk_score": 35, "risk_view": "偏多",
                                   "date": "2026-10-26"})
    s = build_subject({"risk_score": None, "risk_view": "", "date": "2026-10-26"})
    assert "None" not in s


if __name__ == "__main__":
    check("简报 Markdown → 邮件 HTML 渲染", t_render_html)
    check("HTML 注入转义", t_render_escapes_html)
    check("邮件配置解析（环境变量）", t_mail_config)
    check("send_mail dry-run 与未配置", t_send_mail_dry_run_and_missing)
    check("push_daily_brief 状态机", t_push_daily_brief)
    check("推送幂等 / 重试节奏 / 周末跳过", t_pusher_idempotent_and_retry)
    check("重试间隔解析", t_parse_retry)
    check("未配置时调度器停用", t_pusher_from_env_disabled)
    check("邮件主题构造", t_build_subject)
    print(f"\n{PASSED} 通过 / {FAILED} 失败")
    sys.exit(1 if FAILED else 0)
