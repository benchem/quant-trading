#!/usr/bin/env python3
"""市场基线：每周全 universe 等权 t1/t2/t3/t5/t10 均值（供家族矩阵做 abnormal）。"""
import glob
import os

import numpy as np
import pandas as pd
from multiprocessing import Pool

DATA = "/opt/quant-trading/data"
DAILY = os.path.join(DATA, "daily", "hfq")
BASIC = os.path.join(DATA, "dataset", "stock_basic.parquet")
COST = 0.0068
OUT = "/root/.hermes/cache/scratch"

b = pd.read_parquet(BASIC)[["code", "name", "float_mv"]]
keep = (b["float_mv"] / 1e8).between(30, 300) & ~b["name"].astype(str).str.contains("ST|退", na=False)
UNI = set(b.loc[keep, "code"])
files = sorted(glob.glob(os.path.join(DAILY, "*.parquet")))
files = [f for f in files if os.path.basename(f).replace(".parquet", "") in UNI]
dates = pd.concat([pd.read_parquet(f, columns=["date"])["date"] for f in files[:300]])
tds = sorted(set(pd.to_datetime(dates)))
weekly, seen = [], None
for dt in reversed(tds):
    wk = dt.isocalendar()[:2]
    if wk != seen:
        weekly.append(dt)
        seen = wk
weekly = sorted(weekly)[-52:]
HOLDS = [1, 2, 3, 5, 10]


def one_file(f):
    try:
        df = pd.read_parquet(f, columns=["date", "close"])
    except Exception:
        return []
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    c = df["close"].values
    n = len(c)
    dmap = {d: i for i, d in enumerate(df["date"])}
    rows = []
    for k, sd in enumerate(weekly):
        i = dmap.get(sd)
        if i is None or c[i] <= 0 or i + 10 >= n:
            continue
        row = dict(wk_idx=k, wk=str(sd.date()))
        for hh in HOLDS:
            row[f"m{hh}"] = c[i + hh] / c[i] - 1 - COST if i + hh < n else np.nan
        rows.append(row)
    return rows


if __name__ == "__main__":
    with Pool(8) as p:
        allrows = []
        for chunk in p.imap_unordered(one_file, files, chunksize=40):
            allrows.extend(chunk)
    d = pd.DataFrame(allrows)
    g = d.groupby(["wk_idx", "wk"]).mean(numeric_only=True).reset_index()
    g.to_csv(os.path.join(OUT, "r1_market_weekly.csv"), index=False)
    print(g.round(5).to_string())
