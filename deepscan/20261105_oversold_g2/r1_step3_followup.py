#!/usr/bin/env python3
"""R1 step3：补充验证（今日制度状态/恐慌周明细/深度×制度交叉/V日次日gap/同周相关/正确p值）"""
import math
import os
import numpy as np
import pandas as pd
import sys
sys.path.insert(0, "/root/workspaces/quant-trading/deepscan/20261105_oversold_g2")
from r1_step2_attacks import t_p, spearman, load_idx  # noqa: E402  复用已验证实现

OUT = "/root/workspaces/quant-trading/deepscan/20261105_oversold_g2"
W = pd.read_csv(os.path.join(OUT, "r1_weekly_feats.csv"))
W["wk"] = pd.to_datetime(W["wk"])
sig = pd.read_csv(os.path.join(OUT, "r1_signals.csv"))
g2 = sig[sig["g2"]].copy()
g2["wk"] = pd.to_datetime(g2["wk"])
daily = pd.read_csv(os.path.join(OUT, "r1_daily.csv"))

print("== t_p 自检（应≈0.0734 / 0.3672）==")
print(f"  t=2.0,dof=10 → p={t_p(2.0, 10):.4f}   t=-0.91,dof=44 → p={t_p(-0.91, 44):.4f}")

print("\n== 今日（2026-11-05）指数制度状态 ==")
for code, name in [("399006.SZ", "创业板指"), ("000852.SH", "中证1000")]:
    d = load_idx(code)
    c = d["close"].values
    ret20 = c[-1] / c[-21] - 1
    ma60 = c[-60:].mean()
    vol20 = np.std(np.diff(c[-21:]) / c[-21:-1], ddof=1) * math.sqrt(250)
    print(f"  {name}: ret20 {ret20:+.2%}  close/MA60 {c[-1]/ma60-1:+.2%}  vol20 {vol20:.0%}")

print("\n== 恐慌周（创业板指 ret20<=-10%）明细 ==")
panic = W[W["cyb_r20"] <= -0.10]
for _, r in panic.iterrows():
    print(f"  {r['wk'].date()}: 信号数 {r['n_sig']:.0f} 组合T+10 {r['port_mean']:+.2%} 中位 {r['port_median']:+.2%} 指数T+10 {r['cyb_r10']:+.2%}")
nonpanic = W[W["cyb_r20"] > -0.10]
print(f"  恐慌周 {len(panic)} 个 vs 非恐慌周 {len(nonpanic.dropna(subset=['port_mean']))} 个")
pn = nonpanic.dropna(subset=["port_mean"])
print(f"  非恐慌周组合: 均值 {pn['port_mean'].mean():+.2%} 中位 {pn['port_mean'].median():+.2%} 为正比例 {(pn['port_mean']>0).mean():.1%}")

print("\n== 修正后的关键 p 值 ==")
print(f"  周组合 vs 中证1000: alpha t=-0.91 dof=44 → p={t_p(-0.91, 44):.3f}（不显著）")
print(f"  pooled alpha t=0.61 dof=45 → p={t_p(0.61, 45):.3f}（不显著）")
print(f"  Spearman(周计数,周收益) rho=+0.217 n=46 → p={spearman(W['n_sig'].dropna(), W.dropna(subset=['n_sig'])['port_mean'])[1]:.3f}")

print("\n== ret20 深度 × 制度（恐慌周 vs 非恐慌周）交叉 ==")
g2["deep"] = pd.cut(g2["ret20"], [-1, -0.40, -0.30, -0.25, -0.20],
                    labels=["≤-40%", "-40~-30%", "-30~-25%", "-25~-20%"])
g2["is_panic"] = g2["wk"].isin(panic["wk"])
for pk in [True, False]:
    sub = g2[g2["is_panic"] == pk]
    print(f"  —— {'恐慌周' if pk else '非恐慌周'} (N={len(sub)}) ——")
    for b, d in sub.groupby("deep", observed=True):
        print(f"    {b}: N={len(d)} 中位 {d['t10'].median():+.2%} 胜率 {(d['t10']>0).mean():.1%}")

print("\n== 实盘候选 4 只的 ret20 深度在历史样本的分位 ==")
print(f"  候选 ret20: -45.6/-45.6/-43.4/-42.7 → 历史 G2 信号 ret20 分位数: P5 {g2['ret20'].quantile(0.05):.1%} P10 {g2['ret20'].quantile(0.10):.1%} 中位 {g2['ret20'].median():.1%}")

print("\n== 近 10 个交易日信号计数（362 是孤立尖峰还是持续?）==")
print(daily.tail(10)[["date", "n_sig", "t10_mean"]].to_string(index=False))

print("\n== V 日次日开盘缺口（执行口径的关键假设）==")
cyb = load_idx("399006.SZ")
cyb["prev_close"] = cyb["close"].shift(1)
cyb["open_gap"] = cyb["open"] / cyb["prev_close"] - 1
cyb["chg"] = cyb["close"] / cyb["prev_close"] - 1
vdays = cyb[(cyb["open_gap"] <= -0.015) & (cyb["chg"] >= 0.005)]
print(f"  历史 V 日（低开<=-1.5% 且收涨>=0.5%）: {len(vdays)} 天")
print("  " + ", ".join(str(d.date()) for d in vdays["date"].tail(12)))
# 这些 V 日之后信号票的次日开盘 gap 与 exec 口径收益
vdates = set(pd.to_datetime(vdays["date"]))
g2v = g2[g2["wk"].isin(vdates)]
if len(g2v):
    print(f"  V 日信号票: N={len(g2v)} 隔夜gap 中位 {g2v['o_next'].div(g2v['c_sig']).sub(1).median():+.2%}")
    print(f"  V 日 close口径 t10 中位 {g2v['t10'].median():+.2%} vs exec口径 {g2v['t10_exec'].median():+.2%}")
else:
    print("  （周信号日中无 V 日样本——改用全部信号日的 gap 分布）")

print("\n== 同周信号相关系数（4 槽=1个赌注?）——周内两两相关均值 ==")
cors = []
for wk, d in g2.groupby("wk"):
    if len(d) >= 10:
        r = d["t10"].values
        r = r - r.mean()
        n = len(r)
        denom = (r @ r) ** 2 - (r ** 2 @ r ** 2)
        if denom != 0:
            # 平均两两相关 = (S² - Σx²) / ((n-1)·Σx²)
            s2 = (r.sum() ** 2 - (r ** 2).sum()) / 2
            cors.append(((r.sum()) ** 2 - (r ** 2).sum()) / ((n - 1) * (r ** 2).sum()))
cors = pd.Series(cors, dtype=float)
print(f"  有≥10信号的周数 {len(cors)}，周内平均两两相关: 均值 {cors.mean():.2f} 中位 {cors.median():.2f}")
print(f"  → 4 只同日票组合波动 ≈ 单票波动的 {math.sqrt((1+3*cors.median())/4):.2f} 倍（完全独立=0.50，单票=1.00）")

print("\n== 周组合 vs universe 等权（同周对照）==")
ex = W.dropna(subset=["port_mean", "mkt_mean"])
diff = ex["port_mean"] - ex["mkt_mean"]
print(f"  周数 {len(ex)} 组合-同行基线: 均值 {diff.mean():+.2%}/周 中位 {diff.median():+.2%} 为正比例 {(diff>0).mean():.1%}")
