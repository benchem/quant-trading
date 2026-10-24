"""CLI 入口: python -m analyzer [serve|report]"""
from __future__ import annotations

import argparse
import sys
import time


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="analyzer",
        description="A股本地数据分析工具（数据来自 downloader 落盘的 Parquet）",
    )
    ap.add_argument("--data-dir", default="data", help="数据目录（默认 data/）")
    sub = ap.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="启动本地交互式仪表盘（默认命令）")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)

    p_report = sub.add_parser("report", help="输出 matplotlib 静态 PNG 报告")
    p_report.add_argument("--out", default="analyzer/output")
    p_report.add_argument("--years", type=int, default=10, help="图表回顾年数（默认 10）")

    p_fund = sub.add_parser("fundamental", help="抓取季度/年报基本面（同花顺，缓存到 data/fundamental/）")
    p_fund.add_argument("--codes", help="指定代码，逗号分隔（默认 --all 时全市场）")
    p_fund.add_argument("--all", action="store_true", help="批量抓取全部股票")
    p_fund.add_argument("--force", action="store_true", help="忽略缓存重新抓取")
    p_fund.add_argument("--rate", type=float, default=2.0, help="全局请求速率上限 次/秒（默认 2）")
    p_fund.add_argument("--workers", type=int, default=3, help="并发线程数（默认 3）")

    p_min = sub.add_parser("minute", help="同步自选股分钟线（新浪 1 分钟 K 线，真实价，缓存到 data/minute/1/）")
    p_min.add_argument("--codes", help="指定代码，逗号分隔（默认同步全部自选股）")

    args = ap.parse_args(argv)

    if args.cmd == "report":
        from .report import report

        report(args.data_dir, args.out, args.years)
        return 0

    if args.cmd == "fundamental":
        from .fundamental import fetch, prefetch_all

        if args.codes:
            raw_codes = [c.strip() for c in args.codes.split(",") if c.strip()]
            from .datastore import _norm_code

            codes = []
            for c in raw_codes:
                try:
                    codes.append(_norm_code(c))
                except ValueError as e:
                    print(f"  ✗ {e}")
            if not codes:
                return 1
            ok, failed = 0, []
            for c in codes:
                try:
                    df = fetch(c, args.data_dir, force=args.force)
                    print(f"  ✓ {c}: {len(df)} 个报告期（{df['period'].iloc[0].date()} ~ {df['period'].iloc[-1].date()}）")
                    ok += 1
                except Exception as e:  # noqa: BLE001
                    print(f"  ✗ {c}: {e}")
                    failed.append(c)
            print(f"完成: 成功 {ok}，失败 {len(failed)}")
            return 1 if failed else 0

        if args.all:
            print("批量抓取基本面（同花顺），失败会写入 data/fundamental/failed.txt")
            t0 = time.time()
            res = prefetch_all(
                args.data_dir, force=args.force, rate=args.rate, workers=args.workers,
                progress_cb=lambda done, total, code, status, err: print(
                    f"\r  {done}/{total}  {code}  {status}" + (f"  {err}" if err else ""), end="", flush=True
                ),
            )
            print(f"\n完成: 成功 {res['ok']} | 缓存跳过 {res['skipped']} | 失败 {res['failed']} | "
                  f"耗时 {time.time() - t0:.0f}s")
            if res["failed_codes"]:
                print(f"失败清单: data/fundamental/failed.txt（{res['failed'][:5]} 等 {len(res['failed_codes'])} 只，重跑自动补齐）")
            return 0

        ap.error("需要 --codes 或 --all")
        return 2

    if args.cmd == "minute":
        import json
        from pathlib import Path

        from .minute import fetch, info

        if args.codes:
            from .datastore import _norm_code

            codes = []
            for c in (s.strip() for s in args.codes.split(",")):
                if not c:
                    continue
                try:
                    codes.append(_norm_code(c))
                except ValueError as e:
                    print(f"  ✗ {e}")
        else:
            try:
                codes = json.loads((Path(args.data_dir) / "watchlist.json").read_text())["codes"]
            except Exception:
                codes = []
            if not codes:
                print("自选股为空（先在网页 ★ 加自选，或用 --codes 指定）")
                return 0
            print(f"同步自选股 {len(codes)} 只的分钟线…")
        ok = fail = 0
        for c in codes:
            try:
                fetch(c, args.data_dir)
                i = info(c, args.data_dir)
                print(f"  ✓ {c}: 本地累计 {i['bars']} 根（{i['first']} ~ {i['last']}）")
                ok += 1
            except Exception as e:  # noqa: BLE001
                print(f"  ✗ {c}: {e}")
                fail += 1
            time.sleep(1.2)
        print(f"完成: 成功 {ok}，失败 {fail}")
        return 1 if fail else 0

    # 默认 / serve
    from .server import serve

    serve(args.data_dir, args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
