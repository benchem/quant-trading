#!/usr/bin/env python3
"""R1 攻击评审 step1：重建 G2 信号全集（含周组合、每日信号计数、持有期路径）。
严格复刻 probe_oversold.py + probe_pullback.py 口径：
  universe = stock_basic 当前 float_mv 30~300亿 & 非ST
  信号 = ret20<=-20% & cpos>=0.5 & G2(近20日无一字跌停) & i+10<n
输出: r1_signals.csv(周信号明细), r1_weekly.csv(周组合), r1_daily.csv(每日信号计数+t10)
"""
import glob, os
import numpy as np
import pandas as pd

DATA = "/opt/quant-trading/data"
DAILY = os.path.join(DATA, "daily", "hfq")
BASIC = os.path.join(DATA, "dataset", "stock_basic.parquet")
COST = 0.0068
OUT = "/root/workspaces/quant-trading/deepscan/20261105_oversold_g2"

basic = pd.read_parquet(BASIC)[["code", "name", "float_mv"]]
basic["mv_yi"] = basic["float_mv"] / 1e8
uni = basic[basic["mv_yi"].between(30, 300) & ~basic["name"].astype(str).str.contains("ST|退", na=False)]
uni_map = uni.set_index("code")

files = sorted(glob.glob(os.path.join(DAILY, "*.parquet")))
files = [f for f in files if os.path.basename(f).replace(".parquet", "") in uni_map.index]
print(f"universe 文件数: {len(files)}")

# 周信号日（复刻：files[:300] 并集日历，每周最后交易日，取 52 周）
dates = pd.concat([pd.read_parquet(f, columns=["date"])["date"] for f in files[:300]])
tds = sorted(set(pd.to_datetime(dates)))
weekly, seen = [], None
for dt in reversed(tds):
    wk = dt.isocalendar()[:2]
    if wk != seen:
        weekly.append(dt); seen = wk
weekly = sorted(weekly)[-52:]
print(f"周信号日: {len(weekly)}（{weekly[0].date()} ~ {weekly[-1].date()}）")
wk_set = set(weekly)

# 每日分析窗口：近 300 个交易日
daily_dates = tds[-300:]
dd_index = {d: k for k, d in enumerate(daily_dates)}

sig_recs, mkt_recs = [], []
daily_cnt = {}     # date -> [n_sig, sum_t10, n_valid_t10]
today = None

for f in files:
    code = os.path.basename(f).replace(".parquet", "")
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

    # —— 预计算逐日向量化信号要素 ——
    pct = np.full(n, np.nan); pct[1:] = c[1:] / c[:-1] - 1
    oneword = (l == h) & ~(pct <= -0.095) == False  # placeholder, 重算 below
    oneword = (l == h) & (pct <= -0.095)
    oneword[0] = False
    ow_cum = np.cumsum(oneword)
    def ow_in_win(i):  # range(max(0,i-20), i+1)
        lo = max(0, i - 20)
        return (ow_cum[i] - (ow_cum[lo - 1] if lo > 0 else 0)) > 0
    ret20 = np.full(n, np.nan); ret20[20:] = c[20:] / c[:-20] - 1
    rng = h - l
    cpos = np.where(rng > 0, (c - l) / np.where(rng > 0, rng, 1), 0.0)
    c_last = c[-1]
    mv_today = uni_map.loc[code, "mv_yi"]

    # —— 每日信号计数（近300交易日，同口径：G2窗口 range(i-20,i+1)）——
    for d, i in dmap.items():
        if d not in dd_index or i < 60 or c[i] <= 0:
            continue
        if ret20[i] <= -0.20 and cpos[i] >= 0.5 and not ow_in_win(i):
            rec = daily_cnt.setdefault(d, [0, 0.0, 0])
            rec[0] += 1
            if i + 10 < n and not np.isnan(c[i + 10]):
                rec[1] += c[i + 10] / c[i] - 1 - COST
                rec[2] += 1

    # —— 周信号（回测口径明细）——
    for sd in weekly:
        i = dmap.get(sd)
        if i is None or i < 60 or c[i] <= 0:
            continue
        # 市场基线（全 universe）
        if i + 10 < n:
            mkt_recs.append(dict(wk=str(sd.date()), t10=c[i + 10] / c[i] - 1 - COST))
        # 基础信号
        if not (ret20[i] <= -0.20 and cpos[i] >= 0.5):
            continue
        g2 = not ow_in_win(i)
        # 明细字段（base 层也记，g2 标记区分）
        if i + 10 >= n:
            continue
        nxt = c[i + 1:i + 11]
        min_next = float(np.nanmin(nxt)) if len(nxt) else np.nan
        low20 = float(np.nanmin(c[max(0, i - 19):i + 1]))
        sig_recs.append(dict(
            code=code, wk=str(sd.date()), g2=g2,
            ret20=float(ret20[i]), cpos=float(cpos[i]),
            c_sig=float(c[i]),
            o_next=float(o[i + 1]) if i + 1 < n and not np.isnan(o[i + 1]) else np.nan,
            t10=float(c[i + 10] / c[i] - 1 - COST),
            t10_exec=float(c[i + 10] / o[i + 1] - 1 - COST) if i + 1 < n and o[i + 1] and not np.isnan(o[i + 1]) else np.nan,
            t10_gross=float(c[i + 10] / c[i] - 1),
            min_next10=float(min_next / c[i] - 1) if not np.isnan(min_next) else np.nan,
            newlow20=bool(min_next < low20) if not np.isnan(min_next) else None,
            below_sig=bool(c[i + 10] < c[i]),
            mv_signal=float(mv_today * c[i] / c_last) if c_last and not np.isnan(c_last) else np.nan,
        ))

sig = pd.DataFrame(sig_recs)
mkt = pd.DataFrame(mkt_recs)
sig.to_csv(os.path.join(OUT, "r1_signals.csv"), index=False)

# 复现校验
g2 = sig[sig["g2"]]
print("\n== 复现校验 ==")
print(f"base 信号 N={len(sig)}  T+10 中位 {sig['t10'].median():+.3%} 均值 {sig['t10'].mean():+.3%} 胜率 {(sig['t10']>0).mean():.2%}")
print(f"G2   信号 N={len(g2)}  T+10 中位 {g2['t10'].median():+.3%} 均值 {g2['t10'].mean():+.3%} 胜率 {(g2['t10']>0).mean():.2%}")
print(f"G2 exec口径 中位 {g2['t10_exec'].median():+.3%} 均值 {g2['t10_exec'].mean():+.3%}")
print(f"被 G2 剔除 N={len(sig)-len(g2)}  中位 {sig[~sig['g2']]['t10'].median():+.3%}")
print(f"市场基线 N={len(mkt)} 中位 {mkt['t10'].median():+.3%}")

# 周组合
wk_port = g2.groupby("wk")["t10"].agg(["count", "mean", "median"])
mkt_wk = mkt.groupby("wk")["t10"].agg(["mean", "median"]).rename(columns={"mean": "mkt_mean", "median": "mkt_median"})
weekly_df = wk_port.join(mkt_wk, how="outer").fillna({"count": 0})
weekly_df.to_csv(os.path.join(OUT, "r1_weekly.csv"))

# 每日计数
dd = pd.DataFrame([(str(d.date()), v[0], v[1] / v[2] if v[2] else np.nan, v[2])
                   for d, v in sorted(daily_cnt.items())],
                  columns=["date", "n_sig", "t10_mean", "n_valid"])
dd.to_csv(os.path.join(OUT, "r1_daily.csv"), index=False)
print(f"\n每日计数天数: {len(dd)}，末日 {dd.iloc[-1]['date']} 信号数 {dd.iloc[-1]['n_sig']}")
print(f"历史日信号数: 中位 {dd['n_sig'].median():.0f} P90 {dd['n_sig'].quantile(0.9):.0f} 最大 {dd['n_sig'].max():.0f} @ {dd.loc[dd['n_sig'].idxmax(),'date']}")
print("top10 日计数:")
print(dd.nlargest(10, "n_sig")[["date", "n_sig", "t10_mean"]].to_string(index=False))
