"""个股新闻：抓取、解析、本地缓存。

数据源: 东方财富个股新闻（akshare ``stock_news_em``，最近 100 条，
含标题/摘要/来源/链接）。结果缓存到 ``data/news/{code}.json``，
TTL 内直接读缓存（零网络请求）；网络失败时回退旧缓存并标记 ``stale=True``。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

TTL_SECONDS = 30 * 60  # 缓存 30 分钟
MAX_ITEMS = 20
_DIGEST_LEN = 150


class NewsError(RuntimeError):
    """新闻抓取失败（网络/源不可用/无数据）。"""


def _load_cache(f: Path) -> dict | None:
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("items"), list):
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
    """获取单只股票的最新新闻。

    返回 ``{code, updated, updated_ts, stale, items:[{title,time,source,url,digest}]}``。

    - TTL 内且非 refresh：直接返回缓存（``stale=False``）；
    - 抓取成功：写缓存并返回；
    - 抓取失败：有旧缓存则返回旧缓存（``stale=True``），否则抛 :class:`NewsError`。
    """
    code = code.upper()
    f = Path(data_dir) / "news" / f"{code}.json"
    cached = _load_cache(f)
    now = time.time()

    fresh = cached and cached.get("updated_ts") and (now - cached["updated_ts"]) <= ttl
    if fresh and not refresh:
        out = dict(cached)
        out["stale"] = False
        return out

    symbol = code.split(".")[0]
    try:
        import akshare as ak  # 延迟导入（仅抓取时需要）

        raw = ak.stock_news_em(symbol=symbol)
        if raw is None or raw.empty:
            raise NewsError(f"{code}: 东方财富未返回新闻")
        items: list[dict] = []
        for _, r in raw.head(MAX_ITEMS).iterrows():
            title = str(r.get("新闻标题") or "").strip()
            if not title or title == "False":
                continue
            url = str(r.get("新闻链接") or "").strip()
            content = str(r.get("新闻内容") or "").strip()
            items.append(
                {
                    "title": title,
                    "time": str(r.get("发布时间") or "").strip()[:16],
                    "source": str(r.get("文章来源") or "").strip(),
                    "url": url if url and url != "False" else "",
                    "digest": content[:_DIGEST_LEN],
                }
            )
        if not items:
            raise NewsError(f"{code}: 未解析到有效新闻")
        payload = {
            "code": code,
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_ts": now,
            "stale": False,
            "items": items,
        }
        _save_cache(f, payload)
        return payload
    except NewsError as e:
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
        raise NewsError(f"{code}: 抓取失败 {type(e).__name__}: {str(e)[:80]}") from e
