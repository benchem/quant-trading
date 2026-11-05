#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟交易推演：追高漏斗（scan_ultrashort 现行 11 层口径）× 2026-06-01 起 walk-forward

用户指令（2026-11-05 晚）：用 2026年6月开始的 T-1 数据按现行模型选股，用 T+5 数据看结果，
推演过程股票放"模拟交易"台账供人工检查。

口径铁律（本脚本遵守的）：
- T-1 无前视：信号日 T 只用 ≤T 的 bar；市值过滤用当时流通市值 = amount×100/turnover（非今日值）
- 信号日 T 收盘判定 → T+1 开盘买入 → T+5（信号日后第5个bar）收盘卖出
- 执行约束：T+1 一字涨停买不进（跳过）；T+5 跌停锁死顺延至下一可卖 bar；停牌顺延
- 成本：双边 0.68%（沿用回测口径，保守——执行官实测真实摩擦 0.17~0.30%）
- 诚实标注：ST 过滤用当前名单（无历史 ST 数据）；行业归属用当前 board_members（近似）
- 2026-06~11 在 52 周回测窗口内：这是"规则冻结后的机械复算"，不是严格样本外

双口径：
- A 统计口径：每日全部信号等权（每笔 1 万元名义）
- B 资金口径：40,000 元 4 槽 FIFO（信号日预占、T+5 卖出释放、买不进释放）
"""
import glob
import os
import sys

import numpy as np
import pandas as pd

DATA = "/opt/quant-trading/data"
DAILY = os.path.join(DATA, "daily", "hfq")
BASIC = os.path.join(DATA, "dataset", "stock_basic.parquet")
BOARD = os.path.join(DATA, "board_members.parquet")
OUT_DIR = "/root/workspaces/quant-trading/sim"
START = pd.Timestamp("2026-06-01")
END_HINT = pd.Timestamp("2026-11-05")
COST = 0.0068
EXCLUDE = {"300687.SZ", "300199.SZ"}  # 现行规则：持仓股禁入漏斗
SLOT_CAP = 4          # 口径B：同时最多 4 槽
SLOT_CASH = 10000.0   # 每槽 1 万


def limit_pct(code):
    """涨跌停幅度：创业板/科创板 20cm，其余 10cm。"""
    p = code[:3]
    return 0.195 if p in ("300", "301", "688") else 0.095


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    basic = pd.read_parquet(BASIC)[["code", "name"]]
    name_map = dict(zip(basic["code"], basic["name"]))
    st_codes = set(basic.loc[basic["name"].astype(str).str.contains("ST|退", na=False), "code"])

    ind = pd.read_parquet(BOARD)
    ind = ind[ind["board_type"] == "industry"][["code", "board_code", "board_name"]]
    board_map = dict(zip(ind["code"], ind["board_code"]))
    board_name = dict(zip(ind["code"], ind["board_name"]))

    files = sorted(glob.glob(os.path.join(DAILY, "*.parquet")))
    print(f"[load] {len(files)} files", flush=True)
    stocks = {}
    for f in files:
        code = os.path.basename(f).replace(".parquet", "")
        if code[:1] in ("8", "4", "9"):     # 北交所剔除（现行漏斗不覆盖）
            continue
        if code in EXCLUDE or code in st_codes:  # 第1/2层：ST+指定剔除（静态部分）
            continue
        df = pd.read_parquet(f, columns=["date", "open", "high", "low", "close",
                                         "volume", "amount", "turnover"])
        if len(df) < 62:
            continue
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        c = df["close"].astype(float)
        v = df["volume"].astype(float)
        # 向量化指标（全部只用 ≤当行历史）
        df["prev_close"] = c.shift(1)
        df["pct"] = c / c.shift(1) - 1.0
        df["vol_ratio"] = v / v.shift(1).rolling(5).mean()
        rng = df["high"] - df["low"]
        df["cpos"] = (df["close"] - df["low"]) / rng.replace(0, np.nan)
        df["ma250"] = c.rolling(250).mean()
        df["close60_max"] = c.rolling(60).max()
        df["close20_first"] = c.shift(20)
        tur = df["turnover"].astype(float)
        df["float_mv_then"] = np.where(tur > 0, df["amount"] * 100.0 / tur, np.nan)
        stocks[code] = df
    print(f"[load] 有效票 {len(stocks)}", flush=True)

    cal = sorted(set().union(*[set(df.index) for df in stocks.values()]))
    cal = pd.DatetimeIndex([d for d in cal if START - pd.Timedelta(days=10) <= d])
    cal_pos = {d: i for i, d in enumerate(cal)}
    print(f"[cal] 交易日 {len(cal)}: {cal[0].date()} ~ {cal[-1].date()}", flush=True)

    trades = []          # 逐笔（口径A=全部信号；口径B标记 is_slot）
    slots = []           # 口径B持仓: dict(code, buy_date, sell_date, ...)
    NAV = []             # 口径B净值日序列
    cash_marks = []      # (date, 周盈亏)

    for T in cal:
        if T < START:
            continue
        rows = []
        for code, df in stocks.items():
            if T not in df.index:
                continue
            r = df.loc[T]
            if any(pd.isna(x) for x in (r.prev_close, r.vol_ratio, r.cpos, r.close60_max, r.close20_first)):
                continue
            rows.append((code, r))
        if not rows:
            continue
        # 第 11 层依赖：全市场当日行业均值（用当日有 bar 的全部票）
        board_avg = {}
        for code, r in rows:
            b = board_map.get(code)
            if b is not None:
                board_avg.setdefault(b, []).append(r.pct)
        board_avg = {b: np.mean(v) for b, v in board_avg.items()}

        sig = []
        for code, r in rows:
            # 3. 当时流通市值 30~300 亿
            if pd.isna(r.float_mv_then) or not (30e8 <= r.float_mv_then <= 300e8):
                continue
            # 4. 成交额 ≥2 亿（amount 缺失/为0的脏bar跳过）
            if not (r.amount >= 2e8):
                continue
            # 5. 今日 pct ≥ +5%
            if r.pct < 0.05:
                continue
            # 6. 量比 ≥ 2.0
            if r.vol_ratio < 2.0:
                continue
            # 7. 收盘位置 ≥0.70 且非一字（振幅 ≥3%）
            if pd.isna(r.cpos) or r.cpos < 0.70:
                continue
            if (r.high - r.low) / r.prev_close < 0.03:
                continue
            # 8. 结构突破 8a/8b
            sig_type = None
            if pd.notna(r.ma250) and r.prev_close <= r.ma250 < r.close:
                sig_type = "8a"
            elif r.close >= r.close60_max:
                sig_type = "8b"
            if sig_type is None:
                continue
            # 9. 距 60 日高点 ≥5% 空间（仅 8a）
            room60 = r.close60_max / r.close - 1.0
            if sig_type == "8a" and room60 < 0.05:
                continue
            # 10. 近 20 日累计涨幅 ≤ +30%
            if r.close / r.close20_first - 1.0 > 0.30:
                continue
            # 11. 板块效应：行业均涨 ≥+1.5%（无行业保留）
            b = board_map.get(code)
            bavg = board_avg.get(b) if b is not None else None
            if bavg is not None and bavg < 0.015:
                continue
            score = r.vol_ratio * r.cpos * (bavg if bavg is not None else 0.0)
            sig.append(dict(code=code, name=name_map.get(code, ""), T=T, sig=sig_type,
                            vol_ratio=r.vol_ratio, cpos=r.cpos, pct=r.pct,
                            amount_yi=r.amount / 1e8, mv_then_yi=r.float_mv_then / 1e8,
                            board=board_name.get(code, "无行业"), score=score))
        sig.sort(key=lambda x: -x["score"])

        # ── 执行：T+1 开盘买 → T+5 收盘卖 ──
        tp = cal_pos[T] + 1
        t5 = cal_pos[T] + 5
        buy_date = cal[tp] if tp < len(cal) else None
        sell_date = cal[t5] if t5 < len(cal) else None
        matured = sell_date is not None and sell_date <= cal[-1]

        # 口径B槽位：本日可预占 = 4 - 占用中（持仓卖出日 > T）
        occupied = [s for s in slots if s["sell_cal"] > cal_pos[T]]
        free = SLOT_CAP - len(occupied)

        for k, s in enumerate(sig):
            code = s["code"]
            df = stocks[code]
            rec = dict(s)
            rec["buy_date"] = None
            rec["sell_date"] = None
            rec["ret_net"] = None
            rec["status"] = ""
            if buy_date is None:
                rec["status"] = "窗口截止未开仓"
                trades.append(rec)
                continue
            # T+1 买入
            if buy_date not in df.index:
                rec["status"] = "T+1停牌跳过"
                trades.append(rec)
                continue
            b = df.loc[buy_date]
            lim = limit_pct(code)
            if b.high == b.low and b.open == b.high and b.pct >= lim:
                rec["status"] = "T+1一字涨停买不进"
                trades.append(rec)
                continue
            buy_px = float(b.open)
            rec["buy_date"] = buy_date
            rec["buy_px"] = buy_px
            # 卖出：信号日后第5个bar；停牌/跌停锁死顺延
            sd, sell_px, note = sell_date, None, ""
            while sd is not None:
                if sd not in df.index:
                    sd = cal[cal_pos[sd] + 1] if cal_pos[sd] + 1 < len(cal) else None
                    note = "停牌顺延"
                    continue
                sr = df.loc[sd]
                if sr.close == sr.low and sr.pct <= -lim:
                    note = "跌停锁死顺延"
                    sd = cal[cal_pos[sd] + 1] if cal_pos[sd] + 1 < len(cal) else None
                    continue
                sell_px = float(sr.close)
                break
            rec["sell_date"] = sd
            if sell_px is None:
                rec["status"] = "持有中(数据窗未了结)"
                last = df["close"].iloc[-1]
                rec["float_ret"] = last / buy_px - 1.0 - COST
            else:
                rec["status"] = note if note else "正常"
                rec["ret_net"] = sell_px / buy_px - 1.0 - COST
            rec["is_slot"] = k < free if matured or rec["status"].startswith("持有") else (k < free)
            if k < free:
                free -= 1
                slots.append(dict(code=code, buy_cal=tp,
                                  sell_cal=cal_pos[sd] if sd is not None else 10**9))
            trades.append(rec)

        # 口径B日净值（现金+持仓市值，按当日收盘）
        equity = 40000.0
        for s in slots:
            df = stocks[s["code"]]
            if T in df.index and s["buy_cal"] <= cal_pos[T]:
                px_buy = None
                # 找买入价（简化：从trades回查）
                for tr in trades:
                    if tr["code"] == s["code"] and tr["buy_date"] is not None and \
                       cal_pos.get(tr["buy_date"]) == s["buy_cal"]:
                        px_buy = tr["buy_px"]
                        break
                if px_buy:
                    cur = float(df.loc[T, "close"])
                    ret = cur / px_buy - 1.0 - COST * 0.5
                    equity += SLOT_CASH * ret
        NAV.append(dict(date=T, equity=equity, n_slots=len(
            [s for s in slots if s["buy_cal"] <= cal_pos[T] < s["sell_cal"]])))

    tr = pd.DataFrame(trades)
    tr.to_csv(os.path.join(OUT_DIR, "sim_trades_chase_202606.csv"), index=False, encoding="utf-8-sig")
    nav = pd.DataFrame(NAV)
    nav.to_csv(os.path.join(OUT_DIR, "sim_nav_chase_202606.csv"), index=False)

    done = tr[tr["ret_net"].notna()]
    holding = tr[tr["ret_net"].isna()]
    print(f"\n[结果] 信号总数 {len(tr)}，已了结 {len(done)}，未了结 {len(holding)}")
    if len(done):
        r = done["ret_net"]
        print(f"口径A(全信号等权): 中位 {r.median():+.2%} 均值 {r.mean():+.2%} 胜率 {(r>0).mean():.1%}")
        wk = done.assign(wk=pd.to_datetime(done['sell_date']).dt.to_period('W')) \
                .groupby('wk')['ret_net'].agg(['mean', 'count'])
        print(f"周数 {len(wk)} 周均值中位 {wk['mean'].median():+.2%} 正周 {(wk['mean']>0).mean():.0%}")
    sl = tr[tr.get("is_slot", False) == True] if "is_slot" in tr else tr.iloc[0:0]
    done_b = sl[sl["ret_net"].notna()]
    if len(done_b):
        rb = done_b["ret_net"]
        print(f"口径B(4槽×1万): 笔数 {len(done_b)} 中位 {rb.median():+.2%} 胜率 {(rb>0).mean():.1%}")
        pnl = (rb * SLOT_CASH).sum()
        weeks = (pd.to_datetime(done_b['sell_date']).max() - pd.to_datetime(done_b['buy_date']).min()).days / 7
        print(f"口径B 累计盈亏 {pnl:+,.0f} 元 / 约 {weeks:.0f} 周 = 周均 {pnl/max(weeks,1):+,.0f} 元")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)
