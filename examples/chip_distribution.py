"""筹码分布（成本分布）示例：基于日线的经典衰减算法。

原理（与通达信/同花顺筹码指标同源思路）:
- 每天先让全部存量筹码按当日换手率衰减: chips *= (1 - 换手率)
- 再把当日成交量按三角形分布（峰值在近似成交均价）摊到 [low, high] 价格区间
- 逐日累加得到当前成本分布

换手率: 数据里 turnover 有值就用；否则估算 = 成交量(股) / 流通股本
（流通股本 ≈ 流通市值/最新价，来自 stock_list.parquet；对历史是近似）。

用法:
    python examples/chip_distribution.py            # 默认跑 3 只示例股票
    python examples/chip_distribution.py 600519 300687
输出:
    examples/output/chips_<code>.png + 控制台指标
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

plt.rcParams["font.sans-serif"] = ["PingFang SC", "Hiragino Sans GB", "Heiti SC",
                                   "Arial Unicode MS", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ADJUST = "hfq"
OUT_DIR = Path(__file__).resolve().parent / "output"
DEFAULT_CODES = ["300687.SZ", "300199.SZ", "300170.SZ"]


def _price_step(lo: float, hi: float) -> float:
    rng = hi - lo
    if rng > 400:
        return 0.5
    if rng > 150:
        return 0.2
    if rng > 50:
        return 0.05
    return 0.01


def _tri_weights(bins: np.ndarray, lo: float, hi: float, peak: float) -> np.ndarray:
    """[lo, hi] 区间上以 peak 为峰的三角形分布权重（和=1）。"""
    if hi <= lo:
        return np.ones(1)
    peak = min(max(peak, lo), hi)
    left = (bins - lo) / (peak - lo) if peak > lo else np.ones_like(bins)
    right = (hi - bins) / (hi - peak) if hi > peak else np.ones_like(bins)
    w = np.clip(np.minimum(left, right), 0.0, None)
    s = w.sum()
    return w / s if s > 0 else np.ones_like(bins) / len(bins)


def chip_distribution(df: pd.DataFrame, float_shares: float,
                      decay_scale: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """返回 (价格bins, 筹码分布)。decay_scale 作用于换手率(衰减力度)。"""
    lo = float(df["low"].min()) * 0.995
    hi = float(df["high"].max()) * 1.005
    step = _price_step(lo, hi)
    bins = np.arange(lo, hi + step, step)
    chips = np.zeros(len(bins))
    edge = 1.0  # 价格 -> bin 的容差

    for _, row in df.iterrows():
        l, h, o, c = float(row["low"]), float(row["high"]), float(row["open"]), float(row["close"])
        vol = float(row["volume"]) * 100  # 手 -> 股
        t = float(row["turnover"]) / 100.0 if pd.notna(row.get("turnover")) \
            else (vol / float_shares if float_shares > 0 else 0.01)
        t = min(max(t * decay_scale, 0.0), 1.0)
        chips *= (1.0 - t)
        if vol <= 0:
            continue
        peak = float(row["vwap"]) if pd.notna(row.get("vwap")) else (o + c) / 2.0
        i0 = int(np.clip(np.searchsorted(bins, l) - 1, 0, len(bins) - 1))
        i1 = int(np.clip(np.searchsorted(bins, h) + 1, 0, len(bins) - 1))
        if i1 <= i0:
            i0 = i1 = int(np.searchsorted(bins, min(max(peak, l), h)))
        seg = bins[i0:i1 + 1]
        w = _tri_weights(seg, l, h, min(max(peak, l), h))
        chips[i0:i1 + 1] += w * vol
    return bins, chips


def summarize(bins: np.ndarray, chips: np.ndarray, last_close: float) -> dict:
    total = chips.sum()
    if total <= 0:
        return {}
    cdf = np.cumsum(chips) / total
    q = lambda p: float(np.interp(p, cdf, bins))
    p5, p15, p50, p85, p95 = q(0.05), q(0.15), q(0.50), q(0.85), q(0.95)
    return {
        "avg_cost": float(np.sum(bins * chips) / total),
        "median_cost": p50,
        "range90": (p5, p95),
        "conc90": (p95 - p5) / (p95 + p5),
        "range70": (p15, p85),
        "profit_ratio": float(cdf[int(np.searchsorted(bins, last_close)) - 1]) if last_close < bins[-1] else 1.0,
    }


def run_one(code: str, stock_list: pd.DataFrame, plot: bool = True) -> dict | None:
    path = DATA_DIR / "daily" / ADJUST / f"{code}.parquet"
    if not path.exists():
        print(f"跳过 {code}: 未下载（python -m downloader stocks --codes {code}）")
        return None
    df = pd.read_parquet(path).sort_values("date").reset_index(drop=True)

    rec = stock_list[stock_list["code"] == code]
    float_shares = 0.0
    if len(rec) and pd.notna(rec.iloc[0].get("float_mv")) and pd.notna(rec.iloc[0].get("latest_price")):
        float_shares = float(rec.iloc[0]["float_mv"]) / float(rec.iloc[0]["latest_price"])
    if float_shares <= 0 and len(rec) and pd.notna(rec.iloc[0].get("total_mv")) and pd.notna(rec.iloc[0].get("latest_price")):
        float_shares = float(rec.iloc[0]["total_mv"]) / float(rec.iloc[0]["latest_price"])

    if pd.notna(df["amount"]).any():
        df["vwap"] = df["amount"] / np.maximum(df["volume"] * 100, 1)
    else:
        df["vwap"] = np.nan

    last_close = float(df["close"].iloc[-1])
    bins, chips = chip_distribution(df, float_shares)
    m = summarize(bins, chips, last_close)
    name = str(rec.iloc[0]["name"]) if len(rec) else code
    t = df["turnover"]
    src = "数据自带" if t.notna().any() else "估算(量/当前流通股本)"
    print(f"\n===== {code} {name} | 截止 {df['date'].iloc[-1].date()} | 换手率: {src} =====")
    print(f"  最新价(后复权): {last_close:.2f}")
    print(f"  平均成本: {m['avg_cost']:.2f} | 中位成本: {m['median_cost']:.2f}")
    p5, p95 = m["range90"]
    print(f"  90% 成本区间: [{p5:.2f}, {p95:.2f}]  集中度: {m['conc90'] * 100:.1f}%")
    p15, p85 = m["range70"]
    print(f"  70% 成本区间: [{p15:.2f}, {p85:.2f}]")
    print(f"  获利盘比例: {m['profit_ratio'] * 100:.1f}%")

    if plot:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        fig, ax = plt.subplots(figsize=(10, 5.5))
        pct = chips / chips.sum() * 100
        ax.bar(bins, pct, width=(bins[1] - bins[0]) * 0.9, color="#4878cf", alpha=0.9)
        ax.axvline(last_close, color="red", ls="--", lw=1.2,
                   label=f"最新价 {last_close:.2f}")
        ax.axvline(m["avg_cost"], color="green", ls=":", lw=1.2,
                   label=f"平均成本 {m['avg_cost']:.2f}")
        ax.set_yscale("log")
        ax.set_xlabel("价格（后复权）")
        ax.set_ylabel("筹码占比 %（对数轴）")
        ax.set_title(f"筹码分布 {code} {name}（90%区间 {p5:.1f}~{p95:.1f}，获利盘 {m['profit_ratio']*100:.0f}%）")
        ax.legend()
        fig.tight_layout()
        out = OUT_DIR / f"chips_{code.replace('.', '')}.png"
        fig.savefig(out, dpi=150)
        print(f"  图表: {out}")
    return {"code": code, **m}


def main() -> None:
    codes = sys.argv[1:] or DEFAULT_CODES
    sl_path = DATA_DIR / "stock_list.parquet"
    stock_list = pd.read_parquet(sl_path) if sl_path.exists() else pd.DataFrame(columns=["code", "name", "float_mv", "total_mv", "latest_price"])
    for c in codes:
        from downloader.codes import to_wind_code

        run_one(to_wind_code(c), stock_list)
    print("\n说明: 后复权口径的成本分布（价格轴为后复权价，与收益口径一致；"
          "想看实际价格轴可另下 qfq 数据后同样计算）")


if __name__ == "__main__":
    main()
