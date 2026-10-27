"""增量更新缺口回归：盘中价当收盘价 / 新股排队垫底 / 档案副本滞后 / 新股白天不可见。

背景（2026-10-27 事故）:
  1. 盘中部分更新/补跑把当日未收盘的 bar 写进本地日线文件，收盘后的 EOD
     增量按「最后日期+1」判定为已是最新直接跳过——约 1975 只股票（含全部
     自选股）整天停留在盘中价，真实收盘价永远缺失，信号扫描全错；
  2. 全市场队列按代码排序，新股代码段(300xxx/301xxx/688xxx)在队尾，
     EOD 中途卡死重启补跑时新股永远轮不到，上市多日仍无 K 线；
  3. dataset/stock_basic 归档副本只在 build_dataset（EOD 第 7 阶段）里复制，
     EOD 全链路跑不完时副本滞后数天，依赖它的路径看不到新上市股票；
  4. 股票列表只在盘后 EOD 的 base 阶段刷新，新股上市当天白天全站搜不到。

全部离线（不触网）。运行: .venv/bin/python tests/test_daily_incremental.py
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import traceback
from pathlib import Path

import pandas as pd

sys.path.insert(0, ".")

from analyzer import updates  # noqa: E402
from downloader import store  # noqa: E402
from downloader.app import (  # noqa: E402
    CLOSE_CUTOFF,
    Downloader,
    _written_intraday,
)

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


# ----------------------------------------------------------------- 工具


def _cst_epoch(day: str, hhmm: str) -> float:
    """CST 本地时刻 -> epoch 秒（模拟服务器文件 mtime）。"""
    t = pd.Timestamp(f"{day} {hhmm}", tz="Asia/Shanghai")
    return t.timestamp()


def _old_with_last_bar(last_day: str, n: int = 30) -> pd.DataFrame:
    days = pd.bdate_range(end=last_day, periods=n)
    return pd.DataFrame({
        "date": days, "code": "000001.SZ", "close": range(1, n + 1),
    })


# ----------------------------------------------------------------- 1. 盘中写入的当日 bar

def test_written_intraday_detection():
    last = pd.Timestamp("2026-10-27")
    # 当天盘中（10:38）写入 -> 判定盘中
    assert _written_intraday(last, _cst_epoch("2026-10-27", "10:38")) is True
    # 当天 14:59 写入（仍早于 15:05 截止）-> 盘中
    assert _written_intraday(last, _cst_epoch("2026-10-27", "14:59")) is True
    # 当天收盘后（15:33，正常 EOD 写入）-> 已收盘
    assert _written_intraday(last, _cst_epoch("2026-10-27", "15:33")) is False
    # 写入日 != K线日（次日重写/修复脚本）-> 已收盘
    assert _written_intraday(last, _cst_epoch("2026-10-28", "09:00")) is False
    # 无 mtime 信息 -> 无法判定，保守按已收盘
    assert _written_intraday(last, None) is False
    # 收盘截止必须是 15:05 之后
    assert CLOSE_CUTOFF >= dt.time(15, 5)


def test_next_start_intraday_bar_refetched_after_close():
    """本地已有「今日」bar 且为盘中写入 -> 收盘后增量必须重取当日覆盖。"""
    dl = Downloader.__new__(Downloader)  # 跳过 __init__（不触网/不建 HTTP 客户端）
    dl.start = "20200101"
    old = _old_with_last_bar("2026-10-27")
    # 盘中写入 + 收盘后跑 EOD -> 从当日重取（非 uptodate！）
    start, skip = dl._next_start(old, "20261027", full=False, incremental_ok=True,
                                 mtime=_cst_epoch("2026-10-27", "10:38"))
    assert skip == "" and start == "20261027"
    # 收盘后写入 -> 正常跳过
    start, skip = dl._next_start(old, "20261027", full=False, incremental_ok=True,
                                 mtime=_cst_epoch("2026-10-27", "15:33"))
    assert skip == "uptodate"
    # mtime 未知 -> 保守跳过（老行为）
    start, skip = dl._next_start(old, "20261027", full=False, incremental_ok=True,
                                 mtime=None)
    assert skip == "uptodate"
    # K线日 != end（历史回填 end=昨天，今天的 mtime）-> 跳过
    start, skip = dl._next_start(old, "20261026", full=False, incremental_ok=True,
                                 mtime=_cst_epoch("2026-10-27", "10:38"))
    assert skip == "uptodate"
    # 服务器时区不是 CST（mtime 换算错 8 小时也不误判）：
    # UTC 时间 2026-10-27 02:38 == CST 10:38，按 CST 判定仍是盘中
    utc_epoch = pd.Timestamp("2026-10-27 02:38", tz="UTC").timestamp()
    assert _written_intraday(pd.Timestamp("2026-10-27"), utc_epoch) is True


def test_next_start_normal_incremental_unchanged():
    """既有增量语义不回归：缺今日 bar 时按最后日期+1 追加。"""
    dl = Downloader.__new__(Downloader)
    dl.start = "20200101"
    old = _old_with_last_bar("2026-10-23")  # 周五（bdate_range 截到最后一个工作日）
    start, skip = dl._next_start(old, "20261027", full=False, incremental_ok=True,
                                 mtime=_cst_epoch("2026-10-23", "16:00"))
    # 增量起点 = 最后日期+1（周六；源对周末无数据，周一/周二 bar 正常返回）
    assert skip == "" and start == "20261024"
    # full 模式整段重下
    start, skip = dl._next_start(old, "20261027", full=True, incremental_ok=True,
                                 mtime=None)
    assert skip == "" and start == "20200101"
    # qfq（incremental_ok=False）整段重下
    start, skip = dl._next_start(old, "20261027", full=False, incremental_ok=False,
                                 mtime=None)
    assert skip == "" and start == "20200101"


# ----------------------------------------------------------------- 2. 新股优先排队


def test_new_stocks_sorted_first():
    """全市场队列：无本地日线文件的股票（新股）排最前，已有文件顺序不变。"""
    with tempfile.TemporaryDirectory() as tmp:
        ddir = store.daily_dir(tmp, "hfq")
        ddir.mkdir(parents=True, exist_ok=True)
        for c in ("000001.SZ", "000002.SZ"):
            store.save_parquet(ddir / f"{c}.parquet", _old_with_last_bar("2026-10-24", 3))
        targets = [("000001.SZ", "平安银行"), ("301718.SZ", "新股A"),
                   ("000002.SZ", "万科A"), ("688801.SH", "新股B")]
        out = sorted(targets, key=lambda t: (ddir / f"{t[0]}.parquet").exists())
        # 无文件的（新股）在最前，且保持原有相对顺序；已有文件的顺序不变
        assert [t[0] for t in out] == ["301718.SZ", "688801.SH",
                                       "000001.SZ", "000002.SZ"]


# ----------------------------------------------------------------- 3. stock_basic 副本随列表刷新


def test_refresh_stock_list_writes_basic_copy():
    """refresh_stock_list 必须同步落 dataset/stock_basic 副本（不再依赖 EOD 跑到第 7 阶段）。"""
    with tempfile.TemporaryDirectory() as tmp:
        df = pd.DataFrame({"code": ["000001.SZ", "301718.SZ"],
                           "name": ["平安银行", "新股A"]})
        import downloader.source as source_mod
        from downloader.ratelimit import with_retry as _wr
        orig = source_mod.fetch_stock_list
        source_mod.fetch_stock_list = lambda: df
        try:
            dl = Downloader.__new__(Downloader)
            dl.data_dir = Path(tmp)
            dl.adjust = "hfq"
            dl.start = "20200101"
            # monkeypatch with_retry 直接执行
            import downloader.app as app_mod
            orig_wr = app_mod.with_retry
            app_mod.with_retry = lambda fn, **kw: fn()
            try:
                dl.refresh_stock_list()
            finally:
                app_mod.with_retry = orig_wr
        finally:
            source_mod.fetch_stock_list = orig
        basic = store.dataset_dir(tmp) / "stock_basic.parquet"
        assert basic.exists(), "stock_basic 副本未随列表刷新"
        got = pd.read_parquet(basic)
        assert list(got["code"]) == ["000001.SZ", "301718.SZ"]


# ----------------------------------------------------------------- 4. 盘中定期刷列表


class _Store:
    watchlist_codes = staticmethod(lambda: ["300687.SZ"])


class _Notify:
    def push(self, *a, **kw):
        pass


class _ListRecorder:
    def __init__(self):
        self.n = 0

    def refresh_stock_list(self):
        self.n += 1
        return pd.DataFrame({"code": []})


def _updater(recorder, interval=1800.0):
    return updates.EodUpdater(
        _Store(), _Notify(), data_dir=tempfile.mkdtemp(),
        downloader_factory=lambda: recorder,
        check_interval=1.0,
    ), recorder


def test_intraday_list_refresh_window():
    """交易时段内触发盘中列表刷新；非时段/周末/间隔内/运行中不触发。"""
    up, rec = _updater(_ListRecorder(), interval=1800.0)
    up._last_list_refresh = 0.0
    tue = dt.date(2026, 10, 27)  # 周二（工作日退化判定，无日历文件）
    # 盘中 10:00 -> 刷新
    up._maybe_refresh_list(dt.datetime.combine(tue, dt.time(10, 0)))
    assert rec.n == 1
    # 30 分钟内 -> 不重复刷
    up._maybe_refresh_list(dt.datetime.combine(tue, dt.time(10, 5)))
    assert rec.n == 1
    # 超过间隔但盘前 08:00 -> 不刷
    up._last_list_refresh = 0.0
    up._maybe_refresh_list(dt.datetime.combine(tue, dt.time(8, 0)))
    assert rec.n == 1
    # 盘后 15:30（EOD 会刷）-> 不刷
    up._maybe_refresh_list(dt.datetime.combine(tue, dt.time(15, 30)))
    assert rec.n == 1
    # 周末 -> 不刷
    up._maybe_refresh_list(dt.datetime(2026, 10, 31, 10, 0))  # 周六
    assert rec.n == 1
    # EOD 运行中 -> 不刷（避免与 base 阶段同刻双请求）
    up.status["running"] = True
    up._maybe_refresh_list(dt.datetime.combine(tue, dt.time(11, 0)))
    assert rec.n == 1
    up.status["running"] = False
    # 次日盘中 -> 恢复刷新
    up._maybe_refresh_list(dt.datetime(2026, 10, 28, 9, 30))
    assert rec.n == 2


def test_intraday_list_refresh_failure_silent():
    """列表刷新失败静默：不影响调度线程，退避一个周期。"""
    class _Boom:
        def refresh_stock_list(self):
            raise RuntimeError("网络挂了")

    up, _ = _updater(_Boom())
    up._last_list_refresh = 0.0
    tue = dt.date(2026, 10, 27)
    up._maybe_refresh_list(dt.datetime.combine(tue, dt.time(10, 0)))  # 不抛
    # 失败同样退避一个间隔
    up._maybe_refresh_list(dt.datetime.combine(tue, dt.time(10, 5)))
    assert up._last_list_refresh > 0


if __name__ == "__main__":
    check("1. 盘中写入判定 _written_intraday", test_written_intraday_detection)
    check("2. 收盘后重取盘中当日 bar", test_next_start_intraday_bar_refetched_after_close)
    check("3. 既有增量语义不回归", test_next_start_normal_incremental_unchanged)
    check("4. 新股优先排队", test_new_stocks_sorted_first)
    check("5. stock_basic 副本随列表刷新", test_refresh_stock_list_writes_basic_copy)
    check("6. 盘中列表刷新窗口/互斥/节流", test_intraday_list_refresh_window)
    check("7. 盘中列表刷新失败静默", test_intraday_list_refresh_failure_silent)
    print(f"\n===== 结果: {PASSED} passed, {FAILED} failed =====")
    sys.exit(1 if FAILED else 0)
