"""实时行情：腾讯 qt.gtimg.cn（GBK 编码），代理优先、直连兜底。"""
from __future__ import annotations

import os
import re
import socket

import requests


def _default_proxy() -> str | None:
    p = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    if p:
        return p
    try:
        s = socket.create_connection(("127.0.0.1", 7890), timeout=0.3)
        s.close()
        return "http://127.0.0.1:7890"
    except Exception:
        return None


def _sym(code: str) -> str:
    num, mkt = code.split(".")
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(mkt.upper(), "sz") + num


def fetch_quote(code: str, timeout: float = 8.0) -> dict:
    """返回 {name, price, pre_close, open, high, low, volume(手), ts, change, change_pct,
    amount_wan(万), turnover_pct, limit_up, limit_down}。"""
    url = f"https://qt.gtimg.cn/q={_sym(code)}"
    last_err: Exception | None = None
    for proxy in (_default_proxy(), None):
        try:
            s = requests.Session()
            if proxy:
                s.proxies = {"http": proxy, "https": proxy}
            else:
                s.trust_env = False  # 强制直连（忽略环境变量代理）
            r = s.get(url, timeout=timeout)
            r.raise_for_status()
            r.encoding = "gbk"
            m = re.search(r'"([^"]+)"', r.text)
            if not m:
                raise ValueError("行情响应为空")
            f = m.group(1).split("~")

            def fl(i: int) -> float | None:
                try:
                    return float(f[i])
                except Exception:
                    return None

            return {
                "code": code,
                "name": f[1],
                "price": fl(3),
                "pre_close": fl(4),
                "open": fl(5),
                "volume": fl(6),
                "ts": f[30] if len(f) > 30 else "",
                "change": fl(31),
                "change_pct": fl(32),
                "high": fl(33),
                "low": fl(34),
                "amount_wan": fl(37),
                "turnover_pct": fl(38),
                "limit_up": fl(47),
                "limit_down": fl(48),
            }
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"行情获取失败 {code}: {last_err}")
