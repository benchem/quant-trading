"""自选股（analyzer datastore.watchlist*）与 60 分钟聚合（minute.to_hourly）自测（无需网络）。

运行: .venv/bin/python tests/test_watchlist.py

覆盖：
- watchlist add / reorder / remove 往返与持久化（网页拖拽排序的后端）
- reorder 边界：未知代码忽略、缺项追加在末尾、重复去重
- minute.to_hourly：1 分钟线 -> 60 分钟K线（四档时段、OHLC 聚合、跨日排序）
"""
from __future__ import annotations

import sys
import tempfile
import traceback
from pathlib import Path

import pandas as pd

sys.path.insert(0, ".")

from analyzer.datastore import DataStore  # noqa: E402
from analyzer.minute import to_hourly  # noqa: E402

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


def _make_store(tmp: Path) -> DataStore:
    """最小可用 data 目录：两只股票的日线 + 基础信息表。"""
    (tmp / "daily" / "hfq").mkdir(parents=True)
    for code, name in (("600000.SH", "浦发银行"), ("000001.SZ", "平安银行")):
        pd.DataFrame({
            "date": pd.to_datetime(["2026-10-20", "2026-10-21", "2026-10-22"]),
            "code": [code] * 3,
            "open": [10.0, 10.1, 10.2],
            "high": [10.3, 10.4, 10.5],
            "low": [9.9, 10.0, 10.1],
            "close": [10.2, 10.3, 10.4],
            "volume": [1000, 1100, 1200],
            "amount": [1e7, 1.1e7, 1.2e7],
        }).to_parquet(tmp / "daily" / "hfq" / f"{code}.parquet", index=False)
    pd.DataFrame({
        "code": ["600000.SH", "000001.SZ"],
        "name": ["浦发银行", "平安银行"],
        "latest_price": [10.4, 12.0],
        "pct_chg": [0.97, -0.5],
        "total_mv": [1e10, 2e11],
    }).to_parquet(tmp / "stock_list.parquet", index=False)
    pd.DataFrame(
        {"cal_date": pd.to_datetime(["2026-10-20", "2026-10-21", "2026-10-22"])}
    ).to_parquet(tmp / "trade_calendar.parquet", index=False)
    return DataStore(tmp)


def t_watchlist_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        store = _make_store(Path(td))
        assert store.watchlist(None) == []

        # 加入（按加入顺序）
        for c in ("600000.SH", "000001.SZ"):
            codes, added = store.watchlist_add(None, c)
            assert added
        assert [w["code"] for w in store.watchlist(None)] == ["600000.SH", "000001.SZ"]
        # 重复加入幂等
        codes, added = store.watchlist_add(None, "600000.SH")
        assert not added and len(codes) == 2

        # 重排：完全列表
        assert store.watchlist_reorder(None, ["000001.SZ", "600000.SH"]) == ["000001.SZ", "600000.SH"]
        assert [w["code"] for w in store.watchlist(None)] == ["000001.SZ", "600000.SH"]

        # 重排：未知代码忽略 + 小写自动转大写 + 重复去重
        assert store.watchlist_reorder(
            None, ["600000.sh", "999999.SH", "000001.SZ", "600000.SH"]
        ) == ["600000.SH", "000001.SZ"]

        # 重排：缺项按原相对顺序追加在末尾（不丢股）
        assert store.watchlist_reorder(None, ["000001.SZ"]) == ["000001.SZ", "600000.SH"]
        # 空列表 = 全部追加在末尾（等于原顺序）
        assert store.watchlist_reorder(None, []) == ["000001.SZ", "600000.SH"]

        # 持久化：新实例读回同一顺序
        assert [w["code"] for w in DataStore(Path(td)).watchlist(None)] == ["000001.SZ", "600000.SH"]

        # 移除
        codes, removed = store.watchlist_remove(None, "000001.SZ")
        assert removed and codes == ["600000.SH"]
        assert [w["code"] for w in DataStore(Path(td)).watchlist(None)] == ["600000.SH"]


def t_to_hourly():
    # 两个交易日 × 四档时段的 1 分钟线（打乱顺序输入，验证内部排序）
    rows = []
    def bar(day, hm, o, c, v=100, a=1000.0):
        rows.append({
            "datetime": pd.Timestamp(f"{day} {hm}:00"),
            "code": "600000.SH",
            "open": o, "high": o + 0.2, "low": o - 0.2, "close": c,
            "volume": v, "amount": a,
        })
    # 第一天：09:3x -> 09:30 档；10:4x -> 10:30 档；13:1x -> 13:00 档；14:5x -> 14:00 档
    bar("2026-10-20", "09:31", 10.0, 10.1)
    bar("2026-10-20", "09:59", 10.1, 10.2)
    bar("2026-10-20", "10:31", 10.2, 10.3)
    bar("2026-10-20", "11:29", 10.3, 10.4)   # 上午最后一根 -> 10:30 档
    bar("2026-10-20", "13:01", 10.4, 10.5)
    bar("2026-10-20", "14:01", 10.5, 10.6)
    bar("2026-10-20", "14:59", 10.6, 10.7)   # -> 14:00 档
    # 第二天：仅上午两档
    bar("2026-10-21", "09:35", 10.8, 10.9)
    bar("2026-10-21", "10:35", 11.0, 11.1)
    df = pd.DataFrame(rows).sample(frac=1.0, random_state=7).reset_index(drop=True)

    h = to_hourly(df)
    assert list(h["label"]) == [
        "2026-10-20 09:30", "2026-10-20 10:30", "2026-10-20 13:00", "2026-10-20 14:00",
        "2026-10-21 09:30", "2026-10-21 10:30",
    ], list(h["label"])
    b = h.iloc[0]
    assert b["open"] == 10.0 and b["close"] == 10.2, (b["open"], b["close"])
    assert abs(b["high"] - 10.3) < 1e-9 and abs(b["low"] - 9.8) < 1e-9, (b["high"], b["low"])  # max/min(开±0.2)
    assert b["volume"] == 200 and b["amount"] == 2000.0, (b["volume"], b["amount"])
    b2 = h.iloc[1]
    assert b2["open"] == 10.2 and b2["close"] == 10.4   # 10:30 档含 11:29 最后一根
    assert h.iloc[2]["slot"] == "13:00" and h.iloc[3]["slot"] == "14:00"
    # 空输入
    assert to_hourly(pd.DataFrame(columns=df.columns)).empty


if __name__ == "__main__":
    check("自选股 add/reorder/remove 往返", t_watchlist_roundtrip)
    check("1分钟线 -> 60分钟K线聚合", t_to_hourly)
    print(f"\n{PASSED} 通过 / {FAILED} 失败")
    sys.exit(1 if FAILED else 0)
