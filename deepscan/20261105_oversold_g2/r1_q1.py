#!/usr/bin/env python3
"""R1 Q1 多重检验：家族矩阵 + Bonferroni + max-stat 块自助 + 同周同量随机安慰剂。"""
import os

import numpy as np
import pandas as pd

exec(open("/root/.hermes/cache/scratch/r1_q2q3.py").read().split('S = "/root/.hermes/cache/scratch"')[0])  # 复用 t/p 函数

S = "/root/.hermes/cache/scratch"
W = "/root/workspaces/quant-trading"
d = pd.read_csv(os.path.join(S, "r1_weekly_universe.csv.gz"))
mktw = pd.read_csv(os.path.join(S, "r1_market_weekly.csv"))

# 周索引（对齐 chase/prebreak 的 week int）
wk_dates = sorted(d["wk"].unique())
wk2idx = {w: i for i, w in enumerate(wk_dates)}
mktw = mktw[mktw["wk"].isin(wk2idx)].copy()
mktw["idx"] = mktw["wk"].map(wk2idx)
mkt_by_idx = mktw.set_index("idx")

rng = np.random.default_rng(20261105)

# ---------- 家族矩阵：每 cell 周超额均值序列 ----------
def weekly_abn(detail, retcol, mkcol):
    """detail: DataFrame with wk(+idx) & ret; 返回 dict idx->abn(mean)。"""
    g = detail.groupby("idx")[retcol].mean()
    m = mkt_by_idx[mkcol]
    return (g - m).dropna()

cells = {}

# 1) 我的复现：base/G2/strict/G1/G3 @ t10
my = d.copy()
my["idx"] = my["wk"].map(wk2idx)
for tag, mask in [("base", my["base"] == 1), ("G2close", my["g2_20"] == 1),
                  ("G2exec", my["g2_20"] == 1), ("G2_21", my["g2_21"] == 1),
                  ("strict", (my["g1"] == 1) & (my["g2_20"] == 1) & (my["g3"] == 1)),
                  ("G1only", (my["base"] == 1) & (my["g1"] == 1)),
                  ("G3only", (my["base"] == 1) & (my["g3"] == 1))]:
    sub = my[mask.fillna(False)]
    col = "t10_exec" if tag == "G2exec" else "t10_close"
    cells[f"repl_{tag}_t10"] = weekly_abn(sub.dropna(subset=[col]), col, "m10")

# 2) oversold_strict_detail.csv: strict @ t1,t2,t3,t5
st = pd.read_csv(os.path.join(W, "oversold_strict_detail.csv"))
st["idx"] = st["wk"].map(wk2idx)
for hh in [1, 2, 3, 5]:
    cells[f"csv_strict_t{hh}"] = weekly_abn(st.dropna(subset=[f"t{hh}"]), f"t{hh}", f"m{hh}")

# 3) oversold_probe_detail.csv: base @ t5
bp = pd.read_csv(os.path.join(W, "oversold_probe_detail.csv"))
bp["idx"] = bp["wk"].map(wk2idx)
cells["csv_base_t5"] = weekly_abn(bp.dropna(subset=["t5"]), "t5", "m5")

# 4) pullback @ t1,t2,t3,t5,t10
pb = pd.read_csv(os.path.join(W, "pullback_probe_detail.csv"))
pb["idx"] = pb["wk"].map(wk2idx)
for hh in [1, 2, 3, 5, 10]:
    cells[f"csv_pullback_t{hh}"] = weekly_abn(pb.dropna(subset=[f"t{hh}"]), f"t{hh}", f"m{hh}")

# 5) chase / prebreak @ t1,t2,t3,t5（week int 已对齐）
for name, fn in [("chase", "chase_backtest_detail.csv"), ("prebreak", "prebreak_backtest_detail.csv")]:
    x = pd.read_csv(os.path.join(W, fn))
    x["idx"] = x["week"]
    x = x[x["week"].isin(mkt_by_idx.index)]
    for hh in [1, 2, 3, 5]:
        cells[f"csv_{name}_t{hh}"] = weekly_abn(x.dropna(subset=[f"t{hh}"]), f"t{hh}", f"m{hh}")

print(f"家族矩阵 cells（有数据）: {len(cells)}")
for k, v in sorted(cells.items(), key=lambda kv: -kv[1].mean()):
    t, p = ttest_1samp(v.values, 0)
    print(f"  {k:22s} 周数{len(v):3d} 周超额均值 {v.mean():+.4%} t={t:+.3f} p={p:.3f}")

# ---------- G2 的观察统计量 ----------
g2 = cells["repl_G2close_t10"]; g2e = cells["repl_G2exec_t10"]
t_obs, p_obs = ttest_1samp(g2.values, 0)
t_obs_e, p_obs_e = ttest_1samp(g2e.values, 0)
print(f"\nG2close: 周超额均值 {g2.mean():+.4%} t={t_obs:.3f} p_raw={p_obs:.4f}")
print(f"G2exec : 周超额均值 {g2e.mean():+.4%} t={t_obs_e:.3f} p_raw={p_obs_e:.4f}")

# ---------- Bonferroni ----------
n_eff = 34.0  # Q2 已算
for m in [10, 28, 39]:
    a_single = 0.05 / m
    tc = t_crit(a_single, df=n_eff)
    se = g2.std(ddof=1) / np.sqrt(len(g2))
    se_eff = g2.std(ddof=1) / np.sqrt(n_eff)
    print(f"m={m}: 单检验α={a_single:.5f} t_crit(df=34)={tc:.3f} "
          f"最小存活周alpha={tc*se_eff:+.3%}（周超额均值口径）| G2 Bonferroni p = {min(1, p_obs*m):.3f}")

# ---------- max-stat 块自助（White Reality Check 近似）----------
allidx = np.array(sorted(mkt_by_idx.index))
ser = {k: v for k, v in cells.items()}
BL, NB = 5, 4000
obs_max = max(abs(v.mean()) for v in ser.values())
g2_stat = g2.mean()
rng = np.random.default_rng(7)
beats = 0
null_g2 = []
for b in range(NB):
    # 移动块自助周索引
    blocks = []
    while sum(len(x) for x in blocks) < len(allidx):
        s = rng.integers(0, len(allidx) - BL + 1)
        blocks.append(allidx[s:s + BL])
    sample = np.concatenate(blocks)[:len(allidx)]
    mx = 0.0
    for k, v in ser.items():
        va = v.reindex([i for i in sample if i in v.index])
        if len(va) >= 10:
            centred = va - v.mean()   # H0: 真实 alpha=0
            mx = max(mx, abs(centred.mean()))
    if mx >= obs_max:
        beats += 1
    # H0 下 G2 自身均值分布（中心化）
    va = g2.reindex([i for i in sample if i in g2.index]) - g2.mean()
    null_g2.append(va.mean())
print(f"\nmax-stat 块自助({NB}次): 观察家族max|周超额均值|={obs_max:.4%}, "
      f"H0下 P(max>=观察)={beats/NB:.3f}")
null_g2 = np.array(null_g2)
lo, hi = np.percentile(null_g2, [2.5, 97.5])
print(f"G2 周超额均值 H0 95%区间 [{lo:+.3%}, {hi:+.3%}]（观察值 {g2_stat:+.3%}）")

# ---------- 同周同量随机安慰剂（直接攻击 pooled 中位 +0.24%）----------
print("\n===== 安慰剂检验：同周同数量随机选股（pooled 口径）=====")
sig = d[d["g2_20"] == 1].dropna(subset=["t10_close"]).copy()
sig["idx"] = sig["wk"].map(wk2idx)
uni = d.dropna(subset=["mkt_t10"]).copy()
uni["idx"] = uni["wk"].map(wk2idx)
byweek = {i: g["mkt_t10"].values for i, g in uni.groupby("idx")}
cnt = sig.groupby("idx")["t10_close"].count()
obs_med = sig["t10_close"].median()
obs_mean = sig["t10_close"].mean()
pl_meds, pl_means = [], []
for b in range(3000):
    pool = []
    for i, n in cnt.items():
        pool.append(rng.choice(byweek[i], size=n, replace=False))
    pool = np.concatenate(pool)
    pl_meds.append(np.median(pool))
    pl_means.append(pool.mean())
pl_meds, pl_means = np.array(pl_meds), np.array(pl_means)
p_med = (pl_meds >= obs_med).mean()
p_mean = (pl_means >= obs_mean).mean()
print(f"观察: pooled 中位 {obs_med:+.4%} 均值 {obs_mean:+.4%}")
print(f"安慰剂(同周同量随机): 中位 mean {pl_meds.mean():+.4%} sd {pl_meds.std():.4%} "
      f"P5~P95 [{np.percentile(pl_meds,5):+.3%},{np.percentile(pl_meds,95):+.3%}]")
print(f"  → pooled中位 的安慰剂 p 值 = {p_med:.3f}；pooled均值 p = {p_mean:.3f}")
