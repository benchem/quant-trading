"""个股公告（巨潮资讯 cninfo，经 AkShare）—— 落地 data/announce/{code}.json。

- 数据：公告标题/时间/链接（cninfo 官方披露渠道，权威且稳定）；
- 拉取窗口：近 ``WINDOW_DAYS``（默认 180）天，全类别；
- 缓存：本地 JSON，TTL 30 分钟；增量合并按 (day, title) 去重；
- 容错：网络失败回退旧缓存并标记 ``stale=True``（与 news/valuation 同语义）。

AkShare 接口 ``stock_zh_a_disclosure_report_cninfo``（category="" 全类别）。
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

CST = dt.timezone(dt.timedelta(hours=8))

#: 缓存 TTL（秒）
TTL = 30 * 60
#: 拉取窗口（天）
WINDOW_DAYS = 180
#: 本地保留条数上限
KEEP_ITEMS = 200


def _now() -> dict:
    return {"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "fetched_ts": time.time()}


def _cache_path(root: str | Path, code: str) -> Path:
    return Path(root) / "announce" / f"{code}.json"


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


def _norm_symbol(code: str) -> str:
    """600519.SH -> '600519'（cninfo 只认 6 位数字代码）。"""
    code = str(code).upper().strip()
    sym = code.split(".", 1)[0]
    if code.startswith(("SH", "SZ", "BJ")) and len(code) == 8:
        sym = code[2:]
    if not sym.isdigit() or len(sym) != 6:
        raise ValueError(f"代码格式错误: {code}")
    return sym


def _fetch(sym: str) -> list[dict]:
    import akshare as ak  # 延迟导入（缺失时对应功能降级报错）

    end = dt.date.today()
    start = end - dt.timedelta(days=WINDOW_DAYS)
    df = ak.stock_zh_a_disclosure_report_cninfo(
        symbol=sym, market="沪深京", category="",
        start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
    items: list[dict] = []
    for _, row in df.iterrows():
        title = str(row.get("公告标题") or "").strip()
        t = row.get("公告时间")
        day = t.strftime("%Y-%m-%d") if hasattr(t, "strftime") else str(t)[:10]
        if not title or len(day) != 10:
            continue
        items.append({
            "day": day,
            "title": title[:160],
            "url": str(row.get("公告链接") or "")[:300],
        })
    # 新->旧（巨潮返回的就是新的在前）；同日多条保持原序
    items.sort(key=lambda x: x["day"], reverse=True)
    return items[:KEEP_ITEMS]


def fetch(code: str, root: str | Path, refresh: bool = False) -> dict:
    """读取（或抓取）个股公告，返回 {code, items, fetched_at, stale}。"""
    code = str(code).upper()
    sym = _norm_symbol(code)
    path = _cache_path(root, code)
    now = _now()
    cached = _load_cache(path)

    if (not refresh and cached
            and time.time() - cached.get("fetched_ts", 0) <= TTL):
        return dict(cached, stale=False)

    try:
        items = _fetch(sym)
    except ImportError as e:
        raise RuntimeError(f"akshare 未安装: {e}") from None
    except Exception as e:  # noqa: BLE001
        if cached and cached.get("items"):
            return dict(cached, stale=True,
                        error=f"抓取失败，回退旧缓存: {str(e)[:120]}")
        raise RuntimeError(f"公告抓取失败: {str(e)[:200]}") from None

    payload = {"code": code, "items": items, **now}
    _save_cache(path, payload)
    return dict(payload, stale=False)


def latest_titles(code: str, root: str | Path, limit: int = 10) -> dict:
    """近期待办公告标题列表（AI 上下文用：只送标题，不送正文——与新闻同原则）。"""
    data = fetch(code, root)
    items = (data.get("items") or [])[:limit]
    return {
        "available": bool(items),
        "stale": bool(data.get("stale")),
        "n_total": len(data.get("items") or []),
        "items": [{"day": i["day"], "title": i["title"]} for i in items],
    }
