"""多因子量化复合技术模型（Multi-Factor Quant Technical Model / 复合突破选股）。

所属维度: 数理统计与多因子集成。
目标: 用"多因子加权 + 硬过滤"剔除高波动带来的虚假突破，提升单因子突破信号的
胜率（宣称区间 55%~65%，本插件内置历史回测统计可实测验证）。

模型结构（四层，自下而上）:

1) 因子层 —— 把独立技术指标转成有经济含义的连续因子:
   - VAM  波动率调整动量: ret(w) / (std(日收益, w) * √w)，窗口内"收益/已实现波动"，
           相当于无风险利率为 0 的窗口 Sharpe。稳步上行 > 暴涨暴跌。
   - VPD  量价背离: 窗口内 日收益 与 对数量变化 的 Pearson 相关。
           正 = 量价齐升/同步（健康），负 = 背离（价升量缩或价跌量增，虚动）。
   - VR   量比: 当日量 / 前 w 日均量（不含当日）。突破的"燃料"，1.8 倍的连续形式。
   - SQUEEZE 波动压缩: ATR%(14) 在近 250 日的滚动分位（0~1）。低位 = 波动压缩、
           蓄势状态；压缩后的放量突破跟随性更强（TTM Squeeze 思路）。

2) 标准化层 —— 各因子做 250 日滚动 z-score（只用当日及之前数据，无未来函数），
   消除量纲差异，使不同因子可加权。

3) 合成层 —— 加权合成综合分 S = Σ w_i · z_i（缺失因子按中性 0 处理并权重再归一）。
   默认权重: VAM 0.35 / VPD 0.25 / VR 0.20 / SQUEEZE 0.20。

4) 信号层 —— 突破事件 × 三道硬门（任一不过即视为"低质量环境"剔除）:
   - 突破:   close > 前 w 日最高价（不含当日）
   - 量能:   当日量 ≥ vol_mult × 前 w 日均量（默认 1.8 倍）
   - 压缩:   ATR% 250 日分位 ≤ squeeze_q（默认 30%，即低位压缩状态）
   - 共振:   综合分 S ≥ score_gate（默认 1.0，多因子同向共振）

回测验证（防止"拍脑袋胜率"）: 对历史上每一个满足全部条件的信号日，统计其后
5/10/20 日收益的胜率与均值；同时给出**不含共振门**的对照组的胜率，直接展示
多因子组合带来的增量（也是防过拟合说明：参数均有行业标准含义，未做网格搜索）。

作者: quant-trading 项目（多因子复合技术模型实现，2026-10）
数据依赖: 仅需 K线（open/high/low/close/volume），走 ctx.kline。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from stock_analysis import AnalysisPlugin, AnalysisResult, ParamSpec

#: 回测前瞻期（交易日）
HORIZONS = (5, 10, 20)


# ------------------------------------------------------------------ 因子工具


def _atr(df: pd.DataFrame, window: int) -> pd.Series:
    """Wilder ATR（真实波幅的 EWM 均值）。"""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / window, min_periods=window, adjust=False).mean()


def _rolling_zscore(s: pd.Series, window: int) -> pd.Series:
    """滚动 z-score。窗口内 NaN 自动跳过；方差为 0 时置 NaN（无信息）。"""
    m = s.rolling(window, min_periods=window // 2).mean()
    sd = s.rolling(window, min_periods=window // 2).std(ddof=1)
    z = (s - m) / sd
    return z.where(sd > 0)


def _rolling_quantile(s: pd.Series, window: int) -> pd.Series:
    """滚动分位: 当前值在过去 window 个观测中的排名比例（0~1，含当日）。"""
    return s.rolling(window, min_periods=window // 2).apply(
        lambda x: float((x <= x[-1]).mean()), raw=True
    )


def _combine_z(factor_z: dict[str, pd.Series], weights: dict[str, float]) -> pd.Series:
    """加权合成；某因子缺失时按中性 0 处理并权重再归一（标准多因子缺失处理）。"""
    idx = next(iter(factor_z.values())).index
    total = pd.Series(0.0, index=idx)
    wsum = pd.Series(0.0, index=idx)
    for k, z in factor_z.items():
        w = float(weights.get(k, 0.0))
        if w <= 0:
            continue
        zz = z.fillna(0.0)
        total = total + w * zz
        wsum = wsum + w * z.notna().astype(float)
    return total / wsum.clip(lower=1e-9)


def _forward_stats(close: pd.Series, sig_idx: list, horizons: tuple[int, ...]) -> pd.DataFrame:
    """信号日后 horizons 各期的收益/胜率（前视为评估用途，信号判定本身无未来函数）。"""
    pos_of = {d: i for i, d in enumerate(close.index)}
    n = len(close)
    rows = []
    for d in sig_idx:
        i = pos_of[d]
        row = {"date": d}
        for h in horizons:
            row[f"fwd{h}"] = float(close.iloc[i + h] / close.iloc[i] - 1.0) \
                if i + h < n else float("nan")
        rows.append(row)
    if rows:
        df = pd.DataFrame(rows).set_index("date")
    else:  # 无信号: 返回带列名的空表，避免 set_index 失败
        df = pd.DataFrame(columns=[f"fwd{h}" for h in horizons])
    stats = {}
    for h in horizons:
        s = df[f"fwd{h}"].dropna()
        stats[h] = {
            "n": int(len(s)),
            "win": float((s > 0).mean()) if len(s) else float("nan"),
            "mean": float(s.mean()) if len(s) else float("nan"),
            "median": float(s.median()) if len(s) else float("nan"),
        }
    return df, stats


class MultiFactorBreakoutPlugin(AnalysisPlugin):
    name = "multi_factor_breakout"
    display_name = "多因子复合突破"
    description = (
        "VAM波动率调整动量 + VPD量价背离 + 量比 + 波动压缩四因子加权，"
        "突破N日高点时以放量/低波动/多因子共振三道硬门过滤假突破，附历史回测胜率"
    )
    version = "0.1.0"
    category = "technical"
    params = {
        "lookback": ParamSpec(type="int", default=20, min=5, max=60,
                              help="突破参考期与动量/量能窗口（前N日最高价、N日均量）"),
        "vol_mult": ParamSpec(type="float", default=1.8, min=1.0, max=5.0,
                              help="量能硬门: 当日量 ≥ 该倍数 × 前N日均量"),
        "atr_window": ParamSpec(type="int", default=14, min=5, max=30, help="ATR 周期"),
        "squeeze_q": ParamSpec(type="float", default=0.30, min=0.05, max=0.90,
                               help="压缩硬门: ATR% 在近std_window日分位 ≤ 该值视为低位压缩"),
        "std_window": ParamSpec(type="int", default=250, min=120, max=500,
                                help="因子 z-score / ATR% 分位的滚动窗口"),
        "score_gate": ParamSpec(type="float", default=1.0, min=0.0, max=3.0,
                                help="共振门: 综合 z 分 ≥ 该值（0=关闭共振门）"),
        "w_vam": ParamSpec(type="float", default=0.35, min=0.0, max=1.0,
                           help="权重: VAM 波动率调整动量"),
        "w_vpd": ParamSpec(type="float", default=0.25, min=0.0, max=1.0,
                           help="权重: VPD 量价背离"),
        "w_vr": ParamSpec(type="float", default=0.20, min=0.0, max=1.0, help="权重: 量比"),
        "w_squeeze": ParamSpec(type="float", default=0.20, min=0.0, max=1.0,
                               help="权重: 波动压缩（分位越低越好）"),
    }

    def run(self, ctx, code: str, lookback: int = 20, vol_mult: float = 1.8,
            atr_window: int = 14, squeeze_q: float = 0.30, std_window: int = 250,
            score_gate: float = 1.0, w_vam: float = 0.35, w_vpd: float = 0.25,
            w_vr: float = 0.20, w_squeeze: float = 0.20) -> AnalysisResult:
        need = std_window + lookback + 10
        df = ctx.kline(code, last=max(need, 600))
        r = AnalysisResult(self.name, code)
        if len(df) < need:
            r.summary = (f"历史数据不足（需约 {need} 个交易日做 {std_window} 日标准化，"
                         f"实际 {len(df)}），跳过")
            r.signal("warn", "历史数据不足")
            return r

        close = df["close"].astype(float)
        volume = df["volume"].astype(float)
        idx = df.index

        # ================= 1) 因子（全部只用当日及之前数据）=================
        rets = close.pct_change()
        # VAM: 窗口收益 / 窗口已实现波动（无 rf 的窗口 Sharpe）
        ret_w = close.pct_change(lookback)
        sigma_w = rets.rolling(lookback, min_periods=lookback).std(ddof=1)
        vam = (ret_w / (sigma_w * np.sqrt(lookback))).where(sigma_w > 0)
        # VPD: 日收益 与 对数量变化 的滚动相关（+1 量价齐步, -1 完全背离）
        dlog_vol = np.log1p(volume).diff()
        vpd = rets.rolling(lookback, min_periods=lookback).corr(dlog_vol)
        # VR: 当日量 / 前 N 日均量（不含当日）
        vol_base = volume.shift(1).rolling(lookback, min_periods=lookback).mean()
        vr = volume / vol_base
        # SQUEEZE: ATR% 的滚动分位（低 = 波动压缩蓄势）
        atr_pct = _atr(df, atr_window) / close
        sqz_q = _rolling_quantile(atr_pct, std_window)

        # ================= 2) 标准化（滚动 z-score，无未来函数）=================
        # 压缩因子方向翻转: 分位越低越好 → 取 (0.5 - q) 使"高=好"统一方向
        factor_z = {
            "vam": _rolling_zscore(vam, std_window),
            "vpd": _rolling_zscore(vpd, std_window),
            "vr": _rolling_zscore(vr, std_window),
            "squeeze": _rolling_zscore(0.5 - sqz_q, std_window),
        }
        weights = {"vam": w_vam, "vpd": w_vpd, "vr": w_vr, "squeeze": w_squeeze}
        w_total = sum(weights.values())
        if w_total <= 0:
            r.summary = "全部因子权重为 0，无法合成综合分"
            r.signal("warn", "参数错误: 权重和为 0")
            return r
        score = _combine_z(factor_z, weights)

        # ================= 3) 信号: 突破 × 三道硬门 =================
        prior_high = df["high"].astype(float).shift(1).rolling(lookback,
                                                              min_periods=lookback).max()
        breakout = close > prior_high
        volgate = vr >= vol_mult
        sqzgate = sqz_q <= squeeze_q
        use_gate = score_gate > 0
        score_ok = (score >= score_gate) if use_gate else pd.Series(True, index=idx)
        signal_full = breakout & volgate & sqzgate & score_ok & score.notna()
        # 对照组: 突破 + 放量 + 压缩（无共振门），用于展示多因子组合的增量
        signal_base = breakout & volgate & sqzgate & score.notna()

        # ================= 4) 当日状态 =================
        last = -1
        b0 = bool(breakout.iloc[last]) if np.isfinite(prior_high.iloc[last]) else False
        vr0 = float(vr.iloc[last]) if np.isfinite(vr.iloc[last]) else float("nan")
        q0 = float(sqz_q.iloc[last]) if np.isfinite(sqz_q.iloc[last]) else float("nan")
        s0 = float(score.iloc[last]) if np.isfinite(score.iloc[last]) else float("nan")
        fired = bool(signal_full.iloc[last])

        r.metric("close", float(close.iloc[last]), "最新收盘", 3)
        r.metric("prior_high", float(prior_high.iloc[last]), f"前{lookback}日最高", 3)
        r.metric("gate_breakout", b0, "门:突破", 1)
        r.metric("gate_volume", bool(vr0 >= vol_mult) if np.isfinite(vr0) else False,
                 "门:放量", 1)
        r.metric("gate_squeeze", bool(q0 <= squeeze_q) if np.isfinite(q0) else False,
                 "门:低波动", 1)
        r.metric("signal_today", fired, "今日触发复合信号", 1)
        r.metric("vam", float(vam.iloc[last]), "VAM 波动率调整动量", 3)
        r.metric("vpd", float(vpd.iloc[last]), "VPD 量价背离", 3)
        r.metric("vol_ratio", vr0, "量比(当日/前N日均量)", 2)
        r.metric("atr_pct", float(atr_pct.iloc[last]), "ATR%(14)", 4)
        r.metric("squeeze_q", q0, f"ATR% {std_window}日分位", 3)
        r.metric("score_z", s0, "综合z分", 2)
        for k, z in factor_z.items():
            r.metric(f"z_{k}", float(z.iloc[last]), f"{k} z-score", 2)

        # ---- 信号拆解（透明: 逐门报告）----
        if not np.isfinite(prior_high.iloc[last]):
            r.signal("info", f"数据不足以判断前{lookback}日高点")
        elif not b0:
            r.signal("info", f"未突破前{lookback}日最高 {prior_high.iloc[last]:.2f}"
                             f"（收盘 {close.iloc[last]:.2f}）")
        else:
            r.signal("ok", f"突破前{lookback}日最高 {prior_high.iloc[last]:.2f}")
            if vr0 >= vol_mult:
                r.signal("ok", f"量能确认: 量比 {vr0:.2f} ≥ {vol_mult}")
            else:
                r.signal("warn", f"量能不足: 量比 {vr0:.2f} < {vol_mult}"
                                 f"（疑似无量假突破，剔除）")
            if np.isfinite(q0):
                if q0 <= squeeze_q:
                    r.signal("ok", f"波动低位压缩: ATR%分位 {q0:.0%} ≤ {squeeze_q:.0%}")
                else:
                    r.signal("warn", f"波动未压缩: ATR%分位 {q0:.0%} > {squeeze_q:.0%}"
                                     f"（高波动环境，剔除）")
            if use_gate:
                if np.isfinite(s0) and s0 >= score_gate:
                    r.signal("ok", f"多因子共振: 综合z {s0:.2f} ≥ {score_gate}")
                elif np.isfinite(s0):
                    r.signal("warn", f"未共振: 综合z {s0:.2f} < {score_gate}（剔除）")

        if fired:
            r.signal("alert", f"★ 多因子复合突破信号（突破+量比{vr0:.2f}"
                              f"+ATR%分位{q0:.0%}+综合z {s0:.2f}）")

        # ================= 5) 历史回测统计（前视仅用于评估）=================
        full_idx = list(idx[signal_full])
        base_idx = list(idx[signal_base])
        _, stats_full = _forward_stats(close, full_idx, HORIZONS)
        _, stats_base = _forward_stats(close, base_idx, HORIZONS)

        r.metric("bt_count", len(full_idx), "历史信号数(全门)", 1)
        r.metric("bt_count_base", len(base_idx), "历史信号数(对照,无共振门)", 1)
        if stats_full[5]["n"]:
            r.metric("bt_win5", stats_full[5]["win"], "回测5日胜率(全门)", 3)
            r.metric("bt_win10", stats_full[10]["win"], "回测10日胜率(全门)", 3)
            r.metric("bt_win20", stats_full[20]["win"], "回测20日胜率(全门)", 3)
            r.metric("bt_mean5", stats_full[5]["mean"], "回测5日均收益(全门)", 3)
            if stats_base[5]["n"]:
                r.metric("bt_win5_base", stats_base[5]["win"], "回测5日胜率(对照组)", 3)
                r.metric("bt_win10_base", stats_base[10]["win"], "回测10日胜率(对照组)", 3)
                r.metric("bt_win20_base", stats_base[20]["win"], "回测20日胜率(对照组)", 3)
        else:
            r.signal("info", "历史无满足全部条件的信号（回测胜率不可用）")

        # ---- 回测信号明细表（最多 100 行，按日期倒序）----
        detail, _ = _forward_stats(close, full_idx, HORIZONS)
        if len(detail):
            table = pd.DataFrame({
                "close": close.reindex(detail.index),
                "vol_ratio": vr.reindex(detail.index),
                "squeeze_q": sqz_q.reindex(detail.index),
                "score_z": score.reindex(detail.index),
                **{c: detail[c] for c in detail.columns},
            })
            r.add_table("signals", table.sort_index(ascending=False), nd=3, max_rows=100)
        else:
            r.add_table("signals", pd.DataFrame(
                columns=["close", "vol_ratio", "squeeze_q", "score_z",
                         *[f"fwd{h}" for h in HORIZONS]]))

        # ---- 综合评分 0~100（当前信号质量）----
        z_clip = float(np.clip(s0, -2.0, 2.0)) if np.isfinite(s0) else 0.0
        overall = 50.0 + 15.0 * z_clip + (15.0 if fired else 0.0)
        r.score = round(float(np.clip(overall, 0, 100)), 1)
        if fired:
            r.verdict = "强复合信号"
        elif s0 >= 1.5:
            r.verdict = "多因子偏强(待突破)"
        elif s0 >= 0.5:
            r.verdict = "中性偏强"
        elif s0 >= -0.5:
            r.verdict = "中性"
        elif s0 >= -1.5:
            r.verdict = "中性偏弱"
        else:
            r.verdict = "偏弱"

        win5 = stats_full[5]["win"]
        bt_txt = (f"；回测5日胜率 {win5:.0%}（n={stats_full[5]['n']}）"
                  if win5 == win5 else "")
        r.summary = (
            f"综合z {s0:+.2f}" if np.isfinite(s0) else "综合分不可用"
        ) + (
            f"，今日触发复合突破信号" if fired else
            f"，{'已突破' if b0 else '未突破'}前{lookback}日高点"
        ) + bt_txt

        # ---- 图表序列（近 250 日）----
        tail = slice(-250, None)
        r.add_series("close", idx[tail], close[tail], title="收盘价")
        r.add_series("prior_high", idx[tail], prior_high[tail],
                     title=f"前{lookback}日最高(突破线)")
        r.add_series("vol_ratio", idx[tail], vr[tail], title="量比")
        r.add_series("atr_pct", idx[tail], atr_pct[tail], title="ATR%")
        r.add_series("score_z", idx[tail], score[tail], title="综合z分")
        # 信号日打点（0/1 序列，便于前端标出触发日）
        r.add_series("signal", idx[tail],
                     signal_full[tail].astype(float), title="复合突破信号(1=触发)")

        r.extra["model"] = {
            "factors": ["VAM波动率调整动量", "VPD量价背离", "量比VR", "波动压缩Squeeze"],
            "weights": {k: round(float(v) / w_total, 3) for k, v in weights.items()},
            "gates": {
                "breakout": f"close > 前{lookback}日最高价",
                "volume": f"当日量 ≥ {vol_mult} × 前{lookback}日均量",
                "squeeze": f"ATR%分位 ≤ {squeeze_q:.0%}（近{std_window}日）",
                "score": f"综合z ≥ {score_gate}" if use_gate else "关闭",
            },
            "horizons_days": list(HORIZONS),
            "note": "回测前瞻收益仅用于事后评估，信号判定本身无未来函数；"
                    "参数取行业常见值，未做网格搜索（防过拟合）。",
        }
        return r
