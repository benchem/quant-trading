"""统一站内信 / 消息中心：analyzer 与 planwatch 共用的中立模块。"""
from __future__ import annotations

from .core import (  # noqa: F401
    DISABLE_MAC,
    LEVELS,
    SOURCES,
    NotificationCenter,
    get_inbox,
    normalize_level,
)

__all__ = [
    "NotificationCenter", "get_inbox", "normalize_level",
    "LEVELS", "SOURCES", "DISABLE_MAC", "set_desktop_enabled",
]


def set_desktop_enabled(enabled: bool) -> None:
    """全局开关 macOS 桌面通知（CLI --no-notify / 测试环境用）。"""
    import notifybox.core as _c

    _c.DISABLE_MAC = not enabled
