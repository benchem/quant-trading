#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R1-Q1 市值/ST 前视偏差: 今天值套历史信号 vs 当时市值。"""
import pandas as pd
import numpy as np

OUT = "/root/workspaces/quant-trading/deepscan/20261105_oversold_g2"
sig = pd.read_csv(f"{OUT}/r1_signals_full.csv")
sig["mv_then"] = sig["mv_then"] / 1e8  # 元 → 亿
for col in ("is_st", "is_tui", "in_basic", "g2_21", "g2_20", "g2_board", "rng0", "dirty_at_sig"):
    sig[col] = sig[col].astype("boolean").fillna(False).astype(bool)


def stat(d, label):
    s = d["t10"].dropna()
    if len(s) == 0:
        print(f"{label:<44} N=0")
        return
    print(f"{label:<46} N={len(s):>5} 中位 {s.median():+.2%} 均值 {s.mean():+.2%} "
          f"胜率 {(s>0).mean():.1%}")


# ---------- 复现官方口径 ----------
today_ok = sig["in_basic"] & ~sig["is_st"] & ~sig["is_tui"] & sig["mv_today_f"].between(30, 300)
official_base = sig[today_ok]
official_g2 = official_base[official_base["g2_21"]]
print("== 复现官方口径(今天 float_mv 30-300 & 非ST) ==")
stat(official_base, "base(对标官方 N=1945 中位+0.18%)")
stat(official_g2, "G2(对标官方 N=1893 中位+0.24%)")
exec_g2 = official_g2["exec_t10"].dropna()
print(f"   执行口径(次日开盘买): N={len(exec_g2)} 中位 {exec_g2.median():+.2%} (官方 +0.80%)")

# ---------- Q1a: 用当时市值替换今天的市值闸 ----------
mv_then_ok = sig["mv_then"].notna() & sig["mv_then"].between(30, 300)
print("\n== Q1 前视偏差分解 ==")
# 口径A: 官方(今天市值+今天非ST)
# 口径B: 当时市值 + 今天非ST(ST历史状态不可得, 先保持)
then_g2 = sig[mv_then_ok & ~sig["is_st"] & ~sig["is_tui"] & sig["in_basic"] & sig["g2_21"]]
stat(then_g2, "B: 当时市值闸(30-300)+今天非ST")
# 口径C: 去掉市值闸, 保留今天非ST
nomv_g2 = sig[~sig["is_st"] & ~sig["is_tui"] & sig["in_basic"] & sig["g2_21"]]
stat(nomv_g2, "C: 去市值闸, 今天非ST")
# 口径D: 完全不过滤(含今天ST/退市/不在basic)
stat(sig[sig["g2_21"]], "D: 全宇宙无过滤")

# ---------- 偏差来源拆解 ----------
print("\n== 偏差来源拆解(相对官方G2集) ==")
g2set = official_g2.copy()
g2set["mv_then_ok"] = g2set["mv_then"].between(30, 300)
lost = g2set[~g2set["mv_then_ok"]]  # 官方集里当时不在30-300的
stat(g2set[g2set["mv_then_ok"]], "官方G2集 ∩ 当时市值仍30-300")
stat(lost, "官方G2集 - 当时市值<30亿或>300亿(成长幸存者)")
print(f"   其中当时<30亿: {(lost['mv_then']<30).sum()} 只次, 当时>300亿: {(lost['mv_then']>300).sum()} 只次")
print(f"   当时<30亿信号的中位: ", end="")
s = lost.loc[lost["mv_then"] < 30, "t10"].dropna()
print(f"{s.median():+.2%}" if len(s) else "无")

# 反向: 当时在30-300但今天不在(被今天闸剔掉的历史信号)
add = sig[mv_then_ok & sig["g2_21"] & sig["in_basic"]
          & (~sig["mv_today_f"].between(30, 300))]
stat(add, "当时30-300但今天出界(被前视剔掉)")
s = add["t10"].dropna()
if len(s):
    print(f"   今天<30亿: {(add['mv_today_f']<30).sum()}, 今天>300亿: {(add['mv_today_f']>300).sum()}")

# ST今天被剔的信号(在30-300当时)
st_sig = sig[mv_then_ok & sig["g2_21"] & sig["is_st"] == True]
stat(st_sig, "当时市值达标但今天ST(被ST闸剔)")
tui_sig = sig[sig["is_tui"] == True]
stat(tui_sig, "今天带'退'标记")
nb_sig = sig[~sig["in_basic"] & sig["g2_21"]]
stat(nb_sig, "不在stock_basic(已退市缓存文件)")

# ---------- mv_then 质量 sanity ----------
print("\n== mv_then 质量 ==")
both = official_g2.dropna(subset=["mv_then"])
print(f"mv_then 覆盖 {len(both)}/{len(official_g2)}, 与今天市值中位比: "
      f"{(both['mv_then']/both['mv_today_f']).median():.2f}")
print(f"官方G2集当时市值分布: Q10 {both['mv_then'].quantile(.1):.0f}亿 "
      f"Q25 {both['mv_then'].quantile(.25):.0f}亿 中位 {both['mv_then'].median():.0f}亿 "
      f"Q75 {both['mv_then'].quantile(.75):.0f}亿")
