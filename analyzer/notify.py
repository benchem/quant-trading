"""消息中心（向后兼容 shim）：核心实现已抽到中立包 notifybox，供 analyzer
与 planwatch 共用同一收件箱存储。此处再导出，保持既有 `from .notify import
NotificationCenter` 调用不变；另提供多用户分发器 NotificationHub。

统一渠道语义见 notifybox.core。
"""
from __future__ import annotations

import threading

from notifybox import (  # noqa: F401
    DISABLE_MAC,
    LEVELS,
    SOURCES,
    NotificationCenter,
    get_inbox,
    normalize_level,
    set_desktop_enabled,
)


class NotificationHub:
    """多用户消息分发：每个用户一个独立 NotificationCenter（data/users/{user}/
    notifications.jsonl），实现站内信按用户隔离。

    - ``center(user)`` 惰性创建并缓存该用户的收件箱；
    - ``push()`` 签名与 NotificationCenter.push 兼容（EodUpdater 可直接把它
      当作 notifier 使用）：带 ``code`` 的信号只推给「自选股包含该代码」的
      用户，系统类消息（无 code）推给全部用户；
    - 桌面通知仅在首个目标收件箱触发（避免同一事件响 N 遍）。
    """

    def __init__(self, data_dir, users_provider, watchlist_provider=None):
        """
        :param data_dir: 数据根目录（每用户收件箱落 data/users/{user}/ 下）
        :param users_provider: () -> list[str]，当前全部注册用户
        :param watchlist_provider: (user) -> list[str] | None，该用户的自选股
                                   （None/未提供时信号广播给全部用户）
        """
        self.data_dir = data_dir
        self._users_provider = users_provider
        self._watchlist_provider = watchlist_provider
        self._centers: dict[str, NotificationCenter] = {}
        self._lock = threading.Lock()

    def users(self) -> list[str]:
        try:
            return list(self._users_provider())
        except Exception:  # noqa: BLE001
            return []

    def center(self, user: str) -> NotificationCenter:
        """取（并惰性创建）某用户的独立收件箱。"""
        with self._lock:
            c = self._centers.get(user)
            if c is None:
                c = NotificationCenter(f"{self.data_dir}/users/{user}")
                self._centers[user] = c
            return c

    def _targets(self, code: str | None) -> list[str]:
        """消息目标用户：无 code -> 全部；有 code -> 自选含该股的用户（查不到归属时广播全部，保证不丢消息）。"""
        users = self.users()
        if code is None or self._watchlist_provider is None:
            return users
        out = []
        try:
            for u in users:
                codes = self._watchlist_provider(u) or []
                if code in codes:
                    out.append(u)
        except Exception:  # noqa: BLE001 归属解析失败退化为广播
            return users
        return out or users

    def push(self, key, text, level="info", code=None, desktop=None,
             day=None, source="system", dedup=True, extra=None):
        """与 NotificationCenter.push 同签名；按用户分发，返回最后一条的写入结果。"""
        targets = self._targets(code)
        last = None
        for i, u in enumerate(targets):
            try:
                last = self.center(u).push(
                    key, text, level=level, code=code,
                    desktop=desktop if i == 0 else False,
                    day=day, source=source, dedup=dedup, extra=extra,
                )
            except Exception:  # noqa: BLE001 单用户写入失败不影响其余
                continue
        return last

    def list(self, user, **kw) -> dict:
        return self.center(user).list(**kw)

    def mark_read(self, user, **kw) -> int:
        return self.center(user).mark_read(**kw)

    def clear(self, user, **kw) -> int:
        return self.center(user).clear(**kw)


__all__ = [
    "NotificationCenter", "NotificationHub", "get_inbox", "normalize_level",
    "LEVELS", "SOURCES", "DISABLE_MAC", "set_desktop_enabled",
]
