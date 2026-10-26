"""盘后自动更新架构单测：消息中心 / 调度时间 / 阶段顺序 / 信号扫描 / 单例锁。

全部离线（fake Downloader / fake Runner），不触网、不依赖真实 data 目录。

运行: .venv/bin/python tests/test_eod_updates.py
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import threading
import traceback
from pathlib import Path

import pandas as pd

sys.path.insert(0, ".")

from analyzer.notify import NotificationCenter  # noqa: E402
from analyzer.updates import CST, EodUpdater, next_fire, scan_watchlist_signals  # noqa: E402

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
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        FAILED += 1
        return False


# ----------------------------------------------------------------- 替身


class FakeStore:
    def __init__(self, codes):
        self._wl_codes = list(codes)
        self.root = Path(tempfile.mkdtemp(prefix="fake-store-"))  # 离线: 隔离 root
        self.basic = pd.DataFrame({"name": ["茅台", "宁德"][: len(codes)]},
                                  index=self._wl_codes)
        self.reload_calls = 0

    def known_users(self):
        return []

    def watchlist_codes(self, user=None):
        return list(self._wl_codes)

    def kline(self, code, **kw):
        return None

    def reload(self):
        self.reload_calls += 1
        return {"n_stocks": len(self._wl_codes)}


class FakeDownloader:
    """记录调用顺序；可指定某些方法抛错验证容错。"""

    def __init__(self, fail=()):
        self.calls: list[str] = []
        self.fail = set(fail)

    def _c(self, name, ret=None):
        self.calls.append(name)
        if name in self.fail:
            raise RuntimeError(f"{name} 模拟失败")
        return ret if ret is not None else {}

    def refresh_stock_list(self):
        return self._c("refresh_stock_list")

    def refresh_trade_calendar(self):
        return self._c("refresh_trade_calendar")

    def update_stocks(self, codes=None, on_progress=None):
        return self._c("update_stocks_watchlist" if codes else "update_stocks",
                       {"updated": 2, "uptodate": 1})

    def update_indices(self):
        return self._c("update_indices", {"updated": 7})

    def refresh_boards(self, with_concept=False):
        return self._c("refresh_boards")

    def build_dataset(self):
        return self._c("build_dataset")


class FakeResult:
    def __init__(self, d):
        self._d = d

    def to_dict(self):
        return self._d


class FakeRunner:
    """按 (plugin, code) 返回预设结果 dict。"""

    def __init__(self, results):
        self.results = results
        self.calls = []

    def run(self, plugin, code, **kw):
        self.calls.append((plugin, code))
        d = self.results.get((plugin, code), {"ok": False})
        return FakeResult(dict(d, plugin=plugin, code=code))


def _mk_updater(tmp, codes, dl_kwargs=None, results=None):
    store = FakeStore(codes)
    dl = FakeDownloader(**(dl_kwargs or {}))
    nc = NotificationCenter(tmp / "data")
    runner = FakeRunner(results or {})
    up = EodUpdater(store, nc, data_dir=tmp / "data",
                    on_scan=lambda cs: scan_watchlist_signals(store, runner, cs),
                    runner=runner,
                    downloader_factory=lambda: dl)
    # 离线约定: 资讯刷新（阶段3）全部 no-op，不打真实数据源
    for _name in ("_refresh_news", "_refresh_valuation", "_refresh_fundamental",
                  "_refresh_flow", "_refresh_announce", "_refresh_sentiment"):
        setattr(up, _name, lambda c: None)
    return up, dl, nc, store


# ----------------------------------------------------------------- 消息中心


def t_notify_dedup():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        nc = NotificationCenter(tmp)
        r1 = nc.push("update:done:2026-10-23", "更新完成", level="ok", day="2026-10-23")
        assert r1 is not None
        assert nc.push("update:done:2026-10-23", "重复", day="2026-10-23") is None
        # 换天可再报
        assert nc.push("update:done:2026-10-23", "更新完成", day="2026-10-24") is not None
        # 当日文本完全相同 -> 去重（跨 key，防同一信号多渠道重复响）
        assert nc.push("signal:a", "⚡ 突破", code="600519.SH", day="2026-10-23") is not None
        assert nc.push("signal:b", "⚡ 突破", code="300687.SZ", day="2026-10-23") is None


def t_notify_persist_cursor():
    import json as _json

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        # 直接构造 JSONL（同一秒内 push 会撞时间戳，游标需要严格有序的时间）
        recs = [
            {"id": "a1", "key": "k1", "code": None, "level": "info", "text": "第一条",
             "time": "2026-01-01T00:00:00+00:00", "day": "2026-10-23", "read": False},
            {"id": "b2", "key": "k2", "code": "600519.SH", "level": "alert", "text": "第二条",
             "time": "2026-01-02T00:00:00+00:00", "day": "2026-10-23", "read": False},
        ]
        (tmp / "notifications.jsonl").write_text(
            "\n".join(_json.dumps(r, ensure_ascii=False) for r in recs) + "\n", "utf-8")
        # 新实例从 JSONL 恢复
        nc2 = NotificationCenter(tmp)
        out = nc2.list()
        assert len(out["messages"]) == 2
        assert out["messages"][0]["key"] == "k2"          # 时间倒序
        # since 游标只返回比它新的消息
        got = nc2.list(since="2026-01-01T12:00:00+00:00")["messages"]
        assert [m["key"] for m in got] == ["k2"], got
        assert nc2.mark_read(all=True) == 2
        assert nc2.list()["unread"] == 0
        # push 去重对恢复的消息同样生效
        assert nc2.push("k1", "不同文本", day="2026-10-23") is None


# ----------------------------------------------------------------- 调度时间


def t_next_fire_calendar():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        pd.DataFrame({"date": pd.to_datetime(
            [dt.date(2026, 10, 23), dt.date(2026, 10, 26)])}).to_parquet(
            p / "trade_calendar.parquet")
        # 交易日 14:00 -> 今日 15:30
        fire, wait = next_fire(dt.datetime(2026, 10, 23, 14, 0, tzinfo=CST), p)
        assert (fire.date(), fire.time()) == (dt.date(2026, 10, 23), dt.time(15, 30))
        assert wait is False
        # 已过触发点(15:31) -> 仍返回今日 15:30（由 last_run_day 防同日重跑；
        # 旧语义「跳下一交易日」会让 _loop 的 now>=fire 永不成立，调度成死代码）
        fire2, wait2 = next_fire(dt.datetime(2026, 10, 23, 15, 31, tzinfo=CST), p)
        assert (fire2.date(), fire2.time()) == (dt.date(2026, 10, 23), dt.time(15, 30))
        assert wait2 is False


def t_next_fire_catchup_regression():
    """2026-10-26 事故回归：交易日已过 15:30 必须仍指向今日，否则永远不触发。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        # 复刻服务器日历: 只覆盖到 2026-10-23（未来日期退化为工作日判断）
        pd.DataFrame({"trade_date": pd.date_range("2020-01-01", "2026-10-23", freq="B")}) \
            .to_parquet(p / "trade_calendar.parquet")
        # 事故现场: 周一 15:48，当日更新未跑
        fire, wait = next_fire(dt.datetime(2026, 10, 26, 15, 48, tzinfo=CST), p)
        assert (fire.date(), fire.time()) == (dt.date(2026, 10, 26), dt.time(15, 30)), fire
        assert wait is False
        # 周六 -> 下一个交易日周一（非交易日不回归当日）
        fire2, wait2 = next_fire(dt.datetime(2026, 10, 24, 10, 0, tzinfo=CST), p)
        assert fire2.date() == dt.date(2026, 10, 26) and wait2 is True


def t_next_fire_fallback():
    with tempfile.TemporaryDirectory() as d:
        # 无日历文件：退化为工作日判断。周六 -> 下周一
        sat = dt.datetime(2026, 10, 24, 10, 0, tzinfo=CST)
        fire, _ = next_fire(sat, Path(d))
        assert fire.date() == dt.date(2026, 10, 26), fire


# ----------------------------------------------------------------- 信号扫描


def t_scan_signals():
    results = {
        ("multi_factor_breakout", "600519.SH"): {
            "ok": True, "display_name": "多因子突破",
            "signals": [{"level": "alert", "text": "★ 放量突破"},
                        {"level": "info", "text": "普通提示"}],
            "metrics": {"signal_today": 1},
        },
        # 无 alert 但 signal_today=1 -> warn
        ("garch_mean_reversion", "600519.SH"): {
            "ok": True, "display_name": "GARCH", "signals": [],
            "metrics": {"signal_today": 1}, "summary": "波动收缩",
        },
        ("chip_distribution", "600519.SH"): {
            "ok": True, "signals": [], "metrics": {"signal_today": 0},
        },
    }
    runner = FakeRunner(results)
    hits = scan_watchlist_signals(FakeStore(["600519.SH"]), runner, ["600519.SH"],
                                  plugins=("multi_factor_breakout",
                                           "garch_mean_reversion", "chip_distribution"))
    assert len(hits) == 2, hits
    assert hits[0]["level"] == "alert" and "放量突破" in hits[0]["text"]
    assert hits[1]["level"] == "warn" and hits[1]["plugin"] == "garch_mean_reversion"


def t_scan_crash_isolation():
    class Boom:
        def run(self, plugin, code, **kw):
            raise RuntimeError("插件炸了")

    hits = scan_watchlist_signals(FakeStore(["600519.SH"]), Boom(), ["600519.SH"])
    assert hits == []


# ----------------------------------------------------------------- 更新链路


def t_stage_order_watchlist_first():
    with tempfile.TemporaryDirectory() as d:
        up, dl, nc, store = _mk_updater(Path(d), ["600519.SH", "300687.SZ"])
        st = up.run_now()
        assert dl.calls == ["refresh_stock_list", "refresh_trade_calendar",
                            "update_stocks_watchlist", "update_stocks",
                            "update_indices", "refresh_boards", "build_dataset"], dl.calls
        assert st["running"] is False
        assert st["errors"] == []
        assert store.reload_calls == 1
        assert st["stats"]["wl_updated"] == 2      # 自选股统计独立
        assert st["stats"]["updated"] == 9         # 全市场 2 + 指数 7 合并
        day = st["day"]
        msgs = {m["key"] for m in nc.list()["messages"]}
        assert f"update:start:{day}" in msgs
        assert f"update:done:{day}" in msgs


def t_stage_error_continues():
    with tempfile.TemporaryDirectory() as d:
        up, dl, nc, _ = _mk_updater(
            Path(d), ["600519.SH"],
            dl_kwargs={"fail": ("refresh_boards", "update_indices")})
        st = up.run_now()
        # 失败阶段之后仍继续（dataset/reload/scan）
        assert "build_dataset" in dl.calls
        assert len(st["errors"]) == 2, st["errors"]
        day = st["day"]
        assert any(m["key"] == f"update:error:{day}" and m["level"] == "warn"
                   for m in nc.list()["messages"])


def t_signal_notify_integration():
    with tempfile.TemporaryDirectory() as d:
        results = {("multi_factor_breakout", "600519.SH"): {
            "ok": True, "signals": [{"level": "alert", "text": "★ 放量突破"}], "metrics": {}}}
        up, _dl, nc, _ = _mk_updater(Path(d), ["600519.SH"], results=results)
        st = up.run_now()
        assert st["signals"] == 1
        msgs = [m for m in nc.list()["messages"] if m["level"] == "alert"]
        assert len(msgs) == 1 and "600519" in msgs[0]["text"]


def t_trigger_singleton():
    with tempfile.TemporaryDirectory() as d:
        started = threading.Event()
        release = threading.Event()

        class SlowDl(FakeDownloader):
            def refresh_stock_list(self):
                started.set()
                assert release.wait(5)
                return super().refresh_stock_list()

        dl = SlowDl()
        up = EodUpdater(FakeStore(["600519.SH"]), NotificationCenter(Path(d) / "data"),
                        data_dir=Path(d), on_scan=lambda cs: [],
                        downloader_factory=lambda: dl)
        for _name in ("_refresh_news", "_refresh_valuation", "_refresh_fundamental",
                      "_refresh_flow", "_refresh_announce", "_refresh_sentiment"):
            setattr(up, _name, lambda c: None)  # 离线: 资讯刷新 no-op
        assert up.trigger(source="manual")["ok"] is True
        assert started.wait(5)
        assert up.get_status()["running"] is True
        # 运行中再次触发被拒
        assert up.trigger(source="manual")["ok"] is False
        release.set()
        for _ in range(100):
            if not up.get_status()["running"]:
                break
            import time as _t
            _t.sleep(0.05)
        assert up.get_status()["running"] is False
        # 同日 schedule 补跑被拒（manual 不受限）
        assert up.trigger(source="schedule")["ok"] is False


def t_datastore_reload_api():
    """DataStore.reload/clear_caches 与 Context.clear_cache 接口存在且可用（轻量）。"""
    from analyzer.datastore import DataStore
    from stock_analysis.core.context import AnalysisContext

    assert hasattr(DataStore, "reload") and hasattr(DataStore, "clear_caches")

    class FakeDataStore:
        daily_dir = Path("data")
        meta = {}

        def __init__(self):
            self._kline_cache = {("600519.SH", None, None, 20): "x"}

    ctx = AnalysisContext(FakeDataStore())
    ctx.clear_cache()
    assert ctx._kline_cache == {}


# ----------------------------------------------------------------- 看门狗


def t_watchdog_stall_triggers_exit():
    """running + 心跳超时 -> _error + 通知 + os._exit(1)。"""
    import sys as _sys
    import time as _t

    with tempfile.TemporaryDirectory() as d:
        up, _dl, nc, _ = _mk_updater(Path(d), ["600519.SH"])
        up.run_now()  # 完整跑一轮（结束后 running=False）
        # 伪造运行中 + 心跳过期
        up.status["running"] = True
        up.status["stage_label"] = "全市场日线"
        up._heartbeat = _t.monotonic() - (up.stall_timeout + 60)
        exit_codes = []
        real_exit = _sys.modules["os"]._exit
        _sys.modules["os"]._exit = exit_codes.append
        try:
            up._watchdog()
        finally:
            _sys.modules["os"]._exit = real_exit
        assert exit_codes == [1], exit_codes
        assert up._stall_reported is True
        assert any("停滞" in e for e in up.status["errors"]), up.status["errors"]
        assert any(m["key"].startswith("update:stall") for m in nc.list()["messages"])


def t_watchdog_fresh_heartbeat_no_exit():
    """心跳新鲜 / 未运行 -> 都不触发。"""
    import time as _t

    with tempfile.TemporaryDirectory() as d:
        up, _dl, _nc, _ = _mk_updater(Path(d), ["600519.SH"])
        up.run_now()
        up.status["running"] = True
        up._heartbeat = _t.monotonic()  # 刚打点
        up._watchdog()
        assert up._stall_reported is False
        up.status["running"] = False
        up._watchdog()
        assert up._stall_reported is False


def t_watchdog_progress_beat():
    """on_progress 心跳透传到 update_stocks；完整链路推进会打点。"""

    class BeatDl(FakeDownloader):
        def update_stocks(self, codes=None, on_progress=None):
            self.calls.append("update_stocks_watchlist" if codes else "update_stocks")
            if on_progress is not None:
                on_progress(1, 3)
                on_progress(2, 3)
            return {"updated": 2, "uptodate": 1}

    with tempfile.TemporaryDirectory() as d:
        store = FakeStore(["600519.SH"])
        dl = BeatDl()
        nc = NotificationCenter(Path(d) / "data")
        up = EodUpdater(store, nc, data_dir=Path(d) / "data",
                        on_scan=lambda cs: [], downloader_factory=lambda: dl)
        for _name in ("_refresh_news", "_refresh_valuation", "_refresh_fundamental",
                      "_refresh_flow", "_refresh_announce", "_refresh_sentiment"):
            setattr(up, _name, lambda c: None)
        import time as _t
        before = up._heartbeat
        _t.sleep(0.01)
        up.run_now()
        assert up._heartbeat > before  # 链路推进打过点
        assert up.get_status()["running"] is False


def t_bs_pool_routing_offline():
    """_bs_query 路由：池死 -> 进程内；池返回数据 -> 直接用（离线 mock，不 spawn）。"""
    from downloader import source as src

    saved = (src._BS_POOL, src._BS_POOL_DEAD)
    try:
        # 1) 池标记死亡 -> 走 _bs_query_inproc
        src._BS_POOL, src._BS_POOL_DEAD = None, True
        called = {}

        def fake_inproc(code, start, end, flag, _attempt=1):
            called["inproc"] = (code, flag)
            return [["2026-10-26", code, "1.0"]]

        real_inproc = src._bs_query_inproc
        src._bs_query_inproc = fake_inproc
        try:
            rows = src._bs_query("sz.000001", "20261026", "20261026", "1")
        finally:
            src._bs_query_inproc = real_inproc
        assert rows == [["2026-10-26", "sz.000001", "1.0"]]
        assert called["inproc"] == ("sz.000001", "1")

        # 2) 池可用且返回数据 -> 不走进程内
        class _Fut:
            def result(self, timeout=None):
                return [["2026-10-26", "sz.000001", "2.0"]]

        class _Pool:
            def submit(self, *a):
                return _Fut()

        src._BS_POOL_DEAD = False
        src._BS_POOL = _Pool()
        rows = src._bs_query("sz.000001", "20261026", "20261026", "1")
        assert rows == [["2026-10-26", "sz.000001", "2.0"]]
    finally:
        src._BS_POOL, src._BS_POOL_DEAD = saved


def t_update_stocks_on_progress():
    """downloader.update_stocks 的 on_progress 每只回调（离线 mock fetch）。"""
    import pandas as pd
    from downloader.app import Downloader

    with tempfile.TemporaryDirectory() as d:
        dl = Downloader(data_dir=Path(d), adjust="hfq")
        pd.DataFrame({"code": ["000001.SZ", "000002.SZ"], "name": ["平安", "万科"]}) \
            .to_parquet(Path(d) / "stock_list.parquet")
        import downloader.app as app_mod

        def fake_fetch(wind_code, start, end, adjust):
            return pd.DataFrame({
                "date": pd.to_datetime(["2026-10-26"]),
                "code": [wind_code], "open": [1.0], "high": [1.0], "low": [1.0],
                "close": [1.0], "volume": [100], "amount": [100.0],
                "amplitude": [0.0], "pct_chg": [0.0], "change": [0.0], "turnover": [0.0],
            })

        real_fetch = app_mod.source.fetch_daily
        app_mod.source.fetch_daily = fake_fetch
        beats = []
        try:
            stats = dl.update_stocks(codes="000001.SZ,000002.SZ",
                                     on_progress=lambda done, total: beats.append((done, total)))
        finally:
            app_mod.source.fetch_daily = real_fetch
        assert stats.get("new") == 2, stats
        assert beats == [(1, 2), (2, 2)], beats


def main() -> int:
    tests = [
        ("消息中心: 当日去重", t_notify_dedup),
        ("消息中心: 持久化/游标/已读", t_notify_persist_cursor),
        ("调度: 交易日历 next_fire", t_next_fire_calendar),
        ("调度: 事故回归 过点仍指今日(补跑)", t_next_fire_catchup_regression),
        ("调度: 无日历退化工作日", t_next_fire_fallback),
        ("扫描: alert/warn 信号提取", t_scan_signals),
        ("扫描: 插件异常隔离", t_scan_crash_isolation),
        ("链路: 阶段顺序自选股优先", t_stage_order_watchlist_first),
        ("链路: 单阶段失败不中断", t_stage_error_continues),
        ("链路: 信号->消息提醒", t_signal_notify_integration),
        ("调度: 单例锁与同日重跑拒绝", t_trigger_singleton),
        ("接口: reload/清缓存存在", t_datastore_reload_api),
        ("看门狗: 停滞触发告警+退出", t_watchdog_stall_triggers_exit),
        ("看门狗: 心跳新鲜不误报", t_watchdog_fresh_heartbeat_no_exit),
        ("看门狗: on_progress 心跳打点", t_watchdog_progress_beat),
        ("baostock: 池路由/回退(离线)", t_bs_pool_routing_offline),
        ("downloader: on_progress 透传", t_update_stocks_on_progress),
    ]
    for name, fn in tests:
        check(name, fn)
    print(f"\n===== 汇总: {PASSED} PASS / {FAILED} FAIL =====")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
