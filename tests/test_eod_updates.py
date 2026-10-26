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
        self.root = Path("data")
        self.basic = pd.DataFrame({"name": ["茅台", "宁德"][: len(codes)]},
                                  index=self._wl_codes)
        self.reload_calls = 0

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

    def update_stocks(self, codes=None):
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
        # 已过触发点 -> 下一交易日（周末按日历跳过）
        fire2, _ = next_fire(dt.datetime(2026, 10, 23, 15, 31, tzinfo=CST), p)
        assert fire2.date() == dt.date(2026, 10, 26)
        assert fire2.time() == dt.time(15, 30)


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


def main() -> int:
    tests = [
        ("消息中心: 当日去重", t_notify_dedup),
        ("消息中心: 持久化/游标/已读", t_notify_persist_cursor),
        ("调度: 交易日历 next_fire", t_next_fire_calendar),
        ("调度: 无日历退化工作日", t_next_fire_fallback),
        ("扫描: alert/warn 信号提取", t_scan_signals),
        ("扫描: 插件异常隔离", t_scan_crash_isolation),
        ("链路: 阶段顺序自选股优先", t_stage_order_watchlist_first),
        ("链路: 单阶段失败不中断", t_stage_error_continues),
        ("链路: 信号->消息提醒", t_signal_notify_integration),
        ("调度: 单例锁与同日重跑拒绝", t_trigger_singleton),
        ("接口: reload/清缓存存在", t_datastore_reload_api),
    ]
    for name, fn in tests:
        check(name, fn)
    print(f"\n===== 汇总: {PASSED} PASS / {FAILED} FAIL =====")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
