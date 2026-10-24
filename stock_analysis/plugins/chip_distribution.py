"""筹码分布与成本均线模型（Chip Distribution Model）。

所属维度: 筹码微观结构与资金筹码。
目标: 基于每日换手率对历史成交价格做衰减加权，逐日构建筹码成本结构，
输出获利比例 / 平均持仓成本（成本均线）/ 70%·90% 筹码集中度，
识别主力建仓·洗盘特征与关键支撑/阻力位，并对"关键支撑与阻力位预测"
做可实测的质量回测。

模型结构（四层）:

1) 筹码分布（换手衰减法，与通达信/同花顺同源，纯 numpy 向量化）:
   每天先让存量筹码按当日换手率 t 整体衰减 chips *= (1 - t)
   （假设当日成交的筹码按比例来自全体持仓），再把当日成交量按三角形
   分布（峰在近似均价 (O+C)/2）摊到 [low, high]，逐日累加。
   换手率 = 成交量/流通股本（流通股本 = float_mv/latest_price，历史近似，
   与仪表盘筹码面板同一算法）；turnover 有效则优先使用，amount 仅在具备
   逐日 adj_factor 时用于 VWAP，否则回退到同复权口径的 (O+C)/2。
   首个可见价格确定固定网格步长，随后仅随已发生行情扩展，不使用未来极值。

2) 成本结构指标（逐日时间序列，回测用）:
   - 获利比例 profit_ratio: 成本 < 现价的筹码占比（市场整体盈亏状态）
   - 成本均线 avg_cost: 筹码加权平均成本（= 全市场持仓的"均价"）
   - 集中度 conc70 = (P85-P15)/(P85+P15), conc90 = (P95-P5)/(P95+P5)
     （越小越集中; 筹码向窄带聚集 = 主力吸筹/洗盘完成的典型特征）
   - 支撑 support = P15, 阻力 resist = P85（筹码分布的成本支撑/抛压区边界）

3) 信号（方向，阈值取"深度区"分位，防过拟合说明见下）:
   S1 超跌支撑: profit_ratio ≤ 10% → +1
      （9 成持仓套牢、想卖的已卖，卖压衰竭，价格位于筹码下沿支撑区）
   S2 获利抛压: profit_ratio ≥ 90% → -1
      （近 9 成持仓获利，兑现抛压大，价格处于套牢区上沿下方）
   S3 集中建仓: conc70 较 20 日前收窄 ≥ 15% 且 close > avg_cost → +1
      （筹码加速集中 + 价格站上成本均线 = 建仓/洗盘完成特征）

4) regime 检测（针对模型已知弱点"超高换手率时滞"）:
   - shock: 单日换手率 ≥ 10%（筹码剧烈换手，成本结构被快速改写，
     短期成本/集中度估算存在时滞）→ 当日信号压制，仅保留结构展示
   - oversold / overbought / accumulating: 对应 S1/S2/S3 触发区
   - 其余: normal

特色输出:
   - 支撑/阻力位预测质量回测: 以 P15/P85 为支撑/阻力，测其后 10 日
     低点击穿支撑率 / 高点击穿阻力率，并与"以现价为心、等宽"的
     无信息基线对照 —— 若筹码区的击穿率显著低于基线，说明成本结构
     对区间边界有真实预测力（宣称"关键支撑与阻力位预测"的可实测版）;
   - 历史信号方向胜率（5/10/20 日，宣称 50%~58% 的可实测版本），
     并附无 regime 过滤对照组。

防过拟合说明: 获利比例阈值取 10%/90% 分位（"深度区"，教科书标准），
集中度改善窗口/幅度取 20 日/15%（经验初值，未做网格搜索），
换手率冲击阈值 10% 为 A 股单日换手极端值（全市场 P99.9 量级），
支撑/阻力测试视界 10 日固定。全部阈值在参数中声明、可调，
但交付结论以默认参数的实测为准（不调参追胜率）。

数据依赖: OHLCV（ctx.kline）+ 流通股本（ctx.basic 的 float_mv/latest_price），
需 ≥ 约 250 个交易日（建议 800 日让筹码分布充分形成）。
作者: quant-trading 项目（2026-10）
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from stock_analysis import AnalysisPlugin, AnalysisResult, ParamSpec

#: 回测前瞻期（交易日）
HORIZONS = (5, 10, 20)
#: 筹码分布预热期（前若干日分布质量不足，回测从此之后取信号/测区间）
WARMUP = 120
#: 初始价格网格密度（步长由样本首日价格确定，之后固定）
MAX_BINS = 200
_NICE_STEPS = (0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10,
               20, 50, 100, 200, 500, 1000)


# ------------------------------------------------------------------ 筹码分布


def _price_step(price: float) -> float:
    """由首个可见价格确定永久固定的网格步长，不依赖未来价格极值。"""
    raw = max(float(price), 1e-6) / MAX_BINS
    step = next((s for s in _NICE_STEPS if s >= raw), None)
    if step is not None:
        return step
    magnitude = 10.0 ** np.floor(np.log10(raw))
    return float(magnitude * np.ceil(raw / magnitude))


def _tri_weights(seg: np.ndarray, lo: float, hi: float, peak: float) -> np.ndarray:
    """[lo, hi] 上以 peak 为峰的三角形分布权重（和=1）。"""
    if not len(seg):
        return np.array([], dtype=float)
    if hi <= lo:
        w = np.zeros(len(seg))
        w[int(np.argmin(np.abs(seg - peak)))] = 1.0
        return w
    peak = min(max(peak, lo), hi)
    left = (seg - lo) / (peak - lo) if peak > lo else np.ones_like(seg)
    right = (hi - seg) / (hi - peak) if hi > peak else np.ones_like(seg)
    w = np.clip(np.minimum(left, right), 0.0, None)
    s = w.sum()
    return w / s if s > 0 else np.ones_like(seg) / len(seg)


def _chip_series(low: np.ndarray, high: np.ndarray, open_: np.ndarray,
                 close: np.ndarray, vol_sh: np.ndarray,
                 turnover: np.ndarray, amount: np.ndarray | None = None,
                 adj_factor: np.ndarray | None = None) -> dict:
    """逐日换手衰减筹码分布 → 各成本指标时间序列。

    输入均为升序日线的 numpy 数组（价格为同一复权口径，vol_sh 单位=股，
    turnover 单位=小数，缺失为 NaN；amount 单位=元，缺失为 NaN）。
    adj_factor 是未复权价转换为当前 OHLC 复权口径的逐日乘数；缺失时
    amount 不参与 VWAP 估算，避免混用实际成交价与复权 OHLC。
    返回 dict of 与输入等长的数组:
    profit_ratio / avg_cost / p5 / p15 / p50 / p85 / p95 / peak / total。
    """
    n = len(close)
    t = np.where(np.isfinite(turnover), turnover, 0.01)  # 缺失回退 1%/日
    t = np.clip(t, 0.0, 1.0)
    vwap = (open_ + close) / 2.0
    if amount is not None and adj_factor is not None:
        with np.errstate(divide="ignore", invalid="ignore"):
            adjusted_vwap = amount / np.maximum(vol_sh, 1.0) * adj_factor
        usable = (np.isfinite(adjusted_vwap) & (adjusted_vwap > 0)
                  & np.isfinite(adj_factor) & (adj_factor > 0) & (vol_sh > 0))
        vwap = np.where(usable, adjusted_vwap, vwap)

    valid_close = close[np.isfinite(close) & (close > 0)]
    if not len(valid_close):
        raise ValueError("筹码分布需要至少一个有效正收盘价")
    step = _price_step(float(valid_close[0]))
    bins = np.zeros(1)
    chips = np.zeros(1)
    out = {k: np.full(n, np.nan) for k in
           ("profit_ratio", "avg_cost", "p5", "p15", "p50", "p85", "p95",
            "peak", "total")}
    for i in range(n):
        prices = (low[i], high[i], close[i], vwap[i])
        max_price = max((float(p) for p in prices if np.isfinite(p) and p > 0),
                        default=step)
        required = int(np.floor(max_price / step)) + 2
        if required > len(chips):
            chips = np.pad(chips, (0, required - len(chips)))
            bins = np.arange(required, dtype=float) * step

        if t[i] > 0:
            chips *= (1.0 - t[i])
        v = vol_sh[i]
        if np.isfinite(v) and v > 0:
            peak = vwap[i] if np.isfinite(vwap[i]) else (low[i] + high[i]) / 2.0
            peak = min(max(peak, low[i]), high[i])
            i0 = max(0, int(np.floor(low[i] / step)))
            i1 = min(len(chips) - 1, int(np.ceil(high[i] / step)))
            if i1 < i0:
                i0 = i1 = int(np.clip(np.rint(peak / step), 0, len(chips) - 1))
            chips[i0:i1 + 1] += _tri_weights(
                bins[i0:i1 + 1], low[i], high[i], peak) * v
        tot = chips.sum()
        if tot <= 0:
            continue
        cdf = np.cumsum(chips) / tot
        # 每个价格箱的质量位于箱中心，避免现价恰在网格点时把整箱都计为获利。
        cdf_mid = np.clip(cdf - 0.5 * chips / tot, 0.0, 1.0)
        out["profit_ratio"][i] = float(np.interp(close[i], bins, cdf_mid))
        out["avg_cost"][i] = float((bins * chips).sum() / tot)
        for k, q in (("p5", 0.05), ("p15", 0.15), ("p50", 0.50),
                     ("p85", 0.85), ("p95", 0.95)):
            out[k][i] = float(np.interp(q, cdf, bins))
        out["peak"][i] = float(bins[int(np.argmax(chips))])
        out["total"][i] = float(tot)
    return out


def _zone_tests(low: np.ndarray, high: np.ndarray, close: np.ndarray,
                p15: np.ndarray, p85: np.ndarray, n: int, zh: int,
                hold: int, warmup: int) -> dict:
    """支撑/阻力位质量回测（两种口径 × 筹码位/等宽基线）。

    (a) 包络: 价格尚未到位的日子里 (close≤P85 / close≥P15), 后 zh 日
        max(high)/min(low) 是否击穿 P85/P15（击穿率, 越低越"硬"）;
    (b) 触及-收回: 后 zh 日内价格首次触及该位后, 其后 hold 日收盘是否
        全部收回位内（收回率, 越高越"硬"）。
    基线均为"以现价为心、与筹码区同半宽度"的静态位（无信息对照）。
    """
    out = {"resist_break": None, "resist_break_base": None,
           "support_break": None, "support_break_base": None,
           "resist_touch_n": 0, "resist_touch_hold": None,
           "resist_touch_hold_base": None,
           "support_touch_n": 0, "support_touch_hold": None,
           "support_touch_hold_base": None}
    t0, t1 = warmup, n - zh
    if n <= t0 + 1 or t1 <= t0:
        return out
    seg_hi = np.full(n - zh, np.nan)
    seg_lo = np.full(n - zh, np.nan)
    for t in range(0, n - zh):
        seg_hi[t] = high[t + 1:t + 1 + zh].max()
        seg_lo[t] = low[t + 1:t + 1 + zh].min()
    w_rel = (p85[t0:t1] - p15[t0:t1]) / (2.0 * close[t0:t1])
    vseg = np.isfinite(p15[t0:t1]) & np.isfinite(p85[t0:t1]) & np.isfinite(w_rel) \
        & (w_rel > 0)
    in_resist = vseg & (close[t0:t1] <= p85[t0:t1])
    in_support = vseg & (close[t0:t1] >= p15[t0:t1])
    hi, lo = seg_hi[t0:t1], seg_lo[t0:t1]
    pr15, pr85, pc = p15[t0:t1], p85[t0:t1], close[t0:t1]
    if in_resist.any():
        out["resist_break"] = float((hi[in_resist] > pr85[in_resist]).mean())
        out["resist_break_base"] = float(
            (hi[in_resist] > pc[in_resist] * (1 + w_rel[in_resist])).mean())
    if in_support.any():
        out["support_break"] = float((lo[in_support] < pr15[in_support]).mean())
        out["support_break_base"] = float(
            (lo[in_support] < pc[in_support] * (1 - w_rel[in_support])).mean())
    # ---- (b) 触及-收回（n≤800, 逐日循环足够快）----
    rt = rh = rtb = rhtb = 0    # 阻力: 触及数 / 收回数, 基线同
    st = sh = stb = shtb = 0    # 支撑
    for t in range(t0, min(n - zh - hold, t1)):
        if not (np.isfinite(p15[t]) and np.isfinite(p85[t])):
            continue
        w = (p85[t] - p15[t]) / (2.0 * close[t])
        if not np.isfinite(w) or w <= 0:
            continue
        lv_r, lv_rb = p85[t], close[t] * (1 + w)
        lv_s, lv_sb = p15[t], close[t] * (1 - w)
        hi_w = high[t + 1:t + 1 + zh]
        lo_w = low[t + 1:t + 1 + zh]
        cl_w = close[t + 1:t + 1 + zh + hold]
        hit = hi_w >= lv_r
        if hit.any():
            rt += 1
            j = int(np.argmax(hit))
            tail = cl_w[j + 1:j + 1 + hold]
            rh += bool(tail.size and tail.max() < lv_r)
        hitb = hi_w >= lv_rb
        if hitb.any():
            rtb += 1
            j = int(np.argmax(hitb))
            tail = cl_w[j + 1:j + 1 + hold]
            rhtb += bool(tail.size and tail.max() < lv_rb)
        hits = lo_w <= lv_s
        if hits.any():
            st += 1
            j = int(np.argmax(hits))
            tail = cl_w[j + 1:j + 1 + hold]
            sh += bool(tail.size and tail.min() > lv_s)
        hitsb = lo_w <= lv_sb
        if hitsb.any():
            stb += 1
            j = int(np.argmax(hitsb))
            tail = cl_w[j + 1:j + 1 + hold]
            shtb += bool(tail.size and tail.min() > lv_sb)
    out["resist_touch_n"] = rt
    out["resist_touch_hold"] = rh / rt if rt else None
    out["resist_touch_hold_base"] = rhtb / rtb if rtb else None
    out["support_touch_n"] = st
    out["support_touch_hold"] = sh / st if st else None
    out["support_touch_hold_base"] = shtb / stb if stb else None
    return out


def warmup_mask(n: int, warmup: int) -> np.ndarray:
    """前 warmup 个位置为 True（分布预热期）。"""
    m = np.zeros(n, dtype=bool)
    m[:warmup] = True
    return m


# ------------------------------------------------------------------ 插件


class ChipDistributionPlugin(AnalysisPlugin):
    name = "chip_distribution"
    display_name = "筹码分布与成本均线"
    description = (
        "换手衰减法逐日构建筹码成本结构：获利比例/平均成本(成本均线)/70·90%集中度，"
        "超跌支撑(获利≤10%)/获利抛压(≥90%)/筹码集中建仓信号，"
        "超高换手率时滞 regime 检测，支撑/阻力位 10 日击穿率 vs 等宽基线回测"
    )
    version = "0.1.0"
    category = "capital"
    params = {
        "pr_low": ParamSpec(type="float", default=0.10, min=0.02, max=0.30,
                            help="超跌支撑: 获利比例 ≤ 该值（深度套牢区）"),
        "pr_high": ParamSpec(type="float", default=0.90, min=0.70, max=0.98,
                             help="获利抛压: 获利比例 ≥ 该值（普遍获利区）"),
        "conc_window": ParamSpec(type="int", default=20, min=5, max=60,
                                 help="集中度改善观察窗口（日）"),
        "conc_gain": ParamSpec(type="float", default=0.15, min=0.05, max=0.50,
                               help="S3 建仓: conc70 窗口内相对收窄 ≥ 该值"),
        "shock_turnover": ParamSpec(type="float", default=0.10, min=0.03, max=0.30,
                                    help="换手冲击 regime: 单日换手率 ≥ 该值（成本估算时滞）"),
        "zone_horizon": ParamSpec(type="int", default=10, min=3, max=20,
                                  help="支撑/阻力击穿率测试视界（日）"),
    }

    def run(self, ctx, code: str, pr_low: float = 0.10, pr_high: float = 0.90,
            conc_window: int = 20, conc_gain: float = 0.15,
            shock_turnover: float = 0.10,
            zone_horizon: int = 10) -> AnalysisResult:
        need = max(250, conc_window + 120, WARMUP + 40)
        df = ctx.kline(code, last=max(need, 800))
        r = AnalysisResult(self.name, code)
        if len(df) < need:
            r.summary = (f"历史数据不足（筹码分布需约 {need} 个交易日，"
                         f"实际 {len(df)}），跳过")
            r.signal("warn", "历史数据不足")
            return r

        close = df["close"].astype(float)
        idx = df.index
        n = len(df)
        low = df["low"].astype(float).to_numpy()
        high = df["high"].astype(float).to_numpy()
        open_ = df["open"].astype(float).to_numpy()
        close_np = close.to_numpy()
        vol = df["volume"].astype(float).to_numpy() * 100.0  # 手 -> 股
        amount = df["amount"].astype(float).to_numpy()
        adj_factor = (df["adj_factor"].astype(float).to_numpy()
                      if "adj_factor" in df.columns else None)
        turnover_col = df["turnover"].astype(float).to_numpy() / 100.0

        # ---- 换手率: kline 列有效则用，否则 成交量/流通股本 ----
        float_shares = 0.0
        try:
            b = ctx.basic(code)
            fmv, lp = b.get("float_mv"), b.get("latest_price")
            if fmv and lp and float(lp) > 0:
                float_shares = float(fmv) / float(lp)
        except Exception:  # noqa: BLE001
            float_shares = 0.0
        turnover_est = False
        if np.isfinite(turnover_col).mean() > 0.5:
            turnover = np.where(np.isfinite(turnover_col) & (turnover_col > 0),
                                turnover_col, 0.01)
        elif float_shares > 0:
            turnover = vol / float_shares
            turnover = np.where(np.isfinite(turnover) & (vol > 0), turnover, 0.0)
            turnover_est = True
        else:
            turnover = np.full(n, 0.01)  # 无股本信息: 常数 1%/日
            turnover_est = True
        turnover = np.clip(np.where(np.isfinite(turnover), turnover, 0.01), 0.0, 1.0)

        # ============ 1) 逐日筹码分布（VWAP 与 OHLC 保持同一复权口径）============
        cs = _chip_series(low, high, open_, close_np, vol, turnover, amount, adj_factor)
        pr = cs["profit_ratio"]
        avg_cost = cs["avg_cost"]
        p5, p15, p50, p85, p95 = cs["p5"], cs["p15"], cs["p50"], cs["p85"], cs["p95"]
        valid = np.isfinite(pr)
        if int(valid.sum()) < need - 50:
            r.summary = "筹码分布有效天数过少（换手/成交量异常），跳过"
            r.signal("warn", "筹码分布不可用")
            return r

        conc70 = (p85 - p15) / (p85 + p15)
        conc90 = (p95 - p5) / (p95 + p5)
        # S3: 集中度较 conc_window 日前收窄 ≥ conc_gain 且价上成本线
        cw = int(conc_window)
        with np.errstate(invalid="ignore"):
            c_prev = np.full(n, np.nan)
            c_prev[cw:] = conc70[:-cw]
            conc_improve = (c_prev - conc70) / np.where(c_prev > 0, c_prev, np.nan)
        s1 = valid & (pr <= pr_low)
        s2 = valid & (pr >= pr_high)
        s3 = valid & np.isfinite(conc_improve) & (conc_improve >= conc_gain) \
            & (close_np > avg_cost)
        shock = valid & (turnover >= shock_turnover)

        sig_dir = np.where(s1, 1, np.where(s2, -1, np.where(s3, 1, 0))).astype(float)
        sig_dir[warmup_mask(n, WARMUP)] = 0.0
        sig_dir_full = sig_dir.copy()
        sig_dir = np.where(shock, 0.0, sig_dir)          # 时滞 regime 压制信号
        sig_type = np.where(s1, "support", np.where(s2, "resist",
                                                    np.where(s3, "conc", "")))
        regime = np.select(
            [shock, s1, s2, s3],
            ["shock", "oversold", "overbought", "accumulating"],
            default="normal").astype(str)
        # 注意: 不能用 `for i in mask: arr[i]=...` —— numpy 2.x 中 0 维布尔标量
        # 索引会选中整个数组; 必须用一维布尔掩码整体赋值。
        regime[warmup_mask(n, WARMUP)] = "warmup"

        # ============ 2) 回测: 信号方向胜率（5/10/20 日）============
        # 信号冷却: 同一信号类型在 SIGNAL_COOLDOWN 日内只计首次触发，
        # 避免"持续超卖 N 天 = N 个信号"的连续重复计数（胜率按独立事件统计）。
        SIGNAL_COOLDOWN = 10

        def _cooldown(mask_dir):
            out = mask_dir.copy()
            last_fired = {}
            for t in range(WARMUP, n):
                if int(out[t]) == 0:
                    continue
                ty = str(sig_type[t])
                if ty in last_fired and t - last_fired[ty] < SIGNAL_COOLDOWN:
                    out[t] = 0.0
                else:
                    last_fired[ty] = t
            return out

        def _bt(mask_dir):
            rows = []
            wins = {h: [] for h in HORIZONS}
            fwds = {h: [] for h in HORIZONS}
            for t in range(WARMUP, n):
                d = int(mask_dir[t])
                if d == 0 or not valid[t]:
                    continue
                row = {"type": str(sig_type[t]), "close": float(close_np[t]),
                       "profit_ratio": float(pr[t]), "conc70": float(conc70[t])}
                for h in HORIZONS:
                    fwd = float(close_np[t + h] / close_np[t] - 1.0) \
                        if t + h < n else float("nan")
                    row[f"fwd{h}"] = fwd
                    if np.isfinite(fwd):
                        fwds[h].append(fwd)
                        wins[h].append(bool(fwd > 0 if d > 0 else fwd < 0))
                rows.append((idx[t], row))
            stats = {h: {"n": len(wins[h]),
                         "win": float(np.mean(wins[h])) if wins[h] else float("nan"),
                         "mean": float(np.mean(fwds[h])) if fwds[h] else float("nan")}
                     for h in HORIZONS}
            return rows, stats

        rows, stats = _bt(sig_dir)                     # 正式·按日（含时滞过滤）
        sig_dir_ev = _cooldown(sig_dir)                 # 正式·独立事件（冷却后）
        rows_ev, stats_ev = _bt(sig_dir_ev)
        rows_base, stats_base = _bt(sig_dir_full)       # 对照组（无 regime 过滤）

        # ---- 分类型计数与 10 日胜率（按独立事件，展示用）----
        type_stat = {ty: {"n": 0, "ws": []} for ty in ("support", "resist", "conc")}
        for t in range(WARMUP, n):
            ty = str(sig_type[t])
            d = int(sig_dir_ev[t])
            if ty not in type_stat or d == 0 or not valid[t]:
                continue
            type_stat[ty]["n"] += 1
            if t + 10 < n:
                fwd = float(close_np[t + 10] / close_np[t] - 1.0)
                type_stat[ty]["ws"].append(bool(fwd > 0 if d > 0 else fwd < 0))
        type_stat = {ty: {"n": v["n"],
                          "win10": float(np.mean(v["ws"])) if v["ws"] else float("nan")}
                     for ty, v in type_stat.items()}

        # ============ 3) 支撑/阻力位预测质量（10 日击穿率 vs 等宽基线）============
        # ============ 3) 支撑/阻力位预测质量（两种口径 vs 等宽基线）============
        # (a) 10 日包络: 以 P15/P85 为界, 测其后 zone_horizon 日价格是否全部
        #     留在区内（包络测试, 类比 GARCH 区间命中率）;
        # (b) 触及-收回: 其后 zone_horizon 日内价格首次触及该位后, hold 日内
        #     收盘是否收回位内（障碍测试, 直接对应"关键支撑/阻力位预测"）。
        # 基线均为"以现价为心、与筹码区同半宽度"的静态位（无信息对照）。
        zh = int(zone_horizon)
        zh_hold = 2
        zone = _zone_tests(low, high, close_np, p15, p85, n, zh, zh_hold, WARMUP)
        (resist_break, resist_break_base, support_break, support_break_base,
         resist_touch_n, resist_touch_hold, resist_touch_hold_base,
         support_touch_n, support_touch_hold, support_touch_hold_base) = (
            zone["resist_break"], zone["resist_break_base"],
            zone["support_break"], zone["support_break_base"],
            zone["resist_touch_n"], zone["resist_touch_hold"],
            zone["resist_touch_hold_base"],
            zone["support_touch_n"], zone["support_touch_hold"],
            zone["support_touch_hold_base"])

        # ============ 4) 当日状态 ============
        last = n - 1
        p_last = float(close_np[last])
        pr0 = float(pr[last]) if valid[last] else float("nan")
        ac0 = float(avg_cost[last]) if valid[last] else float("nan")
        mc0 = float(p50[last]) if valid[last] else float("nan")
        c70 = float(conc70[last]) if valid[last] else float("nan")
        c90 = float(conc90[last]) if valid[last] else float("nan")
        sup0 = float(p15[last]) if valid[last] else float("nan")
        res0 = float(p85[last]) if valid[last] else float("nan")
        pk0 = float(cs["peak"][last]) if valid[last] else float("nan")
        r90lo, r90hi = (float(p5[last]), float(p95[last])) if valid[last] else (float("nan"),) * 2
        to0 = float(turnover[last]) if valid[last] else float("nan")
        to20 = float(np.nanmean(turnover[max(0, last - 19):last + 1]))
        regime0 = str(regime[last])
        fired = int(sig_dir[last])
        cimp0 = float(conc_improve[last]) if valid[last] and np.isfinite(conc_improve[last]) else float("nan")

        r.metric("close", p_last, "最新收盘", 3)
        r.metric("avg_cost", ac0, "平均持仓成本(成本均线)", 3)
        r.metric("median_cost", mc0, "成本中位数(P50)", 3)
        r.metric("profit_ratio", pr0, "获利比例(成本<现价的筹码占比)", 3)
        r.metric("conc70", c70, "70%筹码集中度(越小越集中)", 3)
        r.metric("conc90", c90, "90%筹码集中度", 3)
        r.metric("support", sup0, "支撑位(P15 成本下沿)", 3)
        r.metric("resistance", res0, "阻力位(P85 成本上沿)", 3)
        r.metric("peak_price", pk0, "主力筹码峰(最大成本集中点)", 3)
        r.metric("range90_lo", r90lo, "90%筹码区间下沿(P5)", 3)
        r.metric("range90_hi", r90hi, "90%筹码区间上沿(P95)", 3)
        r.metric("turnover", to0, "今日换手率(估算)", 4)
        r.metric("turnover_avg20", to20, "20日均换手率", 4)
        r.metric("conc_improve", cimp0, f"集中度{cw}日改善率(收窄为正)", 3)
        r.metric("regime", regime0, "筹码状态(冲击/超跌/获利/建仓/常规)", 1)
        r.metric("signal_today", fired, "今日信号(+1支撑反弹/-1获利抛压)", 1)
        r.metric("bt_count", len(rows), "历史信号日数(含时滞过滤)", 1)
        r.metric("bt_count_ev", len(rows_ev), "独立信号事件数(10日冷却)", 1)
        r.metric("bt_count_base", len(rows_base), "历史信号日数(无regime过滤)", 1)
        r.metric("bt_n_support", type_stat["support"]["n"], "事件数: 超跌支撑(S1)", 1)
        r.metric("bt_n_resist", type_stat["resist"]["n"], "事件数: 获利抛压(S2)", 1)
        r.metric("bt_n_conc", type_stat["conc"]["n"], "事件数: 集中建仓(S3)", 1)
        for h in HORIZONS:
            r.metric(f"bt_win{h}", stats[h]["win"],
                     f"回测{h}日方向胜率(按日, n={stats[h]['n']})", 3)
        for h in HORIZONS:
            r.metric(f"bt_win{h}_ev", stats_ev[h]["win"],
                     f"回测{h}日方向胜率(独立事件, n={stats_ev[h]['n']})", 3)
        for h in (5, 20):
            r.metric(f"bt_win{h}_base", stats_base[h]["win"],
                     f"回测{h}日方向胜率(对照组, n={stats_base[h]['n']})", 3)
        r.metric("resist_break", resist_break,
                 f"阻力位{zh}日包络击穿率(低=硬, n≈{n - 2 * WARMUP - zh})", 3)
        r.metric("resist_break_base", resist_break_base,
                 f"等宽基线{zh}日包络击穿率(对照)", 3)
        r.metric("support_break", support_break,
                 f"支撑位{zh}日包络击穿率(低=硬)", 3)
        r.metric("support_break_base", support_break_base,
                 f"等宽基线{zh}日包络击穿率(对照)", 3)
        r.metric("resist_touch_hold", resist_touch_hold,
                 f"阻力位触及后{zh_hold}日收回率(高=硬, n={resist_touch_n})", 3)
        r.metric("resist_touch_hold_base", resist_touch_hold_base,
                 f"等宽基线触及收回率(对照)", 3)
        r.metric("support_touch_hold", support_touch_hold,
                 f"支撑位触及后{zh_hold}日收回率(高=硬, n={support_touch_n})", 3)
        r.metric("support_touch_hold_base", support_touch_hold_base,
                 f"等宽基线触及收回率(对照)", 3)
        r.metric("turnover_estimated", int(turnover_est), "换手率为估算(量/流通股本)", 1)

        # ---- 信号说明（透明）----
        reg_txt = {
            "shock": "超高换手冲击（筹码剧烈换手，成本估算有时滞）",
            "oversold": "深度套牢区（获利比例≤10%，卖压衰竭）",
            "overbought": "普遍获利区（获利比例≥90%，抛压大）",
            "accumulating": "筹码集中+价上成本线（建仓/洗盘特征）",
            "normal": "常规状态", "warmup": "预热期（分布未完全形成）",
        }[regime0]
        if np.isfinite(pr0):
            if fired > 0 and s1[last]:
                r.signal("alert", f"★ 超跌支撑: 获利比例 {pr0:.0%} ≤ {pr_low:.0%}，"
                                   f"价格位于筹码下沿支撑区 {sup0:.2f} 附近，"
                                   f"卖压衰竭，看反弹")
            elif fired > 0 and s3[last]:
                r.signal("alert", f"★ 集中建仓: conc70 较{cw}日前收窄 "
                                   f"{cimp0:.0%} ≥ {conc_gain:.0%} 且价上成本线 "
                                   f"{ac0:.2f}，建仓/洗盘特征")
            elif fired < 0:
                r.signal("alert", f"★ 获利抛压: 获利比例 {pr0:.0%} ≥ {pr_high:.0%}，"
                                   f"上方套牢区 {res0:.2f} 兑现抛压大，看回落")
            elif regime0 == "shock":
                r.signal("info", f"今日换手率 {to0:.0%} ≥ {shock_turnover:.0%}："
                                  f"筹码剧烈换手，成本结构快速改写，"
                                  f"短期估算存在时滞（信号压制）")
            elif s1[last] or s2[last] or s3[last]:
                r.signal("info", "已达信号条件但处于预热期（筹码分布未完全形成），不发信号")
            else:
                r.signal("info", f"获利比例 {pr0:.0%}（{pr_low:.0%}~{pr_high:.0%} 之间），"
                                  f"未达深度区触发阈值，无信号")
        if regime0 == "shock":
            r.signal("warn", f"单日换手率 {to0:.0%} ≥ {shock_turnover:.0%}："
                             f"超高换手率时滞区，支撑/阻力与成本估算短期不可靠")
        if np.isfinite(c70):
            if c70 <= 0.15:
                r.signal("info", f"筹码高度集中（conc70={c70:.2f} ≤ 0.15），"
                                  f"成本区 {sup0:.2f}~{res0:.2f} 支撑/阻力含义强")
            elif c70 >= 0.45:
                r.signal("info", f"筹码分散（conc70={c70:.2f} ≥ 0.45），"
                                  f"成本支撑/阻力含义弱")
        if resist_touch_hold is not None:
            d1 = (resist_touch_hold_base or 0.0) - resist_touch_hold
            b1 = (f"{resist_touch_hold_base:.1%}" if resist_touch_hold_base is not None else "无")
            s1x = "筹码位更硬 +" if d1 <= 0 else "弱于基线 "
            r.signal("info", f"阻力位(P85)触及后{zh_hold}日收回率 {resist_touch_hold:.1%}"
                             f"（等宽基线 {b1}，{s1x}{abs(d1) * 100:.1f}pp，n={resist_touch_n}）")
        if support_touch_hold is not None:
            d2 = (support_touch_hold_base or 0.0) - support_touch_hold
            b2 = (f"{support_touch_hold_base:.1%}" if support_touch_hold_base is not None else "无")
            s2x = "筹码位更硬 +" if d2 <= 0 else "弱于基线 "
            r.signal("info", f"支撑位(P15)触及后{zh_hold}日收回率 {support_touch_hold:.1%}"
                             f"（等宽基线 {b2}，{s2x}{abs(d2) * 100:.1f}pp，n={support_touch_n}）")
        if resist_break is not None and support_break is not None:
            r.signal("info", f"{zh}日包络: 阻力击穿率 {resist_break:.1%}"
                             f"（基线 {resist_break_base:.1%}），"
                             f"支撑击穿率 {support_break:.1%}"
                             f"（基线 {support_break_base:.1%}）")
        if turnover_est:
            r.signal("info", "换手率为估算值（成交量/流通股本，流通股本取最新值近似历史）")

        # ---- 回测明细表（独立事件，按日期倒序，最多 100 行）----
        if rows_ev:
            recs = [{"date": d, **row} for d, row in rows_ev]
            tbl = pd.DataFrame(recs).set_index("date").sort_index(ascending=False)
            r.add_table("signals", tbl, nd=3, max_rows=100)
        else:
            r.add_table("signals", pd.DataFrame(
                columns=["type", "close", "profit_ratio", "conc70",
                         *[f"fwd{h}" for h in HORIZONS]]))
        if not rows_ev and rows:
            r.signal("info", "历史信号全部被 10 日冷却合并（同类型连续触发只计首次）")

        # ---- 综合评分: 筹码结构机会强度 ----
        a = 0.0
        if s1[last] and valid[last]:
            a = float(np.clip((pr_low - pr0) / pr_low, 0, 1))
        elif s2[last] and valid[last]:
            a = float(np.clip((pr0 - pr_high) / (1 - pr_high), 0, 1))
        elif s3[last] and valid[last]:
            a = float(np.clip(cimp0 / (2 * conc_gain), 0, 1))
        base = 50.0 + 30.0 * a
        if regime0 == "shock":
            score = 50.0 + 0.1 * (base - 50.0)
        elif regime0 == "warmup":
            score = 50.0
        else:
            score = base
        r.score = round(float(np.clip(score, 0, 100)), 1)
        if fired > 0 and s1[last] and regime0 == "oversold":
            r.verdict = "深度套牢区（卖压衰竭，支撑反弹机会）"
        elif fired < 0 and regime0 == "overbought":
            r.verdict = "普遍获利区（抛压大，回落风险）"
        elif fired > 0 and s3[last] and regime0 == "accumulating":
            r.verdict = "筹码集中+价上成本线（建仓/洗盘特征）"
        elif regime0 == "shock":
            r.verdict = "超高换手冲击（成本估算时滞，观望）"
        elif regime0 == "warmup":
            r.verdict = "预热期（筹码分布未完全形成）"
        elif np.isfinite(pr0) and abs(pr0 - 0.5) < 0.15:
            r.verdict = "盈亏平衡区（获利比例≈50%，无方向）"
        else:
            r.verdict = "常规筹码结构（无信号）"

        r.summary = (
            f"获利比例 {pr0:.0%}，平均成本 {ac0:.2f}，"
            f"支撑/阻力 {sup0:.2f}/{res0:.2f}，regime {reg_txt}"
            if np.isfinite(pr0) else reg_txt
        )

        # ---- 图表序列（近 250 日）----
        tail = slice(-250, None)
        r.add_series("close", idx[tail], close_np[tail], title="收盘价")
        r.add_series("avg_cost", idx[tail], avg_cost[tail], title="成本均线(平均持仓成本)")
        r.add_series("support", idx[tail], p15[tail], title="支撑位(P15)")
        r.add_series("resistance", idx[tail], p85[tail], title="阻力位(P85)")
        r.add_series("profit_ratio", idx[tail], pr[tail] * 100.0, title="获利比例(%)")
        r.add_series("conc70", idx[tail], conc70[tail], title="70%集中度(越小越集中)")
        r.add_series("turnover", idx[tail], turnover[tail] * 100.0, title="换手率(%,估算)")
        r.add_series("signal", idx[tail], sig_dir[tail], title="信号(+1支撑反弹/-1获利抛压)")

        r.extra["model"] = {
            "chip_method": "换手衰减法: chips*=(1-t) 后按三角形分布摊入[low,high]"
                           "（VWAP 有逐日复权因子时优先，否则峰在(O+C)/2），"
                           "首日确定固定步长并随历史行情因果扩展",
            "turnover": "kline.turnover 缺失时用 成交量/流通股本"
                        "（流通股本=float_mv/latest_price，历史近似）",
            "signals": {
                "S1 超跌支撑": f"profit_ratio ≤ {pr_low:.0%} → +1",
                "S2 获利抛压": f"profit_ratio ≥ {pr_high:.0%} → -1",
                "S3 集中建仓": f"conc70 较{cw}日前收窄 ≥ {conc_gain:.0%} 且 close>成本均线 → +1",
            },
            "regime_rules": {
                "shock": f"单日换手率 ≥ {shock_turnover:.0%}（成本估算时滞，信号压制）",
                "oversold/overbought/accumulating": "对应 S1/S2/S3 触发区",
                "normal": "其余", "warmup": f"前 {WARMUP} 日（分布未完全形成）",
            },
            "zone_test": f"支撑/阻力位质量双口径 vs 以现价为心的等宽基线: "
                        f"(a){zh}日包络击穿率(低=硬) (b)触及后{zh_hold}日收回率(高=硬)",
            "note": "宣称 50%~58% 胜率与'关键支撑/阻力位预测'均以默认参数实测输出"
                   "（bt_win* / *_break），不调参。筹码分布质量依赖换手率估算，"
                   "超高换手率日（shock regime）成本结构被快速改写、短期估算时滞。",
        }
        return r
