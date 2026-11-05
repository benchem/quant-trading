#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R1-Q3 幸存者偏差: 退市删失 + ST 反推 + 敏感度分析。"""
import os
import re

import numpy as np
import pandas as pd

OUT = "/root/workspaces/quant-trading/deepscan/20261105_oversold_g2"
DAILY = "/opt/quant-trading/data/daily/hfq"
W0, W1 = "2025-11-07", "2026-11-05"

meta = pd.read_csv(f"{OUT}/r1_stock_meta.csv")
sig = pd.read_csv(f"{OUT}/r1_signals_full.csv")
sig["mv_then"] = sig["mv_then"] / 1e8
for col in ("is_st", "is_tui", "in_basic", "g2_21", "g2_20", "g2_board"):
    sig[col] = sig[col].astype("boolean").fillna(False).astype(bool)
mkt = pd.read_csv(f"{OUT}/r1_mkt_baseline.csv")

today_ok = sig["in_basic"] & ~sig["is_st"] & ~sig["is_tui"] & sig["mv_today_f"].between(30, 300)
g2 = sig[today_ok & sig["g2_21"]]


def stat(d, col="t10", label=""):
    s = d[col].dropna()
    if len(s) == 0:
        print(f"  {label:<50} N=0")
        return None
    print(f"  {label:<50} N={len(s):>5} 中位 {s.median():+.2%} 均值 {s.mean():+.2%} 胜率 {(s>0).mean():.1%}")
    return s


print("=" * 70)
print("Q3 幸存者偏差")

# 1) 末bar 早于今天的股票(停牌/退市/缓存残留)
meta["last_dt"] = pd.to_datetime(meta["last_bar"])
ended = meta[meta["last_dt"] < "2026-11-04"]
ended_win = ended[(ended["last_dt"] >= W0)]
print(f"末bar在 2026-11-04 前的: {len(ended)} 只; 其中末bar落在52周窗口内(疑似窗口期退市/长停): {len(ended_win)} 只")
print(ended_win[["code", "board", "in_basic", "last_bar", "is_st", "mv_today_f", "bars"]]
      .to_string(index=False))

# 2) 不在 basic 的缓存文件(已退市)
nb = meta[~meta["in_basic"]]
print(f"\n不在 stock_basic 的缓存文件: {len(nb)} 只")
print(nb[["code", "last_bar", "bars"]].to_string(index=False))

# 3) 删失信号: base 信号后 10bar 内数据终止(官方回测因 i+10<n 直接丢弃)
print("\n删失扫描(重跑, 不要求 i+10<n):")
pat = re.compile(r"^\d{6}\.(SH|SZ)\.parquet$")
files = sorted(f for f in os.listdir(DAILY) if pat.match(f))
dates = pd.concat([pd.read_parquet(os.path.join(DAILY, f), columns=["date"])["date"]
                   for f in files[:300]])
trade_dates = sorted(set(pd.to_datetime(dates)))
weekly, seen = [], None
for dt in reversed(trade_dates):
    wk = dt.isocalendar()[:2]
    if wk != seen:
        weekly.append(dt)
        seen = wk
weekly = sorted(weekly)[-52:]

censor = []
for f in files:
    code = f.replace(".parquet", "")
    try:
        df = pd.read_parquet(os.path.join(DAILY, f), columns=["date", "close", "high", "low"])
    except Exception:
        continue
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    c, h, l = (df[x].to_numpy(float) for x in ("close", "high", "low"))
    n = len(c)
    if n < 70:
        continue
    dmap = {d: i for i, d in enumerate(df["date"])}
    last = df["date"].iloc[-1]
    for sd in weekly:
        i = dmap.get(sd)
        if i is None or i < 60:
            continue
        if c[i] <= 0:
            continue
        ret20 = c[i] / c[i - 20] - 1
        rng = h[i] - l[i]
        cpos = (c[i] - l[i]) / rng if rng > 0 else 0.0
        if ret20 > -0.20 or cpos < 0.5:
            continue
        if i + 10 < n:
            continue  # 未删失, 已入正式样本
        # 删失: 信号后不足10bar数据就终止
        ret_to_end = c[n - 1] / c[i] - 1
        censor.append(dict(code=code, wk=str(sd.date()), last_bar=str(last.date()),
                           bars_after=n - 1 - i, ret_to_end=ret_to_end,
                           in_basic=code in set(meta.loc[meta["in_basic"], "code"])))
cen = pd.DataFrame(censor)
print(f"删失 base 信号(信号后<10bar 即无数据): {len(cen)} 个")
if len(cen):
    print(cen.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    s = cen["ret_to_end"]
    print(f"  删失信号持有至末bar 收益: 中位 {s.median():+.2%} 均值 {s.mean():+.2%} 最差 {s.min():+.2%}")

# 4) 今天ST股的信号(被今天ST闸前视剔掉的)
st_all = sig[sig["is_st"] & sig["g2_21"]]
print()
stat(st_all, label="今天ST股的G2信号(无市值闸)")

# 5) 深跌群体的当前状态分布(唯一股票口径)
cohort = sig.drop_duplicates("code")
print(f"\n全宇宙深跌 base 信号股票数(唯一): {len(cohort)}")
n_st = cohort["is_st"].sum()
n_small = (cohort["mv_today_f"] < 30).sum()
n_tui = cohort["is_tui"].sum()
n_nb = (~cohort["in_basic"]).sum()
print(f"  其中: 今天ST {n_st} ({n_st/len(cohort):.1%}) | 今天<30亿 {n_small} ({n_small/len(cohort):.1%})"
      f" | 带'退' {n_tui} | 不在basic {n_nb}")
print(f"  官方宇宙(今天30-300亿非ST)只覆盖深跌群体的 {len(sig[today_ok].drop_duplicates('code'))/len(cohort):.1%}")

# 6) 敏感度: 补 k 个退市型样本(中位-23.26%缓存经验/或-50%面值退市)后官方G2中位/均值
print("\n敏感度分析(在官方G2集 N=1900 上补 k 个退市样本):")
g2v = g2["t10"].dropna().to_numpy()
cen_rets = cen["ret_to_end"].to_numpy() if len(cen) else np.array([-0.5])
cen_rets = cen_rets - 0.0068  # 扣成本对齐
rng_np = np.random.default_rng(42)
print(f"  {'k':>4} | 补'缓存删失'经验(中位{np.median(cen_rets):+.1%}) | 补-50%面值退市")
for k in (10, 20, 30, 50, 100):
    draws = rng_np.choice(cen_rets, k, replace=True)
    med1 = np.median(np.concatenate([g2v, draws]))
    med2 = np.median(np.concatenate([g2v, np.full(k, -0.5)]))
    print(f"  {k:>4} | 中位→{med1:+.2%} 均值→{np.concatenate([g2v, draws]).mean():+.2%}          | 中位→{med2:+.2%}")

# 7) 市场基线(同 survivor 库)
s = mkt["t10"].dropna()
print(f"\n市场基线(等权全缓存库, N={len(s)}): 中位 {s.median():+.2%} 均值 {s.mean():+.2%}")
print(f"官方G2均值edge(对基线): {g2['t10'].mean()-s.mean():+.2%}")
