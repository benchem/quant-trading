"""基本面质量（示例插件：fundamental 类，演示"数据缺失时的优雅降级"）。

内容（全部来自**本地缓存**的财报，不触发网络）:
- 最近 N 个报告期的 ROE / 营收增速 / 净利增速 / 毛利率 / 负债率
- 成长与质量的综合评分
- 近 N 期明细表
- 若本地无财报数据：返回 ok 结果 + 提示先跑
  ``python -m analyzer fundamental --codes <code>``（演示降级路径）

作者: 框架示例
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from stock_analysis import AnalysisPlugin, AnalysisResult, ParamSpec


class FundamentalQualityPlugin(AnalysisPlugin):
    name = "fundamental_quality"
    display_name = "基本面质量"
    description = "基于本地财报缓存的 ROE/成长/杠杆质量评分（无数据时优雅降级）"
    version = "0.1.0"
    category = "fundamental"
    params = {
        "periods": ParamSpec(type="int", default=8, min=2, max=40, help="分析最近 N 个报告期"),
    }

    def run(self, ctx, code: str, periods: int = 8) -> AnalysisResult:
        df = ctx.fundamental(code)
        r = AnalysisResult(self.name, code)
        if df is None or df.empty:
            r.summary = "本地无财报缓存"
            r.signal("warn", "本地无财报数据，先运行: analyzer fundamental --codes "
                             f"{code}（或 --all 全市场预热）")
            r.extra["hint"] = "python -m analyzer fundamental --codes " + code
            return r

        recent = df.tail(periods).reset_index(drop=True)
        last = recent.iloc[-1]

        def _g(row, col):
            v = row.get(col)
            try:
                v = float(v)
            except (TypeError, ValueError):
                return None
            return v if v == v else None

        roe = _g(last, "roe")
        rev_yoy = _g(last, "rev_yoy")
        np_yoy = _g(last, "net_profit_yoy")
        gm = _g(last, "gross_margin")
        nm = _g(last, "net_margin")
        debt = _g(last, "debt_ratio")
        eps = _g(last, "eps")

        # 成长均值（近 4 期，剔除缺失）
        g4 = recent[["rev_yoy", "net_profit_yoy"]].tail(4)
        growth_vals = [v for v in g4.to_numpy(dtype=float).ravel() if v == v]
        growth_avg = float(np.mean(growth_vals)) if growth_vals else None

        # ---- 评分（透明公式）----
        parts = []
        if roe is not None:
            parts.append((0.4, float(np.clip(roe / 0.20, 0, 1)) * 100))
        if growth_avg is not None:
            parts.append((0.3, float(np.clip(50 + 100 * np.tanh(growth_avg / 0.3), 0, 100))))
        if debt is not None:
            parts.append((0.3, float(np.clip(100 - debt * 120, 0, 100))))
        score = (float(sum(w * value for w, value in parts) / sum(w for w, _ in parts))
                 if parts else None)

        r.metric("roe", roe, "ROE(最新期)", 4)
        r.metric("rev_yoy", rev_yoy, "营收同比", 4)
        r.metric("net_profit_yoy", np_yoy, "净利同比", 4)
        r.metric("growth_avg4", growth_avg, "近4期平均增速", 4)
        r.metric("gross_margin", gm, "毛利率", 4)
        r.metric("net_margin", nm, "净利率", 4)
        r.metric("debt_ratio", debt, "资产负债率", 4)
        r.metric("eps", eps, "EPS(最新期)", 3)
        if score is not None:
            r.score = round(score, 1)
            r.verdict = (
                "优" if score >= 70 else "良" if score >= 55 else
                "一般" if score >= 40 else "弱"
            )
        period_str = (
            f"{recent['period'].iloc[0].date()} ~ {recent['period'].iloc[-1].date()}"
        )
        roe_txt = f"，ROE {roe * 100:.1f}%" if roe is not None else ""
        r.summary = f"近{len(recent)}期({period_str}){roe_txt}"

        # ---- 信号 ----
        if roe is not None and roe < 0:
            r.signal("warn", f"最新报告期 ROE 为负（{roe * 100:.1f}%）")
        if roe is not None and roe >= 0.15 and (growth_avg or 0) > 0:
            r.signal("ok", "高回报 + 正增长")
        if rev_yoy is not None and rev_yoy < 0:
            r.signal("warn", f"最新期营收同比 {rev_yoy * 100:.1f}%（下滑）")
        if np_yoy is not None and np_yoy < 0:
            r.signal("warn", f"最新期净利同比 {np_yoy * 100:.1f}%（下滑）")
        if debt is not None and debt > 0.7:
            r.signal("warn", f"资产负债率偏高（{debt * 100:.0f}%）")

        # ---- 明细表 ----
        cols = ["period", "rev_yoy", "net_profit_yoy", "roe", "gross_margin", "debt_ratio"]
        cols = [c for c in cols if c in recent.columns]
        r.add_table("recent_periods", recent[cols])
        return r
