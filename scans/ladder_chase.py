#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三级阶梯：追高 edge 到底死在哪一环
V1 = 简化chase(涨幅≥5%+量比≥2+cpos≥0.7), 周信号日, 信号日收盘买, 当前市值30~300亿  ← 统计官口径(应复现+)
V2 = V1 但市值用当时值(修前视)
V3 = V2 但逐日信号 + T+1开盘买 + 一字涨停跳过 + 跌停顺延  ← 完全现实
全部: T+5收盘卖, 扣双边0.68%, 2026-06-01起, 排除ST/北交所/300687/300199
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

DATA = "/opt/quant-trading/data"
DAILY = os.path.join(DATA, "daily", "hfq")
BASIC = os.path.join(DATA, "dataset", "stock_basic.parquet")
START = pd.Timestamp("2026-06-01")
COST = 0.0068
EXCLUDE = {"300687.SZ", "300199.SZ"}


def lim(code):
    return 0.195 if code[:3] in ("300", "301", "688") else 0.095


def main():
    basic = pd.read_parquet(BASIC)[["code", "name", "float_mv"]]
    st = set(basic.loc[basic["name"].astype(str).str.contains("ST|退", na=False), "code"])
    cur_cap_ok = set(basic.loc[(basic.float_mv >= 30e8) & (basic.float_mv <= 300e8), "code"])

    stocks = {}
    for f in sorted(glob.glob(os.path.join(DAILY, "*.parquet"))):
        code = os.path.basename(f).replace(".parquet", "")
        if code[:1] in ("8", "4", "9") or code in EXCLUDE or code in st:
            continue
        df = pd.read_parquet(f, columns=["date", "open", "high", "low", "close",
                                         "volume", "amount", "turnover"])
        if len(df) < 62:
            continue
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        c, v = df.close.astype(float), df.volume.astype(float)
        df["pct"] = c / c.shift(1) - 1
        df["vr"] = v / v.shift(1).rolling(5).mean()
        rng = df.high - df.low
        df["cpos"] = (df.close - df.low) / rng.replace(0, np.nan)
        tur = df.turnover.astype(float)
        df["cap_then"] = np.where(tur > 0, df.amount * 100.0 / tur, np.nan)
        stocks[code] = df

    cal = sorted(set().union(*[set(df.index) for df in stocks.values()]))
    cal = pd.DatetimeIndex(cal)
    pos = {d: i for i, d in enumerate(cal)}
    days = [d for d in cal if d >= START]

    def chase3(r):
        return r.pct >= 0.05 and r.vr >= 2.0 and r.cpos >= 0.70

    def collect(mode):
        """mode: 'V1'|'V2'|'V3' — 返回逐笔净收益列表"""
        out = []
        sig_days = days if mode == "V3" else last_dow(days)
        for T in sig_days:
            for code, df in stocks.items():
                if T not in df.index:
                    continue
                r = df.loc[T]
                if any(pd.isna(x) for x in (r.pct, r.vr, r.cpos)):
                    continue
                if not chase3(r):
                    continue
                if mode == "V1":
                    if code not in cur_cap_ok or not (r.amount >= 2e8):
                        continue
                else:
                    if pd.isna(r.cap_then) or not (30e8 <= r.cap_then <= 300e8) or not (r.amount >= 2e8):
                        continue
                # 入场
                if mode in ("V1", "V2"):
                    i = pos[T]
                    if i + 5 >= len(cal) or cal[i + 5] not in df.index:
                        continue
                    px_in, sd = float(r.close), cal[i + 5]
                else:
                    i = pos[T]
                    if i + 1 >= len(cal) or i + 5 >= len(cal):
                        continue
                    bd, sd = cal[i + 1], cal[i + 5]
                    if bd not in df.index:
                        continue
                    b = df.loc[bd]
                    if b.high == b.low and b.open == b.high and b.pct >= lim(code):
                        continue  # 一字涨停买不进
                    px_in = float(b.open)
                # 卖出（V3 含跌停顺延）
                while sd is not None:
                    if sd not in df.index:
                        k = pos[sd] + 1
                        sd = cal[k] if k < len(cal) else None
                        continue
                    s = df.loc[sd]
                    if mode == "V3" and s.close == s.low and s.pct <= -lim(code):
                        k = pos[sd] + 1
                        sd = cal[k] if k < len(cal) else None
                        continue
                    out.append((T, code, sd, s.close / px_in - 1 - COST))
                    break
        return out

    def last_dow(days_):
        """每周最后交易日"""
        seen, out, last = set(), [], None
        for d in days_:
            wk = d.isocalendar()[:2]
            if wk not in seen and last is not None:
                out.append(last)
            seen.add(wk)
            last = d
        out.append(last)
        return out

    for mode in ("V1", "V2", "V3"):
        recs = collect(mode)
        d = pd.DataFrame(recs, columns=["T", "code", "sell", "ret"])
        done = d[d.sell <= cal[-1]]
        r = done.ret
        wk = done.assign(wk=pd.to_datetime(done.sell).dt.to_period("W")).groupby("wk").ret.mean()
        # 市场同周基线（全市场等权 T+5，同周）
        print(f"[{mode}] 笔数 {len(done)} | 中位 {r.median():+.2%} 均值 {r.mean():+.2%} 胜率 {(r>0).mean():.0%} "
              f"| 周数 {len(wk)} 周均值中位 {wk.median():+.2%} 正周 {(wk>0).mean():.0%}")
        d.to_csv(f"sim/ladder_{mode}_detail.csv", index=False)


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)
