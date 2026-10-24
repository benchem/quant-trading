"""提醒输出：控制台彩色 + JSONL 日志 + macOS 系统通知。"""
from __future__ import annotations

import json
import subprocess
import sys

from .common import WATCHLOG, ensure_dir, now_cst

_COLORS = {"info": "\033[36m", "warn": "\033[1;33m", "danger": "\033[1;31m", "target": "\033[1;32m"}
_RESET = "\033[0m"
_MAC = sys.platform == "darwin"
DISABLE_MAC = False


def _esc(s: str) -> str:
    return s.replace('"', '\\"')


def emit(a) -> None:
    line = f"[{now_cst().strftime('%H:%M:%S')}] {a.text}"
    if sys.stdout.isatty():
        print(f"{_COLORS.get(a.level, '')}{line}{_RESET}", flush=True)
    else:
        print(line, flush=True)
    ensure_dir()
    with open(WATCHLOG / "alerts.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(
            {"time": now_cst().isoformat(), "key": a.key, "level": a.level, "text": a.text},
            ensure_ascii=False) + "\n")
    if _MAC and not DISABLE_MAC and a.level in ("warn", "danger", "target"):
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{_esc(a.text)}" with title "计划盯盘 · 300687" sound name "Glass"'],
                timeout=3, check=False, capture_output=True)
        except Exception:  # noqa: BLE001
            pass
