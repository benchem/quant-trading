"""实测 AkShare 数据源接口（需要网络）。运行: .venv/bin/python tests/test_source_live.py"""
from __future__ import annotations

import sys
import traceback

sys.path.insert(0, ".")
from downloader import source  # noqa: E402


def check(name: str, fn):
    print(f"\n===== {name} =====", flush=True)
    try:
        df = fn()
        print(f"OK: {type(df).__name__} rows={len(df)}")
        print(df.head(3).to_string())
        print("columns:", list(df.columns))
        return True
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return False


def main():
    results = {}

    results["stock_list"] = check(
        "股票列表 stock_zh_a_spot_em",
        lambda: source.fetch_stock_list(),
    )
    results["daily_hfq"] = check(
        "个股日线(后复权) 600519.SH",
        lambda: source.fetch_daily("600519.SH", "20240101", "20240131", "hfq"),
    )
    results["daily_qfq"] = check(
        "个股日线(前复权) 000858.SZ",
        lambda: source.fetch_daily("000858.SZ", "20240101", "20240131", "qfq"),
    )
    results["daily_none"] = check(
        "个股日线(不复权) 300750.SZ",
        lambda: source.fetch_daily("300750.SZ", "20240101", "20240115", ""),
    )
    def bj_daily():
        sl = source.fetch_stock_list()
        bj = sl[sl["code"].str.endswith(".BJ")]
        if len(bj) == 0:
            print("  (列表中无北交所股票，跳过实际请求)")
            return sl.head(0)
        code = bj.iloc[0]["code"]
        print(f"  选取北交所股票: {code} {bj.iloc[0]['name']}")
        return source.fetch_daily(code, "20240101", "20240115", "")

    results["daily_bj"] = check("北交所日线（动态取一只）", bj_daily)
    results["index"] = check(
        "指数日线 沪深300",
        lambda: source.fetch_index_daily("000300.SH", "20240101", "20240115"),
    )
    results["trade_dates"] = check(
        "交易日历",
        lambda: source.fetch_trade_dates().to_frame("trade_date"),
    )

    print("\n===== 汇总 =====")
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
    print("\nALL PASS" if all(results.values()) else "\nHAS FAILURES")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
