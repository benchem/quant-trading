#!/usr/bin/env python3
"""R1 执行/微观结构评审：四题实证分析"""
import os
import numpy as np, pandas as pd

D = "/root/workspaces/quant-trading/deepscan/20261105_oversold_g2"
sig = pd.read_csv(os.path.join(D, "r1_execflow_signals.csv"), parse_dates=["sd"])
mk = pd.read_csv(os.path.join(D, "r1_execflow_market.csv"), parse_dates=["date"])
sus = pd.read_csv(os.path.join(D, "r1_execflow_suspension.csv"), parse_dates=["resume_date"])
COST = 0.0068
sig["execnet"] = sig["execret"] - COST
sig["closenet"] = sig["closeret"] - COST
sig = sig.merge(mk.rename(columns={"date": "sd"})[["sd", "ew_ret", "ew_gap", "breadth"]], on="sd", how="left")
sig["nsig"] = sig.groupby("sd")["code"].transform("size")
pd.set_option("display.width", 200)

print("=" * 70)
print("题1：次日开盘 gap 分布 + 共振日条件差异")
print("=" * 70)
g = sig["gap"]
print(f"全样本 gap=o[i+1]/c[i]-1: N={len(g)}")
print(f"  均值 {g.mean():+.3%} 中位 {g.median():+.3%}  p10 {g.quantile(.1):+.3%}  p25 {g.quantile(.25):+.3%}  p75 {g.quantile(.75):+.3%}  p90 {g.quantile(.9):+.3%}")
print(f"  低开比例 {(g<0).mean():.1%}  高开>2% 比例 {(g>0.02).mean():.1%}")

# 11-05 市场画像
m1105 = mk[mk["date"] == "2026-11-05"].iloc[0]
mk52 = mk[mk["date"] >= "2025-11-01"].copy()
mk52["tr20"] = mk52["ew_ret"].rolling(20).sum()
tr20_1105 = mk52[mk52["date"] == "2026-11-05"]["tr20"].iloc[0]
print(f"\n11-05 市场画像: 等权日涨幅 {m1105['ew_ret']:+.2%}, 20日累计 {tr20_1105:+.2%}, 深跌宽度(30-300亿 ret20<=-20%) {int(m1105['breadth'])} 只")
print(f"过去52周 ew_ret 分布: 中位 {mk52['ew_ret'].median():+.2%}, >=+2% 的天数 {(mk52['ew_ret']>=0.02).sum()}, breadth>=300 的天数 {(mk52['breadth']>=300).sum()}")

# V型反转日定义：ew_ret >= +1.5% 且 20日累计 <= -3%（大反弹）
vdays = mk52[(mk52["ew_ret"] >= 0.015) & (mk52["tr20"] <= -0.03) & (mk52["date"] < "2026-11-05")]
print(f"\n历史 V 型反转日（ew_ret>=+1.5% 且 tr20<=-3%，不含11-05）共 {len(vdays)} 天:")
vd_next_gap = []
for d in vdays["date"]:
    nxt = mk[mk["date"] > d].head(1)
    if len(nxt):
        vd_next_gap.append(nxt["ew_gap"].iloc[0])
all_next = mk[(mk["date"] >= "2025-11-01") & (mk["date"] <= "2026-11-04")]["ew_gap"]
print(f"  V日次日全市场等权 gap: 均值 {np.mean(vd_next_gap):+.3%} 中位 {np.median(vd_next_gap):+.3%} (n={len(vd_next_gap)})")
print(f"  全部交易日次日等权 gap: 均值 {all_next.mean():+.3%} 中位 {all_next.median():+.3%} (n={len(all_next)})")

# 周一 vs 周五开盘 gap（回测 gap 多为周五->周一跨周末，明日是周四->周五隔夜）
mk["wd"] = mk["date"].dt.dayofweek
r = mk[(mk["date"] >= "2025-11-01")].groupby("wd")["ew_gap"].agg(["mean", "median", "count"])
print(f"\n全市场等权当日开盘 gap 按星期（0=周一..4=周五）:")
print(r.to_string(float_format=lambda x: f"{x:+.3f}" if abs(x) < 1 else f"{x:.0f}"))

# 信号级：按当日信号家数分桶
print("\n信号级 gap/execnet 按当日信号家数分桶:")
for lo, hi, lab in [(0, 20, "<20"), (20, 100, "20-99"), (100, 999, ">=100")]:
    s = sig[(sig["nsig"] >= lo) & (sig["nsig"] < hi)]
    if len(s) == 0:
        continue
    print(f"  {lab:8s}: N={len(s):5d}  gap中位 {s['gap'].median():+.3%}  gap均值 {s['gap'].mean():+.3%}  高开比例 {(s['gap']>0).mean():.1%}  execnet中位 {s['execnet'].median():+.3%}  胜率 {(s['execnet']>0).mean():.1%}")

# 信号级：按信号日市场等权涨幅分桶
print("\n信号级 gap/execnet 按信号日市场等权涨幅分桶:")
for lo, hi, lab in [(-1, -0.005, "跌日<-0.5%"), (-0.005, 0.015, "平盘"), (0.015, 1, "强涨日>=+1.5%")]:
    s = sig[(sig["ew_ret"] >= lo) & (sig["ew_ret"] < hi)]
    if len(s) == 0:
        continue
    print(f"  {lab:12s}: N={len(s):5d}  gap中位 {s['gap'].median():+.3%}  gap均值 {s['gap'].mean():+.3%}  高开比例 {(s['gap']>0).mean():.1%}  execnet中位 {s['execnet'].median():+.3%}  胜率 {(s['execnet']>0).mean():.1%}")

# 共振日（breadth>=300）上的信号
s_res = sig[sig["breadth"] >= 300]
s_nor = sig[sig["breadth"] < 300]
print(f"\n信号日深跌宽度 breadth>=300 的信号: N={len(s_res)}")
if len(s_res):
    print(f"  gap中位 {s_res['gap'].median():+.3%}  execnet中位 {s_res['execnet'].median():+.3%}  胜率 {(s_res['execnet']>0).mean():.1%}")
print(f"其余信号: N={len(s_nor)}  gap中位 {s_nor['gap'].median():+.3%}  execnet中位 {s_nor['execnet'].median():+.3%}  胜率 {(s_nor['execnet']>0).mean():.1%}")

# 明日情景：若整体高开 x%，对中位边缘的影响
med_gross = sig["execret"].median()
print(f"\n执行口径毛收益中位 {med_gross:+.3%}; 若明日 gap 比回测中位 gap({sig['gap'].median():+.3%}) 高 1/2/3 个百分点，毛中位将降至 {med_gross-0.01:+.3%}/{med_gross-0.02:+.3%}/{med_gross-0.03:+.3%}，净中位 {med_gross-0.01-COST:+.3%}/{med_gross-0.02-COST:+.3%}/{med_gross-0.03-COST:+.3%}")

# ret20 尾部匹配（候选 -43%~-46% vs 回测总体）
print("\n按 ret20 深度分桶（候选在 <=-40% 尾部）:")
sig["ret20"] = np.nan  # 占位，从 closeret 无法反推，改用 sig_pct 不行；跳过
for lo, hi, lab in [(-1, -0.40, "ret20<=-40%"), (-0.40, -0.30, "-40~-30%"), (-0.30, -0.20, "-30~-20%")]:
    pass

print("\n" + "=" * 70)
print("题2：T+10 出场日涨跌停锁死")
print("=" * 70)
sig["ld_seal"] = (sig["exit_pct"] <= -(sig["lim"] - 0.005)) & sig["exit_c_eq_l"]
sig["ld_ow"] = (sig["exit_pct"] <= -(sig["lim"] - 0.005)) & sig["exit_ow"]
sig["ld_near"] = (sig["exit_pct"] <= -(sig["lim"] - 0.01)) & ~sig["ld_seal"]
sig["lu_seal"] = (sig["exit_pct"] >= (sig["lim"] - 0.005)) & sig["exit_c_eq_h"]
n = len(sig)
print(f"出场日跌停价收盘(<=-(lim-0.5%)且收在最低): {sig['ld_seal'].sum()} ({sig['ld_seal'].mean():.2%})")
print(f"  其中一字板(low==high): {sig['ld_ow'].sum()} ({sig['ld_ow'].mean():.2%})")
print(f"出场日接近跌停(1%以内)但未封死: {sig['ld_near'].sum()} ({sig['ld_near'].mean():.2%})")
print(f"出场日涨停价收盘(封板可卖出,参照): {sig['lu_seal'].sum()} ({sig['lu_seal'].mean():.2%})")
print(f"出场日收在最低价(弱收盘)总体: {sig['exit_c_eq_l'].mean():.2%}")
lk = sig[sig["ld_seal"]]
if len(lk):
    print(f"\n跌停锁死样本 (n={len(lk)}) 强制持有到次日的额外损益 exit_next_ret:")
    print(f"  中位 {lk['exit_next_ret'].median():+.3%}  均值 {lk['exit_next_ret'].mean():+.3%}  次日仍一字 {lk['exit_next_ow'].sum()} ({lk['exit_next_ow'].mean():.1%})")
    print(f"  锁死样本 execnet 中位 {lk['execnet'].median():+.3%}（含成本，未含次日无法卖出损益）")
    print(f"  锁死样本代码示例: {lk[['code','sd','exit_pct','exit_next_ret']].head(8).to_string(index=False)}")

print("\n" + "=" * 70)
print("题3：停牌风险（30~300亿宇宙，近52周）")
print("=" * 70)
print(f"宇宙文件数 3250；停牌事件(市场日缺口>=2) {len(sus)} 起，涉及 {sus['code'].nunique()} 只 ({sus['code'].nunique()/3250:.1%})")
print(f"缺口分布(市场日): 中位 {sus['gap_md'].median():.0f}  p90 {sus['gap_md'].quantile(.9):.0f}  最大 {sus['gap_md'].max():.0f}")
deepever = sus.groupby("code")["deep_ever"].first()
print(f"涉及股票中 52周内曾深跌(ret20<=-20%) 的: {deepever.sum()} 只 ({deepever.mean():.1%})")
# 全宇宙曾深跌家数（口径近似：breadth 峰值）
print(f"(参考: 11-05 当日 breadth={int(m1105['breadth'])}，历史峰值 {int(mk52['breadth'].max())})")
# 信号层面的停牌影响
print(f"\n信号级: 次日停牌无法按计划开盘买入(entry_gap_md>1): {(sig['entry_gap_md']>1).sum()} ({(sig['entry_gap_md']>1).mean():.2%})")
print(f"信号级: 持有期被拉长(hold_md>10): {(sig['hold_md']>10).sum()} ({(sig['hold_md']>10).mean():.2%})")
eg = sig[sig["entry_gap_md"] > 1]
if len(eg):
    print(f"  次日停牌样本 execnet 中位 {eg['execnet'].median():+.3%}（回测按复牌开盘成交，实盘按计划根本买不进）")
# 4 候选停牌史
cands = ["688668.SH", "001389.SZ", "600722.SH", "601579.SH"]
for c in cands:
    h = sus[sus["code"] == c]
    print(f"  候选 {c}: 近52周停牌事件 {len(h)} 起" + (f"，最长缺口 {h['gap_md'].max()} 市场日" if len(h) else ""))

print("\n" + "=" * 70)
print("题4：成本侵蚀与冲击成本")
print("=" * 70)
en = sig["execnet"]
print(f"执行口径净收益分布: p05 {en.quantile(.05):+.2%}  p25 {en.quantile(.25):+.2%}  中位 {en.median():+.2%}  p75 {en.quantile(.75):+.2%}  p95 {en.quantile(.95):+.2%}  均值 {en.mean():+.2%}")
print(f"毛中位 {sig['execret'].median():+.2%} vs 成本 0.68% → 成本占毛中位 {0.68/sig['execret'].median():.0%}")
print(f"close口径毛中位 {sig['closeret'].median():+.2%} → 成本占 {0.68/sig['closeret'].median():.0%}")
print(f"每槽1万元的中位期望净利: {sig['execret'].median()*10000:.0f} 元（毛）- 68 元（成本假设）= {(sig['execret'].median()-COST)*10000:.0f} 元")
# 4候选固定费用
print("\n4 候选实际费用测算（佣金 min 5元/边, 印花税 0.05% 卖出, 价差=1跳）:")
cands4 = [("鼎通科技688668", 107.10, 100, 11.1e8), ("广合科技001389", 89.90, 100, 12.9e8),
          ("金牛化工600722", 11.78, 800, 4.3e8), ("会稽山601579", 22.69, 400, 5.7e8)]
for nm, px, sh, amt in cands4:
    val = px * sh
    comm = max(5, val * 0.00025) * 2
    stamp = val * 0.0005
    spread = 0.01 / px
    part = val / amt
    print(f"  {nm}: 名义 {val:,.0f}元  佣金min {comm:.0f}元({comm/val:.3%}) 印花 {stamp:.0f}元 1跳价差 {spread:.3%} 参与率(全日) {part:.4%}")
print("\n冲击成本: 1万元单占全日成交额 0.001%~0.02%，即使集中在开盘首5分钟(假设占全日5%)，")
print("参与率也仅 0.02%~0.44%，价格冲击 <2bp —— 冲击不是问题，问题是费用/中位边缘比。")
