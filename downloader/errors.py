"""统一异常定义（供 http / ratelimit / source / app 共用，避免循环导入）。"""
from __future__ import annotations


class FetchError(Exception):
    """数据获取失败（网络错误 / 所有引擎均失败 / 解析失败等）。"""


class RateLimitedError(FetchError):
    """数据源返回 403/429/501：该 IP 正在被限流/惩罚。

    换引擎（同 IP）没有意义，上层应进入冷却而不是重试。
    """

    BLOCK_CODES = {403, 429, 501}

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


class CooldownBlocked(FetchError):
    """数据源冷却期内主动跳过（无网络请求）。重试层应直接抛出，不重试。"""


class SourceUnsupportedError(FetchError):
    """兜底源不支持该证券（如 baostock 不支持北交所）。

    这是能力边界而非故障：不应触发限流冷却，应直接报错记录。
    """
