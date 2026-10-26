"""planwatch 命令行：monitor（盯盘）/ status（快照）/ record（记买卖）/ plan（调计划）/ report（日报）。"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time

from .alerts import Alert, evaluate
from .common import WATCHLOG, ensure_dir, is_trading_day, now_cst, session
from .notify import emit
from .plan import audit, load as load_plan, save as save_plan, set_level, tail_audit
from .quote import fetch_quote
from .state import load as load_state, positions, record_trade, save as save_state, TRADES

# ---------------- 工具 ----------------

def _fmt_ts(ts: str) -> str:
    if ts and len(ts) == 14:
        return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[8:10]}:{ts[10:12]}:{ts[12:14]}"
    return ts or "-"


def _dist(price: float, level: float) -> str:
    if not level or not price:
        return "-"
    return f"{(price / level - 1) * 100:+.1f}%"


def _next_triggers(P: float, book: dict, plan: dict) -> str:
    L = plan["levels"]
    pend = set(book["pending"])
    parts = []
    if "T1" in pend:
        parts.append(f"T1 {L['t1_zone'][0]}~{L['t1_zone'][1]} {_dist(P, L['t1_zone'][0])}")
    if "T2" in pend:
        parts.append(f"T2 {L['t2_zone'][0]}~{L['t2_zone'][1]} {_dist(P, L['t2_zone'][0])}")
    if "T3" in pend:
        parts.append(f"T3>{L['t3_breakout']} {_dist(P, L['t3_breakout'])}")
    parts.append(f"硬止损 {_dist(P, L['hard_stop'])}")
    return " | ".join(parts)


def _t_net_today() -> int:
    p = WATCHLOG / TRADES
    if not p.exists():
        return 0
    day = now_cst().strftime("%Y-%m-%d")
    net = 0
    for line in p.read_text("utf-8").strip().splitlines():
        t = json.loads(line)
        if t["time"].startswith(day) and t["batch"] == "T":
            net += t["shares"] if t["side"] == "buy" else -t["shares"]
    return net


def _today_log(name: str) -> list[dict]:
    f = WATCHLOG / f"{name}.jsonl"
    if not f.exists():
        return []
    day = now_cst().strftime("%Y-%m-%d")
    out = []
    for line in f.read_text("utf-8").strip().splitlines():
        r = json.loads(line)
        if r.get("time", "").startswith(day):
            out.append(r)
    return out


def _pos_desc(book: dict) -> str:
    tr = " + ".join(f"{x['id']} {x['shares']}@{x['cost']}" for x in book["tranches"]) or "无"
    return f"底仓 {book['base']['shares']}@{book['base']['cost']} + 新仓 [{tr}] = {book['base']['shares'] + sum(t['shares'] for t in book['tranches'])} 股"


# ---------------- status ----------------

def cmd_status(args) -> None:
    plan, book = load_plan(), load_state()
    q = fetch_quote(plan["code"])
    P, p, L = q["price"], positions(book), plan["levels"]
    upl_base = (P - p["base_cost"]) * p["base_shares"]
    upl_new = sum(t["shares"] * (P - t["cost"]) for t in book["tranches"])
    value = book["cash"] + P * p["total_shares"]
    init = plan["capital"]["initial_total"]
    pnl = value - init
    t = plan["target"]
    print("=" * 64)
    print(f"{plan['code']} {plan['name']}   行情时间 {_fmt_ts(q['ts'])}   现在 {now_cst():%Y-%m-%d %H:%M}")
    print(f"价格 {P:.2f} ({q['change_pct']:+.2f}%)   开 {q['open']:.2f} 高 {q['high']:.2f} 低 {q['low']:.2f}"
          f"   量 {q['volume']:,.0f} 手  换手 {q['turnover_pct']}%")
    print(f"目标 {t['price']}（截止 {t['deadline']}，还需 {((t['price'] / P) - 1) * 100:+.1f}%）")
    print("-" * 64)
    print(f"持仓  {_pos_desc(book)}")
    print(f"现金 {book['cash']:,.2f}   总资产 {value:,.2f}   相对初始 {pnl:+,.2f} ({pnl / init * 100:+.1f}%)"
          f"   浮盈 底仓 {upl_base:+,.0f} / 新仓 {upl_new:+,.0f}")
    print("-" * 64)
    print("关键位（距现价）：")
    print(f"  T1 区间 {L['t1_zone'][0]}~{L['t1_zone'][1]:<8} [{'已成交' if 'T1' not in book['pending'] else '待触发'}]  {_dist(P, L['t1_zone'][0])}")
    print(f"  T2 区间 {L['t2_zone'][0]}~{L['t2_zone'][1]:<8} [{'已成交' if 'T2' not in book['pending'] else '待触发'}]  {_dist(P, L['t2_zone'][0])}")
    print(f"  T3 突破 {L['t3_breakout']:<10} [{'已成交' if 'T3' not in book['pending'] else '待触发'}]  {_dist(P, L['t3_breakout'])}")
    print(f"  软止损 {L['soft_stop']:<10} {_dist(P, L['soft_stop'])}   硬止损 {L['hard_stop']:<9} {_dist(P, L['hard_stop'])}")
    for tp in plan["take_profit_plan"]:
        st = "已完成" if tp["id"] in book.get("tp_done", []) else "待触发"
        print(f"  止盈 {tp['id']} @{tp['price']:<7} [{st}]  {_dist(P, tp['price'])}   {tp['action']}")
    print("-" * 64)
    tnet = _t_net_today()
    print(f"今日 T 未对冲: {tnet:+d} 股" + ("   ⚠️ 14:40 强制了结" if tnet else ""))
    aud = tail_audit(3)
    if aud:
        print("近期计划调整：")
        for a in aud:
            print(f"  {a['time']}  {a['action']}: {a['detail']}")
    print("=" * 64)


# ---------------- monitor ----------------

TIME_HOOKS = {
    "09:20": ("info", "📋 盘前检查清单：① 隔夜公告 ② 今日关键位 ③ 计划层挂单（价格写死）④ T 层条件 ⑤ 确认 14:40 提醒"),
    "09:30": ("info", "开盘"),
    "11:30": ("info", "午盘休市，留意午间公告"),
    "13:00": ("info", "午后开盘"),
    "14:38": ("warn", "⏰ 距 14:40 还有 2 分钟：检查 T 是否未对冲，14:50 前必须全部了结"),
    "14:57": ("warn", "⏰ 收盘前 3 分钟：T3 看量能收盘确认 / 未对冲 T 必须了结"),
}


def _check_cond(cond: str, close: float) -> bool:
    m = re.match(r"close\s*([<>])\s*([\d.]+)", cond or "")
    if not m:
        return False
    v = float(m.group(2))
    return close < v if m.group(1) == "<" else close > v


def _time_hooks(now: dt.datetime, plan: dict, q: dict | None, last_min: str, book: dict) -> str:
    tm = now.strftime("%H:%M")
    if tm == last_min:
        return tm
    code = plan.get("code", "")
    if tm in TIME_HOOKS:
        lvl, txt = TIME_HOOKS[tm]
        emit(Alert(f"time_{tm}", lvl, txt, sound=(lvl == "warn"), code=code))
    if tm == "15:01" and q and q.get("price"):
        for rule in plan.get("time_rules", []):
            if rule["date"] == now.strftime("%Y-%m-%d"):
                done = WATCHLOG / f"checkpoint_{rule['date']}.done"
                if done.exists():
                    continue
                close = q["price"]
                ensure_dir()
                done.write_text(now.isoformat(), "utf-8")
                if _check_cond(rule["check"], close):
                    emit(Alert("checkpoint", "danger",
                               f"🔴 时间止损（{rule['date']} 收盘 {close}，{rule['check']}）：{rule['action']}",
                               sound=True, code=code))
                else:
                    emit(Alert("checkpoint", "info", f"时间止损检查通过：{now:%m-%d} 收盘 {close}（{rule['check']}）",
                               code=code))
    return tm


def _status_line(q: dict, plan: dict, book: dict) -> None:
    P = q["price"]
    p = positions(book)
    sys.stdout.write(
        f"\r[{now_cst():%H:%M:%S}] {P:.2f} ({q['change_pct']:+.2f}%)  高{q['high']:.2f} 低{q['low']:.2f}"
        f"  量{q['volume'] / 10000:.1f}万手 | {p['total_shares']}股 现金{book['cash']:,.0f} | "
        f"{_next_triggers(P, book, plan)}   "
    )
    sys.stdout.flush()


def cmd_monitor(args) -> None:
    if args.no_notify:
        import notifybox

        notifybox.set_desktop_enabled(False)
    plan, book = load_plan(), load_state()
    code = plan["code"]
    p = positions(book)
    t = plan["target"]
    L = plan["levels"]
    print("=" * 64)
    print(f"计划盯盘 {code} {plan['name']}   {now_cst():%Y-%m-%d %H:%M}")
    print(f"目标 {t['price']}（截止 {t['deadline']}）  |  {_pos_desc(book)}  |  现金 {book['cash']:,.0f}")
    print(f"关键位: T1 {L['t1_zone'][0]}~{L['t1_zone'][1]} / T2 {L['t2_zone'][0]}~{L['t2_zone'][1]}"
          f" / T3>{L['t3_breakout']} / 止损 {L['soft_stop']}·{L['hard_stop']} / 止盈 36.5·43·50")
    print("提醒: 本窗口 + macOS 系统通知 + watchlog/alerts.jsonl    退出: Ctrl+C")
    print("=" * 64)
    last_min, errors, q, reported = "", 0, None, False
    try:
        while True:
            now = now_cst()
            s = session(now)
            if s == "closed" and not args.force:
                print("今天不是交易日，盯盘退出（--force 可强制运行）。")
                return
            if s in ("pre", "morning", "afternoon"):
                try:
                    q = fetch_quote(code)
                    errors = 0
                except Exception as e:  # noqa: BLE001
                    errors += 1
                    print(f"\n[warn] 行情获取失败(第{errors}次): {e}", flush=True)
                    if errors >= 60:
                        print("连续失败过多，退出。")
                        return
                    time.sleep(30)
                    continue
                for a in evaluate(q, plan, book, allow_t=s in ("morning", "afternoon")):
                    emit(a)
                last_min = _time_hooks(now, plan, q, last_min, book)
                _status_line(q, plan, book)
                time.sleep(args.interval)
            elif s == "lunch":
                last_min = _time_hooks(now, plan, q, last_min, book)
                sys.stdout.write("\n" + " " * 90)
                sys.stdout.flush()
                time.sleep(30)
            else:  # eod
                try:
                    q = fetch_quote(code)
                except Exception:  # noqa: BLE001
                    pass
                last_min = _time_hooks(now, plan, q, last_min, book)
                if not reported and now.time() >= dt.time(15, 2):
                    try:
                        cmd_report(args, auto=True)
                        reported = True
                    except Exception as e:  # noqa: BLE001
                        print(f"[warn] 日报生成失败: {e}", flush=True)
                if now.time() >= dt.time(15, 5):
                    print("已收盘，盯盘退出。")
                    return
                time.sleep(30)
    except KeyboardInterrupt:
        print("\n已手动停止盯盘。")


# ---------------- record ----------------

def cmd_record(args) -> None:
    r = record_trade(args.side, args.shares, args.price, args.batch, args.note or "", args.dry_run)
    rec, s = r["rec"], r["state"]
    tag = "[dry-run] " if r["dry_run"] else ""
    side_cn = "买入" if rec["side"] == "buy" else "卖出"
    print(f"{tag}已{'(试算) ' if r['dry_run'] else ''}记录: {rec['time']}  {side_cn} {rec['shares']} 股 @ {rec['price']}"
          f"  = {rec['amount']:,.2f} 元   批次={rec['batch']}")
    if rec["note"]:
        print(f"  备注: {rec['note']}")
    print(f"  合规: {rec['compliance']}")
    if rec["realized"] is not None:
        print(f"  本笔已实现盈亏: {rec['realized']:+,.2f} 元")
    if args.batch == "T" and not args.dry_run:
        tnet = _t_net_today()
        if tnet != 0:
            print(f"  ⚠️ 今日 T 未对冲: 净 {tnet:+d} 股（须当日对冲，14:40 强制了结）")
    p = positions(s)
    print(f"当前持仓: 底仓 {s['base']['shares']} 股 + 新仓 {p['new_shares']} 股 = {p['total_shares']} 股"
          f"   现金 {s['cash']:,.2f}")


# ---------------- plan ----------------

def cmd_plan(args) -> None:
    plan = load_plan()
    if args.plan_cmd == "show":
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        aud = tail_audit(10)
        if aud:
            print(f"\n计划调整审计（最近 {len(aud)} 条）：")
            for a in aud:
                print(f"  {a['time']}  {a['action']}: {a['detail']}")
    elif args.plan_cmd == "set":
        try:
            value = json.loads(args.value)
        except Exception:  # noqa: BLE001
            value = float(args.value)
        set_level(plan, args.level, value, args.reason)
        print(f"✅ 计划已更新: {args.level} = {value}")
    elif args.plan_cmd == "log":
        audit("note", args.text)
        print("✅ 计划备注已记录（watchlog/plan_audit.jsonl）")
    elif args.plan_cmd == "mark":
        book = load_state()
        if args.tp not in book.get("tp_done", []):
            book.setdefault("tp_done", []).append(args.tp)
            save_state(book)
        audit(f"mark {args.tp} done", args.reason)
        print(f"✅ {args.tp} 标记完成（该止盈不再提醒）")


# ---------------- report ----------------

def cmd_report(args, auto: bool = False) -> None:
    plan, book = load_plan(), load_state()
    now = now_cst()
    day = now.strftime("%Y-%m-%d")
    p = positions(book)
    try:
        q = fetch_quote(plan["code"])
    except Exception:  # noqa: BLE001
        q = None
    P = q["price"] if q else None
    trades = _today_log("trades")
    alerts = _today_log("alerts")
    auds = [a for a in tail_audit(50) if a["time"].startswith(day)]
    lines = [f"# {plan['code']} {plan['name']} · {day} 盯盘日报", "", f"生成时间 {now:%H:%M:%S}（planwatch）", ""]
    if q:
        lines += ["## 行情", "",
                  f"- 价格 {P:.2f} ({q['change_pct']:+.2f}%) | 开 {q['open']:.2f} 高 {q['high']:.2f} 低 {q['low']:.2f}",
                  f"- 量 {q['volume']:,.0f} 手 | 换手 {q['turnover_pct']}% | 行情时间 {_fmt_ts(q['ts'])}", ""]
    lines += ["## 今日成交", ""]
    if trades:
        lines += ["| 时间 | 方向 | 批次 | 股数 | 价格 | 金额 | 合规 |", "|---|---|---|---|---|---|---|"]
        for t in trades:
            lines.append(f"| {t['time'][11:]} | {'买入' if t['side'] == 'buy' else '卖出'} | {t['batch']}"
                         f" | {t['shares']} | {t['price']} | {t['amount']:,.0f} | {t['compliance']} |")
    else:
        lines.append("无")
    lines += ["", "## 今日提醒", ""]
    lines += [f"- [{a['level']}] {a['text']}" for a in alerts] if alerts else ["无"]
    lines += ["", "## 持仓与盈亏", ""]
    if P:
        value = book["cash"] + P * p["total_shares"]
        init = plan["capital"]["initial_total"]
        lines += [f"- {_pos_desc(book)}",
                  f"- 现金 {book['cash']:,.2f} | 总资产 {value:,.2f} | 相对初始 {value - init:+,.2f}"
                  f" ({(value - init) / init * 100:+.1f}%)"]
    else:
        lines.append("- 行情不可用")
    lines += ["", "## 计划进度", ""]
    for tr in plan["tranches"]:
        st = "待触发" if tr["id"] in book["pending"] else "已成交"
        lines.append(f"- {tr['id']}（{tr['shares']} 股）：{st}")
    for tp in plan["take_profit_plan"]:
        st = "已完成" if tp["id"] in book.get("tp_done", []) else "待触发"
        lines.append(f"- {tp['id']} @{tp['price']}：{st}")
    lines.append(f"- 今日 T 未对冲: {_t_net_today():+d} 股")
    if auds:
        lines += ["", "## 计划调整（今日）", ""]
        lines += [f"- {a['time'][11:]} {a['action']}: {a['detail']}" for a in auds]
    md = "\n".join(lines) + "\n"
    out = WATCHLOG / f"{day}.md"
    ensure_dir()
    out.write_text(md, "utf-8")
    print(f"✅ 日报已写入 watchlog/{day}.md")


# ---------------- main ----------------

def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="planwatch",
                                 description="300687 投资计划盯盘器：实时提醒 + 买卖记录 + 计划调整审计")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("monitor", help="实时盯盘（交易时段运行，提醒+状态行，收盘自动日报）")
    m.add_argument("--interval", type=float, default=5.0, help="轮询间隔秒（默认 5）")
    m.add_argument("--force", action="store_true", help="非交易日也运行（测试用）")
    m.add_argument("--no-notify", action="store_true", help="不弹 macOS 系统通知")
    m.set_defaults(fn=cmd_monitor)

    st = sub.add_parser("status", help="一次性快照：行情 + 持仓 + 关键位 + 盈亏")
    st.set_defaults(fn=cmd_status)

    r = sub.add_parser("record", help="记录一笔买卖（自动更新持仓/现金 + 计划合规检查）")
    r.add_argument("--side", choices=["buy", "sell"], required=True)
    r.add_argument("--shares", type=int, required=True)
    r.add_argument("--price", type=float, required=True)
    r.add_argument("--batch", required=True, help="T1 / T2 / T3 / base / T")
    r.add_argument("--note", default="", help="备注（触发依据）")
    r.add_argument("--dry-run", action="store_true", help="只试算不落盘")
    r.set_defaults(fn=cmd_record)

    p = sub.add_parser("plan", help="查看/调整投资计划（带审计日志）")
    psub = p.add_subparsers(dest="plan_cmd", required=True)
    psub.add_parser("show", help="打印计划 + 最近调整审计")
    s2 = psub.add_parser("set", help="修改关键位，如 set hard_stop 22.5 --reason '...'")
    s2.add_argument("level", help="levels 里的键，如 hard_stop 或 t1_zone")
    s2.add_argument("value", help="数值或 JSON，如 22.5 / [26, 27]")
    s2.add_argument("--reason", required=True, help="调整理由（写入审计）")
    l2 = psub.add_parser("log", help="记一条计划备注")
    l2.add_argument("text")
    m2 = psub.add_parser("mark", help="标记止盈完成，如 mark tp1 --reason '已减 500 股'")
    m2.add_argument("tp", help="tp1 / tp2 / tp3")
    m2.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_plan)

    rep = sub.add_parser("report", help="生成今日盯盘日报（markdown，存 watchlog/）")
    rep.set_defaults(fn=cmd_report)

    args = ap.parse_args(argv)
    args.fn(args)
