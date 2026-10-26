"""数据面模块（flow/announce/sentiment）自测：规范化、缓存、降级。

全部离线（mock akshare 函数），不触网。

运行: .venv/bin/python tests/test_market_data.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import traceback
from pathlib import Path
from unittest import mock

sys.path.insert(0, ".")

from analyzer import announce, flow, sentiment  # noqa: E402

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


# ------------------------------------------------------------------ 替身数据


class _Row(dict):
    """支持 row.get('col') 与 row['col'] 的行（仿 pandas Series）。"""

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)


class _FakeDF:
    """仿 pandas DataFrame：iterrows / df[col] 布尔过滤 / .iloc[0] / .empty。"""

    def __init__(self, rows):
        self._rows = rows

    def iterrows(self):
        for i, r in enumerate(self._rows):
            yield i, _Row(r)

    def __getitem__(self, key):
        if isinstance(key, _FakeSeries):
            kept = [r for r, m in zip(self._rows, key._mask) if m]
            return _FakeDF(kept)
        return _FakeSeries([r.get(key) for r in self._rows])

    @property
    def empty(self):
        return not self._rows

    @property
    def iloc(self):
        return _FakeIloc(self._rows)


class _FakeSeries(list):
    def __init__(self, vals, mask=None):
        super().__init__(vals)
        self._mask = mask if mask is not None else list(vals)

    def astype(self, _t):
        return self

    def __eq__(self, other):
        return _FakeSeries(list(self), [v == other for v in self])


class _FakeIloc:
    def __init__(self, rows):
        self._rows = rows

    def __getitem__(self, i):
        return _Row(self._rows[i])


def _flow_rows():
    return [
        {"日期": dt.date(2026, 10, 24), "收盘价": 28.0, "涨跌幅": 1.5,
         "主力净流入-净额": 5e7, "主力净流入-净占比": 8.5,
         "超大单净流入-净额": 3e7, "超大单净流入-净占比": 5.0,
         "大单净流入-净额": 2e7, "大单净流入-净占比": 3.5,
         "中单净流入-净额": -3e7, "中单净流入-净占比": -5.0,
         "小单净流入-净额": -2e7, "小单净流入-净占比": -3.5},
        {"日期": dt.date(2026, 10, 25), "收盘价": 28.5, "涨跌幅": 1.8,
         "主力净流入-净额": -1e7, "主力净流入-净占比": -2.0,
         "超大单净流入-净额": "-", "超大单净流入-净占比": "-",
         "大单净流入-净额": "-", "大单净流入-净占比": "-",
         "中单净流入-净额": "-", "中单净流入-净占比": "-",
         "小单净流入-净额": "-", "小单净流入-净占比": "-"},
    ]


def _ann_rows():
    return [
        {"代码": "300687", "简称": "赛意信息", "公告标题": "2026 年三季报",
         "公告时间": "2026-10-23", "公告链接": "http://cninfo/x1"},
        {"代码": "300687", "简称": "赛意信息", "公告标题": "",
         "公告时间": "2026-10-22", "公告链接": "http://cninfo/x2"},  # 空标题应跳过
    ]


def _sent_hist_rows():
    return [
        {"交易日": dt.date(2026, 10, 23), "评分": 80.5},
        {"交易日": dt.date(2026, 10, 23), "评分": 81.0},  # 同日重复取后值
        {"交易日": dt.date(2026, 10, 22), "评分": 78.2},
    ]


def _sent_snapshot_rows():
    return [
        _Row({"代码": "300687", "名称": "赛意信息", "最新价": 28.7, "涨跌幅": 3.0,
              "换手率": 8.9, "市盈率": 45.0, "主力成本": 27.5,
              "机构参与度": 0.32, "综合得分": 75.5, "上升": 236.0,
              "目前排名": 169.0, "关注指数": 80.0, "交易日": dt.date(2026, 10, 26)}),
    ]


# ------------------------------------------------------------------ 用例


def t_flow_norm_and_cache():
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch("akshare.stock_individual_fund_flow",
                        return_value=_FakeDF(_flow_rows())) as fak:
            d = flow.fetch("300687.SZ", tmp)
            assert d["stale"] is False and len(d["items"]) == 2
            it = d["items"][-1]
            assert it["day"] == "2026-10-25" and it["main_net"] == -1e7
            assert it["super_net"] is None  # '-' -> None
            assert d["items"][0]["super_net"] == 3e7
            # TTL 内零请求（缓存命中）
            d2 = flow.fetch("300687.SZ", tmp)
            assert d2["stale"] is False and fak.call_count == 1
            # refresh 强制重抓
            flow.fetch("300687.SZ", tmp, refresh=True)
            assert fak.call_count == 2
        # latest_metrics 汇总
        m = flow.latest_metrics("300687.SZ", tmp, days=5)
        assert m["available"] and m["n_days"] == 2
        assert m["main_net_sum"] == 4e7
        assert m["main_net_days_inflow"] == 1 and m["main_net_days_outflow"] == 1
        assert m["last"]["day"] == "2026-10-25"


def t_flow_code_norm():
    assert flow._norm_code("600519.SH") == ("600519", "sh")
    assert flow._norm_code("000001.SZ") == ("000001", "sz")
    assert flow._norm_code("830799.BJ") == ("830799", "bj")
    assert flow._norm_code("600519") == ("600519", "sh")
    assert flow._norm_code("300687") == ("300687", "sz")
    assert flow._norm_code("sh600519") == ("600519", "sh")
    for bad in ("", "12345", "60051", "xx60051", "600519.XX"):
        try:
            flow._norm_code(bad)
            raise AssertionError(f"{bad!r} 应当报错")
        except ValueError:
            pass


def t_flow_stale_fallback():
    """网络失败回退旧缓存（stale=True）；无缓存则抛错。"""
    with tempfile.TemporaryDirectory() as tmp:
        # 先写一份有效缓存
        with mock.patch("akshare.stock_individual_fund_flow",
                        return_value=_FakeDF(_flow_rows())):
            flow.fetch("300687.SZ", tmp)
        # 再模拟断网
        with mock.patch("akshare.stock_individual_fund_flow",
                        side_effect=RuntimeError("boom")):
            d = flow.fetch("300687.SZ", tmp, refresh=True)
            assert d["stale"] is True and d["items"]
            assert "boom" in d["error"]
        # 无缓存 + 断网 -> 抛错
        with tempfile.TemporaryDirectory() as tmp2:
            with mock.patch("akshare.stock_individual_fund_flow",
                            side_effect=RuntimeError("boom")):
                try:
                    flow.fetch("300687.SZ", tmp2, refresh=True)
                    raise AssertionError("应当抛 RuntimeError")
                except RuntimeError as e:
                    assert "资金流抓取失败" in str(e)


def t_announce():
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch("akshare.stock_zh_a_disclosure_report_cninfo",
                        return_value=_FakeDF(_ann_rows())) as fak:
            d = announce.fetch("300687.SZ", tmp)
            assert len(d["items"]) == 1  # 空标题被过滤
            assert d["items"][0]["title"] == "2026 年三季报"
            # 排序：新的在前（10-23 在前）
            announce.fetch("300687.SZ", tmp, refresh=True)
            assert fak.call_count == 2
        t = announce.latest_titles("300687.SZ", tmp)
        assert t["available"] and t["n_total"] == 1
        assert t["items"][0]["day"] == "2026-10-23"
        assert "url" not in t["items"][0]  # AI 上下文不送 url


def t_sentiment():
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch("akshare.stock_comment_detail_zhpj_lspf_em",
                        return_value=_FakeDF(_sent_hist_rows())), \
             mock.patch("akshare.stock_comment_em",
                        return_value=_FakeDF(_sent_snapshot_rows())):
            d = sentiment.fetch("300687.SZ", tmp)
            # 同日去重取后值
            assert len(d["history"]) == 2
            assert d["history"][-1] == {"day": "2026-10-23", "score": 81.0}
            assert d["snapshot"]["score"] == 75.5
            assert d["snapshot"]["attention"] == 80.0
            assert d["snapshot"]["rank"] == 169.0
        m = sentiment.latest_metrics("300687.SZ", tmp, days=10)
        assert m["available"] and m["n_days"] == 2
        assert m["last_score"] == 81.0
        assert m["score_mean"] == round((78.2 + 81.0) / 2, 2)


def t_sentiment_partial_failure():
    """快照失败但历史成功 -> 整体可用（parts 记录失败原因）。"""
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch("akshare.stock_comment_detail_zhpj_lspf_em",
                        return_value=_FakeDF(_sent_hist_rows())), \
             mock.patch("akshare.stock_comment_em",
                        side_effect=RuntimeError("snapshot boom")):
            d = sentiment.fetch("300687.SZ", tmp)
            assert d["history"] and d["snapshot"] is None
            assert "snapshot boom" in d.get("parts", {}).get("snapshot", "")
        # 两块都失败且无缓存 -> 抛错
        with tempfile.TemporaryDirectory() as tmp2:
            with mock.patch("akshare.stock_comment_detail_zhpj_lspf_em",
                            side_effect=RuntimeError("h boom")), \
                 mock.patch("akshare.stock_comment_em",
                            side_effect=RuntimeError("s boom")):
                try:
                    sentiment.fetch("300687.SZ", tmp2)
                    raise AssertionError("应当抛 RuntimeError")
                except RuntimeError as e:
                    assert "情绪数据抓取失败" in str(e)


def main() -> int:
    check("资金流：规范化与缓存", t_flow_norm_and_cache)
    check("资金流：代码归一", t_flow_code_norm)
    check("资金流：断网回退旧缓存", t_flow_stale_fallback)
    check("公告：过滤/排序/AI 摘要", t_announce)
    check("情绪：历史去重与快照", t_sentiment)
    check("情绪：部分失败降级", t_sentiment_partial_failure)
    print(f"\n===== 汇总: {PASSED} PASS / {FAILED} FAIL =====")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
