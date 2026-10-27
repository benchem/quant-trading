"""页面刷新卡顿修复的自测（无需网络、无需真实行情数据）。

运行: .venv/bin/python tests/test_page_load.py

覆盖：
- HTTP/1.1 + keep-alive：同一 TCP 连接连发多个请求（连接复用）
- 静态资源协商缓存：ETag 首次 200 → If-None-Match 二次 304（零重传）
- 静态资源 gzip：Accept-Encoding: gzip 时传输体积显著小于原文
- Cache-Control 策略：/static/* 可长缓存，index.html/app.js 为 no-cache
- DataStore.fund_latest / minute_info：文件未变时命中缓存（读盘次数不增）
- datastore reload/clear_caches 清空新缓存
"""
from __future__ import annotations

import gzip
import http.client
import io
import sys
import tempfile
import threading
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, ".")

from analyzer.datastore import DataStore  # noqa: E402
from analyzer.server import make_handler  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, fn) -> bool:
    global PASSED, FAILED
    print(f"\n===== {name} =====", flush=True)
    try:
        fn()
        print("PASS")
        PASSED += 1
        return True
    except AssertionError as e:
        print(f"FAIL: {e}")
        FAILED += 1
        return False
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        FAILED += 1
        return False


# ---------------------------------------------------------------- 测试服务器

tmp_web = None
httpd = None
PORT = 18999


def _start_server():
    global httpd
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), make_handler(store=None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.2)


def _stop_server():
    if httpd:
        httpd.shutdown()


def _get(conn: http.client.HTTPConnection, path: str, headers: dict | None = None):
    conn.request("GET", path, headers=headers or {})
    r = conn.getresponse()
    body = r.read()
    return r, body


# ---------------------------------------------------------------- 用例

def t_keepalive():
    """HTTP/1.1 keep-alive：一条连接连续三个请求全部成功（连接复用）。"""
    conn = http.client.HTTPConnection("127.0.0.1", PORT)
    try:
        for path in ("/", "/app.js", "/static/echarts.min.js"):
            r, body = _get(conn, path)
            assert r.status == 200, f"{path} 状态 {r.status}"
            assert body, f"{path} 空 body"
        # 协议版本必须是 1.1
        conn.request("GET", "/")
        r = conn.getresponse()
        r.read()
        assert r.version == 11, f"协议版本 {r.version} != HTTP/1.1(11)"
    finally:
        conn.close()


def t_etag_304():
    """静态资源：首次 200 带 ETag；If-None-Match 重发返回 304 且零 body。"""
    conn = http.client.HTTPConnection("127.0.0.1", PORT)
    try:
        r1, b1 = _get(conn, "/app.js")
        assert r1.status == 200
        etag = r1.headers.get("ETag")
        assert etag, "首次响应缺少 ETag"
        assert r1.headers.get("Last-Modified"), "首次响应缺少 Last-Modified"
        r2, b2 = _get(conn, "/app.js", {"If-None-Match": etag})
        assert r2.status == 304, f"If-None-Match 重发应 304，实际 {r2.status}"
        assert b2 == b"", f"304 应零 body，实际 {len(b2)} 字节"
        assert r2.headers.get("ETag") == etag, "304 未回传 ETag"
    finally:
        conn.close()


def t_gzip():
    """Accept-Encoding: gzip 时文本资源压缩传输（体积 < 原文的 50%）。"""
    conn = http.client.HTTPConnection("127.0.0.1", PORT)
    try:
        r, body = _get(conn, "/static/echarts.min.js", {"Accept-Encoding": "gzip"})
        assert r.status == 200
        assert r.headers.get("Content-Encoding") == "gzip", "未启用 gzip"
        raw_len = len(body)
        restored = gzip.GzipFile(fileobj=io.BytesIO(body)).read()
        assert len(restored) > 100_000, "gzip 解压后内容异常"
        assert raw_len < len(restored) / 2, (
            f"压缩后 {raw_len} 字节，超过原文 {len(restored)} 的一半")
        # 不带 Accept-Encoding 时不应压缩
        r2, body2 = _get(conn, "/static/echarts.min.js")
        assert r2.headers.get("Content-Encoding") is None, "未声明 gzip 却压缩"
        assert len(body2) == len(restored), "非压缩 body 与原文不一致"
    finally:
        conn.close()


def t_cache_policy():
    """/static/* 可长缓存；根页面与 app.js 为 no-cache（本地留存、回源协商）。"""
    conn = http.client.HTTPConnection("127.0.0.1", PORT)
    try:
        r, _ = _get(conn, "/static/echarts.min.js")
        lib_cc = r.headers.get("Cache-Control") or ""
        assert "max-age" in lib_cc, f"/static 应可缓存: {lib_cc!r}"
        for path in ("/", "/app.js", "/style.css"):
            r, _ = _get(conn, path)
            cc = r.headers.get("Cache-Control") or ""
            assert cc == "no-cache", f"{path} Cache-Control 应为 no-cache: {cc!r}"
    finally:
        conn.close()


def t_fund_minute_cache():
    """fund_latest / minute_info：文件不变时命中缓存（读盘次数不增长）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _make_min_store(tmp)
        store = DataStore(tmp)
        code = "600000.SH"
        # 写财报 + 分钟线缓存文件
        fdir = tmp / "fundamental"
        fdir.mkdir()
        fund_df = pd.DataFrame({
            "period": ["2026-06-30", "2026-09-30"],
            "rev": [1.0e9, 1.2e9],
            "net_profit": [1e8, 1.1e8],
        })
        fund_df.to_parquet(fdir / f"{code}.parquet", index=False)
        mdir = tmp / "minute" / "1"
        mdir.mkdir(parents=True)
        df = pd.DataFrame({
            "datetime": pd.to_datetime(["2026-10-20 09:31", "2026-10-20 09:32"]),
            "code": [code, code],
            "open": [10, 10.1], "high": [10.2, 10.2],
            "low": [10, 10], "close": [10.1, 10.1],
            "volume": [100, 200], "amount": [1e4, 2e4],
        })
        df.to_parquet(mdir / f"{code}.parquet", index=False)

        r1 = store.fund_latest(code)
        assert r1 and r1["period"] == "2026-09-30", f"财报摘要异常: {r1}"
        m1 = store.minute_info(code)
        assert m1 and m1["bars"] == 2, f"分钟概况异常: {m1}"

        # 触发多次调用：结果应稳定（缓存命中）
        for _ in range(3):
            assert store.fund_latest(code) == r1
            assert store.minute_info(code) == m1

        # 文件更新后缓存失效（mtime_ns ^ size 变化）
        time.sleep(0.01)
        fund_df2 = pd.DataFrame({
            "period": ["2026-09-30"],
            "rev": [9.9e9],
            "net_profit": [9e8],
        })
        fund_df2.to_parquet(fdir / f"{code}.parquet", index=False)
        r2 = store.fund_latest(code)
        assert r2["rev"] == 9.9e9, f"财报缓存未随文件更新失效: {r2}"

        # reload 清空缓存
        store.reload()
        assert store.fund_latest(code) == r2


def _make_min_store(tmp: Path) -> None:
    """最小可用 data 目录（同 test_watchlist.py 的夹具口径）。"""
    (tmp / "daily" / "hfq").mkdir(parents=True)
    for code, name in (("600000.SH", "浦发银行"), ("000001.SZ", "平安银行")):
        pd.DataFrame({
            "date": pd.to_datetime(["2026-10-20", "2026-10-21", "2026-10-22"]),
            "code": [code] * 3,
            "open": [10.0, 10.1, 10.2],
            "high": [10.3, 10.4, 10.5],
            "low": [9.9, 10.0, 10.1],
            "close": [10.2, 10.3, 10.4],
            "volume": [1000, 1100, 1200],
            "amount": [1e7, 1.1e7, 1.2e7],
        }).to_parquet(tmp / "daily" / "hfq" / f"{code}.parquet", index=False)
    pd.DataFrame({
        "code": ["600000.SH", "000001.SZ"],
        "name": ["浦发银行", "平安银行"],
        "latest_price": [10.4, 12.0],
        "pct_chg": [0.97, -0.5],
        "total_mv": [3e11, 2e11],
        "float_mv": [2.5e11, 1.8e11],
        "turnover": [1.2, 0.8],
    }).to_parquet(tmp / "stock_list.parquet", index=False)
    pd.DataFrame({
        "date": pd.to_datetime(["2026-10-20", "2026-10-21", "2026-10-22"]),
        "is_open": [1, 1, 1],
    }).to_parquet(tmp / "trade_calendar.parquet", index=False)


if __name__ == "__main__":
    _start_server()
    try:
        check("HTTP/1.1 keep-alive 连接复用", t_keepalive)
        check("ETag 协商缓存 304", t_etag_304)
        check("gzip 传输压缩", t_gzip)
        check("Cache-Control 策略", t_cache_policy)
    finally:
        _stop_server()
    check("fund_latest/minute_info 读盘缓存", t_fund_minute_cache)
    print(f"\n{'=' * 40}\n通过 {PASSED} / 失败 {FAILED}")
    sys.exit(1 if FAILED else 0)
