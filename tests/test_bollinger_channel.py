"""bollinger_channel 插件单测（零网络；真实数据部分自动跳过）。

覆盖:
- 通道解析值: 9/11 交替价 + n=2/K=2 → mid=10/upper=12/lower=8/%b=0.75
- _rolling_pctile: 单调序列分位=1.0、窗口未建→NaN
- 确定性场景: 末日跌破下轨→S1(+1); 末日突破上轨→S2(-1);
  连续贴下轨→ride_dn 压制信号; 极窄波动后突破→S3 挤压突破(+1)
- 合成随机数据（make_context）: 框架全链路 + JSON 可序列化
- 数据不足: 友好降级
- 真实数据冒烟（data/ 存在时）

运行: .venv/bin/python tests/test_bollinger_channel.py
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
from stock_analysis.plugins.bollinger_channel import (  # noqa: E402
    BollingerChannelPlugin,
    _rolling_pctile,
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
    """最小 ctx：只提供 kline / basic / annualize。"""

    def __init__(self, df: pd.DataFrame):
        self._df = df

    def kline(self, code, last=None, **kw):
        return self._df.tail(last) if last else self._df

    def basic(self, code):
        return {}

    def annualize(self):
        return 244.0


def _frame(close) -> pd.DataFrame:
    n = len(close)
    df = pd.DataFrame(
        {
            "open": close, "high": close, "low": close, "close": close,
            "volume": np.full(n, 1.0), "amount": np.full(n, np.nan),
            "pct_chg": np.full(n, np.nan), "turnover": np.full(n, np.nan),
        },
        index=pd.bdate_range("2024-01-02", periods=n),
    )
    df.index.name = "date"
    return df


def _flat(n=400, level=20.0) -> np.ndarray:
    """极小锯齿噪声的平盘价（sd>0 且稳定）。"""
    return np.full(n, level) + np.tile([0.05, -0.05], n // 2)


# ------------------------------------------------------------------ 用例


def test_known_bands() -> None:
    """9/11 交替 + n_period=2/K=2: mid=10, σ=1, upper=12, lower=8, %b=0.75。"""
    c = np.tile([9.0, 11.0], 125)  # 250 行
    r = BollingerChannelPlugin().run(_StubCtx(_frame(c)), "T.SH",
                                     n_period=2, k_std=2.0, squeeze_lookback=20)
    m = r.metrics
    assert abs(m["mid"] - 10.0) < 1e-9
    assert abs(m["upper"] - 12.0) < 1e-9
    assert abs(m["lower"] - 8.0) < 1e-9
    # 末值 11: %b = (11-8)/(12-8) = 0.75
    assert abs(m["pctb"] - 0.75) < 1e-9
    assert abs(m["bandw"] - 0.4) < 1e-9  # (12-8)/10


def test_rolling_pctile() -> None:
    """单调递增序列: 当前值恒为窗口最大 → 分位 1.0; 窗口未建 → NaN。"""
    x = np.arange(1, 101, dtype=float)
    p = _rolling_pctile(x, 20)
    assert np.isnan(p[18])
    assert abs(p[19] - 1.0) < 1e-12
    assert abs(p[50] - 1.0) < 1e-12
    # 全同值: 分位恒为 1.0（≤ 含自身）
    p2 = _rolling_pctile(np.full(50, 5.0), 20)
    assert abs(p2[19] - 1.0) < 1e-12


def test_s1_bottom() -> None:
    """末日跌破下轨: %b<0 → S1(+1) + alert 信号。"""
    c = _flat()
    c[-1] = 19.0  # -5%
    r = BollingerChannelPlugin().run(_StubCtx(_frame(c)), "T.SH")
    m = r.metrics
    assert m["pctb"] < 0.0, m["pctb"]
    assert m["signal_today"] == 1
    assert any(s.level == "alert" and "区间下沿" in s.text for s in r.signals)


def test_s2_top() -> None:
    """末日突破上轨: %b>1 → S2(-1)。"""
    c = _flat()
    c[-1] = 21.0  # +5%
    r = BollingerChannelPlugin().run(_StubCtx(_frame(c)), "T.SH")
    m = r.metrics
    assert m["pctb"] > 1.0, m["pctb"]
    assert m["signal_today"] == -1
    assert any(s.level == "alert" and "区间上沿" in s.text for s in r.signals)


def test_ride_suppression() -> None:
    """连续 10 日阴跌破下轨: ride_dn regime 压制区间信号（防提前抄底）。"""
    c = _flat()
    c[-10:] = 20.0 * (1 - np.linspace(0.01, 0.12, 10))
    r = BollingerChannelPlugin().run(_StubCtx(_frame(c)), "T.SH")
    m = r.metrics
    assert m["regime"] == "ride_dn"
    assert m["pctb"] < 0.0  # 仍满足 S1 条件
    assert m["signal_today"] == 0      # 但被贴轨 regime 压制
    assert any(s.level == "warn" and "贴" in s.text for s in r.signals)


def test_squeeze_breakout() -> None:
    """极窄波动（挤压）后向上突破上轨: S3 挤压突破(+1) 优先于 S2。"""
    rng = np.random.default_rng(7)
    c = np.concatenate([
        20 * np.exp(np.cumsum(rng.normal(0, 0.002, 300))),   # 正常波动
        20 * np.exp(np.cumsum(rng.normal(0, 0.0004, 60))),   # 极窄 → 挤压
    ])
    c[-1] *= 1.03  # +3% 突破
    r = BollingerChannelPlugin().run(_StubCtx(_frame(c)), "T.SH")
    m = r.metrics
    assert m["signal_today"] == 1
    assert any(s.level == "alert" and "挤压突破向上" in s.text for s in r.signals)
    d = r.to_dict()
    rows = d["tables"]["signals"]
    assert rows and rows[0]["type"] == "squeeze_up"


def test_framework_run() -> None:
    """合成随机数据: 全链路 + 可序列化。"""
    ctx = make_context(n_stocks=2, years=5, seed=42)
    runner = Runner(load_plugins(), ctx)
    try:
        r = runner.run("bollinger_channel", "600001.SH").to_dict()
        assert r["ok"], r["summary"]
        m = r["metrics"]
        assert m["lower"] < m["mid"] < m["upper"]
        assert np.isfinite(m["pctb"])
        assert 0.0 < m["bandw"] < 1.0
        if m["hit1"] is not None:
            assert 0.0 < m["hit1"] < 1.0
        for k in ("close", "mid", "upper", "lower", "pctb", "bandw", "signal"):
            assert len(r["series"][k]["values"]) == 250, k
        for k in ("bt_count", "bt_count_ev", "bt_win10", "bt_win10_ev",
                  "hit1", "hit10"):
            assert k in m, k
        json.dumps(r)
    finally:
        ctx.cleanup()


def test_insufficient_data() -> None:
    """数据不足: ok 结果 + warn 信号（不抛异常、不静默）。"""
    r = BollingerChannelPlugin().run(_StubCtx(_frame(np.full(100, 10.0))), "T.SH")
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
    r = runner.run("bollinger_channel", "600519.SH").to_dict()
    assert r["ok"], r["summary"]
    m = r["metrics"]
    assert m["lower"] < m["mid"] < m["upper"]
    assert np.isfinite(m["pctb"])
    assert m["hit1"] is not None and 0.0 < m["hit1"] < 1.0
    assert len(r["series"]["pctb"]["values"]) == 250
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
