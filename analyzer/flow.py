"""个股资金流（东方财富，经 AkShare）—— 落地 data/flow/{code}.json。

- 数据：每日主力/超大单/大单/中单/小单 净流入额与净占比、收盘价、涨跌幅；
- 缓存：本地 JSON（当日 + 近 N 日），TTL 30 分钟（与新闻/估值同策略）；
- 增量：拉取全量历史后按 (day) 截取保留最近 ``KEEP_DAYS`` 日；
- 容错：网络失败回退旧缓存并标记 ``stale=True``（与 news/valuation 同语义）。

AkShare 接口 ``stock_individual_fund_flow``（东财 push2his fflow/daykline）。
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

CST = dt.timezone(dt.timedelta(hours=8))

#: 缓存 TTL（秒）
TTL = 30 * 60
#: 本地保留天数
KEEP_DAYS = 120


def _now() -> dict:
    return {"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "fetched_ts": time.time()}


def _cache_path(root: str | Path, code: str) -> Path:
    return Path(root) / "flow" / f"{code}.json"


def _load_cache(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "items" in data:
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


def _save_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _norm_code(code: str) -> tuple[str, str]:
    """600519.SH -> ('600519', 'sh')；兼容 600519 / sh600519 写法。"""
    code = str(code).upper().strip()
    if "." in code:
        sym, suf = code.split(".", 1)
        suf = suf.lower()
    elif len(code) == 8 and code[:2] in ("SH", "SZ", "BJ"):
        sym, suf = code[2:], code[:2].lower()
    else:
        sym, suf = code, ""
    if not suf:
        if sym.startswith(("6",)):
            suf = "sh"
        elif sym.startswith(("0", "3")):
            suf = "sz"
        elif sym.startswith(("4", "8", "9")):
            suf = "bj"
        else:
            raise ValueError(f"无法识别市场: {code}")
    if suf not in ("sh", "sz", "bj") or not sym.isdigit() or len(sym) != 6:
        raise ValueError(f"代码格式错误: {code}")
    return sym, suf


def _fetch(sym: str, market: str, retries: int = 2) -> list[dict]:
    """调 AkShare 拉全量历史，规范化为 items（旧->新）。

    东财 push2his 偶发瞬断（RemoteDisconnected），akshare 内部不重试，
    这里补一层指数退避（与 downloader 的容错风格一致）。
    """
    import time as _time

    import akshare as ak  # 延迟导入（缺失时对应功能降级报错）

    last_err: Exception | None = None
    df = None
    for attempt in range(retries + 1):
        try:
            df = ak.stock_individual_fund_flow(stock=sym, market=market)
            break
        except Exception as e:  # noqa: BLE001 瞬断重试
            last_err = e
            if attempt < retries:
                _time.sleep(1.5 * (attempt + 1))
    if df is None:
        raise last_err  # type: ignore[misc]
    items: list[dict] = []
    for _, row in df.iterrows():
        d = row.get("日期")
        day = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
        close = row.get("收盘价")
        if close is None or str(close) in ("-", "", "nan"):
            continue  # 停牌/无行情日
        items.append({
            "day": day,
            "close": round(float(close), 4),
            "pct_chg": _f(row.get("涨跌幅")),
            "main_net": _f(row.get("主力净流入-净额")),
            "main_pct": _f(row.get("主力净流入-净占比")),
            "super_net": _f(row.get("超大单净流入-净额")),
            "super_pct": _f(row.get("超大单净占比")),
            "big_net": _f(row.get("大单净流入-净额")),
            "big_pct": _f(row.get("大单净占比")),
            "mid_net": _f(row.get("中单净流入-净额")),
            "mid_pct": _f(row.get("中单净占比")),
            "small_net": _f(row.get("小单净流入-净额")),
            "small_pct": _f(row.get("小单净占比")),
        })
    items.sort(key=lambda x: x["day"])
    return items[-KEEP_DAYS:]


def _f(v):
    """安全浮点转换（'-' / NaN / None -> None）。"""
    try:
        if v is None or str(v) in ("-", ""):
            return None
        f = float(v)
        return round(f, 4) if f == f else None  # NaN 检查
    except (TypeError, ValueError):
        return None


def fetch(code: str, root: str | Path, refresh: bool = False,
          keep_days: int = KEEP_DAYS) -> dict:
    """读取（或抓取）个股资金流，返回 {code, items, fetched_at, stale}。

    - 本地缓存未过期且非 refresh -> 直接返回；
    - 抓取失败 -> 回退旧缓存（stale=True）；旧缓存也没有 -> 抛 RuntimeError。
    """
    code = str(code).upper()
    sym, market = _norm_code(code)
    path = _cache_path(root, code)
    now = _now()
    cached = _load_cache(path)

    if (not refresh and cached
            and time.time() - cached.get("fetched_ts", 0) <= TTL):
        out = dict(cached)
        out["stale"] = False
        return out

    try:
        items = _fetch(sym, market)
    except ImportError as e:
        raise RuntimeError(f"akshare 未安装: {e}") from None
    except Exception as e:  # noqa: BLE001 网络失败回退旧缓存
        if cached and cached.get("items"):
            out = dict(cached)
            out["stale"] = True
            out["error"] = f"抓取失败，回退旧缓存: {str(e)[:120]}"
            return out
        raise RuntimeError(f"资金流抓取失败: {str(e)[:200]}") from None

    if keep_days != KEEP_DAYS:
        items = items[-keep_days:]
    payload = {"code": code, "items": items, **now}
    _save_cache(path, payload)
    return dict(payload, stale=False)


def latest_metrics(code: str, root: str | Path, days: int = 5) -> dict:
    """近 N 日主力净流入摘要（AI 上下文用：轻量、只留汇总统计）。"""
    data = fetch(code, root)
    items = (data.get("items") or [])[-days:]
    if not items:
        return {"available": False, "stale": data.get("stale", False)}
    main_nets = [i["main_net"] for i in items if i.get("main_net") is not None]
    out = {
        "available": True,
        "stale": bool(data.get("stale")),
        "last_day": items[-1]["day"],
        "n_days": len(items),
        "main_net_sum": round(sum(main_nets), 0) if main_nets else None,
        "main_net_days_inflow": sum(1 for v in main_nets if v > 0),
        "main_net_days_outflow": sum(1 for v in main_nets if v < 0),
        "last": {k: items[-1].get(k) for k in
                 ("day", "close", "pct_chg", "main_net", "main_pct", "super_pct")},
    }
    return out
