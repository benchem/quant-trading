"""多因子复合突破模型 —— 批量选股扫描示例。

对应 README「多因子量化复合技术模型」的中短线量化选股用法：
对一组股票运行 multi_factor_breakout 插件，输出三张表：

1) 今日触发复合信号的股票（突破+放量+低波动+多因子共振，按综合z排序）
2) 已突破但尚未确认的股票（哪个硬门没过，一眼看穿假突破风险）
3) 股票池历史信号的回测胜率（信号级合并，验证模型当前有效性）

用法:
    python examples/multi_factor_screen.py                 # 用 data/watchlist.json
    python examples/multi_factor_screen.py 600519 300750 000001
    python examples/multi_factor_screen.py --random 100    # 全市场随机抽 100 只
    python examples/multi_factor_screen.py --all           # 全市场扫描（较慢）
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from analyzer.datastore import DataStore  # noqa: E402
from stock_analysis import Runner, load_plugins  # noqa: E402
from stock_analysis.core.context import AnalysisContext  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def collect_codes(args) -> list[str]:
    codes: list[str] = []
    if args.all:
        codes = sorted(p.stem for p in (ROOT / "data" / "daily" / "hfq").glob("*.parquet"))
    elif args.random:
        all_codes = sorted(p.stem for p in (ROOT / "data" / "daily" / "hfq").glob("*.parquet"))
        codes = random.Random(42).sample(all_codes, min(args.random, len(all_codes)))
    else:
        for c in args.codes:
            codes.append(c)
        if not codes:
            # 缺省用自选股：兼容免登录共享模式(根级 watchlist.json)与
            # 多用户登录模式(data/users/*/watchlist.json)，取全部并集
            from analyzer.auth import all_watchlist_codes

            codes = all_watchlist_codes(ROOT / "data")
    return codes


def main() -> int:
    ap = argparse.ArgumentParser(description="多因子复合突破选股扫描")
    ap.add_argument("codes", nargs="*", help="股票代码（缺省用 watchlist.json）")
    ap.add_argument("--random", type=int, default=0, metavar="N",
                    help="全市场随机抽 N 只")
    ap.add_argument("--all", action="store_true", help="全市场扫描")
    ap.add_argument("--set", action="append", default=[], metavar="K=V",
                    help="传参给插件, 如 --set vol_mult=2.0")
    ap.add_argument("--top", type=int, default=15, help="表格最大行数")
    args = ap.parse_args()

    codes = collect_codes(args)
    if not codes:
        print("没有可扫描的股票（watchlist 为空）")
        return 1
    params = dict(kv.split("=", 1) for kv in args.set)

    store = DataStore(str(ROOT / "data"))
    ctx = AnalysisContext(store)
    runner = Runner(load_plugins(), ctx)

    fired, pending, pool_signals = [], [], []
    t0 = time.perf_counter()
    for i, code in enumerate(codes, 1):
        r = runner.run("multi_factor_breakout", code, **params)
        if not r.ok:
            continue
        m = r.metrics
        name = ctx.name(code)
        row = {
            "code": code, "name": name, "close": m.get("close"),
            "量比": m.get("vol_ratio"), "ATR%分位": m.get("squeeze_q"),
            "综合z": m.get("score_z"), "评分": r.score,
            "5日回测胜率": m.get("bt_win5"), "信号数": m.get("bt_count"),
        }
        if m.get("signal_today"):
            fired.append(row)
        elif m.get("gate_breakout"):
            gates = []
            if not m.get("gate_volume"):
                gates.append(f"量比{m.get('vol_ratio'):.2f}<1.8")
            if not m.get("gate_squeeze"):
                gates.append(f"波动未压缩(分位{m.get('squeeze_q'):.0%})")
            if m.get("score_z") is not None and m.get("score_z") < 1.0:
                gates.append(f"未共振(z={m.get('score_z'):.2f})")
            row["未过门"] = "、".join(gates) or "?"
            pending.append(row)
        # 股票池信号明细（信号级合并回测用）
        for srow in r.tables.get("signals", []):
            d = dict(srow)
            d["code"] = code
            pool_signals.append(d)

    el = time.perf_counter() - t0
    pd.set_option("display.unicode.east_asian_width", True)
    pd.set_option("display.width", 200)

    print(f"\n扫描 {len(codes)} 只 [{el:.1f}s]   参数: {params or '默认'}\n")

    print("━━━ 1) 今日触发复合突破信号 ━━━")
    if fired:
        df = pd.DataFrame(fired).sort_values("综合z", ascending=False)
        for c in ("ATR%分位",):
            df[c] = df[c].map(lambda v: f"{v:.0%}" if v is not None else "—")
        for c in ("5日回测胜率",):
            df[c] = df[c].map(lambda v: f"{v:.0%}" if v is not None else "—")
        print(df.head(args.top).to_string(index=False))
    else:
        print("（今日无全部门通过的信号）")

    print(f"\n━━━ 2) 已突破{params.get('lookback', '20')}日高点、待确认 ━━━")
    if pending:
        df = pd.DataFrame(pending).sort_values("综合z", ascending=False)
        print(df.head(args.top).to_string(index=False))
    else:
        print("（今日无突破）")

    print("\n━━━ 3) 股票池历史信号回测（信号级合并）━━━")
    if pool_signals:
        s = pd.DataFrame(pool_signals)
        s["index"] = pd.to_datetime(s["index"])
        print(f"历史信号 {len(s)} 个（{s['index'].min():%Y-%m-%d} ~ {s['index'].max():%Y-%m-%d}）")
        for h in (5, 10, 20):
            fwd = pd.to_numeric(s.get(f"fwd{h}"), errors="coerce").dropna()
            if len(fwd):
                print(f"  前瞻{h:>2}日: 胜率 {(fwd > 0).mean():.1%}  "
                      f"均值 {fwd.mean():+.2%}  n={len(fwd)}")
    else:
        print("（股票池内无历史信号）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
