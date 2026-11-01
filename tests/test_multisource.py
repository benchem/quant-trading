"""多源下载（同花顺/新浪/搜狐）+ 分批调度 + 复权锚定换算：离线单测。

背景（2026-11 交接单）:
  - 东财出口 IP 被 WAF 拉黑（快速失败 ~0.4s）、腾讯对科创板(688/689)返回 501、
    baostock 黑名单（探测会触发挂起模式）——科创板 617 只日线停在 2026-10-23；
  - 新增三源: 同花顺 v6(唯一有前复权，用于锚定全量重拉) / 新浪 CN_MarketDataService
    (仅不复权，窗口上限 1023 根) / 搜狐 hisHq(仅不复权，任意区间，覆盖北交所)；
  - 本地文件口径 = 后复权(hfq): 新源数据必须经「锚定换算」折算到存量基准后才可落盘，
    绝不许混基准写入；
  - 多源分批调度: 启动探测健康源 -> round-robin 分批 -> 单股失败重派(每股最多
    跨 2 源) -> seam 守卫拦截除权接缝 -> 升级 THS 锚定全量重拉。

全部离线（fixture + mock，不触网）。运行: .venv/bin/python -m pytest tests/test_multisource.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, ".")

from downloader.http import HttpClient  # noqa: E402
from downloader.ratelimit import RateLimiter  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ================================================================ A. HTTP 层
def test_http_get_supports_headers_limiter_timeout(monkeypatch=None):
    """HttpClient.get/get_json 透传 headers/limiter/timeout（新源需要 Referer 与独立限速）。"""
    calls = {}

    def fake_engine(self, url, params, headers=None, timeout=None):
        calls.update(url=url, params=params, headers=headers, timeout=timeout)
        return b'{"ok": 1}'

    orig = HttpClient._engine_requests
    HttpClient._engine_requests = fake_engine
    try:
        c = HttpClient(rate=1000)
        c.engine = "requests"  # 跳过引擎探测（不触网）
        lim = RateLimiter(0)   # 独立限速器（不限速，测试不等待）
        body = c.get("https://x/api", {"a": "1"},
                     headers={"Referer": "https://y"}, limiter=lim, timeout=5.0)
        assert body == b'{"ok": 1}'
        assert calls["headers"] == {"Referer": "https://y"}, calls
        assert calls["timeout"] == 5.0, calls
        assert calls["params"] == {"a": "1"}, calls
        js = c.get_json("https://x/api", headers={"Referer": "https://y"},
                        limiter=lim, timeout=5.0)
        assert js == {"ok": 1}
        # 默认调用（不带新参数）行为不变
        assert c.get("https://x/api") == b'{"ok": 1}'
        assert calls["headers"] is None and calls["timeout"] is None
    finally:
        HttpClient._engine_requests = orig


# ================================================================ B. 三源解析器
from downloader import vendors  # noqa: E402
from downloader.errors import FetchError, SourceUnsupportedError  # noqa: E402


def test_ths_parser():
    """同花顺 JSONP：函数名任意、bar 内逗号分隔且可能空列、close=第5列、volume 股->手。"""
    text = (FIXTURES / "ths_688001_02_last.js").read_text(encoding="utf-8")
    df = vendors.parse_ths_jsonp(text, "688001.SH")
    assert len(df) == 10, len(df)
    # IPO bar（brief 真实样本）：OHLC 精确断言；bar 内有 ",," 空列不能炸
    r0 = df.iloc[0]
    assert r0["date"] == pd.Timestamp("2019-07-22")
    assert (r0["open"], r0["high"], r0["low"], r0["close"]) == (24.00, 26.45, 23.80, 24.30)
    assert r0["volume"] == 411609          # 41160900 股 -> 411609 手
    assert r0["amount"] == 999518980.0
    assert r0["turnover"] == 4.836
    # 尾 bar
    rn = df.iloc[-1]
    assert rn["date"] == pd.Timestamp("2026-10-30") and rn["close"] == 35.80
    # 日期升序 + code 列
    assert df["date"].is_monotonic_increasing
    assert set(df["code"]) == {"688001.SH"}


def test_ths_parser_year_file_and_bj():
    """同花顺年度文件同构可解析；北交所统一 hs_ 前缀。"""
    df = vendors.parse_ths_jsonp(
        (FIXTURES / "ths_688001_02_2019.js").read_text(encoding="utf-8"), "688001.SH")
    assert len(df) == 4 and df.iloc[0]["close"] == 24.30 and df.iloc[-1]["close"] == 25.60
    bj = vendors.parse_ths_jsonp(
        (FIXTURES / "ths_920002_02_last.js").read_text(encoding="utf-8"), "920002.BJ")
    assert len(bj) == 3 and bj.iloc[-1]["close"] == 10.75
    # 符号映射：沪深 + 北交所统一 hs_
    assert vendors.ths_symbol("688001.SH") == "hs_688001"
    assert vendors.ths_symbol("000001.SZ") == "hs_000001"
    assert vendors.ths_symbol("920002.BJ") == "hs_920002"


def test_sina_parser():
    """新浪 JSONP：var t=([...])、字段全字符串、volume 股->手、null/空响应不炸。"""
    df = vendors.parse_sina_jsonp(
        (FIXTURES / "sina_688001.jsonp").read_text(encoding="utf-8"), "688001.SH")
    assert len(df) == 9, len(df)
    r = df[df["date"] == pd.Timestamp("2026-10-29")].iloc[0]
    assert (r["open"], r["high"], r["low"], r["close"]) == (39.150, 40.700, 38.200, 38.530)
    assert r["volume"] == 104938        # 10493800 股 -> 104938 手
    # 不支持代码返回 null / 空数组 -> 空 DataFrame（不抛错）
    assert len(vendors.parse_sina_jsonp("var t=(null)", "920002.BJ")) == 0
    assert len(vendors.parse_sina_jsonp("var t=([])", "920002.BJ")) == 0
    assert len(vendors.parse_sina_jsonp("", "920002.BJ")) == 0
    # 符号映射：sh/sz；北交所不支持
    assert vendors.sina_symbol("688001.SH") == "sh688001"
    assert vendors.sina_symbol("000001.SZ") == "sz000001"
    try:
        vendors.sina_symbol("920002.BJ")
        raise AssertionError("北交所应抛 SourceUnsupportedError")
    except SourceUnsupportedError:
        pass


def test_sohu_parser():
    """搜狐 hisHq 列序坑：close=第2列、low 在 high 前；order=D 降序需转升序；volume=手。"""
    df = vendors.parse_sohu_json(
        (FIXTURES / "sohu_688001.json").read_text(encoding="utf-8"), "688001.SH")
    assert len(df) == 3, len(df)
    # 降序响应 -> 升序输出
    assert list(df["date"]) == [pd.Timestamp("2026-10-23"),
                                pd.Timestamp("2026-10-29"),
                                pd.Timestamp("2026-10-30")]
    r = df.iloc[-1]  # 2026-10-30 真实样本
    # 列序坑精确断言：open=33.60 close=35.80 low=33.10 high=36.00（不是 low=35.80!）
    assert (r["open"], r["close"], r["low"], r["high"]) == (33.60, 35.80, 33.10, 36.00)
    assert r["volume"] == 161434          # 搜狐 volume 直接是手
    assert r["amount"] == 56520.90 * 1e4  # 万 -> 元
    assert r["turnover"] == 1.62 and r["change"] == -2.73 and r["pct_chg"] == -7.09
    # 北交所覆盖
    bj = vendors.parse_sohu_json(
        (FIXTURES / "sohu_920002.json").read_text(encoding="utf-8"), "920002.BJ")
    assert len(bj) == 2 and bj.iloc[-1]["close"] == 10.75
    assert vendors.sohu_symbol("920002.BJ") == "cn_920002"
    # 无数据（status!=0 / 空 hq）-> 空 DataFrame
    assert len(vendors.parse_sohu_json('[{"status":2}]', "600000.SH")) == 0
    assert len(vendors.parse_sohu_json('[{"status":0,"hq":[]}]', "600000.SH")) == 0


def test_ths_parser_rejects_garbage():
    """畸形响应必须报错（而不是静默空/错数据）。"""
    for bad in ("", "garbage", "quotebridge_x(({)", '{"symbol":"hs_688001"}'):
        try:
            vendors.parse_ths_jsonp(bad, "688001.SH")
            raise AssertionError(f"畸形响应未报错: {bad!r}")
        except FetchError:
            pass


# ================================================================ C. 复权锚定换算
from downloader.anchor import (  # noqa: E402
    AnchorError,
    bars_after,
    compute_anchor_ratio,
    compute_tail_ratio,
    scale_prices,
)

#: 锚定换算基准比例（交接单实测：688001 存量 hfq 10-23 收盘 46.0359 / 新浪 raw 44.57）
R_ANCHOR = 46.0359 / 44.57


def _mk_df(dates, closes, opens=None, code="688001.SH"):
    return pd.DataFrame({
        "date": pd.to_datetime(list(dates)),
        "code": [code] * len(closes),
        "open": list(opens) if opens is not None else [c - 0.5 for c in closes],
        "high": [c + 1.0 for c in closes],
        "low": [c - 1.0 for c in closes],
        "close": list(closes),
        "volume": [1000] * len(closes),
        "amount": [c * 100000 for c in closes],
        "turnover": [1.5] * len(closes),
        "change": [0.1] * len(closes),
        "pct_chg": [0.2] * len(closes),
        "amplitude": [3.0] * len(closes),
    })


def test_anchor_ratio_and_conversion():
    """锚定换算：ratio=存量hfq/qfq 重叠中位数 -> 全序列×ratio 与存量精确同基准。"""
    dates = list(pd.bdate_range("2026-09-01", "2026-10-23"))  # 38 个交易日
    qfq_closes = [40.0 + 0.12 * i for i in range(len(dates))]
    qfq_closes[-1] = 44.57
    old = _mk_df(dates, [c * R_ANCHOR for c in qfq_closes])
    # other = 同花顺式 qfq：38 根重叠 + 5 根新 bar
    more_dates = list(pd.bdate_range("2026-10-26", periods=5))
    more_closes = [44.30, 43.90, 41.50, 38.53, 35.80]
    other = _mk_df(dates + more_dates, qfq_closes + more_closes)
    ratio, n = compute_anchor_ratio(old, other)
    assert abs(ratio - R_ANCHOR) < 1e-9, ratio
    assert n == 20, f"重叠应截取最近 20 根: {n}"  # 38 根重叠 -> 取最近 20
    # 换算后序列 = 与存量同基准的 hfq
    out = scale_prices(bars_after(other, pd.Timestamp("2026-10-23")), ratio)
    assert list(out["date"]) == more_dates
    for got, want in zip(out["close"], [c * R_ANCHOR for c in more_closes]):
        assert abs(got - want) < 1e-9
    # volume/turnover/pct_chg 不随价格缩放；change（价差）缩放
    assert list(out["volume"]) == [1000] * 5
    assert list(out["turnover"]) == [1.5] * 5
    assert list(out["pct_chg"]) == [0.2] * 5
    assert abs(out["change"].iloc[0] - 0.1 * ratio) < 1e-12


def test_anchor_rejects_insufficient_overlap():
    """反例1: 重叠 < 5 根 -> 拒绝锚定。"""
    old = _mk_df(pd.bdate_range("2026-10-01", periods=10), [10 + i for i in range(10)])
    other = _mk_df(pd.bdate_range("2026-10-15", periods=3), [20.0, 21.0, 22.0])
    try:
        compute_anchor_ratio(old, other)
        raise AssertionError("重叠不足应拒绝锚定")
    except AnchorError as e:
        assert "重叠" in str(e), e


def test_anchor_rejects_cv_exceeded():
    """反例2: ratio 变异系数超 0.5% -> 拒绝锚定（重叠窗口内混入断裂数据）。"""
    dates = list(pd.bdate_range("2026-10-01", periods=10))
    closes = [10.0 + 0.1 * i for i in range(10)]
    other = _mk_df(dates, closes)
    # 9 根 ratio=2.0 + 1 根 ratio=2.1（5% 离群，模拟存量末根混入异基准价）
    old_closes = [c * 2.0 for c in closes]
    old_closes[9] = closes[9] * 2.1
    old = _mk_df(dates, old_closes)
    try:
        compute_anchor_ratio(old, other)
        raise AssertionError("CV 超标应拒绝锚定")
    except AnchorError as e:
        assert "CV" in str(e) or "变异" in str(e), e


def test_tail_ratio_raw_continuation():
    """raw 续接锚定：ratio = 存量最后 bar hfq_close / 该日 raw_close。"""
    old = _mk_df(["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"],
                 [47.10, 46.58, 45.65, 46.0359])
    raw = _mk_df(["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23",
                  "2026-10-26", "2026-10-27"],
                 [45.60, 45.20, 44.10, 44.57, 44.30, 43.90])
    ratio = compute_tail_ratio(old, raw)
    assert abs(ratio - R_ANCHOR) < 1e-9, ratio
    out = scale_prices(bars_after(raw, pd.Timestamp("2026-10-23")), ratio)
    assert abs(out["close"].iloc[0] - 44.30 * R_ANCHOR) < 1e-9
    # raw 缺存量最后 bar 日期 -> 无法锚定
    raw_gap = raw[raw["date"] <= pd.Timestamp("2026-10-22")]
    try:
        compute_tail_ratio(old, raw_gap)
        raise AssertionError("锚定日缺失应报错")
    except AnchorError:
        pass


# ================================================================ D. fetchers + 探测
class FakeVendorClient:
    """替身 vendors._client()：按脚本顺序返回预设响应体，记录调用参数。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, limiter=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers,
                           "limiter": limiter, "timeout": timeout})
        if not self.responses:
            raise AssertionError("意外的额外请求")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r.encode("utf-8")


def with_fake_vendor_client(responses, fn):
    fake = FakeVendorClient(responses)
    orig = vendors._client
    vendors._client = lambda: fake
    try:
        return fn(fake)
    finally:
        vendors._client = orig


def _fx(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _year_file(year: int, bars: str, symbol: str = "hs_688001",
               start: str = "20190722") -> str:
    return (f'quotebridge_v6_line_{symbol}_02_{year}({{"symbol":"{symbol}",'
            f'"total":9,"start":"{start}","year":{{"{year}":9}},'
            f'"data":"{bars}"}})')


def _last_js(bars: str, symbol: str = "hs_688001", start: str = "20190722") -> str:
    """真实形态的 last.js：meta 含全史起点，data 仅最近若干根。"""
    return (f'quotebridge_v6_line_{symbol}_02_last({{"symbol":"{symbol}",'
            f'"name":"x","total":1760,"start":"{start}",'
            f'"year":{{"2019":118,"2026":176}},"data":"{bars}"}})')


def test_fetch_ths_recent():
    """last.js 增量：URL/Referer/独立限速器正确，返回解析后的 qfq 序列。"""
    def run(fake):
        df = vendors.fetch_ths_recent_qfq("688001.SH")
        assert len(df) == 10 and df.iloc[-1]["close"] == 35.80
        c = fake.calls[0]
        assert c["url"] == "https://d.10jqka.com.cn/v6/line/hs_688001/02/last.js"
        assert c["headers"] == {"Referer": vendors.THS_REFERER}
        assert c["limiter"] is vendors.ths_limiter
    with_fake_vendor_client([_fx("ths_688001_02_last.js")], run)


def test_fetch_ths_full_year_by_year():
    """年度全史（逐年文件解释）：降序拉 2026..2019 + last.js，union 去重、过滤窗口。"""
    def run(fake):
        df = vendors.fetch_ths_qfq_full("688001.SH", "20190101", "20261030")
        # last.js 2 根近期 + 2020~2026 各 1 根 + 2019 上市日 1 根 = 10 根
        assert len(df) == 10, len(df)
        assert df["date"].is_monotonic_increasing
        assert df["date"].min() == pd.Timestamp("2019-07-22")
        assert df["date"].max() == pd.Timestamp("2026-10-30")
        segs = [c["url"].rsplit("/", 1)[-1] for c in fake.calls]
        assert segs[0] == "last.js"
        assert segs[1:] == [f"{y}.js" for y in range(2026, 2018, -1)], segs
    recent = ("20261029,39.15,40.70,38.20,38.53,10493800,404312140.00,2.601,,0.00,0;"
              "20261030,33.60,36.00,33.10,35.80,16143400,565209000.00,4.003,,0.00,0")
    responses = [_last_js(recent)]
    responses += [_year_file(y, f"{y}0102,10.00,10.50,9.80,10.20,100000,1020000.00,1.0,,0.00,0")
                  for y in range(2026, 2019, -1)]
    responses.append(_year_file(2019, "20190722,24.00,26.45,23.80,24.30,41160900,999518980.00,4.836,,0.00,0"))
    with_fake_vendor_client(responses, run)


def test_fetch_ths_full_early_stop_when_year_file_covers_all():
    """年度文件返回全史的解释：当前年文件覆盖即停（防御式 union，两种解释都正确）。"""
    def run(fake):
        df = vendors.fetch_ths_qfq_full("688001.SH", "20190101", "20261030")
        assert len(df) == 5
        # last.js 之后只拉了 2026.js（其 data 含 2019 起全史）即满足覆盖
        segs = [c["url"].rsplit("/", 1)[-1] for c in fake.calls]
        assert segs == ["last.js", "2026.js"], segs
    recent = ("20261029,39.15,40.70,38.20,38.53,10493800,404312140.00,2.601,,0.00,0;"
              "20261030,33.60,36.00,33.10,35.80,16143400,565209000.00,4.003,,0.00,0")
    full_hist = ("20190722,24.00,26.45,23.80,24.30,41160900,999518980.00,4.836,,0.00,0;"
                 "20250102,30.00,30.50,29.80,30.20,100000,3020000.00,1.0,,0.00,0;"
                 "20261020,45.80,46.20,45.10,45.60,6723400,306824540.00,1.668,,0.00,0;"
                 "20261029,39.15,40.70,38.20,38.53,10493800,404312140.00,2.601,,0.00,0;"
                 "20261030,33.60,36.00,33.10,35.80,16143400,565209000.00,4.003,,0.00,0")
    with_fake_vendor_client([_last_js(recent), _year_file(2026, full_hist)], run)


def test_fetch_sina_raw():
    """新浪：必须带 Referer、datalen=1023、scale=240；窗口过滤；北交所在请求前拒绝。"""
    def run(fake):
        df = vendors.fetch_sina_raw("688001.SH", "20261024", "20261030")
        assert list(df["date"]) == list(pd.to_datetime(
            ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]))
        c = fake.calls[0]
        assert c["url"].startswith("https://quotes.sina.cn/cn/api/jsonp_v2.php/")
        assert c["params"]["symbol"] == "sh688001"
        assert c["params"]["scale"] == "240" and c["params"]["datalen"] == "1023"
        assert c["headers"] == {"Referer": vendors.SINA_REFERER}
        assert c["limiter"] is vendors.sina_limiter
    with_fake_vendor_client([_fx("sina_688001.jsonp")], run)
    # 北交所：不发请求直接拒绝
    with_fake_vendor_client([], lambda fake: None)
    try:
        vendors.fetch_sina_raw("920002.BJ", "20261024", "20261030")
        raise AssertionError("北交所应抛 SourceUnsupportedError")
    except SourceUnsupportedError:
        pass


def test_fetch_sohu_raw():
    """搜狐：code=cn_ 前缀、order=D/period=d 参数、无头也行；升序窗口过滤。"""
    def run(fake):
        df = vendors.fetch_sohu_raw("688001.SH", "20261023", "20261030")
        assert list(df["date"]) == list(pd.to_datetime(
            ["2026-10-23", "2026-10-29", "2026-10-30"]))
        c = fake.calls[0]
        assert c["url"] == "https://q.stock.sohu.com/hisHq"
        assert c["params"] == {"code": "cn_688001", "start": "20261023",
                               "end": "20261030", "stat": "1", "order": "D",
                               "period": "d"}
        assert c["limiter"] is vendors.sohu_limiter
    with_fake_vendor_client([_fx("sohu_688001.json")], run)


def test_vendor_probes():
    """探测：健康源 True（5s 超时透传）；故障源 False（不抛错）。"""
    sohu_probe_resp = ('[{"status":0,"hq":[["2024-01-02","1700.00","1699.00",'
                       '"-1.00","-0.06","1695.00","1710.00","29876","50745.20",'
                       '"0.24","0.88"]]}]')
    def run_ok(fake):
        assert vendors.probe_ths() is True
        assert fake.calls[0]["timeout"] == 5.0
        assert vendors.probe_sina() is True
        assert vendors.probe_sohu() is True
    with_fake_vendor_client([_fx("ths_920002_02_last.js"), _fx("sina_688001.jsonp"),
                             sohu_probe_resp], run_ok)

    def run_bad(fake):
        assert vendors.probe_ths() is False
        assert vendors.probe_sina() is False
        assert vendors.probe_sohu() is False
    with_fake_vendor_client([FetchError("HTTP 403"), FetchError("HTTP 403"),
                             FetchError("HTTP 403")], run_bad)


# ================================================================ E. 多源分批调度器
from downloader.scheduler import (  # noqa: E402
    AllSourcesUnavailableError,
    MultiSourceScheduler,
    SourceUnit,
    build_source_units,
)


def _mk_unit(name, probe=True, fail_codes=(), reject_codes=(), calls=None):
    """可编程替身源：fail_codes 中的代码拉取抛错；reject_codes 不支持。"""
    def fetch(code, start, end, old, full=False):
        if calls is not None:
            calls.append((name, code))
        if code in fail_codes:
            raise FetchError(f"{name} 拉取失败 {code}")
        return _mk_df([pd.Timestamp(start) + pd.Timedelta(days=1)], [10.0])
    u = SourceUnit(name, fetch_fn=fetch,
                   probe_fn=lambda: probe,
                   supports=lambda code: code not in reject_codes)
    return u


def _process_recorder(attempts):
    """process_one 替身：调用 unit.fetch 真实拉取（异常=该源对此股失败），
    记录每股尝试过的源序列（与应用层 _update_stock_one 的职责一致）。"""
    def process(code, name, unit):
        attempts.setdefault(code, []).append(unit.name)
        try:
            unit.fetch(code, "20260101", "20260102", None, False)
            return code, "updated", ""
        except Exception as e:  # noqa: BLE001
            return code, "failed", f"{type(e).__name__}: {e}"
    return process


def test_scheduler_batch_covers_all_with_one_source_down():
    """mock 3 源 1 挂（探测失败）：健康源分批覆盖全部股票。"""
    a, b, c = (_mk_unit("ths"), _mk_unit("sina"), _mk_unit("sohu", probe=False))
    attempts: dict[str, list] = {}
    sched = MultiSourceScheduler([a, b, c])
    targets = [(f"60000{i}.SH", f"股{i}") for i in range(6)]
    results, per_source = sched.run(targets, _process_recorder(attempts))
    assert len(results) == 6 and all(s == "updated" for s, _ in results.values())
    # 全部股票被处理，且只派给了两个健康源（round-robin 各 3 只）
    sources_used = {u for v in attempts.values() for u in v}
    assert sources_used == {"ths", "sina"}, sources_used
    n_ths = sum(1 for v in attempts.values() if v == ["ths"])
    n_sina = sum(1 for v in attempts.values() if v == ["sina"])
    assert (n_ths, n_sina) == (3, 3), (n_ths, n_sina)
    assert per_source["ths"]["ok"] == 3 and per_source["sina"]["ok"] == 3
    assert "sohu" not in per_source


def test_scheduler_redispatches_failed_stock_to_other_source():
    """失败股重派到其他源；不重复派同一源；跨源上限可配；仍失败进 failed 结果。"""
    calls: list = []
    a = _mk_unit("ths", fail_codes={f"60000{i}.SH" for i in range(6)}, calls=calls)
    b = _mk_unit("sina", fail_codes={"600003.SH"}, calls=calls)  # 600003 在 sina 也挂
    c = _mk_unit("sohu", calls=calls)
    attempts: dict[str, list] = {}
    # 上限放宽到 3：演示「连挂两源 -> 第三源兜底成功」
    sched = MultiSourceScheduler([a, b, c], max_sources_per_stock=3)
    targets = [(f"60000{i}.SH", "") for i in range(6)]
    results, _ps = sched.run(targets, _process_recorder(attempts))
    assert all(s == "updated" for s, _ in results.values()), results
    for code, tried in attempts.items():
        assert len(set(tried)) == len(tried)    # 不重复派同一源
    # round-robin: 600000/600003 首发 ths -> 失败重派其他源成功
    assert attempts["600000.SH"] == ["ths", "sina"], attempts["600000.SH"]
    # 600003 连挂 ths+sina -> 落到 sohu（跨 3 源成功）
    assert attempts["600003.SH"] == ["ths", "sina", "sohu"], attempts["600003.SH"]
    # 默认上限 2：全源都失败的代码恰尝试 2 个源后进 failed
    units2 = [_mk_unit(n, fail_codes={"600005.SH"}, calls=calls)
              for n in ("ths", "sina", "sohu", "em")]
    attempts2: dict[str, list] = {}
    sched2 = MultiSourceScheduler(units2)
    results2, _ = sched2.run([("600005.SH", "")], _process_recorder(attempts2))
    assert results2["600005.SH"][0] == "failed"
    assert attempts2["600005.SH"] == ["ths", "sina"], attempts2


def test_scheduler_all_sources_down_raises():
    """全挂时报错（不静默空跑）。"""
    units = [_mk_unit("ths", probe=False), _mk_unit("sina", probe=False),
             _mk_unit("sohu", probe=False)]
    sched = MultiSourceScheduler(units)
    try:
        sched.run([("600000.SH", "")], _process_recorder({}))
        raise AssertionError("全挂应报错")
    except AllSourcesUnavailableError as e:
        assert "ths" in str(e) and "sohu" in str(e)


def test_scheduler_circuit_breaker_and_supports():
    """源连续失败熔断：剩余队列重派其他源完成；不支持代码不派给该源。"""
    calls: list = []
    a = _mk_unit("tx", fail_codes={f"60000{i}.SH" for i in range(30)}, calls=calls)
    b = _mk_unit("sina", calls=calls, reject_codes={"600028.SH", "600029.SH"})  # 模拟不支持
    c = _mk_unit("sohu", calls=calls)
    sched = MultiSourceScheduler([a, b, c])
    targets = [(f"60000{i}.SH", "") for i in range(30)]
    attempts: dict[str, list] = {}
    results, _ps = sched.run(targets, _process_recorder(attempts))
    assert all(s == "updated" for s, _ in results.values())
    # tx 连续失败达到熔断阈值(8)后不再接收任务
    tx_calls = [x for x in calls if x[0] == "tx"]
    assert len(tx_calls) == 8, len(tx_calls)
    # 不支持的代码从未派给 sina
    assert not any(x[0] == "sina" and x[1] in ("600028.SH", "600029.SH")
                   for x in calls), calls
    # 被熔断源剩余任务重派给了其他源，且每股最多跨 2 源
    for code, tried in attempts.items():
        assert len(tried) <= 2


def test_build_source_units_wiring():
    """单元工厂：五源齐备、探测/支持接线正确（tx 已恢复科创板支持）。"""
    units = {u.name: u for u in build_source_units()}
    assert set(units) == {"em", "tx", "ths", "sina", "sohu"}
    assert units["tx"].supports("688001.SH") is True
    assert units["tx"].supports("689009.SH") is True
    assert units["tx"].supports("600519.SH") is True
    assert units["sina"].supports("920002.BJ") is False
    assert units["sina"].supports("000001.SZ") is True
    assert units["ths"].supports("920002.BJ") is True
    assert units["sohu"].supports("920002.BJ") is True
    assert units["em"].supports("920002.BJ") is True
    # 探测函数已接线
    for u in units.values():
        assert callable(u.probe_fn)


# ================================================================ F. app 层接线
import contextlib
import io

import downloader.app as app_mod  # noqa: E402
from downloader import store  # noqa: E402
from downloader.app import Downloader, _break_date_from_reason  # noqa: E402


def _mk_dl(root, adjust: str = "hfq", start: str = "20200101", mode=None) -> Downloader:
    """离线构造 Downloader（跳过 __init__，不触网；mode 显式覆盖类默认 serial）。"""
    d = Downloader.__new__(Downloader)
    d.data_dir = Path(root)
    d.adjust = adjust
    d.start = start
    d.workers = 2
    d.rate = 0
    d.rotator = None
    if mode is not None:
        d.mode = mode
    return d


def _write_old(root, code: str, days, closes) -> Path:
    p = store.daily_dir(root, "hfq") / f"{code}.parquet"
    store.save_parquet(p, _mk_df(days, closes, code=code))
    return p


class _patch:
    """临时替换模块属性（上下文管理器，测试风格与文件其余部分一致）。"""

    def __init__(self, obj, name, value):
        self.obj, self.name, self.value = obj, name, value

    def __enter__(self):
        self.saved = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.value)
        return self

    def __exit__(self, *exc):
        setattr(self.obj, self.name, self.saved)
        return False


def test_update_stock_one_fetch_fn(tmp_path):
    """fetch_fn 注入：参数透传 (code,start,end,old,full)；异常计 failed 且不包 with_retry。"""
    d = _mk_dl(tmp_path)
    code = "688001.SH"
    days = list(pd.bdate_range("2026-09-01", "2026-10-23"))
    _write_old(tmp_path, code, days, [40.0 + 0.1 * i for i in range(len(days))])
    seen = {}

    def fetch(code_, start, end, old_, full):
        seen.update(code=code_, start=start, end=end, old=old_, full=full)
        return _mk_df(pd.bdate_range("2026-10-26", "2026-10-30"),
                      [44.30, 43.90, 41.50, 38.53, 35.80], code=code_)

    c, status, msg = d._update_stock_one(code, "华兴源创", "20261030", False, fetch_fn=fetch)
    assert (c, status) == (code, "updated"), (status, msg)
    assert seen["start"] == "20261024" and seen["end"] == "20261030", seen
    assert seen["full"] is False and len(seen["old"]) == len(days)
    after = store.load_daily(store.daily_dir(tmp_path, "hfq") / f"{code}.parquet")
    assert pd.Timestamp(after["date"].max()) == pd.Timestamp("2026-10-30")

    calls = []

    def bad(code_, start, end, old_, full):
        calls.append(1)
        raise FetchError("boom")

    _c2, status2, _m2 = d._update_stock_one(code, "华兴源创", "20261105", False, fetch_fn=bad)
    assert status2 == "failed", status2
    assert len(calls) == 1, "fetch_fn 路径不得包 with_retry（重派即重试）"


def test_update_stocks_batch_mode_and_new_stock_pool(tmp_path):
    """auto 模式：新源健康 -> 多源分批；失败股重派；新股只派 em/tx；per-source 日志。"""
    d = _mk_dl(tmp_path, mode="auto")
    code_old, code_new = "688001.SH", "600900.SH"
    days = list(pd.bdate_range("2026-09-01", "2026-10-23"))
    _write_old(tmp_path, code_old, days, [40.0 + 0.1 * i for i in range(len(days))])
    calls: list = []

    def em_fetch(code, start, end, old_, full=False):
        calls.append(("em", code))
        if code == code_old:
            raise FetchError("em 出口 IP 被拉黑")   # 存量股在 em 失败 -> 重派 ths
        return _mk_df(pd.bdate_range("2026-10-24", "2026-10-30"),
                      [10.0 + i for i in range(5)], code=code)

    def ths_fetch(code, start, end, old_, full=False):
        calls.append(("ths", code))
        return _mk_df(pd.bdate_range("2026-10-24", "2026-10-30"),
                      [43.8 + 0.1 * i for i in range(5)], code=code)

    def raw_fetch(code, start, end, old_, full=False):
        calls.append(("raw", code))
        return _mk_df(pd.bdate_range("2026-10-24", "2026-10-30"),
                      [43.8 + 0.1 * i for i in range(5)], code=code)

    def fake_units(adjust="hfq"):
        return [SourceUnit("em", em_fetch, lambda: True),
                SourceUnit("tx", em_fetch, lambda: False),  # 探测挂
                SourceUnit("ths", ths_fetch, lambda: True, needs_old=True),
                SourceUnit("sina", raw_fetch, lambda: True, needs_old=True),
                SourceUnit("sohu", raw_fetch, lambda: True, needs_old=True)]

    buf = io.StringIO()
    with _patch(app_mod, "build_source_units", fake_units):
        with contextlib.redirect_stdout(buf):
            stats = d.update_stocks(codes=f"{code_old},{code_new}", end="20261030",
                                    lag_retry_delay=0)
    assert stats.get("updated") == 1 and stats.get("new") == 1, stats
    # 新股（无本地文件）只派给 em（tx 探测挂；needs_old 源不接新股）
    new_srcs = {x[0] for x in calls if x[1] == code_new}
    assert new_srcs == {"em"}, calls
    # 存量股: em 失败重派到 ths 成功
    assert [("em", code_old), ("ths", code_old)] == [x for x in calls if x[1] == code_old], calls
    assert "per-source:" in buf.getvalue(), buf.getvalue()
    assert d._ths_enabled is True


def test_update_stocks_serial_mode_forces_legacy(tmp_path):
    """--serial：强制既有串行链路，不探测不建调度单元。"""
    d = _mk_dl(tmp_path, mode="serial")
    code = "600519.SH"
    days = list(pd.bdate_range("2026-09-01", "2026-10-23"))
    _write_old(tmp_path, code, days, [40.0 + 0.1 * i for i in range(len(days))])

    def fake_fetch(wind_code, start, end, adjust):
        return _mk_df(pd.bdate_range("2026-10-26", "2026-10-30"),
                      [43.8 + 0.1 * i for i in range(5)], code=wind_code)

    def boom_units(adjust="hfq"):
        raise AssertionError("serial 模式不得构建多源调度单元")

    with _patch(app_mod, "build_source_units", boom_units), \
            _patch(app_mod.source, "fetch_daily", fake_fetch):
        stats = d.update_stocks(codes=code, end="20261030", lag_retry_delay=0)
    assert stats.get("updated") == 1, stats
    assert d._ths_enabled is False


def test_update_stocks_auto_fallback_when_new_sources_down(tmp_path):
    """auto 但三新源全挂 -> 完整退回串行链路（零回归路径）。"""
    d = _mk_dl(tmp_path, mode="auto")
    code = "600519.SH"
    days = list(pd.bdate_range("2026-09-01", "2026-10-23"))
    _write_old(tmp_path, code, days, [40.0 + 0.1 * i for i in range(len(days))])

    def nope(*_a, **_k):
        raise AssertionError("新源全挂时不得调用任何单元 fetch")

    def fake_units(adjust="hfq"):
        return [SourceUnit("em", nope, lambda: True),
                SourceUnit("tx", nope, lambda: True),
                SourceUnit("ths", nope, lambda: False, needs_old=True),
                SourceUnit("sina", nope, lambda: False, needs_old=True),
                SourceUnit("sohu", nope, lambda: False, needs_old=True)]

    def fake_fetch(wind_code, start, end, adjust):
        return _mk_df(pd.bdate_range("2026-10-26", "2026-10-30"),
                      [43.8 + 0.1 * i for i in range(5)], code=wind_code)

    with _patch(app_mod, "build_source_units", fake_units), \
            _patch(app_mod.source, "fetch_daily", fake_fetch):
        stats = d.update_stocks(codes=code, end="20261030", lag_retry_delay=0)
    assert stats.get("updated") == 1, stats
    assert d._ths_enabled is False


def test_full_refetch_prefers_ths_and_replaces_file(tmp_path):
    """修复优先 THS 锚定全量重拉（full=True 整文件替换，断裂尾根被清除）。"""
    d = _mk_dl(tmp_path, start="20260901")
    d._ths_enabled = True
    code = "600900.SH"
    days = list(pd.bdate_range("2026-09-01", "2026-10-30"))
    closes = [100.0 + 0.5 * i for i in range(len(days))]
    broken = list(closes)
    broken[-1] = 28.77  # 本地末根单位断裂
    _write_old(tmp_path, code, days, broken)
    seen = {}

    def fake_ths(code_, start, end, old_, full=False, anchor_before=None):
        seen.update(start=start, end=end, full=full)
        return _mk_df(days, closes, code=code_)  # 同基准干净全史

    def boom_legacy(wind_code, start, end, adjust):
        raise AssertionError("THS 成功后不得再走旧链路全量重拉")

    with _patch(app_mod.vendors, "fetch_ths_hfq", fake_ths), \
            _patch(app_mod.source, "fetch_daily", boom_legacy):
        ok, msg = d._full_refetch_one(code, "20261030")
    assert ok, msg
    assert seen["full"] is True and seen["start"] == "20260901", seen
    after = store.load_daily(store.daily_dir(tmp_path, "hfq") / f"{code}.parquet")
    assert not (after["close"] < 30).any(), "断裂值必须被清除"
    assert pd.Timestamp(after["date"].max()) == pd.Timestamp("2026-10-30")


def test_full_refetch_ths_failure_falls_back_legacy(tmp_path):
    """THS 失败 -> 回退既有 em/tx/bs 全量重拉（合并写）。"""
    d = _mk_dl(tmp_path, start="20260901")
    d._ths_enabled = True
    code = "600900.SH"
    days = list(pd.bdate_range("2026-09-01", "2026-10-30"))
    closes = [100.0 + 0.5 * i for i in range(len(days))]
    _write_old(tmp_path, code, [d_ for d_, c in zip(days, closes)
                                if d_ <= pd.Timestamp("2026-10-22")],
               [c for d_, c in zip(days, closes) if d_ <= pd.Timestamp("2026-10-22")])
    legacy_calls = []

    def fake_ths(code_, start, end, old_, full=False, anchor_before=None):
        raise FetchError("同花顺 403")

    def fake_legacy(wind_code, start, end, adjust):
        legacy_calls.append((start, end))
        return _mk_df(days, closes, code=wind_code)

    with _patch(app_mod.vendors, "fetch_ths_hfq", fake_ths), \
            _patch(app_mod.source, "fetch_daily", fake_legacy):
        ok, msg = d._full_refetch_one(code, "20261030")
    assert ok, msg
    assert legacy_calls and legacy_calls[0] == ("20260901", "20261030"), legacy_calls
    after = store.load_daily(store.daily_dir(tmp_path, "hfq") / f"{code}.parquet")
    assert pd.Timestamp(after["date"].max()) == pd.Timestamp("2026-10-30")


def test_repair_stocks_auto_probes_ths(tmp_path):
    """repair 自动模式探测 ths：健康则 THS 锚定全量修复，成功移出待修清单。"""
    d = _mk_dl(tmp_path, mode="auto", start="20260901")
    code = "600900.SH"
    days = list(pd.bdate_range("2026-09-01", "2026-10-30"))
    closes = [100.0 + 0.5 * i for i in range(len(days))]
    broken = list(closes)
    broken[-1] = 28.77
    _write_old(tmp_path, code, days, broken)
    d._pending_repair_add(code, "疑似复权单位断裂")
    probed = []

    def fake_probe():
        probed.append(1)
        return True

    def fake_ths(code_, start, end, old_, full=False, anchor_before=None):
        return _mk_df(days, closes, code=code_)

    def boom_legacy(wind_code, start, end, adjust):
        raise AssertionError("THS 修复成功后不得走旧链路")

    with _patch(app_mod.vendors, "probe_ths", fake_probe), \
            _patch(app_mod.vendors, "fetch_ths_hfq", fake_ths), \
            _patch(app_mod.source, "fetch_daily", boom_legacy):
        stats = d.repair_stocks(end="20261030")
    assert probed, "auto 模式应探测 ths"
    assert stats.get("repaired") == 1, stats
    assert not d._pending_repair_load(), "修复成功应移出待修清单"
    after = store.load_daily(store.daily_dir(tmp_path, "hfq") / f"{code}.parquet")
    assert not (after["close"] < 30).any()


# ================================================================ G. CLI 接线
def test_cli_flags_parse():
    """--serial / --source-rate: stocks/all/repair 三子命令可用；默认 serial=False。"""
    from downloader.cli import build_parser
    p = build_parser()
    a = p.parse_args(["stocks", "--serial", "--source-rate", "5"])
    assert a.serial is True and a.source_rate == 5.0
    b = p.parse_args(["all", "--serial", "--source-rate", "0"])
    assert b.serial is True and b.source_rate == 0.0
    c = p.parse_args(["repair", "--serial"])
    assert c.serial is True and c.source_rate is None
    d = p.parse_args(["stocks"])
    assert d.serial is False and d.source_rate is None
    try:  # 其他子命令不接新旗标（现状保持）
        p.parse_args(["index", "--serial"])
        raise AssertionError("index 不应接受 --serial")
    except SystemExit:
        pass


def test_cli_dl_mode_and_source_rate(monkeypatch, tmp_path):
    """CLI 默认 auto、--serial 强制 serial；--source-rate 注入 vendors 限速器。"""
    from downloader import cli
    made = {}

    class FakeDL:
        def __init__(self, data_dir, adjust=None, start=None, workers=None,
                     rate=None, rotate=False, mode="serial"):
            made["mode"] = mode

        def update_stocks(self, **kw):
            return {}

    monkeypatch.setattr(cli, "Downloader", FakeDL)
    monkeypatch.setattr(vendors, "configure_source_rate",
                        lambda r: made.__setitem__("rate", r))
    cli.main(["stocks", "--data-dir", str(tmp_path), "--codes", "600519.SH"])
    assert made["mode"] == "auto", made
    assert "rate" not in made, "未传 --source-rate 不得改限速器"
    cli.main(["stocks", "--data-dir", str(tmp_path), "--codes", "600519.SH",
              "--serial", "--source-rate", "5"])
    assert made["mode"] == "serial" and made["rate"] == 5.0, made


def test_cli_exit2_when_all_sources_unavailable(monkeypatch, tmp_path):
    """全部数据源不可用 -> CLI 打印并以 exit code 2 退出（不得静默空跑）。"""
    from downloader import cli
    from downloader.scheduler import AllSourcesUnavailableError

    class FakeDL:
        def __init__(self, *a, **k):
            pass

        def update_stocks(self, **kw):
            raise AllSourcesUnavailableError("em/tx/ths/sina/sohu 探测全部失败")

        def repair_stocks(self, **kw):
            raise AllSourcesUnavailableError("em/tx/ths/sina/sohu 探测全部失败")

    monkeypatch.setattr(cli, "Downloader", FakeDL)
    for argv in (["stocks", "--data-dir", str(tmp_path), "--codes", "600519.SH"],
                 ["repair", "--data-dir", str(tmp_path)]):
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                cli.main(argv)
            raise AssertionError(f"{argv[0]} 应非零退出")
        except SystemExit as e:
            assert e.code == 2, (argv, e.code)
        assert "不可用" in buf.getvalue(), buf.getvalue()


# ================================================================ H. 接缝验收
def test_seam_dividend_trips_guard_then_ths_anchored_refetch(tmp_path):
    """验收：raw 续接窗口恰逢除权 -> 接缝大跳变被守卫拦截 -> 升级 THS 锚定
    全量重拉（整文件替换写）-> 状态 repaired，本地不残留接缝价。"""
    d = _mk_dl(tmp_path, mode="auto", start="20260901")
    code = "688001.SH"
    days = list(pd.bdate_range("2026-09-01", "2026-10-23"))  # 存量止于 10-23
    closes = [40.0 + 0.1 * i for i in range(len(days))]
    _write_old(tmp_path, code, days, closes)
    old_last = closes[-1]
    calls: list = []

    def ths_incr_fail(code, start, end, old_, full=False):
        calls.append(("ths_incr", code))
        raise FetchError("ths 增量锚定失败")     # 迫使重派到 raw 源制造接缝

    def seam_raw(code, start, end, old_, full=False):
        calls.append(("sina", code))
        # 10-26 除权：raw 价从 ~44 跳到 28（-36%）——若混入 hfq 即单位断裂
        return _mk_df(pd.bdate_range("2026-10-26", "2026-10-30"),
                      [28.0, 28.2, 27.6, 28.4, 28.1], code=code)

    def fake_units(adjust="hfq"):
        return [SourceUnit("em", seam_raw, lambda: False),      # 探测挂
                SourceUnit("tx", seam_raw, lambda: False),      # 探测挂
                SourceUnit("ths", ths_incr_fail, lambda: True, needs_old=True),
                SourceUnit("sina", seam_raw, lambda: True, needs_old=True),
                SourceUnit("sohu", seam_raw, lambda: True, needs_old=True)]

    full_days = list(pd.bdate_range("2026-09-01", "2026-10-30"))
    full_closes = ([40.0 + 0.1 * i for i in range(len(days))]
                   + [old_last + 0.1, old_last + 0.2, old_last - 0.3,
                      old_last + 0.4, old_last + 0.1])           # 连续 hfq 全史

    def fake_ths_full(code_, start, end, old_, full=False, anchor_before=None):
        assert full is True
        return _mk_df(full_days, full_closes, code=code_)

    def boom_legacy(wind_code, start, end, adjust):
        raise AssertionError("THS 锚定全量成功后不得走旧链路")

    with _patch(app_mod, "build_source_units", fake_units), \
            _patch(app_mod.vendors, "fetch_ths_hfq", fake_ths_full), \
            _patch(app_mod.source, "fetch_daily", boom_legacy):
        stats = d.update_stocks(codes=code, end="20261030", lag_retry_delay=0)
    assert stats.get("repaired") == 1, stats
    assert ("ths_incr", code) in calls and ("sina", code) in calls, calls
    after = store.load_daily(store.daily_dir(tmp_path, "hfq") / f"{code}.parquet")
    # 接缝价（28.x）绝不能落盘；文件被 THS 干净全史替换
    assert not (after["close"] < 35).any(), after["close"].tolist()
    assert pd.Timestamp(after["date"].max()) == pd.Timestamp("2026-10-30")
    assert abs(after["close"].iloc[-1] - (old_last + 0.1)) < 1e-9
    assert not d._pending_repair_load(), "自愈成功应移出待修清单"


# ============================================================ I. 2026-11-01 四补丁回归
# （pct_chg 合成 / anchor_before 局部锚定 / THS 限速默认 / pending_repair 自清）

def test_break_date_from_reason():
    """守卫原因串 -> 断裂日（修复路径的局部锚定起点）。"""
    r = "疑似复权单位断裂: 2026-10-28 单日|涨跌|32.1% (阈值 32%)"
    assert _break_date_from_reason(r) == "20261028"
    assert _break_date_from_reason("修复失败: timeout") is None
    assert _break_date_from_reason("") is None


def test_ths_hfq_synthesizes_pct_chg(monkeypatch):
    """THS bar 无 pct_chg/amplitude -> fetch 出口必须合成（NaN 会穿透 AI 分析）。"""
    dates = list(pd.bdate_range("2026-09-01", "2026-10-23"))
    qfq = _mk_df(dates, [40.0 + 0.12 * i for i in range(len(dates))])
    qfq["pct_chg"] = float("nan"); qfq["amplitude"] = float("nan"); qfq["change"] = float("nan")
    monkeypatch.setattr(vendors, "fetch_ths_recent_qfq", lambda wc: qfq.copy())
    old = _mk_df(dates, [c * 1.5 for c in qfq["close"]])
    # 窗口取后半段：帧内保留的前一根恰是窗口首根的前收 -> 窗口内全有 pct_chg
    out = vendors.fetch_ths_hfq("688001.SH", "20261009", "20261023", old)
    assert out is not None and len(out) > 1
    assert not out["pct_chg"].isna().any(), out["pct_chg"].tolist()
    assert not out["amplitude"].isna().any() and not out["change"].isna().any()
    want = (out["close"].iloc[-1] / out["close"].iloc[-2] - 1) * 100
    assert abs(out["pct_chg"].iloc[-1] - want) < 1e-9


def test_raw_continuation_synthesizes_pct_chg(monkeypatch):
    """新浪 raw 续拉：比值缩放后从锚定日 bar 链式合成 -> 接缝 pct 连续。"""
    days = list(pd.bdate_range("2026-10-20", "2026-10-23"))  # 4 天
    old = _mk_df(days[:2], [10.0, 10.0])
    raw = _mk_df(days, [5.0, 5.0, 5.2, 5.3])  # raw 基准，锚定日=10-21 close 5.0
    monkeypatch.setattr(vendors, "fetch_sina_raw", lambda wc, s, e: raw.copy())
    out = vendors.fetch_sina_hfq_cont("600000.SH", "20261022", "20261023", old)
    assert list(out["date"].dt.strftime("%Y%m%d")) == ["20261022", "20261023"]
    # ratio = 10.0/5.0 = 2 -> 10-22 close=10.4, pct_chg=(10.4-10)/10*100=4%
    assert abs(out["close"].iloc[0] - 10.4) < 1e-9
    assert abs(out["pct_chg"].iloc[0] - 4.0) < 1e-6, out["pct_chg"].tolist()
    assert abs(out["change"].iloc[0] - 0.4) < 1e-9


def test_full_refetch_from_date_keeps_pre_break_history(tmp_path, monkeypatch):
    """P3 修复路径：from_date 起局部替换，拉取窗口与锚定都只认断裂前好 bar。"""
    code = "300001.SZ"
    days = list(pd.bdate_range("2026-09-01", "2026-10-23"))
    closes = [10.0 + 0.05 * i for i in range(len(days))]
    _write_old(tmp_path, code, days, closes)
    d = _mk_dl(tmp_path, start="20200101")
    d._ths_enabled = True
    seen = {}

    def fake_ths(code_, start, end, old_, full=False, anchor_before=None):
        seen.update(start=start, anchor_before=anchor_before, full=full)
        idx = days.index(pd.Timestamp("2026-10-20"))
        return _mk_df(days[idx:], [c + 1.0 for c in closes[idx:]], code=code_)  # 局部窗口

    monkeypatch.setattr(app_mod.vendors, "fetch_ths_hfq", fake_ths)
    ok, msg = d._full_refetch_one(code, "20261030", from_date="20261020")
    assert ok, msg
    # app 层把断裂日透传为拉取起点与 anchor_before；「再前移一根避开脏 bar」
    # 的窗口逻辑在 vendors.fetch_ths_hfq 内部（见 test_ths_hfq_drops_dirty_prebreak_bar）
    assert seen["start"] == "20261020" and seen["anchor_before"] == "20261020"
    assert seen["full"] is True
    after = store.load_daily(store.daily_dir(tmp_path, "hfq") / f"{code}.parquet")
    pre = after[after["date"] < pd.Timestamp("2026-10-20")]
    assert len(pre) == days.index(pd.Timestamp("2026-10-20"))
    assert abs(pre["close"].iloc[-1] - closes[len(pre) - 1]) < 1e-9  # 历史原值保留
    assert pd.Timestamp(after["date"].max()) == pd.Timestamp("2026-10-23")


def test_incremental_success_clears_pending_repair(tmp_path, monkeypatch):
    """P4：增量成功过守卫 -> 自动移出待修清单（不再日日全量重拉）。"""
    code = "300002.SZ"
    days = list(pd.bdate_range("2026-09-01", "2026-10-20"))
    _write_old(tmp_path, code, days, [10.0] * len(days))
    d = _mk_dl(tmp_path, start="20200101")
    d._pending_repair_add(code, "疑似复权单位断裂: 2026-10-15 单日|涨跌|33.0%")
    new_days = list(pd.bdate_range("2026-10-21", "2026-10-23"))
    monkeypatch.setattr(app_mod.source, "fetch_daily",
                        lambda wind_code, start, end, adjust:
                        _mk_df(new_days, [10.1, 10.2, 10.3], code=code))
    res = d._update_stock_one(code, "测试", "20261023", full=False)
    assert res[1] == "updated", res
    assert code not in d._pending_repair_load(), "增量过守卫应移出待修清单"
    # 二次调用（清单已空）零开销路径
    res2 = d._update_stock_one(code, "测试", "20261030", full=False)
    assert res2[1] in ("updated", "skipped", "no_progress", "uptodate"), res2


def test_ths_default_rate_slower():
    """THS 实测须 ≥1.5s 间隔 -> 默认限速单独收紧（0.66 req/s）。"""
    assert vendors.DEFAULT_SOURCE_RATE == 2.0
    assert vendors.THS_SOURCE_RATE <= 2.0 / 3 + 1e-9
    assert vendors.ths_limiter._min_interval >= 1.5 - 1e-9
    # 显式 --source-rate 时三源一起重设（用户选择优先），ths 可单独覆盖
    vendors.configure_source_rate(4.0)
    try:
        assert vendors.ths_limiter.rate == 4.0 and vendors.sina_limiter.rate == 4.0
        vendors.configure_source_rate(4.0, ths_rate=1.0)
        assert vendors.ths_limiter.rate == 1.0 and vendors.sina_limiter.rate == 4.0
    finally:
        vendors.configure_source_rate(vendors.DEFAULT_SOURCE_RATE, vendors.THS_SOURCE_RATE)


# ============================================================ J. 守卫历史真实跳变豁免
# （2026-11-01：000525 的 1994-08-01 +45% 是涨跌停制度前真实行情，被 32% 守卫误拦
#   导致修复永久失败；豁免规则=旧序列同日也有 >limit 跳变（两源一致=历史事实））

def test_reject_unit_anomaly_exempts_historical_real_jump():
    """两源在同一天有一致的大跳变（1996 涨跌停制度前/股改复牌）-> 放行。"""
    days = list(pd.bdate_range("1994-07-01", "1994-08-31"))
    closes = [2.0] * len(days)
    closes[days.index(pd.Timestamp("1994-08-01"))] = 3.0  # +50% 历史真实
    old = _mk_df(days, closes)
    new = _mk_df(days, closes)  # 重拉回同样跳变（两源一致）
    assert app_mod._reject_unit_anomaly(old, new) == ""


def test_reject_unit_anomaly_still_blocks_new_jump():
    """new 引入、old 没有的跳变（腾讯 raw 混入/断裂接缝）-> 照常拦截。"""
    days = list(pd.bdate_range("2026-09-01", "2026-10-30"))
    closes = [10.0] * len(days)
    old = _mk_df(days, closes)
    new_closes = list(closes)
    new_closes[-1] = 15.0  # +50% 假跳变
    new = _mk_df(days, new_closes)
    r = app_mod._reject_unit_anomaly(old, new)
    assert "疑似复权单位断裂" in r and "2026-10-30" in r


def test_ths_hfq_drops_dirty_prebreak_bar(monkeypatch):
    """000525 实测场景：断口前收 bar 脏（腾讯除权日回 raw 价 -26%，压在守卫线下）。

    锚定窗口须跳过断口前一根（否则单根脏 bar 把 CV 撑爆 5.8% 拒锚），
    替换窗口从脏 bar 起把它一并换掉。
    """
    days = list(pd.bdate_range("2026-09-01", "2026-10-30"))
    n = len(days)
    ths_closes = [40.0 + 0.1 * i for i in range(n)]
    r_base = 0.98  # 存量/THS 基准比值
    di = days.index(pd.Timestamp("2026-10-27"))
    old_closes = [c * r_base for c in ths_closes]
    old_closes[di] *= 0.736  # 腾讯 raw 混入: -26.4%（<32% 逃过守卫写入存量）
    old = _mk_df(days[:di + 1], old_closes[:di + 1])  # 存量停在脏 bar 10-27
    qfq = _mk_df(days, ths_closes)  # THS 全史（正确基准）
    monkeypatch.setattr(vendors, "fetch_ths_qfq_full", lambda wc, s, e: qfq.copy())
    out = vendors.fetch_ths_hfq("000525.SZ", "20261028", "20261030", old,
                                full=True, anchor_before="20261028")
    # 锚定末根 = 断裂前 2 根（date<10-28 末根 10-27 去尾 = 10-26），
    # 替换窗口从 10-27（脏 bar）起把它一并换掉
    assert out["date"].min() == pd.Timestamp("2026-10-27")
    row = out[out["date"] == pd.Timestamp("2026-10-27")]
    assert abs(row["close"].iloc[0] - ths_closes[di] * r_base) < 1e-6  # 回到基准
    # 修复帧自身接缝平滑（10-26 前收 -> 10-27 新值 <1%）
    ret = out["close"].pct_change().abs()
    assert ret.max() < 0.01, ret.tolist()
