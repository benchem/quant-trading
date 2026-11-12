"""大盘复盘日报（纯规则版，吸收自 DSA daily_stock_analysis 的 market_review）。

DSA 侧 LLM 市场复盘无法直接搬（依赖 akshare 全家桶 + LLM 链路），本模块
吸收其**确定性资产**：Market Light 三维评分（market_analyzer.py:1517-1576
公式逐行照抄：宽度 0.45 + 指数 0.35 + 涨跌停 0.20，status 分界 60/40）与
报告章节结构（指数 / 涨跌家数 / 涨跌停 / 板块领涨领跌 / 附注）。

数据面全部走本地 parquet，不依赖 LLM、不新增依赖：
- 指数    data/dataset/index_daily.parquet（源 pct_chg 几乎全 NaN，close/prev 自算）
- 涨跌家数 DataStore.market_daily（含 partial 标记与假成交额钳制，口径同市场页）
- 涨跌停  dataset/daily_hfq.parquet hfq 收益率阈值法（涨停日无分红 → hfq 收益
          ≈真实收益，阈值 ×0.995 容差吸收因子噪声）；ST 按股票名判定
- 板块    DataStore.board_rank（成分股等权，口径同板块页）

由 EodUpdater 在「缓存刷新」与「信号扫描」之间挂载；产物落
docs/reports/YYYY-MM-DD_市场日报.md 并推送站内信（无 code → 全体用户）。
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

WEEKDAY_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 指数展示顺序（index_daily.parquet 现有 7 只；缺哪只跳哪只）
INDEX_ORDER = (
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("000300.SH", "沪深300"),
    ("000905.SH", "中证500"),
    ("000852.SH", "中证1000"),
    ("000688.SH", "科创50"),
)

# 涨跌幅限制（按代码前缀；ST 覆盖为 5%）。北交所 30%。
# 新股上市初期无涨跌幅限制（主板/创科前 5 日），阈值法会将其计入涨跌停，
# 属已知局限，见 _limit_stats 注释。
_LIMIT_RULES = (
    ("688", 0.20), ("689", 0.20),          # 科创板
    ("300", 0.20), ("301", 0.20),          # 创业板
    ("43", 0.30), ("83", 0.30), ("87", 0.30),
    ("88", 0.30), ("92", 0.30),            # 北交所
)
_DEFAULT_LIMIT = 0.10
_LIMIT_TOL = 0.995  # 阈值容差：|ret| >= limit*0.995 即判停板

# Market Light（DSA market_analyzer.py:1517-1576 原式）
_BREADTH_W, _INDEX_W, _LIMIT_W = 0.45, 0.35, 0.20
_INDEX_SCORE_SENS = 12  # index_score = clamp(50 + avg_chg*12, 0, 100)


def _limit_pct_of(code: str, name: str) -> float:
    c = str(code)
    if "ST" in str(name).upper():
        return 0.05
    for prefix, pct in _LIMIT_RULES:
        if c.startswith(prefix):
            return pct
    return _DEFAULT_LIMIT


def _index_rows(store, day: dt.date) -> list[dict]:
    """指数当日涨跌（close/prev 自算，源列 NaN）+ 近 5 日累计。"""
    p = store.root / "dataset" / "index_daily.parquet"
    if not p.exists():
        return []
    t = pd.read_parquet(p, columns=["date", "code", "close"])
    t["date"] = pd.to_datetime(t["date"]).dt.date
    t = t[t["date"] <= day].sort_values(["code", "date"])
    rows = []
    for code, label in INDEX_ORDER:
        g = t[t["code"] == code]
        if len(g) < 2:
            continue
        closes = g["close"].astype(float).tolist()
        dates = g["date"].tolist()
        if dates[-1] != day:  # 当日无 bar（停发/缺数）→ 跳过
            continue
        chg1 = (closes[-1] / closes[-2] - 1.0) * 100.0
        chg5 = (closes[-1] / closes[-6] - 1.0) * 100.0 if len(closes) >= 6 else None
        rows.append({"code": code, "name": label, "close": closes[-1],
                     "chg1": chg1, "chg5": chg5})
    return rows


def _market_overview(store, day: dt.date) -> dict:
    """涨跌家数/成交额（复用 market 聚合，含 partial 与假成交额钳制）。"""
    m = getattr(store, "market", None)
    day = pd.Timestamp(day).normalize()
    if m is None or m.empty or day not in m.index:
        return {}
    r = m.loc[day]
    return {
        "adv": int(r["adv"]), "dec": int(r["dec"]), "flat": int(r["flat"]),
        "n_codes": int(r["n_codes"]), "mean_ret": float(r["mean_ret"]),
        "med_ret": float(r["med_ret"]),
        "amount_yi": None if pd.isna(r["amount_yi"]) else float(r["amount_yi"]),
        "partial": bool(r["partial"]),
    }


_LIMIT_RET_CACHE: dict[tuple, pd.DataFrame | None] = {}


def _stock_day_ret(store, day: dt.date) -> pd.DataFrame | None:
    """全市场逐股当日收益（hfq close/prev），带 (root, day) 缓存。

    涨跌停与板块排行共用：一次读 daily_hfq（1700 万行 3 列，~10s，仅
    EOD 一次）取代 DataStore._rank_table——后者单日区间首=末恒为 0。
    """
    key = (str(store.root), day)
    if key in _LIMIT_RET_CACHE:
        return _LIMIT_RET_CACHE[key]
    p = store.root / "dataset" / "daily_hfq.parquet"
    if not p.exists():
        _LIMIT_RET_CACHE[key] = None
        return None
    t = pd.read_parquet(p, columns=["date", "code", "close"])
    t["date"] = pd.to_datetime(t["date"]).dt.date
    t = t[t["date"] <= day].sort_values(["code", "date"])
    t = t.groupby("code", sort=False).tail(2)
    out = None
    if len(t) and t["date"].max() == day:
        prev = t.groupby("code", sort=False)["close"].shift(1)
        t = t[t["date"] == day].assign(prev=prev)
        t = t.dropna(subset=["prev"])
        t = t[t["prev"] > 0].copy()
        t["ret"] = t["close"] / t["prev"] - 1.0
        out = t[["code", "ret"]].set_index("code")
    if len(_LIMIT_RET_CACHE) > 4:  # 防 EOD 跨日累积
        _LIMIT_RET_CACHE.clear()
    _LIMIT_RET_CACHE[key] = out
    return out


def _limit_stats(store, day: dt.date) -> dict:
    """涨跌停统计（hfq 收益率阈值法）。

    需当日与前一根 bar（计算收益）；新股上市初期无涨跌幅限制，其巨幅涨跌
    会计入停板数——用「收盘相对发行价」不可得，MVP 先按「收益 > +30%（非
    北交所）剔除」的近似护栏排除无限制期新股，剩余误差在附注中声明。
    """
    t = _stock_day_ret(store, day)
    if t is None or t.empty:
        return {}
    basic = getattr(store, "basic", None)
    names = basic["name"].astype(str) if basic is not None and "name" in basic else None

    ups, downs, up_rows, down_rows = 0, 0, [], []
    for code, r in t.iterrows():
        code = str(code)
        name = str(names.get(code, "")) if names is not None else ""
        lim = _limit_pct_of(code, name)
        ret = float(r["ret"])
        if lim < 0.30 and abs(ret) > 0.30:  # 无限制期新股护栏（非北交所）
            continue
        if ret >= lim * _LIMIT_TOL:
            ups += 1
            up_rows.append((code, name, ret))
        elif ret <= -lim * _LIMIT_TOL:
            downs += 1
            down_rows.append((code, name, ret))
    up_rows.sort(key=lambda x: -x[2])
    down_rows.sort(key=lambda x: x[2])
    return {"limit_up": ups, "limit_down": downs,
            "up_rows": up_rows, "down_rows": down_rows,
            "n_codes": int(len(t))}


def market_light(ov: dict, lim: dict, idx_rows: list[dict]) -> dict:
    """Market Light 三维评分（DSA 原式照抄；缺维度计 50 分并标记降级）。"""
    participants = ov.get("adv", 0) + ov.get("dec", 0)
    breadth_ok = participants > 0
    breadth = int(ov["adv"] / participants * 100) if breadth_ok else 50

    chgs = [r["chg1"] for r in idx_rows]
    index_ok = bool(chgs)
    avg_chg = sum(chgs) / len(chgs) if index_ok else 0.0
    index_score = int(max(0, min(100, 50 + avg_chg * _INDEX_SCORE_SENS)))

    lu, ld = lim.get("limit_up", 0), lim.get("limit_down", 0)
    limit_ok = (lu + ld) > 0
    limit_score = int(lu / (lu + ld) * 100) if limit_ok else 50

    score = int(round(breadth * _BREADTH_W + index_score * _INDEX_W
                      + limit_score * _LIMIT_W))
    status = "green" if score >= 60 else ("yellow" if score >= 40 else "red")
    label = {"green": "可进攻", "yellow": "需观察", "red": "偏防守"}[status]
    temp = ("强势" if score >= 70 else "偏暖" if score >= 55
            else "震荡" if score >= 40 else "偏弱")
    quality = ("unavailable" if not index_ok
               else "ok" if (breadth_ok and limit_ok) else "partial")
    return {"score": score, "status": status, "label": label, "temp": temp,
            "breadth": breadth, "index": index_score, "limit": limit_score,
            "avg_chg": avg_chg, "quality": quality,
            "dims_ok": {"breadth": breadth_ok, "index": index_ok,
                        "limit": limit_ok}}


def _light_reasons(ov: dict, lim: dict, idx_rows: list[dict], light: dict) -> list[str]:
    """理由话术（DSA _build_market_light_reasons_zh 同构）。"""
    reasons = []
    adv, dec = ov.get("adv", 0), ov.get("dec", 0)
    if adv + dec:
        ratio = adv / (adv + dec)
        if ratio >= 0.6:
            reasons.append(f"上涨家数占比 {ratio:.0%}，赚钱效应扩散")
        elif ratio <= 0.4:
            reasons.append(f"上涨家数占比 {ratio:.0%}，亏钱效应较强")
        else:
            reasons.append(f"上涨家数占比 {ratio:.0%}，市场分化")
    if light["dims_ok"]["index"]:
        reasons.append(f"主要指数平均涨跌幅 {light['avg_chg']:+.2f}%")
    if lim.get("limit_up") or lim.get("limit_down"):
        reasons.append(f"涨跌停差 {lim['limit_up'] - lim['limit_down']:+d}")
    amt = ov.get("amount_yi")
    if amt:
        scale = ("放量" if amt > 15000 else "缩量" if amt < 9000 else "量能平稳")
        reasons.append(f"成交额 {amt:,.0f} 亿，{scale}")
    if not reasons:
        reasons.append("结构化涨跌数据有限，按可用行情综合判断")
    return reasons[:4]


def _board_rank_day(store, day: dt.date, board_type: str = "industry",
                    min_stocks: int = 5) -> list[tuple[str, float, int]]:
    """单日板块等权排行（成分股当日收益均值）。

    DataStore.board_rank 有「区间≥5 交易日」的最低样本门槛（_need_days
    max(5, ...)）且 _rank_table 单日区间收益恒为 0，故复用 _stock_day_ret
    的逐股当日收益 + 板块成分自算；口径（等权/min_stocks）与 board_rank 一致。
    """
    members = getattr(store, "_board_members_of", lambda *_: None)(board_type)
    if members is None or members.empty:
        return []
    rets = _stock_day_ret(store, day)
    if rets is None or rets.empty:
        return []
    t = rets.join(members[["board_code"]], how="inner").dropna(subset=["ret"])
    if t.empty:
        return []
    grp = t.groupby("board_code")
    agg = pd.DataFrame({"ret": grp["ret"].mean(), "n": grp.size()})
    agg = agg[agg["n"] >= min_stocks].sort_values("ret", ascending=False)
    board_names = members.groupby("board_code")["board_name"].first()
    return [(str(board_names.get(bc, bc)), float(r["ret"]), int(r["n"]))
            for bc, r in agg.iterrows()]


def build_market_report(store, day: dt.date) -> str:
    """渲染当日日报 markdown。数据缺失的章节自动降级，不抛异常。"""
    idx_rows = _index_rows(store, day)
    ov = _market_overview(store, day)
    lim = _limit_stats(store, day)
    light = market_light(ov, lim, idx_rows)
    reasons = _light_reasons(ov, lim, idx_rows, light)
    wd = WEEKDAY_ZH[day.weekday()]

    L: list[str] = []
    L.append(f"# 大盘复盘日报 {day.isoformat()}（{wd}）")
    L.append("")
    L.append(f"**市场信号：{light['label']}（{light['status']}）· 温度 {light['temp']} · "
             f"评分 {light['score']}/100**")
    L.append("")
    L.append("> " + "；".join(reasons))
    L.append("")

    # 一、指数
    L.append("## 一、指数表现")
    if idx_rows:
        L.append("")
        L.append("| 指数 | 收盘 | 当日 | 近5日 |")
        L.append("|---|---:|---:|---:|")
        for r in idx_rows:
            c5 = f"{r['chg5']:+.2f}%" if r["chg5"] is not None else "—"
            L.append(f"| {r['name']} | {r['close']:.2f} | {r['chg1']:+.2f}% | {c5} |")
    else:
        L.append("- 指数数据缺失")
    L.append("")

    # 二、市场概况
    L.append("## 二、市场概况")
    if ov:
        amt = f"{ov['amount_yi']:,.0f} 亿" if ov.get("amount_yi") else "—"
        mean_pct = f"{ov['mean_ret']*100:+.2f}%" if pd.notna(ov["mean_ret"]) else "—"
        med_pct = f"{ov['med_ret']*100:+.2f}%" if pd.notna(ov["med_ret"]) else "—"
        L.append(f"- 家数：涨 **{ov['adv']}** / 平 {ov['flat']} / 跌 **{ov['dec']}**"
                 f"（覆盖 {ov['n_codes']} 只）")
        L.append(f"- 全市场成交额：**{amt}**；等权收益均值 {mean_pct}、中位数 {med_pct}")
        if ov.get("partial"):
            L.append("- ⚠️ 当日为部分刷新数据，涨跌家数/成交额口径不完整")
    else:
        L.append("- 市场聚合数据缺失")
    L.append("")

    # 三、涨跌停
    L.append("## 三、涨跌停")
    if lim:
        L.append(f"- 涨停 **{lim['limit_up']}** 家，跌停 **{lim['limit_down']}** 家"
                 f"（阈值法，覆盖 {lim['n_codes']} 只）")
        if lim["up_rows"]:
            tops = lim["up_rows"][:20]
            cells = "、".join(f"{n or c}({r*100:+.1f}%)" for c, n, r in tops)
            more = f" …等 {lim['limit_up']} 家" if lim["limit_up"] > 20 else ""
            L.append(f"- 涨停（前 20）：{cells}{more}")
        if lim["down_rows"]:
            tops = lim["down_rows"][:10]
            cells = "、".join(f"{n or c}({r*100:+.1f}%)" for c, n, r in tops)
            more = f" …等 {lim['limit_down']} 家" if lim["limit_down"] > 10 else ""
            L.append(f"- 跌停（前 10）：{cells}{more}")
    else:
        L.append("- 涨跌停数据缺失")
    L.append("")

    # 四、板块
    L.append("## 四、板块领涨领跌")
    try:
        boards = _board_rank_day(store, day)
    except Exception:  # noqa: BLE001
        boards = []
    if boards:
        def _fmt(rows):
            return "、".join(f"{n}({r*100:+.2f}%, {k}只)" for n, r, k in rows)
        L.append(f"- 领涨（前 8）：{_fmt(boards[:8])}")
        if len(boards) > 8:
            L.append(f"- 领跌（后 3）：{_fmt(boards[-3:])}")
        L.append(f"- 共 {len(boards)} 个行业板块参与统计（成分股当日等权，板块成分≥5 只）")
    else:
        L.append("- 板块数据缺失（需板块成分已刷新）")
    L.append("")

    # 附注（口径声明）
    L.append("## 附注")
    L.append("- 本报告为**纯规则版**（无 LLM 参与见解生成），Market Light 评分公式"
             "吸收自 daily_stock_analysis（宽度 0.45/指数 0.35/涨跌停 0.20，"
             "≥60 可进攻 / ≥40 需观察 / <40 偏防守）。")
    L.append("- 涨跌幅为后复权收益率口径；涨跌停按收益阈值（±容差 0.5%）近似判定，"
             "ST 5%/创科 20%/北交 30%/其余 10%；上市初期无涨跌幅限制的新股可能误计。")
    if not light["dims_ok"]["limit"]:
        L.append("- 涨跌停维度今日不可用，评分中该维按 50 分中性计。")
    L.append("- 指数涨跌幅由收盘价自算（数据源 pct 列缺值）。")
    return "\n".join(L)


def emit_market_report(store, day: dt.date, hub=None,
                       out_dir: Path | None = None) -> Path:
    """渲染 + 落盘 docs/reports/ + 推送站内信（系统消息，全体用户可见）。"""
    md = build_market_report(store, day)
    if out_dir is None:
        out_dir = Path(store.root).parent / "docs" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{day.isoformat()}_市场日报.md"
    path.write_text(md, encoding="utf-8")
    if hub is not None:
        try:
            light = None
            # 摘要行取评分头（简单解析，避免重复计算）
            for ln in md.splitlines():
                if ln.startswith("**市场信号"):
                    light = ln.strip("*")
                    break
            hub.push(key=f"market_report:{day.isoformat()}",
                     text=f"📊 {light or '大盘复盘日报已生成'}（详见 docs/reports/）",
                     level="info", desktop=True, source="system")
        except Exception:  # noqa: BLE001 推送失败不影响落盘
            pass
    return path
