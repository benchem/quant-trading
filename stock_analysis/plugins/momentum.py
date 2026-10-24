"""动量/趋势分析（示例插件：technical 类）。

内容:
- window 日动量收益、趋势斜率（对数价格线性回归，年化）
- RSI(14)、收盘价相对长均线位置
- 综合评分 0~100 + 结论 + 信号
- 输出近 250 日 close/ma 序列供画图

作者: 框架示例（可直接复制本文件作为新插件模板）
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from stock_analysis import AnalysisPlugin, AnalysisResult, ParamSpec


def _rsi(close: pd.Series, period: int = 14) -> float | None:
    """Wilder RSI。"""
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    last_g, last_l = float(avg_gain.iloc[-1]), float(avg_loss.iloc[-1])
    if last_l == 0:
        return 100.0 if last_g > 0 else 50.0
    rs = last_g / last_l
    return float(100.0 - 100.0 / (1.0 + rs))


def _slope_annualized(close: pd.Series, annualize: float) -> float | None:
    """对数价格 OLS 斜率 -> 年化收益率（e^slope*n - 1）。"""
    x = np.arange(len(close), dtype=float)
    y = np.log(close.to_numpy(dtype=float))
    if np.any(~np.isfinite(y)):
        return None
    k, _ = np.polyfit(x, y, 1)
    return float(np.exp(k * annualize) - 1.0)


class MomentumPlugin(AnalysisPlugin):
    name = "momentum"
    display_name = "动量/趋势"
    description = "动量收益、趋势斜率、RSI 与均线位置，输出综合评分与图表序列"
    version = "0.1.0"
    category = "technical"
    params = {
        "window": ParamSpec(type="int", default=20, min=5, max=120, help="动量回看交易日数"),
        "ma": ParamSpec(type="int", default=60, min=10, max=250, help="长均线周期（趋势参考）"),
        "rsi_period": ParamSpec(type="int", default=14, min=5, max=30, help="RSI 周期"),
    }

    def run(self, ctx, code: str, window: int = 20, ma: int = 60,
            rsi_period: int = 14) -> AnalysisResult:
        need = max(window, ma, rsi_period) + 5
        df = ctx.kline(code, last=max(need + 250, 300))
        if len(df) < need:
            r = AnalysisResult(self.name, code)
            r.summary = f"历史数据不足（需 {need} 个交易日，实际 {len(df)}），跳过"
            r.signal("warn", "历史数据不足")
            return r

        close = df["close"].astype(float)
        last_close = float(close.iloc[-1])
        ref = float(close.iloc[-1 - window])
        ret_window = last_close / ref - 1.0 if ref > 0 else float("nan")

        ma_s = close.rolling(ma).mean()
        last_ma = float(ma_s.iloc[-1]) if np.isfinite(ma_s.iloc[-1]) else float("nan")
        slope = _slope_annualized(close.tail(window), ctx.annualize())
        rsi = _rsi(close, rsi_period)

        # ---- 分项评分（透明可审计的公式）----
        ret_score = float(np.clip(50.0 + 100.0 * np.tanh(ret_window / 0.25), 0, 100))
        trend_score = (
            float(np.clip(50.0 + (last_close / last_ma - 1.0) * 500.0, 0, 100))
            if np.isfinite(last_ma) and last_ma > 0 else 50.0
        )
        rsi_score = (
            float(100.0 - min(abs(rsi - 55.0), 45.0) / 45.0 * 100.0) if rsi is not None else 50.0
        )
        slope_score = float(np.clip(50.0 + (slope or 0.0) * 100.0, 0, 100))

        score = 0.45 * ret_score + 0.25 * trend_score + 0.20 * rsi_score + 0.10 * slope_score

        r = AnalysisResult(self.name, code)
        r.metric("ret_window", ret_window, f"近{window}日收益", 4)
        r.metric("rsi", rsi, "RSI", 1)
        r.metric("close", last_close, "最新收盘", 3)
        r.metric("ma", last_ma, f"{ma}日均线", 3)
        r.metric("slope_ann", slope, "趋势斜率(年化)", 4)
        r.score = round(score, 1)
        r.verdict = (
            "偏强" if score >= 65 else "中性偏强" if score >= 55 else
            "中性" if score >= 45 else "中性偏弱" if score >= 35 else "偏弱"
        )
        r.summary = (
            f"近{window}日 {ret_window * 100:+.1f}%，"
            f"{'站上' if last_close > last_ma else '跌破'}{ma}日线，"
            f"RSI {rsi:.0f}" if rsi is not None else
            f"近{window}日 {ret_window * 100:+.1f}%"
        )

        # ---- 信号 ----
        if rsi is not None and rsi >= 80:
            r.signal("warn", f"RSI={rsi:.0f} 超买，短期回调风险")
        elif rsi is not None and rsi <= 20:
            r.signal("info", f"RSI={rsi:.0f} 超卖")
        if ret_window > 0.15:
            r.signal("ok", f"近{window}日上涨 {ret_window * 100:.1f}%，动量显著")
        if ret_window < -0.15:
            r.signal("warn", f"近{window}日下跌 {abs(ret_window) * 100:.1f}%")
        if np.isfinite(last_ma) and last_close > last_ma:
            r.signal("info", f"价格位于 {ma} 日均线上方")
        else:
            r.signal("warn", f"价格位于 {ma} 日均线下方")

        # ---- 图表序列（近 250 日）----
        chart = df.tail(250)
        ma_chart = close.rolling(ma).mean().tail(250)
        r.add_series("close", chart.index, chart["close"], title="收盘价")
        r.add_series("ma", chart.index, ma_chart, title=f"{ma}日均线")
        return r
