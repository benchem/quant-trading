#!/usr/bin/env python3
"""超跌回归 G2 版每日选股器（2026-11-05 定稿口径）。

策略口径（52 周回测 T+10 中位 +0.24%/胜率 51.3%/N=1893，唯二先验迭代后冻结）：
  S1 20 日跌幅 <= -20%
  S2 当日收盘位置 >= 0.5（收盘在当日区间上半）
  S3 近 20 日无一字跌停（low==high 且 pct<=-9.5%）——G2 闸，剔除左尾 -5.32% 群体
  S4 非 ST、流通市值 30~300 亿
持有：T+10 收盘卖出（回测口径）；止损无（回测未含止损，实盘灾难止损为未测参数）。

执行口径校验（本脚本附带）：回测为信号日收盘买入，实盘最早次日开盘成交——
此处对 52 周信号集重算 "次日开盘买入 → T+10 收盘卖出" 口径，量化隔夜 gap 损耗。
"""
import glob
import json
import os

import pandas as pd

DATA = "/opt/quant-trading/data"
DAILY = os.path.join(DATA, "daily", "hfq")
BASIC = os.path.join(DATA, "dataset", "stock_basic.parquet")
COST = 0.0068


def load_basic():
    b = pd.read_parquet(BASIC)[["code", "name", "float_mv"]]
    b["mv"] = b["float_mv"] / 1e8
    keep = b["mv"].between(30, 300) & ~b["name"].astype(str).str.contains("ST|退", na=False)
    return b[keep].set_index("code")


def has_one_word_limit_down(l, h, c, i, days=20):
    for k in range(max(0, i - days + 1), i + 1):
        if k == 0:
            continue
        if l[k] == h[k] and c[k] / c[k - 1] - 1 <= -0.095:
            return True
    return False


def main():
    uni = load_basic()
    files = sorted(glob.glob(os.path.join(DAILY, "*.parquet")))
    files = [f for f in files if os.path.basename(f).replace(".parquet", "") in uni.index]

    # —— 52 周执行口径校验（次日开盘买入版）——
    dates = pd.concat([pd.read_parquet(f, columns=["date"])["date"] for f in files[:300]])
    tds = sorted(set(pd.to_datetime(dates)))
    weekly, seen = [], None
    for dt in reversed(tds):
        wk = dt.isocalendar()[:2]
        if wk != seen:
            weekly.append(dt)
            seen = wk
    weekly = sorted(weekly)[-52:]

    exec_rets, close_rets = [], []
    today_sigs = []

    for f in files:
        code = os.path.basename(f).replace(".parquet", "")
        try:
            df = pd.read_parquet(f, columns=["date", "close", "open", "high", "low", "volume", "amount"])
        except Exception:
            continue
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        c = df["close"].values
        o = df["open"].values
        h = df["high"].values
        l = df["low"].values
        n = len(c)
        dmap = {d: i for i, d in enumerate(df["date"])}

        def signal(i):
            if i < 60:
                return None
            ret20 = c[i] / c[i - 20] - 1
            rng = h[i] - l[i]
            cpos = (c[i] - l[i]) / rng if rng > 0 else 0.0
            if ret20 > -0.20 or cpos < 0.5:
                return None
            if has_one_word_limit_down(l, h, c, i):
                return None
            return ret20, cpos

        # 回测校验集
        for sd in weekly:
            i = dmap.get(sd)
            if i is None or i + 11 >= n:
                continue
            s = signal(i)
            if s is None or c[i] <= 0:
                continue
            close_rets.append(c[i + 10] / c[i] - 1 - COST)
            if o[i + 1] > 0:
                exec_rets.append(c[i + 10] / o[i + 1] - 1 - COST)

        # 今日信号（最后一根 bar = 2026-11-05）
        i = n - 1
        s = signal(i)
        if s and c[i] > 0:
            ret20, cpos = s
            today_sigs.append(dict(
                code=code, name=uni.loc[code, "name"],
                close=c[i], pct=c[i] / c[i - 1] - 1, ret20=ret20, cpos=cpos,
                amount_yi=df["amount"].iloc[i] / 1e8 if pd.notna(df["amount"].iloc[i]) else None,
                mv_yi=uni.loc[code, "mv"],
                bars=n,
            ))

    d1 = pd.Series(close_rets)
    d2 = pd.Series(exec_rets)
    print("== 执行口径校验（52 周信号集）==")
    print(f"信号日收盘买入→T+10收盘: N={len(d1)} 中位 {d1.median():+.2%} 均值 {d1.mean():+.2%} 胜率 {(d1 > 0).mean():.1%}")
    print(f"次日开盘买入→T+10收盘: N={len(d2)} 中位 {d2.median():+.2%} 均值 {d2.mean():+.2%} 胜率 {(d2 > 0).mean():.1%}")
    print(f"隔夜gap损耗(中位): {(d2.median() - d1.median()):+.2%}")

    d = pd.DataFrame(today_sigs)
    if len(d):
        d = d.sort_values("ret20")
        print(f"\n== 今日（2026-11-05）超跌回归 G2 信号: {len(d)} 只 ==")
        cols = ["code", "name", "close", "pct", "ret20", "cpos", "amount_yi", "mv_yi"]
        print(d[cols].to_string(index=False, float_format=lambda x: f"{x:.3f}"))
        d.to_csv("/root/workspaces/quant-trading/scans/oversold_g2_candidates_20261105.csv", index=False)
        print("\n明细: scans/oversold_g2_candidates_20261105.csv")
    else:
        print("\n今日无信号")


if __name__ == "__main__":
    main()
