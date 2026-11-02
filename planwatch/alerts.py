"""提醒规则引擎：价格位 + T 信号（当日去重 + 冷却重报）。时间规则在 cli 的时间钩子里。"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .common import WATCHLOG, ensure_dir, now_cst

FIRED = "alert_fired.json"


@dataclass
class Alert:
    key: str
    level: str  # info / warn / danger / target
    text: str
    sound: bool = False
    code: str = ""  # 关联股票代码（写入统一收件箱时定位个股）


def _load_fired() -> dict:
    p = WATCHLOG / FIRED
    return json.loads(p.read_text("utf-8")) if p.exists() else {}


def _save_fired(f: dict) -> None:
    ensure_dir()
    (WATCHLOG / FIRED).write_text(json.dumps(f, ensure_ascii=False, indent=1), "utf-8")


def _can_fire(fired: dict, key: str, cond: bool, price: float, day: str, cool: int) -> bool:
    """当日已报则抑制；条件消失并持续 cool 个 tick 后允许重报（防抖动）。"""
    rec = fired.get(key)
    if rec and rec.get("day") == day and rec.get("fired"):
        if not cond:
            rec["cool"] = rec.get("cool", cool) - 1
            if rec["cool"] <= 0:
                rec["fired"] = False
                rec["cool"] = cool
        return rec["fired"] is False
    return True


def evaluate(q: dict, plan: dict, book: dict, allow_t: bool = True) -> list[Alert]:
    L = plan["levels"]
    P = q["price"]
    day = now_cst().strftime("%Y-%m-%d")
    fired = _load_fired()
    out: list[Alert] = []

    def emit(key: str, level: str, text: str, cond: bool, sound: bool = False, cool: int = 10) -> None:
        if cond and _can_fire(fired, key, cond, P, day, cool):
            fired[key] = {"day": day, "fired": True, "price": P, "cool": cool}
            out.append(Alert(key, level, text, sound, code=plan.get("code", "")))

    pending = set(book["pending"])
    # --- 建仓批次（股数/状态取自 plan json，不硬编码；killed 批次不提醒） ---
    tr_map = {t["id"]: t for t in plan["tranches"]}
    if "T1" in pending and tr_map.get("T1", {}).get("status") != "killed":
        z = L["t1_zone"]
        emit("t1", "target",
             f"🟢 T1 入场窗口 {z[0]}~{z[1]}（{tr_map['T1']['shares']} 股）",
             z[0] <= P <= z[1], cool=5)
    if "T2" in pending and tr_map.get("T2", {}).get("status") != "killed":
        z = L["t2_zone"]
        emit("t2", "target",
             f"🟢 T2 入场窗口 {z[0]}~{z[1]}（{tr_map['T2']['shares']} 股；v2 四条件缺一不可："
             f"未破 {z[0]} + 全天成交额≤3.2 亿 + 收盘≥26.0 + 低点>23.34，收盘确认型，不抢跑）",
             z[0] <= P <= z[1], cool=5)
    if "T3" in pending and tr_map.get("T3", {}).get("status") != "killed":
        emit("t3_watch", "info",
             f"T3 关注：已上 {L['t3_breakout']}，看量能+收盘确认（加 {tr_map['T3']['shares']} 股）",
             P >= L["t3_breakout"], cool=5)
    # --- 风控 ---
    emit("hs_warn", "warn",
         f"⚠️ 接近硬止损 {L['hard_stop']}（进入 {L['hard_stop_warn']} 警戒区），备好收盘触发单",
         P <= L["hard_stop_warn"], sound=True, cool=20)
    emit("hard_stop", "danger",
         f"🔴 硬止损 {L['hard_stop']} 跌破：全部清仓（底仓+新仓），不再看",
         P < L["hard_stop"], sound=True, cool=9999)
    emit("soft_stop", "warn",
         f"🟠 软止损 {L['soft_stop']} 跌破：新仓卖 200（底仓不动）；T2 入场当日作废、新仓计划冻结",
         P < L["soft_stop"], sound=True, cool=20)
    # --- 阶梯止盈 ---
    for tp in plan["take_profit_plan"]:
        if tp["id"] not in book.get("tp_done", []):
            emit(f"tp_{tp['id']}", "target",
                 f"🎯 止盈 {tp['id']} @ {tp['price']}：{tp['action']}",
                 P >= tp["price"], sound=True, cool=20)
    # --- T 信号（v2 默认停用：t_rules.enabled=false；仅盘中，且 T 禁区之外） ---
    t_enabled = plan.get("t_rules", {}).get("enabled", True)
    slv = plan.get("t_rules", {}).get("sleeve", 300)
    if allow_t and t_enabled and L["no_t_below"] < P < L["no_t_above"]:
        dlow, dhigh = q.get("low"), q.get("high")
        lo_pct, hi_pct = L["t_zone_pct"]
        resist = [r for r in L["resistances"] if P * 0.97 <= r <= P * 1.01]
        if dlow and P >= dlow * (1 + hi_pct) and resist:
            emit("t_sell", "info",
                 f"倒T 候选：自低点反弹 {((P / dlow) - 1) * 100:.1f}% 且接近压力 {min(resist):.2f}"
                 f"（卖 {slv} 股，当日买回，14:40 强制了结）", True, cool=15)
        sups = [s for s in L["supports"] if P * 0.99 <= s <= P * 1.03]
        if dhigh and P <= dhigh * (1 - lo_pct) and sups:
            emit("t_buy", "info",
                 f"正T 候选：自高点回落 {((P / dhigh) - 1) * 100:.1f}% 且接近支撑 {max(sups):.2f}"
                 f"（买 {slv} 股，当日卖出存量）", True, cool=15)
    _save_fired(fired)
    return out
