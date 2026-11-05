#!/usr/bin/env python3
"""R1 补刀分析：队列集中度、ret20 尾部、追价成本、V日明细"""
import os
import numpy as np, pandas as pd

D = "/root/workspaces/quant-trading/deepscan/20261105_oversold_g2"
sig = pd.read_csv(os.path.join(D, "r1_execflow_signals.csv"), parse_dates=["sd"])
mk = pd.read_csv(os.path.join(D, "r1_execflow_market.csv"), parse_dates=["date"])
COST = 0.0068
sig["execnet"] = sig["execret"] - COST
sig = sig.merge(mk.rename(columns={"date": "sd"})[["sd", "ew_ret", "breadth"]], on="sd", how="left")
sig["nsig"] = sig.groupby("sd")["code"].transform("size")
pd.set_option("display.width", 220)

print("=" * 70)
print("A. 队列（信号日）集中度分解")
print("=" * 70)
per = sig.groupby("sd").agg(n=("code", "size"), med=("execnet", "median"),
                            win=("execnet", lambda x: (x > 0).mean()), ew_ret=("ew_ret", "first"),
                            breadth=("breadth", "first")).sort_values("n", ascending=False)
print("按信号家数排序的前 12 个队列日:")
print(per.head(12).to_string(float_format=lambda x: f"{x:+.3f}" if abs(x) < 1.5 else f"{x:.0f}"))
print(f"\n队列日总数 {len(per)}，N>=100 的队列日 {(per['n']>=100).sum()} 个，贡献样本 {per[per['n']>=100]['n'].sum()} ({per[per['n']>=100]['n'].sum()/len(sig):.0%})")
top5 = per.head(5).index
print(f"剔除样本数最多的前5个队列日后: N={len(sig[~sig['sd'].isin(top5)])} 中位 {sig[~sig['sd'].isin(top5)]['execnet'].median():+.2%} 胜率 {(sig[~sig['sd'].isin(top5)]['execnet']>0).mean():.1%}")
print(f"强涨日(ew_ret>=+1.5%)信号 {len(sig[sig['ew_ret']>=0.015])} 条，来自 {(sig[sig['ew_ret']>=0.015])['sd'].nunique()} 个队列日:")
sd = sig[sig['ew_ret'] >= 0.015].groupby("sd").agg(n=("code", "size"), med=("execnet", "median"))
print(sd.to_string(float_format=lambda x: f"{x:+.3f}" if abs(x) < 1.5 else f"{x:.0f}"))
print(f"\n本周信号日星期分布: {sig.groupby('wd')['code'].size().to_dict()} (4=周五)")

print("\n" + "=" * 70)
print("B. ret20 尾部匹配（4候选在 -43%~-46%）")
print("=" * 70)
for lo, hi, lab in [(-1, -0.40, "ret20<=-40%"), (-0.40, -0.35, "-40~-35%"), (-0.35, -0.30, "-35~-30%"), (-0.30, -0.25, "-30~-25%"), (-0.25, -0.20, "-25~-20%")]:
    s = sig[(sig["ret20"] > lo) & (sig["ret20"] <= hi)]
    if len(s) == 0:
        continue
    print(f"  {lab:12s}: N={len(s):5d} ({len(s)/len(sig):.1%})  execnet中位 {s['execnet'].median():+.2%}  胜率 {(s['execnet']>0).mean():.1%}  gap中位 {s['gap'].median():+.3%}")
print(f"  候选4只 ret20 区间 -45.6%~-42.7%，回测样本 ret20 中位 {sig['ret20'].median():.1%}、p5 {sig['ret20'].quantile(.05):.1%}")

print("\n" + "=" * 70)
print("C. 09:30 追价成本（h1_run=入场日最高/开盘-1，对手价晚1分钟的上界）")
print("=" * 70)
for lab, m in [("全样本", np.ones(len(sig), bool)),
               ("强涨日信号(ew_ret>=1.5%)", sig["ew_ret"] >= 0.015),
               ("共振日信号(nsig>=100)", sig["nsig"] >= 100)]:
    s = sig[m]
    print(f"  {lab:22s}: h1_run 中位 {s['h1_run'].median():+.2%}  p75 {s['h1_run'].quantile(.75):+.2%}  p90 {s['h1_run'].quantile(.9):+.2%}  l1_dip 中位 {s['l1_dip'].median():+.2%}")

print("\n" + "=" * 70)
print("D. 13 个历史 V 日明细及其次日市场 gap")
print("=" * 70)
mk52 = mk[mk["date"] >= "2025-11-01"].copy()
mk52["tr20"] = mk52["ew_ret"].rolling(20).sum()
v = mk52[(mk52["ew_ret"] >= 0.015) & (mk52["tr20"] <= -0.03) & (mk52["date"] < "2026-11-05")]
out = []
for d in v["date"]:
    nxt = mk[mk["date"] > d].head(1)
    out.append((d.date(), v.loc[v["date"] == d, "ew_ret"].iloc[0], v.loc[v["date"] == d, "tr20"].iloc[0],
                v.loc[v["date"] == d, "breadth"].iloc[0],
                nxt["ew_gap"].iloc[0] if len(nxt) else np.nan, nxt["ew_ret"].iloc[0] if len(nxt) else np.nan))
print(pd.DataFrame(out, columns=["V日", "ew_ret", "tr20", "breadth", "次日gap", "次日ret"]).to_string(
    index=False, float_format=lambda x: f"{x:+.3f}" if abs(x) < 1.5 else f"{x:.0f}"))
ng = pd.Series([o[4] for o in out]).dropna()
print(f"V日次日 gap: 均值 {ng.mean():+.3%} 中位 {ng.median():+.3%}  其中高开(>0)比例 {(ng>0).mean():.0%} ({(ng>0).sum()}/{len(ng)})")
print(f"11-05: ew_ret {mk52[mk52['date']=='2026-11-05']['ew_ret'].iloc[0]:+.2%}, tr20 {mk52[mk52['date']=='2026-11-05']['tr20'].iloc[0]:+.2%}, breadth {int(mk52[mk52['date']=='2026-11-05']['breadth'].iloc[0])}")

print("\n" + "=" * 70)
print("E. 现计划 4 候选 vs 回测入场分布的另一切面：入场日成交额")
print("=" * 70)
s2 = sig[sig["amount_yi"] >= 2]
print(f"回测信号 额>=2亿 子集: N={len(s2)} execnet中位 {s2['execnet'].median():+.2%} 胜率 {(s2['execnet']>0).mean():.1%}")
print(f"回测信号成交额分布(亿): 中位 {sig['amount_yi'].median():.1f}  p10 {sig['amount_yi'].quantile(.1):.1f}  p90 {sig['amount_yi'].quantile(.9):.1f}")
print(f"4候选成交额 4.3~12.9亿，处于回测分布的哪个分位: "
      f"{[f'{q:.0%}' for q in [(sig['amount_yi'] < a).mean() for a in (4.3, 12.9)]]}")
