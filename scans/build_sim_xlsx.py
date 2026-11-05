#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建模拟交易台账 XLSX（用户逐笔检查用）"""
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

SRC = "/root/workspaces/quant-trading/sim/sim_trades_chase_202606.csv"
OUT = "/root/workspaces/quant-trading/sim/模拟交易台账_追高漏斗_202606起.xlsx"

tr = pd.read_csv(SRC, parse_dates=["T", "buy_date", "sell_date"])
tr["盈亏元"] = (tr["ret_net"] * 10000).round(0)
tr["浮盈亏元"] = (tr["float_ret"] * 10000).round(0)

wb = Workbook()

# ── Sheet1 说明 ──
ws = wb.active
ws.title = "台账说明"
notes = [
    ["模拟交易台账：追高漏斗（现行 11 层口径）× 2026-06-01 起 walk-forward 推演", ""],
    ["", ""],
    ["推演口径", ""],
    ["信号日 T", "收盘后按现行 11 层漏斗选股（只用 ≤T 数据，无前视）"],
    ["买入", "T+1 开盘价（一字涨停买不进则跳过；停牌跳过）"],
    ["卖出", "T+5（信号日后第 5 个交易日）收盘价；跌停锁死顺延"],
    ["成本", "双边 0.68%"],
    ["市值闸", "当时流通市值 30~300 亿（amount×100/turnover 重建，非今日值）"],
    ["仓位", "口径A=每日全部信号每笔 1 万元；口径B=4 槽×1 万 FIFO（is_slot=是）"],
    ["", ""],
    ["验证与结论", ""],
    ["漏斗复刻验证", "2026-11-02 信号 13 只，已知候选池 10 只全部命中"],
    ["已了结 282 笔", "中位 -2.76%，均值 -1.77%，胜率 35.8%"],
    ["4 槽账户 64 笔", "累计 -9,463 元 / 22 周 = 周均 -427 元"],
    ["", ""],
    ["注意", "买入价为后复权价（与真实价差复权因子），收益率不受影响；"],
    ["", "ST 过滤用当前名单（无历史 ST 数据，近似）；行业归属为当前值（近似）。"],
    ["", "2026-06~11 在 52 周回测窗口内——这是规则冻结后的机械复算，非严格样本外。"],
]
for r, (a, b) in enumerate(notes, 1):
    ws.cell(r, 1, a).font = Font(bold=True) if b == "" and a else Font()
    ws.cell(r, 2, b)
ws.cell(1, 1).font = Font(bold=True, size=13)
ws.column_dimensions["A"].width = 22
ws.column_dimensions["B"].width = 95

# ── Sheet2 逐笔台账 ──
ws2 = wb.create_sheet("模拟交易逐笔")
cols = [("信号日", "T", "date"), ("代码", "code", None), ("名称", "name", None),
        ("行业", "board", None), ("信号", "sig", None), ("量比", "vol_ratio", "0.00"),
        ("收盘位置", "cpos", "0.00"), ("当日涨幅", "pct", "0.0%"),
        ("成交额亿", "amount_yi", "0.0"), ("当时市值亿", "mv_then_yi", "0"),
        ("买入日", "buy_date", "date"), ("买入价hfq", "buy_px", "0.00"),
        ("卖出日", "sell_date", "date"), ("净收益率", "ret_net", "0.00%"),
        ("盈亏元", "盈亏元", "#,##0"), ("状态", "status", None),
        ("4槽账户", None, None)]
hdr = [c[0] for c in cols]
ws2.append(hdr)
for _, r in tr.iterrows():
    row = []
    for _, key, fmt in cols:
        if key is None:
            row.append("是" if r["is_slot"] else "")
        else:
            v = r[key]
            if key == "is_slot":
                continue
            row.append(v.date() if isinstance(v, pd.Timestamp) else v)
    ws2.append(row)
for j, (_, _, fmt) in enumerate(cols, 1):
    if fmt == "date":
        for cell in ws2.iter_cols(min_col=j, max_col=j, min_row=2):
            for c in cell:
                c.number_format = "yyyy-mm-dd"
    elif fmt:
        for cell in ws2.iter_cols(min_col=j, max_col=j, min_row=2):
            for c in cell:
                c.number_format = fmt
for c in ws2[1]:
    c.font = Font(bold=True, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor="4472C4")
widths = [11, 11, 10, 22, 6, 7, 8, 9, 9, 10, 11, 11, 11, 10, 9, 16, 8]
for j, w in enumerate(widths, 1):
    ws2.column_dimensions[get_column_letter(j)].width = w
ws2.freeze_panes = "A2"
ws2.auto_filter.ref = f"A1:Q{len(tr)+1}"
# 条件格式: 净收益率红绿
from openpyxl.formatting.rule import CellIsRule
rng = f"N2:N{len(tr)+1}"
ws2.conditional_formatting.add(rng, CellIsRule(operator="greaterThan", formula=["0"],
                               fill=PatternFill("solid", fgColor="C6EFCE")))
ws2.conditional_formatting.add(rng, CellIsRule(operator="lessThan", formula=["0"],
                               fill=PatternFill("solid", fgColor="FFC7CE")))

# ── Sheet3 周汇总 ──
ws3 = wb.create_sheet("周汇总")
done = tr[tr["ret_net"].notna()].copy()
done["wk"] = pd.to_datetime(done["sell_date"]).dt.to_period("W").dt.end_time.dt.normalize()
wk = done.groupby("wk").agg(笔数=("ret_net", "size"), 胜率=("ret_net", lambda x: (x > 0).mean()),
                            中位=("ret_net", "median"), 均值=("ret_net", "mean"),
                            全信号周盈亏=("盈亏元", "sum")).reset_index()
sl = done[done["is_slot"] == True]
wks = sl.groupby(sl["sell_date"].dt.to_period("W").dt.end_time.dt.normalize())["盈亏元"].sum()
wk["四槽周盈亏"] = wk["wk"].map(wks).fillna(0).astype(int)
wk["四槽累计"] = wk["四槽周盈亏"].cumsum()
wk = wk.rename(columns={"wk": "周结束日"})
ws3.append(list(wk.columns))
for _, r in wk.iterrows():
    ws3.append([r["周结束日"].date()] + list(r[["笔数", "胜率", "中位", "均值", "全信号周盈亏", "四槽周盈亏", "四槽累计"]]))
for j, fmt in [(2, "0"), (3, "0%"), (4, "0.00%"), (5, "0.00%"), (6, "#,##0"), (7, "#,##0"), (8, "#,##0")]:
    for cell in ws3.iter_cols(min_col=j, max_col=j, min_row=2):
        for c in cell:
            c.number_format = fmt
for c in ws3[1]:
    c.font = Font(bold=True, color="FFFFFF")
    c.fill = PatternFill("solid", fgColor="4472C4")
for j, w in enumerate([11, 7, 7, 9, 9, 12, 12, 11], 1):
    ws3.column_dimensions[get_column_letter(j)].width = w
ws3.freeze_panes = "A2"

wb.save(OUT)
print("saved:", OUT, "| 逐笔", len(tr), "行 | 周", len(wk), "行")
