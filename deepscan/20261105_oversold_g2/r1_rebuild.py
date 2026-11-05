#!/usr/bin/env python3
"""R1 风控评审 · 阶段1：重建 G2 信号集（含逐日持有路径）+ 创业板指联动字段。

口径严格复刻 scans/scan_oversold_g2.py：
  S1 ret20<=-20%  S2 cpos>=0.5  S3 G2 无一字跌停  S4 30~300亿非ST(当前值)
  52 周镜像信号日；i+11<n；执行口径 entry=o[i+1]，exit=c[i+10]，双边成本 0.68%。
额外记录：ret20/cpos/成交额/板块、c[i+1..i+10] 逐日 ohlc、-10% 止损模拟字段、
信号日对应的创业板指 fwd10/min10/d1/d5。
"""
import glob
import os

import numpy as np
import pandas as pd

DATA = "/opt/quant-trading/data"
DAILY = os.path.join(DATA, "daily", "hfq")
BASIC = os.path.join(DATA, "dataset", "stock_basic.parquet")
IDX = os.path.join(DATA, "index", "399006.SZ.parquet")
COST = 0.0068
OUT = "/root/workspaces/quant-trading/deepscan/20261105_oversold_g2"


def load_uni():
    b = pd.read_parquet(BASIC)[["code", "name", "float_mv"]]
    b["mv"] = b["float_mv"] / 1e8
    keep = b["mv"].between(30, 300) & ~b["name"].astype(str).str.contains("ST|退", na=False)
    return set(b.loc[keep, "code"])


def board(code):
    if code.startswith("30"):
        return "cyb20cm"
    if code.startswith("68"):
        return "kcb20cm"
    return "mb10cm"


def main():
    uni = load_uni()
    files = sorted(glob.glob(os.path.join(DAILY, "*.parquet")))
    files = [f for f in files if os.path.basename(f).replace(".parquet", "") in uni]
    print(f"universe files: {len(files)}")

    dates = pd.concat([pd.read_parquet(f, columns=["date"])["date"] for f in files[:300]])
    tds = sorted(set(pd.to_datetime(dates)))
    weekly, seen = [], None
    for dt in reversed(tds):
        wk = dt.isocalendar()[:2]
        if wk != seen:
            weekly.append(dt)
            seen = wk
    weekly = sorted(weekly)[-52:]
    print(f"weekly: {len(weekly)} {weekly[0].date()} ~ {weekly[-1].date()}")

    idx = pd.read_parquet(IDX)[["date", "close"]].copy()
    idx["date"] = pd.to_datetime(idx["date"])
    idx = idx.sort_values("date").reset_index(drop=True)
    ic = idx["close"].values
    imap = {d: i for i, d in enumerate(idx["date"])}
    wk_idx = {}
    for sd in weekly:
        j = imap.get(sd)
        if j is None or j + 10 >= len(ic):
            wk_idx[sd] = dict(idx_fwd10=None, idx_min10=None, idx_d1=None, idx_d5=None)
            continue
        wk_idx[sd] = dict(
            idx_fwd10=ic[j + 10] / ic[j] - 1,
            idx_min10=ic[j + 1:j + 11].min() / ic[j] - 1,
            idx_d1=ic[j + 1] / ic[j] - 1,
            idx_d5=ic[j + 5] / ic[j] - 1,
        )

    sigs, path_rows = [], []
    for f in files:
        code = os.path.basename(f).replace(".parquet", "")
        try:
            df = pd.read_parquet(f, columns=["date", "close", "open", "high", "low", "volume", "amount"])
        except Exception:
            continue
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        c, o, h, l = df["close"].values, df["open"].values, df["high"].values, df["low"].values
        amt = df["amount"].values
        dts = df["date"].values
        n = len(c)
        dmap = {d: i for i, d in enumerate(df["date"])}

        for sd in weekly:
            i = dmap.get(sd)
            if i is None or i + 11 >= n or c[i] <= 0 or i < 60:
                continue
            ret20 = c[i] / c[i - 20] - 1
            rng = h[i] - l[i]
            cpos = (c[i] - l[i]) / rng if rng > 0 else 0.0
            if ret20 > -0.20 or cpos < 0.5:
                continue
            g2 = True
            for k in range(max(0, i - 19), i + 1):
                if k > 0 and l[k] == h[k] and c[k] / c[k - 1] - 1 <= -0.095:
                    g2 = False
                    break
            if not g2:
                continue

            entry = o[i + 1]
            wk = str(sd.date())
            t10c = c[i + 10] / c[i] - 1 - COST
            t10e = (c[i + 10] / entry - 1 - COST) if entry > 0 else np.nan

            # —— -10% 灾难止损模拟（对 entry=o[i+1]）——
            stop_k, touch_k = None, None
            if entry > 0:
                for k in range(1, 11):
                    if c[i + k] <= entry * 0.90:
                        stop_k = k
                        break
                for k in range(1, 11):
                    if l[i + k] <= entry * 0.90:
                        touch_k = k
                        break
            stop_exit_c = (c[i + stop_k] / entry - 1 - COST) if stop_k else np.nan
            # 次日开盘卖出版（stop_k=10 时即终点收盘）
            if stop_k and stop_k < 10:
                stop_exit_no = (o[i + stop_k + 1] / entry - 1 - COST)
            elif stop_k == 10:
                stop_exit_no = stop_exit_c
            else:
                stop_exit_no = np.nan
            # 止损后至 T+10 的反弹（从止损价到终点收盘，不加成本）
            rebound_c = (c[i + 10] / c[i + stop_k] - 1) if stop_k else np.nan
            rebound_no = (c[i + 10] / o[i + stop_k + 1] - 1) if (stop_k and stop_k < 10) else (
                rebound_c if stop_k == 10 else np.nan)
            # 触及-10%后收回（盘中破但收盘未破）
            touched_not_closed = bool(touch_k and not stop_k)

            xi = wk_idx[sd]
            sigs.append(dict(
                code=code, wk=wk, board=board(code), ret20=ret20, cpos=cpos,
                amount_yi=amt[i] / 1e8 if pd.notna(amt[i]) else np.nan,
                entry_open=entry, t10_close=t10c, t10_exec=t10e,
                stop_k=stop_k, touch_k=touch_k, touched_not_closed=touched_not_closed,
                stop_exit_c=stop_exit_c, stop_exit_no=stop_exit_no,
                rebound_c=rebound_c, rebound_no=rebound_no,
                **xi,
            ))
            for k in range(1, 11):
                path_rows.append(dict(
                    code=code, wk=wk, k=k, date=pd.Timestamp(dts[i + k]).date(),
                    o=o[i + k], h=h[i + k], l=l[i + k], c=c[i + k],
                    ret_from_entry=(c[i + k] / entry - 1) if entry > 0 else np.nan,
                ))

    d = pd.DataFrame(sigs)
    p = pd.DataFrame(path_rows)
    d.to_parquet(os.path.join(OUT, "r1_signals.parquet"))
    p.to_parquet(os.path.join(OUT, "r1_path.parquet"))
    print(f"\nsignals N={len(d)}  path rows={len(p)}")
    print(f"t10_close : 中位 {d.t10_close.median():+.2%} 均值 {d.t10_close.mean():+.2%} 胜率 {(d.t10_close>0).mean():.1%}")
    print(f"t10_exec  : 中位 {d.t10_exec.median():+.2%} 均值 {d.t10_exec.mean():+.2%} 胜率 {(d.t10_exec>0).mean():.1%}")
    print(f"对照 digest: close N=1893 中位+0.24% / exec N=1901 中位+0.80%")
    print(f"信号周数: {d.wk.nunique()}  最末完整周: {sorted(d.wk.unique())[-3:]}")


if __name__ == "__main__":
    main()
