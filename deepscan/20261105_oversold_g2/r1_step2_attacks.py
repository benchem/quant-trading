#!/usr/bin/env python3
"""R1 攻击评审 step2：必查三题实证。
① 因子代理：G2 信号 T+10 对 中证1000/创业板指/universe等权 回归 → beta/alpha/显著性
② 制度分段：创业板指 & 中证1000 的 MA60上下 / ret20 / vol20 分段 → 各段中位数
③ 拥挤度：错杀率代理 + 周/日信号计数 vs 后续收益 + ret20 深度分桶（实盘候选在 <-40% 桶）
附加：右尾集中度（去掉最好3/5周）、市值前视偏差、周组合真实样本量
"""
import os
import math
import numpy as np
import pandas as pd


def _betacf(a, b, x):
    MAXIT, EPS, FPMIN = 200, 3e-12, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < FPMIN: d = FPMIN
    d = 1.0 / d; h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN: d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN: c = FPMIN
        d = 1.0 / d; h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN: d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN: c = FPMIN
        d = 1.0 / d; de = d * c; h *= de
        if abs(de - 1.0) < EPS: break
    return h


def t_p(tstat, dof):
    """学生t分布双尾p值（正则化不完全beta实现）。"""
    a, b, x = dof / 2.0, 0.5, dof / (dof + tstat * tstat)
    if x <= 0: return 1.0
    if x >= 1: return 0.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    bt_log = a * math.log(x) + b * math.log(1 - x) + lbeta
    if x < (a + 1) / (a + b + 2):
        ib = math.exp(bt_log + math.log(_betacf(a, b, x))) / a
    else:
        ib = 1 - math.exp(bt_log + math.log(_betacf(b, a, 1 - x))) / b
    return min(1.0, max(0.0, ib))


def spearman(x, y):
    rx, ry = pd.Series(x).rank(), pd.Series(y).rank()
    rho = np.corrcoef(rx, ry)[0, 1]
    n = len(rx)
    t = rho * math.sqrt((n - 2) / max(1e-12, 1 - rho * rho))
    return rho, t_p(t, n - 2)


def cluster_ols(y, X, groups):
    """OLS + 按组聚类的sandwich稳健SE。X 不含常数列。返回 coef/t/p。"""
    Xc = np.column_stack([np.ones(len(X)), X])
    coef, *_ = np.linalg.lstsq(Xc, y, rcond=None)
    resid = y - Xc @ coef
    bread = np.linalg.inv(Xc.T @ Xc)
    meat = np.zeros((Xc.shape[1], Xc.shape[1]))
    for g in np.unique(groups):
        m = groups == g
        Xg = Xc[m]; eg = resid[m]
        s = Xg.T @ eg
        meat += np.outer(s, s)
    cov = bread @ meat @ bread
    se = np.sqrt(np.diag(cov))
    dof = len(np.unique(groups)) - 1
    tv = coef / se
    return coef, tv, [t_p(t, dof) for t in tv]

OUT = "/root/workspaces/quant-trading/deepscan/20261105_oversold_g2"
IDX = "/opt/quant-trading/data/index"

sig = pd.read_csv(os.path.join(OUT, "r1_signals.csv"))
g2 = sig[sig["g2"]].copy()
weekly = pd.read_csv(os.path.join(OUT, "r1_weekly.csv")).drop(columns=["count", "mean", "median"])
daily = pd.read_csv(os.path.join(OUT, "r1_daily.csv"))
g2["wk"] = pd.to_datetime(g2["wk"])
weekly["wk"] = pd.to_datetime(weekly["wk"])
daily["date"] = pd.to_datetime(daily["date"])


def load_idx(code):
    d = pd.read_parquet(os.path.join(IDX, f"{code}.parquet"))[["date", "open", "close"]]
    d["date"] = pd.to_datetime(d["date"])
    return d.sort_values("date").reset_index(drop=True)


idx1000 = load_idx("000852.SH")   # 中证1000
idxcyb = load_idx("399006.SZ")    # 创业板指

# —— 11-05 创业板指 V 型验证 ——
last = idxcyb.iloc[-1]; prev = idxcyb.iloc[-2]
print("== 11-05 创业板指 bar ==")
print(f"open {last['open']/prev['close']-1:+.2%}  close {last['close']/prev['close']-1:+.2%}")

# 周信号日的指数对齐 + 前向10日收益 + 状态变量
def idx_feats(df, tag):
    closes = df["close"].values; dates = df["date"].values
    pos = {pd.Timestamp(d): i for i, d in enumerate(df["date"])}
    rows = []
    for wk, grp in weekly.set_index("wk").iterrows():
        j = pos.get(pd.Timestamp(wk))
        if j is None or j + 10 >= len(closes):
            rows.append(dict(wk=wk)); continue
        r10 = closes[j + 10] / closes[j] - 1
        ma60 = closes[max(0, j - 59):j + 1].mean()
        r20 = closes[j] / closes[max(0, j - 20)] - 1
        vol20 = np.std(np.diff(closes[max(0, j - 20):j + 1]) / closes[max(0, j - 20):j + 1][:-1], ddof=1) * np.sqrt(250)
        rows.append(dict(wk=wk, **{
            f"{tag}_r10": r10, f"{tag}_above_ma60": closes[j] >= ma60,
            f"{tag}_r20": r20, f"{tag}_vol20": vol20}))
    return pd.DataFrame(rows).set_index("wk")

W = weekly.set_index("wk").join(idx_feats(idx1000, "i1000")).join(idx_feats(idxcyb, "cyb"))
# 周组合收益（G2 信号等权）
port = g2.groupby("wk")["t10"].agg(["count", "mean", "median"])
W = W.join(port).rename(columns={"mean": "port_mean", "median": "port_median", "count": "n_sig"})
W = W.dropna(subset=["port_mean"])

def ols(y, x, label):
    x = np.asarray(x, float); y = np.asarray(y, float)
    X = np.column_stack([np.ones(len(x)), x])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    dof = len(y) - 2
    s2 = resid @ resid / dof
    cov = s2 * np.linalg.inv(X.T @ X)
    se = np.sqrt(np.diag(cov))
    t = coef / se
    r2 = 1 - resid @ resid / ((y - y.mean()) @ (y - y.mean()))
    print(f"  {label}: beta={coef[1]:.3f}(t={t[1]:.2f})  alpha={coef[0]*100:+.3f}%/周(t={t[0]:.2f}, p={t_p(t[0], dof):.4f})  R2={r2:.3f}  N={len(y)}")
    return coef, t, r2

print("\n" + "=" * 70)
print("① 因子代理检验：周组合 T+10（净） 对 指数同期 T+10 回归（N=周）")
for col, name in [("i1000_r10", "中证1000"), ("cyb_r10", "创业板指"), ("mkt_mean", "universe等权(30~300亿)")]:
    d = W.dropna(subset=[col])
    ols(d["port_mean"], d[col], f"vs {name}")
    # 超额视角
    ex = d["port_mean"] - d[col]
    print(f"     周超额(组合-指数): 均值 {ex.mean()*100:+.3f}% 中位 {ex.median()*100:+.3f}% 为正比例 {(ex>0).mean():.1%}")

print("\n—— 个股层面 pooled 回归（按周聚类稳健SE）——")
g2x = g2.merge(W[["i1000_r10"]], left_on="wk", right_index=True, how="inner").dropna(subset=["i1000_r10"])
coef_c, t_c, p_c = cluster_ols(g2x["t10"].values, g2x[["i1000_r10"]].values, g2x["wk"].values)
print(f"  pooled N={len(g2x)}: beta={coef_c[1]:.3f}(t={t_c[1]:.2f}) alpha={coef_c[0]*100:+.3f}%(t={t_c[0]:.2f}, p={p_c[0]:.4f})")

print("\n" + "=" * 70)
print("② 制度分段：各段信号 T+10 表现（净，个股份母 N）")
def seg_report(mask_wk, label, df_sig=None):
    d = (df_sig if df_sig is not None else g2)
    m = d["wk"].isin(W.index[W.index.isin(mask_wk)])
    s = d.loc[m, "t10"]
    pw = W.loc[[x for x in W.index if x in mask_wk], "port_mean"]
    if len(s) == 0:
        print(f"  {label}: 无样本"); return
    print(f"  {label}: N={len(s)} 中位 {s.median():+.2%} 均值 {s.mean():+.2%} 胜率 {(s>0).mean():.1%} | 周组合中位 {pw.median():+.2%} 周数 {len(pw)}")

for tag, name in [("cyb", "创业板指"), ("i1000", "中证1000")]:
    print(f"\n—— 按 {name} 状态 ——")
    up = set(W.index[W[f"{tag}_above_ma60"].fillna(False)])
    dn = set(W.index[~W[f"{tag}_above_ma60"].fillna(True)])
    seg_report(up, f"{name} 在 MA60 上")
    seg_report(dn, f"{name} 在 MA60 下")
    r20c = W[f"{tag}_r20"]
    for lo, hi, lab in [(-1, -0.10, "指数20日跌>10%(恐慌)"), (-0.10, 0.0, "指数20日 -10~0%"), (0.0, 1, "指数20日 >0%")]:
        wk_s = set(W.index[(r20c > lo) & (r20c <= hi)])
        seg_report(wk_s, f"{name} {lab}")
    v = W[f"{tag}_vol20"]
    q1, q2 = v.quantile([1/3, 2/3])
    seg_report(set(W.index[v <= q1]), f"{name} 波动低1/3(vol20≤{q1:.0%})")
    seg_report(set(W.index[(v > q1) & (v <= q2)]), f"{name} 波动中1/3")
    seg_report(set(W.index[v > q2]), f"{name} 波动高1/3(vol20>{q2:.0%})")

print("\n" + "=" * 70)
print("③ 拥挤度 / 错杀率")
print("—— 持有期路径（N=G2 信号）——")
print(f"  10日内创20日新低比例: {g2['newlow20'].mean():.1%}")
print(f"  T+10 收盘仍低于信号日收盘比例: {g2['below_sig'].mean():.1%}")
print(f"  持有期内最大浮亏(min_next10): 中位 {g2['min_next10'].median():+.2%} 均值 {g2['min_next10'].mean():+.2%} P10 {g2['min_next10'].quantile(0.1):+.2%}")
print(f"  触发-10%灾难止损比例(任意时点): {(g2['min_next10'] <= -0.10).mean():.1%}")
print(f"  触发-20%比例: {(g2['min_next10'] <= -0.20).mean():.1%}")

print("\n—— 周信号数 vs 当周组合收益（拥挤度）——")
valid = W.dropna(subset=["n_sig"])
rho, pv = spearman(valid["n_sig"], valid["port_mean"])
print(f"  Spearman(周信号数, 周组合T+10) = {rho:+.3f} (p={pv:.3f})  周数 {len(valid)}")
med_n = valid["n_sig"].median()
for lab, m in [("低半区(计数<中位)", valid["n_sig"] <= med_n), ("高半区", valid["n_sig"] > med_n)]:
    d = valid[m]
    print(f"  {lab}: 周数 {len(d)} 信号数均值 {d['n_sig'].mean():.0f} 组合均值 {d['port_mean'].mean():+.2%} 中位 {d['port_median'].median():+.2%}")

print("\n—— 日信号计数 vs 日均 T+10（近300交易日，含重叠窗口）——")
dv = daily.dropna(subset=["t10_mean"])
rho2, pv2 = spearman(dv["n_sig"], dv["t10_mean"])
print(f"  Spearman(日信号数, 日均T+10) = {rho2:+.3f} (p={pv2:.3f})  天数 {len(dv)}")
for lo, hi in [(0, 50), (50, 200), (200, 400), (400, 10000)]:
    d = dv[(dv["n_sig"] >= lo) & (dv["n_sig"] < hi)]
    if len(d):
        print(f"  日信号数 {lo}~{hi}: 天数 {len(d)} 日均T+10 均值 {d['t10_mean'].mean():+.2%} 中位 {d['t10_mean'].median():+.2%}")
print(f"  今日 11-05 信号数 362 | 历史同档(200~400)均值如上")

print("\n—— ret20 深度分桶（实盘候选 -42%~-46% 落在最深桶）——")
g2["ret20_bucket"] = pd.cut(g2["ret20"], [-1, -0.40, -0.30, -0.25, -0.20], right=True,
                            labels=["≤-40%", "-40~-30%", "-30~-25%", "-25~-20%"])
for b, d in g2.groupby("ret20_bucket", observed=True):
    print(f"  {b}: N={len(d)} 中位 {d['t10'].median():+.2%} 均值 {d['t10'].mean():+.2%} 胜率 {(d['t10']>0).mean():.1%} 错杀率(创新低) {d['newlow20'].mean():.1%} -10%触发率 {(d['min_next10']<=-0.10).mean():.1%}")

print("\n" + "=" * 70)
print("附加：右尾集中 / 周真实样本 / 市值前视")
pw = W["port_mean"].sort_values()
print(f"  周组合 T+10: 均值 {pw.mean():+.2%} 中位 {pw.median():+.2%} 为正周比例 {(pw>0).mean():.1%} (N=52)")
for k in [1, 3, 5]:
    trimmed = pw.iloc[:-k] if pw.mean() > 0 else pw
    rest = pw.drop(pw.nlargest(k).index) if k else pw
    print(f"  去掉最好 {k} 周: 均值 {rest.mean():+.2%} 中位 {rest.median():+.2%}")
top5 = W["port_mean"].nlargest(5)
print("  最好5周: " + ", ".join(f"{d.date()} {v:+.1%}" for d, v in top5.items()))
share = top5.sum() / pw.sum() if pw.sum() != 0 else float('nan')
print(f"  最好5周贡献占全周期总收益比例: {share:.0%}（总周收益和 {pw.sum():+.1%}）")

mv_oos = (g2["mv_signal"] < 30) | (g2["mv_signal"] > 300)
print(f"\n  市值前视：信号时点隐含流通市值落在 30~300亿外的比例: {mv_oos.mean():.1%} (N={len(g2)})")
inside = g2[~mv_oos]
print(f"  时点市值过滤后重算: N={len(inside)} 中位 {inside['t10'].median():+.2%} 均值 {inside['t10'].mean():+.2%} 胜率 {(inside['t10']>0).mean():.1%}")
below30 = (g2["mv_signal"] < 30).mean(); above300 = (g2["mv_signal"] > 300).mean()
print(f"    其中当时<30亿(后被筛入=幸存成长): {below30:.1%}  当时>300亿: {above300:.1%}")

# 隔夜 gap 分布（exec 口径优势来源）
g2["gap"] = g2["o_next"] / g2["c_sig"] - 1
print(f"\n  隔夜gap(次开/信号收-1): 中位 {g2['gap'].median():+.2%} 均值 {g2['gap'].mean():+.2%} P10 {g2['gap'].quantile(0.1):+.2%}")
W.to_csv(os.path.join(OUT, "r1_weekly_feats.csv"))
