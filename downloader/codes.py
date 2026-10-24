"""股票代码规范化：统一使用 Wind 风格代码，如 600000.SH / 000001.SZ / 830799.BJ。"""
from __future__ import annotations


def market_of(num6: str) -> str:
    """按 6 位数字代码前缀判断市场。"""
    if num6.startswith("6"):  # 60/68 主板、科创板
        return "SH"
    if num6.startswith(("0", "3")):  # 00/30 主板、创业板
        return "SZ"
    if num6.startswith(("4", "8")) or num6.startswith("92"):  # 43/83/87/88/92 北交所
        return "BJ"
    raise ValueError(f"无法判断市场（非A股代码范围）: {num6}")


def to_wind_code(code) -> str:
    """把 600000 / 600000.SH / sh600000 等输入统一成 600000.SH。"""
    s = str(code).strip().upper().replace("_", ".")
    if s[:2] in ("SH", "SZ", "BJ") and len(s) == 8:  # sh600000
        s = s[2:] + "." + s[:2]
    if "." in s:
        num, mkt = s.split(".", 1)
        if not num.isdigit() or mkt not in ("SH", "SZ", "BJ"):
            raise ValueError(f"无法识别的证券代码: {code!r}")
        return f"{int(num):06d}.{mkt}"
    if not s.isdigit():
        raise ValueError(f"无法识别的证券代码: {code!r}")
    num = s.zfill(6)
    if len(num) != 6:
        raise ValueError(f"证券代码应为6位数字: {code!r}")
    return f"{num}.{market_of(num)}"
