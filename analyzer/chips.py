"""筹码分布（成本分布）：经典换手衰减算法的向量化服务版。

原理（与通达信/同花顺筹码指标同源，见 examples/chip_distribution.py）:
- 每天先让全部存量筹码按当日换手率衰减: chips *= (1 - 换手率)
- 再把当日成交量按三角形分布（峰值在近似成交均价）摊到 [low, high] 价格区间
- 逐日累加得到当前成本分布

本模块相对示例脚本的改进:
- 数组化逐日循环（不用 iterrows），30 年历史 ~8000 日 < 0.5s
- 自适应分箱（目标 ≤200 箱，步长取"整齐"数值），长历史高价位股票不再产生上万箱
- 换手率缺失时回退 成交量/流通股本（流通股本 ≈ 流通市值/最新价，历史近似）
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MAX_BINS = 200
_NICE_STEPS = (0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10,
               20, 50, 100, 200, 500, 1000)


def _bins_for(lo: float, hi: float) -> np.ndarray:
    rng = hi - lo
    if rng <= 0:
        return np.array([lo])
    raw = rng / MAX_BINS
    step = next((s for s in _NICE_STEPS if s >= raw), 1000.0)
    return np.arange(lo, hi + step / 2, step)


def _tri_weights(seg: np.ndarray, lo: float, hi: float, peak: float) -> np.ndarray:
    """[lo, hi] 上以 peak 为峰的三角形分布权重（和=1）。"""
    if hi <= lo:
        return np.ones(len(seg))
    peak = min(max(peak, lo), hi)
    left = (seg - lo) / (peak - lo) if peak > lo else np.ones_like(seg)
    right = (hi - seg) / (hi - peak) if hi > peak else np.ones_like(seg)
    w = np.clip(np.minimum(left, right), 0.0, None)
    s = w.sum()
    return w / s if s > 0 else np.ones_like(seg) / len(seg)


def compute_chips(df: pd.DataFrame, float_shares: float) -> dict:
    """输入按日期升序的日线（后复权），返回 bins/chips/stats。"""
    low = df["low"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    open_ = df["open"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    vol = df["volume"].to_numpy(dtype=float) * 100.0  # 手 -> 股
    turnover = df["turnover"].to_numpy(dtype=float) if "turnover" in df else np.full(len(df), np.nan)
    amount = df["amount"].to_numpy(dtype=float) if "amount" in df else np.full(len(df), np.nan)

    with np.errstate(divide="ignore", invalid="ignore"):
        vwap = np.where((amount > 0) & (vol > 0), amount / np.maximum(vol, 1.0), (open_ + close) / 2.0)
    vwap = np.where(np.isfinite(vwap), vwap, (open_ + close) / 2.0)

    # 换手率：缺失时回退 量/流通股本
    t = turnover / 100.0
    fallback = 0.01
    if float_shares > 0:
        fallback = np.clip(vol / float_shares, 0.0, 1.0)
    t = np.where(np.isnan(t), fallback, t)
    t = np.clip(t, 0.0, 1.0)

    lo = float(low.min()) * 0.995
    hi = float(high.max()) * 1.005
    bins = _bins_for(lo, hi)
    n = len(bins)
    chips = np.zeros(n)

    i0_all = np.clip(np.searchsorted(bins, low) - 1, 0, n - 1)
    i1_all = np.clip(np.searchsorted(bins, high) + 1, 0, n - 1)
    peak = np.clip(vwap, low, high)
    peak_bin = np.clip(np.searchsorted(bins, peak), 0, n - 1)

    for i in range(len(df)):
        if t[i] > 0:
            chips *= (1.0 - t[i])
        v = vol[i]
        if not np.isfinite(v) or v <= 0:
            continue
        i0, i1 = i0_all[i], i1_all[i]
        if i1 <= i0:
            i0 = i1 = peak_bin[i]
        chips[i0:i1 + 1] += _tri_weights(bins[i0:i1 + 1], low[i], high[i], peak[i]) * v

    return {"bins": bins, "chips": chips, "last_close": float(close[-1]),
            "n_days": int(len(df)), "turnover_estimated": bool(np.isnan(turnover).all())}


def summarize(bins: np.ndarray, chips: np.ndarray, last_close: float) -> dict:
    total = chips.sum()
    if total <= 0:
        return {}
    cdf = np.cumsum(chips) / total
    q = lambda p: float(np.interp(p, cdf, bins))
    p5, p15, p50, p85, p95 = q(0.05), q(0.15), q(0.50), q(0.85), q(0.95)
    idx = int(np.searchsorted(bins, last_close))
    profit = float(cdf[min(idx, len(bins) - 1) - 1]) if idx > 0 and last_close < bins[-1] else 1.0
    return {
        "avg_cost": float(np.sum(bins * chips) / total),
        "median_cost": p50,
        "range90": [p5, p95],
        "conc90": (p95 - p5) / (p95 + p5),
        "range70": [p15, p85],
        "profit_ratio": profit,
    }
