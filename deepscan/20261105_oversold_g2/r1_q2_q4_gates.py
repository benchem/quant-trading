#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R1-Q2 脏bar影响 + R1-Q4 G2闸阈值完整性。"""
import re
import pandas as pd
import numpy as np

OUT = "/root/workspaces/quant-trading/deepscan/20261105_oversold_g2"
sig = pd.read_csv(f"{OUT}/r1_signals_full.csv")
meta = pd.read_csv(f"{OUT}/r1_stock_meta.csv")
sig["mv_then"] = sig["mv_then"] / 1e8
for col in ("is_st", "is_tui", "in_basic", "g2_21", "g2_20", "g2_board", "rng0", "dirty_at_sig"):
    sig[col] = sig[col].astype("boolean").fillna(False).astype(bool)

today_ok = sig["in_basic"] & ~sig["is_st"] & ~sig["is_tui"] & sig["mv_today_f"].between(30, 300)
g2 = sig[today_ok & sig["g2_21"]].copy()
base = sig[today_ok].copy()


def stat(d, col="t10", label=""):
    s = d[col].dropna()
    print(f"  {label:<48} N={len(s):>5} 中位 {s.median():+.2%} 均值 {s.mean():+.2%} 胜率 {(s>0).mean():.1%}")


# ================= Q2 脏bar =================
print("=" * 70)
print("Q2 脏bar影响")
# 官方明细 CSV 与脏票交集
det = pd.read_csv("/root/workspaces/quant-trading/oversold_probe_detail.csv")
audit_dirty = set(meta.loc[meta["in_audit_dirty"], "code"])
print(f"官方 base 明细 {len(det)} 信号中落在审计脏票上的: {det['code'].isin(audit_dirty).sum()}")
sub = det[~det["code"].isin(audit_dirty)]
s1, s2 = det["t10"].dropna(), sub["t10"].dropna()
print(f"  剔脏票前: N={len(s1)} 中位 {s1.median():+.2%} | 剔后: N={len(s2)} 中位 {s2.median():+.2%} "
      f"| Δ中位 {s2.median()-s1.median():+.3%}")

# 脏bar星期分布(用 meta 无法看星期 → 直接抽脏票看脏bar星期)
DAILY = "/opt/quant-trading/data/daily/hfq"
wk_cnt = {}
sample = sorted(audit_dirty)
for code in sample:
    try:
        df = pd.read_parquet(f"{DAILY}/{code}.parquet", columns=["date", "amount", "turnover", "volume"])
    except Exception:
        continue
    d = df.tail(280).reset_index(drop=True)
    mv = 100 * d["amount"] / d["turnover"].replace(0, np.nan)
    roll = mv.rolling(11, center=True, min_periods=3).median()
    dirty = ((mv / roll - 1).abs() > 0.25) | (d["amount"] <= 0) | d["amount"].isna()
    dirty = dirty.fillna(False) & ~((d["amount"] <= 0) & (d["volume"] <= 0))
    for dt in pd.to_datetime(d.loc[dirty, "date"]):
        wk_cnt[dt.day_name()] = wk_cnt.get(dt.day_name(), 0) + 1
print(f"52周窗口内脏amount bar 的星期分布: {wk_cnt}")

# 信号窗/持有窗脏bar落点
print(f"G2集(官方口径 N={len(g2)}) 脏bar落点:")
print(f"  信号日当天 amount 脏: {g2['dirty_at_sig'].sum()}")
print(f"  G2回看窗(21bar)含脏bar: {(g2['dirty_in_g2win']>0).sum()} 信号, 累计 {g2['dirty_in_g2win'].sum()} bar")
print(f"  持有窗(i+1..i+10)含脏bar: {(g2['dirty_in_hold']>0).sum()} 信号, 累计 {g2['dirty_in_hold'].sum()} bar")
print(f"  信号窗内 OHLC 越板块涨跌幅界: {(g2['bad_ohlc_in_win']>0).sum()} 信号")
stat(g2[g2["dirty_in_g2win"] > 0], label="G2窗含脏bar的信号收益")
stat(g2[g2["dirty_in_g2win"] == 0], label="G2窗无脏bar的信号收益")

# 今日候选是否踩脏
cand = pd.read_csv("/root/workspaces/quant-trading/scans/oversold_g2_candidates_20261105.csv")
print(f"今日候选 {len(cand)} 只中审计脏票: {cand['code'].isin(audit_dirty).sum()}")

# ================= Q4 G2闸阈值 =================
print("=" * 70)
print("Q4 G2闸阈值完整性")
# 4.1 官方 base 集里被 G2 剔掉的样本, 按板块/触发pct分桶
rej = base[~base["g2_21"]].copy()
print(f"官方 base {len(base)} 中被 G2(一字跌停,-9.5%) 剔除: {len(rej)}")
ev = []
for _, r in rej.iterrows():
    for p in str(r["ow_events"]).split("|"):
        if p:
            ev.append((r["board"], float(p.strip("+").replace("%", "")) / 100, r["t10"]))
ev = pd.DataFrame(ev, columns=["board", "pct", "t10"])
print("  触发事件按板块×pct区间的分布(次数):")
for bd in ("main10", "cyb20", "kcb20"):
    e = ev[ev["board"] == bd]
    if len(e) == 0:
        continue
    b1 = (e["pct"] > -0.195).sum()   # -9.5%~-19.5%: 20cm板上不是真一字跌停
    b2 = (e["pct"] <= -0.195).sum()
    print(f"    {bd}: 共{len(e)} | -9.5%~ -19.5%: {b1} | ≤-19.5%(真20cm一字): {b2}")

# 4.2 20cm 板被误剔的样本(base信号, g2_21=False 但 g2_board=True)
fake = base[base["board"].isin(["cyb20", "kcb20"]) & ~base["g2_21"] & base["g2_board"]]
print(f"\n  20cm板 base 信号被 -9.5% 阈值误剔(窗口内一字bar pct>-19.5%): {len(fake)}")
stat(fake, label="    这些误剔样本的 t10")
true20 = base[base["board"].isin(["cyb20", "kcb20"]) & ~base["g2_21"] & ~base["g2_board"]]
print(f"  20cm板 base 信号被正确剔(窗口内有真≤-19.5%一字): {len(true20)}")
stat(true20, label="    正确剔除样本的 t10")

# 4.3 5cm 漏剔区(ST历史期间的一字跌停 -9.5%~-4.5%)
st5 = base[base["st5_in_win"] > 0]
print(f"\n  base 信号窗口含 -9.5%~-4.5% 一字bar(5cm一字, 现行漏剔): {len(st5)}")
stat(st5, label="    这些漏剔样本的 t10")

# 4.4 两版窗口 21bar vs 20bar 差异
diff = base[base["g2_21"] != base["g2_20"]]
print(f"\n  G2窗口 21bar(probe) vs 20bar(scanner) 判定不同的信号: {len(diff)}")

# 4.5 rng==0 的 cpos 处理(digest 说按0.5计, 代码按0)
rng0 = sig[sig["rng0"]]
print(f"  信号日 high==low(被 cpos=0 拒掉) 的 base 候选: {len(rng0)} (digest 规格称应按0.5计入)")

# 4.6 今日4候选的G2窗口复核
print("\n  今日候选 G2 复核(20cm=688/300/301, 阈值应为-19.5%):")
for code in ("688668.SH", "001389.SZ", "600722.SH", "601579.SH"):
    r = sig[(sig["code"] == code) & (sig["wk"] == "2026-11-05")]
    if len(r):
        r = r.iloc[0]
        print(f"    {code} board={r['board']} G2(21bar,-9.5%)={r['g2_21']} "
              f"窗口一字事件: {r['ow_events'] or '无'}")

# 附: G2集按板块构成
print("\n  官方G2集板块构成:")
print(g2["board"].value_counts().to_string())
