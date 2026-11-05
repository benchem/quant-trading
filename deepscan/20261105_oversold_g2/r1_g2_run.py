#!/usr/bin/env python3
"""R1 统计评审复现：52周镜像全量重跑，逐(code,week)落盘 base/G2-20/G2-21/G1/G2/G3 标记
+ 市场基线。口径逐一复刻 scan_oversold_g2.py / probe_pullback.py。"""
import glob
import os
import sys

import numpy as np
import pandas as pd
from multiprocessing import Pool

DATA = "/opt/quant-trading/data"
DAILY = os.path.join(DATA, "daily", "hfq")
BASIC = os.path.join(DATA, "dataset", "stock_basic.parquet")
COST = 0.0068
OUT = "/root/.hermes/cache/scratch"

b = pd.read_parquet(BASIC)[["code", "name", "float_mv"]]
b["mv"] = b["float_mv"] / 1e8
keep = b["mv"].between(30, 300) & ~b["name"].astype(str).str.contains("ST|退", na=False)
UNI = set(b.loc[keep, "code"])

files = sorted(glob.glob(os.path.join(DAILY, "*.parquet")))
files = [f for f in files if os.path.basename(f).replace(".parquet", "") in UNI]

# 周信号日：复刻 files[:300] 口径
dates = pd.concat([pd.read_parquet(f, columns=["date"])["date"] for f in files[:300]])
tds = sorted(set(pd.to_datetime(dates)))
weekly, seen = [], None
for dt in reversed(tds):
    wk = dt.isocalendar()[:2]
    if wk != seen:
        weekly.append(dt)
        seen = wk
weekly = sorted(weekly)[-52:]
WKS = [str(w.date()) for w in weekly]


def one_file(f):
    code = os.path.basename(f).replace(".parquet", "")
    try:
        df = pd.read_parquet(f, columns=["date", "close", "open", "high", "low"])
    except Exception:
        return []
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    c = df["close"].values
    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    n = len(c)
    dmap = {d: i for i, d in enumerate(df["date"])}
    rows = []
    for sd in weekly:
        i = dmap.get(sd)
        if i is None or i < 60 or i + 10 >= n or c[i] <= 0:
            continue
        wk = str(sd.date())
        # 市场基线（全部 universe）
        r10 = c[i + 10] / c[i] - 1 - COST
        r5 = c[i + 5] / c[i] - 1 - COST if i + 5 < n else np.nan
        row = dict(code=code, wk=wk, mkt_t10=r10, mkt_t5=r5)
        # base 信号 S1+S2
        ret20 = c[i] / c[i - 20] - 1
        rng = h[i] - l[i]
        cpos = (c[i] - l[i]) / rng if rng > 0 else 0.0
        if ret20 <= -0.20 and cpos >= 0.5:
            row["base"] = 1
            row["t10_close"] = r10
            if i + 11 < n and o[i + 1] > 0:
                row["t10_exec"] = c[i + 10] / o[i + 1] - 1 - COST
            # G2 两种实现：20 bar (scan_oversold_g2) vs 21 bar (probe_pullback)
            g2_20 = True
            g2_21 = True
            for k in range(max(0, i - 20), i + 1):  # 21 bar
                if k > 0 and l[k] == h[k] and c[k] / c[k - 1] - 1 <= -0.095:
                    g2_21 = False
                    if k >= i - 19:
                        g2_20 = False
            row["g2_20"] = int(g2_20)
            row["g2_21"] = int(g2_21)
            row["g1"] = int(n >= 250)
            ma60 = c[max(0, i - 59):i + 1].mean()
            row["g3"] = int(c[i] >= ma60 * 0.80)
        rows.append(row)
    return rows


if __name__ == "__main__":
    print(f"universe files: {len(files)}, weeks: {len(WKS)} {WKS[0]}~{WKS[-1]}", flush=True)
    with Pool(8) as p:
        allrows = []
        for j, chunk in enumerate(p.imap_unordered(one_file, files, chunksize=40)):
            allrows.extend(chunk)
            if (j + 1) % 500 == 0:
                print(f"  {j+1}/{len(files)} files done", flush=True)
    d = pd.DataFrame(allrows)
    d.to_csv(os.path.join(OUT, "r1_weekly_universe.csv.gz"), index=False, compression="gzip")
    print("saved", len(d), "rows")

    # 快速核对 headline
    for tag, m in [("base", d.base == 1), ("G2-20", d.g2_20 == 1), ("G2-21", d.g2_21 == 1),
                   ("strict", (d.get("g1") == 1) & (d.g2_20 == 1) & (d.get("g3") == 1))]:
        s = d.loc[m.fillna(False), "t10_close"].dropna()
        e = d.loc[m.fillna(False), "t10_exec"].dropna()
        print(f"{tag}: N_close={len(s)} 中位 {s.median():+.2%} 均值 {s.mean():+.2%} 胜率 {(s>0).mean():.1%} | "
              f"N_exec={len(e)} 中位 {e.median():+.2%} 均值 {e.mean():+.2%} 胜率 {(e>0).mean():.1%}", flush=True)
    mkt = d.drop_duplicates(["wk", "code"]).groupby("wk")["mkt_t10"].mean()
    print(f"市场基线 t10 全池中位 {d['mkt_t10'].median():+.2%} 均值 {d['mkt_t10'].mean():+.2%}")
