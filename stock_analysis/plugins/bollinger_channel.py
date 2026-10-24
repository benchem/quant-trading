"""布林带通道与相对位置（%b）模型（Bollinger Bands Model）。

所属维度: 波动率与通道（区间震荡 + 挤压突破）。
目标: 以 N 日移动平均为中心线、±K 倍滚动标准差构建上下轨，导出
布林带相对位置因子 %b = (close - lower) / (upper - lower)，
输出区间震荡买卖点与挤压（Squeeze）后的突破形态，并对
"通道预测准确性 / 50%~55% 胜率" 做可实测的回测。

模型结构（四层）:

1) 通道（教科书标准，纯 pandas 滚动）:
   mid = SMA(close, N),  σ = 滚动标准差(close, N, ddof=0)
   upper = mid + K·σ,  lower = mid - K·σ
   带宽 bandw = (upper - lower) / mid（波动率的相对度量）
   %b ∈ [0,1]: 0 = 收在下轨, 1 = 收在上轨, 0.5 = 中心线;
   可越界（<0 / >1 = 收盘价突破轨道）

2) 挤压（Squeeze）:
   带宽在回看窗口（默认 120 日）内处于最低分位（默认 ≤10%）= 挤压态。
   波动压缩后往往伴随波动扩张 —— 挤压后收盘价突破上/下轨
   = 突破方向信号（squeeze 突破, S3）。

3) 信号（方向）:
   S1 区间下沿: %b ≤ 0（收盘 ≤ 下轨）→ +1（区间震荡买点）
   S2 区间上沿: %b ≥ 1（收盘 ≥ 上轨）→ -1（区间震荡卖点）
   S3 挤压突破: 近 squeeze_lag 日内出现挤压态 且 收盘突破上轨 → +1
                （同条件跌破下轨 → -1）
   S3 优先于 S1/S2（突破日同时满足轨外条件，取更具体的挤压突破标签）。

4) regime 检测（针对模型已知弱点"强单边趋势中贴轨运行 → 提前离场"）:
   - ride_up / ride_down: 连续 ride_days 日收盘贴在上/下轨之外
     （强趋势的典型形态）。贴轨期区间信号失效 ——
     上行贴轨压制 S2（避免强趋势中过早卖出），下行贴轨压制 S1。
   - trending: 10 日位移 ≥ trend_disp·σ（强单边趋势，通道被拉伸，
     区间信号整体可靠性下降，展示用）
   - squeeze: 当前处于挤压态
   - warmup: 前 WARMUP 日（通道/分位未建立）

特色输出:
   - 通道校准与包络回测: 次日收盘落在当日通道内比例（hit1，经典宣称
     ≈95%）+ 其后 10 日高/低全部留在通道内比例（hit10），并与
     "以现价为心、与通道同宽"的无信息基线对照（hit10_base）;
   - 挤压扩张检验: S3 事件后 10 日 |收益| 均值 vs 全样本均值
     （挤压→波动扩张是否有真实信息）;
   - 历史信号方向胜率（5/10/20 日，宣称 50%~55% 的可实测版本），
     按日 / 独立事件（10 日冷却）/ 无 regime 过滤对照三口径。

防过拟合说明: N=20、K=2.0 为布林带教科书默认值; 挤压回看 120 日 /
10% 分位、贴轨 5 日、位移 3σ 均为经验初值（未做网格搜索）。
宣称"创业板等高波动板块 K 可上调至 2.2~2.5"以参数 k_std 暴露，
默认交付 K=2.0，高波动股的 K 敏感性在实测中单独报告（非调参追胜率）。
全部阈值在参数中声明、可调，交付结论以默认参数实测为准。

数据依赖: OHLC（ctx.kline），需 ≥ 约 250 个交易日。
作者: quant-trading 项目（2026-10）
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from stock_analysis import AnalysisPlugin, AnalysisResult, ParamSpec

#: 回测前瞻期（交易日）
HORIZONS = (5, 10, 20)
#: 信号冷却（同类型连续触发只计首次，独立事件口径）
SIGNAL_COOLDOWN = 10
#: 趋势位移观察窗（日）
TREND_WIN = 10


def _rolling_pctile(x: np.ndarray, lookback: int) -> np.ndarray:
    """x[t] 在 x[t-lookback+1..t] 窗口内的分位（含自身，≤ 的占比）。

    O(n·L) 逐日循环（n≤800, L≤250，足够快）。窗口内 NaN 忽略。
    """
    n = len(x)
    out = np.full(n, np.nan)
    for t in range(lookback - 1, n):
        w = x[t - lookback + 1:t + 1]
        w = w[np.isfinite(w)]
        if w.size < lookback * 0.8:
            continue
        out[t] = float((w <= x[t]).mean()) if np.isfinite(x[t]) else np.nan
    return out


class BollingerChannelPlugin(AnalysisPlugin):
    name = "bollinger_channel"
    display_name = "布林带通道与相对位置（%b）"
    version = "0.1.0"
    category = "technical"
    description = (
        "N日均值±K倍标准差构建布林通道，%b 相对位置因子；"
        "区间震荡买卖点（%b≤0 / ≥1）+ 挤压后突破（带宽120日最低10%分位"
        "后破轨）信号；贴轨运行 regime 自动压制区间信号（防强趋势提前离场）；"
        "通道校准（次日/10日包络命中率 vs 等宽基线）、挤压扩张检验与"
        "5/10/20日方向胜率三口径回测")

    params = {
        "n_period": ParamSpec(type="int", default=20, min=10, max=60,
                              help="通道 MA 窗口（教科书 20）"),
        "k_std": ParamSpec(type="float", default=2.0, min=1.5, max=3.0,
                           help="标准差倍数 K（教科书 2.0；高波动板块可 2.2~2.5）"),
        "squeeze_lookback": ParamSpec(type="int", default=120, min=60, max=250,
                                      help="带宽分位回看窗口（日）"),
        "squeeze_percentile": ParamSpec(type="float", default=0.10, min=0.05,
                                        max=0.25,
                                        help="挤压态: 带宽处于窗口最低该分位"),
        "squeeze_lag": ParamSpec(type="int", default=10, min=3, max=30,
                                 help="S3 突破: 最近多少日内出现过挤压态"),
        "ride_days": ParamSpec(type="int", default=5, min=3, max=10,
                               help="贴轨运行: 连续多少日收盘在轨外（压制区间信号）"),
        "trend_disp": ParamSpec(type="float", default=3.0, min=1.5, max=5.0,
                                help="强趋势: 10日位移 ≥ 该倍数×σ（展示/警示）"),
        "zone_horizon": ParamSpec(type="int", default=10, min=3, max=20,
                                  help="通道包络测试视界（日）"),
    }

    def run(self, ctx, code: str, n_period: int = 20, k_std: float = 2.0,
            squeeze_lookback: int = 120, squeeze_percentile: float = 0.10,
            squeeze_lag: int = 10, ride_days: int = 5,
            trend_disp: float = 3.0, zone_horizon: int = 10) -> AnalysisResult:
        n_per = int(n_period)
        k = float(k_std)
        L = int(squeeze_lookback)
        warmup = max(n_per, L) + 40
        need = max(250, warmup + 40 + max(HORIZONS))
        df = ctx.kline(code, last=max(need, 800))
        r = AnalysisResult(self.name, code)
        if len(df) < need:
            r.summary = (f"历史数据不足（布林通道需约 {need} 个交易日，"
                         f"实际 {len(df)}），跳过")
            r.signal("warn", "历史数据不足")
            return r

        idx = df.index
        n = len(df)
        close = df["close"].astype(float)
        close_np = close.to_numpy()
        low = df["low"].astype(float).to_numpy()
        high = df["high"].astype(float).to_numpy()

        # ============ 1) 通道与 %b ============
        sd = close.rolling(n_per, min_periods=n_per).std(ddof=0).to_numpy()
        mid = close.rolling(n_per, min_periods=n_per).mean().to_numpy()
        upper = mid + k * sd
        lower = mid - k * sd
        valid = np.isfinite(mid) & (sd > 0)
        width = upper - lower
        pctb = np.where(valid & (width > 0),
                        (close_np - lower) / np.where(width > 0, width, 1.0),
                        np.nan)
        bandw = np.where(valid, width / np.where(mid > 0, mid, 1.0), np.nan)
        bw_pct = _rolling_pctile(bandw, L)
        squeeze_state = valid & (bw_pct <= squeeze_percentile)

        # ---- 挤压后突破（突破日之前 squeeze_lag 日内出现过挤压态）----
        csq = np.cumsum(squeeze_state.astype(float))
        recent_sq = np.zeros(n, dtype=bool)
        for t in range(1, n):
            a = max(0, t - squeeze_lag)
            recent_sq[t] = (csq[t - 1] - (csq[a - 1] if a > 0 else 0)) > 0

        s1 = valid & (pctb <= 0.0)          # 下轨外（区间买点）
        s2 = valid & (pctb >= 1.0)          # 上轨外（区间卖点）
        s3_up = valid & (close_np > upper) & recent_sq
        s3_dn = valid & (close_np < lower) & recent_sq

        # ---- 贴轨运行（连续 ride_days 日收盘在轨外）----
        above = valid & (close_np >= upper)
        below = valid & (close_np <= lower)
        ca = np.cumsum(above.astype(float))
        cb = np.cumsum(below.astype(float))
        ride_up = np.zeros(n, dtype=bool)
        ride_dn = np.zeros(n, dtype=bool)
        ride_up[ride_days - 1:] = (
            ca[ride_days - 1:] - np.concatenate([[0], ca[:-ride_days]])
            >= ride_days)
        ride_dn[ride_days - 1:] = (
            cb[ride_days - 1:] - np.concatenate([[0], cb[:-ride_days]])
            >= ride_days)

        # ---- 强趋势: 10 日位移（σ 倍数）----
        tsc = np.full(n, np.nan)
        if n > TREND_WIN:
            tsc[TREND_WIN:] = (
                (close_np[TREND_WIN:] - close_np[:-TREND_WIN])
                / np.where(sd[TREND_WIN:] > 0, sd[TREND_WIN:], 1.0))

        # ---- 信号合成（S3 优先; 贴轨压制区间信号）----
        sig_dir_full = np.zeros(n)
        sig_type = np.array([""] * n, dtype=object)
        m3u = s3_up
        m3d = s3_dn & ~s3_up
        mb = s1 & ~s3_up & ~s3_dn
        mt = s2 & ~s3_up & ~s3_dn
        sig_dir_full[m3u] = 1.0; sig_type[m3u] = "squeeze_up"
        sig_dir_full[m3d] = -1.0; sig_type[m3d] = "squeeze_dn"
        sig_dir_full[mb] = 1.0; sig_type[mb] = "bottom"
        sig_dir_full[mt] = -1.0; sig_type[mt] = "top"

        sig_dir = sig_dir_full.copy()
        sig_dir[mb & ride_dn] = 0.0        # 下行贴轨: 压制抄底
        sig_dir[mt & ride_up] = 0.0        # 上行贴轨: 压制卖出（提前离场弱点）

        # ---- regime ----
        regime = np.array(["normal"] * n, dtype=object)
        regime[:warmup] = "warmup"
        regime[ride_dn] = "ride_dn"
        regime[ride_up] = "ride_up"
        trending = valid & np.isfinite(tsc) & (np.abs(tsc) >= trend_disp)
        regime[trending & (regime == "normal")] = "trending"

        # ============ 2) 回测: 信号方向胜率（5/10/20 日）============
        def _cooldown(mask_dir):
            out = mask_dir.copy()
            last_fired = {}
            for t in range(warmup, n):
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
            for t in range(warmup, n):
                d = int(mask_dir[t])
                if d == 0 or not valid[t]:
                    continue
                row = {"type": str(sig_type[t]), "close": float(close_np[t]),
                       "pctb": float(pctb[t]), "bandw": float(bandw[t])}
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

        rows, stats = _bt(sig_dir)                     # 正式·按日（含贴轨压制）
        sig_dir_ev = _cooldown(sig_dir)                 # 正式·独立事件
        rows_ev, stats_ev = _bt(sig_dir_ev)
        rows_base, stats_base = _bt(sig_dir_full)       # 对照组（无贴轨压制）

        # ---- 分类型计数与 10 日胜率（独立事件）----
        type_stat = {ty: {"n": 0, "ws": []}
                     for ty in ("bottom", "top", "squeeze_up", "squeeze_dn")}
        for t in range(warmup, n):
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

        # ============ 3) 通道校准 / 包络 / 挤压扩张 ============
        # hit1: 次日收盘落在当日通道内（经典 ≈95% 宣称的可实测版）
        # hit10: 后 zh 日 max(high)/min(low) 全部留在通道内（包络）
        # 基线: 以现价为心、与通道同宽的静态带（无信息对照）
        zh = int(zone_horizon)
        t0, t1 = warmup, n - zh
        seg_hi = np.full(max(n - zh, 0), np.nan)
        seg_lo = np.full(max(n - zh, 0), np.nan)
        for t in range(0, n - zh):
            seg_hi[t] = high[t + 1:t + 1 + zh].max()
            seg_lo[t] = low[t + 1:t + 1 + zh].min()
        hv = valid[t0:t1]
        hit1 = hit10 = hit10_base = None
        if t1 > t0 and hv.any():
            t_idx = np.arange(t0, t1)[hv]
            lo_t, up_t = lower[t_idx], upper[t_idx]
            w_t = (up_t - lo_t) / 2.0
            cl1 = close_np[t_idx + 1]
            hit1 = float(((cl1 >= lo_t) & (cl1 <= up_t)).mean())
            inb = (seg_hi[t_idx] <= up_t) & (seg_lo[t_idx] >= lo_t)
            inb_base = (seg_hi[t_idx] <= close_np[t_idx] + w_t) \
                & (seg_lo[t_idx] >= close_np[t_idx] - w_t)
            hit10 = float(inb.mean())
            hit10_base = float(inb_base.mean())

        # ---- 挤压扩张检验: S3 事件后 10 日 |收益| vs 全样本 ----
        sq_mag = []
        all_mag = []
        for t in range(warmup, n - 10):
            if valid[t] and np.isfinite(close_np[t + 10]):
                m = abs(float(close_np[t + 10] / close_np[t] - 1.0))
                all_mag.append(m)
                if sig_dir_ev[t] != 0 and str(sig_type[t]).startswith("squeeze"):
                    sq_mag.append(m)
        squeeze_mag10 = float(np.mean(sq_mag)) if sq_mag else None
        mag10_base = float(np.mean(all_mag)) if all_mag else None

        # ============ 4) 当日状态 ============
        last = n - 1
        c0 = float(close_np[last])
        mid0 = float(mid[last]) if valid[last] else float("nan")
        up0 = float(upper[last]) if valid[last] else float("nan")
        lo0 = float(lower[last]) if valid[last] else float("nan")
        pb0 = float(pctb[last]) if np.isfinite(pctb[last]) else float("nan")
        bw0 = float(bandw[last]) if np.isfinite(bandw[last]) else float("nan")
        bwp0 = float(bw_pct[last]) if np.isfinite(bw_pct[last]) else float("nan")
        tsc0 = float(tsc[last]) if np.isfinite(tsc[last]) else float("nan")
        regime0 = str(regime[last])
        fired = int(sig_dir[last])

        # ---- 指标 ----
        r.metric("close", c0, "收盘价(后复权)", 3)
        r.metric("mid", mid0, f"中轨(MA{n_per})", 3)
        r.metric("upper", up0, f"上轨(MA+{k:g}σ)", 3)
        r.metric("lower", lo0, f"下轨(MA-{k:g}σ)", 3)
        r.metric("pctb", pb0, "%b 相对位置(0=下轨,1=上轨)", 3)
        r.metric("bandw", bw0, "带宽(上-下)/中轨", 3)
        r.metric("bw_percentile", bwp0, f"带宽处于近{L}日分位(低=挤压)", 3)
        r.metric("squeeze", int(bool(squeeze_state[last])),
                 f"挤压态(带宽≤{L}日最低{squeeze_percentile:.0%}分位)", 0)
        r.metric("trend_score10", tsc0, f"{TREND_WIN}日位移(σ倍数)", 2)
        r.metric("regime", regime0, "状态", 0)
        r.metric("signal_today", fired, "今日信号(+1看多/-1看空)", 0)
        r.metric("signal_type_today", str(sig_type[last]) if fired != 0 else "",
                 "今日信号类型(bottom/top/squeeze_up/squeeze_dn)", 0)
        r.metric("bt_count", len(rows), "历史信号日数(含贴轨压制)", 1)
        r.metric("bt_count_ev", len(rows_ev), "独立信号事件数(10日冷却)", 1)
        r.metric("bt_count_base", len(rows_base), "历史信号日数(无贴轨压制)", 1)
        r.metric("bt_n_bottom", type_stat["bottom"]["n"], "事件数: 区间下沿(S1)", 1)
        r.metric("bt_n_top", type_stat["top"]["n"], "事件数: 区间上沿(S2)", 1)
        r.metric("bt_n_squeeze", type_stat["squeeze_up"]["n"] + type_stat["squeeze_dn"]["n"],
                 "事件数: 挤压突破(S3)", 1)
        for h in HORIZONS:
            r.metric(f"bt_win{h}", stats[h]["win"],
                     f"回测{h}日方向胜率(按日, n={stats[h]['n']})", 3)
        for h in HORIZONS:
            r.metric(f"bt_win{h}_ev", stats_ev[h]["win"],
                     f"回测{h}日方向胜率(独立事件, n={stats_ev[h]['n']})", 3)
        for h in (5, 20):
            r.metric(f"bt_win{h}_base", stats_base[h]["win"],
                     f"回测{h}日方向胜率(对照, n={stats_base[h]['n']})", 3)
        r.metric("hit1", hit1, "次日收盘通道内比例(经典宣称≈95%)", 3)
        r.metric("hit10", hit10, f"后{zh}日包络通道内比例", 3)
        r.metric("hit10_base", hit10_base, f"等宽基线{zh}日包络比例(对照)", 3)
        r.metric("squeeze_mag10", squeeze_mag10, "S3事件后10日|收益|均值", 4)
        r.metric("mag10_base", mag10_base, "全样本10日|收益|均值(对照)", 4)

        # ---- 信号说明（透明）----
        reg_txt = {
            "ride_up": "贴轨上行（强趋势，避免过早卖出）",
            "ride_dn": "贴轨下行（弱趋势，避免过早抄底）",
            "trending": "强单边趋势（通道拉伸，区间信号可靠性降）",
            "normal": "常规状态", "warmup": "预热期（通道未建立）",
        }[regime0]
        if np.isfinite(pb0):
            if fired > 0 and s3_up[last]:
                r.signal("alert", f"★ 挤压突破向上: 近{squeeze_lag}日内带宽处于{L}日"
                                   f"最低{squeeze_percentile:.0%}分位 + 收盘 "
                                   f"{c0:.2f} 突破上轨 {up0:.2f}，波动扩张方向信号")
            elif fired < 0 and s3_dn[last]:
                r.signal("alert", f"★ 挤压突破向下: 近{squeeze_lag}日内带宽处于{L}日"
                                   f"最低{squeeze_percentile:.0%}分位 + 收盘 "
                                   f"{c0:.2f} 跌破下轨 {lo0:.2f}，波动扩张方向信号")
            elif fired > 0:
                r.signal("alert", f"★ 区间下沿: %b={pb0:.2f} ≤ 0（收盘 ≤ 下轨 "
                                   f"{lo0:.2f}），区间震荡买点，看反弹")
            elif fired < 0:
                r.signal("alert", f"★ 区间上沿: %b={pb0:.2f} ≥ 1（收盘 ≥ 上轨 "
                                   f"{up0:.2f}），区间震荡卖点，看回落")
            elif (s3_up[last] or s3_dn[last] or s1[last] or s2[last]):
                if last < warmup:
                    r.signal("info", "已达信号条件但处于预热期（通道/分位未建立），不发信号")
                elif (s1[last] and ride_dn[last]) or (s2[last] and ride_up[last]):
                    r.signal("info", "已达信号条件但被贴轨 regime 压制"
                                      "（强趋势，避免提前离场）")
        if regime0 in ("ride_up", "ride_dn"):
            r.signal("warn", f"连续{ride_days}日贴{'上' if regime0 == 'ride_up' else '下'}轨"
                             f"（强{'上' if regime0 == 'ride_up' else '下'}行趋势），"
                             f"区间信号已压制——贴轨期卖出/抄底易提前离场")
        if regime0 == "trending":
            r.signal("info", f"{TREND_WIN}日位移 {tsc0:+.1f}σ ≥ {trend_disp:g}σ："
                             f"强单边趋势，通道被拉伸，区间信号可靠性下降")
        if squeeze_state[last] and not (s3_up[last] or s3_dn[last]):
            r.signal("info", f"当前挤压态: 带宽 {bw0:.1%} 处于近{L}日 "
                             f"{bwp0:.0%} 分位（≤{squeeze_percentile:.0%}），"
                             f"波动压缩——突破前夜")
        if hit1 is not None:
            r.signal("info", f"通道校准: 次日收盘通道内 {hit1:.1%}"
                             f"（经典 2σ 通道宣称≈95%），"
                             f"后{zh}日包络 {hit10:.1%}"
                             f"（等宽基线 {hit10_base:.1%}）")
        if squeeze_mag10 is not None and mag10_base:
            r.signal("info", f"挤压扩张检验: S3 事件后{zh}日|收益|均值 "
                             f"{squeeze_mag10:.2%} vs 全样本 {mag10_base:.2%}"
                             f"（比值 {squeeze_mag10 / mag10_base:.2f}）")

        # ---- 回测明细表（独立事件，按日期倒序，最多 100 行）----
        if rows_ev:
            recs = [{"date": d, **row} for d, row in rows_ev]
            tbl = pd.DataFrame(recs).set_index("date").sort_index(ascending=False)
            r.add_table("signals", tbl, nd=3, max_rows=100)
        else:
            r.add_table("signals", pd.DataFrame(
                columns=["type", "close", "pctb", "bandw",
                         *[f"fwd{h}" for h in HORIZONS]]))
        if not rows_ev and rows:
            r.signal("info", "历史信号全部被 10 日冷却合并（同类型连续触发只计首次）")

        # ---- 综合评分: 通道位置强度 ----
        a = float(np.clip(abs(pb0 - 0.5) * 2.0, 0, 1)) if np.isfinite(pb0) else 0.0
        base = 50.0 + 30.0 * a
        if squeeze_state[last]:
            base = min(90.0, base + 10.0)
        if regime0 in ("ride_up", "ride_dn"):
            score = 50.0 + 0.3 * (base - 50.0)
        elif regime0 == "warmup":
            score = 50.0
        else:
            score = base
        r.score = round(float(np.clip(score, 0, 100)), 1)
        if fired > 0 and s3_up[last]:
            r.verdict = "挤压突破向上（波动压缩后扩张）"
        elif fired < 0 and s3_dn[last]:
            r.verdict = "挤压突破向下（波动压缩后扩张）"
        elif fired > 0:
            r.verdict = "区间下沿（%b≤0，震荡买点）"
        elif fired < 0:
            r.verdict = "区间上沿（%b≥1，震荡卖点）"
        elif regime0 == "ride_up":
            r.verdict = "贴轨上行（强趋势，避免过早卖出）"
        elif regime0 == "ride_dn":
            r.verdict = "贴轨下行（弱趋势，避免过早抄底）"
        elif squeeze_state[last]:
            r.verdict = "波动压缩（挤压态，突破前夜）"
        elif regime0 == "trending":
            r.verdict = "强单边趋势（通道拉伸，区间信号弱）"
        elif regime0 == "warmup":
            r.verdict = "预热期（通道未建立）"
        elif np.isfinite(pb0) and abs(pb0 - 0.5) < 0.15:
            r.verdict = "通道中部（无方向倾向）"
        else:
            r.verdict = "常规通道状态（无信号）"

        r.summary = (
            f"%b {pb0:.2f}，中轨 {mid0:.2f}，上下轨 {lo0:.2f}/{up0:.2f}，"
            f"带宽 {bw0:.1%}（{L}日 {bwp0:.0%} 分位），regime {reg_txt}"
            if np.isfinite(pb0) else reg_txt
        )

        # ---- 图表序列（近 250 日）----
        tail = slice(-250, None)
        r.add_series("close", idx[tail], close_np[tail], title="收盘价")
        r.add_series("mid", idx[tail], mid[tail], title=f"中轨(MA{n_per})")
        r.add_series("upper", idx[tail], upper[tail], title=f"上轨(+{k:g}σ)")
        r.add_series("lower", idx[tail], lower[tail], title=f"下轨(-{k:g}σ)")
        r.add_series("pctb", idx[tail], pctb[tail], title="%b 相对位置")
        r.add_series("bandw", idx[tail], bandw[tail] * 100.0, title="带宽(%)")
        r.add_series("squeeze", idx[tail], squeeze_state[tail].astype(float),
                     title=f"挤压态(带宽≤{L}日最低{squeeze_percentile:.0%}分位)")
        r.add_series("signal", idx[tail], sig_dir[tail],
                     title="信号(+1下沿反弹/挤压向上 · -1上沿回落/挤压向下)")

        r.extra["model"] = {
            "bb_method": f"mid=SMA({n_per}), σ=滚动标准差({n_per}, ddof=0), "
                         f"上下轨=mid±{k:g}σ, %b=(close-下轨)/(上轨-下轨)",
            "signals": {
                "S1 区间下沿": "%b ≤ 0 → +1（区间震荡买点）",
                "S2 区间上沿": "%b ≥ 1 → -1（区间震荡卖点）",
                "S3 挤压突破": f"近{squeeze_lag}日出现挤压态(带宽≤{L}日最低"
                              f"{squeeze_percentile:.0%}分位)后破上轨 → +1 / "
                              f"破下轨 → -1（S3 优先于 S1/S2）",
            },
            "regime_rules": {
                "ride_up/ride_dn": f"连续{ride_days}日收盘贴上/下轨之外"
                                   f"（强趋势，区间信号压制——防提前离场）",
                "trending": f"{TREND_WIN}日位移 ≥ {trend_disp:g}σ",
                "warmup": f"前 {warmup} 日（通道/分位未建立）",
            },
            "channel_test": f"次日收盘通道内比例 hit1（经典≈95%宣称）+ 后{zh}日"
                            f"包络 hit10 vs 以现价为心等宽基线 hit10_base",
            "squeeze_test": "S3 事件后 10 日|收益|均值 vs 全样本（挤压→波动扩张）",
            "note": "宣称 50%~55% 胜率与'通道预测准确性'均以默认参数实测输出"
                    "（bt_win* / hit*），不调参。高波动板块（如创业板）K 可上调至 "
                    "2.2~2.5（参数 k_std），以减少宽幅震荡中的虚假触发；"
                    "强单边趋势中贴轨运行是模型已知弱点（ride regime 自动压制）。",
        }
        return r
