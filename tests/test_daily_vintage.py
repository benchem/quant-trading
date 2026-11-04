"""当日 bar 量指纹守卫单测（2026-11-04 生产事故：sina CDN 盘中快照缓存）。

背景：EOD 15:33 增量更新时 sina 日线 CDN 仍回 14 时盘中快照，000 段 35 只
当日 bar 写入盘中价+快照量（如 000573 bar 量 281 手 vs 真实 264,562 手）。
价差常 <0.5% 难以阈值判别（300199 错显 22.71→24.32 是日历钳制的下游放大），
量指纹最稳：量不受复权/除权影响，且腾讯行情与日线源相互独立。

防复发四件套：
1. fetch_gtimg_quotes 增返 volume(f6 原样)/quote_time(f30)；
2. Downloader.verify_daily_vintage：当日 bar 量 × 行情量比值带
   [0.95,1.05]∪[0.009,0.011]（科创板股-vs-手单位差）对账，
   嫌疑删行（审计 jsonl 留痕）+ 登记待修清单；
3. EOD 链（analyzer/updates.py）4d 阶段调用 + 一行日志；
4. 待修清单交由既有 repair_stocks 次日全量重拉自愈（复用，不新写）。

全部离线（fixture 本地数据 + monkeypatch，不触网）。
运行: .venv/bin/python -m pytest tests/test_daily_vintage.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, ".")

from downloader import source, store  # noqa: E402
from downloader.app import Downloader  # noqa: E402

# --------------------------------------------------------------- 事故数字锚点
CODE_BAD = "000573.SZ"    # 盘中快照坏 bar（bar 量 281 vs 真实 264,562）
CODE_OK = "300199.SZ"     # 正常 bar（量=行情量）
CODE_SUSP = "000831.SZ"   # 停牌：行情无 volume -> 跳过
CODE_KCB = "688001.SH"    # 科创板：行情为股、bar 为手（100 倍单位差）非坏
DAY = "2026-11-04"        # 事故日
PREV = "2026-11-03"
QT_TIME = "20261104161415"   # 腾讯行情时间戳（收盘后）


def _mk_stock(root: Path, code: str, rows: list[tuple[str, float]]) -> None:
    """写单股日线文件；rows = [(date, volume)]，close 任意。"""
    n = len(rows)
    closes = [10.0 + i for i in range(n)]
    pd.DataFrame({
        "date": pd.to_datetime([d for d, _ in rows]), "code": [code] * n,
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [v for _, v in rows],
        "amount": [1e8] * n, "amplitude": [0.0] * n, "pct_chg": [1.0] * n,
        "change": [0.1] * n, "turnover": [1.0] * n,
    }).to_parquet(root / "daily" / "hfq" / f"{code}.parquet", index=False)


def _mk_fixture(volumes: dict[str, float]) -> Path:
    """4 只股票单股文件 + dataset/daily_hfq 合并表（PREV/DAY 两天）。"""
    from tempfile import mkdtemp
    d = Path(mkdtemp())
    (d / "daily" / "hfq").mkdir(parents=True)
    (d / "dataset").mkdir(parents=True)
    parts = []
    for code, day_vol in volumes.items():
        _mk_stock(d, code, [(PREV, 1000.0), (DAY, day_vol)])
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


def _fake_quotes(vol_map: dict[str, dict]):
    return lambda wind_codes: {c: vol_map[c] for c in wind_codes if c in vol_map}


# ============================================================ ① 事故场景：快照坏 bar 删行

def test_snapshot_bar_dropped_and_repair_registered(tmp_path, monkeypatch):
    """000573 型（bar 281 vs 行情 264,562）：删当日行 + 审计 + 待修清单。"""
    d = _mk_fixture({CODE_BAD: 281.0, CODE_OK: 100000.0,
                     CODE_SUSP: 500.0, CODE_KCB: 94950.0})
    dl = _mk_dl(d)
    monkeypatch.setattr(source, "fetch_gtimg_quotes", _fake_quotes({
        CODE_BAD: {"volume": 264562.0, "quote_time": QT_TIME},
        CODE_OK: {"volume": 100000.0, "quote_time": QT_TIME},
        CODE_SUSP: {"volume": 0.0, "quote_time": QT_TIME},      # 停牌
        CODE_KCB: {"volume": 9495033.0, "quote_time": QT_TIME},  # 股口径
    }))
    res = dl.verify_daily_vintage(DAY)
    assert res["checked"] == 3, "停牌不进对账分母"
    assert res["suspects"] == 1 and res["dropped"] == 1

    # 单股文件：坏 bar 当日行已删，PREV 行保留
    f = d / "daily" / "hfq" / f"{CODE_BAD}.parquet"
    df = pd.read_parquet(f)
    dates = set(df["date"].astype(str).str[:10])
    assert dates == {PREV}, f"坏 bar 当日行应被删，实际 {dates}"

    # 审计 jsonl：删前行内容留痕
    audit = [json.loads(x) for x in
             (d / "vintage_dropped_hfq.jsonl").read_text().splitlines()]
    assert len(audit) == 1 and audit[0]["code"] == CODE_BAD
    assert audit[0]["bar_volume"] == 281 and audit[0]["quote_volume"] == 264562
    assert len(audit[0]["dropped_rows"]) == 1

    # 待修清单：已登记（次日 repair_stocks 全量重拉）
    items = dl._pending_repair_load()
    assert CODE_BAD in items and DAY in items[CODE_BAD]

    # 合并表：当日坏行同步删
    merged = pd.read_parquet(d / "dataset" / "daily_hfq.parquet")
    m_day = merged[merged["date"].astype(str).str[:10] == DAY]
    assert CODE_BAD not in set(m_day["code"])
    assert CODE_OK in set(m_day["code"]), "正常股当日行不得误删"


# ============================================================ ② 正常/单位差/停牌不误删

def test_normal_kcb_susp_untouched(tmp_path, monkeypatch):
    """正常股、科创板单位差（股 vs 手）、停牌均不删行。"""
    d = _mk_fixture({CODE_OK: 100000.0, CODE_KCB: 94950.0, CODE_SUSP: 500.0})
    dl = _mk_dl(d)
    monkeypatch.setattr(source, "fetch_gtimg_quotes", _fake_quotes({
        CODE_OK: {"volume": 100000.0, "quote_time": QT_TIME},
        CODE_KCB: {"volume": 9495033.0, "quote_time": QT_TIME},
        CODE_SUSP: {"volume": 0.0, "quote_time": QT_TIME},
    }))
    res = dl.verify_daily_vintage(DAY)
    assert res["dropped"] == 0 and res["suspects"] == 0
    assert res["skipped"] == 1, "停牌应计 skipped"
    for code in (CODE_OK, CODE_KCB, CODE_SUSP):
        df = pd.read_parquet(d / "daily" / "hfq" / f"{code}.parquet")
        assert (df["date"].astype(str).str[:10] == DAY).any(), f"{code} 行被误删"


# ============================================================ ③ 幂等与防御

def test_idempotent_rerun_no_rows(tmp_path, monkeypatch):
    """删行后二次运行：坏股不再命中（审计/待修不增长），正常股照常对账。"""
    d = _mk_fixture({CODE_BAD: 281.0, CODE_OK: 100000.0})
    dl = _mk_dl(d)
    monkeypatch.setattr(source, "fetch_gtimg_quotes", _fake_quotes({
        CODE_BAD: {"volume": 264562.0, "quote_time": QT_TIME},
        CODE_OK: {"volume": 100000.0, "quote_time": QT_TIME},
    }))
    r1 = dl.verify_daily_vintage(DAY)
    assert r1["dropped"] == 1
    r2 = dl.verify_daily_vintage(DAY)
    assert r2["checked"] == 1, "正常股当日行保留，照常对账"
    assert r2["suspects"] == 0 and r2["dropped"] == 0, "坏股已删行，不得重复处理"
    audit = (d / "vintage_dropped_hfq.jsonl").read_text().splitlines()
    assert len(audit) == 1, "审计 jsonl 不得重复追加"
    assert set(dl._pending_repair_load()) == {CODE_BAD}
    f = d / "daily" / "hfq" / f"{CODE_BAD}.parquet"
    assert DAY not in set(pd.read_parquet(f)["date"].astype(str).str[:10])


def test_stale_quote_day_skipped(tmp_path, monkeypatch):
    """行情 quote_time 非对账日（跨日重跑旧行情缓存）-> 不判不删。"""
    d = _mk_fixture({CODE_BAD: 281.0})
    dl = _mk_dl(d)
    monkeypatch.setattr(source, "fetch_gtimg_quotes", _fake_quotes({
        CODE_BAD: {"volume": 264562.0, "quote_time": "20261105100500"},
    }))
    res = dl.verify_daily_vintage(DAY)
    assert res["suspects"] == 0 and res["dropped"] == 0
    df = pd.read_parquet(d / "daily" / "hfq" / f"{CODE_BAD}.parquet")
    assert (df["date"].astype(str).str[:10] == DAY).any(), "行情日不匹配时不得删"


def test_quote_fetch_failure_no_touch(tmp_path, monkeypatch):
    """行情拉取失败（无真值）-> 不碰数据，返回全零。"""
    d = _mk_fixture({CODE_BAD: 281.0})

    def _boom(wind_codes):
        raise source.FetchError("腾讯行情失败")

    dl = _mk_dl(d)
    monkeypatch.setattr(source, "fetch_gtimg_quotes", _boom)
    res = dl.verify_daily_vintage(DAY)
    assert res["dropped"] == 0
    df = pd.read_parquet(d / "daily" / "hfq" / f"{CODE_BAD}.parquet")
    assert (df["date"].astype(str).str[:10] == DAY).any()


# ============================================================ ④ fetch_gtimg_quotes 新字段

class _FakeRawClient:
    def __init__(self, bodies: list[str]):
        self.bodies = list(bodies)

    def get(self, url, params=None, **kw):
        return self.bodies.pop(0).encode("gbk")


def _gtimg_body(sym: str, price: str, vol: str, amount_wan: str,
                quote_time: str = QT_TIME) -> str:
    f = ["51", "测试股", sym, price, "23.81", "24.00", vol, "5000", "5000"]
    f += [""] * 21          # 9..29
    f += [quote_time]       # 30 行情时间
    f += [""] * 6           # 31..36
    f += [amount_wan]       # 37 成交额（万元）
    f += ["1.23"]           # 38 换手率
    f += [""] * 5           # 39..43
    f += ["50.00", "97.24"]  # 44/45 市值（亿）
    return f'v_{sym}="' + "~".join(f) + '";'


def test_fetch_gtimg_quotes_volume_parse(monkeypatch):
    """f6 成交量原样返回、f30 行情时间；停牌（现价 0）仍不返回。"""
    body = (_gtimg_body("sz000573", "9.35", "264562", "24787")
            + _gtimg_body("sh600519", "0.00", "99999", "99999"))
    monkeypatch.setattr(source, "_client", _FakeRawClient([body]))
    out = source.fetch_gtimg_quotes(["000573.SZ", "600519.SH"])
    assert set(out) == {"000573.SZ"}, "停牌不得返回"
    q = out["000573.SZ"]
    assert q["volume"] == 264562.0
    assert q["quote_time"] == QT_TIME
    assert q["amount"] == 24787 * 1e4, "既有 amount 字段不受影响"
