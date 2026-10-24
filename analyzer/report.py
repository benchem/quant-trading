"""静态报告模式：基于 matplotlib 输出 PNG 图表 + 控制台摘要。

用法:
    python -m analyzer report [--out analyzer/output] [--years 10]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from .datastore import ANNUALIZE, DataStore, INDEX_NAMES

plt.rcParams["font.sans-serif"] = [
    "PingFang SC", "Hiragino Sans GB", "Heiti SC", "Arial Unicode MS", "SimHei", "DejaVu Sans",
]
plt.rcParams["axes.unicode_minus"] = False

UP, DOWN, ACCENT = "#ef4b57", "#2ebd85", "#4f8ef7"


def report(data_dir: str = "data", out_dir: str | Path = "analyzer/output", years: int = 10) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    store = DataStore(data_dir)
    m = store.market
    since = m.index.max() - pd.DateOffset(years=years)
    ms = m[m.index >= since]

    print(f"股票数: {store.meta['n_stocks']} | 区间: {since.date()} ~ {m.index.max().date()}")

    # 1) 指数对比（归一化，对数轴）
    fig, ax = plt.subplots(figsize=(11, 5.2))
    for code, idf in store.indexes.items():
        s = idf["close"].reindex(ms.index)
        s = s / s.iloc[0] * 100
        ax.plot(s.index, s.values, lw=1.2, label=INDEX_NAMES.get(code, code))
    ax.set_yscale("log")
    ax.set_title(f"主要指数对比（归一化=100，近{years}年，对数轴）")
    ax.legend(ncol=4, fontsize=9)
    ax.grid(alpha=.3)
    fig.tight_layout()
    fig.savefig(out / "01_index_compare.png", dpi=150)
    plt.close(fig)
    print(f"  -> {out / '01_index_compare.png'}")

    # 2) 等权全市场 vs 沪深300
    fig, ax = plt.subplots(figsize=(11, 5.2))
    nav = ms["nav_eqw"]
    ax.plot(nav.index, nav.values / nav.iloc[0] * 100, lw=1.4, color=ACCENT, label="等权全市场")
    hs = store.indexes.get("000300.SH")
    if hs is not None:
        s = hs["close"].reindex(ms.index)
        ax.plot(s.index, s.values / s.iloc[0] * 100, lw=1.2, color="#f0a03c", ls="--", label="沪深300")
    ax.set_yscale("log")
    ax.set_title(f"累计净值：等权全市场 vs 沪深300（近{years}年，对数轴）")
    ax.legend(fontsize=9)
    ax.grid(alpha=.3)
    fig.tight_layout()
    fig.savefig(out / "02_eqw_nav.png", dpi=150)
    plt.close(fig)
    print(f"  -> {out / '02_eqw_nav.png'}")

    # 3) 市场宽度 + 成交额（近一年双子图）
    yr = ms.tail(ANNUALIZE)
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True,
                                 gridspec_kw={"height_ratios": [1, 1.1]})
    a1.fill_between(yr.index, 0, yr["adv"], color=UP, alpha=.55, label="上涨家数")
    a1.fill_between(yr.index, 0, -yr["dec"], color=DOWN, alpha=.55, label="下跌家数")
    a1.legend(fontsize=9)
    a1.set_title("市场宽度（近一年）")
    a1.grid(alpha=.3)
    amt = yr["amount_yi"].dropna()
    if len(amt):
        a2.fill_between(amt.index, 0, amt.values, color=ACCENT, alpha=.5)
        a2.set_title("全市场日成交额（近一年，亿元）")
        a2.grid(alpha=.3)
    fig.tight_layout()
    fig.savefig(out / "03_breadth_amount.png", dpi=150)
    plt.close(fig)
    print(f"  -> {out / '03_breadth_amount.png'}")

    # 4) 年度等权收益
    yearly = (1 + ms["mean_ret"]).groupby(ms.index.year).prod() - 1
    fig, ax = plt.subplots(figsize=(11, 4.6))
    colors = [UP if v >= 0 else DOWN for v in yearly.values]
    ax.bar(yearly.index.astype(str), yearly.values * 100, color=colors)
    ax.axhline(0, color="k", lw=.8)
    ax.set_title(f"等权全市场年度收益（%）")
    ax.grid(alpha=.3, axis="y")
    plt.xticks(rotation=45)
    fig.tight_layout()
    fig.savefig(out / "04_yearly_return.png", dpi=150)
    plt.close(fig)
    print(f"  -> {out / '04_yearly_return.png'}")

    # 5) 近一年个股波动率分布
    import pyarrow.dataset as pad
    ds = pad.dataset(store.daily_dir, format="parquet")
    t = ds.to_table(
        filter=pad.field("date") >= (m.index.max() - pd.Timedelta(days=365)),
        columns=["date", "code", "close"],
    ).to_pandas()
    t["date"] = pd.to_datetime(t["date"])
    t = t.sort_values(["code", "date"])
    vol = t.groupby("code")["close"].std() * np.sqrt(ANNUALIZE)
    fig, ax = plt.subplots(figsize=(9, 4.6))
    vol.plot.hist(bins=60, color=ACCENT, alpha=.75, ax=ax)
    ax.set_title(f"个股年化波动率分布（近一年，中位数 {vol.median()*100:.1f}%）")
    ax.grid(alpha=.3, axis="y")
    fig.tight_layout()
    fig.savefig(out / "05_volatility_dist.png", dpi=150)
    plt.close(fig)
    print(f"  -> {out / '05_volatility_dist.png'}")

    # 控制台摘要
    last = m.iloc[-1]
    print("\n=== 最近交易日摘要 ===")
    print(f"日期: {m.index.max().date()}")
    print(f"上涨/下跌/平盘: {int(last['adv'])} / {int(last['dec'])} / {int(last['flat'])}")
    print(f"等权平均涨跌: {last['mean_ret']*100:+.2f}%")
    if last["amount_yi"] == last["amount_yi"]:
        print(f"全市场成交额: {last['amount_yi']/1e4:.2f} 万亿元" if last['amount_yi'] >= 1e4
              else f"全市场成交额: {last['amount_yi']:.0f} 亿元")
    print("\n近10年年度等权收益:")
    for y, v in yearly.tail(10).items():
        print(f"  {y}: {v*100:+7.1f}%")
    print(f"\n图表目录: {out.resolve()}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out", default="analyzer/output")
    ap.add_argument("--years", type=int, default=10)
    a = ap.parse_args()
    report(a.data_dir, a.out, a.years)
