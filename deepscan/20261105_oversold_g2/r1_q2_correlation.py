#!/usr/bin/env python3
"""R1 评审 · Q2 v2：共振相关性（ICC方差分解+MC组合方差比）+ V反转次日条件检验。"""
import numpy as np
import pandas as pd

OUT = "/root/workspaces/quant-trading/deepscan/20261105_oversold_g2"
d = pd.read_parquet(f"{OUT}/r1_signals.parquet")
rng = np.random.default_rng(42)
IDX = "/opt/quant-trading/data/index/399006.SZ.parquet"

# —— 指数向后（信号日前）与向前统计 ——
idx = pd.read_parquet(IDX)[["date", "close"]].copy()
idx["date"] = pd.to_datetime(idx["date"])
idx = idx.sort_values("date").reset_index(drop=True)
ic = idx["close"].values
imap = {d_: i for i, d_ in enumerate(idx["date"])}

wk_backward = {}
for wk in d.wk.unique():
    j = imap.get(pd.Timestamp(wk))
    if j is None or j < 6:
        wk_backward[wk] = dict(idx_own=np.nan, idx_prev1=np.nan, idx_prev5=np.nan)
        continue
    wk_backward[wk] = dict(
        idx_own=ic[j] / ic[j - 1] - 1,          # 信号日当天指数涨跌
        idx_prev1=ic[j - 1] / ic[j - 2] - 1,    # 信号日前一日（V日?）
        idx_prev5=ic[j] / ic[j - 6] - 1,        # 信号日前6日累计
    )
for k_ in ["idx_own", "idx_prev1", "idx_prev5"]:
    d[k_] = d.wk.map(lambda w: wk_backward[w][k_])

# 今日(2026-11-05)对照
j_now = len(ic) - 1
print("今日参照: 11-05 指数当日 {:+.2%}, 前一日 {:+.2%}, 前6日 {:+.2%}, 收盘 {:.2f} (kill线2407, 距离 {:+.2%})".format(
    ic[j_now] / ic[j_now - 1] - 1, ic[j_now - 1] / ic[j_now - 2] - 1, ic[j_now] / ic[j_now - 6] - 1,
    ic[j_now], 2407 / ic[j_now] - 1))

print("=" * 70)
print("Q2-A TOP5 共振周（含向后语境）")
cnt = d.groupby("wk").size().sort_values(ascending=False)
top5 = cnt.head(5)
for wk in top5.index:
    g = d[d.wk == wk]
    r = g.t10_exec.dropna().values
    sgn = np.sign(r)
    ii, jj = np.triu_indices(len(r), 1)
    same = (sgn[ii] == sgn[jj]).mean()
    print(f"{wk}: N={len(g):3d} 中位 {np.median(r):+6.2%} 胜率 {(r>0).mean():5.1%} 同号对 {same:5.1%} | "
          f"信号日指数 {g.idx_own.iloc[0]:+6.2%} 前一日 {g.idx_prev1.iloc[0]:+6.2%} 前一日5日 {g.idx_prev5.iloc[0]:+6.2%} "
          f"| 后10日指数 {g.idx_fwd10.iloc[0]:+6.2%}")
print(f"TOP5 周样本占比: {top5.sum()}/{len(d)} = {top5.sum()/len(d):.1%}（5/46 周）")
print(f"今日信号数 362 vs 回测周最大 {cnt.max()}（今日超历史极值）")

print("=" * 70)
print("Q2-B 周内 ICC（单因素方差分解: 组内相关 = 周效应占比）")
x = d.dropna(subset=["t10_exec"])
grand = x.t10_exec.mean()
grp = x.groupby("wk").t10_exec
k_bar = grp.size().mean()
ssb = sum(grp.size()[w] * (m - grand) ** 2 for w, m in grp.mean().items())
ssw = sum(((g.t10_exec - g.t10_exec.mean()) ** 2).sum() for _, g in x.groupby("wk"))
dfb, dfw = x.wk.nunique() - 1, len(x) - x.wk.nunique()
msb, msw = ssb / dfb, ssw / dfw
icc = (msb - msw) / (msb + (k_bar - 1) * msw)
sigma2 = x.t10_exec.var()
print(f"周数 {x.wk.nunique()}, 平均每周 {k_bar:.0f} 样本, 总σ² {sigma2:.5f} (σ {np.sqrt(sigma2):.2%})")
print(f"MSB {msb:.5f} / MSW {msw:.5f} → ICC(1) = {icc:.3f}")
print(f"理论: 同日4只等权组合方差 = σ²/4×(1+3·ICC) = {sigma2/4*(1+3*icc):.5f}, 独立时 = {sigma2/4:.5f}, 方差倍数 {1+3*icc:.2f}x")

print("=" * 70)
print("Q2-C 周效应自相关（相邻周持有期重叠 → 分4周仍有残余相关）")
wm = x.groupby("wk").t10_exec.mean().sort_index()
print(f"相邻周周均收益相关: lag1 {wm.autocorr(1):+.3f}  lag2 {wm.autocorr(2):+.3f}")

print("=" * 70)
print("Q2-D 蒙特卡洛 20000 次：同日4只 vs 分4周（避开相邻周）vs 连续4周各买1只")
bywk = {w: g.t10_exec.dropna().values for w, g in x.groupby("wk")}
wks = sorted(bywk)
def mc_same(pool_wks, n=20000):
    pool = [w for w in pool_wks if len(bywk[w]) >= 4]
    out = []
    for _ in range(n):
        w = pool[rng.integers(len(pool))]
        xx = bywk[w]
        out.append(xx[rng.choice(len(xx), size=4, replace=False)].mean())
    return np.array(out)
def mc_spread(consecutive, n=20000):
    out = []
    for _ in range(n):
        if consecutive:
            start = rng.integers(0, len(wks) - 3)
            chosen = list(range(start, start + 4))
        else:
            chosen = sorted(rng.choice(len(wks), size=4, replace=False))
            while any(chosen[i + 1] - chosen[i] < 2 for i in range(3)):
                chosen = sorted(rng.choice(len(wks), size=4, replace=False))
        out.append(np.mean([bywk[wks[c_]][rng.integers(len(bywk[wks[c_]]))] for c_ in chosen]))
    return np.array(out)

def rep(lab, arr):
    print(f"  {lab}: σ {arr.std():.2%} P10 {np.quantile(arr,.1):+.2%} P5 {np.quantile(arr,.05):+.2%} "
          f"最差 {arr.min():+.2%} 亏>5%概率 {(arr<-0.05).mean():.1%} 亏>10%概率 {(arr<-0.10).mean():.1%}")

a = mc_same(list(top5.index)); b = mc_same(wks); c = mc_spread(False); e = mc_spread(True)
rep("同日4只@TOP5共振周", a)
rep("同日4只@任意周   ", b)
rep("分4周(隔周,不重叠)", c)
rep("分4周(连续4周)   ", e)
print(f"\n  方差比: TOP5同日/隔周分批 = {a.var()/c.var():.2f}x | 任意周同日/隔周分批 = {b.var()/c.var():.2f}x | 连续分批/隔周分批 = {e.var()/c.var():.2f}x")
print(f"  [问题口径] 同日买入组合方差 vs 分4周买入: {b.var()/c.var():.2f}x —— 方差不是降了 {1-b.var()/c.var():.0%}，而是升了 {b.var()/c.var()-1:.0%}")

print("=" * 70)
print("Q2-E V反转次日条件（今日=前一日指数+6.4%后的信号日）—— bucket by idx_prev1")
bins = [-1, -0.02, 0.02, 0.04, 1]
labels = ["前一日<-2%", "-2%~+2%", "+2%~+4%", ">=+4%(V次日)"]
x["vb"] = pd.cut(x.idx_prev1, bins, labels=labels)
g = x.groupby("vb", observed=True).agg(
    N=("t10_exec", "size"), t10中位=("t10_exec", "median"), t10均值=("t10_exec", "mean"),
    胜率=("t10_exec", lambda v: (v > 0).mean()), P5=("t10_exec", lambda v: v.quantile(.05)))
print(g.to_string(float_format=lambda v: f"{v:+.3f}"))
vday = x[x.idx_prev1 >= 0.04]
if len(vday):
    gap = vday.entry_open / (vday.entry_open / (1 + vday.t10_exec + 0.0068)) - 1  # noqa 占位
    # 直接算隔夜 gap: entry_open/c[信号日收盘]-1 —— 需要信号日收盘，改用 t10_close/t10_exec 反推近似不可靠，跳过
    print(f"  V次日组信号日分布: {sorted(vday.wk.unique())}")
    print(f"  V次日组 exec口径中位 {vday.t10_exec.median():+.2%} vs close口径中位 {vday.t10_close.median():+.2%} "
          f"(全样本: {x.t10_exec.median():+.2%} vs {x.t10_close.median():+.2%})")

print("=" * 70)
print("Q2-F edge 集中度：删掉最好几周后剩余 edge")
wm_all = x.t10_exec
order = x.groupby("wk").t10_exec.mean().sort_values(ascending=False)
for k_ in [0, 1, 3, 5]:
    drop = set(order.head(k_).index)
    sub = x[~x.wk.isin(drop)]
    print(f"  删最好{k_}周: N={len(sub)} 中位 {sub.t10_exec.median():+.2%} 均值 {sub.t10_exec.mean():+.2%} 胜率 {(sub.t10_exec>0).mean():.1%}")
# 周级独立样本口径
print(f"  周级(46周)周均收益: 均值 {wm.mean():+.2%} t统计 {wm.mean()/wm.std()*np.sqrt(len(wm)):.2f} 正收益周 {(wm>0).mean():.0%} ({(wm>0).sum()}/{len(wm)})")
