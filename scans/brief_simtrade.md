# 任务：仪表盘改名 + 新增「模拟交易」板块（T-1 选股 → T 日验证回测）

## 目标
1. 把系统名从「A股数据分析仪表盘」改为「量化交易分析」（所有出现处）。
2. 新增一个「模拟交易」tab：内置选股策略，用过去 N 个交易日做 walk-forward 推演——**每个交易日 T，只用 T-1 及以前的数据选股，再用 T 日实际行情验证，计算胜率**。

## 环境（已核实，勿质疑）
- 仓库：/root/workspaces/quant-trading（与运行目录 /opt/quant-trading 同源；本任务只改仓库，部署由主 Agent 负责）
- 服务：analyzer 包，`python -m analyzer serve`，端口 8765，纯标准库 http 路由（server.py 内 if/elif 分发），前端原生 JS + ECharts（web/index.html + app.js + style.css）
- 现有 5 个 tab：市场/个股分析/交易记录/交易日记/站内信（index.html nav.tabs + section.panel 结构；app.js 有对应 tab 切换逻辑）
- 个股分析内有 subtab 模式可参照（走势对比/多因子突破/GARCH 回归/筹码分布/布林通道/…）
- 数据（全部只读）：
  - /opt/quant-trading/data/dataset/daily_hfq.parquet —— 合并日线（后复权），16,961,139 行 × 5,223 只，1990-12-19 → 2026-11-02（最后日 5,217 行），列 = date, code, name, open, high, low, close, volume, amount, amplitude, pct_chg, change, turnover
  - /opt/quant-trading/data/dataset/stock_basic.parquet —— 列 = code, name, latest_price, pct_chg, turnover, total_mv, float_mv（5,223 行）
  - /opt/quant-trading/data/board_members.parquet —— 列 = code, board_type, board_code, board_name（5,222 行；board_type='industry' 为行业）
- Python：/opt/quant-trading/.venv/bin/python（pandas/pyarrow 可用）

## 改动一：改名（先做，独立可验证）
「A股数据分析仪表盘」→「量化交易分析」。已知出现处：index.html 的 `<title>`、header `.brand`、登录卡 `.auth-title`；app.js 首行注释；用 grep 全仓库扫「A股数据分析仪表盘」和「数据分析仪表盘」确保无遗漏（含 README.md、docs/ 若有）。语义性文案（如 auth-sub 副标题）保持通顺可微调。

## 改动二：模拟交易板块

### 后端
新建 `analyzer/backtest.py`：
- **策略注册表**结构（dict: name → 函数），v1 只实现一个策略「超短动量」，便于后续扩展
- 数据加载：懒加载单例（首次调用读 daily_hfq.parquet 只取 date/code/open/high/low/close/volume + stock_basic + board_members，进程内缓存）
- **策略「超短动量」（asof 日期 D-1 收盘后选股，全部向量化）**：
  1. 非 ST/*ST/退市（name 匹配）、D-1 有 bar
  2. 流通市值 30~300 亿（stock_basic.float_mv，元）
  3. D-1 成交额 ≥ 2 亿 —— **amount 列质量差（11-02 仅 3/5217 有值，历史覆盖 48~84%），必须估算**：amount_est = volume×100×close×latest_price/close_latest（同股 hfq 比值换算真实价；锚点：601019 11-02 估算 ≈2.74 亿 vs 真实 2.68 亿）
  4. D-1 自算 pct = close/prev_close - 1 ≥ +5%（**禁止用 pct_chg 列**，已知有错）
  5. 量比 = D-1 volume ÷ 前 5 日 volume 均值 ≥ 2.0
  6. 收盘位置 = (close-low)/(high-low) ≥ 0.70，且 (high-low)/prev_close ≥ 3%（非一字）
  7. 结构突破（二选一）：a) D-2 收 ≤ MA250 且 D-1 收 > MA250（首次站上年线）；b) D-1 收盘 = 近 60 日收盘最高
  8. 近 20 日累计涨幅 ≤ +30%
  9. 板块效应：所属行业成分股 D-1 平均自算 pct ≥ +1.5%（无行业归属跳过本条不剔除）
  - 得分 = 量比 × 收盘位置 × 板块平均涨幅，取 TopN（默认 10）
- **回测引擎 `run_backtest(days=20, topn=10)`**：取最近 N 个交易日为 T（含 2026-11-02），每个 T：
  - 信号日 = T 的前一交易日（交易日历从数据 date 列推导）
  - 对每个 pick，**T 日开盘价买入**，退出规则（保守）：
    - T 日 low ≤ 买价×0.96 → 止损成交 @ 买价×0.96
    - 否则 T 日 high ≥ 买价×1.06 → 止盈成交 @ 买价×1.06
    - 两者同日都满足 → **保守假设止损先成交**
    - 否则 T 日收盘卖出
  - 收益率 = (卖出价-买价)/买价（hfq 口径比值，不换真实价）
  - T 日无 bar（停牌）→ 记 `untradeable_suspended`；T 日 open=high=low（一字）→ 记 `untradeable_limit`；两者不计入胜率、单列计数
  - **胜率 = 收益率>0 的 pick ÷ 可成交 pick**；同时报 T 日收涨（close>open）占比
- 汇总输出：{days, topn, strategy, total_picks, tradeable, wins, win_rate, avg_ret, t_up_rate, tp_rate, stop_rate, untradeable, daily:[{signal_date, t_date, picks:[…], win_rate, avg_ret}], best, worst}
- 性能要求：全向量化（按 date groupby 预切片），单次 run_backtest ≤ 60 秒

`analyzer/server.py` 新增路由（跟随现有 if/elif 风格与鉴权口径，参照 /api/rank）：
- `GET /api/backtest/run?days=20&topn=10` → run_backtest 结果 JSON；同参数结果做 10 分钟内存缓存；参数 clamp：days 5~60、topn 1~30
- 出错返回现有错误 JSON 格式

### 前端（index.html + app.js，必要时 style.css）
- nav 新增 tab「模拟交易」（位置：个股分析之后、交易记录之前），panel `#tab-simtrade`
- 控制区：策略下拉（v1 仅「超短动量」）、回看交易日数（默认 20）、每日 TopN（默认 10）、「运行回测」按钮
- 结果区：
  - 汇总卡片：样本数/胜率/平均次日收益/T日收涨占比/止盈触发率/止损触发率/无法成交数/最佳/最差
  - ECharts 柱状图：每个 T 的平均次日收益%
  - 逐日明细表（D-1 信号日 | T 日 | picks 数 | 当日胜率 | 平均收益），行可展开当日 picks 明细（代码/名称/T-1 涨幅/量比/信号类型/T 日开盘→收盘/收益率/退出类型）
- 样式沿用现有 rank-table/panel/section-title 风格，不引新依赖

## 输出文件白名单（只许动这些，其他一律不动）
- 新建：analyzer/backtest.py
- 修改：analyzer/server.py、analyzer/web/index.html、analyzer/web/app.js、analyzer/web/style.css（如需）
- **不许动**：datastore.py、auth.py、planwatch/、monitoring/、scans/、downloader/、data/ 任何文件；**不执行任何 git 操作**（提交由主 Agent 做）

## 自检锚点（不符先查自身实现）
1. daily_hfq.parquet 行数 = 16,961,139、codes = 5,223、最后日 = 2026-11-02（该日 5,217 行）
2. **最强锚点**：策略以 asof=2026-11-01（T=2026-11-02）运行，选出的 picks 必须包含 601019.SH（山东出版：11-02 +9.97% 涨停、60 日新高、量比 3.43）——实盘同规则扫描已选中它；若回测不含它，先查自己的实现（多半是 amount 估算或 MA250 窗口口径错）
3. 同一 asof 下 300788.SZ（中信出版，11-02 +20%）也应在 picks 中
4. 601019.SH 的 11-02 close(hfq) = 11.2558、volume = 360132、amount 为 NaN（估算 ≈2.74 亿）
5.改名后 `grep -r "数据分析仪表盘" /root/workspaces/quant-trading/analyzer` 应无输出（除非历史 CHANGELOG 类文件，报告说明）

## 口径声明
- 后复权 hfq 序列内做一切比值/均线计算；跨股绝对价不可比
- pct 一律自算；amount 一律估算（口径见上）
- 交易日历 = 数据集中出现的去重 date 升序
- 单位：volume=手（×100=股）、float_mv=元、latest_price=元（真实价）

## 完成标记
全部完成后最后单独一行打印：BACKTEST_MODULE_DONE
