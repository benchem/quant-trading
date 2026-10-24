"""限速与重试工具。"""
from __future__ import annotations

import random
import threading
import time
from typing import Any, Callable

from .errors import CooldownBlocked  # noqa: F401  (re-export)


class RateLimiter:
    """全局最小间隔限速器（线程安全）。rate<=0 表示不限速。"""

    def __init__(self, rate_per_sec: float):
        self.rate = float(rate_per_sec)
        self._min_interval = 1.0 / self.rate if self.rate > 0 else 0.0
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_at)
            self._next_at = start + self._min_interval
        delay = start - now
        if delay > 0:
            time.sleep(delay)


def with_retry(
    fn: Callable[..., Any],
    args: tuple = (),
    kwargs: dict | None = None,
    retries: int = 3,
    base_delay: float = 1.5,
    desc: str = "",
    limiter: RateLimiter | None = None,
) -> Any:
    """带指数退避的重试；每次尝试前先经过限速器。

    CooldownBlocked（限流冷却中）不重试：冷却期内重试毫无意义（不会发请求）。
    """
    kwargs = kwargs or {}
    last_exc: Exception | None = None
    for attempt in range(retries):
        if limiter is not None:
            limiter.wait()
        try:
            return fn(*args, **kwargs)
        except CooldownBlocked:
            raise
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt < retries - 1:
                delay = base_delay * (2**attempt) * (1.0 + random.random() * 0.5)
                time.sleep(delay)
    raise RuntimeError(f"重试 {retries} 次仍失败 [{desc}]: {last_exc}") from last_exc
