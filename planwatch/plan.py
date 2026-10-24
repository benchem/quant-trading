"""投资计划（机器可读 JSON）+ 调整审计日志。"""
from __future__ import annotations

import json

from .common import ROOT, WATCHLOG, ensure_dir, now_cst

PLAN_PATH = ROOT / "plan_300687.json"
AUDIT = "plan_audit.jsonl"


def load() -> dict:
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))


def save(plan: dict) -> None:
    plan["updated"] = now_cst().strftime("%Y-%m-%d %H:%M:%S")
    PLAN_PATH.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")


def audit(action: str, detail: str) -> None:
    ensure_dir()
    rec = {"time": now_cst().strftime("%Y-%m-%d %H:%M:%S"), "action": action, "detail": detail}
    with open(WATCHLOG / AUDIT, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def tail_audit(n: int = 10) -> list[dict]:
    p = WATCHLOG / AUDIT
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(x) for x in lines[-n:]]


def set_level(plan: dict, level: str, value, reason: str) -> None:
    if level not in plan["levels"]:
        raise SystemExit(f"未知 level: {level}（可用: {', '.join(plan['levels'])}）")
    old = plan["levels"][level]
    plan["levels"][level] = value
    audit("set-level", f"{level}: {old} -> {value} | 理由: {reason}")
    save(plan)
