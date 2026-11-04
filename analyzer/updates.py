"""每日收盘后自动更新编排：EOD 调度线程 + 分阶段全量更新 + 自选股信号扫描提醒。

流程（每交易日 trigger_hour:trigger_minute（默认 15:30 CST）触发一次）::

    1. 基础信息   股票列表 + 交易日历
    2. 自选日线   自选股后复权日线优先增量更新（数据最快可用）
    3. 自选资讯   自选股新闻 / 估值 / 财报 / 资金流 / 公告 / 情绪 强制刷新
    4. 全市场日线 剩余股票增量更新
    5. 指数日线   主要指数
    6. 板块成分   行业板块（含概念板块）
    7. 数据集合并 build_dataset（供 DataStore 市场聚合 / 排行使用）
    8. 缓存失效   DataStore.reload + 插件 Runner/Context 缓存清空
    9. 信号扫描   逐只自选股跑模型插件，alert 级信号 -> 消息中心 + macOS 通知
   10. 交易日记   信号/盘后快照写入对应用户 journal + 自动生成规则版复盘

设计要点：
- 单例锁：同一进程内同一时刻只允许一个更新任务（手动触发同理）；
- 补跑：进程启动后若今天为交易日且已过触发时间而当日未跑过，则立即补跑一次；
- 交易日历不可用时退化为工作日判断（planwatch/common.py 同款策略）；
- 盘中每 30 分钟刷新股票列表（交易时段内）：新股上市当天即可搜索/加自选，
  不必等 15:30 盘后更新（2026-10-27 事故：新股白天全站查不到）；
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
    ("journal", "交易日记"),
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
    """下一个触发时刻（CST）；today 为交易日时恒返回 (今日 trigger 时刻, False)，
    否则找次日之后的第一个交易日 -> (次日后, True 表示需后台等待)。

    注意：now 已过今日触发时刻时仍返回「今日」——2026-10-26 事故中旧逻辑
    直接跳到次日，导致 _loop 里 `now >= fire` 永不成立、schedule 触发与
    「补跑」双双失效（服务在跑但一天也没拉过数据）。今日已跑的判定由
    EodUpdater.last_run_day 负责，不在这里做。
    """
    today = now.date()
    fire = now.replace(hour=trigger_hour, minute=trigger_minute,
                       second=0, microsecond=0)
    if _is_trading_day(data_dir, today):
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


# ------------------------------------------------------------------ 交易日记


def _journal_users(store) -> list[str | None]:
    """需要写 journal 的用户集合：共享模式 [None]；多用户模式为全部用户。"""
    try:
        users = store.known_users()
    except Exception:  # noqa: BLE001
        users = []
    return list(users) if users else [None]


def _journal_targets(store, users, code: str) -> list[str | None]:
    """某只股票的 journal 归属用户：自选含该股的用户。

    共享模式（users=[None]）恒返回 [None]（单一共享桶）。
    """
    out: list[str | None] = []
    for u in users:
        try:
            wl = store.watchlist_codes(u) or []
        except Exception:  # noqa: BLE001
            wl = []
        if code in wl:
            out.append(u)
    return out


def write_signal_events(store, hits: list[dict], day: str | None = None) -> int:
    """信号扫描命中写入对应用户的 journal 事件流；返回写入条数。

    归属规则与 NotificationHub 一致：自选股包含该股的用户；多用户模式下
    无人自选则广播给全部用户（与站内信「不丢消息」语义对齐）。
    day 给定时做当日去重（同日同股同插件的信号重跑只记一次）。
    """
    import datetime as _dt

    from .journal import Journal, event_from_signal

    day = day or _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).strftime("%Y-%m-%d")
    users = _journal_users(store)
    n = 0
    for h in hits:
        code = str(h.get("code", "")).upper()
        targets = _journal_targets(store, users, code)
        if not targets:
            targets = list(users)  # 无人自选 -> 广播
        for u in targets:
            try:
                j = Journal(store.root, u)
                # 当日去重：同 (code, plugin) 已写过就跳过
                dup = any(e.get("day") == day and e.get("code") == code
                          and e.get("plugin") == h.get("plugin")
                          for e in j.events(code=code, kind="model", limit=50))
                if dup:
                    continue
                j.add_event(event_from_signal(h))
                n += 1
            except Exception:  # noqa: BLE001 单用户失败不影响其余
                continue
    return n


def write_eod_journals(store, day: str) -> dict:
    """journal 收尾：盘后快照事件 + 当日规则版复盘，逐用户逐自选股生成。

    返回 {"events": 写入事件数, "reviews": 生成复盘数, "errors": [...]}。
    每用户的每只自选股：1 条盘后快照事件 + 1 份规则版复盘（当日已有该股
    复盘则跳过——手动触发过就不重复生成）。
    """
    from . import journal as journal_mod
    from .journal import Journal, eod_snapshot_event

    out = {"events": 0, "reviews": 0, "errors": []}

    def _kline_stats(code: str):
        try:
            k = store.kline(code)
        except Exception:  # noqa: BLE001
            return (None, None, None, None, None)
        if not k:
            return (None, None, None, None, None)
        closes = [c for c in (k.get("close") or []) if c is not None]
        if not closes:
            return (None, None, None, None, None)
        prev = closes[-2] if len(closes) >= 2 else None
        pct = (closes[-1] / prev - 1.0) * 100 if prev else None
        return (prev, closes[-1], pct, None, None)

    for u in _journal_users(store):
        try:
            codes = store.watchlist_codes(u) or []
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"读取自选失败({u}): {e}")
            continue
        j = Journal(store.root, u)
        # 当日去重（重跑防重复）：已写过快照事件的代码 / 已生成过复盘的代码
        day_events = j.events(limit=500)
        done_snap = {e.get("code") for e in day_events
                     if e.get("day") == day and e.get("kind") == "system"}
        done_reviews = {r.get("code") for r in j.reviews(limit=100)
                        if r.get("day") == day}
        for code in codes:
            try:
                if code not in done_snap:
                    prev, close, pct, _vol, _to = _kline_stats(code)
                    if close is not None:
                        j.add_event(eod_snapshot_event(code, prev, close, pct,
                                                       None, None))
                        out["events"] += 1
                if code in done_reviews:
                    continue  # 当日已生成过复盘
                review = journal_mod.build_daily_review(j, store, code, day)
                if review is not None:
                    j.add_review(day, code, review["summary"],
                                 lines=review["lines"], version="rule")
                    out["reviews"] += 1
            except Exception as e:  # noqa: BLE001 单股失败不影响其余
                out["errors"].append(f"{code}: {e}")
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
                 downloader_factory=None, syncer=None, check_interval: float = 20.0,
                 stall_timeout: float = 1200.0):
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
        # 停滞看门狗：running 且心跳超过 stall_timeout 秒未推进 -> 判定挂死
        # （2026-10-26 事故：baostock 裸 socket 无超时 + 全局锁，一条半开连接
        #  让 EOD 无声停摆 1 小时）。默认 20 分钟 = 全市场最慢单股冷却余量。
        self.stall_timeout = float(stall_timeout)
        self._heartbeat = 0.0           # monotonic 时间戳，阶段推进/单股完成时打点
        self._stall_reported = False    # 一次运行只告警+重启一次
        # 盘中股票列表定期刷新（分钟）：股票列表原本只在盘后 EOD 的 base 阶段
        # 刷新——新股上市当天白天不存在于 stock_list，全站搜不到、加不了自选
        # （2026-10-27 事故伴生问题）。交易时段内每 30 分钟刷一次。
        self.list_refresh_interval: float = 1800.0
        self._last_list_refresh = 0.0
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

        # mode=auto: 探测三新源健康则多源分批（全部不可用自动回退串行链路）
        return Downloader(data_dir=self.data_dir, adjust="hfq", mode="auto")

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
                self._watchdog()
                self._maybe_refresh_list(now)
                # 触发时刻前的短暂睡眠（最多 check_interval，便于优雅退出）
                delay = max(0.0, (fire - now).total_seconds())
                self._stop.wait(min(delay, self.check_interval) if delay else self.check_interval)
            except Exception as e:  # noqa: BLE001 调度线程绝不因单次异常退出
                time.sleep(self.check_interval)
                self._error(f"调度循环异常: {e}")

    # ------------------------------------------------------ 停滞看门狗

    def _maybe_refresh_list(self, now: dt.datetime) -> None:
        """交易时段内定期刷新股票列表（盘中新股当天可搜/可加自选）。

        仅工作日 09:20-15:05（盘前集合竞价前 10 分钟到收盘截止）触发；
        与 EOD 更新互斥（EOD 的 base 阶段自己会刷，避免同刻双请求）；
        失败静默——下次周期重试，绝不影响调度主循环。
        """
        if self._is_running():
            return
        if now.weekday() >= 5 or not (dt.time(9, 20) <= now.time() <= dt.time(15, 5)):
            return
        if time.monotonic() - self._last_list_refresh < self.list_refresh_interval:
            return
        self._last_list_refresh = time.monotonic()
        try:
            dl = self._downloader()
            dl.refresh_stock_list()
            print(f"[eod] 盘中股票列表已刷新（{now.strftime('%H:%M')}）", flush=True)
        except Exception as e:  # noqa: BLE001 静默降级：下个周期自动重试
            print(f"[eod] 盘中股票列表刷新失败（稍后重试）: {e}", flush=True)
            # 行情兜底（2026-11-02 事故）：列表源挂了也要让行情字段跟上，
            # 否则全站取价停在旧收盘。兜底也失败时 sidecar 不 bump。
            try:
                dl.refresh_quote_fields()
                print("[eod] 盘中行情已用腾讯实时行情兜底刷新", flush=True)
            except Exception as e2:  # noqa: BLE001 兜底失败同样静默重试
                print(f"[eod] 盘中行情兜底刷新失败（稍后重试）: {e2}", flush=True)

    def _beat(self) -> None:
        """心跳打点（阶段推进 / 单股完成）。"""
        self._heartbeat = time.monotonic()

    def _watchdog(self) -> None:
        """检测 EOD 更新停滞：running 且心跳超时未推进 -> 告警 + 请求进程重启。

        挂死通常发生在 C 扩展/裸 socket 的阻塞 read 上（GIL 不释放），
        进程内无法自救；退出交给 systemd Restart=on-failure（10s 拉起）。
        重启后 last_run_day 归零，调度器立即补跑当日，形成自愈闭环。
        """
        if not self._is_running() or self._stall_reported:
            return
        if self._heartbeat <= 0:
            self._beat()
            return
        stalled = time.monotonic() - self._heartbeat
        if stalled < self.stall_timeout:
            return
        self._stall_reported = True
        stage = self.status.get("stage_label") or self.status.get("stage") or "?"
        msg = (f"盘后更新停滞超过 {int(stalled // 60)} 分钟（阶段: {stage}），"
               "疑似数据源挂死，服务将自动重启并补跑")
        self._error(msg)
        try:
            self.notify.push(f"update:stall:{self.status.get('day') or ''}", msg,
                             level="warn", desktop=True, source="system")
        except Exception:  # noqa: BLE001
            pass
        import os

        os._exit(1)  # 非零退出 -> systemd Restart=on-failure 10s 内拉起

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
        self._heartbeat = time.monotonic()
        self._stall_reported = False
        try:  # 网络活跃心跳：每次真实 HTTP 尝试/baostock 池提交都打点。
            # 解决 2026-10-26 晚误杀：主源 WAF 拦截时单请求重试可耗 10+ 分钟，
            # 4 worker 全在途 -> 完成数型心跳 20 分钟无更新 -> 看门狗把「慢」判成「死」。
            from downloader import source as _dl_source

            _dl_source.set_heartbeat(self._beat)
        except Exception:  # noqa: BLE001
            pass
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
        try:  # 注销网络心跳（防泄漏：进程里后续 CLI 型调用不再触发 EOD 打点）
            from downloader import source as _dl_source

            _dl_source.set_heartbeat(None)
        except Exception:  # noqa: BLE001
            pass
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
        self._beat()

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
            # 行情兜底（2026-11-02 事故）：东财 clist 被封时 EOD list 阶段失败，
            # 但后续 build_dataset 仍会用旧 stock_list 重写 dataset/stock_basic
            # 副本（mtime=今天、内容=旧价）——不补行情，全站取价停在旧收盘。
            # 腾讯兜底刷新行情字段 + 写 sidecar；兜底也失败时 sidecar 不 bump，
            # 读取端 quote_date 保留旧值，陈旧快照不会被当最新。
            try:
                dl.refresh_quote_fields()
                self._merge_stats({"quote_fallback": 1})
            except Exception as e2:  # noqa: BLE001 兜底失败记入状态，主链路继续
                self._error(f"行情兜底刷新失败（腾讯实时行情）: {e2}")
        try:
            dl.refresh_trade_calendar()
        except Exception as e:  # noqa: BLE001
            self._error(f"交易日历刷新失败: {e}")

        # 2. 自选股日线优先
        self._stage("watchlist_daily")
        if codes:
            try:
                stats = dl.update_stocks(codes=",".join(codes), on_progress=self._beat)
                self._merge_stats({f"wl_{k}": v for k, v in (stats or {}).items()})
            except Exception as e:  # noqa: BLE001
                self._error(f"自选股日线更新失败: {e}")

        # 3. 自选股资讯刷新（新闻/估值/财报/资金流/公告/情绪，强制绕过缓存
        #    TTL；失败不中断主链路，但必须记入 status.errors——旧实现裸 except
        #    pass 让故障完全无痕，资金流可无限期陈旧）
        self._stage("watchlist_info")
        for c in codes:
            for fn in (self._refresh_news, self._refresh_valuation,
                       self._refresh_fundamental, self._refresh_flow,
                       self._refresh_announce, self._refresh_sentiment):
                try:
                    fn(c)
                except Exception as e:  # noqa: BLE001 资讯失败不影响主链路
                    self._error(f"自选资讯刷新失败 ({c} {fn.__name__}): {str(e)[:120]}")
                self._beat()  # 单股多源刷新可能很慢，逐项打点防看门狗误报

        # 4. 全市场日线（自选股刚更新过，增量判定为「已是最新」自动跳过，无需排除）
        self._stage("market_daily")
        try:
            stats = dl.update_stocks(on_progress=self._beat)
            self._merge_stats(stats)
        except Exception as e:  # noqa: BLE001
            self._error(f"全市场日线更新失败: {e}")

        # 4b. 单位断裂待修清单自动重试（全量重拉；清单为空时 no-op，不触网）
        if hasattr(dl, "repair_stocks"):
            try:
                self._merge_stats(dl.repair_stocks() or {})
            except Exception as e:  # noqa: BLE001
                self._error(f"单位断裂修复失败: {e}")

        # 4c. 当日成交额缺失防御（2026-11-02 事故：日线源全挂降级 sina 时，
        #     其日线 API 无 amount 字段，全市场 amount/turnover=NaN，市场页
        #     成交额只剩零头）。检测当日覆盖率，坏则用腾讯实时行情回补。
        if hasattr(dl, "refresh_daily_amounts"):
            try:
                dl.refresh_daily_amounts(day)
                chk = getattr(dl, "last_amount_check", None) or {}
                cov = chk.get("coverage")
                cov_s = f"{cov * 100:.1f}%" if cov is not None else "无数据"
                if chk.get("patched"):
                    print(f"[eod] 当日成交额覆盖率 {cov_s} -> "
                          f"已回补 {chk['patched']} 只", flush=True)
                else:
                    print(f"[eod] 当日成交额覆盖率 {cov_s} -> 正常", flush=True)
            except Exception as e:  # noqa: BLE001 回补失败不阻塞主链路
                self._error(f"当日成交额回补失败: {e}")

        # 4d. 量指纹守卫（2026-11-04 事故：sina CDN 盘中快照缓存被 15:33
        #     EOD 写成当日 bar，000 段 35 只量为真实量的 0.1%~70%）。
        #     当日 bar 量 × 腾讯实时行情对账，嫌疑删行+登记待修清单，
        #     次日 EOD repair_stocks 全量重拉自愈（宁缺勿错）。
        if hasattr(dl, "verify_daily_vintage"):
            try:
                chk = dl.verify_daily_vintage(day)
                print(f"[eod] 量指纹守卫: 对账 {chk['checked']} 只 / "
                      f"删行 {chk['dropped']} 只（{chk['skipped']} 只停牌/口径跳过）",
                      flush=True)
            except Exception as e:  # noqa: BLE001 守卫失败不阻塞主链路
                self._error(f"量指纹守卫失败: {e}")

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

        # 9. 自选股信号扫描 -> 消息提醒 + journal 事件
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
            try:
                write_signal_events(self.store, hits, day)
            except Exception as e:  # noqa: BLE001 journal 旁路失败不阻塞
                self._error(f"信号事件写入日记失败: {e}")
        with self._lock:
            self.status["signals"] = n_sig

        # 10. 交易日记收尾：盘后快照事件 + 规则版复盘（按用户隔离）
        self._stage("journal")
        try:
            jr = write_eod_journals(self.store, day)
            if jr.get("errors"):
                self._error(f"交易日记部分失败: {jr['errors'][0]}"
                            + (f" 等{len(jr['errors'])}项" if len(jr["errors"]) > 1 else ""))
            self._merge_stats({"journal_events": jr.get("events", 0),
                               "journal_reviews": jr.get("reviews", 0)})
        except Exception as e:  # noqa: BLE001
            self._error(f"交易日记写入失败: {e}")
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

    def _refresh_flow(self, code: str) -> None:
        from .flow import fetch

        # strict=True: EOD 刷新必须让失败可见（status.errors），不得静默回退旧缓存
        # 假装成功（2026-10-30 根因S4：资金流可无限期陈旧且无痕）。
        fetch(code, self.store.root, refresh=True, strict=True)

    def _refresh_announce(self, code: str) -> None:
        from .announce import fetch

        fetch(code, self.store.root, refresh=True)

    def _refresh_sentiment(self, code: str) -> None:
        from .sentiment import fetch

        fetch(code, self.store.root, refresh=True)
