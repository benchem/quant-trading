#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R1 数据质量审计——全市场重算超跌 G2 信号，逐信号打标签。

产出:
  deepscan/20261105_oversold_g2/r1_signals_full.csv   全部 base 信号(不预过滤宇宙)
  deepscan/20261105_oversold_g2/r1_stock_meta.csv     每股元数据(末bar/脏bar数/一字事件)
  deepscan/20261105_oversold_g2/r1_mkt_baseline.csv   全宇宙周基线
标签: mv_then(robust 当时流通市值) / mv_today / ST今天 / 板块 / G2两版窗口 /
      脏amount bar 落点 / OHLC越界 bar / 5cm一字(漏剔区) / 20cm伪一字区
"""
import os
import re
import sys

import numpy as np
import pandas as pd

DATA = "/opt/quant-trading/data"
DAILY = os.path.join(DATA, "daily", "hfq")
BASIC = os.path.join(DATA, "dataset", "stock_basic.parquet")
COST = 0.0068
OUT = "/root/workspaces/quant-trading/deepscan/20261105_oversold_g2"

basic = pd.read_parquet(BASIC)
basic["is_st"] = basic["name"].astype(str).str.contains("ST", na=False)
basic["is_tui"] = basic["name"].astype(str).str.contains("退", na=False)
basic["mv_today_f"] = basic["float_mv"] / 1e8
basic["mv_today_t"] = basic["total_mv"] / 1e8
basic = basic.set_index("code")

pat = re.compile(r"^\d{6}\.(SH|SZ)\.parquet$")
files = sorted(f for f in os.listdir(DAILY) if pat.match(f))
print(f"标准文件 {len(files)}", file=sys.stderr)

# ---- 周信号日(与 probe_pullback 完全同算法) ----
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
print(f"周信号日 {len(weekly)}: {weekly[0].date()} ~ {weekly[-1].date()}", file=sys.stderr)

# ---- 脏票清单(2026-11-04 审计) ----
dirty_codes, dirty_dates = set(), {}
with open("/root/workspaces/quant-trading/deepscan/20261104_t5_3stocks/data_audit_after_fix.txt") as fh:
    for line in fh:
        m = re.match(r"^(\d{6}\.(?:SH|SZ))\t(.+)$", line.strip())
        if m:
            dirty_codes.add(m.group(1))
            ds = re.findall(r"amt跳变@(\d{4}-\d{2}-\d{2})", m.group(2))
            dirty_dates[m.group(1)] = ds
print(f"脏票清单 {len(dirty_codes)} 只", file=sys.stderr)


def board_of(code):
    p = code[:3]
    if p in ("300", "301", "302"):
        return "cyb20"
    if p in ("688", "689"):
        return "kcb20"
    return "main10"


LIMIT = {"main10": 0.10, "cyb20": 0.20, "kcb20": 0.20}

sig_rows, meta_rows, mkt_rows = [], [], []
W0 = weekly[0]

for fi, f in enumerate(files):
    code = f.replace(".parquet", "")
    bd = board_of(code)
    lim = LIMIT[bd]
    try:
        df = pd.read_parquet(os.path.join(DAILY, f),
                             columns=["date", "open", "high", "low", "close",
                                      "volume", "amount", "turnover"])
    except Exception:
        continue
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    n = len(df)
    if n < 70:
        continue
    c = df["close"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    o = df["open"].to_numpy(float)
    am = df["amount"].to_numpy(float)
    to = df["turnover"].to_numpy(float)
    dmap = {d: i for i, d in enumerate(df["date"])}

    pct = np.full(n, np.nan)
    pct[1:] = c[1:] / c[:-1] - 1

    # ---- 当时流通市值(robust): mv = 100*amount/turnover, 坏bar用邻域中位修 ----
    with np.errstate(divide="ignore", invalid="ignore"):
        mv_raw = np.where((to > 0) & (am > 0), 100.0 * am / to, np.nan)
    mv_s = pd.Series(mv_raw)
    roll = mv_s.rolling(11, center=True, min_periods=3).median()
    dev = (mv_s / roll - 1).abs()
    dirty_amt = (mv_s.isna() & (am > 0)) | (dev > 0.25) | (am <= 0) | np.isnan(am)
    # amount=0 且 volume=0 = 停牌bar, 不算脏; amount坏但volume也坏算脏
    dirty_amt = dirty_amt & ~((am <= 0) & (df["volume"].to_numpy(float) <= 0))
    mv_clean = np.where(dirty_amt, roll.to_numpy(), mv_raw)

    # ---- 一字事件标签(全序列, 供窗口判定) ----
    eq = l == h
    ow_095 = eq & (pct <= -0.095)                 # 现行阈值
    ow_095_off = np.zeros(n, bool)                # 扫描器20bar窗口另行累计
    ow_195 = eq & (pct <= -0.195)                 # 20cm 板正确阈值
    ow_st5 = eq & (pct > -0.095) & (pct <= -0.045)  # 5cm一字(现行漏剔区)
    ow_fake20 = eq & (pct <= -0.095) & (pct > -0.195)  # 20cm板上会被误判成一字跌停的区段

    cum095 = np.cumsum(ow_095)
    cum195 = np.cumsum(ow_195)
    cumst5 = np.cumsum(ow_st5)
    cumdirty = np.cumsum(dirty_amt.to_numpy() if hasattr(dirty_amt, "to_numpy") else dirty_amt)
    # OHLC 越界(上市5日后, 板块涨跌幅+0.7pp 容差)
    bad_ohlc = np.zeros(n, bool)
    bad_ohlc[5:] = np.abs(pct[5:]) > lim + 0.007
    cumohlc = np.cumsum(bad_ohlc)

    def winsum(cum, a, b):
        s = cum[b]
        if a > 0:
            s = s - cum[a - 1]
        return int(s)

    in_basic = code in basic.index
    bi = basic.loc[code] if in_basic else None
    mv_today_f = float(bi["mv_today_f"]) if in_basic else np.nan
    mv_today_t = float(bi["mv_today_t"]) if in_basic else np.nan
    is_st = bool(bi["is_st"]) if in_basic else None
    is_tui = bool(bi["is_tui"]) if in_basic else None

    # 窗口脏统计: 回看20~信号~持有10
    w_start = min(dmap.get(W0, 0), 0) if False else 0
    idx_w0 = dmap.get(weekly[0])
    if idx_w0 is None:
        first_cover = n  # 数据不覆盖窗口起点
    else:
        first_cover = max(0, idx_w0 - 20)

    meta_rows.append(dict(
        code=code, board=bd, bars=n, in_basic=in_basic,
        last_bar=str(df["date"].iloc[-1].date()),
        mv_today_f=mv_today_f, mv_today_t=mv_today_t, is_st=is_st, is_tui=is_tui,
        dirty_amt_n=int(np.nansum(dirty_amt.to_numpy() if hasattr(dirty_amt, 'to_numpy') else dirty_amt)),
        dirty_in_window=int(winsum(cumdirty, first_cover, n - 1)) if first_cover <= n - 1 else 0,
        bad_ohlc_n=int(bad_ohlc.sum()),
        ow095_n=int(ow_095.sum()), ow195_n=int(ow_195.sum()),
        ow_st5_n=int(ow_st5.sum()), ow_fake20_n=int(ow_fake20.sum()),
        in_audit_dirty=code in dirty_codes,
    ))

    for sd in weekly:
        i = dmap.get(sd)
        if i is None or i < 60 or i + 10 >= n:
            continue
        if not np.isfinite(c[i]) or c[i] <= 0:
            continue
        ret20 = c[i] / c[i - 20] - 1
        rng = h[i] - l[i]
        cpos = (c[i] - l[i]) / rng if rng > 0 else 0.0
        # 市场基线(全宇宙, 与官方同条件)
        mkt_rows.append(dict(code=code, wk=str(sd.date()),
                             t10=c[i + 10] / c[i] - 1 - COST))
        if ret20 > -0.20 or cpos < 0.5:
            continue
        a = max(0, i - 20)
        g2_21 = winsum(cum095, a, i) == 0        # probe_pullback: 21bar窗口
        g2_20 = winsum(cum095, max(0, i - 19), i) == 0  # 扫描器: 20bar窗口
        g2_board = winsum(cum195 if bd != "main10" else cum095, a, i) == 0  # 板块正确阈值
        st5_in_win = winsum(cumst5, a, i)
        fake20_in_win = winsum(np.cumsum(ow_fake20), a, i) if bd != "main10" else 0
        ow_all_pct = pct[a:i + 1][ow_095[a:i + 1]] if ow_095[a:i + 1].any() else []
        mv_then = mv_clean[i]
        sig_rows.append(dict(
            code=code, wk=str(sd.date()), board=bd, in_basic=in_basic,
            t10=c[i + 10] / c[i] - 1 - COST,
            t5=c[i + 5] / c[i] - 1 - COST,
            exec_t10=(c[i + 10] / o[i + 1] - 1 - COST) if o[i + 1] > 0 else np.nan,
            ret20=ret20, cpos=cpos, rng0=bool(rng == 0),
            g2_21=g2_21, g2_20=g2_20, g2_board=g2_board,
            st5_in_win=int(st5_in_win), fake20_in_win=int(fake20_in_win),
            ow_events=("|".join(f"{p:+.1%}" for p in ow_all_pct)),
            mv_then=mv_then, mv_today_f=mv_today_f, mv_today_t=mv_today_t,
            is_st=is_st, is_tui=is_tui,
            dirty_at_sig=bool(dirty_amt.iloc[i]) if hasattr(dirty_amt, "iloc") else bool(dirty_amt[i]),
            dirty_in_g2win=int(winsum(cumdirty, a, i)),
            dirty_in_hold=int(winsum(cumdirty, i + 1, i + 10)),
            bad_ohlc_in_win=int(winsum(cumohlc, a, i + 10)),
        ))
    if (fi + 1) % 1000 == 0:
        print(f"  ...{fi+1}/{len(files)}", file=sys.stderr)

sig = pd.DataFrame(sig_rows)
meta = pd.DataFrame(meta_rows)
mkt = pd.DataFrame(mkt_rows)
sig.to_csv(os.path.join(OUT, "r1_signals_full.csv"), index=False)
meta.to_csv(os.path.join(OUT, "r1_stock_meta.csv"), index=False)
mkt.to_csv(os.path.join(OUT, "r1_mkt_baseline.csv"), index=False)
print(f"base信号 {len(sig)} | 股 {len(meta)} | 基线 {len(mkt)}", file=sys.stderr)
print("done")
