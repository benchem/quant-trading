#!/usr/bin/env python3
"""R1 执行评审（执行/微观结构流派）：重建 52 周信号集（复刻 scan_oversold_g2.py 口径），
补齐 gap/出场涨跌停/停牌/市场共振 字段，存 CSV 供后续分析。"""
import glob, os
import numpy as np, pandas as pd

DATA = "/opt/quant-trading/data"
DAILY = os.path.join(DATA, "daily", "hfq")
BASIC = os.path.join(DATA, "dataset", "stock_basic.parquet")
OUTDIR = "/root/workspaces/quant-trading/deepscan/20261105_oversold_g2"
OUT = os.path.join(OUTDIR, "r1_execflow_signals.csv")
COST = 0.0068

b = pd.read_parquet(BASIC)[["code", "name", "float_mv"]]
b["mv"] = b["float_mv"] / 1e8
uni = b[b["mv"].between(30, 300) & ~b["name"].astype(str).str.contains("ST|退", na=False)].set_index("code")
allf = sorted(glob.glob(os.path.join(DAILY, "*.parquet")))
files = [f for f in allf if os.path.basename(f).replace(".parquet", "") in uni.index]
print("universe:", len(uni), "files:", len(files), "of", len(allf))

# —— 完整市场日历（全 parquet 日期并集）——
cal = set()
for f in allf:
    try:
        cal.update(pd.to_datetime(pd.read_parquet(f, columns=["date"])["date"]).tolist())
    except Exception:
        pass
tds = sorted(cal)
tdi = {d: i for i, d in enumerate(tds)}
print("calendar:", tds[0].date(), "->", tds[-1].date(), "days:", len(tds))

# —— 复刻 scanner 的 weekly 日期（files[:300] 派生，保证可比）——
dates300 = pd.concat([pd.read_parquet(f, columns=["date"])["date"] for f in files[:300]])
tds300 = sorted(set(pd.to_datetime(dates300)))
weekly, seen = [], None
for dt in reversed(tds300):
    wk = dt.isocalendar()[:2]
    if wk != seen:
        weekly.append(dt)
        seen = wk
weekly = sorted(weekly)[-52:]
print("weekly:", len(weekly), weekly[0].date(), "->", weekly[-1].date())
onlyfull = sorted(set(tds) - set(tds300))
print("days in full-cal not in files[:300]-cal:", [d.date() for d in onlyfull])


def board_limit(code):
    num = code[:6]
    if code.endswith(".BJ"):
        return 0.30
    if code.endswith(".SH") and num.startswith("68"):
        return 0.20
    if code.endswith(".SZ") and num[:3] in ("300", "301"):
        return 0.20
    return 0.10


rows = []
ow_sig_extra = []          # digest 口径（一字bar cpos按0.5）会多进、scanner 口径拒掉的信号
mkt = {}                   # date -> [sum_ret, n, sum_gap, gn]
breadth = {}               # date -> universe 内 ret20<=-20% 家数
sus_rows = []              # (code, resume_date, gap_md, deep_ever)
SUS_START = pd.Timestamp("2025-11-01")

for f in files:
    code = os.path.basename(f).replace(".parquet", "")
    try:
        df = pd.read_parquet(f, columns=["date", "open", "high", "low", "close", "amount"])
    except Exception:
        continue
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    c = df["close"].values; o = df["open"].values
    h = df["high"].values; l = df["low"].values
    n = len(c)
    if n < 80:
        continue
    dts = list(df["date"])
    dmap = {d: i for i, d in enumerate(df["date"])}

    # —— 市场累计（近 ~300 bar）：等权日收益 / 隔夜 gap / 深跌宽度 ——
    deep_any = False
    for k in range(max(1, n - 300), n):
        d = dts[k]
        if c[k - 1] > 0:
            r = c[k] / c[k - 1] - 1
            e = mkt.setdefault(d, [0.0, 0, 0.0, 0])
            if np.isfinite(r):
                e[0] += r; e[1] += 1
            if o[k] > 0:
                g = o[k] / c[k - 1] - 1
                if np.isfinite(g):
                    e[2] += g; e[3] += 1
        if k >= 20 and c[k - 20] > 0:
            r20 = c[k] / c[k - 20] - 1
            if r20 <= -0.20:
                breadth[d] = breadth.get(d, 0) + 1
                deep_any = True

    # —— 停牌事件（近 52 周，市场日间隔>=2）——
    for k in range(1, n):
        if dts[k] >= SUS_START and dts[k - 1] >= SUS_START:
            gi, gj = tdi.get(dts[k]), tdi.get(dts[k - 1])
            if gi is not None and gj is not None and gi - gj >= 2:
                sus_rows.append((code, dts[k], gi - gj, deep_any))

    def has_ow(i, days=20):
        for k in range(max(0, i - days + 1), i + 1):
            if k == 0:
                continue
            if l[k] == h[k] and c[k] / c[k - 1] - 1 <= -0.095:
                return True
        return False

    for sd in weekly:
        i = dmap.get(sd)
        if i is None or i + 11 >= n or i < 60:
            continue
        ret20s = c[i] / c[i - 20] - 1
        rng = h[i] - l[i]
        cpos = (c[i] - l[i]) / rng if rng > 0 else 0.0
        if ret20s > -0.20:
            continue
        if rng == 0:
            # 一字bar：scanner 口径 cpos=0 被拒；digest 口径按0.5放行 → 记录差异
            if not has_ow(i):
                ow_sig_extra.append((code, sd, c[i] / c[i - 1] - 1))
            continue
        if cpos < 0.5 or has_ow(i):
            continue
        if c[i] <= 0 or o[i + 1] <= 0 or not np.isfinite(o[i + 1]):
            continue
        j = i + 10
        ti, t1, tj = tdi.get(sd), tdi.get(dts[i + 1]), tdi.get(dts[j])
        rows.append(dict(
            code=code, sd=sd, wd=sd.dayofweek, ret20=ret20s,
            gap=o[i + 1] / c[i] - 1,
            execret=c[j] / o[i + 1] - 1,
            closeret=c[j] / c[i] - 1,
            sig_pct=c[i] / c[i - 1] - 1,
            amount_yi=float(df["amount"].iloc[i]) / 1e8 if pd.notna(df["amount"].iloc[i]) else np.nan,
            lim=board_limit(code),
            entry_gap_md=(t1 - ti) if (t1 is not None and ti is not None) else np.nan,
            hold_md=(tj - t1) if (tj is not None and t1 is not None) else np.nan,
            exit_pct=c[j] / c[j - 1] - 1,
            exit_c_eq_l=bool(c[j] == l[j]),
            exit_ow=bool(l[j] == h[j]),
            exit_c_eq_h=bool(c[j] == h[j]),
            h1_run=h[i + 1] / o[i + 1] - 1,
            l1_dip=l[i + 1] / o[i + 1] - 1,
            exit_next_ret=(c[j + 1] / c[j] - 1) if j + 1 < n else np.nan,
            exit_next_ow=bool(l[j + 1] == h[j + 1]) if j + 1 < n else np.nan,
        ))

sig = pd.DataFrame(rows)
sig.to_csv(OUT, index=False)
pd.DataFrame(sus_rows, columns=["code", "resume_date", "gap_md", "deep_ever"]).to_csv(
    os.path.join(OUTDIR, "r1_execflow_suspension.csv"), index=False)
mk = pd.DataFrame([(d, v[0] / v[1] if v[1] else np.nan, v[2] / v[3] if v[3] else np.nan, breadth.get(d, 0))
                   for d, v in sorted(mkt.items())], columns=["date", "ew_ret", "ew_gap", "breadth"])
mk.to_csv(os.path.join(OUTDIR, "r1_execflow_market.csv"), index=False)

print("\n== 复现校验 ==")
d1 = sig["closeret"] - COST
d2 = sig["execret"] - COST
print(f"close->close: N={len(d1)} 中位 {d1.median():+.2%} 均值 {d1.mean():+.2%} 胜率 {(d1>0).mean():.1%}")
print(f"open->close : N={len(d2)} 中位 {d2.median():+.2%} 均值 {d2.mean():+.2%} 胜率 {(d2>0).mean():.1%}")
print("digest 目标 : close N=1893 中位+0.24% | open N=1901 中位+0.80%")
print("\n一字bar信号差异（digest口径会多进）:", len(ow_sig_extra))
print("停牌事件条数:", len(sus_rows))
cnt = sig.groupby("sd").size()
print("\n每周信号家数分布:"); print(cnt.describe())
