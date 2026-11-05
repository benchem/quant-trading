#!/usr/bin/env python3
"""突破前夜扫描器 v2 — 找"大涨前夜"而非"大涨之后"（单遍历回测版）。

背景（对现行漏斗的结构性修正）：
  现行扫描器选"刚大涨完"的票 → 入场参照价在高位 → 好入场=等回踩 → 自动选到弱样本。
  本漏斗在【缩量平台末端】选票：
    P1 平台：近 21 日收盘振幅 <12%（rng<0.12）
    P2 贴沿：末端 5 日收盘均值 >= 窗口 60 分位（蓄势贴箱顶）
    P3 缩量：近 5 日均量 <= 1.2 × 前 15 日均量
    P4 未爆：今日量比 ∈[1.0, 2.0]（>2 的已被现行漏斗漏走）
    P5 站稳：收盘 > MA20；距箱体顶 ∈[0, 3%]
    P6 防火：距 MA60 > -8%（排除下跌中继平台）
    P7 题材（v2 新增）：近 20 日窗口内出现过量比>=3 放量日 >=1 次（有资金活动迹象，
       排除死水防御股——水务/高速/燃气类纯低波动票）
  基本面壳：流通市值 30~300 亿、非 ST。

回测口径（镜像）：
  信号日 T 收盘买入 → 持有 N=1/2/3/5 日收盘卖出，扣双边成本 0.68%。
  对照组 = 现行漏斗近似（pct>=5% + 量比>=2 + 收盘位置>=0.70）同窗镜像。
  每周取一个信号日（该周最后一个交易日），防同事件重复计样。

口径铁律：沿用 scan_ultrashort.py — 自算 pct、hfq 同股内比值有效。
"""
import argparse
import glob
import json
import os
import sys

import pandas as pd
import numpy as np

DATA = "/opt/quant-trading/data"
DAILY = os.path.join(DATA, "daily", "hfq")
BASIC = os.path.join(DATA, "dataset", "stock_basic.parquet")
COST = 0.0068


def load_basic():
    basic = pd.read_parquet(BASIC)[["code", "name", "float_mv"]]
    basic["float_mv_yi"] = basic["float_mv"] / 1e8
    keep = (basic["float_mv_yi"].between(30, 300)
            & ~basic["name"].astype(str).str.contains("ST|退", na=False))
    return set(basic.loc[keep, "code"])


def prebreak_signal(df, i):
    """df 已按 date 升序；i = 信号日下标。返回 dict 或 None。"""
    if i < 60:
        return None
    closes = df["close"].values
    vols = df["volume"].values
    win = closes[i - 20:i + 1]  # 21 日含今日
    today = closes[i]
    prev = closes[i - 1]
    if prev <= 0 or today <= 0:
        return None
    rng = win.max() / win.min() - 1.0
    if rng >= 0.12:
        return None
    tail5 = win[-5:].mean()
    if tail5 < np.quantile(win, 0.60):
        return None
    recent_v = vols[i - 4:i + 1].mean()
    base_v = vols[i - 19:i - 4].mean()
    if base_v <= 0 or recent_v > 1.2 * base_v:
        return None
    prev5v = vols[i - 5:i].mean()
    if prev5v <= 0:
        return None
    vol_ratio = vols[i] / prev5v
    if not (1.0 <= vol_ratio <= 2.0):
        return None
    ma20 = closes[i - 20:i].mean()
    if today <= ma20:
        return None
    box_top = win[:-1].max()
    room_top = box_top / today - 1.0
    if room_top > 0.03 or room_top < 0:
        return None
    ma60 = closes[i - 59:i + 1].mean()
    if today / ma60 - 1.0 < -0.08:
        return None
    # P7 题材：近 20 日（不含今日）出现过量比>=3 的放量日
    # P7b 方向：该放量日须收在当日区间上部 >=0.6（吸筹型；派发型放量平台=出货中继）
    highs = df["high"].values if "high" in df.columns else None
    lows = df["low"].values if "low" in df.columns else None
    hot = 0
    for j in range(max(1, i - 19), i):
        pv = vols[j - 5:j].mean()
        if pv > 0 and vols[j] / pv >= 3.0:
            if highs is not None and lows is not None:
                rngj = highs[j] - lows[j]
                if rngj <= 0 or (closes[j] - lows[j]) / rngj < 0.6:
                    continue  # 放量但收在下部=派发,不计
            hot += 1
    if hot < 1:
        return None
    return dict(rng=rng, room_top=room_top, vr=vol_ratio, hot=hot)


def chase_signal(df, i):
    """对照组：现行漏斗近似。"""
    if i < 60:
        return None
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    vols = df["volume"].values
    c, h, l, prev = closes[i], highs[i], lows[i], closes[i - 1]
    if prev <= 0 or h <= l:
        return None
    if c / prev - 1.0 < 0.05:
        return None
    prev5v = vols[i - 5:i].mean()
    if prev5v <= 0 or vols[i] / prev5v < 2.0:
        return None
    if (c - l) / (h - l) < 0.70:
        return None
    return dict(chase=1)


def forward_ret(df, i, hold):
    if i + hold >= len(df):
        return None  # 窗口截断
    buy = df["close"].values[i]
    sell = df["close"].values[i + hold]
    if buy <= 0:
        return None
    return sell / buy - 1.0 - COST


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="单日扫描基准日")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--weeks", type=int, default=52)
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(DAILY, "*.parquet")))
    universe = load_basic()

    if not args.backtest:
        base_date = pd.Timestamp(args.date) if args.date else pd.Timestamp("2026-11-04")
        rows = []
        for f in files:
            code = os.path.basename(f).replace(".parquet", "")
            if code not in universe:
                continue
            try:
                df = pd.read_parquet(f, columns=["date", "close", "high", "low", "volume"])
            except Exception:
                continue
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
            idx = df.index[df["date"] == base_date]
            if len(idx) == 0:
                continue
            sig = prebreak_signal(df, int(idx[0]))
            if sig:
                rows.append(dict(code=code, **sig))
        print(f"突破前夜候选（基准 {base_date.date()}）: {len(rows)} 只")
        if rows:
            show = pd.DataFrame(rows).sort_values("room_top").head(args.top)
            print(show.to_string(index=False))
        return

    # ---------- 回测：单遍历 ----------
    # 周信号日：从全部文件日期并集取每周最后交易日
    dates = pd.concat([pd.read_parquet(f, columns=["date"])["date"] for f in files[:300]])
    trade_dates = sorted(set(pd.to_datetime(dates)))
    weekly, seen = [], None
    for dt in reversed(trade_dates):
        wk = dt.isocalendar()[:2]
        if wk != seen:
            weekly.append(dt)
            seen = wk
    weekly = sorted(weekly)[-args.weeks:]
    sig_set = set(weekly)
    print(f"回测：{len(weekly)} 个周信号日（{weekly[0].date()} ~ {weekly[-1].date()}）,"
          f"镜像口径扣双边成本 {COST:.2%},universe={len(universe)}")

    sig_idx = {}  # date -> position in weekly
    for k, d in enumerate(weekly):
        sig_idx[d] = k

    pre_hits = [[] for _ in weekly]   # 每周: list[(code, hold->ret)]
    chase_hits = [[] for _ in weekly]
    pre_recs, chase_recs = [], []
    n_files = 0
    for f in files:
        code = os.path.basename(f).replace(".parquet", "")
        if code not in universe:
            continue
        n_files += 1
        try:
            df = pd.read_parquet(f, columns=["date", "close", "high", "low", "volume"])
        except Exception:
            continue
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        dmap = {d: i for i, d in enumerate(df["date"])}
        for k, sig_date in enumerate(weekly):
            i = dmap.get(sig_date)
            if i is None:
                continue
            if prebreak_signal(df, i):
                rets = {}
                for hold in (1, 2, 3, 5):
                    r = forward_ret(df, i, hold)
                    if r is not None:
                        rets[hold] = r
                pre_recs.append(dict(week=k, code=code, **{f"t{h}": v for h, v in rets.items()}))
            if chase_signal(df, i):
                rets = {}
                for hold in (1, 2, 3, 5):
                    r = forward_ret(df, i, hold)
                    if r is not None:
                        rets[hold] = r
                chase_recs.append(dict(week=k, code=code, **{f"t{h}": v for h, v in rets.items()}))

    def summarize(recs, label):
        print(f"\n—— {label} ——")
        out = {}
        if not recs:
            print("  无样本")
            return out
        dfr = pd.DataFrame(recs)
        for hold in (1, 2, 3, 5):
            col = f"t{hold}"
            s = dfr[col].dropna()
            if s.empty:
                continue
            st = dict(n=int(s.size), win=float((s > 0).mean()), med=float(s.median()),
                      mean=float(s.mean()), q25=float(s.quantile(0.25)), q75=float(s.quantile(0.75)),
                      worst=float(s.min()), best=float(s.max()))
            out[f"T+{hold}"] = st
            print(f"T+{hold}: N={st['n']:4d}  胜率 {st['win']:.1%}  中位 {st['med']:+.2%}  "
                  f"均值 {st['mean']:+.2%}  [Q25 {st['q25']:+.2%}~Q75 {st['q75']:+.2%}]  "
                  f"最差 {st['worst']:+.1%}  最好 {st['best']:+.1%}")
        # 每周命中数分布
        wk_count = dfr.groupby("week").size()
        print(f"每周命中: 中位 {wk_count.median():.0f} 只, 范围 {wk_count.min()}~{wk_count.max()}, "
              f"零命中周 {(wk_count == 0).sum()}/{len(weekly)}")
        return out

    print(f"\n遍历文件 {n_files}")
    stats_pre = summarize(pre_recs, "突破前夜（本漏斗）")
    stats_chase = summarize(chase_recs, "对照组：现行漏斗镜像（大涨后追）")

    out = dict(weeks=len(weekly), window=[str(weekly[0].date()), str(weekly[-1].date())],
               cost=COST, prebreak=stats_pre, chase=stats_chase)
    fn = f"prebreak_backtest_{weekly[0].strftime('%Y%m%d')}_{weekly[-1].strftime('%Y%m%d')}.json"
    with open(fn, "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    # 明细存档（CSV）
    if pre_recs:
        pd.DataFrame(pre_recs).to_csv("prebreak_backtest_detail.csv", index=False)
    if chase_recs:
        pd.DataFrame(chase_recs).to_csv("chase_backtest_detail.csv", index=False)
    print(f"\n存档: {fn} + 明细 CSV ×2")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)
