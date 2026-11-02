"""行情快照 vintage 修复单测（2026-11-02 生产事故：陈旧快照被当最新）。

背景：东财 clist 全天被封时 EOD 链路 list 刷新失败，但 build_dataset 仍用旧
stock_list 重写 dataset/stock_basic 副本（mtime=今天、内容=10-30 的价）。
DataStore 按 mtime 推断快照日 → 11-02，把 10-30 快照价 23.81 锚到 11-02 的
K 线末根上，全站显示旧价。

修复三件套：
1. sidecar data/quote_asof.json（真值源：行情成功日；失败不 bump）；
2. DataStore._snapshot_date 取 min(quote_date, mtime 推断日)；
3. 东财列表失败时用腾讯 qt.gtimg 批量行情兜底刷新 stock_list 行情字段。

全部离线（fixture 用事故真实数字，不触网）。
运行: .venv/bin/python -m pytest tests/test_quote_vintage.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, ".")

from analyzer.datastore import DataStore  # noqa: E402
from downloader import source, store  # noqa: E402
from downloader.app import Downloader  # noqa: E402
from downloader.source import FetchError  # noqa: E402

# --------------------------------------------------------------- 事故数字锚点
CODE = "300687.SZ"
SNAP_PRICE_1030 = 23.81      # 10-30 收盘（事故当天全站显示的"最新价"）
HFAQ_CLOSE_1030 = 106.874    # hfq 10-30 收盘
HFAQ_CLOSE_1102 = 115.80     # hfq 11-02 收盘
REAL_1102 = 26.12            # 11-02 真实收盘


def _mk_daily(root: Path, code: str, dates: list[str], closes: list[float]) -> None:
    n = len(dates)
    pd.DataFrame({
        "date": pd.to_datetime(dates), "code": [code] * n,
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1000.0] * n, "amount": [1e8] * n,
        "amplitude": [0.0] * n, "pct_chg": [1.0] * n,
        "change": [0.1] * n, "turnover": [1.0] * n,
    }).to_parquet(root / "daily" / "hfq" / f"{code}.parquet", index=False)


def _mk_dir() -> Path:
    from tempfile import mkdtemp
    d = Path(mkdtemp())
    (d / "daily" / "hfq").mkdir(parents=True)
    return d


def _write_basic(root: Path, latest_price: float, name: str = "stock_list.parquet",
                 mtime: str | None = None) -> None:
    df = pd.DataFrame({
        "code": [CODE], "name": ["中亦科技"], "latest_price": [latest_price],
        "pct_chg": [-5.93], "turnover": [2.0],
        "total_mv": [9.724e9], "float_mv": [5.0e9],
    })
    df.to_parquet(root / name, index=False)
    if mtime:
        import os
        ts = pd.Timestamp(mtime).timestamp()
        os.utime(root / name, (ts, ts))


def _write_sidecar(root: Path, quote_date: str) -> None:
    store.write_quote_date(root, quote_date)


def _mk_incident_fixture(sidecar: str | None = "2026-10-30") -> Path:
    """事故复现 fixture：
    - stock_list: 10-30 的价 23.81，mtime=11-02 22:10（EOD 旧数据重写副本带新 mtime）；
    - dataset/stock_basic: 同内容同 mtime（事故中 DataStore 选中的就是它）；
    - 日线：10-30 / 11-02 两根（11-02 真实收盘 26.12）；
    - sidecar quote_asof.json: quote_date=10-30（行情真实所属日）。
    """
    d = _mk_dir()
    pd.DataFrame({"trade_date": pd.to_datetime(["2026-10-30", "2026-11-02"])}).to_parquet(
        d / "trade_calendar.parquet", index=False)
    _mk_daily(d, CODE, ["2026-10-30", "2026-11-02"], [HFAQ_CLOSE_1030, HFAQ_CLOSE_1102])
    import os
    (d / "dataset").mkdir(exist_ok=True)
    ts = pd.Timestamp("2026-11-02 22:10:19").timestamp()
    _write_basic(d, SNAP_PRICE_1030, mtime="2026-11-02 22:10:19")
    _write_basic(d, SNAP_PRICE_1030, name="dataset/stock_basic.parquet",
                 mtime="2026-11-02 22:10:19")
    os.utime(d / "dataset" / "stock_basic.parquet", (ts, ts))
    if sidecar is not None:
        _write_sidecar(d, sidecar)
    return d


def _mk_dl(root: Path) -> Downloader:
    d = Downloader.__new__(Downloader)
    d.data_dir = root
    d.adjust = "hfq"
    d.start = "20260901"
    d.workers = 2
    d.rate = 0
    d.rotator = None
    return d


# ============================================================ sidecar 存取

def test_sidecar_read_write_roundtrip(tmp_path):
    assert store.read_quote_date(tmp_path) is None  # 无文件 -> None
    store.write_quote_date(tmp_path, "2026-10-30")
    assert store.read_quote_date(tmp_path) == "2026-10-30"
    # 损坏/非法内容 -> None（不阻塞读取端）
    (tmp_path / "quote_asof.json").write_text("not json{", encoding="utf-8")
    assert store.read_quote_date(tmp_path) is None


# ============================================================ 场景 a: 陈旧快照 clamp

def test_stale_snapshot_clamped_by_sidecar():
    """陈旧快照 + 新副本 mtime + sidecar=10-30：
    snapshot_date='2026-10-30'，price_info 按收盘比外推 real ≈ 25.7977。"""
    d = _mk_incident_fixture(sidecar="2026-10-30")
    s = DataStore(str(d))
    assert str(s.snapshot_date.date()) == "2026-10-30", \
        f"快照日应被 sidecar clamp 到 10-30，实际 {s.snapshot_date}"
    pi = s.price_info(CODE)
    expect = SNAP_PRICE_1030 * HFAQ_CLOSE_1102 / HFAQ_CLOSE_1030
    assert abs(pi["real"] - expect) < 0.01, f"real 应外推到 {expect:.4f}，实际 {pi['real']}"
    assert pi["snap_date"] == "2026-10-30" and pi["last_date"] == "2026-11-02"


# ============================================================ 场景 c: 无 sidecar 兼容

def test_no_sidecar_keeps_mtime_behavior():
    """无 sidecar：行为与现行完全一致（mtime 推断，旧 bug 行为保留兼容）。"""
    d = _mk_incident_fixture(sidecar=None)
    s = DataStore(str(d))
    assert str(s.snapshot_date.date()) == "2026-11-02"
    pi = s.price_info(CODE)
    assert pi["real"] == SNAP_PRICE_1030, "无 sidecar 时 mtime 推断快照日=11-02，锚末根 real=快照价"


# ============================================================ 场景 b: 兜底刷新后

def _fake_quotes(price_map: dict[str, dict]) -> dict:
    """替身 source.fetch_gtimg_quotes：返回预设 {code: 行情字段}。"""
    return lambda wind_codes: {c: price_map[c] for c in wind_codes if c in price_map}


def test_fallback_refresh_quotes_recovers(tmp_path, monkeypatch):
    """兜底源刷新后（latest_price=26.12、sidecar=11-02、日线末根 11-02）：real=26.12。"""
    d = _mk_incident_fixture(sidecar="2026-10-30")
    dl = _mk_dl(d)
    monkeypatch.setattr(source, "fetch_gtimg_quotes",
                        _fake_quotes({CODE: {"latest_price": REAL_1102, "pct_chg": 9.70,
                                             "turnover": 3.5, "total_mv": 9.724e9,
                                             "float_mv": 5.0e9}}))
    dl.refresh_quote_fields(quote_date="2026-11-02")
    # 双写 + sidecar
    for p in (store.stock_list_path(d), store.dataset_dir(d) / "stock_basic.parquet"):
        assert pd.read_parquet(p).iloc[0]["latest_price"] == REAL_1102
    assert store.read_quote_date(d) == "2026-11-02"
    s = DataStore(str(d))
    pi = s.price_info(CODE)
    assert pi["real"] == REAL_1102 and pi["snap_date"] == "2026-11-02"


# ============================================================ 场景 d: 兜底也失败

def test_fallback_failure_does_not_bump_sidecar(tmp_path, monkeypatch):
    """兜底源也失败：sidecar 不 bump，quote_date 保留旧值，行情字段原样。"""
    d = _mk_incident_fixture(sidecar="2026-10-30")

    def boom(wind_codes):
        raise FetchError("腾讯行情也挂了")

    dl = _mk_dl(d)
    monkeypatch.setattr(source, "fetch_gtimg_quotes", boom)
    with pytest.raises(Exception):
        dl.refresh_quote_fields(quote_date="2026-11-02")
    assert store.read_quote_date(d) == "2026-10-30"
    assert pd.read_parquet(store.stock_list_path(d)).iloc[0]["latest_price"] == SNAP_PRICE_1030


# ============================================================ 场景 e: 停牌代码

def test_suspended_code_keeps_old_values(tmp_path, monkeypatch):
    """停牌/无返回/现价 0 的代码：保留旧值不覆盖。"""
    d = _mk_incident_fixture(sidecar="2026-10-30")
    # 再放一只股票在列表里（停牌：现价 0；另一只无返回）
    df = pd.read_parquet(store.stock_list_path(d))
    suspended = "300199.SZ"
    df2 = pd.concat([df, pd.DataFrame({
        "code": [suspended], "name": ["停牌股"], "latest_price": [21.86],
        "pct_chg": [-1.0], "turnover": [0.5], "total_mv": [8e9], "float_mv": [4e9],
    })], ignore_index=True)
    store.save_parquet(store.stock_list_path(d), df2)

    dl = _mk_dl(d)
    monkeypatch.setattr(source, "fetch_gtimg_quotes", _fake_quotes({
        CODE: {"latest_price": REAL_1102, "pct_chg": 9.70, "turnover": 3.5,
               "total_mv": 9.724e9, "float_mv": 5.0e9},
        suspended: {"latest_price": 0.0, "pct_chg": 0.0, "turnover": 0.0,
                    "total_mv": 8e9, "float_mv": 4e9},  # 现价 0 -> 停牌
    }))
    dl.refresh_quote_fields(quote_date="2026-11-02")
    out = pd.read_parquet(store.stock_list_path(d)).set_index("code")
    assert out.loc[suspended, "latest_price"] == 21.86, "停牌代码现价 0 时必须保留旧值"
    assert out.loc[suspended, "pct_chg"] == -1.0
    assert out.loc[CODE, "latest_price"] == REAL_1102


# ============================================================ 写入点

def test_refresh_stock_list_success_writes_sidecar(tmp_path, monkeypatch):
    """refresh_stock_list 成功分支写当天 sidecar。"""
    d = _mk_dl(tmp_path)
    old_codes = [f"600{i:03d}.SH" for i in range(1500)] + [f"000{i:03d}.SZ" for i in range(1000)]
    store.save_parquet(store.stock_list_path(tmp_path),
                       pd.DataFrame({"code": old_codes, "name": ["测试"] * len(old_codes)}))
    new_codes = old_codes + [f"688{i:03d}.SH" for i in range(500)]
    monkeypatch.setattr(source, "fetch_stock_list",
                        lambda: pd.DataFrame({"code": new_codes, "name": ["测试"] * len(new_codes)}))
    d.refresh_stock_list()
    import datetime as dt
    assert store.read_quote_date(tmp_path) == dt.date.today().isoformat()


def test_refresh_stock_list_guard_failure_no_bump(tmp_path, monkeypatch):
    """守卫拒绝（列表源失败路径）：sidecar 不写不 bump。"""
    d = _mk_dl(tmp_path)
    store.write_quote_date(tmp_path, "2026-10-30")
    old_codes = ([f"600{i:03d}.SH" for i in range(1500)]
                 + [f"688{i:03d}.SH" for i in range(500)]
                 + [f"000{i:03d}.SZ" for i in range(1000)])
    store.save_parquet(store.stock_list_path(tmp_path),
                       pd.DataFrame({"code": old_codes, "name": ["测试"] * len(old_codes)}))
    bad = [c for c in old_codes if not c.startswith("688")]
    monkeypatch.setattr(source, "fetch_stock_list",
                        lambda: pd.DataFrame({"code": bad, "name": ["测试"] * len(bad)}))
    with pytest.raises(FetchError):
        d.refresh_stock_list()
    assert store.read_quote_date(tmp_path) == "2026-10-30"


def test_build_dataset_copy_does_not_bump_sidecar(tmp_path):
    """build_dataset 拷贝 stock_basic 副本沿用旧 quote_date，不 bump。"""
    d = _mk_incident_fixture(sidecar="2026-10-30")
    dl = _mk_dl(d)
    dl.build_dataset()
    assert store.read_quote_date(d) == "2026-10-30", "副本拷贝不得 bump sidecar"


# ============================================================ 腾讯行情解析

class _FakeRawClient:
    """替身 source._client.get：按顺序返回预设 GBK 字节流。"""

    def __init__(self, bodies: list[str]):
        self.bodies = list(bodies)
        self.calls: list[str] = []

    def get(self, url, params=None, **kw):
        self.calls.append(params.get("q", "") if params else "")
        return self.bodies.pop(0).encode("gbk")


def _gtimg_body(sym: str, price: str, pre_close: str, turnover: str = "1.23",
                float_mv_yi: str = "50.00", total_mv_yi: str = "97.24") -> str:
    f = ["51", "中亦科技", sym, price, pre_close, "24.00", "10000", "10000", "10000"]
    f += [""] * 24          # 9..32
    f += ["", "", "", "", "123456"]  # 33..37
    f += [turnover]         # 38
    f += [""] * 5           # 39..43
    f += [float_mv_yi]      # 44
    f += [total_mv_yi]      # 45
    return f'v_{sym}="' + "~".join(f) + '";'


def test_fetch_gtimg_quotes_parse_and_batch(monkeypatch):
    """60 只/请求、GBK、字段映射（3现价/4昨收/38换手/44流通市值亿/45总市值亿）。"""
    sym_a = "sz300687"
    sym_b = "sh600519"
    body1 = _gtimg_body(sym_a, "26.12", "23.81")
    body2 = _gtimg_body(sym_b, "1500.00", "1480.00", turnover="0.5",
                        float_mv_yi="1800.0", total_mv_yi="1880.0")
    fake = _FakeRawClient([body1, body2])
    monkeypatch.setattr(source, "_client", fake)
    # 61 只 -> 2 批；其余代码无返回（腾讯响应缺失时保留旧值由调用方处理）
    codes = ["300687.SZ"] + [f"000{i:03d}.SZ" for i in range(59)] + ["600519.SH"]
    out = source.fetch_gtimg_quotes(codes)
    assert len(fake.calls) == 2, f"61 只应分 2 批（60/请求），实际 {len(fake.calls)} 批"
    a = out["300687.SZ"]
    assert a["latest_price"] == 26.12 and a["pct_chg"] == pytest.approx(
        (26.12 / 23.81 - 1) * 100, abs=1e-6)
    assert a["turnover"] == 1.23
    assert a["total_mv"] == pytest.approx(97.24e8)   # 亿 -> 元
    assert a["float_mv"] == pytest.approx(50.00e8)
    b = out["600519.SH"]
    assert b["latest_price"] == 1500.00 and b["total_mv"] == pytest.approx(1880.0e8)


def test_fetch_gtimg_quotes_all_empty_raises(monkeypatch):
    """全部批次无有效行情 -> raise（兜底源也失败，sidecar 不 bump 的判定依据）。"""
    fake = _FakeRawClient(['v_pv_none="";', ''])
    monkeypatch.setattr(source, "_client", fake)
    with pytest.raises(FetchError):
        source.fetch_gtimg_quotes(["300687.SZ"] + [f"000{i:03d}.SZ" for i in range(60)])
