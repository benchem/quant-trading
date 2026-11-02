# 超短线候选扫描任务（买 2026-11-03，本周五 11-06 前卖出）

## 目标
在本地 A 股数据库中按下列规则扫描，输出明日超短线买入候选 Top 10，产出一个可重跑的扫描脚本 + CSV + 人读报告。

## 环境（已核实，勿质疑）
- 数据目录：/opt/quant-trading/data/
  - 日线（后复权 hfq）：daily/hfq/*.parquet，共 5223 个文件，基准日 2026-11-02（5217 只含当日 bar，6 只停牌无当日 bar 属正常）
  - 个股基础信息：dataset/stock_basic.parquet，列 = code, name, latest_price, pct_chg, turnover, total_mv, float_mv（5223 行；total_mv/float_mv 单位为元）
  - 行业板块成分：board_members.parquet，列 = code, board_type, board_code, board_name（5222 行）
  - 日线列 = date, code, open, high, low, close, volume, amount, amplitude, pct_chg, change, turnover
- Python：/opt/quant-trading/.venv/bin/python（pandas 可用）
- 工作目录：/root/workspaces/quant-trading

## 口径声明（铁律）
1. **今日涨跌幅必须自算**：pct = close/prev_close - 1（同股相邻两日），**禁止使用日线文件内的 pct_chg 列与 stock_basic 的 pct_chg 列**（已知 vintage 错误）
2. hfq 后复权价：跨股绝对价不可比；同股内的均线/比值/相对位置有效
3. MA20/MA60/MA250 在 hfq close 序列上计算，取截至 2026-11-02
4. 量比 = 11-02 volume ÷ 前 5 个交易日（10-27~10-30，共 4 个可得交易日则用 4 个）volume 均值
5. 基准日 = 2026-11-02 = 每只股票文件内的最大日期
6. 流通市值用 stock_basic.float_mv，单位换算成亿元（÷1e8）

## 选股规则（全部基于 2026-11-02 收盘数据，按序过滤，每步打印剩余数量）
1. 剔除 ST/*ST/退市（name 匹配）与停牌股（无 11-02 bar）
2. 剔除 300687.SZ、300199.SZ
3. 流通市值 30 亿 ~ 300 亿元
4. 11-02 成交额（amount）≥ 2 亿元
5. 11-02 自算 pct ≥ +5%
6. 量比 ≥ 2.0
7. 收盘位置 = (close-low)/(high-low) ≥ 0.70；且非一字板：high>low 且 (high-low)/prev_close ≥ 3%
8. **结构突破（二选一）**：
   a. 昨收 ≤ MA250 且 11-02 收 > MA250（今日首次站上年线）
   b. 11-02 收盘 = 近 60 日收盘最高（平台突破）
9. 距近 60 日最高收盘价还有 ≥ 5% 空间（(60日最高收盘/今收-1) ≥ 5%）
10. 近 20 日累计涨幅（含今日）≤ +30%
11. 板块效应：所属 board_type=行业 的板块，其成分股 11-02 平均自算 pct ≥ +1.5%（无行业归属的股票标"无行业"并跳过本条，不剔除）

排序输出 Top 10：综合分 = 量比 × 收盘位置 × 板块平均涨幅，报告含全部指标列（代码/名称/行业/今pct/量比/收盘位置/距60日高%/20日涨幅%/流通市值亿/成交额亿/触发8a还是8b）。

## 只读纪律
- **不改任何现有文件、不执行任何 git 写操作、不部署、不对 /opt 目录做任何写操作**
- 新建文件仅限 scans/ 目录下四个：brief_ultrashort.md（本文件，已有）、scan_ultrashort.py、scan_ultrashort_20261102.csv、scan_ultrashort_20261102.md
- 脚本对 /opt 数据只读；脚本支持 --date 参数默认 2026-11-02（便于以后重跑）

## 自检锚点（不符先查自身解析）
- daily/hfq 文件总数 = 5223；含 2026-11-02 bar 的 = 5217
- 300687.SZ 在过滤第 2 步被剔除
- 过滤数量应大体单调递减；若规则 5 后剩余 > 400 或最终候选 > 50，先回头查自己的解析
- board_members 覆盖 5222 只，个别股票无行业属正常

## 完成标记
全部完成后，最后单独一行打印：ULTRASHORT_SCAN_DONE
