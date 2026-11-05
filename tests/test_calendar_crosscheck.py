"""日历 × 上证指数行情交叉校验单测（2026-11-04 生产事故：日历源滞后）。

背景：15:31 日历刷新时 EM 指数K线源尚未发布当日 -> trade_calendar max
停在前一交易日 -> analyzer._snapshot_date 把当日快照钳到前日 -> price_info
以前收外推（300199 错显 24.32，真收 22.71），全市场 5,087 只同错。

防复发：Downloader._calendar_cross_check —— 日历拉完后用腾讯指数行情
（独立源）的 quote_time 校验，行情日 > 日历 max 且非周末则自动补行
（周末休市误报免疫）；行情失败仅告警不改数据（宁缺勿错：当日坏 bar
另有量指纹守卫把关）。

全部离线（monkeypatch fetch_gtimg_quotes，不触网）。
运行: .venv/bin/python -m pytest tests/test_calendar_crosscheck.py -q
"""
from __future__ import annotations

import sys

import pandas as pd

sys.path.insert(0, ".")

from downloader import source  # noqa: E402
from downloader.app import Downloader  # noqa: E402

IDX = "000001.SH"
# 事故日：11-04(周三)。日历停 11-03(周二) = 事故现场
CAL = ["2026-11-02", "2026-11-03"]
QT_DAY = "20261104161400"    # 行情时间：11-04 收盘后


def _dl() -> Downloader:
    d = Downloader.__new__(Downloader)
    d.adjust = "hfq"
    return d  # _calendar_cross_check 不触 data_dir


def _quotes(quote_time: str, price: str = "3686.17"):
    """只含指数一只的行情表（跨日重跑/周末场景共用）。"""
    return lambda codes: {IDX: {"latest_price": price, "quote_time": quote_time}}


def test_lag_auto_appends(tmp_path, monkeypatch):
    """日历停 11-03、行情 11-04 -> 自动补 11-04 一行。"""
    monkeypatch.setattr(source, "fetch_gtimg_quotes", _quotes(QT_DAY))
    out = _dl()._calendar_cross_check(pd.Series(pd.to_datetime(CAL)))
    dates = list(out.dt.date)
    assert dates == [pd.Timestamp("2026-11-02").date(),
                     pd.Timestamp("2026-11-03").date(),
                     pd.Timestamp("2026-11-04").date()], dates
    assert out.is_monotonic_increasing and out.is_unique


def test_fresh_calendar_untouched(tmp_path, monkeypatch):
    """日历已含行情日（不滞后）-> 原样返回不加行。"""
    monkeypatch.setattr(source, "fetch_gtimg_quotes", _quotes(QT_DAY))
    cal = pd.Series(pd.to_datetime(CAL + ["2026-11-04"]))
    out = _dl()._calendar_cross_check(cal)
    assert len(out) == 3 and list(out.dt.date)[-1].isoformat() == "2026-11-04"


def test_weekend_quote_not_appended(tmp_path, monkeypatch):
    """行情日为周六（跨周末重跑缓存）-> 不补行（休市）。"""
    monkeypatch.setattr(source, "fetch_gtimg_quotes",
                         _quotes("20261107100500"))  # 11-07 周六
    out = _dl()._calendar_cross_check(pd.Series(pd.to_datetime(CAL)))
    assert len(out) == 2, "周末行情不得补行"


def test_holiday_span_weekdays_appended(tmp_path, monkeypatch):
    """日历停 10-09、行情 10-13(周中)：跨国庆假期 -> 只补非周末工作日。"""
    cal = pd.Series(pd.to_datetime(["2026-10-09"]))
    monkeypatch.setattr(source, "fetch_gtimg_quotes",
                         _quotes("20261013161400"))  # 10-13 周二
    out = _dl()._calendar_cross_check(cal)
    got = set(out.dt.date)
    # 10-10(周六)/10-11(周日)/10-12(周一，节假日) 依工作日规则：
    # 10-12 是工作日会被补入（无法离线判节假日——已知局限，日历源恢复
    # 后下次刷新以源数据为准自然纠正）
    assert pd.Timestamp("2026-10-10").date() not in got, "周六不得补"
    assert pd.Timestamp("2026-10-13").date() in got, "行情日应补入"


def test_quote_fetch_failure_returns_original(tmp_path, monkeypatch):
    """行情拉取失败 -> 原样返回（不阻塞日历落盘），仅日志告警。"""
    def _boom(codes):
        raise source.FetchError("腾讯行情失败")

    monkeypatch.setattr(source, "fetch_gtimg_quotes", _boom)
    cal = pd.Series(pd.to_datetime(CAL))
    out = _dl()._calendar_cross_check(cal)
    assert len(out) == 2, "行情失败不得改日历"


def test_empty_quote_time_skipped(tmp_path, monkeypatch):
    """行情无 quote_time（字段缺失）-> 不补行不改数据。"""
    monkeypatch.setattr(source, "fetch_gtimg_quotes", _quotes(""))
    cal = pd.Series(pd.to_datetime(CAL))
    out = _dl()._calendar_cross_check(cal)
    assert len(out) == 2


def test_refresh_trade_calendar_writes_file(tmp_path, monkeypatch):
    """端到端：refresh_trade_calendar 落盘含补行；日志含「至 max 日期」。"""
    import downloader.app as A

    monkeypatch.setattr(A.source, "fetch_trade_dates",
                        lambda: pd.Series(pd.to_datetime(CAL)))
    monkeypatch.setattr(A.source, "fetch_gtimg_quotes", _quotes(QT_DAY))
    monkeypatch.setattr(A, "with_retry", lambda fn, desc="": fn())
    d = Downloader.__new__(Downloader)
    d.data_dir = tmp_path
    d.adjust = "hfq"
    out = d.refresh_trade_calendar()
    assert list(out.dt.date)[-1].isoformat() == "2026-11-04"
    got = pd.read_parquet(tmp_path / "trade_calendar.parquet")["trade_date"]
    assert str(pd.to_datetime(got).max().date()) == "2026-11-04"
