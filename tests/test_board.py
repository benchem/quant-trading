"""板块功能自测（analyzer.datastore.boards/board_rank/rank 板块/市场过滤，无需网络）。

运行: .venv/bin/python tests/test_board.py

合成数据：30 个交易日、18 只股票
- 行业板块 A (BK0001, 5 只): 100 -> 120（区间收益 +20%）
- 行业板块 B (BK0002, 5 只): 100 -> 80（区间收益 -20%）
- 行业板块 C (BK0003, 3 只): 100 -> 110（成分股 < min_stocks=5，应被过滤）
- 未归板块 5 只: 含 2 只创业板(300xxx.SZ)、1 只科创板(688001.SH)，用于市场板块过滤
- 概念板块（重叠成分）:
  - BK1001 测试概念AI: 600001.SH(+20%) / 600101.SH(-20%) / 600201.SH(+10%) -> 均值 0
  - BK1002 测试概念算力: 600001.SH(+20%) / 600002.SH(+20%) -> 均值 +20%（600001 双概念重叠）
"""
from __future__ import annotations

import shutil
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

START, END = "2026-09-01", "2026-09-30"


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


def _stock_df(code: str, dates: pd.DatetimeIndex, first: float, last: float) -> pd.DataFrame:
    n = len(dates)
    close = np.linspace(first, last, n)
    return pd.DataFrame(
        {
            "date": dates,
            "code": [code] * n,
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": [10000] * n,
            "amount": [1e7] * n,
            "turnover": [1.0] * n,
        }
    )


def _make_store(tmp: Path, with_boards: bool = True, with_concept: bool = False) -> DataStore:
    (tmp / "daily" / "hfq").mkdir(parents=True, exist_ok=True)
    (tmp / "dataset").mkdir(parents=True, exist_ok=True)
    dates = pd.date_range(START, END, freq="D")  # 30 天全部视为交易日
    specs = (
        [("600001.SH", 100, 120), ("600002.SH", 100, 120), ("600003.SH", 100, 120),
         ("600004.SH", 100, 120), ("600005.SH", 100, 120)]          # 板块 A (5)
        + [("600101.SH", 100, 80), ("600102.SH", 100, 80),
           ("600103.SH", 100, 80), ("600104.SH", 100, 80),
           ("600105.SH", 100, 80)]                                  # 板块 B (5)
        + [("600201.SH", 100, 110), ("600202.SH", 100, 110),
           ("600203.SH", 100, 110)]                                  # 板块 C (3)
        + [("600301.SH", 100, 100), ("600302.SH", 100, 100)]         # 未归板块 (2, 沪主板)
        + [("300001.SZ", 100, 130), ("300002.SZ", 100, 90)]          # 未归板块 (2, 创业板)
        + [("688001.SH", 100, 105)]                                   # 未归板块 (1, 科创板)
    )
    frames = []
    for code, f, l in specs:
        df = _stock_df(code, dates, f, l)
        df.to_parquet(tmp / "daily" / "hfq" / f"{code}.parquet", index=False)
        frames.append(df)
    merged = pd.concat(frames, ignore_index=True)
    merged.to_parquet(tmp / "dataset" / "daily_hfq.parquet", index=False)

    pd.DataFrame(
        {
            "code": [c for c, _, _ in specs],
            "name": [f"测试{i}" for i in range(len(specs))],
            "latest_price": [110.0] * len(specs),
            "pct_chg": [0.5] * len(specs),
            "turnover": [1.0] * len(specs),
            "total_mv": [1e9] * len(specs),
            "float_mv": [1e9] * len(specs),
        }
    ).to_parquet(tmp / "dataset" / "stock_basic.parquet", index=False)
    pd.DataFrame({"trade_date": dates}).to_parquet(tmp / "trade_calendar.parquet", index=False)

    if with_boards or with_concept:
        rows = []
        if with_boards:
            for code, _, _ in specs[:5]:
                rows.append((code, "industry", "BK0001", "测试板块A"))
            for code, _, _ in specs[5:10]:
                rows.append((code, "industry", "BK0002", "测试板块B"))
            for code, _, _ in specs[10:13]:
                rows.append((code, "industry", "BK0003", "测试板块C"))
        if with_concept:
            c1 = specs[0][0]; c2 = specs[1][0]; b1 = specs[5][0]; c3 = specs[10][0]
            rows += [
                (c1, "concept", "BK1001", "测试概念AI"),
                (b1, "concept", "BK1001", "测试概念AI"),
                (c3, "concept", "BK1001", "测试概念AI"),
                (c1, "concept", "BK1002", "测试概念算力"),
                (c2, "concept", "BK1002", "测试概念算力"),
            ]
        pd.DataFrame(rows, columns=["code", "board_type", "board_code", "board_name"]).to_parquet(
            tmp / "board_members.parquet", index=False
        )
    return DataStore(tmp)


def test_meta_boards(store: DataStore):
    assert store.meta["boards_available"] is True
    assert store.meta["n_boards"] == 3, store.meta


def test_boards_list(store: DataStore):
    bs = store.boards()
    assert len(bs) == 3, bs
    by = {b["board_code"]: b for b in bs}
    assert by["BK0001"]["n_stocks"] == 5 and by["BK0001"]["board_name"] == "测试板块A"
    assert by["BK0002"]["n_stocks"] == 5
    assert by["BK0003"]["n_stocks"] == 3


def test_board_rank(store: DataStore):
    out = store.board_rank(START, END, metric="ret", n=10, ascending=False)
    assert out["n_days"] == 30
    assert out["n_boards"] == 3, out  # 过滤小板块前的板块总数
    rows = out["rows"]
    assert len(rows) == 2, rows  # C 板块成分股 3 < min_stocks=5 被过滤
    a, b = rows[0], rows[1]
    assert a["board_code"] == "BK0001" and b["board_code"] == "BK0002"
    assert abs(a["ret"] - 0.20) < 1e-9, a["ret"]
    assert abs(b["ret"] - (-0.20)) < 1e-9, b["ret"]
    assert (a["adv"], a["dec"]) == (5, 0), a
    assert (b["adv"], b["dec"]) == (0, 5), b
    assert a["n_stocks"] == 5 and b["n_stocks"] == 5
    assert abs(a["total_mv"] - 5e9) < 1e6 and abs(b["total_mv"] - 5e9) < 1e6
    assert a["turnover"] == 1.0
    # 升序（最后）：B 板块 -20% 应排第一
    out2 = store.board_rank(START, END, metric="ret", n=10, ascending=True)
    assert out2["rows"][0]["board_code"] == "BK0002"


def test_rank_board_filter(store: DataStore):
    out = store.rank(START, END, metric="ret", n=20, board="BK0002")
    codes = {r["code"] for r in out["rows"]}
    assert codes == {f"60010{i}.SH" for i in range(1, 6)}, codes
    assert all(r["board_code"] == "BK0002" and r["board_name"] == "测试板块B" for r in out["rows"])
    # 不存在的板块 -> 空
    assert store.rank(START, END, board="BK9999")["rows"] == []


def test_rank_board_column(store: DataStore):
    out = store.rank(START, END, metric="ret", n=20)
    assert len(out["rows"]) == 18  # 18 只全部满足样本量（30 天）
    row = {r["code"]: r for r in out["rows"]}
    assert row["600001.SH"]["board_code"] == "BK0001"
    assert row["600301.SH"]["board_code"] is None
    assert row["600301.SH"]["board_name"] is None


def test_no_board_data(tmp: Path):
    store = _make_store(tmp, with_boards=False)
    assert store.board_df is None
    assert store.meta["boards_available"] is False and store.meta["n_boards"] == 0
    assert store.boards() == []
    out = store.board_rank(START, END)
    assert "error" in out and out["rows"] == []
    rows = store.rank(START, END)["rows"]
    assert all(r["board_code"] is None for r in rows)



def test_market_of():
    from analyzer.datastore import market_of
    assert market_of("600519.SH") == "sh_main"
    assert market_of("000858.SZ") == "sz_main"
    assert market_of("300750.SZ") == "cyb"
    assert market_of("301123.SZ") == "cyb"
    assert market_of("688981.SH") == "kcb"
    assert market_of("832566.BJ") == "bj"
    assert market_of("920001.BJ") == "bj"
    assert market_of("900901.SH") is None  # B 股不识别


def test_meta_markets(store: DataStore):
    ms = {m["key"]: m for m in store.meta["markets"]}
    assert ms["sh_main"]["n_stocks"] == 15, ms
    assert ms["cyb"]["n_stocks"] == 2
    assert ms["kcb"]["n_stocks"] == 1
    assert "sz_main" not in ms and "bj" not in ms  # 空板块不出现


def test_rank_market_filter(store: DataStore):
    out = store.rank(START, END, metric="ret", n=20, market="cyb")
    codes = [r["code"] for r in out["rows"]]
    assert codes == ["300001.SZ", "300002.SZ"], codes  # +30% 在前, -10% 在后
    out2 = store.rank(START, END, metric="ret", n=20, market="kcb")
    assert [r["code"] for r in out2["rows"]] == ["688001.SH"]
    # 市场 + 板块叠加：cyb 与任何行业板块无交集
    assert store.rank(START, END, board="BK0001", market="cyb")["rows"] == []
    # 不存在的市场 -> 空
    assert store.rank(START, END, market="bj")["rows"] == []


def test_concept_boards_list(store: DataStore):
    assert store.meta["concept_boards_available"] is True
    bs = store.boards("concept")
    by = {b["board_code"]: b for b in bs}
    assert len(bs) == 2, bs
    assert by["BK1001"]["n_stocks"] == 3 and by["BK1001"]["board_name"] == "测试概念AI"
    assert by["BK1002"]["n_stocks"] == 2
    # 行业列表不受概念数据影响
    assert {b["board_code"] for b in store.boards("industry")} == {"BK0001", "BK0002", "BK0003"}


def test_concept_board_rank(store: DataStore):
    out = store.board_rank(START, END, metric="ret", n=10, ascending=False,
                           min_stocks=1, board_type="concept")
    rows = {r["board_code"]: r for r in out["rows"]}
    assert set(rows) == {"BK1001", "BK1002"}, out
    # BK1002: (600001 +20%, 600002 +20%) -> 均值 +20%, 涨2跌0
    assert abs(rows["BK1002"]["ret"] - 0.20) < 1e-9, rows["BK1002"]
    assert (rows["BK1002"]["adv"], rows["BK1002"]["dec"]) == (2, 0)
    assert rows["BK1002"]["n_stocks"] == 2
    # BK1001: (600001 +20%, 600101 -20%, 600201 +10%) -> 均值 +1/30%, 涨2跌1（600001 重叠计入）
    assert abs(rows["BK1001"]["ret"] - 1 / 30) < 1e-9, rows["BK1001"]
    assert (rows["BK1001"]["adv"], rows["BK1001"]["dec"]) == (2, 1)
    assert rows["BK1001"]["n_stocks"] == 3
    # 降序: +20% 的 BK1002 在前
    assert out["rows"][0]["board_code"] == "BK1002"


def test_rank_concept_board_filter(store: DataStore):
    # 概念板块筛选个股: BK1002 = {600001, 600002}
    out = store.rank(START, END, metric="ret", n=10, board="BK1002")
    codes = {r["code"] for r in out["rows"]}
    assert codes == {"600001.SH", "600002.SH"}, codes
    # 个股行「板块」列仍为行业（600001 -> 测试板块A）
    assert all(r["board_name"] == "测试板块A" for r in out["rows"])


def test_concept_missing(tmp: Path):
    store = _make_store(tmp, with_concept=False)
    assert store.meta["concept_boards_available"] is False
    assert store.boards("concept") == []
    out = store.board_rank(START, END, board_type="concept")
    assert "error" in out and "--with-concept" in out["error"], out
    # 行业正常
    assert store.board_rank(START, END)["rows"]


def test_legacy_file_schema(tmp: Path):
    # 旧格式 board_members.parquet（无 board_type 列）兼容
    store = _make_store(tmp)  # 默认 with_concept=False 也写新格式；这里手工降级为旧格式
    pfile = tmp / "board_members.parquet"
    df = pd.read_parquet(pfile)
    df = df.drop(columns=["board_type"])
    df.to_parquet(pfile, index=False)
    store2 = DataStore(tmp)
    assert store2.board_df is not None and store2.board_df["board_code"].nunique() == 3
    assert store2.meta["concept_boards_available"] is False
    assert store2.board_rank(START, END)["n_boards"] == 3


def main() -> int:
    tmp1 = Path(tempfile.mkdtemp(prefix="board_t1_"))
    tmp2 = Path(tempfile.mkdtemp(prefix="board_t2_"))
    tmp3 = Path(tempfile.mkdtemp(prefix="board_t3_"))
    tmp4 = Path(tempfile.mkdtemp(prefix="board_t4_"))
    try:
        store = _make_store(tmp1)
        store_cpt = _make_store(tmp3, with_concept=True)
        check("meta 含板块信息", lambda: test_meta_boards(store))
        check("boards() 板块列表", lambda: test_boards_list(store))
        check("board_rank() 等权聚合", lambda: test_board_rank(store))
        check("rank(board=) 板块过滤", lambda: test_rank_board_filter(store))
        check("rank() 行含板块列", lambda: test_rank_board_column(store))
        check("无板块数据时优雅降级", lambda: test_no_board_data(tmp2))
        check("market_of() 代码规则", lambda: test_market_of())
        check("meta 含市场板块统计", lambda: test_meta_markets(store))
        check("rank(market=) 市场过滤", lambda: test_rank_market_filter(store))
        check("boards(concept) 概念列表", lambda: test_concept_boards_list(store_cpt))
        check("board_rank(concept) 重叠聚合", lambda: test_concept_board_rank(store_cpt))
        check("rank(board=) 概念板块筛选", lambda: test_rank_concept_board_filter(store_cpt))
        check("无概念数据时优雅降级", lambda: test_concept_missing(tmp2))
        check("旧格式 board_members 兼容", lambda: test_legacy_file_schema(tmp4))
    finally:
        shutil.rmtree(tmp1, ignore_errors=True)
        shutil.rmtree(tmp2, ignore_errors=True)
        shutil.rmtree(tmp3, ignore_errors=True)
        shutil.rmtree(tmp4, ignore_errors=True)
    print(f"\n===== 汇总: {PASSED} 通过 / {FAILED} 失败 =====")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
