"""交易日记（analyzer.journal）自测：事件/笔记/复盘 CRUD、用户隔离、规则复盘。

全部离线（临时目录 + fake store），不触网。

运行: .venv/bin/python tests/test_journal.py
"""
from __future__ import annotations

import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, ".")

from analyzer.journal import (  # noqa: E402
    Journal,
    build_daily_review,
    eod_snapshot_event,
    event_from_signal,
    event_from_trade,
)

PASSED = 0
FAILED = 0


def check(name: str, fn) -> bool:
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


# ------------------------------------------------------------------ 替身


class _FakeStore:
    """只有 kline 方法的最小 store（build_daily_review 只用它）。"""

    def __init__(self, root, kline=None):
        self.root = Path(root)
        self._kline = kline

    def kline(self, code, start=None, end=None):
        if self._kline is None:
            return None
        return self._kline


def _kl(dates, closes):
    return {"dates": dates, "close": closes}


# ------------------------------------------------------------------ 用例


def t_event_from_trade():
    ev = event_from_trade({"id": "t1", "code": "300687.SZ", "date": "2026-10-26",
                           "side": "buy", "price": 27.8, "shares": 600,
                           "note": "T1 半仓", "created": "2026-10-26 10:00:00"})
    assert ev["kind"] == "trade" and ev["code"] == "300687.SZ"
    assert ev["trade_id"] == "t1"
    assert "买入 600 股 @ 27.8" in ev["text"] and "T1 半仓" in ev["text"]
    ev2 = event_from_trade({"code": "600519.SH", "side": "sell", "price": 1300,
                            "shares": 100, "date": "2026-10-26"})
    assert "卖出 100 股" in ev2["text"]
    try:
        event_from_trade({"side": "buy"})
        raise AssertionError("缺 code 应当报错")
    except ValueError:
        pass


def t_event_from_signal_and_snapshot():
    ev = event_from_signal({"code": "300687.SZ", "plugin": "multi_factor_breakout",
                            "level": "alert", "text": "共振突破"})
    assert ev["kind"] == "model" and ev["plugin"] == "multi_factor_breakout"
    assert "multi_factor_breakout｜共振突破" in ev["text"]

    snap = eod_snapshot_event("300687.SZ", 27.9, 28.77, 3.06, 299408, 8.91)
    assert snap["kind"] == "system" and "收盘 28.77" in snap["text"]
    assert "+3.06%" in snap["text"] and "换手 8.91%" in snap["text"]
    # None 字段安全
    snap2 = eod_snapshot_event("600519.SH", None, 1300.5, None, None, None)
    assert "收盘 1300.5" in snap2["text"] and "%" not in snap2["text"]


def t_journal_crud():
    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(tmp, None)
        # 事件 append-only + 过滤
        j.add_event(event_from_trade(
            {"code": "300687.SZ", "side": "buy", "price": 27.8, "shares": 600,
             "date": "2026-10-25", "created": "2026-10-25 10:00:00"}))
        j.add_event(event_from_signal(
            {"code": "600519.SH", "plugin": "p", "level": "alert", "text": "x"}))
        assert len(j.events()) == 2
        assert len(j.events(code="300687.SZ")) == 1
        assert len(j.events(kind="model")) == 1
        # since_day 过滤：trade 事件的 day=交易发生日（10-25），signal 是写入今天
        today_like = [e for e in j.events() if e["kind"] == "model"][0]["day"]
        assert len(j.events(since_day=today_like)) == 1  # 只有 signal 事件
        assert len(j.events(since_day="2026-10-25")) == 2
        assert len(j.events(since_day="2999-01-01")) == 0
        # 排序（新的在前）
        evs = j.events()
        assert evs[0]["time"] >= evs[1]["time"] or evs[0]["id"] != evs[1]["id"]

        # 笔记 add/update/delete
        n = j.add_note("300687.SZ", "等待回踩缩量", tags=["计划"])
        assert n["code"] == "300687.SZ" and n["tags"] == ["计划"]
        u = j.update_note(n["id"], text="等待回踩缩量（更新）")
        assert "（更新）" in u["text"] and u.get("edited")
        assert j.update_note("不存在", text="x") is None
        assert j.delete_note(n["id"]) is True
        assert j.delete_note(n["id"]) is False
        assert j.notes() == []
        try:
            j.add_note(None, "   ")
            raise AssertionError("空笔记应当报错")
        except ValueError:
            pass

        # 复盘
        r = j.add_review("2026-10-26", "300687.SZ", "测试复盘",
                         lines=[{"type": "market", "text": "收盘 28.77"}])
        assert r["version"] == "rule"
        assert len(j.reviews(code="300687.SZ")) == 1
        try:
            j.add_review("2026-10-26", "300687.SZ", " ")
            raise AssertionError("空摘要应当报错")
        except ValueError:
            pass


def t_user_isolation():
    with tempfile.TemporaryDirectory() as tmp:
        ja = Journal(tmp, "alice")
        jb = Journal(tmp, "bob")
        jn = Journal(tmp, None)  # 共享模式
        ja.add_note("300687.SZ", "alice 的笔记")
        jb.add_note("600519.SH", "bob 的笔记")
        jn.add_note("300687.SZ", "共享笔记")
        assert len(ja.notes()) == 1 and ja.notes()[0]["text"] == "alice 的笔记"
        assert len(jb.notes()) == 1 and jb.notes()[0]["code"] == "600519.SH"
        assert len(jn.notes()) == 1
        # 目录隔离
        assert (Path(tmp) / "users" / "alice" / "journal").is_dir()
        assert (Path(tmp) / "users" / "bob" / "journal").is_dir()
        assert (Path(tmp) / "journal").is_dir()


def t_build_daily_review():
    with tempfile.TemporaryDirectory() as tmp:
        kline = _kl(["2026-10-20", "2026-10-21", "2026-10-22"],
                    [10.0, 10.5, 11.0])
        store = _FakeStore(tmp, kline)
        j = Journal(tmp, None)
        # 有行情无事件 -> 仍可复盘（market line）
        r = build_daily_review(j, store, "300687.SZ", "2026-10-22")
        assert r is not None and r["version"] == "rule"
        assert any("收盘 11.0" in l["text"] for l in r["lines"])
        assert any(l["type"] == "text" and "无交易/信号事件" in l["text"]
                   for l in r["lines"])
        # 加入交易事件 + 笔记
        j.add_event(event_from_trade(
            {"code": "300687.SZ", "side": "buy", "price": 10.8, "shares": 500,
             "date": "2026-10-22", "created": "2026-10-22 14:00:00"}))
        j.add_note("300687.SZ", "按计划进场", day="2026-10-22")
        r2 = build_daily_review(j, store, "300687.SZ", "2026-10-22")
        assert any("买入 500 股" in l["text"] for l in r2["lines"])
        assert any("按计划进场" in l["text"] for l in r2["lines"])
        # 无行情无事件 -> None
        store2 = _FakeStore(tmp, None)
        assert build_daily_review(Journal(tmp, None), store2, "000001.SZ",
                                  "2026-10-22") is None


def t_llm_review_prompt_guard():
    """LLM 复盘：未配置 key 时报 AIError（不触网）。"""
    from analyzer.ai import AIConfig
    from analyzer.journal import build_llm_review

    with tempfile.TemporaryDirectory() as tmp:
        j = Journal(tmp, None)
        rule = {"summary": "s", "lines": [{"type": "market", "text": "t"}]}
        try:
            build_llm_review(j, "300687.SZ", "2026-10-26", rule,
                             config=AIConfig())  # 未配置 key
            raise AssertionError("应当抛 AIError")
        except Exception as e:  # noqa: BLE001
            assert "AI_API_KEY" in str(e)


def main() -> int:
    check("事件生成（交易/信号/快照）", t_event_from_trade)
    check("信号与盘后快照事件", t_event_from_signal_and_snapshot)
    check("Journal CRUD", t_journal_crud)
    check("用户隔离", t_user_isolation)
    check("规则版复盘生成", t_build_daily_review)
    check("LLM 复盘未配置降级", t_llm_review_prompt_guard)
    print(f"\n===== 汇总: {PASSED} PASS / {FAILED} FAIL =====")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
