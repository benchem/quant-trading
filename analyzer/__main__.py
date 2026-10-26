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
    p_serve.add_argument("--no-auth", action="store_true",
                         help="关闭多用户登录（免登录共享单用户模式，沿用根级 watchlist.json/trades.json）")

    p_user = sub.add_parser("user", help="多用户管理（list 列出 / add 新建 / passwd 重置密码）")
    p_user.add_argument("op", choices=["list", "add", "passwd"],
                        help="list 列出用户 / add 新建 / passwd 重置密码")
    p_user.add_argument("name", nargs="?", help="用户名（add/passwd 必填）")

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

    if args.cmd == "user":
        import getpass

        from .auth import AuthManager

        am = AuthManager(args.data_dir)
        if args.op == "list":
            users = am.users()
            if not users:
                print("（暂无用户；首次访问网页注册即创建，或在命令行 user add）")
            for u in users:
                print(f"  {u}")
            return 0
        if not args.name:
            ap.error(f"user {args.op} 需要用户名")
            return 2
        if args.op == "add":
            if am.user_exists(args.name):
                print(f"  ✗ 用户 {args.name} 已存在")
                return 1
            pw = getpass.getpass(f"为用户 {args.name} 设置密码: ")
            pw2 = getpass.getpass("重复密码: ")
            if pw != pw2:
                print("  ✗ 两次密码不一致")
                return 1
            try:
                info = am.create_user(args.name, pw)
            except ValueError as e:
                print(f"  ✗ {e}")
                return 1
            print(f"  ✓ 已创建用户 {info['name']}")
            return 0
        # passwd
        if not am.user_exists(args.name):
            print(f"  ✗ 用户 {args.name} 不存在")
            return 1
        pw = getpass.getpass(f"重置 {args.name} 的密码: ")
        pw2 = getpass.getpass("重复密码: ")
        if pw != pw2:
            print("  ✗ 两次密码不一致")
            return 1
        try:
            am.change_password(args.name, pw)
        except ValueError as e:
            print(f"  ✗ {e}")
            return 1
        print(f"  ✓ 已重置 {args.name} 的密码")
        return 0

    if args.cmd == "minute":
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
            from .auth import all_watchlist_codes

            codes = all_watchlist_codes(args.data_dir)
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

    # 不带子命令直跑时 argparse 不会填充 serve 的默认值，这里兜底
    serve(args.data_dir,
          getattr(args, "host", "127.0.0.1"),
          getattr(args, "port", 8765),
          auth_enabled=not getattr(args, "no_auth", False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
