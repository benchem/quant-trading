# 任务：200 轮 walk-forward 推演引擎（深度分析 Stage 1）

## 目标
写一个向量化引擎：对过去 200 个交易日做 T-1 选股 → T 日验证的 walk-forward 推演，输出 ① 策略长期统计 ② 个股历史因子统计 ③ asof 2026-11-02 的 Top30 候选池（带历史统计列）。这是后续多 agent 对抗打分的数据底座，**数字必须可复算**。

## 环境（已核实，勿质疑）
- 仓库：/root/workspaces/quant-trading；Python：/opt/quant-trading/.venv/bin/python（pandas/pyarrow）
- 数据（只读）：
  - /opt/quant-trading/data/dataset/daily_hfq.parquet —— 16,961,139 行 × 5,223 只，1990-12-19 → 2026-11-02（最后日 5,217 行）；列 = date, code, name, open, high, low, close, volume, amount, amplitude, pct_chg, change, turnover
  - /opt/quant-trading/data/dataset/stock_basic.parquet —— code, name, latest_price, pct_chg, turnover, total_mv, float_mv（float_mv 单位=元）
  - /opt/quant-trading/data/board_members.parquet —— code, board_type, board_code, board_name（board_type='industry' 为行业，覆盖 5,222 只）
- 注意：**另有一个 CC 实例正在改 analyzer/ 目录（模拟交易模块）——你不许读写 analyzer/ 下任何文件**。

## 口径（与既有超短扫描完全一致，铁律）
1. pct 自算 = close/prev_close - 1（禁用 pct_chg 列）
2. hfq 后复权：跨股绝对价不可比；同股内均线/比值有效
3. amount 列质量差（11-02 仅 3/5217 有值）→ 成交额估算：amount_est = volume×100×close×latest_price/close_latest（锚点：601019 11-02 估算 ≈2.74 亿 vs 真实 2.68 亿）
4. 量比 = 当日 volume ÷ 前 5 日 volume 均值；MA250/MA60/MA20 在 hfq close 上算
5. 交易日历 = 数据集 date 去重升序；200 轮 = 截至并含 2026-11-02 的最后 200 个交易日

## 策略「超短动量」（asof 日 D 收盘后选股）
1. 非 ST/*ST/退市（name 匹配）、D 有 bar
2. 流通市值 30~300 亿
3. D 成交额（估算）≥ 2 亿
4. D 自算 pct ≥ +5%
5. 量比 ≥ 2.0
6. 收盘位置 ≥ 0.70 且 (high-low)/prev_close ≥ 3%（非一字）
7. 结构突破（二选一）：a) D-1 收 ≤ MA250 且 D 收 > MA250；b) D 收盘 = 近 60 日收盘最高（8b 免检下方空间要求）
8. 近 20 日累计涨幅 ≤ +30%
9. 板块效应：行业成分股 D 平均自算 pct ≥ +1.5%（无行业跳过本条不剔除）
- 得分 = 量比 × 收盘位置 × 板块平均涨幅，取 TopN=10

## 回测执行模型（每轮 T）
- 信号日 = T 的前一交易日；picks = 该日策略 Top10
- T 日开盘价买入；退出（保守）：low ≤ 买价×0.96 → 止损 @0.96；否则 high ≥ 买价×1.06 → 止盈 @1.06；两者同日 → 止损优先；否则收盘卖出
- T 无 bar → untradeable_suspended；open=high=low → untradeable_limit；不计入胜率
- 胜率 = 收益>0 的 pick ÷ 可成交 pick

## 输出（全部写到 deepscan/20261102_deep200/）
1. `engine_summary.json` —— 策略统计：{rounds:200, total_picks, tradeable, win_rate, avg_ret, t_up_rate, tp_rate, stop_rate, untradeable_suspended, untradeable_limit, best_round, worst_round, 月度胜率序列}
2. `per_stock_stats.csv` —— 200 轮个股累计：code, name, times_picked, avg_ret_when_picked, win_rate_when_picked, last_picked_date
3. `candidates_top30.csv` —— **asof 2026-11-02 策略排名 Top30**（为 11-03 买入日选股），列 = code, name, board_name, pct, vol_ratio, close_pos, room60, pct20, float_mv_yi, amount_est_yi, signal(8a/8b), board_avg_pct, score, rank + 左连 per_stock_stats 的四列历史统计（times_picked 等，没被选过填 0）
4. `rounds_detail.csv` —— 每轮：t_date, signal_date, n_picks, n_tradeable, win_rate, avg_ret
5. `engine200.py` 放 deepscan/，支持 `--rounds 200 --topn 10 --asof 2026-11-02` 参数

## 性能要求
全程向量化（按 date 预切片 + groupby），单次完整运行 ≤ 300 秒。禁止逐股票循环读 5,223 个小文件（读合并 dataset）。

## 输出文件白名单（只许动这些）
- 新建：deepscan/engine200.py、deepscan/20261102_deep200/ 下上述 4 个产物
- **不许动**：analyzer/、planwatch/、monitoring/、scans/、downloader/、data/ 任何文件；不执行任何 git 操作
- /opt 数据只读

## 自检锚点（不符先查自身实现）
1. daily_hfq.parquet 行数 = 16,961,139、codes = 5,223、最后日 = 2026-11-02（5,217 行）
2. **asof=2026-11-01（T=2026-11-02）那轮的 picks 必含 601019.SH**（山东出版：11-02 +9.97% 涨停、8b 60 日新高、量比 3.43——实盘同规则扫描已选中）
3. 同轮 300788.SZ（中信出版 +20%）也应在 picks
4. 601019.SH 11-02 close(hfq)=11.2558、volume=360132、amount=NaN
5. 200 轮合计 total_picks 应在数千量级（约 2,000 笔），若只有几百或上万，查实现
6. engine_summary.win_rate 应在 0.3~0.7 之间（超短策略合理区间），若 >0.9 或 <0.1，先查成交模型实现（多半是一字/停牌没剔除干净或前视偏差）

## 前视偏差自查（必做，写进报告）
- MA/量比/20日涨幅 的窗口只用 ≤ 信号日数据；板块平均涨幅只用 ≤ 信号日
- 任何用到 T 日 open/high/low/close 的地方只出现在执行模型，不出现在选股

## 完成标记
全部完成后最后单独一行打印：ENGINE200_DONE
