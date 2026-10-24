"""chip_distribution 插件单测（零网络；真实数据部分自动跳过）。

覆盖:
- 筹码算法: 零换手质量守恒 / 平盘日获利比例≈0.5 / 零换手成本加权精确值
- _zone_tests: 未触及→n=0 / 触及-收回分类正确
- 合成随机数据（make_context, turnover 列有效路径）: 框架全链路 + JSON 可序列化
- 确定性场景: 深跌→oversold+S1(+1); 大涨→overbought+S2(-1);
  深跌+末日巨量→shock 压制信号（时滞 regime）
- 数据不足: 友好降级
- 真实数据冒烟（data/ 存在时）

运行: .venv/bin/python tests/test_chip_distribution.py
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, ".")

from stock_analysis import Runner, load_plugins  # noqa: E402
from stock_analysis.plugins.chip_distribution import (  # noqa: E402
    ChipDistributionPlugin,
    _chip_series,
    _zone_tests,
)
from stock_analysis.testing import make_context  # noqa: E402

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


# ------------------------------------------------------------------ 测试辅助


class _StubCtx:
    """最小 ctx：kline / basic / annualize（插件依赖的全部接口）。

    turnover 列全 NaN → 走 成交量/流通股本 估算路径。
    """

    def __init__(self, df: pd.DataFrame, float_shares: float = 8e8):
        self._df = df
        self._fs = float_shares

    def kline(self, code, last=None, **kw):
        return self._df.tail(last) if last else self._df

    def basic(self, code):
        c = float(self._df["close"].iloc[-1])
        return {"code": code, "name": "合成股", "latest_price": c,
                "pct_chg": None, "total_mv": None,
                "float_mv": c * self._fs}

    def annualize(self):
        return 244.0


def _frame(close, hi, lo, vol) -> pd.DataFrame:
    n = len(close)
    df = pd.DataFrame(
        {
            "open": close, "high": hi, "low": lo, "close": close,
            "volume": vol, "amount": np.full(n, np.nan),
            "pct_chg": np.full(n, np.nan), "turnover": np.full(n, np.nan),
        },
        index=pd.bdate_range("2024-01-02", periods=n),
    )
    df.index.name = "date"
    return df


#: 流通股本 8e8 股时, 2% 换手对应的手数（volume 单位=手）
VOL2 = 1.6e5


# ------------------------------------------------------------------ 用例


def test_conservation() -> None:
    """质量守恒: total[t] = total[t-1]·(1-t_t) + v_t。"""
    n = 300
    c = np.full(n, 10.0)
    vol = np.full(n, 1.0)  # 股
    # 零换手: 逐日累加
    cs = _chip_series(c * 0.999, c * 1.001, c, c, vol, np.zeros(n))
    err = np.abs(cs["total"][1:] - (cs["total"][:-1] + 1.0)).max()
    assert err < 1e-9, f"零换手守恒误差 {err}"
    # 全换手: 每天清仓, total ≈ v
    cs2 = _chip_series(c * 0.999, c * 1.001, c, c, vol, np.ones(n))
    assert abs(cs2["total"][-1] - 1.0) < 1e-9
    # 半换手: total 收敛到 v/(1-(1-0.5)) = 2v
    cs3 = _chip_series(c * 0.999, c * 1.001, c, c, vol, np.full(n, 0.5))
    assert abs(cs3["total"][-1] - 2.0) < 0.05, cs3["total"][-1]


def test_flat_price() -> None:
    """平盘市: 平均成本≈价格, 获利比例≈0.5。"""
    n = 400
    c = np.full(n, 20.0)
    cs = _chip_series(c * 0.999, c * 1.001, c, c, np.full(n, 1.0), np.full(n, 0.02))
    assert abs(cs["avg_cost"][-1] - 20.0) < 0.1, cs["avg_cost"][-1]
    assert 0.35 <= cs["profit_ratio"][-1] <= 0.65, cs["profit_ratio"][-1]


def test_zero_turnover_cost() -> None:
    """零换手两段价: 成本 = 量加权均价; 获利比例 = 低位筹码占比。"""
    n = 300
    c = np.full(n, 10.0)
    c[200:] = 20.0
    cs = _chip_series(c * 0.999, c * 1.001, c, c, np.full(n, 1.0), np.zeros(n))
    expect = (200 * 10 + 100 * 20) / 300
    assert abs(cs["avg_cost"][-1] - expect) < 0.2, cs["avg_cost"][-1]
    # close=20: 20 元段三角形分布以 20 为峰, 一半在 20 上方 → pr∈[0.6,0.95]
    pr = cs["profit_ratio"][-1]
    assert 0.6 <= pr <= 0.95, pr


def test_zone_tests() -> None:
    """_zone_tests: 未触及→n=0; 触及后收回分类正确。"""
    n = 60
    close = np.full(n, 100.0)
    p15 = np.full(n, 90.0)
    p85 = np.full(n, 110.0)
    # (a) 未触及: high<110, low>90
    hi = np.full(n, 105.0)
    lo = np.full(n, 95.0)
    z = _zone_tests(lo, hi, close, p15, p85, n, zh=5, hold=2, warmup=10)
    assert z["resist_touch_n"] == 0 and z["support_touch_n"] == 0
    assert z["resist_break"] is not None  # 包络测试照常有值
    assert z["resist_break"] == 0.0       # 从未越界
    # (b) 触及: 第 30 日 high=112 触及 110, 之后收盘收回 100 → hold
    hi2 = hi.copy()
    hi2[31] = 112.0
    z2 = _zone_tests(lo, hi2, close, p15, p85, n, zh=5, hold=2, warmup=10)
    assert z2["resist_touch_n"] >= 1
    assert z2["resist_touch_hold"] == 1.0  # 触及后 2 日收盘全部收回位内
    # (c) 跌破支撑不收回: low=85 触及 90, 收盘 88 < 90
    lo3 = lo.copy()
    lo3[41] = 85.0
    close3 = close.copy()
    close3[42:44] = 88.0
    z3 = _zone_tests(lo3, hi, close3, p15, p85, n, zh=5, hold=2, warmup=10)
    assert z3["support_touch_n"] >= 1
    assert z3["support_touch_hold"] == 0.0  # 未收回


def test_framework_run() -> None:
    """合成随机数据（turnover 列有效路径）: 全链路 + 可序列化。"""
    ctx = make_context(n_stocks=2, years=5, seed=42)
    runner = Runner(load_plugins(), ctx)
    try:
        r = runner.run("chip_distribution", "600001.SH").to_dict()
        assert r["ok"], r["summary"]
        m = r["metrics"]
        assert 0.0 <= m["profit_ratio"] <= 1.0
        assert m["support"] < m["resistance"]
        assert m["avg_cost"] > 0
        for k in ("close", "avg_cost", "support", "resistance",
                  "profit_ratio", "conc70", "signal"):
            assert len(r["series"][k]["values"]) == 250, k
        # 回测指标存在（n 可为 0 但键必须有）
        for k in ("bt_count", "bt_count_ev", "bt_win10", "bt_win10_ev",
                  "resist_touch_hold", "support_touch_hold"):
            assert k in m, k
        json.dumps(r)  # 可序列化（NaN→null）
    finally:
        ctx.cleanup()


def test_oversold_signal() -> None:
    """深跌: pr≤10% → oversold + S1(+1) + alert 信号。"""
    n = 400
    c = np.full(n, 20.0)
    c[350:] = 20 + (13 - 20) * np.linspace(0, 1, 50)
    df = _frame(c, c * 1.005, c * 0.995, np.full(n, VOL2))
    r = ChipDistributionPlugin().run(_StubCtx(df), "T.SH")
    m = r.metrics
    assert m["profit_ratio"] <= 0.10, m["profit_ratio"]
    assert m["regime"] == "oversold"
    assert m["signal_today"] == 1
    assert any(s.level == "alert" and "超跌支撑" in s.text for s in r.signals)


def test_overbought_signal() -> None:
    """大涨: pr≥90% → overbought + S2(-1)。"""
    n = 400
    c = np.full(n, 20.0)
    c[350:] = 20 + (30 - 20) * np.linspace(0, 1, 50)
    df = _frame(c, c * 1.005, c * 0.995, np.full(n, VOL2))
    r = ChipDistributionPlugin().run(_StubCtx(df), "T.SH")
    m = r.metrics
    assert m["profit_ratio"] >= 0.90, m["profit_ratio"]
    assert m["regime"] == "overbought"
    assert m["signal_today"] == -1


def test_shock_suppression() -> None:
    """深跌+末日巨量(换手≥10%) → shock 压制 S1 信号（成本时滞）。"""
    n = 400
    c = np.full(n, 20.0)
    c[350:] = 20 + (13 - 20) * np.linspace(0, 1, 50)
    vol = np.full(n, VOL2)
    vol[-1] *= 6  # 2% → 12%
    df = _frame(c, c * 1.005, c * 0.995, vol)
    r = ChipDistributionPlugin().run(_StubCtx(df), "T.SH")
    m = r.metrics
    assert m["turnover"] >= 0.10, m["turnover"]
    assert m["regime"] == "shock"
    assert m["profit_ratio"] <= 0.10  # 满足 S1 条件
    assert m["signal_today"] == 0      # 但被时滞 regime 压制
    assert any(s.level == "warn" and "换手" in s.text for s in r.signals)


def test_insufficient_data() -> None:
    """数据不足: ok 结果 + warn 信号（不抛异常、不静默）。"""
    df = _frame(np.full(100, 10.0), np.full(100, 10.2),
                np.full(100, 9.8), np.full(100, VOL2))
    r = ChipDistributionPlugin().run(_StubCtx(df), "T.SH")
    assert r.ok
    assert "历史数据不足" in r.summary
    assert any(s.level == "warn" for s in r.signals)


def test_real_smoke() -> None:
    """真实数据冒烟（data/ 存在时）。"""
    f = Path("data/daily/hfq/600519.SH.parquet")
    if not f.exists():
        print("SKIP（无本地数据）")
        return
    from analyzer.datastore import DataStore
    from stock_analysis.core.context import AnalysisContext

    store = DataStore("data")
    runner = Runner(load_plugins(), AnalysisContext(store))
    r = runner.run("chip_distribution", "600519.SH").to_dict()
    assert r["ok"], r["summary"]
    m = r["metrics"]
    assert 0.0 < m["profit_ratio"] < 1.0
    assert m["support"] < m["resistance"]
    assert m["avg_cost"] > 0
    assert len(r["series"]["avg_cost"]["values"]) == 250
    tbl = r["tables"]["signals"]
    if tbl:
        assert "type" in tbl[0] and "fwd10" in tbl[0]
    json.dumps(r)


# ------------------------------------------------------------------ 主入口


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            check(name, fn)
    print(f"\n\n===== 汇总: {PASSED} PASS / {FAILED} FAIL =====")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
