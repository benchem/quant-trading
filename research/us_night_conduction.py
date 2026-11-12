#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""us_night_conduction.py — 纳指夜 → 次日A股(创业板指) 传导基率研究

背景: 建议C(regime闸)建闸前的扩样本重算(2026-11-07 批复点3②)。
此前证据仅 n≈6 夜(10-29/30 完整样本+本周4对)。本研究回溯 2023-04 起全部
"美股session T → A股次日" 配对, 回答:
  1. 纳指夜涨跌分桶 → 次日创业板 开盘缺口/收盘涨跌/日内方向 的条件分布
  2. 恐慌夜(≤-2%/-3%/-4%)后 A股次日是否显著更差 → kill-switch 的基率依据
  3. 恐慌夜后的反弹夜(先≤-3%后≥+1%, 本周11-05→11-06形态) → 次日A股表现
口径: 基率研究非策略, 不含执行成本, 不构成买卖规则; US=close-to-close,
A缺口=开/前收-1, A收盘=收/前收-1。数据: 腾讯 ifzq usIXIC 日线(与 kill 红线
同源, 900根上限≈3.6年) + 本地 399006.SZ.parquet。
运行: .venv/bin/python research/us_night_conduction.py
"""
import json
from urllib.request import Request, build_opener, ProxyHandler

import numpy as np
import pandas as pd

US_URL = "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get?param=usIXIC,day,,,900,qfq"
A_PARQUET = "/opt/quant-trading/data/index/399006.SZ.parquet"


def load_us():
    op = build_opener(ProxyHandler({}))
    d = json.loads(op.open(Request(US_URL, headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read())
    kl = d["data"]["usIXIC"]["day"]
    us = pd.DataFrame([r[:5] for r in kl], columns=["date", "open", "close", "high", "low"])
    us["date"] = pd.to_datetime(us["date"])
    for c in ("open", "close", "high", "low"):
        us[c] = us[c].astype(float)
    us = us.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    us["r_us"] = us["close"].pct_change()
    return us


def load_a():
    a = pd.read_parquet(A_PARQUET)[["date", "open", "close"]]
    a["date"] = pd.to_datetime(a["date"])
    a = a.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    a["gap"] = a["open"] / a["close"].shift(1) - 1
    a["ret"] = a["close"] / a["close"].shift(1) - 1
    a["intraday"] = a["close"] / a["open"] - 1
    return a


def align(a, us):
    """每个A股日 D 配对: U = 最后一个 date < D 的美股session(该session于北京时间D日05:00前收盘)"""
    idx = np.searchsorted(us["date"].values, a["date"].values, side="left") - 1
    ok = idx >= 1
    out = a[ok].copy()
    ui = idx[ok]
    out["us_date"] = us["date"].values[ui]
    out["r_us"] = us["r_us"].values[ui]
    out["r_us_prev"] = us["r_us"].values[ui - 1]
    return out.dropna(subset=["gap", "ret", "r_us"]).reset_index(drop=True)


def stat_block(g, label):
    n = len(g)
    if n == 0:
        return f"{label}: n=0"
    return (f"{label}: n={n} | 开盘缺口 中位{g['gap'].median():+.2%} 均值{g['gap'].mean():+.2%} | "
            f"收盘 中位{g['ret'].median():+.2%} 均值{g['ret'].mean():+.2%} | "
            f"P(收跌)={(g['ret']<0).mean():.0%} P(低开)={(g['gap']<0).mean():.0%} "
            f"P(收<开)={(g['intraday']<0).mean():.0%}")


def main():
    us = load_us()
    a = load_a()
    g = align(a, us)
    print(f"美股session: {us['date'].min().date()} ~ {us['date'].max().date()} 共{len(us)}根")
    print(f"A股配对样本: {g['date'].min().date()} ~ {g['date'].max().date()} 共{len(g)}对")
    for d, tag in (("2026-11-05", "US11-04夜→A股11-05"), ("2026-11-06", "US11-05夜→A股11-06")):
        row = g[g["date"] == d]
        if len(row):
            r = row.iloc[0]
            print(f"锚点自检 {tag}: r_us={r['r_us']:+.2%} A_gap={r['gap']:+.2%} A_ret={r['ret']:+.2%}")

    print("\n═══ 分桶条件分布 (全窗口) ═══")
    bins = [-1, -0.04, -0.03, -0.02, -0.01, 0, 0.01, 0.02, 0.03, 0.04, 1]
    labels = ["≤-4%", "-4~-3", "-3~-2", "-2~-1", "-1~0", "0~+1", "+1~+2", "+2~+3", "+3~+4", "≥+4%"]
    gg = g.copy()
    gg["bucket"] = pd.cut(gg["r_us"], bins=bins, labels=labels)
    rows = []
    for lab, sub in gg.groupby("bucket", observed=True):
        rows.append({
            "桶": lab, "n": len(sub),
            "缺口中位": f"{sub['gap'].median():+.2%}", "缺口均值": f"{sub['gap'].mean():+.2%}",
            "收盘中位": f"{sub['ret'].median():+.2%}", "收盘均值": f"{sub['ret'].mean():+.2%}",
            "P收跌": f"{(sub['ret']<0).mean():.0%}", "P低开": f"{(sub['gap']<0).mean():.0%}",
            "P收<开": f"{(sub['intraday']<0).mean():.0%}",
        })
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n═══ 无条件基准 ═══")
    print(stat_block(g, "全部"))

    print("\n═══ 恐慌夜 (kill-switch 基率依据) ═══")
    base_med = g["ret"].median()
    for th in (-0.02, -0.03, -0.04):
        sub = g[g["r_us"] <= th]
        print(stat_block(sub, f"US夜≤{th:.0%}"))
        if len(sub) >= 5:
            print(f"   ↳ 次日收盘中位 vs 全样本中位: {sub['ret'].median()-base_med:+.2%}")

    print("\n═══ 极端夜明细 (US夜≤-3%) ═══")
    for _, r in g[g["r_us"] <= -0.03].iterrows():
        print(f"   {r['us_date'].date()} US{r['r_us']:+.2%} → A{r['date'].date()} gap{r['gap']:+.2%} ret{r['ret']:+.2%}")

    print("\n═══ 恐慌夜→反弹夜 (本周11-05→11-06形态) ═══")
    bounce = g[(g["r_us_prev"] <= -0.03) & (g["r_us"] >= 0.01)]
    print(stat_block(bounce, "先≤-3%夜, 再≥+1%夜 → 次日A股"))
    for _, r in bounce.iterrows():
        print(f"   {r['date'].date()} ← US夜{r['r_us']:+.2%}(前夜{r['r_us_prev']:+.2%}) | A gap{r['gap']:+.2%} ret{r['ret']:+.2%}")

    print("\n═══ 近12个月子窗口 ═══")
    recent = g[g["date"] >= "2025-11-07"]
    print(stat_block(recent, "近12月全部"))
    for th in (-0.02, -0.03):
        print(stat_block(recent[recent["r_us"] <= th], f"近12月 US夜≤{th:.0%}"))

    print("\n═══ 目标重算算术 (批复点3①) ═══")
    acct = 130505
    sleeve = 0.3 * acct
    for wk in (500, 800, 3000):
        print(f"周{wk}元 = 全账户年化 {wk*52/acct:.0%} | 30%袖仓年化 {wk*52/sleeve:.0%}")


if __name__ == "__main__":
    main()
