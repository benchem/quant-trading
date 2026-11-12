"""决策信号台账 + 命中率闭环（吸收自 DSA daily_stock_analysis 的 decision_signal 体系）。

吸收边界（DSA 为 MIT 协议，src/core/backtest_engine.py:276-364/642-685 的
evaluate_decision_signal/_classify_signal_outcome 公式逐行照抄）：
- P0 信号源 = 本地量化模型意见（EOD scan 时逐自选股逐插件采样 score/verdict，
  对应 DSA 的 skill_opinion 样本体系）；AI 报告结构化信号为 P1；
- 评分→档位→方向刻度照抄 DSA decision_scale.py:25-31（80+强买/60 买/40 观望/
  20 减/0 卖）；
- 评估：anchor=信号日 hfq 收盘，forward=其后 N 根 hfq bar（T+1 语义，停牌自然
  跳过），三分类 hit/miss/neutral（neutral band ±2%，DSA 默认；not_up 无
  neutral 下界——防守建议的「正确」标准更宽，DSA 有意设计）；
- 命中率分母不含 neutral（DSA outcome_service:998-1023 聚合口径），样本
  <30 标记不充分（DSA MIN_PROFILE_CALIBRATION_SAMPLE_SIZE）。

存储（对齐仓库既有 parquet+JSON 习惯，不引数据库）::

    data/signals/decision_signals.jsonl          # 信号主表（append + 全量重写改状态）
    data/signals/decision_signal_outcomes.parquet # 评估结果，唯一键 (signal_id, horizon)

本模块属规则面：不 import analyzer.agent（纪律同 journal.py），LLM 接触点在
P1 的报告抽取层。
"""
from __future__ import annotations

import datetime as dt
import json
import math
import threading
from pathlib import Path

import pandas as pd

from .journal import _JsonlFile, _read_jsonl

ENGINE_VERSION = "signal-v1"
#: 评估窗口（交易日数）；信号全档评估，统计页按 horizon 分列
HORIZONS = (("1d", 1), ("3d", 3), ("5d", 5), ("10d", 10))
#: 中性带（%）：|return| < band 判 neutral（not_up 方向无下界，DSA 同）
NEUTRAL_BAND_PCT = 2.0
#: 信号有效期（交易日）：到期惰性转 expired（DSA TTL 语义，P0 取 10d）
TTL_TRADE_DAYS = 10
#: 命中率样本充分性阈值（DSA MIN_PROFILE_CALIBRATION_SAMPLE_SIZE）
MIN_SAMPLE = 30
#: 可重试的 unable 原因：数据到位后下轮 EOD 自动补评
_RETRYABLE = {"missing_anchor_price", "insufficient_forward_bars"}

# ---- DSA decision_scale.py:25-31 评分刻度（照抄）----
_SCORE_BANDS = ((80.0, "strong_buy"), (60.0, "buy"), (40.0, "watch"),
                (20.0, "reduce"), (0.0, "sell"))
_ACTION_LABELS = {"strong_buy": "强烈看多", "buy": "看多", "watch": "观望",
                  "reduce": "看空", "sell": "强烈看空"}
_DIRECTION_FOR_KEY = {"strong_buy": "up", "buy": "up", "watch": "not_down",
                      "reduce": "not_up", "sell": "not_up"}


def score_to_signal_key(score) -> tuple[str | None, str | None]:
    """(signal_key, label)：score 落档；非法/缺失返回 (None, None)。"""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None, None
    if not math.isfinite(s):
        return None, None
    for floor, key in _SCORE_BANDS:
        if s >= floor:
            return key, _ACTION_LABELS[key]
    return "sell", _ACTION_LABELS["sell"]


# ---- DSA backtest_engine.py:642-685 _classify_signal_outcome（照抄）----
def _classify_signal_outcome(stock_return_pct, direction_expected,
                             neutral_band_pct=NEUTRAL_BAND_PCT):
    if stock_return_pct is None:
        return None, None
    band = abs(float(neutral_band_pct))
    r = float(stock_return_pct)
    if direction_expected == "up":
        if r >= band:
            return "hit", True
        if r <= -band:
            return "miss", False
        return "neutral", None
    if direction_expected == "not_down":
        if r >= 0:
            return "hit", True
        if r <= -band:
            return "miss", False
        return "neutral", None
    if direction_expected == "not_up":
        if r <= band:
            return "hit", True
        return "miss", False
    return None, None


def _finite_optional_float(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# ---- DSA backtest_engine.py:276-364 evaluate_decision_signal（照抄，bar 用 dict）----
def evaluate_decision_signal(*, direction_expected, start_price,
                             window_bars, eval_days,
                             neutral_band_pct=NEUTRAL_BAND_PCT) -> dict:
    """评估单信号单窗口。

    window_bars: list[dict(date, high, low, close)]（hfq 序列，已截取
    anchor 之后的 forward 段，长度 >= eval_days 才可评）。
    """
    start = _finite_optional_float(start_price)
    if start is None or start <= 0:
        return {"eval_status": "unable", "unable_reason": "invalid_anchor_price",
                "direction_expected": direction_expected, "eval_window_days": eval_days}

    if len(window_bars) < eval_days:
        return {"eval_status": "unable", "unable_reason": "insufficient_forward_bars",
                "direction_expected": direction_expected, "eval_window_days": eval_days,
                "start_price": start}

    bars = window_bars[:eval_days]
    end_close = _finite_optional_float(bars[-1]["close"])
    highs = [h for h in (_finite_optional_float(b["high"]) for b in bars) if h is not None]
    lows = [l for l in (_finite_optional_float(b["low"]) for b in bars) if l is not None]

    if end_close is None:
        return {"eval_status": "unable", "unable_reason": "invalid_end_close",
                "direction_expected": direction_expected, "eval_window_days": eval_days,
                "start_price": start, "max_high": max(highs) if highs else None,
                "min_low": min(lows) if lows else None}

    stock_return_pct = (end_close - start) / start * 100.0
    outcome, direction_correct = _classify_signal_outcome(
        stock_return_pct, direction_expected, neutral_band_pct)
    return {
        "eval_status": "completed", "outcome": outcome,
        "direction_expected": direction_expected,
        "direction_correct": direction_correct,
        "eval_window_days": eval_days,
        "start_price": start, "end_close": end_close,
        "max_high": max(highs) if highs else None,
        "min_low": min(lows) if lows else None,
        "stock_return_pct": stock_return_pct,
    }


class SignalLedger:
    """信号台账：模型意见采集（EOD）+ 到期维护 + 命中评估 + 统计。"""

    def __init__(self, data_dir):
        self.dir = Path(data_dir) / "signals"
        self._signals = _JsonlFile(self.dir / "decision_signals.jsonl")
        self._outcomes_path = self.dir / "decision_signal_outcomes.parquet"
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 采集

    def record_model_opinions(self, store, runner, codes, day: str,
                              plugins, name_of=None) -> int:
        """EOD 采集模型意见信号（幂等键 source_ref=plugin:code:day）。

        score 经五档刻度落 signal_key/direction；scan 阶段 runner 已跑过
        同 (plugin, code)，此处命中 Runner 缓存，无重复计算。
        """
        existing = {r.get("source_ref") for r in self._signals.read()}
        n = 0
        anchor = dt.date.fromisoformat(str(day))
        for code in codes:
            code = str(code).upper()
            name = name_of(code) if name_of else code
            for plugin in plugins:
                ref = f"{plugin}:{code}:{day}"
                if ref in existing:
                    continue
                try:
                    r = runner.run(plugin, code)
                    d = r.to_dict() if hasattr(r, "to_dict") else r
                except Exception:  # noqa: BLE001 插件故障隔离
                    continue
                if not d.get("ok"):
                    continue
                key, label = score_to_signal_key(d.get("score"))
                if key is None:
                    continue
                sig = {
                    "id": self._next_id(),
                    "code": code,
                    "name": str(name),
                    "source_type": "model_opinion",
                    "source_ref": ref,
                    "trigger_source": "eod_scan",
                    "plugin": str(plugin),
                    "plugin_name": str(d.get("display_name") or plugin),
                    "score": _finite_optional_float(d.get("score")),
                    "signal_key": key,
                    "action_label": label,
                    "direction": _DIRECTION_FOR_KEY[key],
                    "verdict_text": (d.get("verdict") or "")[:200] or None,
                    "status": "active",
                    "anchor_date": str(day),
                    "created_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "expires_at": self._expires_at(store, anchor),
                    "updated_at": None,
                    "metadata": {},
                }
                with self._lock:
                    self._signals.append(sig)
                    existing.add(ref)
                n += 1
        return n

    def _next_id(self) -> int:
        return 1 + max((int(r.get("id") or 0) for r in self._signals.read()),
                       default=0)

    def _expires_at(self, store, anchor: dt.date) -> str | None:
        try:
            cal = pd.DatetimeIndex(store.calendar)
            fut = cal[cal > pd.Timestamp(anchor)]
            if len(fut) >= TTL_TRADE_DAYS:
                return str(fut[TTL_TRADE_DAYS - 1].date())
        except Exception:  # noqa: BLE001 日历缺失 → 自然日兜底
            pass
        return str(anchor + dt.timedelta(days=TTL_TRADE_DAYS * 2))

    # ------------------------------------------------------------ 评估

    def _expire_due(self) -> list[dict]:
        """惰性到期：active 且 expires_at <= today → expired（整文件重写）。"""
        sigs = self._signals.read()
        today = dt.date.today().isoformat()
        changed = False
        for s in sigs:
            if s.get("status") == "active" and s.get("expires_at") and s["expires_at"] <= today:
                s["status"] = "expired"
                s["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
                changed = True
        if changed:
            self._signals.rewrite(sigs)
        return sigs

    def _load_outcomes(self) -> dict[tuple[int, str], dict]:
        if not self._outcomes_path.exists():
            return {}
        df = pd.read_parquet(self._outcomes_path)
        return {(int(r["signal_id"]), str(r["horizon"])): r
                for r in df.to_dict("records")}

    def _save_outcomes(self, rows: dict[tuple[int, str], dict]) -> None:
        df = pd.DataFrame(list(rows.values()))
        if df.empty:
            return
        cols = ["signal_id", "horizon", "engine_version", "eval_status", "outcome",
                "direction_expected", "direction_correct", "unable_reason",
                "anchor_date", "eval_window_days", "start_price", "end_close",
                "max_high", "min_low", "stock_return_pct", "created_at", "updated_at"]
        for c in cols:
            if c not in df.columns:
                df[c] = None
        df = df[cols].sort_values(["signal_id", "horizon"])
        tmp = self._outcomes_path.with_suffix(".tmp.parquet")
        df.to_parquet(tmp, index=False)
        tmp.replace(self._outcomes_path)

    def _hfq_bars(self, store, code: str) -> list[dict] | None:
        p = Path(store.root) / "daily" / "hfq" / f"{code}.parquet"
        if not p.exists():
            return None
        t = pd.read_parquet(p, columns=["date", "high", "low", "close"])
        t["date"] = pd.to_datetime(t["date"]).dt.date
        return [{"date": r.date, "high": r.high, "low": r.low, "close": r.close}
                for r in t.itertuples(index=False)]

    def evaluate_due(self, store, today: dt.date | None = None) -> dict:
        """EOD 全量评估：due 信号 × 各 horizon → outcomes upsert。

        completed 行不重算；retryable-unable（缺 anchor/窗口不足）重算；
        其余 unable 与 invalid 跳过。返回统计。
        """
        sigs = self._expire_due()
        outcomes = self._load_outcomes()
        bars_cache: dict[str, list[dict] | None] = {}
        now = dt.datetime.now().isoformat(timespec="seconds")
        evaluated = skipped = unable = 0
        for sig in sigs:
            direction = sig.get("direction")
            if not direction or sig.get("status") in ("invalidated", "archived"):
                continue
            code = sig["code"]
            if code not in bars_cache:
                bars_cache[code] = self._hfq_bars(store, code)
            bars = bars_cache[code]
            anchor = dt.date.fromisoformat(str(sig["anchor_date"]))
            for h, n_days in HORIZONS:
                key = (int(sig["id"]), h)
                old = outcomes.get(key)
                if old and old.get("eval_status") == "completed":
                    continue
                if old and old.get("eval_status") == "unable" \
                        and old.get("unable_reason") not in _RETRYABLE:
                    continue
                row = {"signal_id": int(sig["id"]), "horizon": h,
                       "engine_version": ENGINE_VERSION,
                       "direction_expected": direction,
                       "anchor_date": str(sig["anchor_date"]),
                       "created_at": (old or {}).get("created_at") or now,
                       "updated_at": now}
                if bars is None:
                    row.update(eval_status="unable",
                               unable_reason="missing_stock_data")
                else:
                    anchor_bars = [b for b in bars if b["date"] == anchor]
                    anchor_close = (_finite_optional_float(anchor_bars[0]["close"])
                                    if anchor_bars else None)
                    if anchor_close is None or anchor_close <= 0:
                        row.update(eval_status="unable",
                                   unable_reason="missing_anchor_price")
                    else:
                        fwd = [b for b in bars if b["date"] > anchor][:n_days]
                        res = evaluate_decision_signal(
                            direction_expected=direction,
                            start_price=anchor_close,
                            window_bars=fwd, eval_days=n_days)
                        row.update(res)
                outcomes[key] = row
                if row["eval_status"] == "completed":
                    evaluated += 1
                elif row.get("unable_reason") in _RETRYABLE:
                    unable += 1   # 下轮 EOD 自动补评
                else:
                    skipped += 1
        with self._lock:
            self._save_outcomes(outcomes)
        return {"signals": len(sigs), "evaluated": evaluated,
                "pending": unable, "skipped": skipped}

    # ------------------------------------------------------------ 统计

    def stats(self) -> list[dict]:
        """按 plugin × horizon 聚合命中率（分母不含 neutral；样本 <30 标记）。"""
        sigs = {int(r["id"]): r for r in self._signals.read()}
        rows = list(self._load_outcomes().values())
        agg: dict[tuple, dict] = {}
        for o in rows:
            if o.get("eval_status") != "completed":
                continue
            sid = int(o["signal_id"])
            sig = sigs.get(sid)
            if not sig:
                continue
            k = (sig.get("plugin"), o["horizon"])
            a = agg.setdefault(k, {"plugin": k[0], "horizon": k[1],
                                   "hit": 0, "miss": 0, "neutral": 0,
                                   "ret_sum": 0.0, "ret_n": 0})
            oc = o.get("outcome")
            if oc == "hit":
                a["hit"] += 1
            elif oc == "miss":
                a["miss"] += 1
            elif oc == "neutral":
                a["neutral"] += 1
            r = _finite_optional_float(o.get("stock_return_pct"))
            if r is not None:
                a["ret_sum"] += r
                a["ret_n"] += 1
        out = []
        for (plugin, horizon), a in sorted(agg.items(), key=lambda x: (x[0][0], x[0][1])):
            n_decided = a["hit"] + a["miss"]
            out.append({
                "plugin": plugin, "horizon": horizon,
                "hit": a["hit"], "miss": a["miss"], "neutral": a["neutral"],
                "n_decided": n_decided,
                "hit_rate_pct": round(a["hit"] / n_decided * 100, 1) if n_decided else None,
                "avg_return_pct": round(a["ret_sum"] / a["ret_n"], 2) if a["ret_n"] else None,
                "sample_sufficient": n_decided >= MIN_SAMPLE,
            })
        return out

    def summary_json(self) -> str:
        return json.dumps({"stats": self.stats()}, ensure_ascii=False, indent=1)
