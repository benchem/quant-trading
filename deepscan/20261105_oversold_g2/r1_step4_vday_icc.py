#!/usr/bin/env python3
"""R1 step4：V日队列执行检验 / 本回撤片段已发信号浮亏 / ICC / 中证1000脏bar清理后重回归"""
import glob
import math
import os
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, "/root/workspaces/quant-trading/deepscan/20261105_oversold_g2")
from r1_step2_attacks import t_p, load_idx, ols  # noqa: E402

DATA = "/opt/quant-trading/data"
DAILY = os.path.join(DATA, "daily", "hfq")
OUT = "/root/workspaces/quant-trading/deepscan/20261105_oversold_g2"
COST = 0.0068

# —— 0) 中证1000 脏 bar 定位 ——
i1000 = load_idx("000852.SH")
c = i1000["close"].values
chg = np.diff(c) / c[:-1]
bad = np.where((np.abs(chg) > 0.50) | (c[1:] < 100))[0]
print("== 000852.SH 中证1000 脏bar（|日涨跌|>50% 或 close<100）==")
print(f"  数量 {len(bad)}")
for j in bad[-12:]:
    print(f"  {i1000['date'].iloc[j+1].date()}: close {c[j]:.0f}→{c[j+1]:.0f} ({chg[j]:+.0%})")
print(f"  最后12根bar:")
print(i1000.tail(12).assign(chg=np.append(chg[-11:], np.nan))[["date", "close", "chg"]].to_string(index=False))

# 用 399006 校验近期走势（399006 相对干净？先打印近期）
cyb = load_idx("399006.SZ")
print("\n== 399006 创业板指 近 12 根 ==")
cc = cyb["close"].values
cchg = np.diff(cc) / cc[:-1]
bad_cyb = np.where((np.abs(cchg) > 0.50) | (cc[1:] < 100))[0]
print(f"  399006 脏bar数量: {len(bad_cyb)}")
print(cyb.tail(12).assign(chg=np.append(cchg[-11:], np.nan))[["date", "close", "chg"]].to_string(index=False))

# 本回撤片段: 创业板指从高点回撤
peak_i = cc[:-15].argmax()
print(f"\n  创业板指近端峰值 {cyb['date'].iloc[peak_i].date()} @ {cc[peak_i]:.0f} → 11-05 {cc[-1]:.0f} = {cc[-1]/cc[peak_i]-1:+.1%}")

# —— 1) V日队列 + 近期高计数日部分收益扫描 ——
basic = pd.read_parquet(os.path.join(DATA, "dataset", "stock_basic.parquet"))[["code", "name", "float_mv"]]
basic["mv_yi"] = basic["float_mv"] / 1e8
uni = basic[basic["mv_yi"].between(30, 300) & ~basic["name"].astype(str).str.contains("ST|退", na=False)]
uni_set = set(uni["code"])

cyb["prev_close"] = cyb["close"].shift(1)
cyb["open_gap"] = cyb["open"] / cyb["prev_close"] - 1
cyb["chg"] = cyb["close"] / cyb["prev_close"] - 1
vdays = cyb[(cyb["open_gap"] <= -0.015) & (cyb["chg"] >= 0.005)]
vdates = [pd.Timestamp(d) for d in vdays["date"]]
recent_hd = [pd.Timestamp(x) for x in ["2026-10-23", "2026-10-28", "2026-11-02", "2026-11-04", "2026-11-05"]]
targets = set(vdates + recent_hd)

files = sorted(glob.glob(os.path.join(DAILY, "*.parquet")))
files = [f for f in files if os.path.basename(f).replace(".parquet", "") in uni_set]

recs = {d: [] for d in targets}
for f in files:
    try:
        df = pd.read_parquet(f, columns=["date", "open", "high", "low", "close"])
    except Exception:
        continue
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    c = df["close"].values; o = df["open"].values
    h = df["high"].values; l = df["low"].values
    n = len(c)
    if n < 70:
        continue
    dmap = {d: i for i, d in enumerate(df["date"])}
    pct = np.full(n, np.nan); pct[1:] = c[1:] / c[:-1] - 1
    ow = (l == h) & (pct <= -0.095); ow[0] = False
    owc = np.cumsum(ow)
    ret20 = np.full(n, np.nan); ret20[20:] = c[20:] / c[:-20] - 1
    rng = h - l
    cpos = np.where(rng > 0, (c - l) / np.where(rng > 0, rng, 1), 0.0)
    for d in targets:
        i = dmap.get(d)
        if i is None or i < 60 or c[i] <= 0:
            continue
        if ret20[i] <= -0.20 and cpos[i] >= 0.5:
            lo = max(0, i - 20)
            if owc[i] - (owc[lo - 1] if lo > 0 else 0) > 0:
                continue
            r = dict(
                gap=o[i + 1] / c[i] - 1 if i + 1 < n else np.nan,
                t10_close=c[i + 10] / c[i] - 1 - COST if i + 10 < n else np.nan,
                t10_exec=c[i + 10] / o[i + 1] - 1 - COST if i + 10 < n and i + 1 < n and o[i + 1] > 0 else np.nan,
                mtm_now=c[-1] / c[i] - 1,  # 到 11-05 的浮动收益
                days_held=n - 1 - i,
            )
            recs[d].append(r)

print("\n== V日队列（信号次日开盘 vs 信号收盘入场，T+10）==")
allv_close, allv_exec, allv_gap = [], [], []
for d in vdates:
    rs = recs[d]
    if not rs:
        continue
    dfv = pd.DataFrame(rs)
    tc, te, gp = dfv["t10_close"].dropna(), dfv["t10_exec"].dropna(), dfv["gap"].dropna()
    if len(tc):
        allv_close += tc.tolist(); allv_exec += te.tolist(); allv_gap += gp.tolist()
        print(f"  {d.date()}: N={len(dfv)} gap中位 {gp.median():+.2%} | 收盘入场 t10 中位 {tc.median():+.2%} | 次开入场 t10 中位 {te.median():+.2%} | 差 {te.median()-tc.median():+.2%}pp")
sv = pd.Series(allv_close); se = pd.Series(allv_exec); sg = pd.Series(allv_gap)
print(f"  合计 V日样本 N={len(sv)}: 收盘入场 t10 中位 {sv.median():+.2%} 均值 {sv.mean():+.2%} | 次开入场 中位 {se.median():+.2%} 均值 {se.mean():+.2%}")
print(f"  V日隔夜gap 中位 {sg.median():+.2%} vs 全信号日 gap 中位 -0.09%")

print("\n== 本回撤片段已发信号的浮亏（到 11-05 收盘）==")
for d in recent_hd:
    rs = recs[d]
    if not rs:
        print(f"  {d.date()}: 无信号")
        continue
    dfv = pd.DataFrame(rs)
    m = dfv["mtm_now"]
    print(f"  {d.date()}: N={len(dfv)} 持有 {int(dfv['days_held'].iloc[0])} 日 → 浮动中位 {m.median():+.2%} 均值 {m.mean():+.2%} 为正比例 {(m>0).mean():.1%} 最差 {m.min():+.1%}")

# —— 2) ICC：周内共同因子占比 ——
sig = pd.read_csv(os.path.join(OUT, "r1_signals.csv"))
g2 = sig[sig["g2"]].copy()
groups = g2.groupby("wk")["t10"]
W_, N_ = groups.ngroups, len(g2)
nbar = N_ / W_
grand = g2["t10"].mean()
msb = sum(len(v) * (v.mean() - grand) ** 2 for _, v in groups) / (W_ - 1)
msw = sum(((v - v.mean()) ** 2).sum() for _, v in groups) / (N_ - W_)
icc = (msb - msw) / (msb + (nbar - 1) * msw)
print(f"\n== 周内 ICC（共同因子相关）== MSB={msb:.5f} MSW={msw:.5f} n̄={nbar:.0f}")
print(f"  ICC = {icc:.2f} → 同周两只信号票 T+10 收益平均相关 {icc:.2f}")
print(f"  4 只同日票组合波动 ≈ 单票的 {math.sqrt((1+3*icc)/4):.2f} 倍（独立=0.50）")

# —— 3) 中证1000 修复后重回归（剔除含脏bar的周窗口）——
Wf = pd.read_csv(os.path.join(OUT, "r1_weekly_feats.csv"))
Wf["wk"] = pd.to_datetime(Wf["wk"])
bad_dates = set(i1000["date"].iloc[j + 1] for j in bad)
# 受影响周：其 10 日前向窗口覆盖脏bar日期
def affected(row):
    j = i1000.index[i1000["date"] == row["wk"]]
    if len(j) == 0:
        return False
    j = j[0]
    win = i1000["date"].iloc[j:j + 11]
    return any(d in bad_dates for d in win)
Wf["aff"] = Wf.apply(affected, axis=1)
print(f"\n== 回归稳健性：受中证1000脏bar影响的周 {Wf['aff'].sum()} 个 ==")
clean = Wf[~Wf["aff"]].dropna(subset=["port_mean", "i1000_r10"])
ols(clean["port_mean"], clean["i1000_r10"], "剔除脏bar周后 vs 中证1000")
ols(Wf.dropna(subset=["port_mean", "i1000_r10"])["port_mean"],
    Wf.dropna(subset=["port_mean", "i1000_r10"])["i1000_r10"], "全样本 vs 中证1000")
