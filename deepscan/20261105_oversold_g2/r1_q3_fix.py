#!/usr/bin/env python3
"""R1 评审 · Q3 补充修正：正确的再破线概率、最差周MC修复、V日组与深跌桶交叉、数据质量抽查。"""
import numpy as np
import pandas as pd

OUT = "/root/workspaces/quant-trading/deepscan/20261105_oversold_g2"
d = pd.read_parquet(f"{OUT}/r1_signals.parquet")
p = pd.read_parquet(f"{OUT}/r1_path.parquet")
rng = np.random.default_rng(7)
IDX = "/opt/quant-trading/data/index/399006.SZ.parquet"
DAILY = "/opt/quant-trading/data/daily/hfq"

idx = pd.read_parquet(IDX)[["date", "close"]].copy()
idx["date"] = pd.to_datetime(idx["date"])
idx = idx.sort_values("date").reset_index(drop=True)
ic = idx["close"].values
ilog = np.diff(np.log(ic))
imap = {dd: i for i, dd in enumerate(idx["date"])}

print("=" * 70)
print("Q3-A2 正确版：刚从线下穿越到线上后，10日内再跌回2407下方的概率（几何匹配今日）")
# 事件: ic[t] < 2407 且 ic[t+2] >= 2407（两日内收复线上，今日=2391.65→2566.80）
ev = [t for t in range(30, len(ic) - 13) if ic[t] < 2407 and ic[t + 2] >= 2407]
res = [(ic[t + 3:t + 13] < 2407).any() for t in ev]
yrs = [idx.date.iloc[t].year for t in ev]
print(f"事件数 N={len(ev)}（{sorted(set(yrs))}），其后10日内再收于2407下方: {np.mean(res):.0%}")
if ev:
    print(f"样本年份分布: {pd.Series(yrs).value_counts().to_dict()}")
# 更贴近今日：收复后站在线上方 5%~8%（今日 2566.80/2407 = +6.6%）
ev2 = [t for t in range(30, len(ic) - 13) if ic[t] < 2407 <= ic[t + 2] and 0.04 <= ic[t + 2] / 2407 - 1 <= 0.09]
res2 = [(ic[t + 3:t + 13] < 2407).any() for t in ev2]
print(f"其中收复幅度+4%~9%（今日+6.6%）: N={len(ev2)}，再跌回线上方概率→跌破: {np.mean(res2) if res2 else float('nan'):.0%}")
# 无条件几何匹配：现价高于线 5%~8%，10日内触及线
ev3 = [t for t in range(30, len(ic) - 13) if 0.04 <= ic[t] / 2407 - 1 <= 0.09]
res3 = [(ic[t + 1:t + 11] < 2407).any() for t in ev3]
print(f"任意时刻处于线上+4%~9%（今日+6.6%）后10日内跌破: N={len(ev3)}, 概率 {np.mean(res3):.0%}")

print("=" * 70)
print("Q3-F2 最差周 MC 修复：按中位排序的全部周（含N），取 N>=4 的最差5周")
d3 = d.dropna(subset=["t10_exec"]).copy()
wk_stats = d3.groupby("wk").agg(N=("t10_exec", "size"), med=("t10_exec", "median"),
                                win=("t10_exec", lambda v: (v > 0).mean()),
                                imin=("idx_min10", "first"))
print(wk_stats.sort_values("med").head(10).to_string(float_format=lambda v: f"{v:+.3f}"))
bywk = {w: g.t10_exec.values for w, g in d3.groupby("wk")}
wmed = wk_stats.sort_values("med")
pool_w = [w for w in wmed.index if bywk[w].size >= 4][:5]
print(f"\n最差5周(N>=4): {pool_w}")
mcw = []
for _ in range(20000):
    w = pool_w[rng.integers(len(pool_w))]
    xx = bywk[w]
    mcw.append(xx[rng.choice(len(xx), 4, replace=False)].mean())
mcw = np.array(mcw)
wave = 38200
print(f"最差5周同日4只MC: 均值 {mcw.mean():+.2%} 中位 {np.median(mcw):+.2%} P5 {np.quantile(mcw,.05):+.2%} "
      f"最差 {mcw.min():+.2%} → 3.82万仓 {np.quantile(mcw,.05)*wave:+,.0f} ~ {mcw.min()*wave:+,.0f} 元")

print("=" * 70)
print("Q3-H2 V次日组 × 深跌桶交叉（今日 setup 的最相似历史子样本）")
wk_backward = {}
for wk in d.wk.unique():
    j = imap.get(pd.Timestamp(wk))
    wk_backward[wk] = dict(idx_prev1=ic[j - 1] / ic[j - 2] - 1) if j and j >= 2 else dict(idx_prev1=np.nan)
d["idx_prev1"] = d.wk.map(lambda w: wk_backward[w]["idx_prev1"])
d3 = d.dropna(subset=["t10_exec"]).copy()
vday = d3[d3.idx_prev1 >= 0.04]
print(f"V次日组(前一日指数>=+4%): N={len(vday)}, 周={sorted(vday.wk.unique())}")
print(f"  全组: 中位 {vday.t10_exec.median():+.2%} 胜率 {(vday.t10_exec>0).mean():.0%}")
deep = vday[vday.ret20 <= -0.40]
print(f"  其中深跌<=-40%: N={len(deep)} 中位 {deep.t10_exec.median() if len(deep) else float('nan'):+.2%} "
      f"胜率 {(deep.t10_exec>0).mean() if len(deep) else float('nan'):.0%}")
deep_all = d3[d3.ret20 <= -0.40]
print(f"  [对照] 全样本深跌<=-40%: N={len(deep_all)} 中位 {deep_all.t10_exec.median():+.2%} 来自周 {sorted(deep_all.wk.unique())}")
# '>0稳'桶的周构成
stab = d3[d3.idx_min10 > 0]
print(f"\n'指数10日单边不回撤'桶: N={len(stab)} 中位 {stab.t10_exec.median():+.2%} 胜率 {(stab.t10_exec>0).mean():.0%}")
print(f"  构成周: {sorted(stab.wk.unique())}")

print("=" * 70)
print("Q3-I 4 候选数据质量抽查（近60bar 缺口/脏bar/停牌）")
for code in ["688668.SH", "001389.SZ", "600722.SH", "601579.SH", "300687.SZ"]:
    df = pd.read_parquet(f"{DAILY}/{code}.parquet")
    tail = df.tail(60)
    nan_c = tail.close.isna().sum()
    zero_v = (tail.volume == 0).sum()
    dup_d = tail.date.duplicated().sum()
    # bar 间隔（交易日历近似：>5个自然日=疑似停牌缺口）
    dt = pd.to_datetime(tail.date)
    gaps = (dt.diff().dt.days > 5).sum()
    print(f"  {code}: 近60bar NaN收盘 {nan_c}, 零量 {zero_v}, 重复日期 {dup_d}, >5自然日缺口 {gaps}, "
          f"最后bar {dt.iloc[-1].date()}")
print("脏bar清单: failed_repair_hfq.txt / failed_stocks_hfq.txt 中是否含候选")
import subprocess
r_ = subprocess.run(["grep", "-l", "688668", "/opt/quant-trading/data/failed_repair_hfq.txt",
                     "/opt/quant-trading/data/failed_stocks_hfq.txt"], capture_output=True, text=True)
print("  候选在failed清单:", r_.stdout or "无")
