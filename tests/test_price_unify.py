"""价格口径归一自测（无需网络）—— 复现 2026-10-26 部分刷新场景。

运行: .venv/bin/python tests/test_price_unify.py

背景：用户盘中只刷新了 stock_list（快照价已是今日）与部分股票的日线，
但 dataset/stock_basic.parquet / daily_hfq.parquet 仍是昨日构建。
旧版所有取价点固定读 dataset/stock_basic（旧价），且市场聚合只看合并表，
导致：自选价 ≠ K线末根 ≠ 交易页浮动盈亏 ≠ 今日数据不出现。

覆盖：
- 快照双源按 mtime 取新（stock_list 新于 dataset/stock_basic）
- price_info 锚点折算：K线末根=快照日 → real=快照价；K线比快照新 → 外推
- kline / watchlist / positions / chips / search 同一口径
- 市场聚合检测合并表滞后 → 回退单股目录聚合，partial 标记半截日
- last_trade_date 指向最后全量日，date_max 含今日
- 无 dataset 目录的最小 fixture（stock_list-only）不受影响
- server /api/trades 元数据走统一价（间接：DataStore.real_price 可用）
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, ".")

from analyzer.datastore import DataStore  # noqa: E402

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


def _mk_daily(root: Path, code: str, dates: list[str], closes: list[float],
              amounts: list[float] | None = None) -> None:
    n = len(dates)
    df = pd.DataFrame({
        "date": pd.to_datetime(dates),
        "code": [code] * n,
        "open": closes,
        "high": closes,
        "low": closes,
        "close": closes,
        "volume": [1000.0] * n,
        "amount": amounts if amounts is not None else [1e8] * n,
        "amplitude": [0.0] * n,
        "pct_chg": [1.0] * n,
        "change": [0.1] * n,
        "turnover": [1.0] * n,
    })
    df.to_parquet(root / "daily" / "hfq" / f"{code}.parquet", index=False)


def _mk_store_dir() -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "daily" / "hfq").mkdir(parents=True)
    return d


def _write_basic(root: Path, rows: list[tuple], name: str = "stock_list.parquet") -> None:
    codes = [r[0] for r in rows]
    df = pd.DataFrame({
        "code": codes,
        "name": [r[1] for r in rows],
        "latest_price": [r[2] for r in rows],
        "pct_chg": [r[3] for r in rows],
        "total_mv": [r[4] for r in rows],
        "float_mv": [r[5] for r in rows],
    })
    df.to_parquet(root / name, index=False)


def _make_partial_refresh_fixture() -> Path:
    """复现 2026-10-26 场景：
    - 2 只股票，K 线到 10-23；
    - dataset/{stock_basic,daily_hfq} 是 10-25 构建的（合并表最大日期 10-23）；
    - stock_list.parquet 是 10-26 盘中刷新的（今日价）；
    - 只有 000001.SZ 的 K 线刷到了 10-26（部分刷新）。
    """
    d = _mk_store_dir()
    # 日历到今日
    pd.DataFrame({"cal_date": pd.to_datetime(
        ["2026-10-22", "2026-10-23", "2026-10-26"])}).to_parquet(d / "trade_calendar.parquet", index=False)

    # 旧合并表 + 旧快照副本（10-25 构建）：000001 旧价 11.0
    os.makedirs(d / "dataset", exist_ok=True)
    _write_basic(d, [
        ("000001.SZ", "平安银行", 11.0, 1.0, 2.0e11, 1.0e11),
        ("600000.SH", "浦发银行", 9.0, -1.0, 2.0e11, 1.0e11),
    ], name="dataset/stock_basic.parquet")
    # 旧合并表：到 10-23
    pd.DataFrame({
        "date": pd.to_datetime(["2026-10-22", "2026-10-23"] * 2),
        "code": ["000001.SZ"] * 2 + ["600000.SH"] * 2,
        "open": [10.0, 10.5, 8.0, 8.5],
        "high": [10.0, 10.5, 8.0, 8.5],
        "low": [10.0, 10.5, 8.0, 8.5],
        "close": [10.0, 10.5, 8.0, 8.5],
        "volume": [100.0] * 4,
        "amount": [1e8] * 4,
    }).to_parquet(d / "dataset" / "daily_hfq.parquet", index=False)

    # 单股文件：000001 刷到了 10-26（今日，部分刷新），600000 仍是 10-23
    _mk_daily(d, "000001.SZ", ["2026-10-22", "2026-10-23", "2026-10-26"], [10.0, 10.5, 10.6])
    _mk_daily(d, "600000.SH", ["2026-10-22", "2026-10-23"], [8.0, 8.5])

    # 新 stock_list（10-26 快照）：000001 今日价 10.6，600000 仍是快照价 8.5
    _write_basic(d, [
        ("000001.SZ", "平安银行", 10.6, 0.95, 2.1e11, 1.05e11),
        ("600000.SH", "浦发银行", 8.5, 0.0, 2.0e11, 1.0e11),
    ])

    # mtime：stock_list = 现在（10-26 盘中），dataset = 10-25
    now = pd.Timestamp.now().timestamp()
    os.utime(d / "stock_list.parquet", (now, now))
    old = (pd.Timestamp("2026-10-25 11:58:00").timestamp(),) * 2
    os.utime(d / "dataset" / "stock_basic.parquet", old)
    os.utime(d / "dataset" / "daily_hfq.parquet", old)
    return d


# ---------------------------------------------------------------- test cases

def t_fresher_snapshot_wins() -> None:
    d = _make_partial_refresh_fixture()
    s = DataStore(str(d))
    # 000001 应取到新快照价 10.6，而不是 dataset 副本里的 11.0
    assert s._basic_path.name == "stock_list.parquet", "应选中更新的 stock_list.parquet"
    assert s.real_price("000001.SZ") == 10.6, f"real_price 应为新快照价 10.6，实际 {s.real_price('000001.SZ')}"


def t_price_info_anchor() -> None:
    d = _make_partial_refresh_fixture()
    s = DataStore(str(d))
    # 000001: K线末根 10-26 == 快照日 → real=快照价，factor=快照价/hfq末根
    pi = s.price_info("000001.SZ")
    assert pi["real"] == 10.6 and pi["snap_date"] == "2026-10-26" and pi["last_date"] == "2026-10-26"
    assert abs(pi["factor"] - 10.6 / 10.6) < 1e-9  # 快照价==hfq末根收盘的 fixture 里正好 1.0
    # 600000: K线末根 10-23 < 快照日 → 锚在末根，real=快照价（不外推）
    pi2 = s.price_info("600000.SH")
    assert pi2["real"] == 8.5 and pi2["snap_date"] == "2026-10-26" and pi2["last_date"] == "2026-10-23"


def t_kline_watchlist_positions_same_price() -> None:
    d = _make_partial_refresh_fixture()
    s = DataStore(str(d))
    k = s.kline("000001.SZ")
    assert k["close"][-1] == 10.6, "K线末根（折算后）应等于快照价"
    assert k["real_price"] == 10.6 and k["price_unit"] == "qfq"

    # watchlist：先造一份用户自选
    u = "tester"
    (d / "users" / u).mkdir(parents=True, exist_ok=True)
    s.watchlist_add(u, "000001.SZ")
    wl = s.watchlist(u)
    row = [w for w in wl if w["code"] == "000001.SZ"][0]
    assert row["latest_price"] == 10.6, f"自选价应为统一真实价 10.6，实际 {row['latest_price']}"

    # positions：交易后市值/浮盈用统一价
    s.trade_add(u, "000001.SZ", "2026-10-20", "buy", 10.0, 100)
    pos = s.positions(u)["000001.SZ"]
    assert pos["held"] == 100 and abs(pos["avg_cost"] - 10.0) < 1e-9
    assert abs(pos["unrealized"] - (10.6 - 10.0) * 100) < 1e-6, \
        f"浮盈应用统一价 10.6，实际对应价 {pos['unrealized'] / 100 + 10.0}"

    # search 同口径
    r = s.search_stocks("000001")[0]
    assert r["latest_price"] == 10.6


def t_chips_unified_factor() -> None:
    d = _make_partial_refresh_fixture()
    s = DataStore(str(d))
    ch = s.chips("000001.SZ")
    assert ch is not None
    assert abs(ch["last_close"] - 10.6) < 1e-6, f"筹码 last_close 应折算到真实价 10.6，实际 {ch['last_close']}"
    assert ch["price_unit"] == "qfq"
    # stats 的 avg_cost 也应在真实价量纲
    assert 0 < ch["stats"]["avg_cost"] <= 10.6 + 1e-6


def t_market_partial_day() -> None:
    d = _make_partial_refresh_fixture()
    s = DataStore(str(d))
    # 合并表（10-23）< 单股目录（10-26）→ 回退单股聚合，10-26 行存在且标记 partial
    assert s._market_from_stocks is True, "检测到合并表滞后应回退单股目录聚合"
    assert s.meta["date_max"] == "2026-10-26", f"date_max 应含今日，实际 {s.meta['date_max']}"
    assert s.meta["last_trade_date"] == "2026-10-23", \
        f"last_trade_date 应为最后全量日 10-23（10-26 只有 1/2 股票），实际 {s.meta['last_trade_date']}"
    row = s.market.loc[pd.Timestamp("2026-10-26")]
    assert bool(row["partial"]) is True and int(row["n_codes"]) == 1
    # 市场序列接口也包含今日
    ms = s.market_series()
    assert "2026-10-26" in ms["dates"]


def t_no_dataset_dir() -> None:
    """最小 fixture（只有 stock_list + 单股日线，无 dataset/）不受影响。"""
    d = _mk_store_dir()
    pd.DataFrame({"cal_date": pd.to_datetime(["2026-10-23"])}).to_parquet(
        d / "trade_calendar.parquet", index=False)
    _write_basic(d, [("000001.SZ", "平安银行", 10.4, 1.0, 2e11, 1e11)])
    _mk_daily(d, "000001.SZ", ["2026-10-23"], [10.4])
    s = DataStore(str(d))
    assert s.real_price("000001.SZ") == 10.4
    k = s.kline("000001.SZ")
    assert k["close"][-1] == 10.4 and k["price_unit"] == "qfq"
    assert s.meta["last_trade_date"] == "2026-10-23" and s.meta["n_stocks_last_day"] == 1
    # 单日数据 partial=False（当日即为全量）
    assert not bool(s.market["partial"].iloc[-1])


def t_mtime_stability_under_partial_write() -> None:
    """save_parquet 非原子：watcher 依赖「两次采样一致」——验证 reload 在
    半写文件上失败时不抛出（store.reload 内部 try）——这里验证 DataStore
    层面：损坏的 stock_list 应回退到 dataset 副本，而不是崩溃退出。
    回退后价格 = 旧快照价 11.0（锚 10-23 收盘 10.5，外推到 10-26 收盘 10.6）。"""
    d = _make_partial_refresh_fixture()
    s = DataStore(str(d))
    # 直接覆盖写一个坏文件，然后重新构造：应回退 dataset 副本（旧价 11.0）
    (d / "stock_list.parquet").write_bytes(b"not a parquet")
    s2 = DataStore(str(d))
    assert s2._basic_path.name == "stock_basic.parquet", "坏 stock_list 应回退到 dataset 副本"
    expect = 11.0 * 10.6 / 10.5  # 旧快照 11.0 锚在 10-23，按 hfq 收盘比外推到 10-26
    assert s2.real_price("000001.SZ") is not None and abs(s2.real_price("000001.SZ") - expect) < 1e-9


def t_snapshot_newer_than_kline() -> None:
    """快照比 K 线新（只刷了快照没刷日线）：锚在 K 线末根，real=快照价原样。"""
    d = _mk_store_dir()
    pd.DataFrame({"cal_date": pd.to_datetime(["2026-10-22", "2026-10-23", "2026-10-26"])}).to_parquet(
        d / "trade_calendar.parquet", index=False)
    _write_basic(d, [("000001.SZ", "平安银行", 10.6, 0.95, 2.1e11, 1.05e11)])
    _mk_daily(d, "000001.SZ", ["2026-10-22", "2026-10-23"], [10.0, 10.5])
    # mtime 设为今日 10-26 15:00
    now = pd.Timestamp("2026-10-26 15:00:00").timestamp()
    os.utime(d / "stock_list.parquet", (now, now))
    s = DataStore(str(d))
    pi = s.price_info("000001.SZ")
    # 快照价 10.6，K线末根 10.5（10-23）：real=10.6（快照价直接用），factor=10.6/10.5
    assert pi["real"] == 10.6
    assert abs(pi["factor"] - 10.6 / 10.5) < 1e-9
    k = s.kline("000001.SZ")
    assert k["close"][-1] == 10.6, "K线末根应折算到快照价"


def t_kline_newer_than_snapshot() -> None:
    """K 线比快照新（快照后只刷了 K 线）：按 hfq 收盘比外推到最新交易日。"""
    d = _mk_store_dir()
    pd.DataFrame({"cal_date": pd.to_datetime(["2026-10-22", "2026-10-23", "2026-10-26"])}).to_parquet(
        d / "trade_calendar.parquet", index=False)
    # 快照（10-23 收盘后）：价 10.5
    _write_basic(d, [("000001.SZ", "平安银行", 10.5, 0.5, 2.1e11, 1.05e11)])
    now = pd.Timestamp("2026-10-23 16:00:00").timestamp()
    os.utime(d / "stock_list.parquet", (now, now))
    # K 线刷到了 10-26：10-23 收 10.5，10-26 收 11.55（+10%）
    _mk_daily(d, "000001.SZ", ["2026-10-22", "2026-10-23", "2026-10-26"], [10.0, 10.5, 11.55])
    s = DataStore(str(d))
    pi = s.price_info("000001.SZ")
    # 外推：10.5 × 11.55/10.5 = 11.55
    assert abs(pi["real"] - 11.55) < 1e-9, f"应外推到 11.55，实际 {pi['real']}"
    assert abs(pi["factor"] - 10.5 / 10.5) < 1e-9  # factor = 快照价/快照日收盘 = 1
    k = s.kline("000001.SZ")
    assert abs(k["close"][-1] - 11.55) < 1e-9


def t_board_rank_partial_protection() -> None:
    """板块排行等按 market 索引取日期的调用在部分刷新下不越界。"""
    d = _make_partial_refresh_fixture()
    s = DataStore(str(d))
    end = s.meta["date_max"]  # 2026-10-26（前端默认用 date_max / last_trade_date）
    ms = s.market_series(end=end)
    assert ms["dates"][-1] == "2026-10-26"


def main() -> int:
    for name, fn in [
        ("快照双源取新（部分刷新场景）", t_fresher_snapshot_wins),
        ("price_info 锚点折算", t_price_info_anchor),
        ("kline/自选/持仓/搜索 同口径", t_kline_watchlist_positions_same_price),
        ("筹码统一折算", t_chips_unified_factor),
        ("市场聚合部分日标记", t_market_partial_day),
        ("无 dataset 目录 fixture", t_no_dataset_dir),
        ("损坏快照回退", t_mtime_stability_under_partial_write),
        ("快照新于K线（锚末根）", t_snapshot_newer_than_kline),
        ("K线新于快照（外推）", t_kline_newer_than_snapshot),
        ("市场序列含部分日", t_board_rank_partial_protection),
    ]:
        check(name, fn)
    print(f"\n===== 汇总: {PASSED} PASS / {FAILED} FAIL =====")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
