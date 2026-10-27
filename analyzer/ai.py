"""个股 AI 综合分析 —— 兼容壳。

智能体（LLM）能力已迁至 analyzer.agent 包（进化区）：
    agent/llm.py      传输与配置（AIConfig / AIError / chat_completion）
    agent/prompts.py  全部 prompt 文本与版本号
    agent/context.py  上下文组装策略
    agent/analysis.py generate() 入口与缓存
    agent/review.py   LLM 版复盘

本文件仅保留 re-export，保证旧 import 路径（from analyzer.ai import …）
继续可用；新代码请直接 import analyzer.agent。
架构契约见 analyzer/agent/__init__.py 与 tests/test_architecture.py。
"""
from __future__ import annotations

import requests  # noqa: F401 兼容：测试/旧代码以 analyzer.ai.requests 为 mock 锚点

from .agent.analysis import (CACHE_TTL_SECONDS,  # noqa: F401
                             generate, _cache_path, _load_cache, _save_cache,
                             _fingerprint)
from .agent.context import (DEFAULT_PLUGINS,  # noqa: F401
                            MAX_CONTEXT_CHARS, MAX_NEWS, MAX_TABLE_ROWS,
                            MAX_SIGNALS, build_prompt, collect_context,
                            context_meta, _context_json, _fit_context)
from .agent.llm import AIConfig, AIError, DISCLAIMER, chat_completion  # noqa: F401
from .agent.prompts import PROMPT_VERSION, REVIEW_PROMPT_VERSION  # noqa: F401
from .agent.review import build_llm_review  # noqa: F401
from .overseas import load_daily_brief  # noqa: F401 兼容旧引用路径
