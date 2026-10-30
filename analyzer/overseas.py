"""每日美股简报的当日有效性校验与 AI 上下文提取。"""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Shanghai")
MAX_HEADLINES = 6
MAX_MAPPINGS = 4
MAX_ITEM_CHARS = 400


def _unavailable(day: date, reason: str) -> dict:
    return {
        "status": "unavailable",
        "date": day.isoformat(),
        "message": f"无有效海外信号（{reason}）",
        "risk_score": None,
        "headlines": [],
        "watchlist_mapping": [],
    }


def _field(text: str, label: str) -> str:
    pattern = rf"^\s*(?:[-*+]\s*)?(?:\*\*)?{re.escape(label)}(?:\*\*)?\s*[：:]\s*(.+?)\s*$"
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        return ""
    return match.group(1).strip().strip("*").strip()


def _section(text: str, keyword: str) -> list[str]:
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if match and keyword in match.group(1).replace("*", ""):
            start = i + 1
            break
    if start is None:
        return []
    end = len(lines)
    for i in range(start, len(lines)):
        if re.match(r"^#{1,6}\s+", lines[i]):
            end = i
            break
    return lines[start:end]


def _items(lines: list[str], limit: int) -> list[str]:
    items = []
    for line in lines:
        line = line.strip()
        if not line or re.match(r"^(?:-{3,}|\*\*\*)$", line):
            continue
        if re.match(r"^(?:状态|简报日期|风险偏好分数|情绪判断|置信度)\s*[：:]", line):
            continue
        line = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)、]\s*)", "", line)
        line = re.sub(r"\[([^]]+)]\([^)]*\)", r"\1", line)
        line = line.replace("**", "").replace("`", "").strip()
        if not line or "不构成投资建议" in line:
            continue
        items.append(line[:MAX_ITEM_CHARS])
        if len(items) >= limit:
            break
    return items


def _mapping_for(lines: list[str], code: str | None) -> list[str]:
    """code=None 时不按个股过滤（整体视角，如邮件推送）。"""
    if code is None:
        return _items(lines, MAX_MAPPINGS)
    code = code.upper()
    selected = []
    for item in _items(lines, 50):
        codes = {c.upper() for c in re.findall(r"\b\d{6}\.(?:SH|SZ|BJ)\b", item, re.I)}
        if codes and code not in codes:
            continue
        selected.append(item)
        if len(selected) >= MAX_MAPPINGS:
            break
    return selected


def load_daily_brief(data_dir: str | Path, code: str | None,
                     on_date: date | None = None,
                     _filename: str | None = None) -> dict:
    """读取当天简报；任何校验失败均不返回旧简报中的信号内容。

    code=None 时不做个股映射过滤（整体推送视角）。
    _filename 供生成端校验草稿用（默认按日期命名，推送/AI 分析勿传）。
    """
    day = on_date or datetime.now(LOCAL_TZ).date()
    if day.weekday() >= 5:
        return _unavailable(day, "周末")
    fname = _filename or f"{day.isoformat()}.md"
    path = Path(data_dir) / "news" / "us_market" / fname
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _unavailable(day, "当日简报缺失")
    except (OSError, UnicodeError):
        return _unavailable(day, "当日简报读取失败")

    status = _field(text, "状态")
    if status != "有效":
        reason = status or "未明确标记有效状态"
        return _unavailable(day, reason)
    brief_date = _field(text, "简报日期")
    if brief_date and brief_date[:10] != day.isoformat():
        return _unavailable(day, "简报日期不一致")

    score_text = _field(text, "风险偏好分数")
    match = re.search(r"[-+]?\d+(?:\.\d+)?", score_text.replace("−", "-"))
    if not match:
        return _unavailable(day, "缺少有效风险偏好分数")
    score = float(match.group())
    if not math.isfinite(score) or not -100 <= score <= 100:
        return _unavailable(day, "风险偏好分数越界")
    headlines = _items(_section(text, "核心要闻"), MAX_HEADLINES)
    if not headlines:
        return _unavailable(day, "缺少核心要闻")

    return {
        "status": "valid",
        "date": day.isoformat(),
        "message": "当日海外信号有效",
        "risk_score": score,
        "risk_view": _field(text, "情绪判断"),
        "confidence": _field(text, "置信度"),
        "headlines": headlines,
        "watchlist_mapping": _mapping_for(_section(text, "自选股映射"), code),
    }
