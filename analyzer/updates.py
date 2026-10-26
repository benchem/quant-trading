"""每日收盘后自动更新编排：EOD 调度线程 + 分阶段全量更新 + 自选股信号扫描提醒。

流程（每交易日 trigger_hour:trigger_minute（默认 15:30 CST）触发一次）::

    1. 基础信息   股票列表 + 交易日历
    2. 自选日线   自选股后复权日线优先增量更新（数据最快可用）
    3. 自选资讯   自选股新闻 / 估值 / 财报强制刷新（refresh 绕过本地缓存 TTL）
    4. 全市场日线 剩余股票增量更新
    5. 指数日线   主要指数
    6. 板块成分   行业板块（含概念板块）
    7. 数据集合并 build_dataset（供 DataStore 市场聚合 / 排行使用）
    8. 缓存失效   DataStore.reload + 插件 Runner/Context 缓存清空
    9. 信号扫描   逐只自选股跑模型插件，alert 级信号 -> 消息中心 + macOS 通知

设计要点：
- 单例锁：同一进程内同一时刻只允许一个更新任务（手动触发同理）；
- 补跑：进程启动后若今天为交易日且已过触发时间而当日未跑过，则立即补跑一次；
- 交易日历不可用时退化为工作日判断（planwatch/common.py 同款策略）；
- 各阶段独立容错：单阶段失败记入 errors 并继续后续阶段（自选股优先原则
  保证即便后续失败，自选数据也已刷新）；
- 关键节点推送站内消息（进度 info、完成 ok、失败/触发信号 warn/alert）。
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")

#: 更新完成后逐只自选股扫描信号的模型插件（与个股分析面板一致）
SCAN_PLUGINS = (
    "multi_factor_breakout",
    "garch_mean_reversion",
    "chip_distribution",
    "bollinger_channel",
)

STAGES = (
    ("base", "基础信息"),
    ("watchlist_daily", "自选股日线"),
    ("watchlist_info", "自选资讯"),
    ("market_daily", "全市场日线"),
    ("index", "指数日线"),
    ("board", "板块成分"),
    ("dataset", "数据集合并"),
    ("reload", "缓存刷新"),
    ("scan", "信号扫描"),
)
STAGE_LABELS = dict(STAGES)
_STAGE_ORDER = [k for k, _ in STAGES]


def _is_trading_day(data_dir, d: dt.date) -> bool:
    """按 data/trade_calendar.parquet 判断；缺文件/列名异常退化为周一~周五。

    日历未覆盖的「未来日期」也退化为工作日判断：数据源故障时日历可能只含历史
    日期（2026-10-25 事故中日历止于 10-23），若直接返回 False 会让 next_fire
    无限向后寻日，datetime 溢出报 date value out of range、调度线程永久崩溃。
    """
    try:
        import pandas as pd

        p = Path(data_dir) / "trade_calendar.parquet"
        cal = pd.read_parquet(p)
        col = "date" if "date" in cal.columns else cal.columns[0]
        days = set(pd.to_datetime(cal[col]).dt.date)
        if d in days:
            return True
        return d > max(days) and d.weekday() < 5
    except Exception:  # noqa: BLE001
        return d.weekday() < 5


def next_fire(now: dt.datetime, data_dir, trigger_hour: int = 15,
              trigger_minute: int = 30) -> tuple[dt.datetime, bool]:
    """下一个触发时刻（CST）；now 早于今日触发时间且为交易日 -> (今日, False)，
    否则找次日之后的第一个交易日 -> (次日后, True 表示需后台等待)。"""
    today = now.date()
    fire = now.replace(hour=trigger_hour, minute=trigger_minute,
                       second=0, microsecond=0)
    if now < fire and _is_trading_day(data_dir, today):
        return fire, False
    d = today + dt.timedelta(days=1)
    for _ in range(90):  # 硬上限：日历完全不可用时避免无限寻日
        if _is_trading_day(data_dir, d):
            return dt.datetime.combine(d, fire.timetz()), True
        d += dt.timedelta(days=1)
    # 90 天内找不到交易日（日历彻底不可用）: 退化为下一个工作日，保证调度不中断
    d = today + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return dt.datetime.combine(d, fire.timetz()), True


# ------------------------------------------------------------------ 信号扫描


def scan_watchlist_signals(store, runner, codes, plugins=SCAN_PLUGINS,
                           name_of=None) -> list[dict]:
    """逐只自选股跑模型插件，提取 alert 级信号与当日触发指标（signal_today）。

    返回 [{"code", "name", "plugin", "level", "text"}]；单股/单插件异常静默跳过
    （插件故障隔离原则，与 Runner 一致）。
    """
    out: list[dict] = []
    for code in codes:
        name = name_of(code) if name_of else code
        for plugin in plugins:
            try:
                r = runner.run(plugin, code)
                d = r.to_dict() if hasattr(r, "to_dict") else r
            except Exception:  # noqa: BLE001
                continue
            if not d.get("ok"):
                continue
            alerts = [s for s in d.get("signals") or [] if s.get("level") == "alert"]
            hit_alert = bool(alerts)
            metrics = d.get("metrics") or {}
            st = metrics.get("signal_today")
            hit_today = isinstance(st, (int, float)) and st == 1
            if hit_alert:
                for s in alerts:
                    out.append({"code": code, "name": name, "plugin": plugin,
                                "level": "alert", "text": s.get("text", "")})
            elif hit_today:
                label = (d.get("display_name") or d.get("plugin") or plugin)
                summary = (d.get("summary") or d.get("verdict") or "").strip()
                out.append({"code": code, "name": name, "plugin": plugin,
                            "level": "warn",
                            "text": f"{label} 当日触发信号" + (f"：{summary}" if summary else "")})
    return out


# ------------------------------------------------------------------ 调度器


class EodUpdater:
    """每日收市后全量数据更新调度器（守护线程，随 analyzer 进程存活）。

    参数:
        store:    analyzer.datastore.DataStore（读自选列表 / reload / 资讯刷新）
        notify:   analyzer.notify.NotificationCenter（进度与信号消息）
        on_cache_invalidate: 数据落盘后调用（清 Runner/Context 缓存）
        on_scan:  可选自定义信号扫描 (codes) -> list[dict]；None 时用内置
                  scan_watchlist_signals(store, runner, codes)
        runner:   信号扫描用的 Runner（或其代理）
    """

    def __init__(self, store, notify, data_dir="data", trigger_hour=15, trigger_minute=30,
                 on_cache_invalidate=None, on_scan=None, runner=None,
                 downloader_factory=None, syncer=None, check_interval: float = 20.0):
        self.store = store
        self.notify = notify
        self.data_dir = Path(data_dir)
        self.trigger_hour = int(trigger_hour)
        self.trigger_minute = int(trigger_minute)
        self.on_cache_invalidate = on_cache_invalidate
        self.on_scan = on_scan
        self.runner = runner
        self.syncer = syncer
        self.check_interval = float(check_interval)
        self._downloader_factory = downloader_factory
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # 单例锁：防并发更新（trigger 非阻塞抢占；run_now 可带超时等待）
        self._guard = threading.Lock()
        # 状态锁：可重入（更新线程持锁执行各阶段时，内部 helper 会再次取锁）
        self._lock = threading.RLock()
        self.last_run_day: str | None = None   # 最近一次成功启动更新的日期（防同日重跑）
        self.status: dict = {
            "running": False, "trigger": f"{trigger_hour:02d}:{trigger_minute:02d}",
            "started": None, "finished": None, "day": None,
            "stage": None, "stage_label": None, "stage_index": 0, "n_stages": len(STAGES),
            "stats": None, "errors": [], "watchlist_n": 0, "signals": 0,
            "last_ok": None,
        }

    # ------------------------------------------------------ Downloader

    def _downloader(self):
        if self._downloader_factory is not None:
            return self._downloader_factory()
        from downloader.app import Downloader

        return Downloader(data_dir=self.data_dir, adjust="hfq")

    def _wl_codes(self) -> list[str]:
        try:
            return list(self.store._wl_codes)
        except AttributeError:  # duck typing：测试替身
            return list(getattr(self.store, "watchlist_codes", lambda: [])())

    # ------------------------------------------------------ 状态

    def get_status(self) -> dict:
        with self._lock:
            return dict(self.status, running=self._is_running())

    def _is_running(self) -> bool:
        with self._lock:
            return bool(self.status.get("running"))

    # ------------------------------------------------------ 调度线程

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="eod-updater", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = dt.datetime.now(CST)
                fire, _wait = next_fire(now, self.data_dir,
                                        self.trigger_hour, self.trigger_minute)
                if (now >= fire and self.last_run_day != fire.date().isoformat()
                        and not self._is_running()):
                    self.trigger(source="schedule")
                # 触发时刻前的短暂睡眠（最多 check_interval，便于优雅退出）
                delay = max(0.0, (fire - now).total_seconds())
                self._stop.wait(min(delay, self.check_interval) if delay else self.check_interval)
            except Exception as e:  # noqa: BLE001 调度线程绝不因单次异常退出
                time.sleep(self.check_interval)
                self._error(f"调度循环异常: {e}")

    # ------------------------------------------------------ 触发入口

    def trigger(self, source: str = "manual") -> dict:
        """异步触发一轮更新；已在跑/当日已跑（仅 schedule 源）时拒绝。"""
        day = dt.datetime.now(CST).date().isoformat()
        if source == "schedule" and self.last_run_day == day:
            return {"ok": False, "error": "今日已执行过更新"}
        if not self._guard.acquire(blocking=False):
            return {"ok": False, "error": "更新任务已在运行中"}
        t = threading.Thread(target=self._run_guarded, args=(source, day),
                             name=f"eod-run-{day}", daemon=True)
        t.start()
        return {"ok": True, "started": True, "source": source}

    def run_now(self, timeout: float | None = None) -> dict:
        """同步执行完整链路（测试与调试用）。timeout 秒内拿不到单例锁则报忙。"""
        day = dt.datetime.now(CST).date().isoformat()
        got = (self._guard.acquire(timeout=timeout)
               if timeout is not None else self._guard.acquire(blocking=False))
        if not got:
            return self.get_status() | {"ok": False, "error": "更新任务已在运行中"}
        try:
            self._run(source="manual", day=day)
        finally:
            self._guard.release()
        return self.get_status()

    def _run_guarded(self, source: str, day: str) -> None:
        try:
            self._run(source, day)
        except Exception as e:  # noqa: BLE001 兜底：任何异常都落状态、发消息
            with self._lock:
                self.status["running"] = False
                self.status["errors"].append(f"更新线程异常: {e}")
            try:
                self.notify.push(f"update:error:{day}", f"数据更新异常中断：{e}",
                                 level="warn", desktop=True, source="system")
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._guard.release()

    # ------------------------------------------------------ 各阶段

    def _begin(self, source: str, day: str) -> None:
        codes = self._wl_codes()
        with self._lock:
            self.status.update({
                "running": True, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                "finished": None, "day": day, "stage": None, "stage_label": None,
                "stage_index": 0, "stats": None, "errors": [], "signals": 0,
                "watchlist_n": len(codes), "source": source,
            })
        self.last_run_day = day
        self.notify.push(f"update:start:{day}",
                         f"盘后数据更新启动（{len(codes)} 只自选股优先刷新）",
                         level="info", desktop=False, source="system")

    def _end(self) -> None:
        with self._lock:
            self.status["running"] = False
            self.status["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.status["last_ok"] = self.status["finished"]
            errors = list(self.status["errors"])
            stats = dict(self.status.get("stats") or {})
        day = self.status.get("day") or ""
        if errors:
            self.notify.push(f"update:error:{day}",
                             f"数据更新完成但有 {len(errors)} 个错误：{errors[0]}"
                             + (f" 等{len(errors)}项" if len(errors) > 1 else ""),
                             level="warn", desktop=True, source="system")
        else:
            tail = " | ".join(f"{k}:{v}" for k, v in list(stats.items())[:4])
            self.notify.push(f"update:done:{day}",
                             f"盘后数据更新完成（截至 {self.status.get('day')}）"
                             + (f"：{tail}" if tail else ""),
                             level="ok", desktop=False, source="system")
        try:
            if self.syncer is not None:
                for c in self._wl_codes():
                    self.syncer.submit(c)
        except Exception:  # noqa: BLE001
            pass

    def _stage(self, key: str) -> None:
        with self._lock:
            self.status["stage"] = key
            self.status["stage_label"] = STAGE_LABELS.get(key, key)
            self.status["stage_index"] = (_STAGE_ORDER.index(key) + 1
                                         if key in _STAGE_ORDER else 0)

    def _error(self, msg: str) -> None:
        with self._lock:
            self.status["errors"].append(msg)

    def _merge_stats(self, part: dict) -> None:
        with self._lock:
            stats = self.status.get("stats") or {}
            for k, v in (part or {}).items():
                if isinstance(v, (int, float)):
                    stats[k] = stats.get(k, 0) + int(v)
            self.status["stats"] = stats

    def _run(self, source: str, day: str) -> None:
        self._begin(source, day)
        dl = self._downloader()
        codes = self._wl_codes()

        # 1. 基础信息：股票列表 + 交易日历（日历失败不阻塞，退化本地缓存）
        self._stage("base")
        try:
            dl.refresh_stock_list()
        except Exception as e:  # noqa: BLE001
            self._error(f"股票列表刷新失败: {e}")
        try:
            dl.refresh_trade_calendar()
        except Exception as e:  # noqa: BLE001
            self._error(f"交易日历刷新失败: {e}")

        # 2. 自选股日线优先
        self._stage("watchlist_daily")
        if codes:
            try:
                stats = dl.update_stocks(codes=",".join(codes))
                self._merge_stats({f"wl_{k}": v for k, v in (stats or {}).items()})
            except Exception as e:  # noqa: BLE001
                self._error(f"自选股日线更新失败: {e}")

        # 3. 自选股资讯刷新（新闻/估值/财报，强制绕过缓存 TTL；失败静默降级）
        self._stage("watchlist_info")
        for c in codes:
            for fn in (self._refresh_news, self._refresh_valuation, self._refresh_fundamental):
                try:
                    fn(c)
                except Exception:  # noqa: BLE001 资讯失败不影响主链路
                    pass

        # 4. 全市场日线（自选股刚更新过，增量判定为「已是最新」自动跳过，无需排除）
        self._stage("market_daily")
        try:
            stats = dl.update_stocks()
            self._merge_stats(stats)
        except Exception as e:  # noqa: BLE001
            self._error(f"全市场日线更新失败: {e}")

        # 5. 指数
        self._stage("index")
        try:
            self._merge_stats(dl.update_indices() or {})
        except Exception as e:  # noqa: BLE001
            self._error(f"指数更新失败: {e}")

        # 6. 板块成分（行业+概念；耗时较长，失败降级）
        self._stage("board")
        try:
            dl.refresh_boards(with_concept=True)
        except Exception as e:  # noqa: BLE001
            self._error(f"板块成分刷新失败: {e}")

        # 7. 数据集合并
        self._stage("dataset")
        try:
            dl.build_dataset()
        except Exception as e:  # noqa: BLE001
            self._error(f"数据集合并失败: {e}")

        # 8. 缓存失效（数据新后必须让 API 重算）
        self._stage("reload")
        try:
            self.store.reload()
        except Exception as e:  # noqa: BLE001
            self._error(f"DataStore 重载失败: {e}")
        if self.on_cache_invalidate is not None:
            try:
                self.on_cache_invalidate()
            except Exception as e:  # noqa: BLE001
                self._error(f"分析缓存清理失败: {e}")

        # 9. 自选股信号扫描 -> 消息提醒
        self._stage("scan")
        n_sig = 0
        if codes:
            try:
                if self.on_scan is not None:
                    hits = self.on_scan(codes) or []
                else:
                    hits = scan_watchlist_signals(self.store, self.runner, codes,
                                                  name_of=self._name_of)
            except Exception as e:  # noqa: BLE001
                hits = []
                self._error(f"信号扫描失败: {e}")
            for h in hits:
                n_sig += 1
                self.notify.push(
                    key=f"signal:{h['code']}:{h['plugin']}:{day}",
                    text=f"⚡ {h['name']}({h['code']}) {h['text']}",
                    level=h.get("level", "alert"), code=h["code"], desktop=True,
                    source="model", extra={"plugin": h["plugin"]})
        with self._lock:
            self.status["signals"] = n_sig
        self._end()

    def _name_of(self, code: str) -> str:
        try:
            return str(self.store.basic.loc[code, "name"])
        except Exception:  # noqa: BLE001
            return code

    # ------------------------------------------------------ 资讯小工具

    def _refresh_news(self, code: str) -> None:
        from .news import fetch

        fetch(code, self.store.root, refresh=True)

    def _refresh_valuation(self, code: str) -> None:
        from .valuation import fetch

        fetch(code, self.store.root, refresh=True)

    def _refresh_fundamental(self, code: str) -> None:
        from .fundamental import fetch

        fetch(code, self.store.root, force=True)
