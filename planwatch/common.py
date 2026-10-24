"""公共工具：路径、北京时间、交易日历、交易时段。"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parent.parent
WATCHLOG = ROOT / "watchlog"
CALENDAR = ROOT / "data" / "trade_calendar.parquet"

_cal_days: set[dt.date] | None = None


def now_cst() -> dt.datetime:
    return dt.datetime.now(CST)


def ensure_dir() -> Path:
    WATCHLOG.mkdir(parents=True, exist_ok=True)
    return WATCHLOG


def is_trading_day(d: dt.date | None = None) -> bool:
    """按 data/trade_calendar.parquet（8575 个交易日）判断；缺文件则退化为周一~周五。"""
    global _cal_days
    d = d or now_cst().date()
    try:
        if _cal_days is None:
            import pandas as pd

            cal = pd.read_parquet(CALENDAR)
            _cal_days = set(pd.to_datetime(cal["date"]).dt.date)
        return d in _cal_days
    except Exception:
        return d.weekday() < 5


def session(now: dt.datetime | None = None) -> str:
    """交易时段：closed 非交易日 / pre 集合竞价 / morning 早盘 / lunch 午休 / afternoon 午盘 / eod 已收盘。"""
    now = now or now_cst()
    if not is_trading_day(now.date()):
        return "closed"
    t = now.time()
    if t < dt.time(9, 15):
        return "pre"
    if t < dt.time(11, 31):
        return "morning"
    if t < dt.time(13, 1):
        return "lunch"
    if t <= dt.time(15, 5):
        return "afternoon"
    return "eod"
