#!/usr/bin/env python3
"""超短线候选扫描器 — 基准日默认 2026-11-02，支持 --date 重跑。

口径铁律：
- 今日涨跌幅自算 pct = close/prev_close - 1（禁止用文件内 pct_chg / stock_basic.pct_chg）
- hfq 后复权价：跨股绝对价不可比，同股内均线/比值有效
- 基准日 = 每只股票文件内最大日期（应等于 --date）
"""
import argparse
import glob
import os
import sys

import pandas as pd

DATA = "/opt/quant-trading/data"
DAILY = os.path.join(DATA, "daily", "hfq")
BASIC = os.path.join(DATA, "dataset", "stock_basic.parquet")
BOARD = os.path.join(DATA, "board_members.parquet")

EXCLUDE_CODES = {"300687.SZ", "300199.SZ"}
TOP_N = 10


def log(step, msg):
    print(f"[{step}] {msg}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-11-02")
    args = ap.parse_args()
    base_date = pd.Timestamp(args.date)

    basic = pd.read_parquet(BASIC)[["code", "name", "float_mv", "total_mv", "latest_price"]]
    boards = pd.read_parquet(BOARD)
    ind = boards[boards["board_type"] == "industry"][["code", "board_code", "board_name"]]
    ind_map = dict(zip(ind["code"], zip(ind["board_code"], ind["board_name"])))

    files = sorted(glob.glob(os.path.join(DAILY, "*.parquet")))
    log(0, f"daily/hfq 文件总数 = {len(files)}")

    rows = {}
    n_with_bar = 0
    for f in files:
        code = os.path.basename(f).replace(".parquet", "")
        df = pd.read_parquet(f, columns=["date", "close", "high", "low", "volume", "amount"])
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        if df.empty or df["date"].iloc[-1] != base_date:
            continue  # 停牌或无基准日 bar
        n_with_bar += 1
        hist = df.iloc[:-1]  # 基准日之前
        if hist.empty:
            continue
        prev_close = float(hist["close"].iloc[-1])
        if prev_close <= 0:
            continue
        c = float(df["close"].iloc[-1])
        h = float(df["high"].iloc[-1])
        l = float(df["low"].iloc[-1])
        pct = c / prev_close - 1.0

        closes = df["close"].astype(float)
        ma20 = closes.tail(20).mean()
        ma60 = closes.tail(60).mean()
        ma250 = closes.tail(250).mean() if len(closes) >= 250 else None

        # 量比：基准日 volume / 前5个交易日 volume 均值（可得则用）
        prev5 = df["volume"].iloc[-6:-1]
        vol_ratio = float(df["volume"].iloc[-1]) / prev5.mean() if len(prev5) and prev5.mean() > 0 else None

        rows[code] = dict(
            prev_close=prev_close, close=c, high=h, low=l,
            amount=float(df["amount"].iloc[-1]), volume=float(df["volume"].iloc[-1]),
            pct=pct, ma20=ma20, ma60=ma60, ma250=ma250, vol_ratio=vol_ratio,
            close60_max=float(closes.tail(60).max()),
            close20_first=float(closes.iloc[max(0, len(closes) - 21)]),  # 20日前收盘(不含今日的起点)
        )

    log(0, f"含 {base_date.date()} bar 的 = {n_with_bar}")

    dfb = pd.DataFrame.from_dict(rows, orient="index").rename_axis("code").reset_index()
    dfb = dfb.merge(basic, on="code", how="left")

    # 行业平均自算 pct（全市场口径）
    dfb["board_code"] = dfb["code"].map(lambda x: ind_map.get(x, (None, None))[0])
    dfb["board_name"] = dfb["code"].map(lambda x: ind_map.get(x, (None, None))[1])
    board_avg = dfb.groupby("board_code")["pct"].mean()

    n = len(dfb)
    steps = []

    def remaining(tag):
        steps.append(f"{tag}: 剩余 {len(dfb)}")

    # 1. ST/退市/停牌（停牌已在读取时排除）
    mask = ~dfb["name"].astype(str).str.contains("ST|退", na=False)
    dfb = dfb[mask]
    remaining("1.剔除ST/退市/停牌")

    # 2. 指定剔除
    dfb = dfb[~dfb["code"].isin(EXCLUDE_CODES)]
    remaining("2.剔除指定代码")

    # 3. 流通市值 30~300 亿
    dfb["float_mv_yi"] = dfb["float_mv"] / 1e8
    dfb = dfb[(dfb["float_mv_yi"] >= 30) & (dfb["float_mv_yi"] <= 300)]
    remaining("3.流通市值30~300亿")

    # 4. 成交额 >= 2亿
    # 注：hfq 文件 amount 列在基准日几乎全为 NaN（5223 中仅 3 只有值），
    # 校准（000028.SZ: amount 9946.5万 / volume 46639手×100 = 21.3元/股）确认
    # volume 单位为手。成交额以 volume×100×latest_price(原始价) 估算。
    dfb["amount_yi"] = dfb["volume"] * 100.0 * dfb["latest_price"] / 1e8
    dfb = dfb[dfb["amount_yi"] >= 2.0]
    remaining("4.成交额>=2亿")

    # 5. 自算 pct >= 5%
    dfb = dfb[dfb["pct"] >= 0.05]
    remaining("5.今日pct>=+5%")

    # 6. 量比 >= 2.0
    dfb = dfb[dfb["vol_ratio"].notna() & (dfb["vol_ratio"] >= 2.0)]
    remaining("6.量比>=2.0")

    # 7. 收盘位置 >= 0.70 且非一字板
    rng = dfb["high"] - dfb["low"]
    dfb["close_pos"] = (dfb["close"] - dfb["low"]) / rng.replace(0, pd.NA)
    dfb = dfb[
        dfb["close_pos"].notna() & (dfb["close_pos"] >= 0.70)
        & (dfb["high"] > dfb["low"]) & ((dfb["high"] - dfb["low"]) / dfb["prev_close"] >= 0.03)
    ]
    remaining("7.收盘位置>=0.70且非一字板")

    # 8. 结构突破
    def breakthrough(r):
        if r["ma250"] is not None and pd.notna(r["ma250"]):
            if r["prev_close"] <= r["ma250"] and r["close"] > r["ma250"]:
                return "8a"
        if r["close"] >= r["close60_max"]:
            return "8b"
        return None

    dfb["signal"] = dfb.apply(breakthrough, axis=1)
    dfb = dfb[dfb["signal"].notna()]
    remaining("8.结构突破(8a/8b)")

    # 9. 距60日最高收盘 >= 5% 空间（8b 触发者空间为0，自然剔除）
    dfb["room60"] = dfb["close60_max"] / dfb["close"] - 1.0
    dfb = dfb[dfb["room60"] >= 0.05]
    remaining("9.距60日高点>=5%")

    # 10. 近20日累计涨幅（含今日） <= +30%
    dfb["pct20"] = dfb["close"] / dfb["close20_first"] - 1.0
    dfb = dfb[dfb["pct20"] <= 0.30]
    remaining("10.近20日涨幅<=+30%")

    # 11. 板块效应
    dfb["board_avg_pct"] = dfb["board_code"].map(board_avg)
    has_ind = dfb["board_code"].notna()
    dfb = dfb[~has_ind | (dfb["board_avg_pct"] >= 0.015)]
    remaining("11.板块平均涨幅>=+1.5%(无行业保留)")

    # 排序输出
    dfb["board_name"] = dfb["board_name"].fillna("无行业")
    dfb["score"] = dfb["vol_ratio"] * dfb["close_pos"] * dfb["board_avg_pct"].fillna(0)
    dfb = dfb.sort_values("score", ascending=False)

    out_cols = ["code", "name", "board_name", "pct", "vol_ratio", "close_pos",
                "room60", "pct20", "float_mv_yi", "amount_yi", "signal", "board_avg_pct", "score"]
    top = dfb.head(TOP_N)[out_cols].copy()
    for c, nd in [("pct", 4), ("vol_ratio", 2), ("close_pos", 2), ("room60", 4),
                  ("pct20", 4), ("float_mv_yi", 1), ("amount_yi", 2), ("board_avg_pct", 4), ("score", 4)]:
        top[c] = top[c].astype(float).round(nd)
    top = top.rename(columns={
        "board_name": "行业", "pct": "今pct", "vol_ratio": "量比", "close_pos": "收盘位置",
        "room60": "距60日高%", "pct20": "20日涨幅%", "float_mv_yi": "流通市值亿",
        "amount_yi": "成交额亿", "signal": "突破类型", "board_avg_pct": "板块均涨幅", "score": "综合分"})

    csv_path = f"scan_ultrashort_{base_date.strftime('%Y%m%d')}.csv"
    md_path = f"scan_ultrashort_{base_date.strftime('%Y%m%d')}.md"
    all_cols = ["code", "name", "行业", "今pct", "量比", "收盘位置", "距60日高%", "20日涨幅%",
                "流通市值亿", "成交额亿", "突破类型", "板块均涨幅", "综合分"]
    top.to_csv(csv_path, index=False, encoding="utf-8-sig")

    lines = [f"# 超短线候选扫描报告（基准日 {base_date.date()}，买入 2026-11-03，11-06 前卖出）", "",
             f"最终候选 {len(dfb)} 只，按综合分 = 量比 × 收盘位置 × 板块平均涨幅 取 Top {min(TOP_N, len(top))}。",
             "",
             "> 注：hfq 日线 amount 列在基准日几乎全空（5223 只中仅 3 只有值），成交额列为",
             "> `volume×100×latest_price` 估算值（volume 单位=手，已用 3 只有真实 amount 的文件校准）。",
             ""]
    if len(top):
        lines.append(top.to_markdown(index=False))
    else:
        lines.append("（无候选）")
    lines += ["", "## 过滤漏斗", ""] + [f"- {s}" for s in steps] + [""]
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    print("\n" + "\n".join(steps))
    print()
    if len(top):
        print(top.to_string(index=False))
    print(f"\n输出: {csv_path}, {md_path}")


if __name__ == "__main__":
    main()
    import os
    os._exit(0)  # 跳过 pyarrow/pandas 关闭阶段（本环境解释器退出时会 core dump）
