"""日线 amount 缺失防御单测（2026-11-02 生产事故：sina 兜底源无成交额字段）。

背景：东财/腾讯/搜狐/同花顺日线源全挂时 EOD 增量降级 sina（ok=5214），
sina 日线 API 只有 OHLCV 无 amount，11-02 全市场 amount/turnover=NaN
（5214/5217 只），市场页「全市场成交额」求和只剩 3 只=8 亿（真实约
1.4 万亿）。volume/close/pct_chg 均正常，行数判据的 partial 抓不住。

修复四件套：
1. fetch_gtimg_quotes 增返 amount(元)/turnover（f37 成交额万元 ×1e4、f38）；
2. Downloader.refresh_daily_amounts：当日 amount 覆盖率检测 + 腾讯行情回补；
3. EOD 链（analyzer/updates.py）stocks 阶段后调用 + 一行日志；
4. DataStore._build_market_daily 成交额合理性钳制（< 近 20 日中位数 30% 置
   NaN，前端显示 — 而非 8 亿的假数字）。

全部离线（fixture 本地数据 + monkeypatch，不触网）。
运行: .venv/bin/python -m pytest tests/test_amount_vintage.py -q
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

# --------------------------------------------------------------- 事故数字锚点
CODE_A = "300687.SZ"   # 当日 amount 正常
CODE_B = "600519.SH"   # 当日 amount NaN -> 可回补
CODE_C = "000001.SZ"   # 当日 amount NaN -> 停牌（无行情）不回补
DAY = "2026-11-02"     # 事故日
PREV = "2026-10-30"    # 前一交易日
BAD_AMOUNT_YI = 8.0          # 事故日全市场成交额（亿）——真实约 1.4 万亿
NORMAL_AMOUNT_YI = 13000.0   # 正常日全市场成交额（亿）


def _mk_stock(root: Path, code: str, rows: list[tuple[str, float, float | None]]) -> None:
    """写单股日线文件；rows = [(date, close, amount_or_None)]。"""
    n = len(rows)
    amounts = [a if a is not None else float("nan") for _, _, a in rows]
    closes = [c for _, c, _ in rows]
    pd.DataFrame({
        "date": pd.to_datetime([d for d, _, _ in rows]), "code": [code] * n,
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1000.0 + i for i in range(n)], "amount": amounts,
        "amplitude": [0.0] * n, "pct_chg": [1.0] * n,
        "change": [0.1] * n, "turnover": [1.0 if a else float("nan") for a in amounts],
    }).to_parquet(root / "daily" / "hfq" / f"{code}.parquet", index=False)


def _mk_fixture(prev: dict[str, float | None], day: dict[str, float | None]) -> Path:
    """3 只股票：单股文件 + dataset/daily_hfq 合并表（PREV/DAY 两天）。"""
    from tempfile import mkdtemp
    d = Path(mkdtemp())
    (d / "daily" / "hfq").mkdir(parents=True)
    (d / "dataset").mkdir(parents=True)
    parts = []
    for code in (CODE_A, CODE_B, CODE_C):
        rows = [(PREV, 10.0, prev[code]), (DAY, 11.0, day[code])]
        _mk_stock(d, code, rows)
        parts.append(pd.read_parquet(d / "daily" / "hfq" / f"{code}.parquet"))
    store.save_parquet(store.dataset_dir(d) / "daily_hfq.parquet",
                       pd.concat(parts, ignore_index=True))
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


def _fake_quotes(price_map: dict[str, dict]):
    return lambda wind_codes: {c: price_map[c] for c in wind_codes if c in price_map}


# ============================================================ ① 覆盖率检测 + 回补

def test_bad_coverage_detected_and_patched(tmp_path, monkeypatch):
    """覆盖率 1/3（<50%，且对前一日 100% 跌 67pp）判坏：B 回补、C 停牌不动。"""
    d = _mk_fixture(prev={CODE_A: 1e9, CODE_B: 1e9, CODE_C: 1e9},
                    day={CODE_A: 1e9, CODE_B: None, CODE_C: None})
    dl = _mk_dl(d)
    monkeypatch.setattr(source, "fetch_gtimg_quotes", _fake_quotes({
        CODE_B: {"latest_price": 1500.0, "amount": 5.0e8, "turnover": 2.5},
    }))
    patched, skipped = dl.refresh_daily_amounts(DAY)
    assert (patched, skipped) == (1, 1), "B 回补 1 只，C 停牌跳过 1 只"
    chk = dl.last_amount_check
    assert chk["bad"] is True and chk["coverage"] == pytest.approx(1 / 3)
    assert chk["prev_coverage"] == pytest.approx(1.0)


def test_normal_coverage_no_fetch(tmp_path, monkeypatch):
    """覆盖率 2/3 且与前一日持平（跌 0pp、绝对值 ≥50%）：正常，不触网。"""
    d = _mk_fixture(prev={CODE_A: 1e9, CODE_B: 1e9, CODE_C: None},
                    day={CODE_A: 1e9, CODE_B: 1e9, CODE_C: None})
    dl = _mk_dl(d)
    called = []

    def spy(wind_codes):
        called.append(list(wind_codes))
        return {}

    monkeypatch.setattr(source, "fetch_gtimg_quotes", spy)
    patched, skipped = dl.refresh_daily_amounts(DAY)
    assert (patched, skipped) == (0, 1)
    assert not called, "覆盖率正常时不得触发回补请求"
    assert dl.last_amount_check["bad"] is False


def test_coverage_drop_over_30pp_triggers(tmp_path, monkeypatch):
    """覆盖率 2/3（≥50%）但较前一日 100% 跌 33pp > 30pp：仍判坏并尝试回补。"""
    d = _mk_fixture(prev={CODE_A: 1e9, CODE_B: 1e9, CODE_C: 1e9},
                    day={CODE_A: 1e9, CODE_B: 1e9, CODE_C: None})
    dl = _mk_dl(d)
    called = []

    def spy(wind_codes):
        called.append(list(wind_codes))
        return {}

    monkeypatch.setattr(source, "fetch_gtimg_quotes", spy)
    patched, skipped = dl.refresh_daily_amounts(DAY)
    assert dl.last_amount_check["bad"] is True
    assert called and called[0] == [CODE_C], "应对缺失的 C 发起回补"
    assert (patched, skipped) == (0, 1)  # 无行情可补


# ============================================================ ② 只动 amount/turnover

def test_patch_only_amount_turnover(tmp_path, monkeypatch):
    """回补只改当日行的 amount/turnover，close/volume/pct_chg 等一概不动。"""
    d = _mk_fixture(prev={CODE_A: 1e9, CODE_B: 1e9, CODE_C: 1e9},
                    day={CODE_A: 1e9, CODE_B: None, CODE_C: None})
    before = pd.read_parquet(d / "daily" / "hfq" / f"{CODE_B}.parquet")
    dl = _mk_dl(d)
    monkeypatch.setattr(source, "fetch_gtimg_quotes", _fake_quotes({
        CODE_B: {"amount": 5.0e8, "turnover": 2.5},
    }))
    dl.refresh_daily_amounts(DAY)
    after = pd.read_parquet(d / "daily" / "hfq" / f"{CODE_B}.parquet")
    assert len(after) == len(before) and list(after.columns) == list(before.columns)
    row = after[after["date"] == pd.Timestamp(DAY)].iloc[0]
    assert row["amount"] == 5.0e8 and row["turnover"] == 2.5
    for col in ("close", "volume", "pct_chg", "open", "high", "low"):
        assert row[col] == before[before["date"] == pd.Timestamp(DAY)].iloc[0][col], col
    # 前一日行不受影响
    prev_row = after[after["date"] == pd.Timestamp(PREV)].iloc[0]
    assert prev_row["amount"] == 1e9 and prev_row["turnover"] == 1.0
    # 其他股票文件不动（A 正常 / C 停牌保持 NaN）
    a = pd.read_parquet(d / "daily" / "hfq" / f"{CODE_A}.parquet")
    c = pd.read_parquet(d / "daily" / "hfq" / f"{CODE_C}.parquet")
    assert a["amount"].tolist() == [1e9, 1e9]
    assert pd.isna(c[c["date"] == pd.Timestamp(DAY)].iloc[0]["amount"])
    # 合并表对应行同步回补
    m = pd.read_parquet(store.dataset_dir(d) / "daily_hfq.parquet")
    mb = m[(m["code"] == CODE_B) & (m["date"] == pd.Timestamp(DAY))].iloc[0]
    assert mb["amount"] == 5.0e8 and mb["turnover"] == 2.5
    mc = m[(m["code"] == CODE_C) & (m["date"] == pd.Timestamp(DAY))].iloc[0]
    assert pd.isna(mc["amount"]), "停牌股在合并表也不得被写假值"


# ============================================================ ③ 幂等

def test_idempotent_rerun(tmp_path, monkeypatch):
    """重跑：已有值的跳过（不二次覆盖），只剩停牌股缺失。"""
    d = _mk_fixture(prev={CODE_A: 1e9, CODE_B: 1e9, CODE_C: 1e9},
                    day={CODE_A: 1e9, CODE_B: None, CODE_C: None})
    dl = _mk_dl(d)
    monkeypatch.setattr(source, "fetch_gtimg_quotes", _fake_quotes({
        CODE_B: {"amount": 5.0e8, "turnover": 2.5},
    }))
    assert dl.refresh_daily_amounts(DAY) == (1, 1)
    # 第二跑：B 已有值 -> 不再出现在回补目标；仅剩 C（停牌）
    patched2, skipped2 = dl.refresh_daily_amounts(DAY)
    assert patched2 == 0 and skipped2 == 1
    b = pd.read_parquet(d / "daily" / "hfq" / f"{CODE_B}.parquet")
    assert b[b["date"] == pd.Timestamp(DAY)].iloc[0]["amount"] == 5.0e8, \
        "重跑不得二次改动已回补的值"


# ============================================================ ④ 市场聚合钳制

def _mk_market_fixture() -> Path:
    """市场页钳制 fixture：3 只股票 × 8 个交易日（单股文件 + 合并表）。

    - 10-19：首日，成交额 1 亿（无前日中位数 -> 不钳制，保留）；
    - 10-20..10-27（6 个正常日）：每日全市场 13000 亿；
    - 10-28：坏日，全市场 8 亿（事故数字）-> 应被钳成 NaN；
    - 10-29：正常日 13000 亿（不受坏日影响）；
    - 10-30：全 NaN 日（sina 事故的另一形态）-> 本就是 NaN。
    """
    from tempfile import mkdtemp
    d = Path(mkdtemp())
    (d / "daily" / "hfq").mkdir(parents=True)
    (d / "dataset").mkdir(parents=True)
    plan = [
        ("2026-10-19", BAD_AMOUNT_YI),          # 首日：无中位数参照
        ("2026-10-20", NORMAL_AMOUNT_YI),
        ("2026-10-21", NORMAL_AMOUNT_YI),
        ("2026-10-22", NORMAL_AMOUNT_YI),
        ("2026-10-23", NORMAL_AMOUNT_YI),
        ("2026-10-26", NORMAL_AMOUNT_YI),
        ("2026-10-27", NORMAL_AMOUNT_YI),
        ("2026-10-28", BAD_AMOUNT_YI),          # 事故日
        ("2026-10-29", NORMAL_AMOUNT_YI),
        ("2026-10-30", None),                   # 全 NaN 日
    ]
    parts = []
    for code in (CODE_A, CODE_B, CODE_C):
        rows = [(day, 10.0 + i, (total_yi * 1e8 / 3.0 if total_yi is not None else None))
                for i, (day, total_yi) in enumerate(plan)]
        _mk_stock(d, code, rows)
        parts.append(pd.read_parquet(d / "daily" / "hfq" / f"{code}.parquet"))
    store.save_parquet(store.dataset_dir(d) / "daily_hfq.parquet",
                       pd.concat(parts, ignore_index=True))
    pd.DataFrame({
        "code": [CODE_A, CODE_B, CODE_C], "name": ["甲", "乙", "丙"],
        "latest_price": [11.0, 11.0, 11.0], "pct_chg": [1.0, 1.0, 1.0],
        "turnover": [1.0, 1.0, 1.0], "total_mv": [1e9, 1e9, 1e9],
        "float_mv": [5e8, 5e8, 5e8],
    }).to_parquet(d / "stock_list.parquet", index=False)
    pd.DataFrame({"trade_date": pd.to_datetime(
        [p[0] for p in plan] + ["2026-10-19"])}).to_parquet(
        d / "trade_calendar.parquet", index=False)
    return d


def test_market_amount_clamp():
    """8 亿 vs 中位 13000 亿 -> NaN；正常日与首日不受影响。"""
    s = DataStore(str(_mk_market_fixture()))
    ms = s.market_series()
    by_day = dict(zip(ms["dates"], ms["amount_yi"]))
    assert by_day["2026-10-28"] is None, "坏日成交额应被钳制成 NaN（前端显示 —）"
    assert by_day["2026-10-30"] is None, "全 NaN 日保持 NaN"
    assert by_day["2026-10-27"] == pytest.approx(NORMAL_AMOUNT_YI, rel=1e-6)
    assert by_day["2026-10-29"] == pytest.approx(NORMAL_AMOUNT_YI, rel=1e-6), \
        "坏日之后的正常日不受影响"
    assert by_day["2026-10-19"] == pytest.approx(BAD_AMOUNT_YI, rel=1e-6), \
        "首日无中位数参照（窗口全 NaN）时不得钳制"


# ============================================================ ⑤ 腾讯行情 f37 解析

class _FakeRawClient:
    """替身 source._client.get：按顺序返回预设 GBK 字节流。"""

    def __init__(self, bodies: list[str]):
        self.bodies = list(bodies)

    def get(self, url, params=None, **kw):
        return self.bodies.pop(0).encode("gbk")


def _gtimg_body(sym: str, price: str, amount_wan: str, turnover: str = "1.23") -> str:
    f = ["51", "测试股", sym, price, "23.81", "24.00", "10000", "10000", "10000"]
    f += [""] * 28          # 9..36
    f += [amount_wan]       # 37 成交额（万元）
    f += [turnover]         # 38 换手率
    f += [""] * 5           # 39..43
    f += ["50.00", "97.24"]  # 44/45 市值（亿）
    return f'v_{sym}="' + "~".join(f) + '";'


def test_fetch_gtimg_quotes_amount_parse(monkeypatch):
    """f37 成交额万元 ×1e4 -> 元；f38 换手率；停牌（现价 0）不返回。"""
    body = (""
            + _gtimg_body("sz300687", "26.12", "64668")        # 64668 万 -> 元
            + _gtimg_body("sh600519", "0.00", "99999")          # 停牌：现价 0
            + _gtimg_body("sz000001", "10.50", "", "0.88"))     # 成交额缺失
    monkeypatch.setattr(source, "_client", _FakeRawClient([body]))
    out = source.fetch_gtimg_quotes(["300687.SZ", "600519.SH", "000001.SZ"])
    assert set(out) == {"300687.SZ", "000001.SZ"}, "停牌（现价 0）不得返回"
    a = out["300687.SZ"]
    assert a["amount"] == 64668 * 1e4, "f37 万元 ×1e4 存元"
    assert a["turnover"] == 1.23
    c = out["000001.SZ"]
    assert c["amount"] is None and c["turnover"] == 0.88, "f37 缺失 -> amount None"
