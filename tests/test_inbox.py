"""统一站内信（notifybox）单测：级别归一 / 来源 / 过滤计数 / 跨进程尾部同步 / planwatch 汇入。

全部离线（tempfile 目录），不触网、不污染真实 data/ 与 watchlog/。

运行: .venv/bin/python tests/test_inbox.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")

from notifybox import NotificationCenter, get_inbox, normalize_level  # noqa: E402

PASSED = 0
FAILED = 0


def check(name, fn) -> bool:
    global PASSED, FAILED
    print(f"\n===== {name} =====", flush=True)
    try:
        fn()
        print("PASS")
        PASSED += 1
        return True
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        FAILED += 1
        return False


def _tmp():
    return Path(tempfile.mkdtemp())


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ 用例


def t_normalize_level():
    assert normalize_level("danger") == "alert"
    assert normalize_level("target") == "alert"
    assert normalize_level("warn") == "warn"
    assert normalize_level("warning") == "warn"
    assert normalize_level("ok") == "ok"
    assert normalize_level("success") == "ok"
    assert normalize_level("info") == "info"
    assert normalize_level("alert") == "alert"
    assert normalize_level("bogus") == "info"
    assert normalize_level(None) == "info"


def t_push_source_and_dedup():
    d = _tmp()
    nc = NotificationCenter(d)
    r1 = nc.push("trade:2026-10-23:t1", "T1 入场窗口", level="target",
                 code="300687.SZ", source="trade", day="2026-10-23")
    assert r1 is not None
    assert r1["level"] == "alert"          # danger/target 归一到 alert
    assert r1["raw_level"] == "target"
    assert r1["source"] == "trade"
    # 同 key 同 day 去重
    assert nc.push("trade:2026-10-23:t1", "T1 入场窗口", source="trade",
                   day="2026-10-23") is None
    # 同文本不同 key 也去重（当日）
    assert nc.push("other:key", "T1 入场窗口", source="trade", day="2026-10-23") is None
    # 不同 day 放行
    assert nc.push("trade:2026-10-24:t1", "T1 入场窗口", source="trade",
                   day="2026-10-24") is not None
    # dedup=False 强制放行
    assert nc.push("trade:2026-10-23:t1", "T1 再触发", source="trade",
                   day="2026-10-23", dedup=False) is not None


def t_list_filters_and_counts():
    d = _tmp()
    nc = NotificationCenter(d)
    nc.push("m:1", "模型A 突破", level="alert", source="model", code="600519.SH", day="2026-10-23")
    nc.push("t:1", "止损", level="danger", source="trade", code="300687.SZ", day="2026-10-23")
    nc.push("s:1", "更新完成", level="ok", source="system", day="2026-10-23")
    out = nc.list()
    assert out["counts"]["total"] == 3
    assert out["counts"]["by_source"]["model"]["total"] == 1
    assert out["counts"]["by_source"]["trade"]["total"] == 1
    assert out["counts"]["by_source"]["system"]["total"] == 1
    assert out["counts"]["unread"] == 3
    # 按来源过滤
    assert len(nc.list(source="trade")["messages"]) == 1
    # 按级别过滤（danger 归一 alert）
    assert len(nc.list(level="alert")["messages"]) == 2   # model alert + trade alert
    # 按股票过滤
    assert nc.list(code="600519.SH")["messages"][0]["source"] == "model"
    # 只看未读 + 标记后过滤
    nc.mark_read(all=True)
    assert nc.list(unread_only=True)["messages"] == []
    assert nc.list()["counts"]["unread"] == 0


def t_cursor_incremental():
    from datetime import timedelta
    d = _tmp()
    d.mkdir(parents=True, exist_ok=True)
    base = datetime(2026, 10, 25, 10, 0, 0, tzinfo=timezone.utc)

    def iso(sec):
        return (base + timedelta(seconds=sec)).isoformat(timespec="seconds")

    recs = [
        {"id": "c1", "key": "s:1", "code": None, "level": "info", "raw_level": "info",
         "source": "system", "text": "第一条", "time": iso(0), "day": "2026-10-25", "read": False},
        {"id": "c2", "key": "s:2", "code": None, "level": "warn", "raw_level": "warn",
         "source": "system", "text": "第二条", "time": iso(60), "day": "2026-10-25", "read": False},
    ]
    (d / "notifications.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n", "utf-8")
    nc = NotificationCenter(d)
    out = nc.list()
    assert len(out["messages"]) == 2
    assert out["cursor"] == iso(60)          # 游标为最新一条时间
    fresh = nc.list(since=iso(0))["messages"]  # 只取比 iso(0) 新的
    assert len(fresh) == 1 and fresh[0]["text"] == "第二条"


def t_cross_process_tail_sync():
    d = _tmp()
    d.mkdir(parents=True, exist_ok=True)
    nc = NotificationCenter(d)                     # 先启动的读取端（无文件）
    assert len(nc.list()["messages"]) == 0
    # 模拟另一进程直接向文件追加一行
    rec = {"id": uuid.uuid4().hex[:10], "key": "trade:x", "code": "300687.SZ",
           "level": "alert", "raw_level": "danger", "source": "trade",
           "text": "外部交易提醒", "time": _now(), "day": "2026-10-25", "read": False}
    with open(d / "notifications.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    out = nc.list()                                # 轮询触发 tail sync
    assert len(out["messages"]) == 1
    assert out["messages"][0]["text"] == "外部交易提醒"
    assert out["counts"]["by_source"]["trade"]["unread"] == 1
    # 追加半行（无换行）不应被消费
    with open(d / "notifications.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"partial":"no-newline')
    assert len(nc.list()["messages"]) == 1
    # 补齐换行后应被消费
    with open(d / "notifications.jsonl", "a", encoding="utf-8") as fh:
        fh.write(",\"key\":\"trade:y\",\"day\":\"2026-10-25\"}\n")
    # 该行含非法字段组合（无 id/text），tail 逻辑需健壮：不应崩溃
    nc.list()


def t_self_push_not_duplicated_on_sync():
    """本进程 push 也写文件；后续 tail sync 读到自身行时按 id 去重，不重复入内存。"""
    d = _tmp()
    nc = NotificationCenter(d)
    for i in range(3):
        nc.push(f"s:{i}", f"消息{i}", source="system", day="2026-10-25")
    n_before = len(nc.list()["messages"])
    # 多次 list 触发 _sync（本进程写过的行 offset 会前进，且 id 去重兜底）
    for _ in range(5):
        nc.list()
    assert len(nc.list()["messages"]) == n_before == 3


def t_mark_read_scoped_by_source():
    d = _tmp()
    nc = NotificationCenter(d)
    nc.push("m:1", "模型", source="model", day="2026-10-25")
    nc.push("t:1", "交易", source="trade", day="2026-10-25")
    n = nc.mark_read(all=True, source="model")
    assert n == 1
    assert nc.list(source="model")["counts"]["by_source"]["model"]["unread"] == 0
    assert nc.list(source="trade")["counts"]["by_source"]["trade"]["unread"] == 1


def t_clear_by_source():
    d = _tmp()
    nc = NotificationCenter(d)
    nc.push("m:1", "模型1", source="model", day="2026-10-25")
    nc.push("m:2", "模型2", source="model", day="2026-10-25")
    nc.push("t:1", "交易1", source="trade", day="2026-10-25")
    removed = nc.clear(source="model")
    assert removed == 2
    assert nc.list()["counts"]["total"] == 1
    # clear 重写文件后，读取端从全量重读恢复一致
    nc2 = NotificationCenter(d)
    assert nc2.list()["counts"]["total"] == 1
    assert nc2.list()["messages"][0]["source"] == "trade"


def t_get_inbox_singleton():
    d = _tmp()
    a = get_inbox(d)
    b = get_inbox(d)
    assert a is b


def t_analyzer_shim_identity():
    from analyzer.notify import NotificationCenter as ShimNC
    assert ShimNC is NotificationCenter


def t_persist_reload_tolerance():
    """旧记录（无 source/raw_level 字段）从 JSONL 恢复时补默认值，list 正常。"""
    d = _tmp()
    d.mkdir(parents=True, exist_ok=True)
    legacy = {"id": "x1", "key": "update:done:2026-10-23", "code": None,
              "level": "ok", "text": "旧格式消息", "time": _now(),
              "day": "2026-10-23", "read": True}
    (d / "notifications.jsonl").write_text(
        json.dumps(legacy, ensure_ascii=False) + "\n", "utf-8")
    nc = NotificationCenter(d)
    msgs = nc.list()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["source"] == "system"     # 补默认
    assert msgs[0]["read"] is True


def t_planwatch_emit_lands_in_inbox():
    """planwatch.notify.emit 汇入统一收件箱：source=trade、级别归一、携带 code。"""
    sys.path.insert(0, ".")
    from planwatch.alerts import Alert
    import planwatch.notify as pn

    d = _tmp()
    d.mkdir(parents=True, exist_ok=True)
    # 隔离：重定向收件箱目录与 CLI 本地日志目录，避免污染真实 watchlog/
    pn._DATA_DIR = d
    pn.WATCHLOG = d
    pn.ensure_dir = lambda: d
    a = Alert("hard_stop", "danger", "🔴 硬止损 22.5 跌破：全部清仓", sound=True,
              code="300687.SZ")
    pn.emit(a)
    nc = NotificationCenter(d)
    out = nc.list()
    assert out["counts"]["total"] == 1
    m = out["messages"][0]
    assert m["source"] == "trade"
    assert m["level"] == "alert"            # danger -> alert
    assert m["code"] == "300687.SZ"
    # 同 key 同 day 再次 emit -> 去重（返回 None，但 emit 本身吞异常不外泄）
    assert pn.emit(a) is None
    assert NotificationCenter(d).list()["counts"]["total"] == 1


def main():
    cases = [
        ("级别归一 normalize_level", t_normalize_level),
        ("push 来源+多级去重", t_push_source_and_dedup),
        ("list 过滤与 counts", t_list_filters_and_counts),
        ("游标增量 since", t_cursor_incremental),
        ("跨进程尾部同步 tail", t_cross_process_tail_sync),
        ("自身 push 不重复消费", t_self_push_not_duplicated_on_sync),
        ("按来源标记已读", t_mark_read_scoped_by_source),
        ("按来源清空", t_clear_by_source),
        ("get_inbox 进程级单例", t_get_inbox_singleton),
        ("analyzer.notify shim 同类型", t_analyzer_shim_identity),
        ("旧记录恢复容错", t_persist_reload_tolerance),
        ("planwatch emit 汇入收件箱", t_planwatch_emit_lands_in_inbox),
    ]
    for name, fn in cases:
        check(name, fn)
    print(f"\n===== 汇总: {PASSED} PASS / {FAILED} FAIL =====")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
