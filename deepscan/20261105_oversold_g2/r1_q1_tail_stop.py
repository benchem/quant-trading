#!/usr/bin/env python3
"""R1 评审 · Q1：无止损回测左尾 + -10% 灾难止损模拟（反弹错失量化）。"""
import numpy as np
import pandas as pd

OUT = "/root/workspaces/quant-trading/deepscan/20261105_oversold_g2"
d = pd.read_parquet(f"{OUT}/r1_signals.parquet")
p = pd.read_parquet(f"{OUT}/r1_path.parquet")

pd.set_option("display.width", 200)

print("=" * 70)
print("Q1-A 无止损 T+10（执行口径 entry=次日开盘）收益分布左尾")
s = d["t10_exec"].dropna()
print(f"N={len(s)} 均值 {s.mean():+.2%} 中位 {s.median():+.2%} 胜率 {(s>0).mean():.1%}")
for q in [0.01, 0.05, 0.10, 0.25]:
    print(f"  P{int(q*100):02d} = {s.quantile(q):+.2%}")
print(f"  最差 = {s.min():+.2%}")
worst10 = d.nsmallest(10, "t10_exec")[["code", "wk", "board", "ret20", "cpos", "t10_exec", "stop_k"]]
print("\n最差 10 个样本：")
print(worst10.to_string(index=False, float_format=lambda x: f"{x:.3f}"))

print("=" * 70)
print("Q1-B -10% 灾难止损模拟（对 entry=次日开盘价）")
n = len(d)
stop = d[d.stop_k.notna()].copy()
touch = d[d.touch_k.notna()].copy()
print(f"收盘触发止损: {len(stop)}/{n} = {len(stop)/n:.1%}   盘中触及-10%: {len(touch)}/{n} = {len(touch)/n:.1%}")
print(f"盘中触及但收盘收回(假触发): {d.touched_not_closed.sum()}/{len(touch)} = {d.touched_not_closed.sum()/max(len(touch),1):.1%}")
print(f"触发日分布: {stop.stop_k.value_counts().sort_index().to_dict()}")

print(f"\n—— 止损触发组（N={len(stop)}）若不止损的最终结局 ——")
print(f"  最终 t10 中位 {stop.t10_exec.median():+.2%} 均值 {stop.t10_exec.mean():+.2%}")
print(f"  最终好于-10%（止损帮倒忙）: {(stop.t10_exec > stop.stop_exit_c).mean():.1%}")
print(f"  最终回到水上(>0): {(stop.t10_exec > 0).mean():.1%}")
print(f"  最终比止损更差: {(stop.t10_exec < stop.stop_exit_c).mean():.1%}")
print(f"  止损退出收益(收盘卖) 中位 {stop.stop_exit_c.median():+.2%} 均值 {stop.stop_exit_c.mean():+.2%}")
print(f"  止损退出收益(次日开卖) 中位 {stop.stop_exit_no.median():+.2%} 均值 {stop.stop_exit_no.mean():+.2%}")

print(f"\n—— 触发-10%后至T+10的反弹（止损价→终点收盘，不加成本） ——")
r = stop.rebound_c.dropna()
print(f"  中位 {r.median():+.2%} 均值 {r.mean():+.2%} 最大 {r.max():+.2%} 最小 {r.min():+.2%}")
print(f"  反弹>0（错失反弹）占比 {(r>0).mean():.1%}；反弹>+10%占比 {(r>0.10).mean():.1%}")
rn = stop.rebound_no.dropna()
print(f"  [次日开卖版] 中位 {rn.median():+.2%} 均值 {rn.mean():+.2%} >0占比 {(rn>0).mean():.1%}")

print(f"\n—— 全样本分布：无止损 vs 有止损 ——")
for col, lab in [("t10_exec", "无止损"), ("stop_exit_c", "止损@收盘卖"), ("stop_exit_no", "止损@次日开卖")]:
    x = d[col].fillna(d.t10_exec).dropna()
    print(f"  {lab:10s}: 均值 {x.mean():+.2%} 中位 {x.median():+.2%} 胜率 {(x>0).mean():.1%} "
          f"P5 {x.quantile(.05):+.2%} 最差 {x.min():+.2%}")
ev_c = (d.stop_exit_c.fillna(d.t10_exec) - d.t10_exec).mean()
ev_no = (d.stop_exit_no.fillna(d.t10_exec) - d.t10_exec).mean()
print(f"  止损对期望收益的净影响: 收盘卖 {ev_c:+.3%} / 次日开卖 {ev_no:+.3%} （每信号）")
x = d.stop_exit_c.fillna(d.t10_exec)
print(f"  组合级(1万/槽): 止损版 P5 单槽亏 {abs(x.quantile(.05))*10000:,.0f} 元, 无止损版 P5 单槽亏 {abs(d.t10_exec.quantile(.05))*10000:,.0f} 元")

print("=" * 70)
print("Q1-C 板块结构（20cm gap 穿透风险）")
g = d.groupby("board").agg(
    N=("t10_exec", "size"), t10中位=("t10_exec", "median"), 胜率=("t10_exec", lambda x: (x > 0).mean()),
    止损率=("stop_k", lambda x: x.notna().mean()), 盘中触及率=("touch_k", lambda x: x.notna().mean()))
print(g.to_string(float_format=lambda x: f"{x:.3f}"))
# 持有期内单日收盘跌幅分布（20cm vs 10cm）
big = p[p.ret_from_entry <= -0.10].groupby(["code", "wk"]).size()
codes_20 = set(d[d.board != "mb10cm"].code)
print(f"持有期内出现单日收盘<=-10%的仓位: {len(big)} 个; 其中20cm板块 {sum(1 for c_,_ in big.index if c_ in codes_20)} 个")

print("=" * 70)
print("Q1-D 候选筛选维度检验（实盘挑选规则 vs 回测分布）")
d["depth"] = pd.cut(d.ret20, [-1, -0.45, -0.40, -0.35, -0.30, -0.20])
g2 = d.groupby("depth", observed=True).agg(
    N=("t10_exec", "size"), t10中位=("t10_exec", "median"), t10均值=("t10_exec", "mean"),
    胜率=("t10_exec", lambda x: (x > 0).mean()), P5=("t10_exec", lambda x: x.quantile(.05)))
print("[按 ret20 深度分桶]")
print(g2.to_string(float_format=lambda x: f"{x:+.3f}"))
g3 = d.groupby(pd.cut(d.amount_yi, [0, 2, 5, 100]), observed=True).agg(
    N=("t10_exec", "size"), t10中位=("t10_exec", "median"), 胜率=("t10_exec", lambda x: (x > 0).mean()))
print("\n[按信号日成交额分桶——实盘加了额>=2亿过滤，回测没有]")
print(g3.to_string(float_format=lambda x: f"{x:+.3f}"))
