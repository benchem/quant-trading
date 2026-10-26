"""自选股买卖流水（analyzer datastore.trades*）自测（无需网络）。

运行: .venv/bin/python tests/test_trades.py

覆盖：
- position_of 移动加权平均（持仓/均价/已实现/浮动盈亏）
- trade_add 参数校验与落盘、trades 过滤、trade_remove 往返
"""
from __future__ import annotations

import sys
import tempfile
import traceback
from pathlib import Path

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


def _make_store(tmp: Path) -> DataStore:
    """最小可用 data 目录：一只股票的 3 根日线 + 基础信息表。"""
    (tmp / "daily" / "hfq").mkdir(parents=True)
    daily = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-10-20", "2026-10-21", "2026-10-22"]),
            "code": ["600000.SH"] * 3,
            "open": [10.0, 10.1, 10.2],
            "high": [10.3, 10.4, 10.5],
            "low": [9.9, 10.0, 10.1],
            "close": [10.2, 10.3, 10.4],
            "volume": [1000, 1100, 1200],
            "amount": [1e7, 1.1e7, 1.2e7],
        }
    )
    daily.to_parquet(tmp / "daily" / "hfq" / "600000.SH.parquet", index=False)
    pd.DataFrame(
        {
            "code": ["600000.SH"],
            "name": ["浦发银行"],
            "latest_price": [10.4],
            "pct_chg": [0.97],
            "total_mv": [1e10],
        }
    ).to_parquet(tmp / "stock_list.parquet", index=False)
    # 交易日历：避免走 _load_calendar 里依赖 self.market 的兜底分支
    pd.DataFrame(
        {"cal_date": pd.to_datetime(["2026-10-20", "2026-10-21", "2026-10-22"])}
    ).to_parquet(tmp / "trade_calendar.parquet", index=False)
    return DataStore(tmp)


def t_position_math():
    pos_of = DataStore.position_of
    # 空
    p = pos_of([], 10.0)
    assert p["held"] == 0 and p["avg_cost"] is None and p["n_trades"] == 0, p
    # 单笔买入
    p = pos_of([{"date": "2026-10-20", "created": "a", "side": "buy", "price": 10.0, "shares": 100}], 11.0)
    assert p["held"] == 100 and p["avg_cost"] == 10.0 and p["unrealized"] == 100.0, p
    # 加仓摊薄/抬升均价
    p = pos_of(
        [
            {"date": "2026-10-20", "created": "a", "side": "buy", "price": 10.0, "shares": 100},
            {"date": "2026-10-21", "created": "b", "side": "buy", "price": 12.0, "shares": 100},
        ],
        13.0,
    )
    assert p["held"] == 200 and abs(p["avg_cost"] - 11.0) < 1e-9, p
    assert abs(p["unrealized"] - (13.0 - 11.0) * 200) < 1e-9, p
    # 卖出产生已实现盈亏（按卖出时点均价）
    p = pos_of(
        [
            {"date": "2026-10-20", "created": "a", "side": "buy", "price": 10.0, "shares": 100},
            {"date": "2026-10-21", "created": "b", "side": "buy", "price": 12.0, "shares": 100},
            {"date": "2026-10-22", "created": "c", "side": "sell", "price": 14.0, "shares": 100},
        ],
        13.0,
    )
    assert p["held"] == 100 and p["avg_cost"] == 11.0, p
    assert abs(p["realized"] - (14.0 - 11.0) * 100) < 1e-9, p
    assert abs(p["unrealized"] - (13.0 - 11.0) * 100) < 1e-9, p
    # 超卖不产生负持仓（按可卖数量计已实现）
    p = pos_of(
        [
            {"date": "2026-10-20", "created": "a", "side": "buy", "price": 10.0, "shares": 100},
            {"date": "2026-10-21", "created": "b", "side": "sell", "price": 9.0, "shares": 500},
        ],
        None,
    )
    assert p["held"] == 0 and p["avg_cost"] is None, p
    assert abs(p["realized"] - (9.0 - 10.0) * 100) < 1e-9, p
    # 乱序输入按日期排序
    p = pos_of(
        [
            {"date": "2026-10-21", "created": "b", "side": "buy", "price": 12.0, "shares": 100},
            {"date": "2026-10-20", "created": "a", "side": "buy", "price": 10.0, "shares": 100},
        ],
        None,
    )
    assert p["held"] == 200 and abs(p["avg_cost"] - 11.0) < 1e-9, p


def t_store_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        store = _make_store(Path(td))
        assert store.trades(None) == []
        assert store.trades(None, "600000.SH") == []

        # 校验
        bad = [
            ("600000", "2026-10-20", "buy", 10.0, 100),      # 代码格式
            ("600000.SH", "2026-99-20", "buy", 10.0, 100),   # 日期格式
            ("600000.SH", "2026-10-20", "hold", 10.0, 100),  # 方向
            ("600000.SH", "2026-10-20", "buy", 0, 100),      # 价格
            ("600000.SH", "2026-10-20", "buy", 10.0, 10.5),  # 股数非整数
            ("600000.SH", "2026-10-20", "buy", 10.0, 0),     # 股数非正
        ]
        for args in bad:
            try:
                store.trade_add(None, *args)
                raise AssertionError(f"应当报错: {args}")
            except ValueError:
                pass
        assert store.trades(None) == [], "校验失败的记录不应落盘"

        # 正常添加
        ts, r1 = store.trade_add(None, "600000.SH", "2026-10-20", "buy", 10.05, 100, "首仓")
        ts, r2 = store.trade_add(None, "600000.SH", "2026-10-22", "sell", 11.0, 50, "减仓")
        assert len(ts) == 2 and r1["code"] == "600000.SH" and r2["side"] == "sell"
        assert r1["id"] and r2["id"] and r1["id"] != r2["id"]
        # 排序
        assert [t["date"] for t in store.trades(None)] == ["2026-10-20", "2026-10-22"]
        # 过滤
        assert len(store.trades(None, "600000.SH")) == 2
        assert store.trades(None, "000001.SZ") == []
        # 持久化 + 重新加载
        store2 = DataStore(td)
        assert len(store2.trades(None)) == 2
        assert store2.trades(None)[0]["note"] == "首仓"
        assert abs(store2.trades(None)[0]["price"] - 10.05) < 1e-9
        # 持仓汇总（latest_price=10.4）
        pos = store2.positions(None)
        assert pos["600000.SH"]["held"] == 50
        assert abs(pos["600000.SH"]["avg_cost"] - 10.05) < 1e-9
        assert abs(pos["600000.SH"]["realized"] - (11.0 - 10.05) * 50) < 1e-6
        assert abs(pos["600000.SH"]["unrealized"] - (10.4 - 10.05) * 50) < 1e-6
        # 删除
        ts, removed = store2.trade_remove(None, "600000.SH", r1["id"])
        assert removed and len(ts) == 1
        assert DataStore(td).trades(None)[0]["side"] == "sell"
        # 删除不存在的
        ts, removed = store2.trade_remove(None, "600000.SH", "no-such-id")
        assert not removed and len(ts) == 1
        # 其他股票的 id 不匹配
        ts, removed = store2.trade_remove(None, "000001.SZ", r2["id"])
        assert not removed and len(ts) == 1


def t_store_update():
    with tempfile.TemporaryDirectory() as td:
        store = _make_store(Path(td))
        ts, r1 = store.trade_add(None, "600000.SH", "2026-10-20", "buy", 10.05, 100, "首仓")
        # 部分字段更新（价格+备注）
        ts, upd = store.trade_update(None, "600000.SH", r1["id"], price=10.20, note="改价")
        assert upd["id"] == r1["id"] and upd["price"] == 10.20 and upd["note"] == "改价", upd
        assert upd["date"] == "2026-10-20" and upd["shares"] == 100, "未传字段不应变"
        # 更新日期/方向/股数
        ts, upd = store.trade_update(None, "600000.SH", r1["id"], date="2026-10-21", side="sell", shares=50)
        assert upd["date"] == "2026-10-21" and upd["side"] == "sell" and upd["shares"] == 50, upd
        # 清空备注
        ts, upd = store.trade_update(None, "600000.SH", r1["id"], note="")
        assert upd["note"] == "", upd
        # 排序随日期更新
        assert store.trades(None)[0]["date"] == "2026-10-21"
        # 持久化
        assert DataStore(td).trades(None)[0]["side"] == "sell"
        # 校验：非法值拒绝且不落盘
        before = store.trades(None)[0]
        for kwargs in ({"price": -1}, {"shares": 10.5}, {"side": "hold"}, {"date": "2026-99-01"}):
            try:
                store.trade_update(None, "600000.SH", r1["id"], **kwargs)
                raise AssertionError(f"应当报错: {kwargs}")
            except ValueError:
                pass
        assert store.trades(None)[0] == before, "非法更新不应改动记录"
        # 不存在的 id
        try:
            store.trade_update(None, "600000.SH", "no-such-id", price=1)
            raise AssertionError("应当报错")
        except ValueError:
            pass
        # 其他股票的 id 不匹配
        try:
            store.trade_update(None, "000001.SZ", r1["id"], price=1)
            raise AssertionError("应当报错")
        except ValueError:
            pass


def t_store_fee():
    with tempfile.TemporaryDirectory() as td:
        store = _make_store(Path(td))
        # 不带 fee：记录无实际费用
        ts, r1 = store.trade_add(None, "600000.SH", "2026-10-20", "buy", 10.0, 100)
        assert "fee" not in r1 or r1.get("fee") is None, r1
        # 带 fee：持久化 + 回读
        ts, r2 = store.trade_add(None, "600000.SH", "2026-10-21", "sell", 11.0, 50, fee=6.3)
        assert r2.get("fee") == 6.3, r2
        back = DataStore(td).trades(None)
        assert back[0].get("fee") is None and back[1].get("fee") == 6.3, back
        # 校验：负数 / 非数字拒绝
        for bad in (-1, "abc", float("inf")):
            try:
                store.trade_add(None, "600000.SH", "2026-10-22", "buy", 10.0, 10, fee=bad)
                raise AssertionError(f"应当报错: {bad}")
            except ValueError:
                pass
        # update：设 / 清空 / 不动
        ts, u = store.trade_update(None, "600000.SH", r2["id"], fee=7.5)
        assert u.get("fee") == 7.5, u
        ts, u = store.trade_update(None, "600000.SH", r2["id"], fee=None)
        assert u.get("fee") is None, u
        ts, u = store.trade_update(None, "600000.SH", r2["id"], price=11.5)  # 未传 fee → 不动
        assert u.get("fee") is None and u["price"] == 11.5, u
        ts, u = store.trade_update(None, "600000.SH", r2["id"], fee=2.0)
        assert u.get("fee") == 2.0, u
        try:
            store.trade_update(None, "600000.SH", r2["id"], fee=-5)
            raise AssertionError("应当报错")
        except ValueError:
            pass
        assert DataStore(td).trades(None)[1].get("fee") == 2.0


def t_store_batch():
    import analyzer.datastore as ds
    with tempfile.TemporaryDirectory() as td:
        store = _make_store(Path(td))
        items = [
            {"date": "2025-03-05", "side": "buy", "price": 38.5, "shares": 500},
            {"date": "2025/3/6", "side": "sell", "price": 39.2, "shares": 500, "fee": 8.9, "note": "止盈"},
            {"date": "bad-date", "side": "buy", "price": 10, "shares": 100},          # 日期错
            {"date": "2025-03-07", "side": "hold", "price": 10, "shares": 100},       # 方向错
            {"date": "2025-03-08", "side": "buy", "price": -1, "shares": 100},        # 价格错
            {"date": "2025-03-09", "side": "buy", "price": 10, "shares": 1.5},        # 股数错
        ]
        ts, added, errors = store.trade_add_batch(None, "600000.SH", items)
        assert len(added) == 2, added
        assert len(errors) == 4, errors
        assert [e["index"] for e in errors] == [2, 3, 4, 5], errors
        assert errors[1]["error"].startswith("side"), errors[1]
        # 持久化 + 排序
        back = DataStore(td).trades(None)
        assert len(back) == 2, back
        assert back[0]["date"] == "2025-03-05" and back[1]["date"] == "2025-03-06"
        assert back[1]["fee"] == 8.9 and back[1]["note"] == "止盈", back[1]
        # 空 / 非数组 / 超长
        for bad in ([], "x"):
            try:
                store.trade_add_batch(None, "600000.SH", bad)
                raise AssertionError(f"应当报错: {bad!r}")
            except ValueError:
                pass
        try:
            store.trade_add_batch(None, "600000.SH", [{"date": "2025-03-01", "side": "buy", "price": 1, "shares": 1}] * 600)
            raise AssertionError("超 500 应当报错")
        except ValueError as e:
            assert "500" in str(e)
        # 总上限：临时调低 _MAX_TRADES，超出的条目记为错误而非抛错
        saved = ds._MAX_TRADES
        ds._MAX_TRADES = 2
        try:
            ts, added2, errors2 = store.trade_add_batch(None, 
                "600000.SH",
                [{"date": f"2025-04-0{d}", "side": "buy", "price": 10, "shares": 100} for d in (1, 2, 3)],
            )
        finally:
            ds._MAX_TRADES = saved
        assert len(added2) == 0 and len(errors2) == 3, (added2, errors2)
        assert all("上限" in e["error"] for e in errors2), errors2


if __name__ == "__main__":
    check("position_of 移动加权平均", t_position_math)
    check("DataStore 交易流水往返", t_store_roundtrip)
    check("DataStore 交易更新", t_store_update)
    check("DataStore 费用字段", t_store_fee)
    check("DataStore 批量导入", t_store_batch)
    print(f"\n{PASSED} 通过 / {FAILED} 失败")
    sys.exit(1 if FAILED else 0)
