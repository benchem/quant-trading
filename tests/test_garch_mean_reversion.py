"""garch_mean_reversion 插件单测（零网络；真实数据部分自动跳过）。

覆盖:
- 估计器: Nelder-Mead / ARMA(2,1) Hannan-Rissanen 参数恢复 / GARCH(1,1) MLE
  参数恢复（含低持续性组）/ 条件方差递归收敛
- 合成随机数据: 框架全链路运行 + JSON 可序列化
- 确定性场景: 黑天鹅(末日暴跌)→black_swan 无回归信号但区间保留;
  温和超卖→range + 超卖信号触发; 强单边趋势→trending 无信号
- walk-forward: 历史信号与区间标定满足前缀不变性，样本外包络命中率合理
- 数据不足: 友好降级
- 真实数据冒烟（data/ 存在时）

运行: .venv/bin/python tests/test_garch_mean_reversion.py
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
from stock_analysis.plugins.garch_mean_reversion import (  # noqa: E402
    GarchMeanReversionPlugin,
    _arma_forecast,
    _fit_arma,
    _fit_garch,
    _garch_sigma2,
    _nelder_mead,
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


def _frame(close, hi, lo, vol) -> pd.DataFrame:
    n = len(close)
    df = pd.DataFrame(
        {
            "open": close, "high": hi, "low": lo, "close": close, "volume": vol,
            "amount": np.full(n, np.nan), "pct_chg": np.full(n, np.nan),
            "turnover": np.full(n, np.nan),
        },
        index=pd.bdate_range("2024-01-02", periods=n),
    )
    df.index.name = "date"
    return df


def _synth(n: int = 600, noise: float = 0.005, last_drop: float = 0.0,
           seed: int = 11) -> pd.DataFrame:
    """随机游走 + 可选末日跌幅; high/low 含日内偏移（与 noise 成比例）。"""
    rng = np.random.default_rng(seed)
    base = 20.0
    close = base * np.cumprod(1.0 + noise * rng.standard_normal(n))
    if last_drop:
        close[-1] = close[-2] * (1.0 - last_drop)
    spread = noise * 0.6 * np.abs(rng.standard_normal(n)) + 1e-4
    return _frame(close, close * (1 + spread), close * (1 - spread),
                  1e6 * (1 + 0.1 * rng.standard_normal(n)))


def _trend_path(n: int = 600, seed: int = 11) -> pd.DataFrame:
    """随机游走 + 末 30 日持续上涨 15%（强单边趋势）。"""
    df = _synth(n=n, noise=0.005, seed=seed)
    close = df["close"].to_numpy().copy()
    close[-30:] *= 1.0 + np.linspace(0, 0.15, 30)
    hi, lo = df["high"].to_numpy().copy(), df["low"].to_numpy().copy()
    k = close / df["close"].to_numpy()
    return _frame(close, hi * k, lo * k, df["volume"].to_numpy())


# ------------------------------------------------------------------ 用例


def t_estimators():
    """纯 numpy 估计器参数恢复（无 scipy/arch 依赖的核心验证）。"""
    # --- Nelder-Mead: 二次函数
    x, f = _nelder_mead(lambda v: (v[0] - 1) ** 2 + (v[1] + 2) ** 2,
                        np.array([0.0, 0.0]))
    assert abs(x[0] - 1) < 1e-3 and abs(x[1] + 2) < 1e-3, x
    assert f < 1e-6

    # --- Nelder-Mead: 线性方程组（病态二次型）
    # 11v0+v1=30, v0+11v1=-10 → Cramer: v0=340/120, v1=-140/120
    def q(v):
        return 0.5 * ((11 * v[0] + v[1] - 30) ** 2 + (v[0] + 11 * v[1] + 10) ** 2)
    x, f = _nelder_mead(q, np.array([0.0, 0.0]))
    assert abs(x[0] - 340.0 / 120) < 1e-3, x
    assert abs(x[1] + 140.0 / 120) < 1e-3, x

    # --- ARMA(2,1) 恢复
    rng = np.random.default_rng(7)
    n = 4000
    e = 0.01 * rng.standard_normal(n)
    r = np.zeros(n)
    for t in range(2, n):
        r[t] = 0.001 + 0.3 * r[t - 1] - 0.1 * r[t - 2] + e[t] + 0.2 * e[t - 1]
    r = r[500:]
    fit = _fit_arma(r, 2, 1)
    assert abs(fit["phi"][0] - 0.3) < 0.08, fit["phi"]
    assert abs(fit["phi"][1] + 0.1) < 0.08, fit["phi"]
    assert abs(fit["theta"][0] - 0.2) < 0.12, fit["theta"]
    assert abs(fit["c"] - 0.001) < 0.0005, fit["c"]
    mu = _arma_forecast(fit, r)
    assert np.isfinite(mu) and abs(mu) < 0.03, mu

    # --- GARCH 条件方差递归: 常数 e² → 收敛到无条件方差 ω/(1-α-β)
    s2 = _garch_sigma2(2e-6, 0.10, 0.88, np.full(2000, 1e-4), 1e-4)
    assert abs(s2[-1] - 2e-6 / 0.02) / 1e-4 < 1e-6, s2[-1]

    # --- GARCH(1,1) MLE 恢复（高持续性）
    rng = np.random.default_rng(11)
    n = 6000
    s2t, et = np.zeros(n), np.zeros(n)
    s2t[0] = 1e-4
    for t in range(1, n):
        s2t[t] = 2e-6 + 0.10 * et[t - 1] ** 2 + 0.88 * s2t[t - 1]
        et[t] = np.sqrt(s2t[t]) * rng.standard_normal()
    g = _fit_garch(et[1000:])
    assert 0.05 <= g["alpha"] <= 0.15, g["alpha"]
    assert 0.82 <= g["beta"] <= 0.95, g["beta"]
    assert 0.93 <= g["persistence"] <= 0.99, g["persistence"]
    assert 0.5e-4 <= np.mean(g["sigma2"]) <= 1.5e-4, np.mean(g["sigma2"])

    # --- GARCH(1,1) MLE 恢复（低持续性）
    rng = np.random.default_rng(23)
    s2t, et = np.zeros(n), np.zeros(n)
    s2t[0] = 4e-4
    for t in range(1, n):
        s2t[t] = 2e-5 + 0.20 * et[t - 1] ** 2 + 0.50 * s2t[t - 1]
        et[t] = np.sqrt(s2t[t]) * rng.standard_normal()
    g2 = _fit_garch(et[1000:])
    assert 0.12 <= g2["alpha"] <= 0.28, g2["alpha"]
    assert 0.38 <= g2["beta"] <= 0.62, g2["beta"]
    assert abs(g2["persistence"] - 0.70) < 0.08, g2["persistence"]


def t_synth_framework_run():
    """走完整框架（发现/Runner/缓存路径）: 合成随机数据。"""
    ctx = make_context(n_stocks=2, years=5, seed=42)
    try:
        runner = Runner(load_plugins(), ctx)
        assert "garch_mean_reversion" in runner.registry.names()
        r = runner.run("garch_mean_reversion", "600001.SH")
        assert r.ok, r.error
        assert r.summary
        assert r.score is not None and 0 <= r.score <= 100
        assert "signals" in r.tables
        for k in ("close", "ma", "band_lo", "band_hi", "dev_z", "signal"):
            assert k in r.series, k
        m = r.metrics
        for k in ("dev_z", "regime", "rho_dev", "half_life", "rev_frac",
                  "persistence", "sigma1", "band_hit", "shock"):
            assert k in m, k
        s = json.dumps(r.to_dict(), ensure_ascii=False)
        assert len(s) > 100
        # 参数覆盖
        r2 = runner.run("garch_mean_reversion", "600001.SH",
                        ma_window=40, horizon=10, z_entry=1.5)
        assert r2.ok, r2.error
    finally:
        ctx.cleanup()


def t_black_swan():
    """黑天鹅: 末日暴跌 8%（≈16σ）→ black_swan, 无回归信号, 区间保留。"""
    df = _synth(last_drop=0.08)
    ctx = _StubCtx(df)
    r = GarchMeanReversionPlugin().run(ctx, "600001.SH")
    assert r.ok, r.error
    m = r.metrics
    assert m["regime"] == "black_swan", m["regime"]
    assert m["shock"] >= 6.0, m["shock"]
    assert m["signal_today"] == 0, "黑天鹅日不应发回归信号"
    assert m["sigma1"] > 0.015, "末日冲击后次日波动预测应显著抬升"
    # 区间预测保留（黑天鹅只关方向信号, 不关区间）
    assert m["range_lo"] < m["close"] < m["range_hi"]
    assert any(s.level == "warn" and "黑天鹅" in s.text for s in r.signals)
    # 区间命中率仍应接近标定值（全期含黑天鹅日）
    assert 0.85 <= m["band_hit"] <= 1.0, m["band_hit"]


def t_oversold_signal():
    """温和超卖: 末日跌 2.5%（≈5σ, 非黑天鹅）→ range + 超卖信号。"""
    df = _synth(last_drop=0.025)
    ctx = _StubCtx(df)
    r = GarchMeanReversionPlugin().run(ctx, "600001.SH")
    assert r.ok, r.error
    m = r.metrics
    assert m["regime"] == "range", m["regime"]
    assert m["dev_z"] <= -2.0, m["dev_z"]
    assert m["signal_today"] == 1, "超卖应触发 +1 回归信号"
    assert m["rev_frac"] >= 0.05, m["rev_frac"]
    assert m["exp_rev"] > 0, m["exp_rev"]
    assert any(s.level == "alert" and "超卖均值回归" in s.text for s in r.signals)
    assert "超卖" in r.verdict
    # 历史回测表非空且前瞻收益可算（回归方向胜率为有限值或 None 但不崩溃）
    json.dumps(r.to_dict(), ensure_ascii=False)


def t_trending_no_signal():
    """强单边趋势: 末 30 日持续上涨 15% → trending, 无回归信号。"""
    df = _trend_path()
    ctx = _StubCtx(df)
    r = GarchMeanReversionPlugin().run(ctx, "600001.SH")
    assert r.ok, r.error
    m = r.metrics
    assert m["regime"] == "trending", m["regime"]
    assert m["trend_score"] >= 2.5, m["trend_score"]
    assert m["signal_today"] == 0, "趋势市不应发回归信号"
    assert any(s.level == "warn" and "强趋势" in s.text for s in r.signals)
    assert r.verdict != "强均值回归机会（超卖）"


def t_band_calibration():
    """因果区间标定: 纯随机游走上，样本外 high/low 包络命中率应接近 95%。"""
    df = _synth(seed=99)
    ctx = _StubCtx(df)
    r = GarchMeanReversionPlugin().run(ctx, "600001.SH")
    assert r.ok, r.error
    m = r.metrics
    # 两侧 97.5 分位标定 → 联合 ~95%（独立越界近似）
    assert 0.90 <= m["band_hit"] <= 0.99, m["band_hit"]
    # 偏移倍数为正且有限
    assert np.isfinite(m["hi_off"]) and m["hi_off"] > 0
    assert np.isfinite(m["lo_off"]) and m["lo_off"] > 0
    # 当日区间预测包住最新价
    assert m["range_lo"] < m["close"] < m["range_hi"]


def t_walk_forward_prefix_invariance():
    """追加未来行情不得改变既有历史信号、波动和区间标定。"""
    cut, n = 450, 500
    full = _synth(n=n, seed=31)
    close = full["close"].to_numpy().copy()
    close[cut:] *= np.linspace(1.0, 3.0, n - cut)
    scale = close / full["close"].to_numpy()
    full = _frame(close, full["high"].to_numpy() * scale,
                  full["low"].to_numpy() * scale, full["volume"].to_numpy())
    short_result = GarchMeanReversionPlugin().run(_StubCtx(full.iloc[:cut]), "T.SH")
    full_result = GarchMeanReversionPlugin().run(_StubCtx(full), "T.SH")
    assert short_result.ok and full_result.ok
    for key in ("dev_z", "sigma", "signal", "band_lo", "band_hi"):
        short = short_result.series[key]
        extended = full_result.series[key]
        future_values = dict(zip(extended["dates"], extended["values"]))
        compared = 0
        for date, value in zip(short["dates"], short["values"]):
            if date not in future_values:
                continue
            assert future_values[date] == value, f"{key}@{date} 被未来数据改写"
            compared += 1
        assert compared == 200


def t_insufficient_data():
    """数据不足: ok 结果 + warn 信号（不抛异常、不静默）。"""
    idx = pd.bdate_range("2024-01-02", periods=100)
    df = pd.DataFrame(
        {"open": 10.0, "high": 10.2, "low": 9.8, "close": 10.0, "volume": 1000.0},
        index=idx,
    )
    ctx = _StubCtx(df)
    r = GarchMeanReversionPlugin().run(ctx, "600001.SH")
    assert r.ok
    assert "历史数据不足" in r.summary
    assert any(s.level == "warn" and "数据不足" in s.text for s in r.signals)
    # 框架路径同样降级（years=1 ≈ 244 日 < 需求 300）
    ctx2 = make_context(n_stocks=1, years=1, seed=3)
    try:
        runner = Runner(load_plugins(), ctx2)
        r2 = runner.run("garch_mean_reversion", "600001.SH")
        assert r2.ok and "历史数据不足" in r2.summary
    finally:
        ctx2.cleanup()


def t_realdata_smoke():
    """真实数据冒烟（data/ 不存在时跳过）: 区间命中率与胜率实测。"""
    daily = Path("data/daily/hfq/600519.SH.parquet")
    if not daily.exists():
        print("SKIP: 无真实数据 data/daily/hfq/600519.SH.parquet")
        return
    from analyzer.datastore import DataStore
    from stock_analysis.core.context import AnalysisContext

    store = DataStore("data")
    ctx = AnalysisContext(store)
    runner = Runner(load_plugins(), ctx)
    for code in ("600519.SH", "300750.SZ", "000001.SZ"):
        r = runner.run("garch_mean_reversion", code)
        assert r.ok, f"{code}: {r.error}"
        assert r.summary, code
        m = r.metrics
        # 区间命中率应稳定在 95% 附近（GARCH 标定质量的核心指标）
        assert 0.88 <= m["band_hit"] <= 0.99, f"{code} band_hit={m['band_hit']}"
        assert 0.0 < m["sigma1"] < 0.15, f"{code} sigma1={m['sigma1']}"
        assert 0.0 <= m["persistence"] < 0.98, f"{code} persist={m['persistence']}"
        json.dumps(r.to_dict(), ensure_ascii=False)
        print(f"  {code}: regime={m['regime']} dev_z={m['dev_z']:+.2f} "
              f"band_hit={m['band_hit']:.1%} bt_n={m['bt_count']} "
              f"win5={m['bt_win5']} [{r.elapsed_ms}ms]")


# ------------------------------------------------------------------ 主流程


def main() -> int:
    tests = [
        ("估计器参数恢复", t_estimators),
        ("框架全链路(合成数据)", t_synth_framework_run),
        ("确定性: 黑天鹅无信号区间保留", t_black_swan),
        ("确定性: 温和超卖触发信号", t_oversold_signal),
        ("确定性: 强趋势无信号", t_trending_no_signal),
        ("区间命中率标定", t_band_calibration),
        ("walk-forward 历史前缀不变性", t_walk_forward_prefix_invariance),
        ("数据不足友好降级", t_insufficient_data),
        ("真实数据冒烟", t_realdata_smoke),
    ]
    for name, fn in tests:
        check(name, fn)
    print(f"\n===== 汇总: {PASSED} PASS / {FAILED} FAIL =====")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
