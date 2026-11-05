#!/usr/bin/env python3
"""R1 Q2/Q3：G2 周队列统计、有效样本量、右尾依赖。"""
import numpy as np
import pandas as pd
import os


def _betacf(a, b, x):
    MAXIT, EPS, FPMIN = 200, 3e-12, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < EPS:
            break
    return h


def t_pvalue(t, df):
    """双侧 p 值（t 分布）。"""
    from math import lgamma, exp, log
    a, b, x = 0.5 * df, 0.5, df / (df + t * t)
    lnbeta = lgamma(a) + lgamma(b) - lgamma(a + b)
    if x < (a + 1.0) / (a + b + 2.0):
        ib = exp(a * log(x) + b * log(1 - x) - lnbeta) * _betacf(a, b, x) / a
    else:
        ib = 1 - exp(b * log(1 - x) + a * log(x) - lnbeta) * _betacf(b, a, 1 - x) / b
    return ib  # = P(|T|>|t|)


def ttest_1samp(arr, popmean=0.0):
    arr = np.asarray(arr, dtype=float)
    n = len(arr)
    mu = arr.mean()
    sd = arr.std(ddof=1)
    t = (mu - popmean) / (sd / np.sqrt(n))
    return t, t_pvalue(abs(t), n - 1)


def t_crit(prob, df):
    """t 分布上侧分位数（双侧 p=prob 的临界值），二分法。"""
    lo, hi = 0.0, 100.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if t_pvalue(mid, df) > prob:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def spearman(x, y):
    rx = pd.Series(x).rank().values
    ry = pd.Series(y).rank().values
    n = len(x)
    rho = np.corrcoef(rx, ry)[0, 1]
    z = rho * np.sqrt((n - 3) / (1 - rho ** 2)) if abs(rho) < 1 else np.inf
    return rho, z


S = "/root/.hermes/cache/scratch"
d = pd.read_csv(os.path.join(S, "r1_weekly_universe.csv.gz"))

sig = d[d["g2_20"] == 1].copy()          # 最终冻结口径（20 bar）
print(f"G2-20 信号 N={len(sig)}, 有信号周数={sig['wk'].nunique()}")

# 市场周基线（t10）
mkt = d.groupby("wk")["mkt_t10"].agg(["mean", "median", "count"]).rename(
    columns={"mean": "mkt_mean", "median": "mkt_median", "count": "uni_n"})

for col, name in [("t10_close", "收盘买"), ("t10_exec", "次日开盘买")]:
    s = sig.dropna(subset=[col])
    w = s.groupby("wk")[col].agg(["count", "mean", "median"])
    w = w.join(mkt, how="left")
    w["abn"] = w["mean"] - w["mkt_mean"]
    print(f"\n===== {name} ({col}) =====")
    print(f"有信号周数: {len(w)}")
    x = w["mean"].values
    a = w["abn"].values
    for label, arr in [("周队列原始均值", x), ("周队列超额(abnormal)", a)]:
        t, p = ttest_1samp(arr, 0)
        print(f"  {label}: mean {arr.mean():+.4%} median {np.median(arr):+.4%} "
              f"std {arr.std(ddof=1):.4%} t={t:.3f} p={p:.4f} 正周占比 {(arr>0).mean():.1%}")
    # 滞后1自相关 → 有效N
    rho = np.corrcoef(a[:-1], a[1:])[0, 1]
    n = len(a)
    n_eff = n * (1 - rho) / (1 + rho)
    # Newey-West(滞后3) t 值
    def nw_t(arr, lags=3):
        m = len(arr)
        mu = arr.mean()
        g = np.array([np.mean((arr[k:] - mu) * (arr[:m - k] - mu)) for k in range(1, lags + 1)])
        s0 = np.mean((arr - mu) ** 2)
        lrv = s0 + 2 * sum((1 - k / (lags + 1)) * g[k - 1] for k in range(1, lags + 1))
        se = np.sqrt(lrv / m)
        return mu / se
    print(f"  周超额 lag-1 自相关 ρ={rho:.3f} → 有效N≈{n_eff:.1f}（名义 {n}）")
    print(f"  周超额 Newey-West(3) t = {nw_t(a):.3f}")

    # ===== Q3: 右尾依赖 =====
    print(f"  —— 右尾依赖 ——")
    pooled_all = s[col]
    print(f"  全样本: pooled 中位 {pooled_all.median():+.4%} 均值 {pooled_all.mean():+.4%} "
          f"胜率 {(pooled_all>0).mean():.1%}")
    total_pnl = s.assign(pnl=s[col]).groupby("wk")["pnl"].sum().sort_values()
    rank_by_mean = w.sort_values("mean", ascending=False)
    for k in [3, 5, 10]:
        drop = rank_by_mean.index[:k]
        s2 = s[~s["wk"].isin(drop)]
        w2 = w.drop(index=drop)
        t2, p2 = ttest_1samp(w2["abn"].values, 0)
        print(f"  删最佳{k}周({','.join(drop[:3])}...): pooled中位 {s2[col].median():+.4%} "
              f"等权周中位 {w2['mean'].median():+.4%} 周超额均值 {w2['abn'].mean():+.4%} "
              f"t={t2:.3f} 正周占比 {(w2['abn']>0).mean():.1%}")
    # P&L 集中度
    pos = total_pnl[total_pnl > 0]
    print(f"  正P&L周数 {len(pos)}/{len(total_pnl)}, 总P&L {total_pnl.sum():.2%}")
    for k in [1, 3, 5, 10]:
        topk = pos.sort_values(ascending=False).head(k)
        print(f"    最佳{k}周占总P&L: {topk.sum()/pos.sum():.1%}（{list(topk.index)}）")
    # 信号数与周收益的关系（危机周 beta）
    sc_rho, sc_z = spearman(w["count"], w["abn"])
    print(f"  周信号数 vs 周超额 Spearman ρ={sc_rho:.3f} (z={sc_z:.2f})")
    big = w[w["count"] >= 100]
    small = w[w["count"] < 100]
    print(f"  大信号周(≥100只, n={len(big)}): 超额均值 {big['abn'].mean():+.4%}; "
          f"小信号周(<100只, n={len(small)}): {small['abn'].mean():+.4%}")

    # 等权 vs 信号加权（伪重复视角）
    print(f"  —— 加权方式 ——")
    print(f"  信号加权pooled均值 {s[col].mean():+.4%} vs 等权周均值 {w['mean'].mean():+.4%} "
          f"vs 等权周中位 {w['mean'].median():+.4%}")

# G2 与 base 对照（G2 闸的贡献）
print("\n===== G2 闸对照（t10_close）=====")
for tag, m in [("base(无G2)", d["base"] == 1), ("G2-20", d["g2_20"] == 1),
               ("被G2剔除", (d["base"] == 1) & (d["g2_20"] == 0))]:
    ss = d.loc[m.fillna(False), "t10_close"].dropna()
    print(f"  {tag}: N={len(ss)} 中位 {ss.median():+.4%} 均值 {ss.mean():+.4%} 胜率 {(ss>0).mean():.1%}")
