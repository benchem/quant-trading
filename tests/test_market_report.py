"""大盘复盘日报（analyzer/market_report）单测：合成数据（tmp_path 真实写 parquet）。"""
from __future__ import annotations

import datetime as dt
import pathlib

import pandas as pd
import pytest

from analyzer import market_report as mr

DAY = dt.date(2026, 11, 11)
PREV = dt.date(2026, 11, 10)
PRICES = {  # code -> (prev_close, day_close)
    "600AAA.SH": (10.0, 11.0),     # 主板 +10% → 涨停
    "300BBB.SZ": (20.0, 24.0),     # 创业板 +20% → 涨停
    "601CCC.SH": (5.0, 5.275),     # ST +5.5% → 涨停
    "602DDD.SH": (8.0, 7.92),      # -1%
    "603EEE.SH": (9.0, 9.18),      # +2%
    "604FFF.SH": (7.0, 6.79),      # -3%
    "605GGG.SH": (12.0, 12.12),    # +1%   半导体
    "606HHH.SH": (15.0, 15.075),   # +0.5% 半导体
    "607III.SH": (18.0, 17.91),    # -0.5% 半导体
    "608JJJ.SH": (11.0, 11.33),    # +3%   银行
    "609KKK.SH": (14.0, 14.14),    # +1%   银行
    "610LLL.SH": (16.0, 16.08),    # +0.5% 银行
}
NAMES = {"600AAA.SH": "A公司", "300BBB.SZ": "B公司", "601CCC.SH": "*ST丙",
         "602DDD.SH": "D公司", "603EEE.SH": "E公司", "604FFF.SH": "F公司",
         "605GGG.SH": "G公司", "606HHH.SH": "H公司", "607III.SH": "I公司",
         "608JJJ.SH": "J公司", "609KKK.SH": "K公司", "610LLL.SH": "L公司"}
# 半导体：600AAA/300BBB/601CCC/605GGG/606HHH/607III；银行：其余 6 只
BOARD_OF = {"600AAA.SH": "BK1", "300BBB.SZ": "BK1", "601CCC.SH": "BK1",
            "602DDD.SH": "BK2", "603EEE.SH": "BK2", "604FFF.SH": "BK2",
            "605GGG.SH": "BK1", "606HHH.SH": "BK1", "607III.SH": "BK1",
            "608JJJ.SH": "BK2", "609KKK.SH": "BK2", "610LLL.SH": "BK2"}
BOARD_NAME = {"BK1": "半导体", "BK2": "银行"}


class FakeStore:
    root: pathlib.Path
    market: pd.DataFrame
    basic: pd.DataFrame

    def __init__(self, root):
        self.root = root
        self._members = pd.DataFrame({
            "board_code": [BOARD_OF[c] for c in PRICES],
            "board_name": [BOARD_NAME[BOARD_OF[c]] for c in PRICES],
        }, index=list(PRICES))

    def _board_members_of(self, board_type):
        return self._members


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    root = tmp_path_factory.mktemp("data")
    (root / "dataset").mkdir()
    rows = []
    for code, (p0, p1) in PRICES.items():
        rows.append({"date": PREV, "code": code, "close": p0})
        rows.append({"date": DAY, "code": code, "close": p1})
    pd.DataFrame(rows).to_parquet(root / "dataset/daily_hfq.parquet", index=False)
    idx = pd.DataFrame({
        "date": [PREV, DAY, PREV, DAY],
        "code": ["000001.SH", "000001.SH", "399006.SZ", "399006.SZ"],
        "close": [3000.0, 3030.0, 2000.0, 1960.0],
    })
    idx.to_parquet(root / "dataset/index_daily.parquet", index=False)

    s = FakeStore(root)
    s.market = pd.DataFrame({
        "adv": [9], "dec": [3], "flat": [0], "n_codes": [12],
        "mean_ret": [0.0119], "med_ret": [0.01], "amount_yi": [16000.0],
        "partial": [False], "nav_eqw": [1.0],
    }, index=pd.to_datetime([pd.Timestamp(DAY)]))
    s.basic = pd.DataFrame({"name": [NAMES[c] for c in PRICES]}, index=list(PRICES))
    mr._LIMIT_RET_CACHE.clear()
    return s


def test_limit_stats(store):
    lim = mr._limit_stats(store, DAY)
    assert lim["limit_up"] == 3
    assert lim["limit_down"] == 0
    assert "*ST丙" in {n for _, n, _ in lim["up_rows"]}


def test_limit_down_and_guard(store):
    # 604FFF -3%（主板未触跌停）→ 无跌停记录
    lim = mr._limit_stats(store, DAY)
    assert not lim["down_rows"]
    t = mr._stock_day_ret(store, DAY)
    assert len(t) == len(PRICES)


def test_market_light_formula(store):
    ov = mr._market_overview(store, DAY)
    lim = mr._limit_stats(store, DAY)
    idx_rows = mr._index_rows(store, DAY)
    light = mr.market_light(ov, lim, idx_rows)
    assert light["breadth"] == 75           # 9/(9+3)*100
    # index avg = (+1.0% -2.0%)/2 = -0.5 → 50-6=44
    assert light["index"] in (43, 44)
    assert light["limit"] == 100            # 3/3
    assert light["quality"] == "ok"
    assert light["status"] == "green"


def test_board_rank_day(store):
    rows = mr._board_rank_day(store, DAY, min_stocks=5)
    d = {n: r for n, r, _ in rows}
    assert set(d) == {"半导体", "银行"}
    # 半导体: (0.10+0.20+0.055+0.01+0.005-0.005)/6
    assert d["半导体"] == pytest.approx((0.10 + 0.20 + 0.055 + 0.01 + 0.005 - 0.005) / 6, abs=1e-6)
    # 银行: (-0.01+0.02-0.03+0.03+0.01+0.005)/6
    assert d["银行"] == pytest.approx((-0.01 + 0.02 - 0.03 + 0.03 + 0.01 + 0.005) / 6, abs=1e-6)
    assert rows[0][0] == "半导体"            # 降序


def test_board_min_stocks(store):
    assert mr._board_rank_day(store, DAY, min_stocks=7) == []


def test_build_report_sections(store):
    md = mr.build_market_report(store, DAY)
    for sec in ("# 大盘复盘日报 2026-11-11（周三）", "## 一、指数表现",
                "## 二、市场概况", "## 三、涨跌停", "## 四、板块领涨领跌",
                "## 附注", "**市场信号", "半导体", "*ST丙"):
        assert sec in md
    assert "3030.00" in md and "+1.00%" in md


def test_emit_and_push(store, tmp_path):
    pushed = []

    class Hub:
        def push(self, **kw):
            pushed.append(kw)

    out = tmp_path / "reports"
    p1 = mr.emit_market_report(store, DAY, hub=Hub(), out_dir=out)
    p2 = mr.emit_market_report(store, DAY, hub=Hub(), out_dir=out)
    assert p1 == p2 and p1.exists() and p1.name == "2026-11-11_市场日报.md"
    assert len(pushed) == 2
    assert pushed[0]["key"] == "market_report:2026-11-11"


def test_no_hub_still_writes(store, tmp_path):
    p = mr.emit_market_report(store, DAY, hub=None, out_dir=tmp_path)
    assert p.exists()


def test_missing_day_degrades(store):
    md = mr.build_market_report(store, dt.date(2026, 12, 31))
    assert "数据缺失" in md
    light = mr.market_light({}, {}, [])
    assert light["quality"] == "unavailable"
    assert light["status"] == "yellow"      # 全维度降级 50 分
