"""模拟交易回测：T-1 选股 → T 日验证（walk-forward 推演）。

口径铁律（与 scans/scan_ultrashort.py 实盘扫描一致）：
- 一切比值/均线在后复权 hfq 序列内计算，跨股绝对价不可比；
- pct 一律自算（close/prev_close-1），禁止用 daily 的 pct_chg 列（已知有错）；
- amount 列质量差（末日几乎全空），成交额一律估算：
  amount_est = volume×100 × close(D-1) × latest_price / close_latest
  （latest_price 为 stock_basic 最新真实价，用同股 hfq 比值换算到信号日真实价；
  信号日=数据末日时退化为 volume×100×latest_price，与实盘扫描口径一致）；
- 交易日历 = daily_hfq 中出现的去重 date 升序。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

# ------------------------------------------------------------------ 常量

AMOUNT_MIN = 2.0e8          # D-1 成交额下限（元）
FLOAT_MV_MIN = 30e8         # 流通市值下限（元）
FLOAT_MV_MAX = 300e8        # 流通市值上限（元）
PCT_MIN = 0.05              # D-1 涨幅下限
VOL_RATIO_MIN = 2.0         # 量比下限
CLOSE_POS_MIN = 0.70        # 收盘位置下限
DAY_RANGE_MIN = 0.03        # (high-low)/prev_close 下限（非一字）
PCT20_MAX = 0.30            # 近 20 日累计涨幅上限
BOARD_PCT_MIN = 0.015       # 行业平均涨幅下限

STOP_PCT = 0.96             # T 日止损线（买价 ×）
TP_PCT = 1.06               # T 日止盈线（买价 ×）


# ------------------------------------------------------------------ 数据加载（懒加载单例）

class _Data:
    """全市场日线 + 基础信息 + 行业归属，进程内缓存（首次调用加载并预计算指标）。"""

    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir).resolve()
        t0 = time.time()

        df = pd.read_parquet(
            self.root / "dataset" / "daily_hfq.parquet",
            columns=["date", "code", "open", "high", "low", "close", "volume"],
        )
        df["date"] = pd.to_datetime(df["date"])
        df["code"] = df["code"].astype("category")
        df = df.sort_values(["code", "date"], kind="stable").reset_index(drop=True)

        # ---- 逐股预计算指标（groupby transform，一次性成本，之后按日切片零成本）----
        g = df.groupby("code", observed=True, sort=False)
        prev_close = g["close"].shift(1)
        # 量比分母：前 5 根 bar 的 volume 均值（不含当日；不足 5 根 = NaN）
        vol_prev5 = g["volume"].transform(
            lambda s: s.shift(1).rolling(5, min_periods=5).mean())
        # MA250：近 250 根（含当日）收盘均值，不足 250 根 = NaN
        ma250 = g["close"].transform(
            lambda s: s.rolling(250, min_periods=250).mean())
        # 近 60 日（含当日）收盘最高（上市不足 60 日取可得范围，与实盘扫描一致）
        max60 = g["close"].transform(lambda s: s.rolling(60, min_periods=1).max())
        # 20 日累计涨幅基准：20 根 bar 前的收盘
        close20 = g["close"].shift(20)
        for col, arr in [("prev_close", prev_close), ("vol_prev5", vol_prev5),
                         ("ma250", ma250), ("max60", max60), ("close20", close20)]:
            df[col] = arr.astype("float64")

        # 每股最新 hfq 收盘（amount 估算的真实价换算锚点）
        g = df.groupby("code", observed=True, sort=False)
        df["close_last"] = g["close"].transform("last").astype("float64")

        # 自算 pct（禁止用 pct_chg 列）
        df["pct"] = df["close"] / df["prev_close"] - 1.0

        # ---- 改按 date 排序 + 日期边界索引（按日切片 = 零拷贝 iloc 视图）----
        df = df.sort_values(["date", "code"], kind="stable").reset_index(drop=True)
        self.df = df
        dates_all = df["date"].values
        uniq = np.unique(dates_all)  # 升序
        self.dates = pd.DatetimeIndex(uniq)
        self._bounds = {np.datetime64(d): (int(np.searchsorted(dates_all, d, "left")),
                                           int(np.searchsorted(dates_all, d, "right")))
                        for d in uniq}

        # ---- 基础信息（真实价/市值/名称）----
        basic = pd.read_parquet(self.root / "dataset" / "stock_basic.parquet")
        basic = basic.set_index("code")
        self.basic = basic
        self.latest_price = basic["latest_price"].astype("float64")

        # ---- 行业归属 ----
        bm = pd.read_parquet(self.root / "board_members.parquet")
        ind = bm[bm["board_type"] == "industry"][["code", "board_code", "board_name"]]
        self.industry = ind.drop_duplicates("code").set_index("code")[
            ["board_code", "board_name"]]

        self.load_seconds = round(time.time() - t0, 1)

    def day(self, d) -> pd.DataFrame:
        """某交易日全市场切片（零拷贝视图，含预计算指标列）。"""
        key = pd.Timestamp(d).to_datetime64()
        lo, hi = self._bounds[key]
        return self.df.iloc[lo:hi]


_lock = threading.Lock()
_cache: dict[str, _Data] = {}


def _get_data(data_dir) -> _Data:
    key = str(Path(data_dir).resolve())
    data = _cache.get(key)
    if data is None:
        with _lock:
            data = _cache.get(key)
            if data is None:
                data = _Data(data_dir)
                _cache[key] = data
    return data


# ------------------------------------------------------------------ 策略：超短动量

def _pick_ultrashort(data: _Data, signal_date, topn: int = 10) -> list[dict]:
    """超短动量：signal_date 收盘后选股，返回按综合分排序的 TopN picks。

    过滤规则（与实盘扫描 scans/scan_ultrashort.py 同口径，仅去掉其中的
    指定代码剔除与 8a 距高点免检两条本地化规则）：
    1. 非 ST/*ST/退市；2. 流通市值 30~300 亿；3. D-1 成交额(估) ≥ 2 亿；
    4. 自算 pct ≥ +5%；5. 量比 ≥ 2.0；6. 收盘位置 ≥ 0.70 且非一字；
    7. 结构突破（8a 首次站上 MA250 / 8b 近 60 日收盘新高）；8. 近 20 日涨幅 ≤ +30%；
    9. 行业平均自算 pct ≥ +1.5%（无行业归属不剔除）。
    """
    d = pd.Timestamp(signal_date)
    day = data.day(d)
    if day.empty:
        return []

    codes = day["code"].astype(str).values
    basic = data.basic
    bsub = basic.reindex(codes)
    st_mask = bsub["name"].astype(str).str.contains("ST|退", na=False).values
    float_mv = bsub["float_mv"].astype("float64").values
    latest_price = bsub["latest_price"].astype("float64").values

    close = day["close"].values
    high = day["high"].values
    low = day["low"].values
    volume = day["volume"].values
    prev_close = day["prev_close"].values
    vol_prev5 = day["vol_prev5"].values
    ma250 = day["ma250"].values
    max60 = day["max60"].values
    close20 = day["close20"].values
    close_last = day["close_last"].values
    pct = day["pct"].values

    # 成交额估算（元）：volume(手)×100 × 真实价(signal_date)
    # 真实价 = latest_price × close(D-1)/close_latest（同股 hfq 比值换算）
    with np.errstate(divide="ignore", invalid="ignore"):
        real_price = latest_price * close / close_last
    amount_arr = volume * 100.0 * real_price
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_ratio_arr = volume / vol_prev5
    with np.errstate(divide="ignore", invalid="ignore"):
        pct20_arr = close / close20 - 1.0

    ok = (
        ~st_mask                                            # 1
        & (float_mv >= FLOAT_MV_MIN) & (float_mv <= FLOAT_MV_MAX)  # 2
        & (amount_arr >= AMOUNT_MIN)                     # 3
        & (pct >= PCT_MIN)                                    # 4
        & np.isfinite(vol_prev5) & (vol_prev5 > 0) & (vol_ratio_arr >= VOL_RATIO_MIN)  # 5
    )
    rng = high - low
    with np.errstate(divide="ignore", invalid="ignore"):
        close_pos = (close - low) / rng
    ok &= (
        (rng > 0) & (close_pos >= CLOSE_POS_MIN)              # 6
        & (rng / prev_close >= DAY_RANGE_MIN)
    )
    sig_8a_arr = (prev_close <= ma250) & (close > ma250)      # 7a（首站年线）
    sig_8b_arr = close >= max60                               # 7b（60 日收盘新高）
    ok &= (sig_8a_arr | sig_8b_arr)
    ok &= np.isfinite(pct20_arr) & (pct20_arr <= PCT20_MAX)   # 8
    ok &= np.isfinite(pct) & (prev_close > 0)

    idx = day.index[ok]
    if len(idx) == 0:
        return []

    sub = day.loc[idx].copy()
    sub_codes = sub["code"].astype(str).values
    sub["vol_ratio"] = vol_ratio_arr[ok]
    sub["close_pos"] = close_pos[ok]
    sub["pct20"] = pct20_arr[ok]
    sub["float_mv"] = float_mv[ok]
    sub["amount_est"] = amount_arr[ok]
    sub["signal_type"] = np.where(sig_8a_arr[ok], "8a", "8b")

    # 行业平均涨幅（当日全市场口径：含 ST，仅要求当日有 bar）
    board_code = data.industry["board_code"].reindex(sub_codes)
    day_codes_all = day["code"].astype(str).values
    bc_all = data.industry["board_code"].reindex(day_codes_all)
    board_avg = pd.Series(day["pct"].values).groupby(bc_all.values).mean()
    sub["board_avg_pct"] = board_code.map(board_avg).values

    # 9. 板块效应（无行业归属跳过不剔除）
    keep = sub["board_avg_pct"].isna() | (sub["board_avg_pct"] >= BOARD_PCT_MIN)
    sub = sub[keep.values].reset_index(drop=True)
    sub_codes = sub["code"].astype(str).values

    score = (sub["vol_ratio"].values * sub["close_pos"].values
             * np.nan_to_num(sub["board_avg_pct"].values))

    order = np.argsort(-score)[:topn]
    out = []
    names = data.basic["name"].reindex(sub_codes)
    for k in order:
        code = sub_codes[k]
        out.append({
            "code": code,
            "name": str(names.iloc[k]) if pd.notna(names.iloc[k]) else code,
            "pct": _round(sub["pct"].values[k], 4),
            "vol_ratio": _round(sub["vol_ratio"].values[k], 2),
            "close_pos": _round(sub["close_pos"].values[k], 2),
            "pct20": _round(sub["pct20"].values[k], 4),
            "float_mv_yi": _round(sub["float_mv"].values[k] / 1e8, 1),
            "amount_yi": _round(sub["amount_est"].values[k] / 1e8, 2),
            "signal_type": sub["signal_type"].values[k],
            "score": _round(score[k], 4),
        })
    return out


def _round(x, nd):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(v) else round(v, nd)


# ------------------------------------------------------------------ 回测引擎

def _validate_t(data: _Data, pick: dict, t_date) -> dict:
    """T 日验证：开盘买入，止损/止盈/收盘退出（保守：同日双触发先记止损）。"""
    day = data.day(t_date)
    row = day[day["code"].astype(str) == pick["code"]]
    pick = dict(pick)
    if row.empty:
        pick.update(exit_type="untradeable_suspended", t_open=None, t_close=None,
                    ret=None)
        return pick
    r = row.iloc[0]
    o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
    pick["t_open"], pick["t_close"] = _round(o, 4), _round(c, 4)
    if o == h == l:  # 一字板（开盘即涨停/跌停，无法按开盘价成交）
        pick.update(exit_type="untradeable_limit", ret=None)
        return pick
    if l <= o * STOP_PCT:                       # 止损优先（保守假设）
        pick.update(exit_type="stop", ret=_round(o * STOP_PCT / o - 1.0, 4))
    elif h >= o * TP_PCT:
        pick.update(exit_type="tp", ret=_round(o * TP_PCT / o - 1.0, 4))
    else:
        pick.update(exit_type="close", ret=_round(c / o - 1.0, 4))
    return pick


def run_backtest(days: int = 20, topn: int = 10, data_dir="data",
                 strategy: str = "ultrashort") -> dict:
    """最近 N 个交易日为 T（含数据末日），每个 T 用前一交易日信号选股并验证。

    汇总胜率/平均收益/止盈止损触发率等；另附 latest_signal = 数据末日的
    最新信号（次一交易日待验证，不计入统计）。
    """
    fn = STRATEGIES.get(strategy)
    if fn is None:
        raise ValueError(f"未知策略: {strategy}")
    days = int(max(1, min(days, 250)))
    topn = int(max(1, min(topn, 50)))

    data = _get_data(data_dir)
    cal = data.dates
    if len(cal) < 2:
        raise ValueError("交易日历不足两天，无法回测")

    t_dates = cal[-days:]
    daily = []
    total_picks = tradeable = wins = tp_n = stop_n = t_up = 0
    ret_sum = 0.0
    best = worst = None
    untradeable = {"suspended": 0, "limit": 0}

    for i, t in enumerate(t_dates):
        signal = cal[cal.get_loc(t) - 1]
        picks = fn(data, signal, topn)
        if not picks:
            daily.append({"signal_date": signal.strftime("%Y-%m-%d"),
                          "t_date": t.strftime("%Y-%m-%d"), "picks": [],
                          "n_picks": 0, "tradeable": 0, "win_rate": None,
                          "avg_ret": None})
            continue
        picks = [_validate_t(data, p, t) for p in picks]
        rets = [p["ret"] for p in picks if p["ret"] is not None]
        day_tr = len(rets)
        day_wins = sum(1 for r in rets if r > 0)
        for p in picks:
            et = p["exit_type"]
            if et == "untradeable_suspended":
                untradeable["suspended"] += 1
            elif et == "untradeable_limit":
                untradeable["limit"] += 1
        day_tp = sum(1 for p in picks if p["exit_type"] == "tp")
        day_stop = sum(1 for p in picks if p["exit_type"] == "stop")
        day_up = sum(1 for p in picks
                     if p["t_open"] is not None and p["t_close"] is not None
                     and p["t_close"] > p["t_open"])
        total_picks += len(picks)
        tradeable += day_tr
        wins += day_wins
        tp_n += day_tp
        stop_n += day_stop
        t_up += day_up
        ret_sum += sum(rets)
        for p in picks:
            if p["ret"] is None:
                continue
            rec = {"code": p["code"], "name": p["name"], "ret": p["ret"],
                   "signal_date": signal.strftime("%Y-%m-%d"),
                   "t_date": t.strftime("%Y-%m-%d"), "exit_type": p["exit_type"]}
            if best is None or p["ret"] > best["ret"]:
                best = rec
            if worst is None or p["ret"] < worst["ret"]:
                worst = rec
        daily.append({
            "signal_date": signal.strftime("%Y-%m-%d"),
            "t_date": t.strftime("%Y-%m-%d"),
            "picks": picks,
            "n_picks": len(picks),
            "tradeable": day_tr,
            "win_rate": _round(day_wins / day_tr, 4) if day_tr else None,
            "avg_ret": _round(sum(rets) / day_tr, 4) if day_tr else None,
        })

    # 最新信号：数据末日收盘后选股（次一交易日验证，即实盘扫描的用途）
    latest_signal = None
    last_day = cal[-1]
    lp = fn(data, last_day, topn)
    if lp:
        latest_signal = {
            "signal_date": last_day.strftime("%Y-%m-%d"),
            "t_date": "（次一交易日，待验证）",
            "picks": lp, "n_picks": len(lp),
        }

    return {
        "days": days, "topn": topn, "strategy": strategy,
        "strategy_name": STRATEGY_NAMES.get(strategy, strategy),
        "date_max": last_day.strftime("%Y-%m-%d"),
        "total_picks": total_picks, "tradeable": tradeable, "wins": wins,
        "win_rate": _round(wins / tradeable, 4) if tradeable else None,
        "avg_ret": _round(ret_sum / tradeable, 4) if tradeable else None,
        "t_up_rate": _round(t_up / tradeable, 4) if tradeable else None,
        "tp_rate": _round(tp_n / tradeable, 4) if tradeable else None,
        "stop_rate": _round(stop_n / tradeable, 4) if tradeable else None,
        "untradeable": untradeable,
        "daily": daily, "best": best, "worst": worst,
        "latest_signal": latest_signal,
    }


STRATEGIES = {
    "ultrashort": _pick_ultrashort,
}
STRATEGY_NAMES = {
    "ultrashort": "超短动量",
}
