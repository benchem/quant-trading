#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R1 收尾: 真退市/停牌删失 + 今日候选G2复核 + main10一字分布 + mv_then抽检。"""
import os
import re

import numpy as np
import pandas as pd

OUT = "/root/workspaces/quant-trading/deepscan/20261105_oversold_g2"
DAILY = "/opt/quant-trading/data/daily/hfq"

meta = pd.read_csv(f"{OUT}/r1_stock_meta.csv")
meta["last_dt"] = pd.to_datetime(meta["last_bar"])
sig = pd.read_csv(f"{OUT}/r1_signals_full.csv")
sig["mv_then"] = sig["mv_then"] / 1e8
for col in ("is_st", "is_tui", "in_basic", "g2_21", "g2_board"):
    sig[col] = sig[col].astype("boolean").fillna(False).astype(bool)

# 周信号日(全局一次)
_files = sorted(f for f in os.listdir(DAILY) if re.match(r"^\d{6}\.(SH|SZ)\.parquet$", f))
_dates = pd.concat([pd.read_parquet(os.path.join(DAILY, f), columns=["date"])["date"] for f in _files[:300]])
_tds = sorted(set(pd.to_datetime(_dates)))
weekly, seen = [], None
for dt in reversed(_tds):
    wk = dt.isocalendar()[:2]
    if wk != seen:
        weekly.append(dt)
        seen = wk
weekly = sorted(weekly)[-52:]

# ============ A. 真正中途断数据的股票(末bar<2026-11-04)的删失信号 ============
dead_stocks = meta[meta["last_dt"] < pd.Timestamp("2026-11-04")]["code"].tolist()
print(f"中途断数据股票 {len(dead_stocks)} 只: {dead_stocks}")
print("\n这些股票在窗口内的 base 信号(含删失, 持有到最后可得bar):")
rows = []
for code in dead_stocks:
    df = pd.read_parquet(f"{DAILY}/{code}.parquet", columns=["date", "close", "high", "low"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    c, h, l = (df[x].to_numpy(float) for x in ("close", "high", "low"))
    n = len(c)
    dmap = {d: i for i, d in enumerate(df["date"])}
    for sd in weekly:
        i = dmap.get(sd)
        if i is None or i < 60 or c[i] <= 0:
            continue
        ret20 = c[i] / c[i - 20] - 1
        rng = h[i] - l[i]
        cpos = (c[i] - l[i]) / rng if rng > 0 else 0.0
        if ret20 > -0.20 or cpos < 0.5:
            continue
        j = min(i + 10, n - 1)
        rows.append(dict(code=code, sig=str(sd.date()), last=str(df["date"].iloc[-1].date()),
                         bars=j - i, ret=c[j] / c[i] - 1 - 0.0068))
d = pd.DataFrame(rows)
if len(d):
    print(d.to_string(index=False, float_format=lambda x: f"{x:+.1%}"))
    print(f"中途断数据股的信号收益: 中位 {d['ret'].median():+.2%} 均值 {d['ret'].mean():+.2%} N={len(d)}")

# 不在basic(已退市缓存)的已有: 7信号 中位-23.26% → 合并成"退市/垂死经验分布"
nb = sig[~sig["in_basic"]]
combo = pd.concat([d["ret"], nb["t10"]]) if len(d) else nb["t10"]
print(f"\n退市+垂死合并经验: N={len(combo)} 中位 {combo.median():+.2%} 均值 {combo.mean():+.2%}")

# 用合并经验做敏感度
today_ok = sig["in_basic"] & ~sig["is_st"] & ~sig["is_tui"] & sig["mv_today_f"].between(30, 300)
g2v = sig.loc[today_ok & sig["g2_21"], "t10"].dropna().to_numpy()
rng_np = np.random.default_rng(7)
print("\n敏感度(补 k 个退市/垂死经验收益到官方G2集):")
for k in (10, 20, 30, 50):
    draws = rng_np.choice(combo.to_numpy(), k, replace=True)
    allv = np.concatenate([g2v, draws])
    print(f"  k={k:>3}: 中位 {np.median(allv):+.2%} 均值 {allv.mean():+.2%}")

# ============ B. 今日4候选 G2 窗口复核 ============
print("\n今日候选 G2 复核:")
for code in ("688668.SH", "001389.SZ", "600722.SH", "601579.SH"):
    df = pd.read_parquet(f"{DAILY}/{code}.parquet", columns=["date", "close", "high", "low"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    c, h, l = (df[x].to_numpy(float) for x in ("close", "high", "low"))
    n = len(c)
    i = n - 1
    evs = []
    for k in range(max(1, i - 20), i + 1):
        if l[k] == h[k]:
            p = c[k] / c[k - 1] - 1
            evs.append(f"{df['date'].iloc[k].date()} pct={p:+.1%}")
    bd = "kcb20/cyb20(20cm)" if code[:3] in ("688", "689", "300", "301", "302") else "main10(10cm)"
    print(f"  {code} [{bd}] 近21bar一字(low==high)事件: {evs if evs else '无'}")

# ============ C. main10 被剔样本的一字事件 pct 分布 ============
base = sig[sig["in_basic"] & ~sig["is_st"] & ~sig["is_tui"] & sig["mv_today_f"].between(30, 300)]
rej = base[~base["g2_21"]]
evs = []
for _, r in rej.iterrows():
    for p in str(r["ow_events"]).split("|"):
        if p:
            evs.append((r["board"], float(p.strip("+").replace("%", "")) / 100))
ev = pd.DataFrame(evs, columns=["board", "pct"])
print("\n被G2剔样本的一字事件 pct 分布:")
for bd in ("main10", "cyb20", "kcb20"):
    e = ev[ev["board"] == bd]["pct"]
    if len(e):
        print(f"  {bd}: n={len(e)} min {e.min():+.1%} max {e.max():+.1%} "
              f"其中≤-10.7%(主板不可能区/复牌): {(e <= -0.107).sum()}")

# ============ D. mv_then 抽检(3个官方G2信号, 独立法复核) ============
print("\nmv_then 抽检(信号日 vwap×今天股本 vs amount/turnover):")
basic = pd.read_parquet("/opt/quant-trading/data/dataset/stock_basic.parquet").set_index("code")
basic["shares"] = basic["float_mv"] / basic["latest_price"]
chk = sig[today_ok & sig["g2_21"]].sample(3, random_state=1)
for _, r in chk.iterrows():
    df = pd.read_parquet(f"{DAILY}/{r['code']}.parquet", columns=["date", "close", "volume", "amount"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    m = df[df["date"] == pd.Timestamp(r["wk"])]
    if len(m) and m["volume"].iloc[0] > 0:
        vwap = m["amount"].iloc[0] / (m["volume"].iloc[0] * 100)
        mv_alt = vwap * basic.loc[r["code"], "shares"] / 1e8
        print(f"  {r['code']} {r['wk']}: amount/turnover法 {r['mv_then']:.1f}亿 | "
              f"vwap×今天股本法 {mv_alt:.1f}亿 | 今天 {r['mv_today_f']:.1f}亿")
