"""提醒输出：控制台彩色 + watchlog/alerts.jsonl（CLI 日报依赖）+ 统一站内信收件箱。

统一渠道改造要点：
- 交易提醒以 source="trade" 写入 notifybox 共享收件箱（data/notifications.jsonl），
  使 Web 仪表盘站内信能实时看到 planwatch 的价格位/止损止盈/T 信号/时间钩子提醒；
- macOS 桌面弹窗统一由收件箱按级别（warn/alert）触发，本模块不再自行 osascript
  （避免与 analyzer 双重弹窗）；--no-notify 经 notifybox.set_desktop_enabled 关闭。
- 保留 watchlog/alerts.jsonl 落盘：cmd_report 依赖它汇总当日提醒。
"""
from __future__ import annotations

import json
import sys

from .common import ROOT, WATCHLOG, ensure_dir, now_cst
from notifybox import get_inbox, set_desktop_enabled  # noqa: F401  (set 供 cli --no-notify)

_COLORS = {"info": "\033[36m", "warn": "\033[1;33m", "danger": "\033[1;31m", "target": "\033[1;32m"}
_RESET = "\033[0m"

_DATA_DIR = ROOT / "data"


def emit(a) -> None:
    day = now_cst().strftime("%Y-%m-%d")
    line = f"[{now_cst().strftime('%H:%M:%S')}] {a.text}"
    if sys.stdout.isatty():
        print(f"{_COLORS.get(a.level, '')}{line}{_RESET}", flush=True)
    else:
        print(line, flush=True)
    # 1) CLI 本地日志（日报依赖，保持原样）
    ensure_dir()
    with open(WATCHLOG / "alerts.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(
            {"time": now_cst().isoformat(), "key": a.key, "level": a.level, "text": a.text},
            ensure_ascii=False) + "\n")
    # 2) 统一站内信（跨进程共享；桌面弹窗由收件箱按级别触发）
    try:
        get_inbox(_DATA_DIR).push(
            key=f"trade:{day}:{a.key}", text=a.text, level=a.level,
            code=getattr(a, "code", "") or None, source="trade", day=day,
            extra={"event": a.key},
        )
    except Exception:  # noqa: BLE001 收件箱写入失败不影响 CLI 提醒本身
        pass
