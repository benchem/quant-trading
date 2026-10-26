"""个股估值：PE(TTM)/PE(静)/PB/PEG/PS/市现率 + 股本/市值（东财估值分析，本地缓存）。

数据源: 东方财富数据中心-估值分析（akshare ``stock_value_em``，每日快照序列，
取最新一行）。结果缓存到 ``data/valuation/{code}.json``，TTL 内直接读缓存
（零网络请求）；网络失败时回退旧缓存并标记 ``stale=True``。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

TTL_SECONDS = 30 * 60  # 缓存 30 分钟

#: 东财源列 -> 内部字段
FIELDS = {
    "当日收盘价": "price",
    "总市值": "total_mv",
    "流通市值": "float_mv",
    "总股本": "total_shares",
    "流通股本": "float_shares",
    "PE(TTM)": "pe_ttm",
    "PE(静)": "pe_static",
    "市净率": "pb",
    "PEG值": "peg",
    "市销率": "ps",
    "市现率": "pcf",
}


class ValuationError(RuntimeError):
    """估值数据抓取失败（网络/源不可用/无数据）。"""


def _num(v):
    try:
        if v is None or (isinstance(v, float) and v != v):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_cache(f: Path) -> dict | None:
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("code"):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


def _save_cache(f: Path, payload: dict) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(f)


def fetch(code: str, data_dir: str | Path = "data",
          ttl: float = TTL_SECONDS, refresh: bool = False) -> dict:
    """获取单只股票的最新估值快照。

    返回 ``{code, date, price, total_mv, float_mv, total_shares, float_shares,
    pe_ttm, pe_static, pb, peg, ps, pcf, updated, updated_ts, stale}``。

    - TTL 内且非 refresh：直接返回缓存（``stale=False``）；
    - 抓取成功：写缓存并返回；
    - 抓取失败：有旧缓存则返回旧缓存（``stale=True``），否则抛 :class:`ValuationError`。
    """
    code = code.upper()
    f = Path(data_dir) / "valuation" / f"{code}.json"
    cached = _load_cache(f)
    now = time.time()

    if cached and (now - cached.get("updated_ts", 0)) <= ttl and not refresh:
        out = dict(cached)
        out["stale"] = False
        return out

    symbol = code.split(".")[0]
    try:
        import akshare as ak  # 延迟导入（仅抓取时需要）

        raw = ak.stock_value_em(symbol=symbol)
        if raw is None or raw.empty:
            raise ValuationError(f"{code}: 东方财富未返回估值数据")
        last = raw.iloc[-1]
        payload: dict = {
            "code": code,
            "date": str(last.get("数据日期"))[:10],
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_ts": now,
            "stale": False,
        }
        for src, dst in FIELDS.items():
            payload[dst] = _num(last.get(src))
        _save_cache(f, payload)
        return payload
    except ValuationError as e:
        if cached:
            out = dict(cached)
            out["stale"] = True
            return out
        raise
    except Exception as e:  # noqa: BLE001
        if cached:
            out = dict(cached)
            out["stale"] = True
            return out
        raise ValuationError(f"{code}: 抓取失败 {type(e).__name__}: {str(e)[:80]}") from e
