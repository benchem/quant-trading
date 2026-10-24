"""快速上手示例：读取 build 合并后的数据集，做基础市场分析。

前置条件:
    python -m downloader all     # 下载/更新数据
    python -m downloader build   # 合并建模数据集

运行:
    python examples/quick_analysis.py
输出:
    控制台统计 + examples/output/*.png
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 无图形界面环境也能运行
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

# 中文字体（macOS 常见字体），找不到时回退默认（图内中文可能显示为方块，不影响数据）
plt.rcParams["font.sans-serif"] = ["PingFang SC", "Hiragino Sans GB", "Heiti SC",
                                   "Arial Unicode MS", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ADJUST = "hfq"  # 与下载时 --adjust 保持一致
OUT_DIR = Path(__file__).resolve().parent / "output"

ANNUALIZE = 244  # A股年均交易日约 243~244


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    daily_path = DATA_DIR / "dataset" / f"daily_{ADJUST}.parquet"
    idx_path = DATA_DIR / "dataset" / "index_daily.parquet"
    if not daily_path.exists():
        raise SystemExit(f"缺少 {daily_path}，请先运行: python -m downloader all && python -m downloader build")
    daily = pd.read_parquet(daily_path, engine="pyarrow")
    print(
        f"日线: {len(daily):,} 行 | {daily['code'].nunique():,} 只股票 | "
        f"{daily['date'].min().date()} ~ {daily['date'].max().date()}"
    )

    # 1) 个股日收益率：后复权 close 的 pct_change ≈ 含分红再投资的日总收益
    daily = daily.sort_values(["code", "date"]).reset_index(drop=True)
    daily["ret"] = daily.groupby("code", sort=False)["close"].pct_change()

    # 2) 全市场等权组合 vs 沪深300（累计净值，对数轴）
    eq = daily.groupby("date")["ret"].mean().rename("等权全市场")
    if idx_path.exists():
        idx = pd.read_parquet(idx_path, engine="pyarrow")
        hs300 = (
            idx[idx["code"] == "000300.SH"]
            .sort_values("date")
            .set_index("date")["close"]
            .pct_change()
            .rename("沪深300")
        )
        cmp = pd.concat([eq, hs300], axis=1).dropna()
        nav = (1 + cmp).cumprod()

        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot(nav.index, nav["等权全市场"], lw=1.0, label="等权全市场")
        ax.plot(nav.index, nav["沪深300"], lw=1.0, label="沪深300")
        ax.set_yscale("log")
        ax.set_title("累计净值对比（对数轴）")
        ax.legend()
        fig.tight_layout()
        fig.savefig(OUT_DIR / "market_overview.png", dpi=150)
        print(f"净值对比 -> {OUT_DIR / 'market_overview.png'}")

        # 3) 市场宽度：每日上涨家数 - 下跌家数（近一年）
        breadth = (
            daily.groupby("date")["ret"]
            .apply(lambda s: int((s > 0).sum()) - int((s < 0).sum()))
        )
        recent_b = breadth.tail(244)
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.bar(recent_b.index, recent_b.values, width=1.0)
        ax.axhline(0, color="k", lw=0.8)
        ax.set_title("市场宽度（上涨家数 - 下跌家数，近一年）")
        fig.tight_layout()
        fig.savefig(OUT_DIR / "breadth.png", dpi=150)
        print(f"市场宽度 -> {OUT_DIR / 'breadth.png'}")
    else:
        print("(未下载指数数据，跳过指数对比)")

    # 4) 个股风险统计示例：最近一年日收益的年化波动率
    recent = daily[daily["date"] >= daily["date"].max() - pd.Timedelta(days=365)]
    vol = recent.groupby("code")["ret"].std() * np.sqrt(ANNUALIZE)
    vol = vol.dropna()
    print("\n最近一年个股年化波动率分布（分位数, %）:")
    for q in (0.1, 0.25, 0.5, 0.75, 0.9):
        print(f"  P{int(q * 100):>2}: {vol.quantile(q) * 100:6.1f}")
    top = vol.sort_values(ascending=False).head(5)
    names = daily.drop_duplicates("code").set_index("code")["name"]
    print("\n波动率最高 5 只:")
    for code, v in top.items():
        print(f"  {code} {names.get(code, '')}: {v * 100:.1f}%")

    # 5) 数据质量速览
    missing = int(daily[["open", "high", "low", "close", "volume"]].isna().sum().sum())
    print(f"\n核心字段 OHLCV 缺失值: {missing}")
    print(f"图表输出目录: {OUT_DIR}")


if __name__ == "__main__":
    main()
