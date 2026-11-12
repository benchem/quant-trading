"""LLM 传输层：配置、错误、OpenAI 兼容 chat/completions 调用。

进化定位：换模型网关、加重试/降级、调整默认参数只改本文件；
prompt 内容在 prompts.py，"喂什么数据"在 context.py。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import requests

#: 报告统一免责声明（由系统附加，不进 prompt）
DISCLAIMER = ("免责声明：本报告由 AI 基于本地量化模型结果与公开新闻生成，"
              "仅供参考，不构成任何投资建议；股市有风险，决策需独立判断。")


class AIError(RuntimeError):
    """配置/网络/响应错误，message 为可直接展示的中文说明。"""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class AIConfig:
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    timeout: float = 60.0
    max_tokens: int = 2500

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_env(cls) -> "AIConfig":
        return cls(
            base_url=os.environ.get("AI_BASE_URL", cls.base_url).rstrip("/"),
            api_key=os.environ.get("AI_API_KEY", "").strip(),
            model=os.environ.get("AI_MODEL", cls.model),
            timeout=float(os.environ.get("AI_TIMEOUT", cls.timeout)),
            max_tokens=int(os.environ.get("AI_MAX_TOKENS", cls.max_tokens)),
        )


def chat_completion(config: AIConfig, messages: list[dict],
                    tools: list[dict] | None = None,
                    tool_choice: str | dict | None = None) -> dict:
    """调用 OpenAI 兼容 chat/completions，返回 {content, tool_calls, usage}。

    tools: OpenAI function-calling 工具声明（问股 Agent 用）；None 为纯文本。
    带 tool_calls 时 content 允许为空；失败抛 AIError。
    """
    if not config.enabled:
        raise AIError("未配置 AI_API_KEY，AI 分析不可用", 503)
    payload: dict = {"model": config.model, "messages": messages,
                     "temperature": 0.3, "max_tokens": config.max_tokens,
                     "stream": False}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    try:
        resp = requests.post(
            f"{config.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {config.api_key}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=config.timeout,
        )
    except requests.Timeout:
        raise AIError(f"大模型请求超时（{config.timeout:.0f} 秒），请稍后重试或调大 AI_TIMEOUT") from None
    except requests.RequestException as e:
        raise AIError(f"大模型网络错误: {str(e)[:200]}") from None

    if resp.status_code in (401, 403):
        raise AIError("大模型认证失败：请检查 AI_API_KEY 是否正确、账户是否有权限/余额")
    if resp.status_code == 429:
        raise AIError("大模型限流（429）：请求过于频繁或额度不足，请稍后重试")
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = str((resp.json().get("error") or {}).get("message") or "")[:200]
        except ValueError:
            detail = resp.text[:200]
        raise AIError(f"大模型返回错误（HTTP {resp.status_code}）: {detail}")
    try:
        data = resp.json()
        msg = data["choices"][0]["message"]
        content = (msg.get("content") or "").strip()
        if not content:
            # GLM/Z 系思考模型经 one-api 透传时正文可能落在 reasoning_content
            content = (msg.get("reasoning_content") or "").strip()
        raw_calls = msg.get("tool_calls") or []
        tool_calls = [{"id": c.get("id") or f"call_{i}",
                       "name": (c.get("function") or {}).get("name") or "",
                       "arguments": (c.get("function") or {}).get("arguments") or "{}"}
                      for i, c in enumerate(raw_calls)]
    except (ValueError, KeyError, IndexError, TypeError):
        raise AIError("大模型响应格式异常（非 OpenAI 兼容格式）") from None
    if not content and not tool_calls:
        raise AIError("大模型返回了空内容")
    return {"content": content, "tool_calls": tool_calls,
            "usage": data.get("usage") or {}}
