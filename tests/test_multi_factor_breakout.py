"""multi_factor_breakout 插件单测（零网络；真实数据部分自动跳过）。

覆盖:
- 因子工具函数（ATR / 滚动z-score / 滚动分位 / 缺失权重再归一）
- 合成随机数据: 框架全链路运行 + JSON 可序列化
- 确定性构造数据: "高波动→压缩蓄势→放量突破" 应触发全部门与复合信号；
  下跌路径不应触发
- 数据不足: 友好降级（ok + warn，不抛异常）
- 真实数据冒烟（data/ 存在时）

运行: .venv/bin/python tests/test_multi_factor_breakout.py
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
from stock_analysis.plugins.multi_factor_breakout import (  # noqa: E402
    MultiFactorBreakoutPlugin,
    _atr,
    _combine_z,
    _forward_stats,
    _rolling_quantile,
    _rolling_zscore,
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
    """最小 ctx：只提供 kline / annualize（插件依赖的全部接口）。"""

    def __init__(self, df: pd.DataFrame):
        self._df = df

    def kline(self, code, last=None, **kw):
        return self._df.tail(last) if last else self._df

    def annualize(self):
        return 244.0


def _frame(close, open_, hi, lo, vol) -> pd.DataFrame:
    n = len(close)
    df = pd.DataFrame(
        {
            "open": open_, "high": hi, "low": lo, "close": close, "volume": vol,
            "amount": np.full(n, np.nan), "pct_chg": np.full(n, np.nan),
            "turnover": np.full(n, np.nan),
        },
        index=pd.bdate_range("2024-01-02", periods=n),
    )
    df.index.name = "date"
    return df


def _squeeze_breakout_path(n: int = 320, seed: int = 7, down: bool = False):
    """确定性构造: 前 235 天高波动 → 84 天低波动压缩蓄势 → 末日放量突破。

    down=True 时末日改为放量下跌（不应触发任何门）。
    """
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=float)
    base = 10.0 * (1.0 + 0.0006 * t)  # 缓慢上行
    hi_vol_end = 235
    noise = np.where(t < hi_vol_end, 0.009, 0.0008)  # 日收益噪声: 高波→压缩
    close = base * (1.0 + noise * rng.standard_normal(n))
    open_ = close * (1.0 + 0.001 * rng.standard_normal(n))
    spread = np.where(t < hi_vol_end, 0.015, 0.0015) * np.abs(rng.standard_normal(n)) + 0.0005
    hi = np.maximum(open_, close) * (1.0 + spread)
    lo = np.minimum(open_, close) * (1.0 - spread)
    vol = 10000.0 * (1.0 + 0.002 * t) * (1.0 + 0.02 * rng.standard_normal(n))

    # 末日: 突破前 20 日最高价（不含当日），量能为前 20 日均量 3 倍
    prior_high = float(hi[-21:-1].max())
    prior_vol = float(vol[-21:-1].mean())
    if down:
        close[-1] = lo[-21:-1].min() * 0.96
        open_[-1] = close[-1] * 1.01
        hi[-1] = open_[-1] * 1.002
        lo[-1] = close[-1] * 0.998
    else:
        close[-1] = prior_high * 1.01
        open_[-1] = prior_high * 0.998
        hi[-1] = close[-1] * 1.002
        lo[-1] = open_[-1] * 0.999
    vol[-1] = prior_vol * 3.0
    return _frame(close, open_, hi, lo, vol)


# ------------------------------------------------------------------ 用例


def t_helpers():
    idx = pd.bdate_range("2024-01-02", periods=300)
    s = pd.Series(np.linspace(1, 2, 300), index=idx)

    # 滚动 z-score: 常数列方差为 0 -> NaN（无信息）
    const = pd.Series(1.0, index=idx)
    assert _rolling_zscore(const, 250).isna().all()
    # 单调列: 末端 z 为正
    z = _rolling_zscore(s, 250)
    assert z.iloc[-1] > 0

    # 滚动分位: 递增序列末端分位 = 1.0（自身及以下占满）
    q = _rolling_quantile(s, 250)
    assert abs(q.iloc[-1] - 1.0) < 1e-9
    # 递减序列末端分位接近 1/n
    q2 = _rolling_quantile(s.iloc[::-1].reset_index(drop=True), 250)
    assert q2.iloc[-1] <= 1.0 / 249 + 1e-9

    # ATR: 常数价格 + 固定振幅 -> ATR ≈ 该振幅
    n = 60
    close = pd.Series(np.full(n, 10.0))
    df = pd.DataFrame({"close": close, "high": close + 0.2, "low": close - 0.2})
    atr = _atr(df, 14)
    assert abs(atr.iloc[-1] - 0.4) < 1e-6

    # 加权合成: 单因子缺失时权重再归一
    a = pd.Series([1.0, 1.0], index=[0, 1])
    b = pd.Series([np.nan, 3.0], index=[0, 1])
    comb = _combine_z({"a": a, "b": b}, {"a": 0.3, "b": 0.7})
    assert abs(comb.iloc[0] - 1.0) < 1e-9  # 只有 a -> 归一到 a 的值
    assert abs(comb.iloc[1] - (0.3 + 0.7 * 3) / 1.0) < 1e-9

    # 前瞻统计: 无信号返回空表不炸
    df_empty, stats = _forward_stats(s, [], (5, 10))
    assert df_empty.empty and stats[5]["n"] == 0
    assert np.isnan(stats[5]["win"])


def t_synth_framework_run():
    """走完整框架（发现/Runner/缓存路径）: 合成随机数据。"""
    ctx = make_context(n_stocks=2, years=5, seed=42)
    try:
        runner = Runner(load_plugins(), ctx)
        assert "multi_factor_breakout" in runner.registry.names()
        r = runner.run("multi_factor_breakout", "600001.SH")
        assert r.ok, r.error
        assert r.summary
        assert r.score is not None and 0 <= r.score <= 100
        assert "signals" in r.tables
        for k in ("close", "prior_high", "vol_ratio", "atr_pct", "score_z", "signal"):
            assert k in r.series, k
        s = json.dumps(r.to_dict(), ensure_ascii=False)
        assert len(s) > 100
        # 参数覆盖
        r2 = runner.run("multi_factor_breakout", "600001.SH", lookback=60, vol_mult=2.5)
        assert r2.ok, r2.error
    finally:
        ctx.cleanup()


def t_signal_fires_on_squeeze_breakout():
    """确定性路径: 压缩蓄势 + 放量突破 -> 三门全过 + 复合信号触发。"""
    df = _squeeze_breakout_path()
    ctx = _StubCtx(df)
    r = MultiFactorBreakoutPlugin().run(ctx, "600001.SH")
    assert r.ok, r.error
    m = r.metrics
    assert m["gate_breakout"] is True, f"突破门未过: {m}"
    assert m["gate_volume"] is True, f"量能门未过: 量比={m['vol_ratio']}"
    assert m["gate_squeeze"] is True, f"压缩门未过: ATR%分位={m['squeeze_q']}"
    assert m["signal_today"] is True, f"复合信号未触发: score_z={m['score_z']}"
    assert m["score_z"] >= 1.0
    # 历史信号表包含末日（末日无前瞻收益 -> fwd 为 None）
    assert m["bt_count"] == 1
    rows = r.tables["signals"]
    assert len(rows) == 1 and rows[0]["fwd5"] is None
    assert any(s.level == "alert" and "复合突破信号" in s.text for s in r.signals)
    assert r.verdict == "强复合信号"
    # 参数可关掉共振门（score_gate=0）
    r0 = MultiFactorBreakoutPlugin().run(ctx, "600001.SH", score_gate=0.0)
    assert r0.ok and r0.metrics["signal_today"] is True


def t_no_signal_on_breakdown():
    """确定性路径: 放量下跌 -> 不触发（突破门直接拦下）。"""
    df = _squeeze_breakout_path(down=True)
    ctx = _StubCtx(df)
    r = MultiFactorBreakoutPlugin().run(ctx, "600001.SH")
    assert r.ok, r.error
    m = r.metrics
    assert m["gate_breakout"] is False
    assert m["signal_today"] is False
    assert m["bt_count"] == 0
    assert not any(s.level == "alert" for s in r.signals)
    # 放量门在下跌日可能为 True（量确实放大），但突破门 False -> 不可能触发
    assert r.verdict != "强复合信号"


def t_insufficient_data():
    """数据不足: ok 结果 + warn 信号（不抛异常、不静默）。"""
    idx = pd.bdate_range("2024-01-02", periods=100)
    df = pd.DataFrame(
        {"open": 10.0, "high": 10.2, "low": 9.8, "close": 10.0, "volume": 1000.0},
        index=idx,
    )
    ctx = _StubCtx(df)
    r = MultiFactorBreakoutPlugin().run(ctx, "600001.SH")
    assert r.ok
    assert "历史数据不足" in r.summary
    assert any(s.level == "warn" and "数据不足" in s.text for s in r.signals)
    # 框架路径同样降级（years=1 ≈ 244 日 < 需求 280）
    ctx2 = make_context(n_stocks=1, years=1, seed=3)
    try:
        runner = Runner(load_plugins(), ctx2)
        r2 = runner.run("multi_factor_breakout", "600001.SH")
        assert r2.ok and "历史数据不足" in r2.summary
    finally:
        ctx2.cleanup()


def t_realdata_smoke():
    """真实数据冒烟（data/ 不存在时跳过）。"""
    daily = Path("data/daily/hfq/600519.SH.parquet")
    if not daily.exists():
        print("SKIP: 无真实数据 data/daily/hfq/600519.SH.parquet")
        return
    from analyzer.datastore import DataStore
    from stock_analysis.core.context import AnalysisContext

    store = DataStore("data")
    ctx = AnalysisContext(store)
    try:
        runner = Runner(load_plugins(), ctx)
        for code in ("600519.SH", "300750.SZ", "000001.SZ"):
            r = runner.run("multi_factor_breakout", code)
            assert r.ok, f"{code}: {r.error}"
            assert r.summary, code
            json.dumps(r.to_dict(), ensure_ascii=False)
            print(f"  {code}: score={r.score} verdict={r.verdict} "
                  f"[{r.elapsed_ms}ms] {r.summary}")
    finally:
        pass  # 真实 store 无需 cleanup


# ------------------------------------------------------------------ 主流程


def main() -> int:
    tests = [
        ("因子工具函数", t_helpers),
        ("框架全链路(合成数据)", t_synth_framework_run),
        ("确定性: 压缩突破触发信号", t_signal_fires_on_squeeze_breakout),
        ("确定性: 放量下跌不触发", t_no_signal_on_breakdown),
        ("数据不足友好降级", t_insufficient_data),
        ("真实数据冒烟", t_realdata_smoke),
    ]
    for name, fn in tests:
        check(name, fn)
    print(f"\n===== 汇总: {PASSED} PASS / {FAILED} FAIL =====")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
