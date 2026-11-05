#!/usr/bin/env python3
"""B 路线研究双探针（M2 策略扩充）——缩量回踩漏斗 + 超跌回归加严版。

背景（2026-11-05 用户批复 M1~M4 路线后启动研究）：
  1) 缩量回踩漏斗：T2 手工条件（大涨后等缩量回踩再买）的系统化验证。
     先验假设：放量涨日（燃料日）后，出现缩量浅回踩 = 卖压衰竭，次日真空反弹。
  2) 超跌回归加严：基础版 T+10 中位 +0.17%/胜率 50.9% 已有效，加三道闸压左尾：
     上市满 1 年、近 20 日无一字跌停（流动性死亡）、距 MA60 > -20%（非自由落体）。

先验参数（一次性定死，禁止回测后调参）：
  回踩漏斗：
    F1 燃料日：近 5 日内存在 pct>=+5% 且 vol_ratio>=1.8（vs 其前 5 均量）之日
    F2 缩量：信号日量 <= 0.75 × 燃料日量
    F3 浅回踩：信号日 pct ∈ [-6%, -0.5%]，且收盘 >= 燃料日开盘（未吃掉大阳实体）
    F4 收盘位置 >= 0.25（不在当日最弱段接刀）
    F5 壳：非 ST、流通市值 30~300 亿
  超跌加严（在基础版 20 日跌>=20% + 收盘位置>=0.5 之上）：
    G1 上市满 1 年：len(df) >= 250
    G2 无流动性死亡：近 20 日无一字板跌停（low==high 且 pct<=-9.5%）
    G3 防自由落体：close >= MA60 × 0.80

回测口径（镜像，与 quant-funnel-backtest 技能铁律一致）：
  信号日 T 收盘买入 → 持有 N=1/2/3/5/10 日收盘卖出，扣双边成本 0.68%。
  每周取一个信号日（该周最后一个交易日），防同事件重复计样。
  市场基线 = 全 universe 等权同持有期（扣同成本）。
"""
import glob
import json
import os

import numpy as np
import pandas as pd

DATA = "/opt/quant-trading/data"
DAILY = os.path.join(DATA, "daily", "hfq")
BASIC = os.path.join(DATA, "dataset", "stock_basic.parquet")
COST = 0.0068
OUT = "/root/workspaces/quant-trading"

HOLDS = [1, 2, 3, 5, 10]


def load_basic():
    basic = pd.read_parquet(BASIC)[["code", "name", "float_mv"]]
    basic["float_mv_yi"] = basic["float_mv"] / 1e8
    keep = (basic["float_mv_yi"].between(30, 300)
            & ~basic["name"].astype(str).str.contains("ST|退", na=False))
    return set(basic.loc[keep, "code"])


def forward(c, i):
    """信号日 i 收盘买入后各持有期收益（截断处返回 None）。"""
    out = {}
    for n in HOLDS:
        j = i + n
        out[f"t{n}"] = (c[j] / c[i] - 1 - COST) if j < len(c) else None
    return out


def pullback_signal(c, v, o, i):
    """缩量回踩漏斗。返回 dict 或 None（truthy 铁律：成功必返回非空 dict）。"""
    if i < 30:
        return None
    # F1 燃料日：近5日内(不含当日)找 pct>=5% 且量比>=1.8
    fuel = None
    for k in range(max(0, i - 5), i):
        if k < 6:
            continue
        pct = c[k] / c[k - 1] - 1
        base = v[k - 5:k].mean()
        if base > 0 and pct >= 0.05 and v[k] / base >= 1.8:
            fuel = k
            break
    if fuel is None:
        return None
    # F2 缩量
    if v[i] > 0.75 * v[fuel] or v[i] <= 0:
        return None
    # F3 浅回踩 + 未吃掉大阳实体
    pct_i = c[i] / c[i - 1] - 1
    if not (-0.06 <= pct_i <= -0.005):
        return None
    if c[i] < o[fuel]:
        return None
    # F4 收盘位置（无 high/low 列时用 c[i] vs c[i-1] 近似强度，此处需要当日高低——由调用方传入）
    return dict(fuel_pct=c[fuel] / c[fuel - 1] - 1, pct=pct_i)


def pullback_cpos(c, h, l, i):
    rng = h[i] - l[i]
    return (c[i] - l[i]) / rng if rng > 0 else 0.0


def oversold_signal(c, i):
    """超跌回归基础版 + G1~G3 加严。返回 'strict' / 'base' / None 分层标记。"""
    if i < 60:
        return None
    ret20 = c[i] / c[i - 20] - 1
    rng = h_g[i] - l_g[i]
    cpos = (c[i] - l_g[i]) / rng if rng > 0 else 0.0
    if ret20 > -0.20 or cpos < 0.5:
        return None
    return "base"


def main():
    files = sorted(glob.glob(os.path.join(DAILY, "*.parquet")))
    files = [f for f in files if os.path.basename(f).replace(".parquet", "") in load_basic()]
    print(f"universe 文件数: {len(files)}")

    # 周信号日
    dates = pd.concat([pd.read_parquet(f, columns=["date"])["date"] for f in files[:300]])
    trade_dates = sorted(set(pd.to_datetime(dates)))
    weekly, seen = [], None
    for dt in reversed(trade_dates):
        wk = dt.isocalendar()[:2]
        if wk != seen:
            weekly.append(dt)
            seen = wk
    weekly = sorted(weekly)[-52:]
    print(f"周信号日: {len(weekly)} 周（{weekly[0].date()} ~ {weekly[-1].date()}）")

    pb_recs, os_strict, os_base, mkt = [], [], [], []
    global h_g, l_g  # oversold_signal 复用当日高低

    for f in files:
        code = os.path.basename(f).replace(".parquet", "")
        try:
            df = pd.read_parquet(f, columns=["date", "close", "high", "low", "open", "volume"])
        except Exception:
            continue
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        c = df["close"].values
        h = df["high"].values
        l = df["low"].values
        o = df["open"].values
        v = df["volume"].values
        n = len(c)
        dmap = {d: i for i, d in enumerate(df["date"])}

        for sd in weekly:
            i = dmap.get(sd)
            if i is None:
                continue
            if c[i] <= 0:
                continue

            # 市场基线
            if i + 10 < n:
                mkt.append(forward(c, i))

            # —— 缩量回踩漏斗 ——
            sig = pullback_signal(c, v, o, i)
            if sig and pullback_cpos(c, h, l, i) >= 0.25 and i + 10 < n:
                r = forward(c, i)
                r.update(code=code, wk=str(sd.date()), **sig)
                pb_recs.append(r)

            # —— 超跌回归（基础 + 加严分层）——
            h_g, l_g = h, l
            tag = oversold_signal(c, i)
            if tag and i + 10 < n:
                r = forward(c, i)
                r.update(code=code, wk=str(sd.date()))
                # G1 上市满1年
                g1 = n >= 250
                # G2 近20日无一字跌停
                g2 = True
                for k in range(max(0, i - 20), i + 1):
                    if l[k] == h[k] and c[k] / c[k - 1] - 1 <= -0.095:
                        g2 = False
                        break
                # G3 距MA60 > -20%
                ma60 = c[max(0, i - 59):i + 1].mean()
                g3 = c[i] >= ma60 * 0.80
                if g1 and g2 and g3:
                    r["tier"] = "strict"
                    os_strict.append(r)
                else:
                    r["tier"] = "base_only"
                    r.update(g1=g1, g2=g2, g3=g3)
                    os_base.append(r)

    def report(label, recs):
        d = pd.DataFrame(recs)
        print(f"\n—— {label}: N={len(d)} ——")
        for n_ in HOLDS:
            col = f"t{n_}"
            s = d[col].dropna()
            if len(s) == 0:
                print(f"  T+{n_}: 无样本")
                continue
            print(f"  T+{n_:>2}: 中位 {s.median():+.2%} 均值 {s.mean():+.2%} 胜率 {(s > 0).mean():.1%} "
                  f"[Q25 {s.quantile(.25):+.1%}~Q75 {s.quantile(.75):+.1%}] 最差 {s.min():+.1%} 最好 {s.max():+.1%}")
        return d

    print("\n========== 结果 ==========")
    d_mkt = report("市场基线(等权全universe)", mkt)
    d_pb = report("缩量回踩漏斗", pb_recs)
    d_strict = report("超跌回归-加严(G1上市1年+G2无一字板+G3距MA60>-20%)", os_strict)
    d_base = report("超跌回归-基础(仅20日跌>=20%+cpos>=0.5, 不含加严)", os_base)

    # 周命中分布
    for label, recs in [("回踩", pb_recs), ("超跌加严", os_strict)]:
        wk_cnt = pd.Series([r["wk"] for r in recs]).value_counts()
        print(f"\n{label} 周命中: 中位 {wk_cnt.median():.0f} 只/周, 零命中周 {(~pd.Series(sorted(set(str(w.date()) for w in weekly))).isin(wk_cnt.index)).sum() if len(recs) else '—'}")

    d_pb.to_csv(os.path.join(OUT, "pullback_probe_detail.csv"), index=False)
    d_strict.to_csv(os.path.join(OUT, "oversold_strict_detail.csv"), index=False)
    summary = dict(
        window=f"{weekly[0].date()}~{weekly[-1].date()}",
        cost=COST, holds=HOLDS,
        pullback=dict(N=len(d_pb)),
        oversold_strict=dict(N=len(d_strict)),
        oversold_base=dict(N=len(d_base)),
        market=dict(N=len(d_mkt)),
    )
    with open(os.path.join(OUT, "scans", "probe_pullback_oversold_result.json"), "w") as fp:
        json.dump(summary, fp, ensure_ascii=False, indent=2)
    print("\n明细: pullback_probe_detail.csv / oversold_strict_detail.csv")


if __name__ == "__main__":
    main()
