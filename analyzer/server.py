"""本地 HTTP 服务：静态仪表盘 + JSON API（纯标准库，无额外依赖）。"""
from __future__ import annotations

import json
import mimetypes
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .datastore import DataStore, _f, _r, _UNSET

WEB_DIR = Path(__file__).resolve().parent / "web"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class _AutoReloadRunner:
    """Runner 代理: 监测插件目录文件集，变化时自动重建注册表。

    新增/删除插件 .py 文件后**无需重启服务器**（下一次 API 请求即生效）；
    修改已有插件文件的内容仍需重启进程（Python import 缓存不重读已加载模块）。
    每次请求只做一次目录 glob（几个文件，开销可忽略），带锁防并发重建。
    """

    def __init__(self, build, plugins_dirs):
        self._build = build                    # () -> Runner
        self._dirs = list(plugins_dirs)
        self._stamp = self._scan()
        self._lock = threading.Lock()
        self._runner = build()

    def _scan(self) -> list[str]:
        files: list[str] = []
        for d in self._dirs:
            try:
                files += sorted(p.name for p in d.glob("*.py"))
            except OSError:
                pass
        return files

    def _refresh(self):
        stamp = self._scan()
        if stamp == self._stamp:
            return
        with self._lock:
            if stamp != self._stamp:           # 双检：并发请求只重建一次
                self._stamp = stamp
                try:
                    runner = self._build()
                    print(f"插件目录变化，已重新发现 "
                          f"{len(runner.registry.names())} 个插件: "
                          f"{', '.join(runner.registry.names())}")
                    self._runner = runner
                except Exception as e:  # noqa: BLE001 重建失败沿用旧注册表
                    print(f"警告: 插件重新发现失败，沿用旧注册表（{e}）")

    # ---- Runner 接口代理（每次调用前检查目录变化）----
    @property
    def registry(self):
        self._refresh()
        return self._runner.registry

    def run(self, *args, **kwargs):
        self._refresh()
        return self._runner.run(*args, **kwargs)

    def list_plugins(self, *args, **kwargs):
        self._refresh()
        return self._runner.list_plugins(*args, **kwargs)


def _safe_static(path: str) -> Path | None:
    """防目录穿越地解析静态文件路径。"""
    if path == "/" or path == "":
        p = WEB_DIR / "index.html"
    else:
        rel = path.lstrip("/")
        p = (WEB_DIR / rel).resolve()
        if not str(p).startswith(str(WEB_DIR.resolve())):
            return None
    return p if p.is_file() else None


def make_handler(store: DataStore, runner=None, syncer=None):
    """runner: stock_analysis.core.runner.Runner（插件分析）；None 时禁用 /api/plugins*。
    syncer: analyzer.minute.MinuteSyncer（自选股分钟线后台同步）；None 时禁用。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "ashare-analyzer/1.0"

        def log_message(self, fmt, *args):  # 精简日志
            sys.stderr.write(
                f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}\n"
            )

        # ------------------------------------------------------------ helpers

        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8")

        def _params(self) -> dict:
            # 兼容浏览器 percent-encoding 与 curl 直发原始 UTF-8 字节两种写法
            raw = self.path.encode("iso-8859-1", "ignore")
            parsed = urlparse(raw.decode("utf-8", "replace"))
            q = parsed.query
            return {k: v[-1] for k, v in parse_qs(q, encoding="utf-8").items()}

        # ---------------------------------------------------------------- API

        def _api(self, path: str, params: dict):
            try:
                if path == "/api/meta":
                    self._json(store.meta)
                elif path == "/api/watchlist":
                    self._json({
                        "codes": store.watchlist(),
                        "syncing": syncer.pending() if syncer else [],
                    })
                elif path == "/api/stocks":
                    self._json(store.search_stocks(params.get("q", ""), int(params.get("limit", 30))))
                elif path == "/api/kline":
                    code = (params.get("code") or "").upper()
                    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
                        return self._json({"error": "code 格式应为 600519.SH"}, 400)
                    data = store.kline(code, params.get("start"), params.get("end"))
                    if data is None:
                        return self._json({"error": f"未找到 {code} 的日线数据"}, 404)
                    self._json(data)
                elif path == "/api/market":
                    self._json(store.market_series(params.get("start"), params.get("end")))
                elif path == "/api/rank":
                    start, end = params.get("start"), params.get("end")
                    if not start or not end:
                        return self._json({"error": "需要 start/end 参数"}, 400)
                    self._json(
                        store.rank(
                            start,
                            end,
                            params.get("metric", "ret"),
                            int(params.get("n", 30)),
                            params.get("asc", "0") == "1",
                        )
                    )
                elif path == "/api/chips":
                    code = (params.get("code") or "").upper()
                    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
                        return self._json({"error": "code 格式应为 600519.SH"}, 400)
                    data = store.chips(code)
                    if data is None:
                        return self._json({"error": f"未找到 {code} 的日线数据"}, 404)
                    self._json(data)
                elif path == "/api/fundamental":
                    code = (params.get("code") or "").upper()
                    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
                        return self._json({"error": "code 格式应为 600519.SH"}, 400)
                    if code not in store.basic.index:
                        return self._json({"error": f"{code} 不在股票列表中（可能已退市）"}, 404)
                    try:
                        df = store.fundamental(code, params.get("refresh") == "1")
                    except Exception as e:  # FundamentalError 及其他
                        return self._json({"error": f"财报数据不可用: {str(e)[:120]}"}, 502)
                    if df.empty:
                        return self._json({"error": f"未找到 {code} 的财报数据"}, 404)
                    periods = df["period"].dt.strftime("%Y-%m-%d").tolist()
                    out = {"code": code, "periods": periods, "n": len(df)}
                    for col in df.columns:
                        if col in ("code", "period"):
                            continue
                        out[col] = _f(df[col].tolist())
                    self._json(out)
                elif path == "/api/trades":
                    code = (params.get("code") or "").upper() or None
                    ts = store.trades(code)
                    # 附带股票名称/最新价快照，供交易记录页展示与浮动盈亏计算
                    meta = {}
                    for t in ts:
                        c = t["code"]
                        if c in meta or c not in store.basic.index:
                            continue
                        row = store.basic.loc[c]
                        meta[c] = {"name": row.get("name"), "latest_price": _r(row.get("latest_price"))}
                    self._json({"code": code, "trades": ts, "meta": meta})
                elif path == "/api/news":
                    code = (params.get("code") or "").upper()
                    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
                        return self._json({"error": "code 格式应为 600519.SH"}, 400)
                    try:
                        data = store.news(code, refresh=params.get("refresh") == "1")
                    except Exception as e:  # NewsError 及其他
                        return self._json({"error": f"新闻数据不可用: {str(e)[:120]}"}, 502)
                    self._json(data)
                elif path == "/api/index_kline":
                    code = (params.get("code") or "").upper()
                    data = store.index_kline(code, params.get("start"), params.get("end"))
                    if data is None:
                        return self._json({"error": f"未找到指数 {code}"}, 404)
                    self._json(data)
                elif path == "/api/minute":
                    code = (params.get("code") or "").upper()
                    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
                        return self._json({"error": "code 格式应为 600519.SH"}, 400)
                    date = params.get("date") or ""
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                        return self._json({"error": "date 格式应为 YYYY-MM-DD"}, 400)
                    self._json(self._minute_view(code, date))
                elif path == "/api/plugins":
                    if runner is None:
                        return self._json({"error": "插件分析未启用"}, 503)
                    self._json({
                        "plugins": runner.list_plugins(),
                        "failures": runner.registry.discovery_failures,
                    })
                elif path.startswith("/api/plugins/"):
                    if runner is None:
                        return self._json({"error": "插件分析未启用"}, 503)
                    name = path[len("/api/plugins/"):]
                    meta = runner.registry.info(name)
                    if meta is None:
                        return self._json({"error": f"未找到插件 {name}"}, 404)
                    self._json(meta)
                elif path == "/api/analysis":
                    if runner is None:
                        return self._json({"error": "插件分析未启用"}, 503)
                    plugin = params.get("plugin") or ""
                    code = params.get("code") or ""
                    if not plugin or not code:
                        return self._json({"error": "需要 plugin 和 code 参数"}, 400)
                    # 其余 query 参数当作插件参数（类型由插件声明决定）
                    pparams = {
                        k: v for k, v in params.items() if k not in ("plugin", "code")
                    }
                    result = runner.run(plugin, code, **pparams)
                    self._json(result.to_dict())
                else:
                    self._json({"error": "unknown api"}, 404)
            except Exception:
                traceback.print_exc()
                self._json({"error": "internal error"}, 500)

        # -------------------------------------------------------------- routes

        def _minute_view(self, code: str, date: str) -> dict:
            """某交易日的 1 分钟 K 线视图（本地 data/minute/1/{code}.parquet，真实价）。"""
            from . import minute as minute_mod

            syncing = syncer is not None and code in syncer.pending()
            df = minute_mod.load(code, store.root)
            base = {"ok": False, "code": code, "date": date, "syncing": syncing}
            if df.empty:
                base["days"] = []
                base["error"] = (f"{code} 无本地分钟线（分钟线对自选股自动后台同步；"
                                 f"可点「立即拉取」从新浪抓取最近约 8 个交易日）")
                return base
            days = sorted(df["datetime"].dt.strftime("%Y-%m-%d").unique().tolist())
            base["days"] = days
            day = df[df["datetime"].dt.strftime("%Y-%m-%d") == date]
            if day.empty:
                rng = f"{days[0]} ~ {days[-1]}" if len(days) > 1 else days[0]
                base["error"] = (f"{date} 无分钟数据（本地覆盖 {rng}，共 {len(days)} 个交易日；"
                                 f"仅自选股自动同步，或点「立即拉取」抓最近约 8 个交易日）")
                return base
            day = day.sort_values("datetime", kind="stable")
            # 前一交易日收盘（分钟数据内最近一个更早的收盘，真实价口径）
            prev_close = None
            earlier = df[df["datetime"] < day["datetime"].iloc[0]]
            if not earlier.empty:
                prev_close = _r(earlier["close"].iloc[-1])
            # 均价线 VWAP = 累计成交额 / 累计成交量（量为 0 的分钟置 NaN）
            vol_cs = day["volume"].cumsum().replace(0, float("nan"))
            vwap = day["amount"].cumsum() / vol_cs
            return {
                "ok": True, "code": code, "date": date, "n": int(len(day)),
                "days": days, "syncing": syncing, "prev_close": prev_close,
                "time": day["datetime"].dt.strftime("%H:%M").tolist(),
                "open": _f(day["open"].tolist()),
                "high": _f(day["high"].tolist()),
                "low": _f(day["low"].tolist()),
                "close": _f(day["close"].tolist()),
                "volume": _f(day["volume"].tolist()),
                "amount": _f(day["amount"].tolist()),
                "vwap": _f(vwap.tolist()),
            }

        def _post_minute(self):
            """触发某股分钟线同步（新浪源，单次拉取最近约 8 个交易日并增量合并）。"""
            if syncer is None:
                return self._json({"error": "分钟线同步未启用"}, 503)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                req = json.loads(body.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                return self._json({"error": "请求体应为 JSON"}, 400)
            code = (req.get("code") or "").upper()
            if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
                return self._json({"error": "code 格式应为 600519.SH"}, 400)
            syncer.submit(code)
            self._json({"ok": True, "code": code, "queued": True,
                        "pending": syncer.pending()})

        def _post_watchlist(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                req = json.loads(body.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                return self._json({"error": "请求体应为 JSON"}, 400)
            action = req.get("action")
            try:
                if action == "add":
                    codes, added = store.watchlist_add(req.get("code", ""))
                    if syncer:
                        syncer.submit(req.get("code", "").upper())
                    self._json({"codes": codes, "added": added})
                elif action == "remove":
                    codes, removed = store.watchlist_remove(req.get("code", ""))
                    self._json({"codes": codes, "removed": removed})
                elif action == "clear":
                    with store._wl_lock:
                        store._wl_codes = []
                        store._save_watchlist()
                    self._json({"codes": [], "cleared": True})
                else:
                    self._json({"error": "action 应为 add/remove/clear"}, 400)
            except ValueError as e:
                self._json({"error": str(e)}, 400)

        def _post_trades(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                req = json.loads(body.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                return self._json({"error": "请求体应为 JSON"}, 400)
            action = req.get("action")
            try:
                if action == "add":
                    trades, rec = store.trade_add(
                        req.get("code", ""),
                        req.get("date", ""),
                        req.get("side", ""),
                        req.get("price"),
                        req.get("shares"),
                        req.get("note", ""),
                        fee=req.get("fee"),
                    )
                    self._json({"code": rec["code"], "trades": trades, "added": rec})
                elif action == "remove":
                    code = (req.get("code") or "").upper()
                    trades, removed = store.trade_remove(code, str(req.get("id", "")))
                    self._json({"code": code, "trades": trades, "removed": removed})
                elif action == "update":
                    code = (req.get("code") or "").upper()
                    tid = str(req.get("id", ""))
                    trades, rec = store.trade_update(
                        code,
                        tid,
                        date=req.get("date"),
                        side=req.get("side"),
                        price=req.get("price"),
                        shares=req.get("shares"),
                        note=req.get("note"),
                        # 缺省键=不改动；显式 null=清空（回到自动估算）；数字=实际费用
                        fee=req.get("fee", _UNSET),
                    )
                    self._json({"code": code, "trades": trades, "updated": rec})
                elif action == "add_batch":
                    code = (req.get("code") or "").upper()
                    trades, added, errors = store.trade_add_batch(code, req.get("trades"))
                    self._json({"code": code, "trades": trades, "added": added,
                                "errors": errors, "n_added": len(added)})
                else:
                    return self._json({"error": "action 应为 add/remove/update/add_batch"}, 400)
            except ValueError as e:
                self._json({"error": str(e)}, 400)

        def do_POST(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path == "/api/watchlist":
                return self._post_watchlist()
            if url.path == "/api/trades":
                return self._post_trades()
            if url.path == "/api/minute":
                return self._post_minute()
            self._json({"error": "unknown api"}, 404)

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            path = url.path
            if path.startswith("/api/"):
                return self._api(path, self._params())
            p = _safe_static(path)
            if p is None:
                return self._json({"error": "not found"}, 404)
            ctype = CONTENT_TYPES.get(p.suffix, "application/octet-stream")
            return self._send(200, p.read_bytes(), ctype)

    return Handler


def serve(data_dir: str = "data", host: str = "127.0.0.1", port: int = 8765):
    store = DataStore(data_dir)

    # 自选股分钟线后台同步：启动即同步已有自选，之后加自选即入队，每 30 分钟增量刷新
    from .minute import MinuteSyncer

    syncer = MinuteSyncer(data_dir)
    syncer.set_watchlist_reader(lambda: list(store._wl_codes))
    if store._wl_codes:
        print(f"自选股 {len(store._wl_codes)} 只，后台开始同步分钟线…")
        for c in list(store._wl_codes):
            syncer.submit(c)

    runner = None
    try:
        import importlib

        from stock_analysis import Runner, load_plugins
        from stock_analysis.core.context import AnalysisContext
        from stock_analysis.core.registry import (DEFAULT_PLUGIN_PACKAGE,
                                                  extra_plugin_dirs)

        pkg = importlib.import_module(DEFAULT_PLUGIN_PACKAGE)
        dirs = [Path(pkg.__file__).resolve().parent] + extra_plugin_dirs()

        def _build():
            return Runner(load_plugins(), AnalysisContext(store))

        runner = _AutoReloadRunner(_build, dirs)
        n_fail = len(runner.registry.discovery_failures)
        print(f"分析插件: {len(runner.registry.names())} 个已加载"
              + (f"（{n_fail} 个加载失败，见日志）" if n_fail else "")
              + "；新增插件文件自动生效（改内容需重启）")
    except Exception as e:  # 插件框架缺失/损坏不阻塞仪表盘主功能
        print(f"警告: 插件分析不可用（{e}），/api/plugins* 将返回 503")

    httpd = ThreadingHTTPServer((host, port), make_handler(store, runner=runner, syncer=syncer))
    url = f"http://{host}:{port}/"
    print(f"数据加载完成: {store.meta['n_stocks']} 只股票 | "
          f"{store.meta['date_min']} ~ {store.meta['date_max']} | "
          f"耗时 {store.meta['load_seconds']}s")
    print(f"股票分析仪表盘: {url}   (Ctrl+C 退出)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    serve()
