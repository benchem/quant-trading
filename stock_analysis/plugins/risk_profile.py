"""风险画像（示例插件：risk 类）。

内容:
- 年化波动率、年化收益、最大回撤（含区间日期）
- 相对基准指数的 beta、下行标准差、Sharpe/Calmar（rf=0）
- 日收益分位数（尾部风险）、胜率
- 滚动 60 日波动/回撤明细表（近 12 个月，按月末）
- 回撤曲线序列供画图

作者: 框架示例
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from stock_analysis import AnalysisPlugin, AnalysisResult, DataUnavailable, ParamSpec

#: 日收益截断（去 hfq 低价股最小报价单位的量化噪声；真实日收益极少超过 ±30%）
_RET_CLIP = 0.30


class RiskProfilePlugin(AnalysisPlugin):
    name = "risk_profile"
    display_name = "风险画像"
    description = "波动率/最大回撤/beta/尾部风险，相对基准指数的风险画像"
    version = "0.1.0"
    category = "risk"
    params = {
        "window": ParamSpec(type="int", default=250, min=60, max=500,
                            help="统计窗口（交易日）"),
        "index": ParamSpec(type="str", default="000300.SH",
                           choices=("000001.SH", "000300.SH", "000905.SH", "399001.SZ", "399006.SZ"),
                           help="基准指数"),
    }

    def run(self, ctx, code: str, window: int = 250,
            index: str = "000300.SH") -> AnalysisResult:
        df = ctx.kline(code, last=window + 5)
        if len(df) < 60:
            r = AnalysisResult(self.name, code)
            r.summary = f"历史数据不足（实际 {len(df)} 个交易日 < 60），跳过"
            r.signal("warn", "历史数据不足")
            return r

        close = df["close"].astype(float)
        rets = (close.pct_change().dropna()).clip(-_RET_CLIP, _RET_CLIP)
        ann = ctx.annualize()

        # ---- 基本风险指标 ----
        total_ret = float(close.iloc[-1] / close.iloc[0] - 1.0)
        n = len(close)
        ann_ret = float((1.0 + total_ret) ** (ann / n) - 1.0) if n > 1 else 0.0
        vol = float(rets.std(ddof=1) * np.sqrt(ann)) if len(rets) > 2 else float("nan")

        cummax = close.cummax()
        dd = close / cummax - 1.0
        maxdd = float(dd.min())
        # 回撤起点 = 谷底前最高价所在日（用位置索引，避免 DatetimeIndex 标签歧义）
        if maxdd < 0:
            trough_pos = int(np.argmin(dd.to_numpy()))
            peak_pos = int(np.argmax(close.to_numpy()[: trough_pos + 1]))
            dd_from, dd_to = close.index[peak_pos], close.index[trough_pos]
        else:
            dd_from = dd_to = close.index[-1]

        downside = rets[rets < 0]
        downside_dev = float(downside.std(ddof=1) * np.sqrt(ann)) if len(downside) > 2 else float("nan")
        sharpe = ann_ret / vol if vol and vol > 0 else float("nan")
        calmar = ann_ret / abs(maxdd) if maxdd < 0 else float("nan")
        win_rate = float((rets > 0).mean())
        worst_day = float(rets.min())
        p5 = float(rets.quantile(0.05))

        # ---- 相对基准 ----
        beta = cov = None
        idf = ctx.index_kline(index)
        if idf is not None and "close" in idf.columns:
            idx_close = idf["close"].reindex(close.index).ffill()
            i_rets = idx_close.pct_change().clip(-_RET_CLIP, _RET_CLIP)
            j = pd.concat([rets, i_rets], axis=1).dropna()
            j.columns = ["a", "b"]
            if len(j) > 10 and float(j["b"].var()) > 0:
                cov = float(j["a"].cov(j["b"]))
                beta = cov / float(j["b"].var())

        # ---- 滚动 60 日明细（近 12 个月月末）----
        roll_vol = rets.rolling(60).std(ddof=1) * np.sqrt(ann)
        roll_dd = (close / close.cummax() - 1.0).rolling(120).min()
        # 每月最后一个交易日
        month_ends = (
            close.to_frame("x").groupby(close.index.to_period("M")).tail(1).index[-13:]
        )
        table = pd.DataFrame({
            "vol60": roll_vol.reindex(month_ends),
            "dd120": roll_dd.reindex(month_ends),
            "close": close.reindex(month_ends),
        }).dropna(how="all")

        # ---- 评分（越高 = 风险越低）----
        vol_pen = min(vol / 0.80, 1.0) if vol == vol else 0.5
        dd_pen = min(abs(maxdd) / 0.60, 1.0) if maxdd == maxdd else 0.5
        score = 100.0 * (1.0 - 0.5 * vol_pen - 0.5 * dd_pen)

        r = AnalysisResult(self.name, code)
        r.metric("ann_ret", ann_ret, "年化收益", 4)
        r.metric("vol", vol, "年化波动", 4)
        r.metric("maxdd", maxdd, "最大回撤", 4)
        r.metric("beta", beta, "beta(vs基准)", 2)
        r.metric("downside_dev", downside_dev, "下行波动", 4)
        r.metric("sharpe", sharpe, "Sharpe(rf=0)", 2)
        r.metric("calmar", calmar, "Calmar", 2)
        r.metric("win_rate", win_rate, "上涨日占比", 4)
        r.metric("worst_day", worst_day, "最差单日", 4)
        r.metric("ret_p5", p5, "5%分位日收益", 4)
        r.extra["dd_from"] = str(dd_from.date())
        r.extra["dd_to"] = str(dd_to.date())
        r.extra["benchmark"] = index
        r.score = round(score, 1)
        r.verdict = (
            "低风险" if score >= 70 else "中低风险" if score >= 55 else
            "中风险" if score >= 40 else "中高风险" if score >= 25 else "高风险"
        )
        beta_txt = f"，beta {beta:.2f}" if beta is not None else ""
        r.summary = (
            f"年化波动 {vol * 100:.0f}%，最大回撤 {maxdd * 100:.0f}%"
            f"（{dd_from.date()}~{dd_to.date()}）{beta_txt}"
        )

        # ---- 信号 ----
        if vol >= 0.6:
            r.signal("alert", f"波动率极高（年化 {vol * 100:.0f}%）")
        elif vol >= 0.4:
            r.signal("warn", f"波动率偏高（年化 {vol * 100:.0f}%）")
        if maxdd <= -0.5:
            r.signal("alert", f"最大回撤达 {maxdd * 100:.0f}%")
        elif maxdd <= -0.35:
            r.signal("warn", f"最大回撤 {maxdd * 100:.0f}%")
        if beta is not None and beta >= 1.5:
            r.signal("info", f"beta={beta:.2f}，显著高于市场")
        if worst_day <= -0.15:
            r.signal("warn", f"出现过 {worst_day * 100:.1f}% 的单日暴跌")

        # ---- 表格 + 图表序列 ----
        r.add_table("monthly", table)
        dd_chart = (dd.tail(250))
        r.add_series("drawdown", dd_chart.index, dd_chart, title="回撤曲线")
        return r
