#!/usr/bin/env python3
"""R1 评审 · Q3：规模适配 + kill-switch(创业板<2407)联动 + 组合压力测试。"""
import numpy as np
import pandas as pd

OUT = "/root/workspaces/quant-trading/deepscan/20261105_oversold_g2"
d = pd.read_parquet(f"{OUT}/r1_signals.parquet")
p = pd.read_parquet(f"{OUT}/r1_path.parquet")
rng = np.random.default_rng(7)
IDX = "/opt/quant-trading/data/index/399006.SZ.parquet"
DAILY = "/opt/quant-trading/data/daily/hfq"
COST = 0.0068

idx = pd.read_parquet(IDX)[["date", "close"]].copy()
idx["date"] = pd.to_datetime(idx["date"])
idx = idx.sort_values("date").reset_index(drop=True)
ic = idx["close"].values
ilog = np.diff(np.log(ic))
imap = {d_: i for i, d_ in enumerate(idx["date"])}

print("=" * 70)
print("Q3-A kill-switch 触发概率（创业板指 现 2566.80，kill线 2407 = -6.23%）")
thr = 2407 / ic[-1] - 1
print(f"距离: {thr:.2%}；持仓窗口 11-06~11-19 共 10 个交易日")
# 无条件：历史上任意 10 日窗口内最低收盘较起点跌幅 >=6.23% 的频率
hits, tot = 0, 0
for t in range(len(ic) - 11):
    tot += 1
    if ic[t + 1:t + 11].min() / ic[t] - 1 <= thr:
        hits += 1
print(f"全历史({idx.date.iloc[0].date()}~) 10日窗口内跌破-6.23%频率: {hits/tot:.1%} (N={tot})")
# 波动率条件化：当前 20 日已实现波动
vol20 = np.std(ilog[-20:]) * np.sqrt(252)
print(f"当前 20 日年化波动: {vol20:.0%}")
vols = np.array([np.std(ilog[t-20:t]) * np.sqrt(252) for t in range(20, len(ic) - 11)])
tarr = np.arange(20, len(ic) - 11)
hit_arr = np.array([ic[t + 1:t + 11].min() / ic[t] - 1 <= thr for t in tarr])
q = (vols >= np.quantile(vols, 0.8))
print(f"高波动 regime(20日波动>=P80={np.quantile(vols,0.8):.0%}): 跌破频率 {hit_arr[q].mean():.1%} (N={q.sum()})")
print(f"低波动 regime(<P80): 跌破频率 {hit_arr[~q].mean():.1%}")
# 刚从 kill 线下方的 V 反弹（模拟当前: 2日前收盘 2391.65 < 2407，随后两日 +6.4%/+0.9%）
below = [t for t in range(30, len(ic) - 12) if ic[t] < 2407 and ic[t + 2] / ic[t] - 1 >= 0.06]
if below:
    back = [t for t in below if ic[t + 2] < 2407 or ic[t + 1] < 2407]
    fut = [(ic[min(t + 2 + k, len(ic) - 1)] < 2407) for t in below for k in range(1, 11)]
    print(f"历史'收盘<2407后两日反弹>=6%'事件 N={len(below)} 次: 其后10日内再破2407比例 {np.mean(fut):.0%}")

print("=" * 70)
print("Q3-B 持仓窗口内指数崩跌条件下的信号组结局（kill 触发 = 跌破信号日收盘-6.23%）")
d3 = d.dropna(subset=["t10_exec"]).copy()
bins = [-1, -0.0623, 0, 1]
labels = ["min10<=-6.23%(kill触发)", "-6.23%~0", ">0(稳)"]
d3["kb"] = pd.cut(d3.idx_min10, bins, labels=labels)
g = d3.groupby("kb", observed=True).agg(
    N=("t10_exec", "size"), t10中位=("t10_exec", "median"), t10均值=("t10_exec", "mean"),
    胜率=("t10_exec", lambda v: (v > 0).mean()), P5=("t10_exec", lambda v: v.quantile(.05)),
    P10=("t10_exec", lambda v: v.quantile(.10)))
print(g.to_string(float_format=lambda v: f"{v:+.3f}"))

print("=" * 70)
print("Q3-C kill-switch 强平模拟：触发日收盘卖出 vs 死扛到 T+10（对触发组）")
# 每个信号周的 kill 日（k=1..10 中首个 ic[j+k] <= ic[j]*0.9377）
kill_k_by_wk = {}
for wk in d3.wk.unique():
    j = imap.get(pd.Timestamp(wk))
    kk = None
    if j is not None and j + 10 < len(ic):
        for k in range(1, 11):
            if ic[j + k] <= ic[j] * 0.9377:
                kk = k
                break
    kill_k_by_wk[wk] = kk
# kill 日个股收盘价：path 表 pivot（行=code,wk；列=k）
path_c = p.pivot_table(index=["code", "wk"], columns="k", values="c")
trig = d3[d3.wk.map(lambda w: kill_k_by_wk[w] is not None)].copy()
trig["kill_k"] = trig.wk.map(lambda w: kill_k_by_wk[w])
trig["exit_c"] = [path_c.loc[(r.code, r.wk), r.kill_k] if (r.code, r.wk) in path_c.index else np.nan
                  for r in trig.itertuples()]
trig["kill_ret"] = trig.exit_c / trig.entry_open - 1 - COST
ok = trig.dropna(subset=["kill_ret"])
print(f"触发组 N={len(ok)}（来自 {ok.wk.nunique()} 周: {sorted(ok.wk.unique())}）")
print(f"  kill日收盘强平: 中位 {ok.kill_ret.median():+.2%} 均值 {ok.kill_ret.mean():+.2%}")
print(f"  死扛到T+10    : 中位 {ok.t10_exec.median():+.2%} 均值 {ok.t10_exec.mean():+.2%}")
print(f"  强平后至T+10继续涨(强平吃亏): {(ok.t10_exec > ok.kill_ret).mean():.1%}")
print(f"  强平避开进一步下跌(强平赚): {(ok.t10_exec < ok.kill_ret).mean():.1%}")
print(f"  强平 vs 死扛 差值均值: {(ok.kill_ret - ok.t10_exec).mean():+.2%}")

print("=" * 70)
print("Q3-D 既有持仓 beta：赛意300687 及 4 候选 vs 创业板指（近120交易日）")
def beta_of(code, days=120):
    df = pd.read_parquet(f"{DAILY}/{code}.parquet")[["date", "close"]]
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    s = np.diff(np.log(df.close.values))[-days:]
    n = min(len(s), len(ilog))
    b = np.polyfit(ilog[-n:], s, 1)[0]
    r = np.corrcoef(ilog[-n:], s)[0, 1]
    ret20 = np.exp(np.log(df.close.values[-1]) - np.log(df.close.values[-21])) - 1
    return b, r, ret20
total_assets = 133400
for code, name, val in [("300687.SZ", "赛意信息", 40000), ("688668.SH", "鼎通科技", 10710),
                        ("001389.SZ", "广合科技", 8990), ("600722.SH", "金牛化工", 9424),
                        ("601579.SH", "会稽山", 9076)]:
    b, r, ret20 = beta_of(code)
    print(f"  {code} {name}: beta {b:.2f} 相关 {r:.2f} ret20 {ret20:+.1%} 持仓 {val:,}元")

print("=" * 70)
print("Q3-E 组合压力测试（kill 场景: 创业板指自 2566.80 跌 6.23% 至 2407 下方）")
wave = 38200
kb = d3[d3.idx_min10 <= -0.0623]
wave_med = kb.t10_exec.median(); wave_p5 = kb.t10_exec.quantile(.05)
b_sy, _, _ = beta_of("300687.SZ")
sy_loss = 40000 * b_sy * (-0.0623)
print(f"  波段仓 3.82万 × 触发组中位 {wave_med:+.2%} = {wave*wave_med:+,.0f} 元 | ×P5 {wave_p5:+.2%} = {wave*wave_p5:+,.0f} 元")
print(f"  赛意 4万 × beta{b_sy:.2f} × -6.23% = {sy_loss:+,.0f} 元")
print(f"  合计(kill场景,中位): {wave*wave_med + sy_loss:+,.0f} 元 = 总资产 {total_assets:,} 的 {(wave*wave_med+sy_loss)/total_assets:+.1%}")
print(f"  合计(kill场景,P5)  : {wave*wave_p5 + sy_loss:+,.0f} 元 = {(wave*wave_p5+sy_loss)/total_assets:+.1%}")

print("=" * 70)
print("Q3-F 规模适配：单槽与组合级亏损（执行口径，1万/槽×4）")
s = d.t10_exec.dropna()
print(f"  单槽: P5 {s.quantile(.05):+.2%}={abs(s.quantile(.05))*10000:,.0f}元({abs(s.quantile(.05))*10000/total_assets:.1%}总资产) "
      f"P10 {s.quantile(.10):+.2%} 最差 {s.min():+.2%}={abs(s.min())*10000:,.0f}元")
bywk = {w: g.t10_exec.dropna().values for w, g in d3.groupby("wk")}
# 全46周同日4抽 MC（含均值）
pool = [w for w in bywk if len(bywk[w]) >= 4]
mc = np.array([bywk[(w := pool[rng.integers(len(pool))])][rng.choice(len(bywk[w]), 4, False)].mean() for _ in range(20000)])
print(f"  同日4只MC(任意周): 均值 {mc.mean():+.2%} P5 {np.quantile(mc,.05):+.2%}({np.quantile(mc,.05)*wave:+,.0f}元) "
      f"最差 {mc.min():+.2%}({mc.min()*wave:+,.0f}元={abs(mc.min())*wave/total_assets:.1%}总资产)")
# 最差5周同日4抽
wmed = d3.groupby("wk").t10_exec.median().sort_values()
worst5 = list(wmed.head(5).index)
pool_w = [w for w in worst5 if len(bywk[w]) >= 4]
mcw = np.array([bywk[(w := pool_w[rng.integers(len(pool_w))])][rng.choice(len(bywk[w]), 4, False)].mean() for _ in range(20000)])
print(f"  最差5周({','.join(worst5)})同日4只MC: 均值 {mcw.mean():+.2%} P5 {np.quantile(mcw,.05):+.2%} "
      f"({np.quantile(mcw,.05)*wave:+,.0f}元) 最差 {mcw.min():+.2%}({mcw.min()*wave:+,.0f}元)")
print(f"  [对照] 若4槽分4个不相邻周: 均值≈周级均值 {d3.groupby('wk').t10_exec.mean().mean():+.2%}")

print("=" * 70)
print("Q3-G 候选相似切片：主板10cm & ret20<=-40%（4候选中3只的板块）")
cand = d[(d.board == "mb10cm") & (d.ret20 <= -0.40)]
if len(cand) >= 5:
    print(f"  N={len(cand)} 中位 {cand.t10_exec.median():+.2%} 均值 {cand.t10_exec.mean():+.2%} "
          f"胜率 {(cand.t10_exec>0).mean():.1%} P5 {cand.t10_exec.quantile(.05):+.2%}")
    print(f"  样本周: {sorted(cand.wk.unique())}")
cand2 = d[(d.board == "kcb20cm") & (d.ret20 <= -0.40)]
if len(cand2):
    print(f"  科创板&ret20<=-40%: N={len(cand2)} 中位 {cand2.t10_exec.median():+.2%} 胜率 {(cand2.t10_exec>0).mean():.0%}")

print("=" * 70)
print("Q3-H V日(07-10周)解剖：隔夜gap 与逐周全景")
# 隔夜 gap = entry_open / 信号日收盘 - 1（由 t10_close 反推信号日收盘）
path_c10 = p[p.k == 10].set_index(["code", "wk"])["c"]
d4 = d.dropna(subset=["t10_exec", "t10_close"]).copy()
d4["c10"] = [path_c10.get((r.code, r.wk), np.nan) for r in d4.itertuples()]
d4["c_sig"] = d4.c10 / (1 + d4.t10_close + COST)
d4["gap"] = d4.entry_open / d4.c_sig - 1
for wk in ["2026-07-10"]:
    g_ = d4[d4.wk == wk]
    print(f"  {wk}: N={len(g_)} 隔夜gap 中位 {g_.gap.median():+.2%} 均值 {g_.gap.mean():+.2%} 高开>3%比例 {(g_.gap>0.03).mean():.0%}")
    print(f"          close口径中位 {g_.t10_close.median():+.2%} → exec口径中位 {g_.t10_exec.median():+.2%}（gap损耗 {g_.t10_exec.median()-g_.t10_close.median():+.2%}）")
g_all = d4[d4.idx_prev1 >= 0.04]
print(f"  全体V次日组(N={len(g_all)}): gap 中位 {g_all.gap.median():+.2%}, 高开>3%比例 {(g_all.gap>0.03).mean():.0%}, "
      f"close→exec 口径损耗 {g_all.t10_exec.median()-g_all.t10_close.median():+.2%}")
print(f"  全样本(N={len(d4)}): gap 中位 {d4.gap.median():+.2%}, 口径损耗 {d4.t10_exec.median()-d4.t10_close.median():+.2%}")
# 46 周全景表
wk_stats = d3.groupby("wk").agg(N=("t10_exec", "size"), med=("t10_exec", "median"),
                                win=("t10_exec", lambda v: (v > 0).mean()),
                                imin=("idx_min10", "first"), kb=("kb", "first"))
print("\n  [46周全景: 周信号数/中位/胜率/指数min10]")
print(wk_stats.sort_values("med").head(8).to_string(float_format=lambda v: f"{v:+.2f}"))
print(wk_stats.sort_values("med").tail(5).to_string(float_format=lambda v: f"{v:+.2f}"))
