"""智能体层（进化区）：所有依赖 LLM 的代码集中在本包。

架构契约（由 tests/test_architecture.py 机器强制）：
    数据面（datastore/news/flow/announce/sentiment/overseas…）
        → 模型/规则面（stock_analysis/journal/updates/report…，纯确定性逻辑）
            → 智能体面（本包：prompt、LLM 调用、上下文组装策略）
    依赖只能向下（agent 可 import 数据面/模型面），禁止反向；
    规则面禁止 import 本包（升级/更换 LLM 不得改动规则代码）。
    例外：server.py 是组合根，负责接线（本包 ← server 是唯一允许的上层引用）。

本包内的文件分工（进化时按此定位改动点）：
    llm.py      传输与配置：AIConfig / AIError / chat_completion（换网关/加重试改这里）
    prompts.py  全部 prompt 文本与版本号（改措辞/输出结构只改这里，递增版本使缓存失效）
    context.py  上下文组装策略：聚合哪些数据、超限如何裁剪（改"喂什么"改这里）
    analysis.py 个股综合分析入口与报告缓存
    review.py   LLM 版每日复盘
"""
from .llm import AIConfig, AIError, chat_completion, DISCLAIMER  # noqa: F401
from .prompts import PROMPT_VERSION, REVIEW_PROMPT_VERSION  # noqa: F401
from .context import collect_context, build_prompt, DEFAULT_PLUGINS  # noqa: F401
from .analysis import generate  # noqa: F401
from .review import build_llm_review  # noqa: F401
