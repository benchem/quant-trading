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

from .auth import SESSION_COOKIE, SESSION_TTL, AuthManager
from .datastore import DataStore, _f, _r, _UNSET
from .notify import NotificationHub

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

    def clear_cache(self):
        """数据更新后清 Runner 结果缓存与 Context kline 缓存（由盘后调度器调用）。"""
        with self._lock:
            self._runner.clear_cache()
            ctx = getattr(self._runner, "ctx", None)
            if ctx is not None and hasattr(ctx, "clear_cache"):
                ctx.clear_cache()


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


def make_handler(store: DataStore, runner=None, syncer=None, notifier=None, updater=None,
                 auth: AuthManager | None = None, brief_pusher=None):
    """runner: stock_analysis.core.runner.Runner（插件分析）；None 时禁用 /api/plugins*。
    syncer: analyzer.minute.MinuteSyncer（自选股分钟线后台同步）；None 时禁用。
    notifier: 消息中心——多用户模式传 NotificationHub（按用户隔离收件箱），
        共享单用户模式传单个 NotificationCenter；None 时禁用 /api/notifications。
    updater: analyzer.updates.EodUpdater（盘后自动更新调度器）；None 时禁用 /api/update/*。
    brief_pusher: analyzer.overseas_push.OverseasBriefPusher（美股简报邮件推送）；
        None 时禁用 /api/us_brief_push。
    auth: AuthManager；None 为「共享」单用户模式（免登录，数据存根级文件），
        否则所有业务 API 需登录（自选/交易/站内信按用户隔离）。"""
    hub = isinstance(notifier, NotificationHub)

    def _journal_trade_event(user: str | None, rec: dict) -> None:
        """交易登记成功后写一条 journal 事件（失败静默——不影响交易主链路）。"""
        try:
            from .journal import Journal, event_from_trade

            Journal(store.root, user).add_event(event_from_trade(rec))
        except Exception:  # noqa: BLE001 日记是旁路，绝不阻塞交易记录
            pass

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
            if getattr(self, "_pending_cookie", None):
                self.send_header("Set-Cookie", self._pending_cookie)
                self._pending_cookie = None
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

        # --------------------------------------------------------- 鉴权

        def _current_user(self) -> str | None:
            """当前请求的用户；共享模式恒为 None（= 共享桶）；认证模式下未登录为 None。"""
            if auth is None:
                return None
            m = re.search(rf"(?:^|;\s*){SESSION_COOKIE}=([^;]+)", self.headers.get("Cookie", ""))
            if not m:
                return None
            return auth.user_of_token(m.group(1))

        def _require_user(self) -> str | None:
            """业务 API 鉴权：未登录时回 401 并返回 None（调用方直接 return）。"""
            user = self._current_user()
            if user is None:
                self._json({"error": "未登录", "auth": True}, 401)
                return None
            return user

        def _set_session(self, token: str) -> None:
            self._pending_cookie = (
                f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}"
            )

        def _clear_session(self) -> None:
            self._pending_cookie = f"{SESSION_COOKIE}=; Path=/; Max-Age=0"

        def _inbox(self, user: str):
            """当前用户的收件箱对象（hub 模式按用户隔离；共享模式为同一收件箱）。"""
            if notifier is None:
                return None
            return notifier.center(user) if hub else notifier

        # ------------------------------------------------------------ 认证 API

        def _api_auth_status(self):
            """公开端点：当前登录状态（前端据此决定显示登录框还是仪表盘）。"""
            if auth is None:
                self._json({"auth": False, "user": None, "n_users": 0})
                return
            self._json({
                "auth": True,
                "user": self._current_user(),
                "n_users": len(auth.users()),
            })

        def _post_auth_register(self, req: dict):
            if auth is None:
                return self._json({"error": "当前为免登录共享模式，无需注册"}, 400)
            name = str(req.get("username") or "").strip()
            password = req.get("password")
            try:
                first = not auth.users()
                info = auth.create_user(name, password)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            # 首个注册用户：把升级前的 legacy 数据（自选/交易/站内信）迁入其名下
            if first:
                moved = store.migrate_legacy_to(info["name"])
                if moved:
                    print(f"已将 legacy 数据迁入用户 {info['name']}: {', '.join(moved)}")
            token = auth.login(name, password)
            self._set_session(token)
            # first=True 表示本次注册即首个用户（其 legacy 数据已完成迁移）
            self._json({"ok": True, "user": info["name"], "first": first})

        def _post_auth_login(self, req: dict):
            if auth is None:
                return self._json({"error": "当前为免登录共享模式，无需登录"}, 400)
            name = str(req.get("username") or "").strip()
            token = auth.login(name, req.get("password") or "")
            if token is None:
                return self._json({"error": "用户名或密码错误"}, 401)
            self._set_session(token)
            self._json({"ok": True, "user": name})

        def _post_auth_password(self):
            user = self._require_user()
            if user is None:
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                req = json.loads(body.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                return self._json({"error": "请求体应为 JSON"}, 400)
            old = req.get("old_password") or ""
            new = req.get("new_password")
            if not auth.verify(user, old):
                return self._json({"error": "原密码错误"}, 400)
            try:
                auth.change_password(user, new)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            self._json({"ok": True})

        # ---------------------------------------------------------------- API

        def _api(self, path: str, params: dict, user: str | None = None):
            """业务 GET API。user：当前用户（共享模式为 None=共享桶；
            调用前 do_GET 已完成鉴权，认证模式下不会为 None）。"""
            try:
                if path == "/api/meta":
                    self._json(store.meta)
                elif path == "/api/watchlist":
                    self._json({
                        "codes": store.watchlist(user),
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
                            board=(params.get("board") or None),
                            market=(params.get("market") or None),
                        )
                    )
                elif path == "/api/boards":
                    btype = params.get("type", "industry")
                    self._json(
                        {
                            "type": btype,
                            "available": store._board_members_of(btype) is not None,
                            "industry_available": store.board_df is not None,
                            "concept_available": bool(
                                store.board_all_df is not None
                                and (store.board_all_df["board_type"] == "concept").any()
                            ),
                            "boards": store.boards(btype),
                        }
                    )
                elif path == "/api/board_rank":
                    start, end = params.get("start"), params.get("end")
                    if not start or not end:
                        return self._json({"error": "需要 start/end 参数"}, 400)
                    out = store.board_rank(
                        start,
                        end,
                        params.get("metric", "ret"),
                        int(params.get("n", 30)),
                        params.get("asc", "0") == "1",
                        board_type=params.get("type", "industry"),
                    )
                    if out.get("error"):
                        return self._json(out, 400)
                    self._json(out)
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
                    ts = store.trades(user, code)
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
                elif path == "/api/flow":
                    code = (params.get("code") or "").upper()
                    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
                        return self._json({"error": "code 格式应为 600519.SH"}, 400)
                    try:
                        from . import flow as flow_mod

                        self._json(flow_mod.fetch(code, store.root,
                                                  refresh=params.get("refresh") == "1"))
                    except Exception as e:  # noqa: BLE001 数据源故障降级为 502
                        return self._json({"error": f"资金流数据不可用: {str(e)[:120]}"}, 502)
                elif path == "/api/announce":
                    code = (params.get("code") or "").upper()
                    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
                        return self._json({"error": "code 格式应为 600519.SH"}, 400)
                    try:
                        from . import announce as announce_mod

                        self._json(announce_mod.fetch(code, store.root,
                                                       refresh=params.get("refresh") == "1"))
                    except Exception as e:  # noqa: BLE001
                        return self._json({"error": f"公告数据不可用: {str(e)[:120]}"}, 502)
                elif path == "/api/sentiment":
                    code = (params.get("code") or "").upper()
                    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
                        return self._json({"error": "code 格式应为 600519.SH"}, 400)
                    try:
                        from . import sentiment as sentiment_mod

                        self._json(sentiment_mod.fetch(code, store.root,
                                                        refresh=params.get("refresh") == "1"))
                    except Exception as e:  # noqa: BLE001
                        return self._json({"error": f"情绪数据不可用: {str(e)[:120]}"}, 502)
                elif path == "/api/journal":
                    # 交易日记（按用户隔离）：事件 + 笔记 + 复盘一次取回
                    from .journal import Journal

                    j = Journal(store.root, user)
                    code = (params.get("code") or "").upper() or None
                    self._json({
                        "code": code,
                        "events": j.events(code=code, limit=int(params.get("limit", 100))),
                        "notes": j.notes(code=code, limit=int(params.get("notes_limit", 50))),
                        "reviews": j.reviews(code=code, limit=int(params.get("reviews_limit", 20))),
                    })
                elif path == "/api/valuation":
                    code = (params.get("code") or "").upper()
                    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
                        return self._json({"error": "code 格式应为 600519.SH"}, 400)
                    try:
                        data = store.valuation(code, refresh=params.get("refresh") == "1")
                    except Exception as e:  # ValuationError 及其他
                        return self._json({"error": f"估值数据不可用: {str(e)[:120]}"}, 502)
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
                elif path == "/api/hourly":
                    code = (params.get("code") or "").upper()
                    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
                        return self._json({"error": "code 格式应为 600519.SH"}, 400)
                    self._json(self._hourly_view(code))
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
                elif path == "/api/update/status":
                    if updater is None:
                        return self._json({"error": "盘后自动更新未启用"}, 503)
                    self._json(updater.get_status())
                elif path == "/api/us_brief_push/status":
                    if brief_pusher is None:
                        return self._json({"error": "美股简报推送未启用"}, 503)
                    self._json(brief_pusher.get_status())
                elif path == "/api/notifications":
                    nb = self._inbox(user)
                    if nb is None:
                        return self._json({"error": "消息中心未启用"}, 503)
                    self._json(nb.list(
                        limit=int(params.get("limit", 100)),
                        since=params.get("since") or None,
                        source=params.get("source") or None,
                        level=params.get("level") or None,
                        code=params.get("code") or None,
                        unread_only=params.get("unread") == "1",
                    ))
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
            # 1 分钟线源（新浪）只保留最近约 8 个交易日的滚动窗口，
            # 更早的日期补不回来（东财 1 分钟仅当日/近 5 日，腾讯仅约 1 日，均无更深历史）
            src_edge = time.strftime("%Y-%m-%d", time.localtime(time.time() - 14 * 86400))
            fetchable = date >= src_edge
            df = minute_mod.load(code, store.root)
            base = {"ok": False, "code": code, "date": date, "syncing": syncing,
                    "fetchable": fetchable}
            if df.empty:
                base["days"] = []
                if fetchable:
                    base["error"] = (f"{code} 无本地分钟线（分钟线对自选股自动后台同步；"
                                     f"可点「立即拉取」从新浪抓取最近约 8 个交易日）")
                else:
                    base["error"] = (f"{code} 无本地分钟线，且 {date} 已超出数据源保留范围："
                                     f"1 分钟线源（新浪/东财/腾讯）都只保留最近几个交易日，"
                                     f"更早的分钟历史无法补拉")
                return base
            days = sorted(df["datetime"].dt.strftime("%Y-%m-%d").unique().tolist())
            base["days"] = days
            day = df[df["datetime"].dt.strftime("%Y-%m-%d") == date]
            if day.empty:
                rng = f"{days[0]} ~ {days[-1]}" if len(days) > 1 else days[0]
                if fetchable:
                    base["error"] = (f"{date} 无分钟数据（本地覆盖 {rng}，共 {len(days)} 个交易日；"
                                     f"可点「立即拉取」从新浪抓最近约 8 个交易日）")
                else:
                    base["error"] = (f"{date} 不在本地覆盖范围（{rng}），且已超出数据源保留范围"
                                     f"（1 分钟线源只保留最近约 8 个交易日）——更早的分钟历史"
                                     f"无法补拉；自选股的本地分钟线从加入自选当日起自动累积")
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
                "fetchable": fetchable,
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

        def _hourly_view(self, code: str) -> dict:
            """60 分钟 K 线视图：本地 1 分钟线聚合（真实价，仅自选股有本地数据）。"""
            from . import minute as minute_mod

            syncing = syncer is not None and code in syncer.pending()
            df = minute_mod.load(code, store.root)
            base = {"ok": False, "code": code, "syncing": syncing}
            if df.empty:
                base["days"] = []
                base["error"] = (f"{code} 无本地分钟线，无法生成时K线（分钟线对自选股"
                                 f"自动后台同步；加入自选后即可查看）")
                return base
            days = sorted(df["datetime"].dt.strftime("%Y-%m-%d").unique().tolist())
            h = minute_mod.to_hourly(df)
            if h.empty:
                base["days"] = days
                base["error"] = f"{code} 分钟线数据异常，无法聚合为 60 分钟 K 线"
                return base
            return {
                "ok": True, "code": code, "syncing": syncing,
                "dates": h["label"].tolist(),
                "open": _f(h["open"].tolist()),
                "high": _f(h["high"].tolist()),
                "low": _f(h["low"].tolist()),
                "close": _f(h["close"].tolist()),
                # 分钟线 volume 单位是股，日线口径为手
                "volume": [int(round(v / 100)) for v in h["volume"].tolist()],
                "amount": _f(h["amount"].tolist()),
                "days": days, "n_days": len(days),
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

        def _post_ai_analysis(self):
            """个股 AI 综合分析：聚合插件结果 -> 云端大模型，报告缓存 data/ai/。"""
            if runner is None:
                return self._json({"error": "插件分析未启用，无法生成 AI 分析"}, 503)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                req = json.loads(body.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                return self._json({"error": "请求体应为 JSON"}, 400)
            code = (req.get("code") or "").upper()
            if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
                return self._json({"error": "code 格式应为 600519.SH"}, 400)
            from . import ai as ai_mod

            config = ai_mod.AIConfig.from_env()
            if not config.enabled:
                return self._json({"error": "AI 分析未配置：请在服务端设置环境变量 "
                                            "AI_API_KEY（可选 AI_BASE_URL / AI_MODEL）后重启 analyzer"}, 503)
            try:
                self._json(ai_mod.generate(store, runner, code,
                                           refresh=bool(req.get("refresh")), config=config))
            except ai_mod.AIError as e:
                return self._json({"error": str(e)}, e.status)
            except Exception:
                traceback.print_exc()
                self._json({"error": "AI 分析生成失败"}, 500)

        def _post_watchlist(self, user: str | None):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                req = json.loads(body.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                return self._json({"error": "请求体应为 JSON"}, 400)
            action = req.get("action")
            try:
                if action == "add":
                    codes, added = store.watchlist_add(user, req.get("code", ""))
                    if syncer:
                        syncer.submit(req.get("code", "").upper())
                    self._json({"codes": codes, "added": added})
                elif action == "remove":
                    codes, removed = store.watchlist_remove(user, req.get("code", ""))
                    self._json({"codes": codes, "removed": removed})
                elif action == "reorder":
                    codes = req.get("codes")
                    if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes):
                        return self._json({"error": "codes 应为字符串数组"}, 400)
                    self._json({"codes": store.watchlist_reorder(user, codes), "reordered": True})
                elif action == "clear":
                    store.watchlist_clear(user)
                    self._json({"codes": [], "cleared": True})
                else:
                    self._json({"error": "action 应为 add/remove/reorder/clear"}, 400)
            except ValueError as e:
                self._json({"error": str(e)}, 400)

        def _post_trades(self, user: str | None):
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
                        user,
                        req.get("code", ""),
                        req.get("date", ""),
                        req.get("side", ""),
                        req.get("price"),
                        req.get("shares"),
                        req.get("note", ""),
                        fee=req.get("fee"),
                    )
                    _journal_trade_event(user, rec)
                    self._json({"code": rec["code"], "trades": trades, "added": rec})
                elif action == "remove":
                    code = (req.get("code") or "").upper()
                    trades, removed = store.trade_remove(user, code, str(req.get("id", "")))
                    self._json({"code": code, "trades": trades, "removed": removed})
                elif action == "update":
                    code = (req.get("code") or "").upper()
                    tid = str(req.get("id", ""))
                    trades, rec = store.trade_update(
                        user,
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
                    trades, added, errors = store.trade_add_batch(user, code, req.get("trades"))
                    for rec in added:
                        _journal_trade_event(user, rec)
                    self._json({"code": code, "trades": trades, "added": added,
                                "errors": errors, "n_added": len(added)})
                else:
                    return self._json({"error": "action 应为 add/remove/update/add_batch"}, 400)
            except ValueError as e:
                self._json({"error": str(e)}, 400)

        def _post_journal(self, user: str | None):
            """交易日记（按用户隔离）：笔记 CRUD + 复盘生成（规则版/LLM 版）。"""
            from . import journal as journal_mod
            from .journal import Journal

            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                req = json.loads(body.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                return self._json({"error": "请求体应为 JSON"}, 400)
            action = req.get("action")
            code = (req.get("code") or "").upper() or None
            if code and not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
                return self._json({"error": "code 格式应为 600519.SH"}, 400)
            j = Journal(store.root, user)
            try:
                if action == "note_add":
                    rec = j.add_note(code, req.get("text") or "",
                                     day=req.get("day") or None,
                                     tags=req.get("tags"))
                    self._json({"ok": True, "note": rec})
                elif action == "note_update":
                    rec = j.update_note(str(req.get("id") or ""),
                                        text=req.get("text"),
                                        tags=req.get("tags"))
                    if rec is None:
                        return self._json({"error": "笔记不存在"}, 404)
                    self._json({"ok": True, "note": rec})
                elif action == "note_delete":
                    ok = j.delete_note(str(req.get("id") or ""))
                    self._json({"ok": ok, "deleted": ok})
                elif action == "review_generate":
                    # 规则版复盘（本地统计，不调 LLM，随时可用）
                    if not code:
                        return self._json({"error": "需要 code 参数"}, 400)
                    day = req.get("day") or time.strftime("%Y-%m-%d")
                    review = journal_mod.build_daily_review(j, store, code, day)
                    if review is None:
                        return self._json({"error": "当日无事件且无行情，无可复盘内容"}, 400)
                    rec = j.add_review(day, code, review["summary"],
                                       lines=review["lines"], version="rule")
                    self._json({"ok": True, "review": rec})
                elif action == "review_llm":
                    # LLM 版复盘：基于当日规则版复盘（无则先生成）+ 笔记 + 信号事件
                    if not code:
                        return self._json({"error": "需要 code 参数"}, 400)
                    day = req.get("day") or time.strftime("%Y-%m-%d")
                    review = journal_mod.build_daily_review(j, store, code, day)
                    if review is None:
                        return self._json({"error": "当日无事件且无行情，无可复盘内容"}, 400)
                    try:
                        out = journal_mod.build_llm_review(j, code, day, review)
                    except journal_mod.__dict__.get("AIError", RuntimeError) as e:  # noqa: BLE001
                        # AIError 从 analyzer.ai 延迟导入，此处统一兜住
                        return self._json({"error": str(e)}, getattr(e, "status", 502))
                    rec = j.add_review(day, code, out["report"], version="llm")
                    self._json({"ok": True, "review": rec, "usage": out.get("usage")})
                else:
                    return self._json({"error": "action 应为 note_add/note_update/"
                                                "note_delete/review_generate/review_llm"}, 400)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                return self._json({"error": f"日记操作失败: {str(e)[:120]}"}, 500)

        def _post_update(self):
            """手动触发一轮盘后更新链路（异步；进度经 /api/update/status 查询）。"""
            if updater is None:
                return self._json({"error": "盘后自动更新未启用"}, 503)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                json.loads(body.decode("utf-8") or "{}")  # 仅校验 JSON
            except (ValueError, UnicodeDecodeError):
                return self._json({"error": "请求体应为 JSON"}, 400)
            self._json(updater.trigger(source="manual"))

        def _post_us_brief_push(self):
            """手动推送一次昨夜美股简报邮件（当日已成功时幂等跳过）。"""
            if brief_pusher is None or not brief_pusher.cfg.enabled:
                return self._json({"error": "美股简报推送未配置（SMTP_USER/"
                                            "SMTP_PASS/MAIL_TO）"}, 503)
            self._json(brief_pusher.push_once(source="manual"))

        def _post_notifications(self, user: str | None):
            nb = self._inbox(user)
            if nb is None:
                return self._json({"error": "消息中心未启用"}, 503)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                req = json.loads(body.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                return self._json({"error": "请求体应为 JSON"}, 400)
            action = req.get("action")
            if action == "read":
                ids = req.get("ids")
                if ids is not None and (not isinstance(ids, list)
                                        or not all(isinstance(x, str) for x in ids)):
                    return self._json({"error": "ids 应为字符串数组"}, 400)
                n = nb.mark_read(ids, all=bool(req.get("all")),
                                 source=req.get("source") or None)
                self._json({"ok": True, "marked": n})
            elif action == "clear":
                n = nb.clear(before_day=req.get("before_day") or None,
                             source=req.get("source") or None)
                self._json({"ok": True, "removed": n})
            else:
                self._json({"error": "action 应为 read/clear"}, 400)

        def _auth_post_body(self, handler):
            """认证类 POST 的 JSON body 读取（注册/登录共用）。"""
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                req = json.loads(body.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                return self._json({"error": "请求体应为 JSON"}, 400)
            return handler(req)

        def do_POST(self):  # noqa: N802
            url = urlparse(self.path)
            path = url.path
            # ---- 认证相关 POST（注册/登录/登出/改密）----
            if path == "/api/auth/register":
                return self._auth_post_body(self._post_auth_register)
            if path == "/api/auth/login":
                return self._auth_post_body(self._post_auth_login)
            if path == "/api/auth/logout":
                self._clear_session()
                return self._json({"ok": True})
            if path == "/api/auth/password":
                return self._post_auth_password()
            # ---- 业务 API：先鉴权（共享模式 user=None 表示共享桶）----
            user = self._require_user() if auth is not None else None
            if user is None and auth is not None:
                return
            if path == "/api/watchlist":
                return self._post_watchlist(user)
            if path == "/api/trades":
                return self._post_trades(user)
            if path == "/api/minute":
                return self._post_minute()
            if path == "/api/ai_analysis":
                return self._post_ai_analysis()
            if path == "/api/update/run":
                return self._post_update()
            if path == "/api/us_brief_push/run":
                return self._post_us_brief_push()
            if path == "/api/notifications":
                return self._post_notifications(user)
            if path == "/api/journal":
                return self._post_journal(user)
            self._json({"error": "unknown api"}, 404)

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            path = url.path
            if path == "/api/auth/status":
                return self._api_auth_status()
            if path.startswith("/api/"):
                user = self._require_user() if auth is not None else None
                if user is None and auth is not None:
                    return
                return self._api(path, self._params(), user)
            p = _safe_static(path)
            if p is None:
                return self._json({"error": "not found"}, 404)
            ctype = CONTENT_TYPES.get(p.suffix, "application/octet-stream")
            return self._send(200, p.read_bytes(), ctype)

    return Handler


def serve(data_dir: str = "data", host: str = "127.0.0.1", port: int = 8765,
          auth_enabled: bool = True):
    """启动仪表盘。auth_enabled=True 时启用多用户登录（自选/交易/站内信按用户
    隔离，存 data/users/{user}/）；False 为免登录共享单用户模式（沿用根级
    watchlist.json / trades.json，与旧版本行为一致）。"""
    store = DataStore(data_dir)

    # 多用户登录（默认启用）
    auth = None
    if auth_enabled:
        auth = AuthManager(data_dir)
        if auth.users():
            print(f"多用户登录: 已启用（{len(auth.users())} 个用户: "
                  f"{', '.join(auth.users())}）")
        else:
            print("多用户登录: 已启用（尚无用户——首个注册的用户将继承现有"
                  "自选股/交易记录/站内信）")

    # 自选股分钟线后台同步：启动即同步「全部用户自选并集」，每 30 分钟增量刷新
    from .minute import MinuteSyncer

    syncer = MinuteSyncer(data_dir)
    syncer.set_watchlist_reader(store.watchlist_codes_all)
    wl_all = store.watchlist_codes_all()
    if wl_all:
        print(f"自选股（全部用户并集）{len(wl_all)} 只，后台开始同步分钟线…")
        for c in wl_all:
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

    from .ai import AIConfig

    ai_cfg = AIConfig.from_env()
    if ai_cfg.enabled:
        print(f"AI 分析: 已启用（模型 {ai_cfg.model} · {ai_cfg.base_url}）")
    else:
        print("AI 分析: 未启用（设置环境变量 AI_API_KEY 后可用，可选 AI_BASE_URL/AI_MODEL）")

    # 消息中心 + 盘后自动更新：每交易日 15:30（CST）触发全量更新，
    # 自选股数据优先刷新，更新完成后扫描自选股模型信号并推送提醒。
    from .notify import NotificationHub, NotificationCenter
    from .updates import EodUpdater

    if auth is not None:
        # 多用户：每用户独立收件箱（data/users/{user}/notifications.jsonl）；
        # 带 code 的信号只推给自选含该股的用户，系统消息推给全部用户
        notifier = NotificationHub(data_dir, users_provider=auth.users,
                                   watchlist_provider=store.watchlist_codes)
    else:
        notifier = NotificationCenter(data_dir)
    updater = EodUpdater(
        store, notifier, data_dir=data_dir,
        on_cache_invalidate=(runner.clear_cache if runner is not None else None),
        runner=runner, syncer=syncer,
    )
    updater.start()
    print(f"盘后自动更新: 已启用（每交易日 {updater.status['trigger']} CST 触发，"
          f"自选股优先刷新 + 信号提醒；POST /api/update/run 可手动触发）")

    # 昨夜美股简报邮件推送（借 iam 服务器 trending 项目的 email 渠道做法）：
    # 每工作日 08:40 CST 把 data/news/us_market/当日.md 渲染成 HTML 邮件推送；
    # 未配置 SMTP_USER/SMTP_PASS/MAIL_TO 时自动停用，不影响其余功能。
    from .overseas_push import pusher_from_env

    brief_pusher = pusher_from_env(data_dir, notifier=notifier)
    brief_pusher.start()

    httpd = ThreadingHTTPServer((host, port), make_handler(
        store, runner=runner, syncer=syncer, notifier=notifier, updater=updater,
        auth=auth, brief_pusher=brief_pusher))
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
