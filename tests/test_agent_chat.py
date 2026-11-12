"""问股 Agent 单测：工具面 / 意图层 / ReAct 循环 / 会话持久化。

不触网：LLM 用 monkeypatch 假替身（脚本化 tool_calls→文本两轮），
store 用 FakeStore（内存 kline），策略目录指向真实 strategies/（15 yaml）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyzer.agent import tools as agent_tools  # noqa: E402
from analyzer.agent import chat as agent_chat  # noqa: E402
from analyzer.agent.chat import (run_chat_turn, resolve_intent,  # noqa: E402
                                 detect_codes, load_strategies, strategy_section)
from analyzer.agent.llm import AIConfig  # noqa: E402


# ---------------------------------------------------------------- FakeStore

def _series(n=80, base=10.0):
    """波动序列（有涨有跌，RSI 可算）。"""
    import math
    return [round(base + math.sin(i / 5) * 2.0 - 0.02 * i, 3) for i in range(n)]


class FakeStore:
    root = "/tmp/fake-agent-root"

    def __init__(self):
        c = _series()
        self._k = {
            "code": "300687.SZ", "name": "中际旭创",
            "dates": [f"2026-08-{i:02d}" if i <= 31 else f"2026-09-{i-31:02d}"
                      for i in range(1, 81)],
            "open": c, "high": [x + 0.2 for x in c], "low": [x - 0.2 for x in c],
            "close": c, "volume": [1_000_000 + i * 1000 for i in range(80)],
            "amount": [1e8 + i * 1e6 for i in range(80)],
            "pct_chg": [0.01] * 80, "turnover": [1.5] * 80,
            "total_mv": 100e8, "float_mv": 80e8,
            "real_price": c[-1], "hfq_close": c, "price_unit": "CNY",
        }
        self._basic_names = {"300687.SZ": "中际旭创", "600519.SH": "贵州茅台"}

    def kline(self, code, **kw):
        return dict(self._k) if code == "300687.SZ" else None

    def stock_info(self, code):
        return {"week52_high": 20.0, "week52_low": 8.0,
                "ytd_chg": 0.35, "y1y_chg": 0.6} if code == "300687.SZ" else {}

    def real_price(self, code):
        return self._k["real_price"] if code == "300687.SZ" else None

    def fund_latest(self, code):
        return {"period": "2026Q3", "rev": 100e8, "rev_yoy": 0.3,
                "net_profit": 10e8, "net_profit_yoy": 0.4, "eps": 1.2,
                "roe": 0.15, "gross_margin": 0.4, "net_margin": 0.2,
                "debt_ratio": 0.3} if code == "300687.SZ" else None

    def valuation(self, code):
        return {"pe_ttm": 30.0, "pe_static": 32.0, "pb": 5.0,
                "date": "2026-11-11"} if code == "300687.SZ" else None

    def chips(self, code):
        return {"code": code, "name": "中际旭创", "stats": {
            "profit_ratio": 0.7, "avg_cost": 12.0,
            "cost_90_low": 10.0, "cost_90_high": 14.0, "concentration_90": 0.2}}

    def news(self, code):
        return {"items": [{"title": "Q3 业绩超预期", "source": "公告",
                           "date": "2026-11-01", "content": "净利润同比+40%"}],
                "stale": False} if code == "300687.SZ" else None

    def positions(self, user=None):
        return {"300687.SZ": {"held": 1500, "avg_cost": 25.237,
                              "realized": 16773.0, "unrealized": 2494.0,
                              "n_trades": 58}}

    def board_rank(self, start, end, **kw):
        rows = [
            {"board_code": "B1", "board_name": "元件", "ret": 0.0503,
             "n_stocks": 6, "vol": 0.02, "maxdd": 0.05, "turnover": 0.03,
             "adv": 4, "dec": 2, "total_mv": 1e9},
            {"board_code": "B2", "board_name": "白酒", "ret": -0.012,
             "n_stocks": 8, "vol": 0.01, "maxdd": 0.03, "turnover": 0.02,
             "adv": 2, "dec": 6, "total_mv": 2e9}]
        if kw.get("ascending"):
            rows = list(reversed(rows))
        return {"n_boards": 2, "rows": rows}

    @property
    def market_series(self):
        return {"dates": ["2026-11-10", "2026-11-11"], "adv": [3000, 3200],
                "dec": [2000, 1800], "amount_yi": [20000, 21000],
                "indexes": {"sh000001": {"dates": ["2026-11-11"],
                                          "close": [3500.0]}}}

    @property
    def watchlist_codes_all(self):
        return ["300687.SZ", "600519.SH"]

    @property
    def basic(self):
        import pandas as pd
        df = pd.DataFrame({"code": list(self._basic_names),
                           "name": list(self._basic_names.values())})
        return df.set_index("code")


# ---------------------------------------------------------------- tools

def test_norm_code_variants():
    assert agent_tools._norm_code("600519") == "600519.SH"
    assert agent_tools._norm_code("000048") == "000048.SZ"
    assert agent_tools._norm_code("830799") == "830799.BJ"
    assert agent_tools._norm_code("300687.sz") == "300687.SZ"
    assert agent_tools._norm_code("600519.SH") == "600519.SH"


def test_realtime_quote_shape():
    r = agent_tools.tool_realtime_quote(FakeStore(), "300687")
    assert r["stock_code"] == "300687.SZ"
    assert r["name"] == "中际旭创"
    assert r["price"] == pytest.approx(_series()[-1])
    assert r["week52_high"] == 20.0


def test_daily_history_tail_clip():
    r = agent_tools.tool_daily_history(FakeStore(), "300687.SZ", days=10)
    assert r["count"] == 10 and len(r["bars"]) == 10
    assert set(r["bars"][0]) >= {"date", "open", "close", "pct_chg"}


def test_trend_metrics():
    r = agent_tools.tool_trend(FakeStore(), "300687.SZ")
    assert r["trend_status"] in ("上升趋势", "下降趋势", "震荡")
    assert r["ma_alignment"] in ("多头", "空头", "交织")
    assert r["macd_state"] and r["rsi6"] is not None
    assert r["support"] < r["resistance"]
    assert r["volume_ratio_5d"] is not None


def test_chip_and_info_and_news():
    s = FakeStore()
    assert agent_tools.tool_chip_distribution(s, "300687")["profit_ratio"] == 0.7
    info = agent_tools.tool_stock_info(s, "300687.SZ")
    assert info["fundamentals"]["roe"] == 0.15
    assert info["valuation"]["pe_ttm"] == 30.0
    news = agent_tools.tool_news(s, "300687")
    assert news["count"] == 1 and news["items"][0]["title"]


def test_sector_and_market_and_positions():
    s = FakeStore()
    sec = agent_tools.tool_sector_rankings(s)
    assert sec["top"][0]["board"] == "元件"
    assert sec["bottom"][0]["ret_pct"] == pytest.approx(-1.2)
    mkt = agent_tools.tool_market_indices(s)
    assert mkt["adv"][-1] == 3200 and "sh000001" in mkt["indexes"]
    pos = agent_tools.tool_positions(s)
    assert pos["count"] == 1 and pos["positions"][0]["shares"] == 1500


def test_unknown_tool_and_bad_args():
    s = FakeStore()
    r = json.loads(agent_tools.execute_tool(s, "no_such_tool", "{}"))
    assert "error" in r
    r = json.loads(agent_tools.execute_tool(s, "get_realtime_quote", "not json"))
    assert "error" in r
    # kline 缺数据：错误 JSON 而非异常
    r = json.loads(agent_tools.execute_tool(s, "get_realtime_quote",
                                            '{"stock_code": "999999"}'))
    assert "error" in r


def test_capital_flow_no_cache(tmp_path, monkeypatch):
    # 只读缓存路径不存在 → 如实报无数据（不抓网）
    class Store:
        root = str(tmp_path)
    r = agent_tools.tool_capital_flow(Store(), "300687.SZ")
    assert "error" in r


# ---------------------------------------------------------------- 意图层

def test_detect_codes_regex_and_name():
    s = FakeStore()
    assert detect_codes("600519 现在怎么样", s) == ["600519.SH"]
    assert detect_codes("中际旭创能买吗", s) == ["300687.SZ"]
    assert detect_codes("随便聊聊", s) == []


def test_resolve_intent_modes():
    s = FakeStore()
    sess = {"active_code": "300687.SZ"}
    assert resolve_intent("它的技术面如何", sess, s)["mode"] == "maintain"
    r = resolve_intent("换成 600519 看看", sess, s)
    assert r["mode"] == "switch" and r["active_code"] == "600519.SH"
    r = resolve_intent("对比 300687 和 600519 哪个好", sess, s)
    assert r["mode"] == "compare" and len(r["codes"]) == 2
    assert resolve_intent("你好", {}, s)["mode"] == "new"


# ---------------------------------------------------------------- 策略

def test_load_strategies_15():
    st = load_strategies()
    assert len(st) >= 15
    for name, s in st.items():
        assert s.get("instructions"), f"{name} 缺 instructions"
    assert "chan_theory" in st and "ma_golden_cross" in st


def test_strategy_section_injection():
    base = strategy_section(None)
    assert "默认交易纪律基线" in base
    with_chan = strategy_section("chan_theory")
    assert "缠论" in with_chan and len(with_chan) > len(base)


# ---------------------------------------------------------------- ReAct

def _fake_llm_two_turn(monkeypatch, captured):
    """第一轮回 tool_call（取实时行情），第二轮回文本。"""
    def fake(config, messages, tools=None, tool_choice=None):
        captured.append([m["role"] for m in messages])
        if not any(m["role"] == "tool" for m in messages):
            return {"content": "", "tool_calls": [{
                "id": "c1", "name": "get_realtime_quote",
                "arguments": '{"stock_code": "300687"}'}],
                "usage": {"total_tokens": 10}}
        # 工具结果已回填 → 检查 tool 消息内容后作答
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        captured.append(tool_msgs[0]["content"][:80])
        return {"content": f"中际旭创现价 {json.loads(tool_msgs[0]['content'])['price']}，趋势稳定。",
                "tool_calls": [], "usage": {"total_tokens": 10}}
    monkeypatch.setattr(agent_chat, "chat_completion", fake)


def test_run_chat_turn_react_loop(tmp_path, monkeypatch):
    captured: list = []
    _fake_llm_two_turn(monkeypatch, captured)
    store = FakeStore()
    store.root = str(tmp_path)
    cfg = AIConfig(api_key="k", base_url="http://x", model="m")
    out = run_chat_turn(store, "u1", "sess1", "300687 现在能买吗",
                        config=cfg)
    assert out["steps"] == 1
    assert "现价" in out["reply"]
    assert out["active_code"] == "300687.SZ"
    assert out["mode"] == "new"
    # 会话落盘：两条消息（user+assistant），active_code 持久化
    f = tmp_path / "agent_chat" / "u1" / "sess1.json"
    data = json.loads(f.read_text())
    assert len(data["messages"]) == 2
    assert data["active_code"] == "300687.SZ"
    assert data["messages"][1]["steps"] == 1
    # 第二轮：active_code 继承（maintain），历史带前两轮
    out2 = run_chat_turn(store, "u1", "sess1", "它的筹码分布呢",
                         config=cfg)
    assert out2["mode"] == "maintain"
    assert out2["active_code"] == "300687.SZ"


def test_run_chat_turn_strategy_persist(tmp_path, monkeypatch):
    seen_system: list = []

    def fake(config, messages, tools=None, tool_choice=None):
        seen_system.append(messages[0]["content"])
        return {"content": "按缠论框架，当前处于中枢震荡。", "tool_calls": [], "usage": {}}
    monkeypatch.setattr(agent_chat, "chat_completion", fake)
    store = FakeStore()
    store.root = str(tmp_path)
    cfg = AIConfig(api_key="k", base_url="http://x", model="m")
    out = run_chat_turn(store, "u2", "s2", "300687 怎么看",
                        strategy="chan_theory", config=cfg)
    assert "缠论" in seen_system[0]
    data = json.loads((tmp_path / "agent_chat" / "u2" / "s2.json").read_text())
    assert data["strategy"] == "chan_theory"
    # 不传 strategy → 继承会话策略
    run_chat_turn(store, "u2", "s2", "再看看", config=cfg)
    assert "缠论" in seen_system[-1]


def test_run_chat_turn_budget_guard(tmp_path, monkeypatch):
    """首轮回 tool_call 但剩余预算不足 → 注入直接作答提示（不再带 tools）。"""
    calls: list = []

    def fake(config, messages, tools=None, tool_choice=None):
        calls.append(bool(tools))
        if tools:
            return {"content": "", "tool_calls": [{
                "id": "c1", "name": "get_daily_history",
                "arguments": '{"stock_code": "300687"}'}], "usage": {}}
        return {"content": "预算内直接作答。", "tool_calls": [], "usage": {}}
    monkeypatch.setattr(agent_chat, "chat_completion", fake)
    store = FakeStore()
    store.root = str(tmp_path)
    cfg = AIConfig(api_key="k", base_url="http://x", model="m")
    # 把预算耗到 MIN 之下：伪造已流逝时间
    import analyzer.agent.chat as C
    real_time = C.time.time
    monkeypatch.setattr(C.time, "time", lambda: real_time() + C.CHAT_TIMEOUT_S)
    out = run_chat_turn(store, "u3", "s3", "300687", config=cfg)
    # 首轮即超时：直接收口作答
    assert out["reply"]
    assert calls[-1] is False or out["steps"] == 0


def test_run_chat_turn_max_steps(tmp_path, monkeypatch):
    """LLM 每轮都要求工具 → max_steps 打满后强制收口。"""

    def fake(config, messages, tools=None, tool_choice=None):
        if tools:
            return {"content": "", "tool_calls": [{
                "id": f"c{len(messages)}", "name": "get_market_indices",
                "arguments": "{}"}], "usage": {}}
        return {"content": "已按上限收口作答。", "tool_calls": [], "usage": {}}
    monkeypatch.setattr(agent_chat, "chat_completion", fake)
    store = FakeStore()
    store.root = str(tmp_path)
    cfg = AIConfig(api_key="k", base_url="http://x", model="m")
    out = run_chat_turn(store, "u4", "s4", "看看大盘", config=cfg)
    assert out["steps"] == agent_chat.MAX_STEPS
    assert out["reply"] == "已按上限收口作答。"
