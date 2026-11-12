"""问股 Agent 工具面：DataStore 薄封装，OpenAI function-calling 声明。

工具名与输出键名对齐 DSA daily_stock_analysis（strategies/*.yaml 的
instructions 内嵌引用这些工具名，沿用命名使 15 个策略 YAML 零改动 COPY）。

数据源全部本地（datastore parquet + 资讯缓存），无外网依赖：
- get_realtime_quote   快照真实价 + 当日涨跌 + 估值/市值（非盘口流）
- get_daily_history    kline 真实价口径（最新价=真实价）
- analyze_trend        本地现算 MA/MACD/RSI/bias/量比/支撑阻力
- get_chip_distribution 复用 analyzer/chips.py（datastore.chips 入口）
- get_stock_info       stock_info + 财报摘要 + 估值
- get_capital_flow     analyzer/flow.py 主力资金（东财缓存）
- get_sector_rankings  board_rank 近 5 日板块榜
- get_market_indices   market_series 大盘概览
- search_stock_news    本地新闻缓存（条数如实，不编造）
- get_positions        模拟持仓（FIFO，口径同交易页）

裁剪护栏（对齐 DSA compact 手法）：kline ≤120 根、新闻 ≤5 条、
板块 ≤10 行——防 context 失控。
"""
from __future__ import annotations

import datetime as dt
import json
import re

import numpy as np
import pandas as pd

# ---------------------------------------------------------------- 声明

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "get_realtime_quote",
        "description": "获取股票最新快照行情：价格、涨跌幅、成交量额、市值、换手率、52周位置（非盘口实时流，为最近收盘快照口径）",
        "parameters": {"type": "object", "properties": {
            "stock_code": {"type": "string", "description": "股票代码，如 300687.SZ / 600519.SH"}},
            "required": ["stock_code"]}}},
    {"type": "function", "function": {
        "name": "get_daily_history",
        "description": "获取股票近 N 日日K线（真实价口径：open/high/low/close/volume/amount/涨跌幅/换手），默认60日",
        "parameters": {"type": "object", "properties": {
            "stock_code": {"type": "string"},
            "days": {"type": "integer", "description": "返回根数，1..120，默认60"}},
            "required": ["stock_code"]}}},
    {"type": "function", "function": {
        "name": "analyze_trend",
        "description": "技术面分析：均线排列(MA5/10/20/60)、MACD、RSI、乖离率、量比、支撑阻力位、趋势状态判定",
        "parameters": {"type": "object", "properties": {
            "stock_code": {"type": "string"}},
            "required": ["stock_code"]}}},
    {"type": "function", "function": {
        "name": "get_chip_distribution",
        "description": "筹码分布：获利比例、平均成本、90%/70%成本区间与集中度",
        "parameters": {"type": "object", "properties": {
            "stock_code": {"type": "string"}},
            "required": ["stock_code"]}}},
    {"type": "function", "function": {
        "name": "get_stock_info",
        "description": "股票基本面画像：市值/流通市值、52周高低、年初至今涨幅、最新财报摘要(营收/净利/ROE/毛净利率/负债率)、估值(PE/PB)",
        "parameters": {"type": "object", "properties": {
            "stock_code": {"type": "string"}},
            "required": ["stock_code"]}}},
    {"type": "function", "function": {
        "name": "get_capital_flow",
        "description": "主力资金流向：当日/近5日/近10日主力净流入",
        "parameters": {"type": "object", "properties": {
            "stock_code": {"type": "string"}},
            "required": ["stock_code"]}}},
    {"type": "function", "function": {
        "name": "get_sector_rankings",
        "description": "行业板块近5日涨跌幅排行（领涨领跌，成分股等权）",
        "parameters": {"type": "object", "properties": {
            "top_n": {"type": "integer", "description": "每侧返回数，默认10"}}}}},
    {"type": "function", "function": {
        "name": "get_market_indices",
        "description": "大盘概览：主要指数走势与全市场涨跌家数、成交额（近10日）",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "search_stock_news",
        "description": "查询股票近期新闻（本地缓存，最多5条；无结果时如实返回0条，绝不编造）",
        "parameters": {"type": "object", "properties": {
            "stock_code": {"type": "string"}},
            "required": ["stock_code"]}}},
    {"type": "function", "function": {
        "name": "get_positions",
        "description": "查询当前模拟持仓：代码、名称、股数、成本、浮盈、市值",
        "parameters": {"type": "object", "properties": {
            "user": {"type": "string", "description": "可选，用户名，默认当前用户"}}}}},
]


def _norm_code(code) -> str:
    c = str(code or "").strip().upper()
    m = re.match(r"^(\d{6})(?:\.(SH|SZ|BJ))?$", c)
    if not m:
        return c
    num, suf = m.group(1), m.group(2)
    if not suf:
        suf = "SH" if num.startswith(("6", "9", "5")) else \
              ("BJ" if num.startswith(("4", "8")) else "SZ")
    return f"{num}.{suf}"


def _tool_error(msg: str, retriable: bool = False) -> dict:
    return {"error": msg, "retriable": retriable}


# ---------------------------------------------------------------- handlers

def tool_realtime_quote(store, code):
    code = _norm_code(code)
    k = store.kline(code)
    if not k:
        return _tool_error(f"未找到 {code} 的日线数据", False)
    si = store.stock_info(code) or {}
    pct = k["pct_chg"][-1] if k.get("pct_chg") else None
    return {
        "stock_code": code, "name": k.get("name"),
        "price": k.get("real_price") or (k["close"][-1] if k.get("close") else None),
        "change_pct": round(pct * 100, 2) if pct is not None else None,
        "pre_close": round(k["close"][-2], 3) if len(k.get("close") or []) >= 2 else None,
        "volume": k["volume"][-1] if k.get("volume") else None,
        "amount": k["amount"][-1] if k.get("amount") else None,
        "turnover_rate": k["turnover"][-1] if k.get("turnover") else None,
        "total_mv": k.get("total_mv"), "circ_mv": k.get("float_mv"),
        "week52_high": si.get("week52_high"), "week52_low": si.get("week52_low"),
        "ytd_chg": si.get("ytd_chg"),
        "last_date": k["dates"][-1] if k.get("dates") else None,
    }


def tool_daily_history(store, code, days=60):
    code = _norm_code(code)
    days = max(1, min(int(days or 60), 120))
    k = store.kline(code)
    if not k:
        return _tool_error(f"未找到 {code} 的日线数据", False)
    dates = k["dates"][-days:]
    rows = [{"date": str(d)[:10],
             "open": round(o, 3) if o is not None else None,
             "high": round(h, 3) if h is not None else None,
             "low": round(l, 3) if l is not None else None,
             "close": round(c, 3) if c is not None else None,
             "volume": v, "amount": a,
             "pct_chg": round(p * 100, 2) if p is not None else None,
             "turnover": t}
            for d, o, h, l, c, v, a, p, t in zip(
                dates, k["open"][-days:], k["high"][-days:], k["low"][-days:],
                k["close"][-days:], k["volume"][-days:], k["amount"][-days:],
                k["pct_chg"][-days:], k["turnover"][-days:])]
    return {"stock_code": code, "name": k.get("name"), "count": len(rows),
            "bars": rows, "last_date": str(dates[-1])[:10] if dates else None}


def tool_trend(store, code):
    """本地现算技术面（键名对齐 DSA analysis_tools 输出，策略 yaml 直接引用）。"""
    code = _norm_code(code)
    k = store.kline(code)
    if not k or len(k["close"]) < 20:
        return _tool_error(f"{code} 数据不足（需≥20根日K）", False)
    close = pd.Series(k["close"], dtype=float)
    high = pd.Series(k["high"], dtype=float)
    low = pd.Series(k["low"], dtype=float)
    vol = pd.Series(k["volume"], dtype=float)

    mas = {f"ma{n}": (round(close.rolling(n).mean().iloc[-1], 3)
                      if len(close) >= n else None) for n in (5, 10, 20, 60)}
    ma_now = {kk: v for kk, v in mas.items() if v is not None}
    vals = list(ma_now.values())
    aligned = "多头" if all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)) \
        else ("空头" if all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1)) else "交织")
    c = close.iloc[-1]
    bias = {f"bias_ma{n}": round((c / v - 1) * 100, 2)
            for n, v in ma_now.items()}

    # MACD（12,26,9）
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    bar = (dif - dea) * 2
    macd_state = ("金叉" if dif.iloc[-1] > dea.iloc[-1] and dif.iloc[-2] <= dea.iloc[-2]
                  else "死叉" if dif.iloc[-1] < dea.iloc[-1] and dif.iloc[-2] >= dea.iloc[-2]
                  else ("零上多头" if dif.iloc[-1] > dea.iloc[-1] > 0
                        else "零下空头" if dif.iloc[-1] < dea.iloc[-1] < 0 else "纠缠"))

    # RSI（Wilder 6/12）
    def _rsi(n):
        d = close.diff()
        up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
        dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
        rs = up / dn.replace(0, np.nan)
        return round(float((100 - 100 / (1 + rs)).iloc[-1]), 1) if np.isfinite(rs.iloc[-1]) else None

    # 量比（5日）
    vol_ma5 = vol.rolling(5).mean().iloc[-1]
    vol_ratio_5d = round(float(vol.iloc[-1] / vol_ma5), 2) if vol_ma5 else None
    volume_status = ("放量" if vol_ratio_5d and vol_ratio_5d > 1.5
                     else "缩量" if vol_ratio_5d and vol_ratio_5d < 0.7 else "平量")

    # 支撑阻力（近 60 日枢轴）
    h60, l60 = high.tail(60), low.tail(60)
    resist = round(float(h60.max()), 3)
    support = round(float(l60.min()), 3)
    near_high = h60.rank(pct=True).iloc[-1]

    chg20 = round((c / close.iloc[-21] - 1) * 100, 2) if len(close) > 21 else None
    trend_status = ("上升趋势" if chg20 is not None and chg20 > 5 and aligned != "空头"
                    else "下降趋势" if chg20 is not None and chg20 < -5 and aligned != "多头"
                    else "震荡")

    return {
        "stock_code": code, "name": k.get("name"),
        "trend_status": trend_status, "ma_alignment": aligned, **mas,
        **bias, "macd_dif": round(float(dif.iloc[-1]), 4),
        "macd_dea": round(float(dea.iloc[-1]), 4),
        "macd_bar": round(float(bar.iloc[-1]), 4), "macd_state": macd_state,
        "rsi6": _rsi(6), "rsi12": _rsi(12),
        "volume_status": volume_status, "volume_ratio_5d": vol_ratio_5d,
        "support": support, "resistance": resist,
        "close_pos_60d": round(float(near_high), 2),
        "chg_20d": chg20,
        "last_date": k["dates"][-1],
    }


def tool_chip_distribution(store, code):
    code = _norm_code(code)
    try:
        c = store.chips(code)
    except Exception as e:  # noqa: BLE001
        return _tool_error(f"筹码数据不可用: {e}", False)
    if not c:
        return _tool_error(f"{code} 无筹码数据", False)
    stats = c.get("stats") or {}
    out = {"stock_code": code, "name": c.get("name"),
           "profit_ratio": stats.get("profit_ratio"),
           "avg_cost": stats.get("avg_cost")}
    for k_ in ("cost_90_low", "cost_90_high", "concentration_90",
               "cost_70_low", "cost_70_high", "concentration_70"):
        if k_ in stats:
            out[k_] = stats[k_]
    return out


def tool_stock_info(store, code):
    code = _norm_code(code)
    si = store.stock_info(code) or {}
    k = store.kline(code)
    out = {"stock_code": code, "name": (k or {}).get("name"),
           "total_mv": (k or {}).get("total_mv"),
           "circ_mv": (k or {}).get("float_mv"),
           "week52_high": si.get("week52_high"), "week52_low": si.get("week52_low"),
           "ytd_chg": si.get("ytd_chg"), "y1y_chg": si.get("y1y_chg")}
    try:
        f = store.fund_latest(code)
        if f:
            out["fundamentals"] = {kk: f.get(kk) for kk in
                                   ("period", "rev", "rev_yoy", "net_profit",
                                    "net_profit_yoy", "eps", "roe",
                                    "gross_margin", "net_margin", "debt_ratio")
                                   if f.get(kk) is not None}
    except Exception:  # noqa: BLE001
        out["fundamentals"] = None
    try:
        v = store.valuation(code)
        if v:
            out["valuation"] = {kk: v.get(kk) for kk in
                                ("pe_ttm", "pe_static", "pb", "date")
                                if v.get(kk) is not None}
    except Exception:  # noqa: BLE001
        out["valuation"] = None
    return out


def tool_capital_flow(store, code):
    code = _norm_code(code)
    # 只读本地缓存（不触发外网抓取——问股不应有抓取副作用）
    try:
        from ..flow import _load_cache, _cache_path
        f = _load_cache(_cache_path(store.root, code))
    except ImportError:
        try:
            from analyzer.flow import _load_cache, _cache_path
            f = _load_cache(_cache_path(store.root, code))
        except ImportError as e:
            return _tool_error(f"资金流数据不可用: {e}", False)
    except Exception as e:  # noqa: BLE001
        return _tool_error(f"资金流数据不可用: {e}", False)
    if not f or not f.get("items"):
        return _tool_error(f"{code} 无资金流数据（本地缓存为空）", False)
    items = f["items"]

    def _sum(n):
        try:
            return round(sum(float(x.get("main_net_inflow") or 0)
                             for x in items[:n]) / 1e8, 2)
        except Exception:  # noqa: BLE001
            return None

    return {"stock_code": code,
            "main_net_inflow": items[0].get("main_net_inflow"),
            "inflow_5d_yi": _sum(5), "inflow_10d_yi": _sum(10),
            "date": items[0].get("date"), "n_days": len(items),
            "stale": bool(f.get("stale"))}


def tool_sector_rankings(store, top_n=10, days=5):
    top_n = max(1, min(int(top_n or 10), 10))
    end = dt.date.today()
    start = str(end - dt.timedelta(days=days * 2 + 4))
    try:
        top = store.board_rank(start, str(end), n=top_n, board_type="industry")
        bot = store.board_rank(start, str(end), n=3, ascending=True,
                               board_type="industry")
    except Exception as e:  # noqa: BLE001
        return _tool_error(f"板块数据不可用: {e}", False)
    fmt = lambda rows: [{"board": r["board_name"], "n_stocks": r["n_stocks"],
                         "ret_pct": round(r["ret"] * 100, 2)} for r in rows]
    return {"window_days": days, "n_boards": top.get("n_boards"),
            "top": fmt(top.get("rows") or []),
            "bottom": fmt(bot.get("rows") or [])}


def tool_market_indices(store):
    ms = store.market_series
    if not ms or not ms.get("dates"):
        return _tool_error("大盘数据不可用", False)
    n = min(10, len(ms["dates"]))
    idx = ms.get("indexes") or {}
    return {
        "dates": [str(d)[:10] for d in ms["dates"][-n:]],
        "adv": list(ms["adv"][-n:]), "dec": list(ms["dec"][-n:]),
        "amount_yi": [round(float(x), 0) for x in ms["amount_yi"][-n:]],
        "indexes": {kk: {"dates": [str(d)[:10] for d in (v.get("dates") or [])[-n:]],
                         "close": [round(float(c), 2) for c in (v.get("close") or [])[-n:]]}
                    for kk, v in idx.items()},
    }


def tool_news(store, code):
    code = _norm_code(code)
    try:
        n = store.news(code)
    except Exception as e:  # noqa: BLE001
        return _tool_error(f"新闻数据不可用: {e}", False)
    if not n:
        return {"stock_code": code, "count": 0, "items": []}
    items = (n.get("items") or [])[:5]
    return {"stock_code": code, "count": len(items), "stale": n.get("stale"),
            "items": [{"title": x.get("title"), "source": x.get("source"),
                       "date": x.get("date") or x.get("published_date"),
                       "snippet": (x.get("content") or x.get("snippet") or "")[:120]}
                      for x in items]}


def tool_positions(store, user=None):
    try:
        p = store.positions(user) if user else store.positions()
    except Exception as e:  # noqa: BLE001
        return _tool_error(f"持仓数据不可用: {e}", False)
    if not p:
        return {"count": 0, "positions": []}
    # positions -> dict[code, {held, avg_cost, realized, unrealized, n_trades}]
    out = []
    for code, r in list(p.items())[:12]:
        if not r or not r.get("held"):
            continue
        name = ""
        try:
            if code in store.basic.index:
                name = str(store.basic.loc[code, "name"])
        except Exception:  # noqa: BLE001
            pass
        price = store.real_price(code)
        out.append({"code": code, "name": name,
                    "shares": r.get("held"), "avg_cost": r.get("avg_cost"),
                    "last_price": price,
                    "realized": r.get("realized"),
                    "unrealized": r.get("unrealized")})
    return {"count": len(out), "positions": out}


# ---------------------------------------------------------------- 派发

_HANDLERS = {
    "get_realtime_quote": lambda store, a: tool_realtime_quote(store, a.get("stock_code", "")),
    "get_daily_history": lambda store, a: tool_daily_history(
        store, a.get("stock_code", ""), a.get("days", 60)),
    "analyze_trend": lambda store, a: tool_trend(store, a.get("stock_code", "")),
    "get_chip_distribution": lambda store, a: tool_chip_distribution(store, a.get("stock_code", "")),
    "get_stock_info": lambda store, a: tool_stock_info(store, a.get("stock_code", "")),
    "get_capital_flow": lambda store, a: tool_capital_flow(store, a.get("stock_code", "")),
    "get_sector_rankings": lambda store, a: tool_sector_rankings(store, a.get("top_n", 10)),
    "get_market_indices": lambda store, a: tool_market_indices(store),
    "search_stock_news": lambda store, a: tool_news(store, a.get("stock_code", "")),
    "get_positions": lambda store, a: tool_positions(
        store, a.get("user") or (store._agent_user or None)),
}


def execute_tool(store, name: str, arguments_json: str) -> str:
    """执行单个工具调用，返回 JSON 字符串（失败也返回 JSON，retriable=False）。

    store._agent_user 由 run_chat_turn 注入（登录用户），get_positions
    无显式 user 参数时回落到它。
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        return json.dumps(_tool_error(f"未知工具: {name}"), ensure_ascii=False)
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        return json.dumps(_tool_error("工具参数不是合法 JSON"), ensure_ascii=False)
    try:
        result = handler(store, args)
    except Exception as e:  # noqa: BLE001 工具故障隔离：报错继续
        result = _tool_error(f"{type(e).__name__}: {e}", False)
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(result), ensure_ascii=False)
