"""个股 AI 综合分析入口：上下文 -> prompt -> LLM -> 缓存报告。

进化定位：报告缓存策略（TTL/指纹/并发锁）改这里；
"喂什么数据"改 context.py，"怎么说"改 prompts.py，"怎么调"改 llm.py。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from .context import build_prompt, collect_context, context_meta, _fit_context, _context_json
from .llm import AIConfig, AIError, DISCLAIMER, chat_completion
from .prompts import PROMPT_VERSION

CACHE_TTL_SECONDS = 6 * 3600   # 报告缓存 6 小时（上下文指纹变化立即失效）


def _cache_path(root: str | Path, code: str) -> Path:
    return Path(root) / "ai" / f"{code}.json"


def _load_cache(f: Path) -> dict | None:
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("report") and data.get("fingerprint"):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


def _save_cache(f: Path, payload: dict) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(f)


def _fingerprint(context: dict, model: str) -> str:
    raw = f"{PROMPT_VERSION}|{model}|{_context_json(context)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(code: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(code, threading.Lock())


def generate(store, runner, code: str, refresh: bool = False,
             config: AIConfig | None = None) -> dict:
    """生成（或命中缓存）单只股票的 AI 综合分析报告。

    缓存键 = prompt 版本 + 模型名 + 上下文指纹（含数据截止日期），TTL 6 小时；
    refresh=True 强制重新调用大模型。按股票加进程内锁，避免并发重复扣费。
    """
    config = config or AIConfig.from_env()
    if not config.enabled:
        raise AIError("未配置 AI_API_KEY，AI 分析不可用", 503)
    code = code.upper()
    with _lock_for(code):
        context, warnings = collect_context(store, runner, code)
        context = _fit_context(context)
        fingerprint = _fingerprint(context, config.model)
        cache_file = _cache_path(store.root, code)
        cached = _load_cache(cache_file)
        now = time.time()
        if (not refresh and cached
                and cached.get("fingerprint") == fingerprint
                and now - cached.get("created_ts", 0) <= CACHE_TTL_SECONDS):
            out = dict(cached)
            out["cached"] = True
            return out

        resp = chat_completion(config, build_prompt(context, warnings))
        report = f"{resp['content']}\n\n---\n{DISCLAIMER}"
        payload = {
            "code": code,
            "model": config.model,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "created_ts": now,
            "fingerprint": fingerprint,
            "report": report,
            "warnings": warnings,
            "usage": resp["usage"],
            "context_meta": context_meta(context),
        }
        _save_cache(cache_file, payload)
        out = dict(payload)
        out["cached"] = False
        return out
