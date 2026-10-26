"""数据守卫单测：腾讯复权键回退 / 无交易日窗口裁剪 / 合并前单位断裂拦截 / 日历过期调度。

背景（2026-10-25 事故）: 腾讯源对无交易日窗口回退返回最新一根【不复权】bar，
曾被静默当作 hfq 数据合并，导致约 2100+ 只深市股票末根 K 线单位断裂、前复权
折算因子塌缩为 1、图表整体显示后复权价。本文件固化四层防御：
  1. _tx_kline_page: 复权键(hfqday/qfqday)缺失且响应含 day 键时必须报错，不得退回原始价
  2. _tx_klines: 返回行必须裁剪到请求的 [start, end] 区间（杀掉「回退」数据）
  3. _reject_unit_anomaly: 合并前拦截单位断裂（新数据引入 >32% 不可能跳变）
  4. next_fire/_is_trading_day: 日历过期(止于历史日期)时退化为工作日，调度不死循环

全部离线（mock 网络层），不触网。运行: .venv/bin/python tests/test_data_guards.py
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import traceback
from pathlib import Path

import pandas as pd

sys.path.insert(0, ".")

from analyzer import updates  # noqa: E402
from analyzer.updates import next_fire  # noqa: E402
from downloader import source  # noqa: E402
from downloader.app import _reject_unit_anomaly  # noqa: E402
from downloader.source import FetchError  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, fn) -> bool:
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


# ----------------------------------------------------------------- 替身


class FakeClient:
    """替身 _client.get_json：按脚本顺序返回预设响应。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get_json(self, url, params):
        self.calls.append((url, params))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def with_fake_client(responses, fn):
    old = source._client
    old_idx = source._tx_kline_url_idx[0]
    fake = FakeClient(responses)
    source._client = fake
    try:
        return fn(fake)
    finally:
        source._client = old
        source._tx_kline_url_idx[0] = old_idx


def make_daily(dates, closes):
    return pd.DataFrame({"date": pd.to_datetime(dates), "close": closes})


# ----------------------------------------------------------------- 用例


def t_tx_missing_fq_key_must_raise():
    """复权请求下响应只有 day 键 -> 必须报错，绝不能返回原始价数据。"""
    raw = [["2026-10-23", "25.11", "28.77", "28.90", "24.26", "290671"]]

    def run(_=None):
        return source._tx_kline_page(
            "sz300687", "hfq", dt.date(2026, 10, 24), dt.date(2026, 10, 25))

    # 三个主机都返回同样畸形响应 -> 最终抛 FetchError（而不是返回原始价）
    resp = {"code": 0, "data": {"sz300687": {"day": raw}}}
    try:
        with_fake_client([resp, resp, resp], run)
    except FetchError as e:
        assert "hfqday" in str(e), f"报错信息应指明缺失复权键: {e}"
    else:
        raise AssertionError("复权键缺失时未报错，仍会静默混入不复权数据！")

    # 对照: 正常返回 hfqday 时照常工作
    ok = {"code": 0, "data": {"sz300687": {"hfqday": [["2026-10-23"] + ["1"] * 5]}}}
    rows = with_fake_client([ok, ok, ok], run)
    assert rows and rows[0][0] == "2026-10-23", rows

    # 对照: 两个键都没有(空窗口正常态) -> 返回空列表，由上层当 uptodate 处理
    empty = with_fake_client([{"code": 0, "data": {}}, {"code": 0, "data": {}},
                              {"code": 0, "data": {}}], run)
    assert empty == []


def t_tx_klines_window_filter():
    """腾讯对无交易日窗口「回退」返回的区间外数据必须被裁掉。"""
    # 请求 10-24~10-25(周末)，腾讯回退返回最新一根 10-23 bar
    back = [["2026-10-23", "25.11", "28.77", "28.90", "24.26", "290671"]]

    def run(_=None):
        return source._tx_klines("300687.SZ", "20261024", "20261025", "hfq")

    rows = with_fake_client([{"data": {"sz300687": {"hfqday": back}}}] * 3, run)
    assert rows == [], f"区间外(10-23)数据未被裁剪: {rows}"

    # 对照: 请求 10-20~10-23, 返回里混入区间前的 10-16 -> 只保留区间内 4 根
    mixed = [[f"2026-10-{d:02d}", "1", "1", "1", "1", "1"]
             for d in (16, 20, 21, 22, 23)]

    def run2(_=None):
        return source._tx_klines("300687.SZ", "20261020", "20261023", "hfq")

    rows = with_fake_client([{"data": {"sz300687": {"hfqday": mixed}}}] * 3, run2)
    assert [r[0] for r in rows] == [f"2026-10-{d:02d}" for d in (20, 21, 22, 23)], rows


def t_reject_unit_anomaly():
    """合并前拦截: 新 bar 真实价 28.77 接到 hfq 序列 111.65 之后(-74%)必须拒绝。"""
    old = make_daily([f"2026-10-{d:02d}" for d in range(1, 23)],
                     [100 + i * 0.5 for i in range(22)])
    old.loc[old.index[-1], "close"] = 111.652  # 10-22 后复权收盘
    bad = make_daily(["2026-10-23"], [28.77])  # 腾讯回退的不复权 bar
    reason = _reject_unit_anomaly(old, bad)
    assert reason and "单位断裂" in reason and "74" in reason, reason

    # 正常新增 bar(+3%) 放行
    good = make_daily(["2026-10-23"], [115.0])
    assert _reject_unit_anomaly(old, good) == ""

    # 同日刷新(覆盖已有日期)且数值正常 -> 放行
    refresh = make_daily(["2026-10-22"], [111.9])
    assert _reject_unit_anomaly(old, refresh) == ""

    # 只检查新数据引起的跳变: 旧数据内部已有断裂时不背锅、放行正常增量
    old_broken = old.copy()
    old_broken.loc[old_broken.index[5], "close"] = 50.0  # 历史遗留断裂
    assert _reject_unit_anomaly(old_broken, good) == ""

    # 新股不校验(旧数据 < 6 根): 上市初期无涨跌停
    young = make_daily(["2026-10-20"] * 0 + [f"2026-10-{d}" for d in (18, 19, 20)],
                       [10.0, 11.0, 13.0])
    ipo_bar = make_daily(["2026-10-23"], [9.0])
    assert _reject_unit_anomaly(young, ipo_bar) == ""

    # 覆盖式修复: 新数据同日重写断裂值本身也允许(比如降级后的修复重拉)
    fix = make_daily(["2026-10-23"], [127.674])
    assert _reject_unit_anomaly(old, fix) == ""


def t_reject_via_update_stock_one():
    """端到端: _update_stock_one 收到断裂数据时保留旧文件并返回 skipped。"""
    from downloader.app import Downloader

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        d = Downloader.__new__(Downloader)
        d.data_dir = root
        d.adjust = "hfq"
        d.start = "20200101"
        d.workers = 1
        d.rate = 1
        code = "300687.SZ"
        p = root / "daily" / "hfq" / f"{code}.parquet"
        p.parent.mkdir(parents=True)
        dates = pd.date_range("2026-09-01", "2026-10-22", freq="B")
        old = pd.DataFrame({"date": dates, "close": [110 + i * 0.05 for i in range(len(dates))]})
        old.to_parquet(p, index=False)

        # 打桩 fetch_daily 返回「真实价」bar
        real_fetch = source.fetch_daily

        def fake_fetch(wind_code, start, end, adjust):
            assert adjust == "hfq"
            return pd.DataFrame({"date": [pd.Timestamp("2026-10-23")],
                                 "close": [28.77]})
        source.fetch_daily = fake_fetch
        try:
            c, status, msg = d._update_stock_one(code, "赛意信息", "20261025", False)
        finally:
            source.fetch_daily = real_fetch
        assert status == "skipped", (status, msg)
        assert "单位断裂" in msg, msg
        after = pd.read_parquet(p)
        assert float(after["close"].iloc[-1]) > 100, "旧数据被断裂数据覆盖！"
        assert len(after) == len(old)


def t_next_fire_stale_calendar():
    """日历止于历史日期(数据源故障)时: 调度不死循环、不抛 date value out of range。"""
    with tempfile.TemporaryDirectory() as td:
        cal = pd.DataFrame({"trade_date": pd.date_range("2020-01-01", "2026-10-23", freq="B")})
        cal.to_parquet(Path(td) / "trade_calendar.parquet", index=False)
        # 周日 19:20 (事故现场), 日历止于上个周五 10-23
        now = dt.datetime(2026, 10, 25, 19, 20)
        fire, later = next_fire(now, td)
        assert later is True
        assert fire.date() == dt.date(2026, 10, 26) and fire.weekday() == 0, fire  # 下周一
        # 90 天寻日硬上限: 日历止于远久以前 + 全部未来日期非工作日的极端构造不炸
        pd.DataFrame({"trade_date": pd.date_range("2020-01-01", "2026-01-02", freq="B")}) \
            .to_parquet(Path(td) / "trade_calendar.parquet", index=False)
        fire2, _ = next_fire(dt.datetime(2026, 10, 25, 19, 20), td)
        assert fire2.year == 2026 and fire2 > now, fire2
        # 对照: 完整日历(覆盖未来)时行为不变 -> 找到下一个真实交易日
        pd.DataFrame({"trade_date": pd.date_range("2026-10-01", "2026-12-31", freq="B")}) \
            .to_parquet(Path(td) / "trade_calendar.parquet", index=False)
        fire3, _ = next_fire(dt.datetime(2026, 10, 25, 19, 20), td)
        assert fire3.date() == dt.date(2026, 10, 26), fire3
        # 交易日盘中(15:00) -> 今日 15:30 触发
        fire4, today = next_fire(dt.datetime(2026, 10, 26, 15, 0), td)
        assert today is False and fire4 == dt.datetime(2026, 10, 26, 15, 30), fire4
        # _is_trading_day: 日历未覆盖的未来日期退化为工作日判断
        assert updates._is_trading_day(td, dt.date(2026, 10, 26)) is True
        assert updates._is_trading_day(td, dt.date(2026, 10, 31)) is False  # 周六
        # 日历内的非交易日仍为 False
        assert updates._is_trading_day(td, dt.date(2026, 10, 25)) is False  # 周日


if __name__ == "__main__":
    t_tx_missing_fq_key_must_raise.__name__ = "腾讯源: 复权键缺失必须报错(不退回原始价)"
    t_tx_klines_window_filter.__name__ = "腾讯源: 无交易日窗口回退数据必须裁剪"
    t_reject_unit_anomaly.__name__ = "合并守卫: 单位断裂拦截(纯函数)"
    t_reject_via_update_stock_one.__name__ = "合并守卫: _update_stock_one 端到端"
    t_next_fire_stale_calendar.__name__ = "调度: 日历过期不死循环"
    for fn in (t_tx_missing_fq_key_must_raise, t_tx_klines_window_filter,
               t_reject_unit_anomaly, t_reject_via_update_stock_one,
               t_next_fire_stale_calendar):
        check(fn.__name__, fn)
    print(f"\n{'=' * 40}\n结果: {PASSED} 通过, {FAILED} 失败")
    sys.exit(1 if FAILED else 0)
