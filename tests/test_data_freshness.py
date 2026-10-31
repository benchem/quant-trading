"""数据新鲜度修复单测（2026-10-30 生产事故：全市场日线大面积落后）。

覆盖五条症状的修复：
1. 空窗口/无进展不再无条件「已是最新」：adjust=none 同窗口交叉核对区分
   「停牌(uptodate)」与「源缺数据(stale)」；窗口无交易日直接 uptodate。
2. 复权单位断裂自愈：守卫命中 -> 待修清单 pending_repair_{adjust}.txt +
   全量重拉再过守卫 -> repaired / skipped；CLI `repair` 手动补修。
3. 指数新鲜度判定：_em_index_daily 全市场无数据必须 raise（激活 em->tx->bs
   降级）；指数空窗/无进展按交易日历判 stale/uptodate。
4. 股票列表完整性守卫：截断/家族缺失拒绝写盘保留旧列表；分页空页 raise。
5. 资金流 EOD 刷新：strict 失败必须 raise 且记入状态，不再静默吞错。

全部离线（mock 数据源/HTTP 层），不触网。运行: .venv/bin/pytest tests/test_data_freshness.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, ".")

from analyzer import flow as flow_mod  # noqa: E402
from analyzer.notify import NotificationCenter  # noqa: E402
from analyzer.updates import EodUpdater  # noqa: E402
from downloader import source, store  # noqa: E402
from downloader.app import Downloader  # noqa: E402
from downloader.source import FetchError  # noqa: E402


# ----------------------------------------------------------------- 替身


class FakeClient:
    """替身 source._client：按脚本顺序返回预设响应（列表/指数分页测试用）。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get_json(self, url, params):
        self.calls.append((url, dict(params)))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture(autouse=True)
def _reset_source_state():
    """_SourceManager / 指数 secid 缓存是模块级单例，逐测试复位。"""
    m = source._mgr
    m.em_failures = 0
    m.primary = "eastmoney"
    m.tx_calls = 0
    m._cooldown_until = 0.0
    saved_market = dict(source._EM_INDEX_MARKET)
    yield
    m.em_failures = 0
    m.primary = "eastmoney"
    m.tx_calls = 0
    m._cooldown_until = 0.0
    source._EM_INDEX_MARKET.clear()
    source._EM_INDEX_MARKET.update(saved_market)


def _empty_ohlcv() -> pd.DataFrame:
    return pd.DataFrame(columns=source.OHLCV_COLUMNS)


def _df(dates, closes, code: str = "600001.SH") -> pd.DataFrame:
    return pd.DataFrame({"date": pd.to_datetime(dates), "close": closes,
                         "code": [code] * len(dates)})


def _mk_dl(root: Path, adjust: str = "hfq", start: str = "20260901") -> Downloader:
    d = Downloader.__new__(Downloader)  # 跳过 __init__（不触网/不改全局 HTTP 配置）
    d.data_dir = root
    d.adjust = adjust
    d.start = start
    d.workers = 2
    d.rate = 0
    d.rotator = None
    return d


def _write_daily(root: Path, code: str, last: str, n: int = 30,
                 adjust: str = "hfq") -> Path:
    days = pd.bdate_range(end=last, periods=n)
    p = store.daily_dir(root, adjust) / f"{code}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": days, "code": code,
                  "close": [10.0 + i * 0.1 for i in range(n)]}).to_parquet(p, index=False)
    return p


def _write_index(root: Path, code: str, last: str, n: int = 30) -> Path:
    days = pd.bdate_range(end=last, periods=n)
    p = store.index_dir(root) / f"{code}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": days, "code": code,
                  "close": [3000.0 + i for i in range(n)]}).to_parquet(p, index=False)
    return p


def _write_calendar(root: Path, last: str = "2026-12-31") -> None:
    pd.DataFrame({"trade_date": pd.bdate_range("2026-09-01", last)}).to_parquet(
        store.trade_cal_path(root), index=False)


def _clist_item(code: str) -> dict:
    return {"f12": code, "f14": "测试", "f2": 10.0, "f3": 0.5,
            "f8": 1.0, "f20": 1e9, "f21": 1e9}


def _clist_resp(total: int, codes) -> dict:
    return {"data": {"total": total, "diff": [_clist_item(c) for c in codes]}}


# ============================================================ Wave 1: source.py
# 指数: _em_index_daily 全市场无数据 -> raise（激活降级链）


def test_em_index_daily_all_empty_raises(monkeypatch):
    """东财所有 secid 市场前缀都无 K 线时必须报错，不能返回空 df 假装成功
    （旧行为让 with_fallback 不降级，000688.SH 指数停在旧数据）。"""
    empty = {"data": {"klines": []}}
    fake = FakeClient([empty, empty, empty])  # 000688.SH 依次试 1/0/2 三个市场
    monkeypatch.setattr(source, "_client", fake)
    with pytest.raises(FetchError):
        source._em_index_daily("000688.SH", "20261023", "20261030")


def test_fetch_index_daily_em_empty_falls_back_to_tx(monkeypatch):
    """端到端: 东财全市场空 -> with_fallback 降级腾讯拿到数据。"""
    em_empty = {"data": {"klines": []}}
    tx_rows = [["2026-10-23", "100", "101", "102", "99", "1000"],
               ["2026-10-26", "101", "103", "104", "100", "1100"]]
    tx_resp = {"data": {"sh000688": {"day": tx_rows}}}
    fake = FakeClient([em_empty, em_empty, em_empty, tx_resp])
    monkeypatch.setattr(source, "_client", fake)
    df = source.fetch_index_daily("000688.SH", "20261023", "20261030")
    assert len(df) == 2, df
    assert list(df["date"]) == [pd.Timestamp("2026-10-23"), pd.Timestamp("2026-10-26")]
    assert (df["code"] == "000688.SH").all()
    # 东财请求了 3 个市场前缀 + 腾讯 1 次
    em_calls = [c for c in fake.calls if "push2his" in c[0]]
    tx_calls = [c for c in fake.calls if "gtimg" in c[0] or "qq.com" in c[0]]
    assert len(em_calls) == 3 and len(tx_calls) == 1


# 股票列表: 分页空页 / total 不符必须 raise（2026-10-26 截断事故）


def test_em_stock_list_full_success(monkeypatch):
    """分页完整（len(items) >= total 正常终止）-> 正常返回。"""
    p1 = [f"600{i:03d}" for i in range(500)]
    p2 = [f"601{i:03d}" for i in range(500)]
    fake = FakeClient([_clist_resp(1000, p1), _clist_resp(1000, p2)])
    monkeypatch.setattr(source, "_client", fake)
    df = source._em_stock_list()
    assert len(df) == 1000
    assert "600000.SH" in set(df["code"])


def test_em_stock_list_empty_page_raises(monkeypatch):
    """中途空页（重试同一页后仍空）-> 分页异常必须 raise，禁止静默截断落盘。"""
    p1 = [f"600{i:03d}" for i in range(500)]
    empty_page = {"data": {"total": 1000, "diff": []}}
    fake = FakeClient([_clist_resp(1000, p1), empty_page, empty_page])
    monkeypatch.setattr(source, "_client", fake)
    with pytest.raises(FetchError):
        source._em_stock_list()


def test_em_stock_list_total_mismatch_raises(monkeypatch):
    """服务端 total 虚高导致翻页提前遇到空页（实际数据 < total）-> raise。"""
    p1 = [f"600{i:03d}" for i in range(500)]
    p2 = [f"601{i:03d}" for i in range(200)]
    empty_page = {"data": {"total": 1000, "diff": []}}
    fake = FakeClient([_clist_resp(1000, p1), _clist_resp(1000, p2),
                       empty_page, empty_page])
    monkeypatch.setattr(source, "_client", fake)
    with pytest.raises(FetchError):
        source._em_stock_list()


# ============================================================ Wave 2: app.py
# 空窗/无进展: none 同窗口交叉核对区分「停牌」与「源缺数据」


def _fake_fetch_factory(hfq_result, none_result, record=None):
    """按 adjust 分派假数据源（hfq=主请求, none=交叉核对）。"""
    def fake_fetch(wind_code, start, end, adjust):
        if record is not None:
            record.append((wind_code, start, end, adjust))
        result = none_result if adjust == "none" else hfq_result
        if isinstance(result, Exception):
            raise result
        return result
    return fake_fetch


def test_update_stock_one_empty_window_source_gap_stale(monkeypatch, tmp_path):
    """窗口空 + 不复权交叉核对有 bar -> stale（源缺数据），不得标 uptodate、不得写盘。

    2026-10-30 根因A: 550 只股票 10-30 有成交但 hfq 窗口空，被误判「已是最新」。
    """
    d = _mk_dl(tmp_path)
    code = "000001.SZ"
    p = _write_daily(tmp_path, code, "2026-10-29")
    _write_calendar(tmp_path)
    rec: list = []
    monkeypatch.setattr(source, "fetch_daily", _fake_fetch_factory(
        _empty_ohlcv(), _df(["2026-10-30"], [10.0], code), rec))
    c, status, msg = d._update_stock_one(code, "测试", "20261030", False)
    assert c == code and status == "stale", (status, msg)
    assert "交叉核对" in msg or "数据缺口" in msg, msg
    # 未写盘: 本地仍是 10-29 截止的旧数据
    after = pd.read_parquet(p)
    assert pd.Timestamp(after["date"].max()) == pd.Timestamp("2026-10-29")
    assert len(after) == 30
    # 交叉核对确实用了同窗口 [20261030, 20261030] 的 none 请求
    assert ("000001.SZ", "20261030", "20261030", "none") in rec


def test_update_stock_one_empty_window_suspended_uptodate(monkeypatch, tmp_path):
    """窗口空 + 不复权交叉核对也空 -> uptodate（停牌，明确判定依据）。"""
    d = _mk_dl(tmp_path)
    code = "000002.SZ"
    p = _write_daily(tmp_path, code, "2026-10-29")
    _write_calendar(tmp_path)
    monkeypatch.setattr(source, "fetch_daily", _fake_fetch_factory(
        _empty_ohlcv(), _empty_ohlcv()))
    c, status, msg = d._update_stock_one(code, "测试", "20261030", False)
    assert status == "uptodate", (status, msg)
    after = pd.read_parquet(p)
    assert len(after) == 30


def test_update_stock_one_no_progress_stale(monkeypatch, tmp_path):
    """返回的 bar 全在窗口起点之前（源回退/越界数据）-> 视同无进展 -> stale。

    2026-10-30 根因B: 393 只「写入却无新 bar」——窗口请求 10-30 却拿到 <=10-29
    的旧 bar 被当 updated 写入。
    """
    d = _mk_dl(tmp_path)
    code = "000003.SZ"
    p = _write_daily(tmp_path, code, "2026-10-29")
    _write_calendar(tmp_path)
    monkeypatch.setattr(source, "fetch_daily", _fake_fetch_factory(
        _df(["2026-10-29"], [9.9], code),  # 窗口 [10-30,10-30] 却回 10-29
        _df(["2026-10-30"], [10.0], code)))
    c, status, msg = d._update_stock_one(code, "测试", "20261030", False)
    assert status == "stale", (status, msg)
    after = pd.read_parquet(p)
    assert pd.Timestamp(after["date"].max()) == pd.Timestamp("2026-10-29")


def test_update_stock_one_window_no_trading_day_uptodate(monkeypatch, tmp_path):
    """窗口内无交易日（周六）-> 直接 uptodate，不做 none 交叉核对请求。"""
    d = _mk_dl(tmp_path)
    code = "000004.SZ"
    _write_daily(tmp_path, code, "2026-10-30")  # 周五收盘后
    _write_calendar(tmp_path)
    rec: list = []

    def fake_fetch(wind_code, start, end, adjust):
        rec.append((wind_code, start, end, adjust))
        if adjust == "none":
            raise AssertionError("无交易日窗口不应做交叉核对请求")
        return _empty_ohlcv()

    monkeypatch.setattr(source, "fetch_daily", fake_fetch)
    # 本地止于周五 10-30，end=周六 11-01 -> 窗口 [11-01, 11-01] 无交易日
    c, status, msg = d._update_stock_one(code, "测试", "20261101", False)
    assert status == "uptodate", (status, msg)
    assert all(a != "none" for *_, a in rec)


def test_update_stock_one_crosscheck_fails_failsafe_stale(monkeypatch, tmp_path):
    """交叉核对自身失败 -> fail-safe 按 stale（宁可多报缺口，不可误标最新）。"""
    d = _mk_dl(tmp_path)
    code = "000005.SZ"
    _write_daily(tmp_path, code, "2026-10-29")
    _write_calendar(tmp_path)
    monkeypatch.setattr(source, "fetch_daily", _fake_fetch_factory(
        _empty_ohlcv(), ConnectionError("交叉核对网络中断")))
    c, status, msg = d._update_stock_one(code, "测试", "20261030", False)
    assert status == "stale", (status, msg)


def test_window_has_trading_day_beyond_calendar(tmp_path):
    """日历覆盖内查日历；超出日历最大值退化工作日判断；日历缺失返回 None。"""
    d = _mk_dl(tmp_path)
    pd.DataFrame({"trade_date": pd.bdate_range("2026-09-01", "2026-10-30")}).to_parquet(
        store.trade_cal_path(tmp_path), index=False)
    # 日历覆盖内: 含交易日 / 纯周末
    assert d._window_has_trading_day("20261023", "20261030") is True
    assert d._window_has_trading_day("20261024", "20261025") is False  # 六/日
    # 超出日历最大值: 退化工作日
    assert d._window_has_trading_day("20261102", "20261104") is True  # 一~三
    assert d._window_has_trading_day("20261107", "20261108") is False  # 六/日
    # 日历文件缺失 -> None（调用方保守处理）
    d2 = _mk_dl(tmp_path / "other")
    assert d2._window_has_trading_day("20261102", "20261104") is None


# ----------------------------------------------------------------- 单位断裂自愈


def _broken_heal_env(tmp_path, full_series_broken: bool, last_day: str = "2026-10-22"):
    """构造 600900.SH：本地 hfq 序列（东财口径）止于 last_day。

    full_series_broken=False: 全量重拉返回连续序列（可自愈）；
    True: 全量序列内部也断裂（源本身坏，自愈失败）。
    返回 (dl, code, fake_fetch)。
    """
    d = _mk_dl(tmp_path, start="20260901")
    code = "600900.SH"
    days = pd.bdate_range("2026-09-01", "2026-10-30")
    closes = [100.0 + 0.5 * i for i in range(len(days))]
    if full_series_broken:
        i = list(days).index(pd.Timestamp("2026-10-23"))
        closes[i] = 28.77  # 全量序列内部单位断裂
    full_df = pd.DataFrame({"date": days, "code": code, "close": closes})
    old_df = full_df[full_df["date"] <= last_day].copy()
    p = store.daily_dir(tmp_path, "hfq") / f"{code}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    old_df.to_parquet(p, index=False)

    def fake_fetch(wind_code, start, end, adjust):
        assert wind_code == code and adjust == "hfq"
        if start == "20261023":  # 增量窗口: 返回断裂 bar
            return pd.DataFrame({"date": [pd.Timestamp("2026-10-23")],
                                 "code": [code], "close": [28.77]})
        assert start == "20260901", f"意外的全量窗口起点 {start}"  # 全量重拉
        return full_df.copy()

    return d, code, p, fake_fetch


def test_update_stock_one_unit_anomaly_self_heal_repaired(monkeypatch, tmp_path):
    """增量守卫命中 -> 全量重拉连续 -> repaired，合并落盘，待修清单清空。"""
    d, code, p, fake = _broken_heal_env(tmp_path, full_series_broken=False)
    _write_calendar(tmp_path)
    monkeypatch.setattr(source, "fetch_daily", fake)
    c, status, msg = d._update_stock_one(code, "上汽集团", "20261030", False)
    assert c == code and status == "repaired", (status, msg)
    after = pd.read_parquet(p)
    assert pd.Timestamp(after["date"].max()) == pd.Timestamp("2026-10-30")
    # 断裂值被全量数据修正: 末根回到连续序列（>100），无 28.77 残留
    assert after["close"].iloc[-1] > 100
    assert not (after["close"] < 30).any()
    # 自愈成功 -> 待修清单为空/不存在
    assert not d._pending_repair_path().exists() or not d._pending_repair_path().read_text().strip()


def test_update_stock_one_unit_anomaly_full_still_broken_skipped(monkeypatch, tmp_path):
    """全量重拉仍断裂 -> skipped，旧文件保留，代码留在待修清单。"""
    d, code, p, fake = _broken_heal_env(tmp_path, full_series_broken=True)
    _write_calendar(tmp_path)
    before = pd.read_parquet(p)
    monkeypatch.setattr(source, "fetch_daily", fake)
    c, status, msg = d._update_stock_one(code, "上汽集团", "20261030", False)
    assert status == "skipped", (status, msg)
    assert "单位断裂" in msg, msg
    after = pd.read_parquet(p)
    pd.testing.assert_frame_equal(after, before)  # 旧数据未被覆盖
    # 待修清单含该代码（code\tdate\treason）
    lines = d._pending_repair_path().read_text(encoding="utf-8").strip().splitlines()
    assert lines and lines[0].split("\t")[0] == code, lines
    assert len(lines[0].split("\t")) == 3


# ============================================================ Wave 3: update_stocks
# stale 运行内二次重试


def test_update_stocks_stale_second_pass(monkeypatch, tmp_path):
    """主遍 stale 的股票冷却后再跑一遍：恢复的转 updated，仍缺的保持 stale。

    2026-10-30: EOD 15:50 起跑时 hfq 数据尚未生成，整批股票空窗口；18:41 才
    有源数据。冷却后二次重试让同一轮 EOD 内自愈，不必等到次日。
    """
    d = _mk_dl(tmp_path)
    _write_calendar(tmp_path)
    a, b = "600001.SH", "600002.SH"
    _write_daily(tmp_path, a, "2026-10-29")
    _write_daily(tmp_path, b, "2026-10-29")
    calls = {"a": 0, "b": 0}

    def fake_fetch(wind_code, start, end, adjust):
        if adjust == "none":  # 交叉核对: 两股 10-30 都有真实成交
            return _df(["2026-10-30"], [10.0], wind_code)
        key = "a" if wind_code == a else "b"
        calls[key] += 1
        if key == "a" or calls[key] == 1:
            return _empty_ohlcv()  # A 源缺口持续; B 首遍缺、二遍恢复
        return _df(["2026-10-30"], [10.0], wind_code)

    monkeypatch.setattr(source, "fetch_daily", fake_fetch)
    stats = d.update_stocks(codes=f"{a},{b}", end="20261030", lag_retry_delay=0)
    assert stats["updated"] == 1 and stats["stale"] == 1, stats
    assert calls["b"] == 2, "stale 股应在二次重试中再跑一次"
    # A 仍 stale: 文件未写入 10-30; B 已恢复: 写入 10-30
    fa = pd.read_parquet(store.daily_dir(tmp_path, "hfq") / f"{a}.parquet")
    fb = pd.read_parquet(store.daily_dir(tmp_path, "hfq") / f"{b}.parquet")
    assert pd.Timestamp(fa["date"].max()) == pd.Timestamp("2026-10-29")
    assert pd.Timestamp(fb["date"].max()) == pd.Timestamp("2026-10-30")
    # 失败清单只含仍 stale 的 A（B 已恢复，不入清单）
    failfile = tmp_path / "failed_stocks_hfq.txt"
    assert failfile.exists()
    codes_in = {l.split("\t")[0] for l in failfile.read_text().splitlines() if l.strip()}
    assert codes_in == {a}, codes_in


def test_update_stocks_stale_counted_once(monkeypatch, tmp_path):
    """二次重试后 stats 按最终状态单次计数（不得首遍 stale + 二遍 updated 双计）。"""
    d = _mk_dl(tmp_path)
    _write_calendar(tmp_path)
    code = "600003.SH"
    _write_daily(tmp_path, code, "2026-10-29")
    n = {"hfq": 0}

    def fake_fetch(wind_code, start, end, adjust):
        if adjust == "none":
            return _df(["2026-10-30"], [10.0], wind_code)
        n["hfq"] += 1
        if n["hfq"] == 1:
            return _empty_ohlcv()
        return _df(["2026-10-30"], [10.0], wind_code)

    monkeypatch.setattr(source, "fetch_daily", fake_fetch)
    stats = d.update_stocks(codes=code, end="20261030", lag_retry_delay=0)
    assert sum(stats.values()) == 1, stats  # 总共 1 只股票，恰好计 1 次
    assert stats["updated"] == 1 and stats.get("stale", 0) == 0, stats


# ============================================================ Wave 4: 列表守卫 + 指数新鲜度

def _sl_df(codes) -> pd.DataFrame:
    return pd.DataFrame({"code": list(codes), "name": ["测试"] * len(codes)})


def test_stock_list_guard_first_write_floor():
    """无旧列表（首次写入）：总量 <3000 拒绝（A股总数远超此值，截断列表不配落盘）。"""
    from downloader.app import _stock_list_guard
    assert _stock_list_guard(_sl_df([f"600{i:03d}.SH" for i in range(2999)]), None) != ""
    assert _stock_list_guard(_sl_df([f"600{i:03d}.SH" for i in range(3000)]), None) == ""


def test_stock_list_guard_family_disappears_rejected():
    """旧列表存在的 3 位前缀家族在新列表中整体消失 -> 拒绝（2026-10-26: 688/689/BJ 整段缺失）。"""
    from downloader.app import _stock_list_guard
    old = _sl_df([f"600{i:03d}.SH" for i in range(1000)]
                 + [f"688{i:03d}.SH" for i in range(500)]
                 + [f"000{i:03d}.SZ" for i in range(1000)])
    new = _sl_df([f"600{i:03d}.SH" for i in range(1000)] + [f"000{i:03d}.SZ" for i in range(1000)])
    r = _stock_list_guard(new, old)
    assert r and "688" in r, r


def test_stock_list_guard_family_shrink_over_50pct_rejected():
    """单家族缩水 >50% -> 拒绝。"""
    from downloader.app import _stock_list_guard
    old = _sl_df([f"600{i:03d}.SH" for i in range(1000)] + [f"300{i:03d}.SZ" for i in range(1000)])
    new = _sl_df([f"600{i:03d}.SH" for i in range(1000)] + [f"300{i:03d}.SZ" for i in range(499)])
    r = _stock_list_guard(new, old)
    assert r and "300" in r, r


def test_stock_list_guard_total_shrink_over_30pct_rejected():
    """家族均只缩水 <50% 但总量缩水 >30% -> 拒绝。"""
    from downloader.app import _stock_list_guard
    old = _sl_df([f"{p}{i:03d}.SH" for p in ("600", "601", "603") for i in range(1000)])
    new = _sl_df([f"{p}{i:03d}.SH" for p in ("600", "601", "603") for i in range(650)])
    r = _stock_list_guard(new, old)
    assert r and "30" in r, r


def test_stock_list_guard_growth_and_new_families_ok():
    """正常 + 新增家族/新股 -> 放行（不得把正常扩容当事故）。"""
    from downloader.app import _stock_list_guard
    old = _sl_df([f"600{i:03d}.SH" for i in range(1000)] + [f"000{i:03d}.SZ" for i in range(1000)])
    new = _sl_df(list(old["code"]) + [f"688{i:03d}.SH" for i in range(580)]
                 + [f"430{i:03d}.BJ" for i in range(250)])
    assert _stock_list_guard(new, old) == ""


def test_refresh_stock_list_rejects_bad_keeps_old(monkeypatch, tmp_path):
    """守卫拒绝 -> raise FetchError，旧列表文件原样保留（不覆盖、不截断）。"""
    d = _mk_dl(tmp_path)
    old_codes = ([f"600{i:03d}.SH" for i in range(1500)]
                 + [f"688{i:03d}.SH" for i in range(500)]
                 + [f"000{i:03d}.SZ" for i in range(1000)])
    store.save_parquet(store.stock_list_path(tmp_path), _sl_df(old_codes))
    bad_codes = [c for c in old_codes if not c.startswith("688")]  # 688 家族整体消失
    monkeypatch.setattr(source, "fetch_stock_list", lambda: _sl_df(bad_codes))
    with pytest.raises(FetchError):
        d.refresh_stock_list()
    ondisk = pd.read_parquet(store.stock_list_path(tmp_path))
    assert len(ondisk) == len(old_codes)
    assert (ondisk["code"].str.startswith("688")).sum() == 500  # 旧列表未丢


def test_refresh_stock_list_good_list_written(monkeypatch, tmp_path):
    """守卫放行 -> 正常写盘 + dataset/stock_basic 副本同步。"""
    d = _mk_dl(tmp_path)
    old_codes = [f"600{i:03d}.SH" for i in range(1500)] + [f"000{i:03d}.SZ" for i in range(1000)]
    store.save_parquet(store.stock_list_path(tmp_path), _sl_df(old_codes))
    new_codes = old_codes + [f"688{i:03d}.SH" for i in range(500)]  # 补回 688 家族（从坏列表恢复）
    monkeypatch.setattr(source, "fetch_stock_list", lambda: _sl_df(new_codes))
    df = d.refresh_stock_list()
    assert len(df) == len(new_codes)
    ondisk = pd.read_parquet(store.stock_list_path(tmp_path))
    assert len(ondisk) == len(new_codes)
    assert (ondisk["code"].str.startswith("688")).sum() == 500
    basic = store.dataset_dir(tmp_path) / "stock_basic.parquet"
    assert basic.exists()
    assert len(pd.read_parquet(basic)) == len(new_codes)


# ----------------------------------------------------------------- 指数新鲜度

def test_update_indices_empty_window_trading_day_stale(monkeypatch, tmp_path):
    """窗口是交易日但源返回空 -> stale（源缺数据），不得标 uptodate、不得写盘。

    2026-10-30 根因S3: 000688.SH 指数停在 10-23，空窗被当「已是最新」。
    """
    d = _mk_dl(tmp_path)
    _write_calendar(tmp_path)
    code = "000688.SH"
    p = _write_index(tmp_path, code, "2026-10-29")
    monkeypatch.setattr(source, "fetch_index_daily",
                        lambda wind_code, start, end: _empty_ohlcv())
    stats = d.update_indices(indices=code, end="20261030")
    assert stats["stale"] == 1, stats
    after = pd.read_parquet(p)
    assert pd.Timestamp(after["date"].max()) == pd.Timestamp("2026-10-29")
    # stale 入失败清单
    failfile = tmp_path / "failed_index_hfq.txt"
    assert failfile.exists()
    assert code in failfile.read_text()


def test_update_indices_empty_window_no_trading_day_uptodate(monkeypatch, tmp_path):
    """窗口无交易日（周六）-> uptodate，且不发起网络请求。"""
    d = _mk_dl(tmp_path)
    _write_calendar(tmp_path)
    code = "000001.SH"
    p = _write_index(tmp_path, code, "2026-10-30")  # 周五
    monkeypatch.setattr(
        source, "fetch_index_daily",
        lambda wind_code, start, end: (_ for _ in ()).throw(AssertionError("无交易日窗口不应请求")))
    stats = d.update_indices(indices=code, end="20261101")  # 周六
    assert stats["uptodate"] == 1, stats
    after = pd.read_parquet(p)
    assert pd.Timestamp(after["date"].max()) == pd.Timestamp("2026-10-30")


def test_update_indices_no_progress_stale(monkeypatch, tmp_path):
    """窗口 [10-30,10-30] 却返回 10-29 的旧 bar -> 无进展 -> stale，不得当 updated 写入。"""
    d = _mk_dl(tmp_path)
    _write_calendar(tmp_path)
    code = "000300.SH"
    p = _write_index(tmp_path, code, "2026-10-29")
    old_bar = pd.DataFrame({"date": [pd.Timestamp("2026-10-29")],
                            "code": [code], "close": [3000.0]})
    monkeypatch.setattr(source, "fetch_index_daily",
                        lambda wind_code, start, end: old_bar)
    stats = d.update_indices(indices=code, end="20261030")
    assert stats["stale"] == 1, stats
    assert stats.get("updated", 0) == 0, stats
    after = pd.read_parquet(p)
    assert pd.Timestamp(after["date"].max()) == pd.Timestamp("2026-10-29")
    assert len(after) == 30


# ============================================================ Wave 5: repair_stocks + CLI

def _broken_local_and_full(tmp_path, code: str, break_at: str | None = "2026-10-23"):
    """本地 = 连续序列截断至 10-22；返回完整序列（可选在 break_at 处注入断裂）。"""
    days = pd.bdate_range("2026-09-01", "2026-10-30")
    closes = [100.0 + 0.5 * i for i in range(len(days))]
    if break_at is not None:
        i = list(days).index(pd.Timestamp(break_at))
        closes[i] = 28.77
    full = pd.DataFrame({"date": days, "code": code, "close": closes})
    p = store.daily_dir(tmp_path, "hfq") / f"{code}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    full[full["date"] <= pd.Timestamp("2026-10-22")].to_parquet(p, index=False)
    return full


def test_repair_stocks_pending_list_repaired(monkeypatch, tmp_path):
    """默认处理待修清单：全量重拉通过守卫 -> repaired，清单清空，数据补齐到 end。"""
    d = _mk_dl(tmp_path)
    c1, c2 = "600900.SH", "600901.SH"
    full = {c: _broken_local_and_full(tmp_path, c, break_at=None) for c in (c1, c2)}
    d._pending_repair_save({c1: "单位断裂", c2: "单位断裂"})
    monkeypatch.setattr(source, "fetch_daily",
                        lambda wind_code, start, end, adjust: full[wind_code].copy())
    stats = d.repair_stocks(end="20261030")
    assert stats["repaired"] == 2, stats
    assert not d._pending_repair_path().exists()
    for c in (c1, c2):
        after = pd.read_parquet(store.daily_dir(tmp_path, "hfq") / f"{c}.parquet")
        assert pd.Timestamp(after["date"].max()) == pd.Timestamp("2026-10-30")


def test_repair_stocks_failed_stays_in_list(monkeypatch, tmp_path):
    """全量重拉仍断裂 -> failed，代码留在待修清单，失败清单落盘。"""
    d = _mk_dl(tmp_path)
    c = "600902.SH"
    full = _broken_local_and_full(tmp_path, c)  # 10-23 处断裂
    d._pending_repair_save({c: "单位断裂"})
    monkeypatch.setattr(source, "fetch_daily",
                        lambda wind_code, start, end, adjust: full.copy())
    stats = d.repair_stocks(end="20261030")
    assert stats["failed"] == 1, stats
    assert c in d._pending_repair_load()  # 留清单等待下次重试
    assert (tmp_path / "failed_repair_hfq.txt").exists()


def test_repair_stocks_codes_force_new_stock(monkeypatch, tmp_path):
    """--codes + --force：无清单记录、无本地文件也允许（新股整段重拉）。"""
    d = _mk_dl(tmp_path)
    c = "688001.SH"
    full = _broken_local_and_full(tmp_path, c, break_at=None)
    (store.daily_dir(tmp_path, "hfq") / f"{c}.parquet").unlink()  # 无本地文件
    monkeypatch.setattr(source, "fetch_daily",
                        lambda wind_code, start, end, adjust: full.copy())
    stats = d.repair_stocks(codes=c, force=True, end="20261030")
    assert stats["repaired"] == 1, stats
    after = pd.read_parquet(store.daily_dir(tmp_path, "hfq") / f"{c}.parquet")
    assert pd.Timestamp(after["date"].max()) == pd.Timestamp("2026-10-30")
    assert not d._pending_repair_path().exists()  # 成功后移出清单


def test_repair_stocks_codes_without_force_rejected(monkeypatch, tmp_path):
    """无清单记录 + 无本地文件 + 未 --force -> skipped（防误敲代码），不发请求。"""
    d = _mk_dl(tmp_path)
    n = {"n": 0}

    def fake(w, s, e, a):
        n["n"] += 1
        raise AssertionError("不应发请求")

    monkeypatch.setattr(source, "fetch_daily", fake)
    stats = d.repair_stocks(codes="688999.SH", end="20261030")
    assert stats["skipped"] == 1 and n["n"] == 0, stats


def test_repair_stocks_codes_merged_with_pending(monkeypatch, tmp_path):
    """--codes 加入清单并与既有待修项合并处理；均有本地文件时无需 --force。"""
    d = _mk_dl(tmp_path)
    c1, c2 = "600903.SH", "600904.SH"
    full = {c: _broken_local_and_full(tmp_path, c, break_at=None) for c in (c1, c2)}
    d._pending_repair_save({c1: "单位断裂"})
    monkeypatch.setattr(source, "fetch_daily",
                        lambda wind_code, start, end, adjust: full[wind_code].copy())
    stats = d.repair_stocks(codes=c2, end="20261030")
    assert stats["repaired"] == 2, stats  # c1(清单) + c2(指定) 都修了
    assert not d._pending_repair_path().exists()


def test_cli_repair_parse():
    from downloader.cli import build_parser
    args = build_parser().parse_args(
        ["repair", "--codes", "600900.SH,600901.SH", "--force", "--end", "20261030"])
    assert args.cmd == "repair"
    assert args.codes == "600900.SH,600901.SH"
    assert args.force is True
    assert args.end == "20261030"
    args2 = build_parser().parse_args(["repair"])
    assert args2.codes is None and args2.force is False


def test_cli_repair_dispatch(monkeypatch, tmp_path):
    """CLI 端到端: repair 子命令处理待修清单并清空（走真实 cli.main 分派）。"""
    from downloader import cli
    c = "600905.SH"
    full = _broken_local_and_full(tmp_path, c, break_at=None)
    d = _mk_dl(tmp_path)
    d._pending_repair_save({c: "单位断裂"})
    monkeypatch.setattr(source, "fetch_daily",
                        lambda wind_code, start, end, adjust: full.copy())
    cli.main(["repair", "--data-dir", str(tmp_path), "--rate", "0", "--end", "20261030"])
    assert not (tmp_path / "pending_repair_hfq.txt").exists()
    after = pd.read_parquet(store.daily_dir(tmp_path, "hfq") / f"{c}.parquet")
    assert pd.Timestamp(after["date"].max()) == pd.Timestamp("2026-10-30")


# ============================================================ Wave 6: flow 重试/strict + EOD 可见性

def _write_flow_cache(root: Path, code: str, day: str = "2026-10-29") -> None:
    from analyzer import flow as flow_mod
    payload = {"code": code, "items": [{"day": day, "close": 100.0, "pct_chg": 0.5,
                                        "main_net": 1.0e6, "main_pct": 1.0,
                                        "super_net": None, "super_pct": None,
                                        "big_net": None, "big_pct": None,
                                        "mid_net": None, "mid_pct": None,
                                        "small_net": None, "small_pct": None}],
               "fetched_at": "2026-10-29 15:30:00",
               "fetched_ts": 1761723000.0}
    flow_mod._save_cache(flow_mod._cache_path(root, code), payload)


def test_flow_fetch_backoff_retries(monkeypatch, tmp_path):
    """_fetch retries=4，退避 2/4/8/16s（旧: retries=2、1.5/3.0s）。"""
    from analyzer import flow as flow_mod
    import akshare as ak
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    n = {"n": 0}

    def boom(stock, market):
        n["n"] += 1
        raise ConnectionError("push2his 瞬断")

    monkeypatch.setattr(ak, "stock_individual_fund_flow", boom)
    with pytest.raises(RuntimeError, match="资金流抓取失败"):
        flow_mod.fetch("600519.SH", tmp_path, refresh=True)
    assert n["n"] == 5, "1 次初始 + 4 次重试"
    assert sleeps == [2.0, 4.0, 8.0, 16.0], sleeps


def test_flow_fetch_strict_raises_instead_of_silent_fallback(monkeypatch, tmp_path):
    """strict=True（EOD 路径）: 抓取失败必须 raise，不得静默回退旧缓存。

    2026-10-30 根因S4: 旧实现失败回退 stale 缓存并正常返回，EOD 无感知，
    资金流数据可无限期陈旧且无任何错误记录。
    """
    from analyzer import flow as flow_mod
    import akshare as ak
    _write_flow_cache(tmp_path, "600519.SH")
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(ak, "stock_individual_fund_flow",
                        lambda stock, market: (_ for _ in ()).throw(ConnectionError("断")))
    with pytest.raises(RuntimeError):
        flow_mod.fetch("600519.SH", tmp_path, refresh=True, strict=True)


def test_flow_fetch_nonstrict_falls_back_to_cache(monkeypatch, tmp_path):
    """strict=False（默认/交互路径）: 失败回退旧缓存 stale=True（行为不变）。"""
    from analyzer import flow as flow_mod
    import akshare as ak
    _write_flow_cache(tmp_path, "600519.SH")
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(ak, "stock_individual_fund_flow",
                        lambda stock, market: (_ for _ in ()).throw(ConnectionError("断")))
    out = flow_mod.fetch("600519.SH", tmp_path, refresh=True)
    assert out["stale"] is True and out["items"]


def test_updates_refresh_flow_uses_strict(monkeypatch, tmp_path):
    """EOD _refresh_flow 必须走 strict=True。"""
    from analyzer import flow as flow_mod
    from test_eod_updates import _mk_updater
    up, _dl, _nc, _store = _mk_updater(tmp_path, ["600519.SH"])
    calls: dict = {}

    def fake_fetch(code, root, refresh=False, keep_days=flow_mod.KEEP_DAYS, strict=False):
        calls["strict"] = strict
        return {"code": code, "items": [], "stale": False}

    monkeypatch.setattr(flow_mod, "fetch", fake_fetch)
    # _mk_updater 已把实例上的 _refresh_flow no-op 掉，直接调类方法验证真实实现
    from analyzer.updates import EodUpdater
    EodUpdater._refresh_flow(up, "600519.SH")
    assert calls["strict"] is True


def test_eod_watchlist_info_failure_recorded(tmp_path):
    """资讯刷新失败（含 strict 资金流）必须记入 status.errors，主链路照常完成。

    旧实现 watchlist_info 循环裸 except pass，失败完全无痕。
    """
    from test_eod_updates import _mk_updater
    up, dl, _nc, _store = _mk_updater(tmp_path, ["600519.SH"])

    def boom(c):
        raise RuntimeError("资金流抓取失败 (strict): push2his 瞬断")

    boom.__name__ = "_refresh_flow"  # 记错信息按 fn.__name__ 格式，替身需伪装方法名
    up._refresh_flow = boom
    st = up.run_now()
    assert any("自选资讯刷新失败 (600519.SH _refresh_flow)" in e for e in st["errors"]), \
        st["errors"]
    # 主链路（全市场/指数/数据集）不受影响
    assert "update_stocks" in dl.calls and "build_dataset" in dl.calls
    assert st["running"] is False
