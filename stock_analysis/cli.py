"""命令行入口: python -m stock_analysis <list|show|run>

示例:
    python -m stock_analysis list
    python -m stock_analysis show momentum
    python -m stock_analysis run momentum --code 600519.SH
    python -m stock_analysis run momentum risk_profile --code 600519.SH --set window=30
    python -m stock_analysis run momentum --code 600519.SH --json
    python -m stock_analysis run momentum --code 600001.SH --demo   # 合成数据演示
"""
from __future__ import annotations

import argparse
import json
import logging
import sys


def _build_runner(args, use_demo: bool):
    if use_demo:
        from .testing import make_context

        from . import load_plugins

        ctx = make_context(n_stocks=3, years=6, seed=42)
        from .core.runner import Runner

        runner = Runner(load_plugins(), ctx, use_cache=not args.no_cache)
        return runner, ctx
    from analyzer.datastore import DataStore

    from . import load_plugins
    from .core.context import AnalysisContext
    from .core.runner import Runner

    store = DataStore(args.data_dir)
    ctx = AnalysisContext(store)
    runner = Runner(load_plugins(), ctx, use_cache=not args.no_cache)
    return runner, ctx


def _print_result(r) -> None:
    head = f"◆ {r.plugin} @ {r.code}"
    if r.plugin_version:
        head += f"  (v{r.plugin_version})"
    print(f"\n{head}  [{r.elapsed_ms}ms]")
    if not r.ok:
        print(f"  ✗ 失败: {r.error}")
        return
    if r.score is not None:
        print(f"  评分 {r.score}/100  结论: {r.verdict}")
    if r.summary:
        print(f"  {r.summary}")
    if r.metrics:
        labels = r.metric_labels or {}
        width = max(len(k) for k in list(r.metrics) + list(labels))
        for k, v in r.metrics.items():
            if v is None:
                continue
            print(f"    {labels.get(k, k):<{width}}  {v}")
    for s in r.signals:
        icon = {"info": "·", "ok": "✓", "warn": "⚠", "alert": "‼"}[s.level]
        print(f"    {icon} {s.text}")
    for name, rows in r.tables.items():
        print(f"  表格 {name}: {len(rows)} 行（--json 查看完整数据）")
        for row in rows[:3]:
            print(f"    {row}")
        if len(rows) > 3:
            print(f"    ... 其余 {len(rows) - 3} 行")
    if r.series:
        print("  序列: " + ", ".join(f"{k}({len(v['values'])}点)" for k, v in r.series.items()))


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(
        prog="stock_analysis",
        description="插件式个股分析框架（list/show/run）",
    )
    ap.add_argument("--data-dir", default="data", help="数据目录（默认 data/）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出全部已发现插件")
    p_list.add_argument("--category", help="按分类过滤（technical/risk/fundamental/...）")

    p_show = sub.add_parser("show", help="查看插件详情（元数据/参数）")
    p_show.add_argument("name")

    p_run = sub.add_parser("run", help="运行一个或多个插件")
    p_run.add_argument("plugins", nargs="+", help="插件名，可多个")
    p_run.add_argument("--code", required=True, help="股票代码（600519 / sh600519 / 600519.SH）")
    p_run.add_argument("--set", action="append", default=[], metavar="K=V",
                       help="传参（可重复）: --set window=30 --set index=000905.SH")
    p_run.add_argument("--json", action="store_true", help="输出完整 JSON 结果")
    p_run.add_argument("--no-cache", action="store_true", help="禁用结果缓存")
    p_run.add_argument("--demo", action="store_true",
                       help="使用合成数据演示（无需真实数据集）")

    args = ap.parse_args(argv)

    if args.cmd == "list":
        from . import load_plugins

        reg = load_plugins()
        rows = reg.meta_list()
        if args.category:
            rows = [x for x in rows if x["category"] == args.category]
        if not rows:
            print("（未发现插件）")
        for m in rows:
            ps = ", ".join(f"{k}={v['default']}" for k, v in m["params"].items())
            print(f"  {m['name']:<22} {m['display_name']:<10} [{m['category']}]  {m['description']}")
            if ps:
                print(f"  {'':<22} 参数: {ps}")
        for name, err in reg.discovery_failures:
            print(f"  ✗ 加载失败 {name}: {err}", file=sys.stderr)
        print(f"\n共 {len(rows)} 个插件")
        return 0

    if args.cmd == "show":
        from . import load_plugins

        reg = load_plugins()
        m = reg.info(args.name)
        if m is None:
            print(f"未找到插件 {args.name}（可用: {', '.join(reg.names()) or '无'}）", file=sys.stderr)
            return 1
        print(json.dumps(m, ensure_ascii=False, indent=2))
        return 0

    # run
    params = {}
    for item in args.set:
        if "=" not in item:
            print(f"--set 需要 K=V 形式: {item}", file=sys.stderr)
            return 2
        k, v = item.split("=", 1)
        params[k.strip()] = v.strip()

    runner, _ctx = _build_runner(args, args.demo)
    try:
        results = runner.run_many(args.plugins, args.code, **params)
    finally:
        _ctx.cleanup()

    if args.json:
        print(json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2))
    else:
        for r in results:
            _print_result(r)
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
