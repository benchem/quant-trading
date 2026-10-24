"""持仓、现金与买卖台账（JSON 状态 + JSONL 流水）。"""
from __future__ import annotations

import copy
import json

from .common import WATCHLOG, ensure_dir, now_cst
from .plan import load as load_plan

STATE = "state.json"
TRADES = "trades.jsonl"

DEFAULT_STATE = {
    "cash": 80000.0,
    "base": {"shares": 1500, "cost": 17.1},
    "tranches": [],  # 已成交新仓批次: {id, shares, cost, date}
    "pending": ["T1", "T2", "T3"],
    "tp_done": [],
    "note": "按 trading_plan_300687.md 初始化（2026-10-23）：底仓 1500 股 @17.1 + 现金 8 万",
}


def load() -> dict:
    p = WATCHLOG / STATE
    if not p.exists():
        ensure_dir()
        p.write_text(json.dumps(DEFAULT_STATE, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.loads(p.read_text(encoding="utf-8"))


def save(s: dict) -> None:
    ensure_dir()
    (WATCHLOG / STATE).write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


def positions(s: dict) -> dict:
    base_sh, base_cost = s["base"]["shares"], s["base"]["cost"]
    new_sh = sum(t["shares"] for t in s["tranches"])
    new_amt = sum(t["shares"] * t["cost"] for t in s["tranches"])
    return {
        "base_shares": base_sh,
        "base_cost": base_cost,
        "new_shares": new_sh,
        "new_cost": (new_amt / new_sh) if new_sh else 0.0,
        "total_shares": base_sh + new_sh,
        "total_cost": base_sh * base_cost + new_amt,
    }


def _compliance(s: dict, side: str, batch: str, price: float) -> str:
    """计划合规检查（提示性，不阻止记录）。传入的是成交【前】状态。"""
    msgs = []
    plan = load_plan()
    if side == "buy":
        if batch in ("T1", "T2", "T3"):
            tr = next((t for t in plan["tranches"] if t["id"] == batch), None)
            if tr is None:
                msgs.append(f"计划中无 {batch} 批次")
            elif batch not in s["pending"]:
                msgs.append(f"{batch} 已成交过，此笔属计划外")
            elif not (tr["zone"][0] <= price <= tr["zone"][1]):
                msgs.append(f"价格 {price} 不在 {batch} 区间 {tr['zone']}")
        elif batch not in ("T", "base"):
            msgs.append(f"买入批次 {batch} 不在计划内")
        if s["cash"] < 0:
            msgs.append("现金将为负")
    else:
        if batch == "T":
            msgs.append("T 腿：仓位不变，须当日对冲（14:40 强制了结）")
        elif batch not in ("T1", "T2", "T3", "base"):
            msgs.append(f"卖出批次 {batch} 不在计划内")
    return "; ".join(msgs) if msgs else "OK"


def record_trade(side: str, shares: int, price: float, batch: str, note: str = "", dry_run: bool = False) -> dict:
    shares, price = int(shares), round(float(price), 3)
    amt = round(shares * price, 2)
    ts = now_cst()
    s0 = load()
    comp = _compliance(s0, side, batch, price)
    s = copy.deepcopy(s0)
    realized: float | None = None
    if side == "buy":
        if batch in ("T1", "T2", "T3") and batch in s["pending"]:
            s["tranches"].append({"id": batch, "shares": shares, "cost": price, "date": ts.strftime("%Y-%m-%d")})
            s["pending"].remove(batch)
        elif batch == "base":
            b = s["base"]
            new_sh = b["shares"] + shares
            b["cost"] = round((b["cost"] * b["shares"] + amt) / new_sh, 3) if new_sh > 0 else 0.0
            b["shares"] = new_sh
        # batch == "T"：仓位不变，只动现金
        s["cash"] = round(s["cash"] - amt, 2)
    else:
        if batch in ("T1", "T2", "T3"):
            t = next((t for t in s["tranches"] if t["id"] == batch), None)
            if t is None:
                raise SystemExit(f"无法卖出 {batch}：没有已成交批次")
            if t["shares"] < shares:
                raise SystemExit(f"无法卖出 {batch}：已成交 {t['shares']} 股 < 卖出 {shares} 股")
            realized = round((price - t["cost"]) * shares, 2)
            t["shares"] -= shares
            if t["shares"] == 0:
                s["tranches"].remove(t)
        elif batch == "base":
            b = s["base"]
            if b["shares"] < shares:
                raise SystemExit(f"无法卖出底仓：仅 {b['shares']} 股")
            realized = round((price - b["cost"]) * shares, 2)
            b["shares"] -= shares
        s["cash"] = round(s["cash"] + amt, 2)
    rec = {
        "time": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "side": side,
        "shares": shares,
        "price": price,
        "amount": amt,
        "batch": batch,
        "note": note,
        "realized": realized,
        "compliance": comp,
    }
    if not dry_run:
        save(s)
        ensure_dir()
        with open(WATCHLOG / TRADES, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"rec": rec, "state": s, "dry_run": dry_run}
