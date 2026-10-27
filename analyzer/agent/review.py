"""LLM 版每日复盘：规则版复盘 + 用户笔记 + 模型信号事件 -> 大模型。

进化定位：复盘的 LLM 行为（prompt、引用多少笔记/信号）改这里和 prompts.py；
规则版复盘（build_daily_review）留在规则面 journal.py，不依赖本模块——
LLM 不可用/未配置时，规则复盘照常工作。
"""
from __future__ import annotations

import datetime as dt

from .llm import AIConfig, AIError, chat_completion
from .prompts import (REVIEW_PROMPT_VERSION,  # noqa: F401 版本供外部感知/失效
                      REVIEW_SYSTEM_PROMPT, REVIEW_USER_TEMPLATE)

#: 复盘引用窗口（天）与条数上限
REVIEW_LOOKBACK_DAYS = 10
LLM_NOTES_LIMIT = 8
LLM_SIGNAL_EVENTS_LIMIT = 10


def build_llm_review(journal, code: str, day: str, rule_review: dict,
                     config=None) -> dict:
    """LLM 版复盘：基于规则版复盘 + 用户笔记 + 信号事件生成。

    需要 AI_API_KEY；失败抛 AIError。返回 {report, usage}，
    由调用方 add_review(version="llm") 落盘。
    """
    config = config or AIConfig.from_env()
    if not config.enabled:
        raise AIError("未配置 AI_API_KEY，LLM 复盘不可用", 503)
    code = code.upper()
    lookback_start = (dt.datetime.strptime(day, "%Y-%m-%d") - dt.timedelta(
        days=int(REVIEW_LOOKBACK_DAYS * 1.6))).strftime("%Y-%m-%d")

    notes = journal.notes(code=code, limit=LLM_NOTES_LIMIT)
    note_lines = [f"- {n.get('day')} {n.get('text')}" for n in notes] or ["（无笔记）"]
    sig_evs = journal.events(code=code, kind="model",
                             since_day=lookback_start, limit=LLM_SIGNAL_EVENTS_LIMIT)
    sig_lines = [f"- {e.get('day')} [{e.get('level')}] {e.get('text')}" for e in sig_evs] \
        or ["（无信号事件）"]

    rule_text = rule_review.get("summary", "") + "\n" + "\n".join(
        f"- [{l.get('type')}] {l.get('text')}" for l in rule_review.get("lines") or [])
    user = REVIEW_USER_TEMPLATE.format(
        code=code, day=day, rule_review=rule_text,
        n_notes=len(notes), notes="\n".join(note_lines),
        n_signals=len(sig_evs), signals="\n".join(sig_lines),
    )
    resp = chat_completion(config, [
        {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ])
    return {"report": resp["content"], "usage": resp.get("usage") or {}}
