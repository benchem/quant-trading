"""上下文组装策略：决定"喂给 LLM 什么数据"。

进化定位：新增数据源（如龙虎榜、北向资金）、调整裁剪优先级、
改插件清单都改本文件——不碰 prompt（prompts.py）也不碰传输（llm.py）。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from ..overseas import load_daily_brief
from .llm import AIError
from .prompts import SYSTEM_PROMPT, USER_TEMPLATE

#: 输入模型清单（新增插件需手动加入才会纳入 AI 上下文）
DEFAULT_PLUGINS = (
    "multi_factor_breakout",
    "garch_mean_reversion",
    "chip_distribution",
    "bollinger_channel",
    "momentum",
    "risk_profile",
    "fundamental_quality",
)

MAX_NEWS = 10
MAX_TABLE_ROWS = 8
MAX_SIGNALS = 8
MAX_CONTEXT_CHARS = 12000      # 上下文 JSON 体积上限（超出按优先级裁剪）


# ------------------------------------------------------------------ 聚合


def _plugin_display_name(runner, plugin: str) -> str:
    try:
        meta = runner.registry.info(plugin) or {}
        return meta.get("display_name") or plugin
    except Exception:  # noqa: BLE001 元数据缺失不影响分析
        return plugin


def _trim_result(d: dict) -> dict:
    """裁剪插件 to_dict()：去掉图表序列与计时等不稳定/大体积字段。

    只保留结论性内容，保证同一数据下上下文 JSON 稳定（缓存指纹可复用）。
    """
    return {
        "score": d.get("score"),
        "verdict": d.get("verdict"),
        "summary": d.get("summary"),
        "metrics": d.get("metrics") or {},
        "metric_labels": d.get("metric_labels") or {},
        "signals": (d.get("signals") or [])[:MAX_SIGNALS],
        "tables": {k: (v or [])[-MAX_TABLE_ROWS:]
                   for k, v in (d.get("tables") or {}).items()},
        "extra": d.get("extra") or {},
    }


def collect_context(store, runner, code: str) -> tuple[dict, list[str]]:
    """聚合单只股票的模型结果/行情/新闻/资金流/公告/情绪，返回 (context, warnings)。

    单个插件或数据块失败只记 warning，不阻塞整体（降级原则）。
    """
    code = code.upper()
    kline = store.kline(code)
    if kline is None:
        raise AIError(f"未找到 {code} 的日线数据", 404)
    warnings: list[str] = []

    closes = [c for c in kline.get("close") or [] if c is not None]
    market = {
        "latest_close": closes[-1] if closes else None,
        "real_price": kline.get("real_price"),
        "total_mv": kline.get("total_mv"),
        "ret_1y": (round(closes[-1] / closes[-251] - 1.0, 4)
                   if len(closes) > 250 and closes[-251] else None),
        "bars": len(closes),
    }

    def _run(plugin: str) -> dict:
        name = _plugin_display_name(runner, plugin)
        entry: dict = {"plugin": plugin, "name": name}
        try:
            d = runner.run(plugin, code).to_dict()
        except Exception as e:  # noqa: BLE001 与插件框架同级的隔离边界
            entry.update(ok=False, error=f"{type(e).__name__}: {e}")
            return entry
        if not d.get("ok"):
            entry.update(ok=False, error=d.get("error") or "计算失败")
            return entry
        entry.update(ok=True, **_trim_result(d))
        return entry

    with ThreadPoolExecutor(max_workers=4) as pool:
        models = list(pool.map(_run, DEFAULT_PLUGINS))
    for m in models:
        if not m.get("ok"):
            warnings.append(f"模型 {m['name']} 不可用: {m.get('error')}")

    news_items: list[dict] = []
    try:
        news = store.news(code)
        for it in (news.get("items") or [])[:MAX_NEWS]:
            news_items.append({"title": it.get("title"),
                               "time": it.get("time"), "source": it.get("source")})
        if news.get("stale"):
            warnings.append("新闻为网络异常时的旧缓存")
    except Exception as e:  # noqa: BLE001 新闻不可用不阻塞 AI 分析
        warnings.append(f"新闻不可用: {str(e)[:120]}")

    overseas = load_daily_brief(store.root, code)
    if overseas["status"] != "valid":
        warnings.append(overseas["message"])

    # 资金流/公告/情绪（数据面；单块失败降级为 warning，不阻塞）
    flow_ctx: dict | None = None
    try:
        from ..flow import latest_metrics as flow_metrics

        flow_ctx = flow_metrics(code, store.root, days=5)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"资金流不可用: {str(e)[:120]}")
        flow_ctx = {"available": False, "error": str(e)[:120]}
    if flow_ctx and flow_ctx.get("stale"):
        warnings.append("资金流为网络异常时的旧缓存")

    announce_ctx: dict | None = None
    try:
        from ..announce import latest_titles

        announce_ctx = latest_titles(code, store.root, limit=10)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"公告不可用: {str(e)[:120]}")
        announce_ctx = {"available": False, "error": str(e)[:120]}
    if announce_ctx and announce_ctx.get("stale"):
        warnings.append("公告为网络异常时的旧缓存")

    sentiment_ctx: dict | None = None
    try:
        from ..sentiment import latest_metrics as sentiment_metrics

        sentiment_ctx = sentiment_metrics(code, store.root, days=10)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"情绪数据不可用: {str(e)[:120]}")
        sentiment_ctx = {"available": False, "error": str(e)[:120]}
    if sentiment_ctx and sentiment_ctx.get("stale"):
        warnings.append("情绪数据为网络异常时的旧缓存")

    context = {
        "code": code,
        "name": kline.get("name") or code,
        "as_of": (kline.get("dates") or [None])[-1],
        "market": market,
        "models": models,
        "news": news_items,
        "overseas": overseas,
        "flow": flow_ctx,
        "announcements": announce_ctx,
        "sentiment": sentiment_ctx,
    }
    return context, warnings


# ------------------------------------------------------------------ 裁剪与 prompt


def _context_json(context: dict) -> str:
    return json.dumps(context, ensure_ascii=False, default=str, sort_keys=True)


def _fit_context(context: dict) -> dict:
    """上下文超限时按优先级裁剪：公告减半 -> 表格 -> 新闻减半 -> 新闻清空
    -> extra/信号 -> 指标数。"""
    ctx = json.loads(_context_json(context))  # 深拷贝，避免污染缓存指纹外的调用方
    if len(_context_json(ctx)) <= MAX_CONTEXT_CHARS:
        return ctx
    ann = ctx.get("announcements") or {}
    if isinstance(ann.get("items"), list) and ann["items"]:
        ann["items"] = ann["items"][: max(1, len(ann["items"]) // 2)]
    if len(_context_json(ctx)) <= MAX_CONTEXT_CHARS:
        return ctx
    for m in ctx["models"]:
        m.pop("tables", None)
    if len(_context_json(ctx)) <= MAX_CONTEXT_CHARS:
        return ctx
    ctx["news"] = ctx["news"][:MAX_NEWS // 2]
    if len(_context_json(ctx)) <= MAX_CONTEXT_CHARS:
        return ctx
    ctx["news"] = []
    if len(_context_json(ctx)) <= MAX_CONTEXT_CHARS:
        return ctx
    for m in ctx["models"]:
        m.pop("extra", None)
        m["signals"] = (m.get("signals") or [])[:3]
    if len(_context_json(ctx)) <= MAX_CONTEXT_CHARS:
        return ctx
    for m in ctx["models"]:
        metrics = m.get("metrics") or {}
        trimmed = {k: (v[:80] if isinstance(v, str) else v)
                   for k, v in list(metrics.items())[:10]}
        m["metrics"] = trimmed
        m["metric_labels"] = {k: v for k, v in (m.get("metric_labels") or {}).items()
                              if k in trimmed}
    if len(_context_json(ctx)) <= MAX_CONTEXT_CHARS:
        return ctx
    for m in ctx["models"]:  # 兜底：结论与信号文本同样限制长度
        m["summary"] = (m.get("summary") or "")[:300]
        m["signals"] = [{**s, "text": str(s.get("text") or "")[:200]}
                        for s in (m.get("signals") or [])]
    return ctx


def context_meta(context: dict) -> dict:
    """上下文摘要（随报告落盘，供前端/排查展示）。"""
    models = context.get("models") or []
    overseas = context.get("overseas") or {}
    flow = context.get("flow") or {}
    ann = context.get("announcements") or {}
    sent = context.get("sentiment") or {}
    return {
        "as_of": context.get("as_of"),
        "models_ok": sum(1 for m in models if m.get("ok")),
        "models_failed": sum(1 for m in models if not m.get("ok")),
        "news_count": len(context.get("news") or []),
        "overseas_status": overseas.get("status", "unavailable"),
        "overseas_date": overseas.get("date"),
        "overseas_risk_score": overseas.get("risk_score"),
        "flow_available": bool(flow.get("available")),
        "flow_last_day": flow.get("last_day"),
        "announcements_available": bool(ann.get("available")),
        "announcements_n": ann.get("n_total"),
        "sentiment_available": bool(sent.get("available")),
        "sentiment_last_day": sent.get("last_day"),
    }


def build_prompt(context: dict, warnings: list[str] | None = None) -> list[dict]:
    # 数据质量警告（新闻不可用/过期、模型失败）必须让模型知晓，避免把"无新闻"误读为"无消息"。
    data_quality = "；".join(warnings) if warnings else "无（各模型与新闻均正常）"
    user = USER_TEMPLATE.format(
        code=context["code"], name=context["name"], as_of=context["as_of"],
        data_quality=data_quality, payload=_context_json(context),
    )
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]
