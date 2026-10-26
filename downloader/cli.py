"""命令行入口。

用法示例:
    python -m downloader all                # 一键更新: 列表+日历+板块+全部日线+指数
    python -m downloader board              # 刷新行业板块成分表（排行榜「板块」功能的数据）
    python -m downloader board --with-concept  # 追加概念板块（东财题材/风格标签）
    python -m downloader stocks --codes 600519,000858
    python -m downloader stocks --start 20180101 --adjust qfq
    python -m downloader index
    python -m downloader build              # 合并为单个建模数据集
    python -m downloader status
"""
from __future__ import annotations

import argparse

from . import config
from .app import Downloader, _log


def _add_common(p: argparse.ArgumentParser, with_range: bool = False,
                with_rotate: bool = False) -> None:
    p.add_argument("--data-dir", default=str(config.DEFAULT_DATA_DIR), help="数据落盘目录")
    if with_range:
        p.add_argument(
            "--adjust",
            default=config.DEFAULT_ADJUST,
            choices=list(config.ADJUST_CHOICES),
            help="复权方式: hfq=后复权(建模推荐) qfq=前复权 none=不复权",
        )
        p.add_argument("--start", default=config.DEFAULT_START, help="历史数据起始日期 YYYYMMDD")
    p.add_argument("--workers", type=int, default=config.DEFAULT_WORKERS, help="并发线程数")
    p.add_argument("--rate", type=float, default=config.DEFAULT_RATE, help="请求速率上限(次/秒)，0=不限速")
    if with_rotate:
        p.add_argument(
            "--rotate", action="store_true",
            help="数据源限流(403/429/501)时自动切换 Clash 节点换出口 IP",
        )


def _add_range_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--end", default=None, help="结束日期 YYYYMMDD（默认今天）")
    p.add_argument("--full", action="store_true", help="忽略本地已有日期，从起始日期整段重下")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ashare-dl",
        description="中国大陆A股交易数据下载器：东方财富/腾讯行情源 -> 本地 Parquet（建模友好）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="命令")

    ps = sub.add_parser("stocks", help="下载/更新A股日线行情",
                        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    _add_common(ps, with_range=True, with_rotate=True)
    _add_range_opts(ps)
    ps.add_argument("--codes", default=None, help="只下载指定代码，逗号分隔，如 600519,000858")
    ps.add_argument("--market", default="all", choices=["all", "sh", "sz", "bj"], help="限定市场")
    ps.add_argument("--limit", type=int, default=None, help="只处理前 N 只（调试用）")

    pi = sub.add_parser("index", help="下载/更新主要指数日线",
                        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    _add_common(pi)
    _add_range_opts(pi)
    pi.add_argument(
        "--indices",
        default=None,
        help="逗号分隔的指数代码（带市场后缀），如 000001.SH,000300.SH；默认下载内置主要宽基指数",
    )

    pl = sub.add_parser("list", help="刷新全部A股基础列表（代码/名称/市值）")
    _add_common(pl)

    pc = sub.add_parser("calendar", help="刷新交易日历")
    _add_common(pc)

    pbo = sub.add_parser(
        "board",
        help="刷新板块成分表（行业板块：东财行业分类，每只股票归属一个板块；--with-concept 追加概念板块）",
    )
    pbo.add_argument("--with-concept", action="store_true",
                     help="同时刷新概念板块（东财题材/风格标签，成分有重叠，约多 2 分钟；东财不可用时保留旧缓存）")
    _add_common(pbo)

    pb = sub.add_parser("build", help="合并全部日线 -> 单个建模数据集 parquet")
    _add_common(pb, with_range=True)

    pst = sub.add_parser("status", help="查看本地数据状态")
    _add_common(pst, with_range=True)

    pa = sub.add_parser("all", help="一键更新: 列表+日历+板块+全部日线+指数",
                        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    pa.add_argument("--skip-board", action="store_true", help="跳过行业板块成分表刷新")
    _add_common(pa, with_range=True, with_rotate=True)
    _add_range_opts(pa)
    pa.add_argument("--codes", default=None, help="只下载指定代码，逗号分隔，如 600519,000858")
    pa.add_argument("--market", default="all", choices=["all", "sh", "sz", "bj"], help="限定市场")
    pa.add_argument("--limit", type=int, default=None, help="只处理前 N 只（调试用）")

    pp = sub.add_parser("proxy", help="查看/切换 Clash 出口节点（配合 --rotate 使用）",
                        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    pp.add_argument("--list", action="store_true", help="列出可选节点")
    pp.add_argument("--set", dest="set_node", default=None,
                    help="切换节点（支持子串匹配，如 '日本Z01'）")
    return p


def _dl(args) -> Downloader:
    return Downloader(args.data_dir, getattr(args, "adjust", None), getattr(args, "start", None),
                      args.workers, args.rate, rotate=getattr(args, "rotate", False))


def _proxy_cmd(args) -> None:
    from .proxy import ClashAPI

    clash = ClashAPI.find()
    if clash is None:
        _log("未找到 Clash API（可用 CLASH_API / CLASH_API_UNIX 环境变量指定）")
        return
    if args.set_node:
        _set_proxy_node(clash, args.set_node)
        return
    # 默认：显示当前状态
    from .proxy import ProxyRotator
    try:
        rot = ProxyRotator(clash, verbose=False)
        cur = clash.current(rot.group)
        _log(f"分组: {rot.group} | 当前节点: {cur or '-'} | 可选 {len(rot.pool)} 个")
        ip = clash.egress_ip()
        _log(f"当前出口 IP: {ip or '(检测失败)'}")
    except Exception as e:  # noqa: BLE001
        _log(f"读取节点信息失败: {e}")
        return
    if args.list:
        for i, n in enumerate(rot.pool):
            _log(f"  {i + 1:>2}. {n}")


def _set_proxy_node(clash, pattern: str) -> None:
    from .proxy import ProxyRotator

    rot = ProxyRotator(clash, verbose=False)
    if pattern in rot.pool:
        name = pattern
    else:
        matches = [n for n in rot.pool if pattern in n]
        if len(matches) == 1:
            name = matches[0]
        elif not matches:
            _log(f"节点池中无匹配 '{pattern}' 的节点")
            return
        else:
            _log(f"'{pattern}' 匹配多个节点，请更精确:")
            for m in matches:
                _log(f"  - {m}")
            return
    clash.set_node(rot.group, name)
    import time

    time.sleep(1.0)
    ip = clash.egress_ip()
    _log(f"已切换: {rot.group} -> {name} | 出口 IP: {ip or '(检测失败)'}")


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    if args.cmd == "proxy":
        _proxy_cmd(args)
        return
    dl = _dl(args)
    if args.cmd == "list":
        dl.refresh_stock_list()
    elif args.cmd == "calendar":
        dl.refresh_trade_calendar()
    elif args.cmd == "board":
        dl.refresh_boards(with_concept=getattr(args, "with_concept", False))
    elif args.cmd == "stocks":
        dl.update_stocks(
            codes=args.codes, end=args.end, full=args.full, market=args.market, limit=args.limit
        )
    elif args.cmd == "index":
        dl.update_indices(indices=args.indices, end=args.end, full=args.full)
    elif args.cmd == "build":
        dl.build_dataset()
    elif args.cmd == "status":
        dl.status()
    elif args.cmd == "all":
        dl.refresh_stock_list()
        try:
            dl.refresh_trade_calendar()
        except Exception as e:  # noqa: BLE001
            _log(f"交易日历刷新失败: {e}")
        dl.update_stocks(
            codes=args.codes, end=args.end, full=args.full, market=args.market, limit=args.limit
        )
        dl.update_indices(end=args.end, full=args.full)
        if not args.skip_board:
            try:
                dl.refresh_boards()
            except Exception as e:  # noqa: BLE001 - 板块失败不阻塞主数据
                _log(f"行业板块刷新失败（不影响其他数据，可稍后运行 python -m downloader board 补齐）: {e}")
        _log("提示: 运行 `python -m downloader build` 生成合并建模数据集")


if __name__ == "__main__":
    main()
