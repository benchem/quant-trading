"""个股情绪数据（东方财富股吧，经 AkShare）—— 落地 data/sentiment/{code}.json。

两个维度：

1. **个股评分历史**（``stock_comment_detail_zhpj_lspf_em``）：千股千评每日
   综合评分（0~100），连续序列，适合看情绪趋势；
2. **当日快照**（``stock_comment_em`` 全市场千股千评）：综合得分 / 关注指数 /
   机构参与度 / 目前排名 / 主力成本 —— 只取该股一行。

- 缓存：本地 JSON，TTL 30 分钟；评分历史增量合并（按 day 去重，旧->新）；
- 容错：网络失败回退旧缓存并标记 ``stale=True``。

注意口径：股吧情绪是**散户行为信号**，噪声大，只作辅助参考，不进任何买卖
规则（消费方：AI 上下文 + 前端展示，prompt 中明确标注其性质）。
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

CST = dt.timezone(dt.timedelta(hours=8))

#: 缓存 TTL（秒）
TTL = 30 * 60
#: 评分历史保留天数
KEEP_DAYS = 120


def _now() -> dict:
    return {"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "fetched_ts": time.time()}


def _cache_path(root: str | Path, code: str) -> Path:
    return Path(root) / "sentiment" / f"{code}.json"


def _load_cache(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "history" in data:
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
    code = str(code).upper().strip()
    sym = code.split(".", 1)[0]
    if code.startswith(("SH", "SZ", "BJ")) and len(code) == 8:
        sym = code[2:]
    if not sym.isdigit() or len(sym) != 6:
        raise ValueError(f"代码格式错误: {code}")
    return sym


def _f(v):
    try:
        if v is None or str(v) in ("-", ""):
            return None
        f = float(v)
        return round(f, 4) if f == f else None
    except (TypeError, ValueError):
        return None


def _fetch_history(sym: str) -> list[dict]:
    """千股千评个股评分历史（旧->新，按 day 去重）。"""
    import akshare as ak

    df = ak.stock_comment_detail_zhpj_lspf_em(symbol=sym)
    out: list[dict] = []
    for _, row in df.iterrows():
        d = row.get("交易日")
        day = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
        score = _f(row.get("评分"))
        if len(day) != 10 or score is None:
            continue
        out.append({"day": day, "score": score})
    out.sort(key=lambda x: x["day"])
    # 同日多条（数据源偶发重复）取最后一条
    dedup: dict[str, dict] = {}
    for r in out:
        dedup[r["day"]] = r
    return [dedup[k] for k in sorted(dedup)][-KEEP_DAYS:]


def _fetch_snapshot(sym: str) -> dict | None:
    """全市场千股千评快照中该股的一行（拉一次全表很重，仅在缓存过期时拉）。"""
    import akshare as ak

    df = ak.stock_comment_em()
    row = df[df["代码"].astype(str) == sym]
    if row.empty:
        return None
    r = row.iloc[0]
    d = r.get("交易日")
    return {
        "day": d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10],
        "score": _f(r.get("综合得分")),
        "attention": _f(r.get("关注指数")),
        "inst_participation": _f(r.get("机构参与度")),
        "rank": _f(r.get("目前排名")),
        "rank_change": _f(r.get("上升")),
        "main_cost": _f(r.get("主力成本")),
    }


def fetch(code: str, root: str | Path, refresh: bool = False) -> dict:
    """读取（或抓取）个股情绪数据。

    返回 {code, history, snapshot, fetched_at, stale, parts}：
    - history: [{day, score}] 评分序列（旧->新）；
    - snapshot: 当日千股千评快照（可能为 None：该股不在当日榜单）；
    - parts: 各部分抓取成功与否（单部分失败不阻塞整体）。
    """
    code = str(code).upper()
    sym = _norm_symbol(code)
    path = _cache_path(root, code)
    now = _now()
    cached = _load_cache(path)

    if (not refresh and cached
            and time.time() - cached.get("fetched_ts", 0) <= TTL):
        return dict(cached, stale=False)

    parts: dict[str, str] = {}
    history = None
    snapshot = None
    try:
        history = _fetch_history(sym)
    except ImportError as e:
        raise RuntimeError(f"akshare 未安装: {e}") from None
    except Exception as e:  # noqa: BLE001 单部分失败降级
        parts["history"] = str(e)[:120]
    try:
        snapshot = _fetch_snapshot(sym)
    except Exception as e:  # noqa: BLE001
        parts["snapshot"] = str(e)[:120]

    if history is None and snapshot is None:
        if cached and (cached.get("history") or cached.get("snapshot")):
            return dict(cached, stale=True, parts=parts,
                        error="抓取失败，回退旧缓存")
        raise RuntimeError(
            f"情绪数据抓取失败: {parts.get('history') or parts.get('snapshot')}")

    # 增量合并：历史与旧缓存按 day 去重（新值覆盖旧值）
    if history is not None and cached:
        old = {h.get("day"): h for h in cached.get("history") or []}
        old.update({h["day"]: h for h in history})
        history = [old[k] for k in sorted(old)][-KEEP_DAYS:]

    payload = {"code": code, "history": history or [], "snapshot": snapshot,
               **now, **({"parts": parts} if parts else {})}
    _save_cache(path, payload)
    return dict(payload, stale=False)


def latest_metrics(code: str, root: str | Path, days: int = 10) -> dict:
    """近 N 日情绪摘要（AI 上下文用：评分趋势 + 快照关键值）。"""
    data = fetch(code, root)
    hist = (data.get("history") or [])[-days:]
    snap = data.get("snapshot")
    out: dict = {"available": bool(hist or snap),
                 "stale": bool(data.get("stale"))}
    if hist:
        scores = [h["score"] for h in hist if h.get("score") is not None]
        out.update({
            "n_days": len(hist),
            "last_day": hist[-1]["day"],
            "last_score": hist[-1].get("score"),
            "score_mean": round(sum(scores) / len(scores), 2) if scores else None,
            "score_chg_5d": (round(scores[-1] - scores[-6], 2)
                             if len(scores) >= 6 else None),
        })
    if snap:
        out["snapshot"] = {k: snap.get(k) for k in
                           ("day", "score", "attention", "inst_participation",
                            "rank", "rank_change")}
    return out
