"""信号台账 + 命中率闭环（analyzer/signals）单测：合成数据。"""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import pytest

from analyzer import signals as sg


class FakeStore:
    def __init__(self, root):
        self.root = str(root)
        cal = pd.bdate_range("2026-10-01", periods=40)  # 40 个工作日
        self.calendar = pd.DatetimeIndex(cal)


class FakeResult:
    def __init__(self, d):
        self._d = d

    def to_dict(self):
        return self._d


class FakeRunner:
    """opinions: {(plugin, code): score}"""
    def __init__(self, opinions):
        self.opinions = opinions
        self.calls = []

    def run(self, plugin, code):
        self.calls.append((plugin, code))
        if (plugin, code) not in self.opinions:
            raise RuntimeError("boom")
        return FakeResult({"ok": True, "score": self.opinions[(plugin, code)],
                           "verdict": "测试", "display_name": f"模型{plugin}"})


def _mk_hfq(root, code, closes, start=dt.date(2026, 10, 1)):
    rows = [{"date": start + dt.timedelta(days=i), "high": c * 1.02,
             "low": c * 0.98, "close": c} for i, c in enumerate(closes)]
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    p = root / "daily" / "hfq"
    p.mkdir(parents=True)
    df.to_parquet(p / f"{code}.parquet", index=False)
    # 日期列写回 date 类型（与生产 parquet 一致由 datastore 维护，此处只需可读）
    return p / f"{code}.parquet"


# ---------------------------------------------------------------- 公式照抄锚点

def test_classify_outcome_dsa_semantics():
    # up 方向：±2% 带
    assert sg._classify_signal_outcome(3.0, "up") == ("hit", True)
    assert sg._classify_signal_outcome(-3.0, "up") == ("miss", False)
    assert sg._classify_signal_outcome(1.0, "up") == ("neutral", None)
    # not_down：r>=0 即 hit；-2% 以下 miss；其间 neutral
    assert sg._classify_signal_outcome(0.5, "not_down") == ("hit", True)
    assert sg._classify_signal_outcome(-1.0, "not_down") == ("neutral", None)
    assert sg._classify_signal_outcome(-3.0, "not_down") == ("miss", False)
    # not_up：r<=band 即 hit（无 neutral 下界——防守标准更宽）
    assert sg._classify_signal_outcome(1.0, "not_up") == ("hit", True)
    assert sg._classify_signal_outcome(3.0, "not_up") == ("miss", False)
    assert sg._classify_signal_outcome(None, "up") == (None, None)


def test_score_bands_dsa_scale():
    assert sg.score_to_signal_key(95) == ("strong_buy", "强烈看多")
    assert sg.score_to_signal_key(80) == ("strong_buy", "强烈看多")
    assert sg.score_to_signal_key(79.9) == ("buy", "看多")
    assert sg.score_to_signal_key(60) == ("buy", "看多")
    assert sg.score_to_signal_key(50) == ("watch", "观望")
    assert sg.score_to_signal_key(39) == ("reduce", "看空")
    assert sg.score_to_signal_key(5) == ("sell", "强烈看空")
    assert sg.score_to_signal_key(None) == (None, None)
    assert sg.score_to_signal_key(float("nan")) == (None, None)


def test_evaluate_signal_insufficient_bars():
    bars = [{"date": dt.date(2026, 10, 2), "high": 11, "low": 9, "close": 10}]
    r = sg.evaluate_decision_signal(direction_expected="up", start_price=10,
                                    window_bars=bars, eval_days=3)
    assert r["eval_status"] == "unable"
    assert r["unable_reason"] == "insufficient_forward_bars"
    assert r["start_price"] == 10


def test_evaluate_signal_completed_math():
    bars = [{"date": dt.date(2026, 10, 2 + i), "high": 10 * (1.03 + i * 0.01),
             "low": 9.0, "close": 10 * (1.02 + i * 0.01)} for i in range(3)]
    r = sg.evaluate_decision_signal(direction_expected="up", start_price=10,
                                    window_bars=bars, eval_days=3)
    assert r["eval_status"] == "completed"
    assert r["outcome"] == "hit" and r["direction_correct"] is True
    assert r["stock_return_pct"] == pytest.approx(4.0, abs=0.01)
    assert r["max_high"] == pytest.approx(10 * 1.05)
    assert r["min_low"] == pytest.approx(9.0)


def test_evaluate_invalid_anchor():
    r = sg.evaluate_decision_signal(direction_expected="up", start_price=0,
                                    window_bars=[], eval_days=1)
    assert r["eval_status"] == "unable" and r["unable_reason"] == "invalid_anchor_price"


# ---------------------------------------------------------------- 台账流程

def test_record_and_evaluate_roundtrip(tmp_path):
    store = FakeStore(tmp_path)
    day = "2026-10-01"
    # anchor 10-01 close=10；forward 10-02..10-05: 10.5/11.0/11.5/12.0（+20%）
    closes = [10.0, 10.5, 11.0, 11.5, 12.0]
    _mk_hfq(tmp_path, "600AAA.SH", closes)
    runner = FakeRunner({("p1", "600AAA.SH"): 75,  # buy → up
                         ("p2", "600AAA.SH"): 30,  # reduce → not_up
                         ("p3", "600AAA.SH"): 55})  # watch → not_down
    led = sg.SignalLedger(tmp_path)
    n = led.record_model_opinions(store, runner, ["600AAA.SH"], day,
                                  plugins=("p1", "p2", "p3", "dead"))
    assert n == 3
    sigs = led._signals.read()
    assert len(sigs) == 3
    by_plugin = {s["plugin"]: s for s in sigs}
    assert by_plugin["p1"]["direction"] == "up"
    assert by_plugin["p2"]["direction"] == "not_up"
    assert by_plugin["p3"]["direction"] == "not_down"
    assert by_plugin["p1"]["anchor_date"] == "2026-10-01"
    assert all(s["status"] == "active" for s in sigs)
    # 插件异常隔离（dead 未产出）
    assert "dead" not in by_plugin

    ev = led.evaluate_due(store)
    # forward 4 根：1d/3d 完成（3 信号×2=6），5d/10d 窗口不足 pending（6）
    assert ev["evaluated"] == 6
    assert ev["pending"] == 6
    st = {(r["plugin"], r["horizon"]): r for r in led.stats()}
    # p1 up：1d +5% hit；3d +10% hit … 全 hit
    assert st[("p1", "1d")]["hit"] == 1 and st[("p1", "1d")]["hit_rate_pct"] == 100.0
    # p2 not_up：1d +5% > band=2 → miss
    assert st[("p2", "1d")]["miss"] == 1
    # p3 not_down：1d +5% ≥0 → hit
    assert st[("p3", "1d")]["hit"] == 1
    # 样本不足标记
    assert st[("p1", "1d")]["sample_sufficient"] is False

    # 幂等：同日重采不重复
    runner2 = FakeRunner({("p1", "600AAA.SH"): 75, ("p2", "600AAA.SH"): 30,
                          ("p3", "600AAA.SH"): 55})
    n2 = led.record_model_opinions(store, runner2, ["600AAA.SH"], day,
                                   plugins=("p1", "p2", "p3"))
    assert n2 == 0 and len(led._signals.read()) == 3

    # 评估幂等：completed 不重算（updated_at 不变）
    ev2 = led.evaluate_due(store)
    assert ev2["evaluated"] == 0


def test_expires_and_pending(tmp_path):
    store = FakeStore(tmp_path)
    # anchor 10-01，仅 2 根 forward bar → 10d 窗口不足（pending 重试）
    _mk_hfq(tmp_path, "600BBB.SH", [10.0, 10.2, 10.3])
    led = sg.SignalLedger(tmp_path)
    led._signals.append({
        "id": 1, "code": "600BBB.SH", "name": "B", "source_type": "model_opinion",
        "source_ref": "p1:600BBB.SH:2026-10-01", "trigger_source": "eod_scan",
        "plugin": "p1", "plugin_name": "模型p1", "score": 75.0,
        "signal_key": "buy", "action_label": "看多", "direction": "up",
        "status": "active", "anchor_date": "2026-10-01",
        "created_at": "2026-10-01T15:30:00", "expires_at": "2026-10-20",
        "updated_at": None, "metadata": {}})
    ev = led.evaluate_due(store)
    assert ev["evaluated"] == 1  # forward 仅 2 根 → 1d completed
    # 3d/5d/10d 窗口不足 → pending 重试
    assert ev["pending"] == 3
    oc = led._load_outcomes()
    assert oc[(1, "1d")]["eval_status"] == "completed"
    assert oc[(1, "10d")]["unable_reason"] == "insufficient_forward_bars"

    # 惰性到期：把 expires_at 挪到过去
    sigs = led._signals.read()
    sigs[0]["expires_at"] = "2026-01-01"
    led._signals.rewrite(sigs)
    sigs = led._expire_due()
    assert sigs[0]["status"] == "expired"
    # expired 信号仍参与评估（DSA：expired 也是可评估状态）
    ev2 = led.evaluate_due(store)
    assert ev2["evaluated"] == 0  # completed 已有，不重算


def test_invalidated_not_evaluated(tmp_path):
    store = FakeStore(tmp_path)
    _mk_hfq(tmp_path, "600CCC.SH", [10.0, 11.0, 12.0, 13.0])
    led = sg.SignalLedger(tmp_path)
    led._signals.append({
        "id": 1, "code": "600CCC.SH", "name": "C", "source_type": "model_opinion",
        "source_ref": "p1:600CCC.SH:2026-10-01", "trigger_source": "eod_scan",
        "plugin": "p1", "plugin_name": "模型p1", "score": 75.0,
        "signal_key": "buy", "action_label": "看多", "direction": "up",
        "status": "invalidated", "anchor_date": "2026-10-01",
        "created_at": "2026-10-01T15:30:00", "expires_at": "2026-10-20",
        "updated_at": None, "metadata": {}})
    ev = led.evaluate_due(store)
    assert ev["evaluated"] == 0 and not led._outcomes_path.exists() or \
        len(led._load_outcomes()) == 0


def test_missing_stock_data(tmp_path):
    store = FakeStore(tmp_path)
    led = sg.SignalLedger(tmp_path)
    led._signals.append({
        "id": 1, "code": "600XXX.SH", "name": "X", "source_type": "model_opinion",
        "source_ref": "p1:600XXX.SH:2026-10-01", "trigger_source": "eod_scan",
        "plugin": "p1", "plugin_name": "模型p1", "score": 75.0,
        "signal_key": "buy", "action_label": "看多", "direction": "up",
        "status": "active", "anchor_date": "2026-10-01",
        "created_at": "2026-10-01T15:30:00", "expires_at": "2026-10-20",
        "updated_at": None, "metadata": {}})
    ev = led.evaluate_due(store)
    assert ev["pending"] == 0 and ev["skipped"] == 4  # missing_stock_data 不可重试


def test_stats_neutral_excluded(tmp_path):
    store = FakeStore(tmp_path)
    # anchor 10-01 close=10；forward +1%/+1%（neutral 带 ±2%）
    _mk_hfq(tmp_path, "600DDD.SH", [10.0, 10.1, 10.1, 10.1])
    led = sg.SignalLedger(tmp_path)
    for i, (plug, direction) in enumerate((("pu", "up"), ("pd", "not_up"))):
        led._signals.append({
            "id": i + 1, "code": "600DDD.SH", "name": "D",
            "source_type": "model_opinion",
            "source_ref": f"{plug}:600DDD.SH:2026-10-01",
            "trigger_source": "eod_scan", "plugin": plug,
            "plugin_name": plug, "score": 75.0, "signal_key": "buy",
            "action_label": "看多", "direction": direction, "status": "active",
            "anchor_date": "2026-10-01", "created_at": "2026-10-01T15:30:00",
            "expires_at": "2026-10-20", "updated_at": None, "metadata": {}})
    led.evaluate_due(store)
    st = {(r["plugin"], r["horizon"]): r for r in led.stats()}
    # 1d: +1% → up neutral / not_up hit
    assert st[("pu", "1d")]["neutral"] == 1 and st[("pu", "1d")]["n_decided"] == 0
    assert st[("pu", "1d")]["hit_rate_pct"] is None
    assert st[("pd", "1d")]["hit"] == 1
    # json 汇总可序列化
    json.loads(led.summary_json())
