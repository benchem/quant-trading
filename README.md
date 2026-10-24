# A股交易数据下载器

中国大陆A股（沪深京）日线行情下载工具：从公开行情接口拉取数据，落地为本地
**Parquet** 文件，为量化建模/回测提供干净的输入。

- **数据源**：东方财富（主源，字段全）+ 腾讯行情（备用源，自动降级），与 AkShare 同源
- **复权**：默认**后复权(hfq)**（历史值稳定，适合建模）；支持前复权/不复权
- **增量更新**：每股一个 parquet 文件，日常更新只拉取缺口日期
- **容错**：全局限速 + 指数退避重试 + 失败清单（重跑自动补齐）；限流(403/429/501)
  自动冷却（指数退避，零请求）；可配合 Clash **自动轮换出口节点**（`--rotate`）；
  自动适配对 Python `requests` TLS 指纹过滤的网络环境（内置 curl 等备用引擎）
- **北交所**：东财源覆盖沪深京；腾讯备用源仅覆盖沪深

## 安装

```bash
cd quant-trading
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .                   # 可选：注册 ashare-dl 命令
```

## 快速开始

```bash
# 1) 首次全量下载（列表 + 日历 + 全部A股日线 + 主要指数），约 10~120 分钟
python -m downloader all

# 2) 合并为单个建模数据集
python -m downloader build

# 3) 查看状态
python -m downloader status

# 4) 跑一个分析示例（读取 build 产物，输出统计与图表）
python examples/quick_analysis.py
```

之后每天（交易日盘后）只需：

```bash
python -m downloader all     # 增量更新
python -m downloader build   # 重建合并数据集
```

## 命令

```
python -m downloader <命令> [选项]
```

| 命令 | 说明 |
|---|---|
| `stocks` | 下载/更新A股日线 |
| `index`  | 下载/更新主要指数日线（上证/深成/创业板/沪深300/中证500/中证1000/科创50） |
| `list`   | 刷新全部A股基础列表（代码/名称/市值/最新价） |
| `calendar` | 刷新交易日历 |
| `build`  | 合并全部日线 → 单个建模数据集 |
| `status` | 查看本地数据状态 |
| `all`    | 一键更新：list + calendar + stocks + index |

常用选项（`stocks`/`all` 等）：

| 选项 | 默认 | 说明 |
|---|---|---|
| `--start` | `19901219` | 历史起始日期 YYYYMMDD |
| `--end` | 今天 | 结束日期 |
| `--adjust` | `hfq` | 复权方式：`hfq` 后复权 / `qfq` 前复权 / `none` 不复权（不同复权方式数据分目录存放） |
| `--codes` | 全部 | 只下载指定代码，逗号分隔：`--codes 600519,000858,300750`（支持 `600519` / `sh600519` / `600519.SH` 写法） |
| `--market` | `all` | 限定市场：`sh` / `sz` / `bj` |
| `--full` | 关 | 忽略本地数据，从起始日期整段重下 |
| `--limit` | 无 | 只处理前 N 只（调试） |
| `--workers` | 4 | 并发线程数 |
| `--rate` | 6 | 对数据源的全局请求速率上限（次/秒），0=不限速 |
| `--data-dir` | `data` | 数据落盘目录 |

示例：

```bash
python -m downloader stocks --codes 600519,000858        # 只下两只
python -m downloader stocks --start 20180101 --adjust qfq # 2018年起、前复权（独立目录）
python -m downloader index --indices 000001.SH,399001.SZ  # 指定指数
python -m downloader stocks --workers 8 --rate 10         # 提速（接口稳定时）
```

### 股票列表口径（重要）

`list` 命令的股票列表来自腾讯 `aStock` 榜单，**不含科创板（688/689）**。2026-10 已用
baostock 全市场快照 + qt.gtimg 实时行情补齐了 616 只科创板，`stock_list.parquet`
现为 **5223 只 = 沪深主板 + 创业板 + 科创板**。注意：

- **北交所（8xx/4xx/920xxx）不在范围内**：腾讯榜单源不含、baostock 也不支持
  （代码无法识别 sh/sz），需等腾讯/东财恢复后另行处理；
- `list` 刷新时会以腾讯榜单为准重建，可能再次丢失科创板——科创板行建议保留
  （其代码不在腾讯榜单中，`drop_duplicates` 不会误删，但整表重建会丢）。

### 限流应对：冷却 + baostock 兜底 + Clash 节点轮换

数据源对高频/数据中心 IP 会返回 `403/429/501`。工具的降级链（2026-10 起）：

1. **主源互备**：东财连续失败 5 次 → 本次运行切腾讯主源；腾讯模式下每 100 次请求
   回探东财，恢复后切回；
2. **baostock 兜底源**（2026-10 新增，`baostock` 包，自有服务器、不走 Clash）：
   腾讯/东财都不可用或被限流时自动降级。背景：2026-10-24 起腾讯 `fqkline` 复权
   K线 API 被 WAF 全量封锁（所有节点/家庭出口均 501）、东财全网停服，baostock
   是当前唯一可用的全历史日线源。注意两点：
   - 仅支持沪深（北交所不支持，会直接记失败）；
   - 后复权因子与腾讯 hfq 有 ~1% 供应商差异（除权舍入口径不同），同一股票跨源
     的 hfq 绝对价位略有出入，收益率序列基本一致。腾讯 fqkline 恢复后重跑
     `stocks`（`--full` 可整段重下）即可统一口径；
3. **限流冷却**：主源限流且 baostock 也不可用时进入冷却（60s→120s→…→15min 指数
   退避），冷却期内**零请求**快速失败，避免加重惩罚；
4. **Clash 节点轮换（可选）**：加 `--rotate` 后，限流时先自动切换 Clash 出口节点
   换 IP（并验证出口 IP 确实变化），一轮轮换用完才进入冷却。

```bash
python -m downloader proxy                 # 查看当前节点/出口 IP
python -m downloader proxy --list          # 列出可选节点
python -m downloader proxy --set 日本Z01    # 手动切换节点（子串匹配）
python -m downloader stocks --rotate       # 下载，限流时自动轮换节点
```

Clash API 自动发现：clash-verge 服务模式的 unix socket、`127.0.0.1:9097/9090` 等
external-controller；也可用环境变量指定：`CLASH_API_UNIX=<socket路径>`、
`CLASH_API=127.0.0.1:9097[:secret]`、`CLASH_GROUP=分组名`（默认自动选选项最多的
Selector 分组）。

## 目录结构

```
data/
├── stock_list.parquet              # 全部A股基础信息（code/name/市值/最新价…）
├── trade_calendar.parquet          # 交易日历
├── daily/
│   ├── hfq/600000.SH.parquet       # 个股日线，每股一个文件（增量追加）
│   ├── qfq/...                     # 前复权数据（独立目录，互不干扰）
│   └── none/...
├── fundamental/600519.SH.parquet   # 季度/年报基本面（analyzer fundamental 抓取，可选）
├── news/600519.SH.json             # 个股新闻缓存（东方财富，30 分钟 TTL，可选）
├── watchlist.json                  # 自选股列表（网页 ★ 加自选自动写入，可选）
├── trades.json                     # 自选股买卖流水（网页「记录买卖」登记，可选）
├── minute/1/600519.SH.parquet      # 自选股 1 分钟 K 线（新浪，真实价；后台自动同步，可选）
├── index/000001.SH.parquet         # 指数日线
├── dataset/                        # build 产物（建模直接用）
│   ├── daily_hfq.parquet           # 全市场合并长表
│   ├── index_daily.parquet         # 指数合并
│   └── stock_basic.parquet         # 股票基础信息
└── failed_stocks_hfq.txt           # 本次运行失败清单（如有）
planwatch/                          # 投资计划盯盘器（独立命令行工具）
plan_300687.json                    # 机器可读的投资计划（关键位/批次/止盈/时间规则）
watchlog/                           # 盯盘运行时产物（持仓/流水/提醒/日报，已 gitignore）
analyzer/                           # 股票分析工具（交互式仪表盘 + 静态报告）
analyzer/output/                    # report 模式 PNG 输出（已 gitignore）
stock_analysis/                     # 插件式个股分析框架（多人并行开发分析模块）
```

## 数据字典（日线字段）

| 字段 | 含义 | 单位/说明 |
|---|---|---|
| `date` | 交易日 | 日期 |
| `code` | 证券代码 | Wind 风格：`600000.SH` / `000001.SZ` / `830799.BJ` |
| `name` | 简称 | 仅合并数据集里有（基础信息表按代码关联） |
| `open`/`high`/`low`/`close` | 开/高/低/收 | 元（**复权后**价格，见下） |
| `volume` | 成交量 | **手**（1手=100股） |
| `amount` | 成交额 | 元 |
| `amplitude` | 振幅 | % |
| `pct_chg` | 涨跌幅 | % |
| `change` | 涨跌额 | 元 |
| `turnover` | 换手率 | % |

> 停牌日不产生行（数据源只返回有行情的交易日）。
> 备用源（腾讯）拉取的数据没有 `amount`/`turnover`/`amplitude`/`pct_chg`/`change`
> 这几个字段（值为 NaN），`close` 等核心字段齐全；收益可直接用 `close.pct_change()` 计算。

**分钟线**（`data/minute/1/{code}.parquet`，仅自选股）：`datetime`（分钟时间戳）、
`code`、`open`/`high`/`low`/`close`（元，**不复权真实价**，与日线复权价不同）、
`volume`（**股**，注意与日线的"手"不同）、`amount`（元）。
新浪源单次返回最近约 1970 根（≈8 个交易日），后台每 30 分钟增量合并，本地历史
会随运行时间累积成更长序列。

### 复权方式怎么选

| 方式 | 特性 | 适用 |
|---|---|---|
| **hfq 后复权（默认）** | 上市以来历史值**永不改变**，新数据向后追加；价格数值≠实际成交价 | **建模/回测推荐**：总收益口径稳定、可复现 |
| `qfq` 前复权 | 最新价=实际价；但每次分红除权后**全部历史被重写**，所以本工具每次运行都会整段重下 | 看盘/图表直观 |
| `none` 不复权 | 实际成交价，跨除权日有跳空 | 需要真实价格/计算除权事件研究 |

> **hfq 口径备注**：不同数据源的后复权因子有 ~1% 级差异（除权除息舍入口径不同）。
> 2026-10 起腾讯 fqkline 被 WAF 封锁期间的数据由 baostock 兜底下载，与腾讯 hfq
> 的绝对价位略有出入（收益率序列基本一致）。腾讯源恢复后 `--full` 重下即可统一。

## 增量更新机制

- 每只股票读取本地 parquet 的最大日期，只拉取 `最大日期+1 ~ 结束日期` 的缺口，
  按 `(code, date)` 去重合并写回；
- 重跑命令是幂等的：已是最新的股票 0 请求跳过；失败的股票记录在
  `data/failed_*_<adjust>.txt`，重跑同一命令自动补齐；
- `qfq` 数据例外：因历史会被分红重写，每次运行整段重下（见上表）。

## planwatch 投资计划盯盘器

独立命令行工具，围绕一份机器可读的投资计划（`plan_300687.json`，由
`trading_plan_300687.md` 派生）做三件事：**实时盯盘提醒、记录买卖、调整计划**。
行情源为腾讯实时接口（`qt.gtimg.cn`，走 Clash 代理，失败自动直连兜底）。

```bash
# 实时盯盘：交易时段轮询（默认 5s），价格/时间/T 信号触发提醒，收盘自动写日报
python -m planwatch monitor            # 后台跑: nohup ... &
python -m planwatch monitor --interval 3 --no-notify   # 高频 / 不弹系统通知

# 一次性快照：行情 + 持仓 + 关键位 + 盈亏（随时可看）
python -m planwatch status

# 记录一笔买卖（自动更新持仓/现金/已实现盈亏，并做计划合规检查）
python -m planwatch record --side buy  --shares 1200 --price 29.0 --batch T1 --note "场景B"
python -m planwatch record --side sell --shares 500  --price 36.5 --batch T1 --note "tp1减仓"
python -m planwatch record --side buy  --shares 300 --price 28.0 --batch T --dry-run  # 试算不落盘

# 查看 / 调整计划（每次调整写入审计日志 watchlog/plan_audit.jsonl）
python -m planwatch plan show
python -m planwatch plan set hard_stop 22.5 --reason "结构下移"
python -m planwatch plan log "11月看算力新合同"
python -m planwatch plan mark tp1 --reason "已减500股"   # 标记止盈完成，不再提醒

# 生成今日盯盘日报（markdown → watchlog/2026-10-23.md）
python -m planwatch report
```

**提醒分三类**（`monitor` 运行时触发，macOS 系统通知 + 本窗口 + `watchlog/alerts.jsonl`）：

- **价格位**：T1/T2/T3 入场窗口、软止损、硬止损（含 23.5 警戒）、三级止盈——当日去重，
  条件消失并持续若干 tick 后才允许重报（防抖动）；
- **时间钩子**：09:20 盘前检查清单、14:38「T 强制了结倒计时」、14:57 收盘确认，
  以及 `time_rules` 里的检查点（如 2026-11-20 收盘 <31.3 → 新仓减半）；
- **T 信号**：自低点反弹 ≥4% 且贴近压力位 → 倒T 候选；自高点回落 ≥3% 且贴近支撑位 → 正T 候选。

**持仓/台账**：`watchlog/state.json`（底仓+新仓批次+现金+已完成止盈）、
`watchlog/trades.jsonl`（逐笔流水）。`record` 会做**计划合规检查**（批次是否存在、
是否已成交过、价格是否落在批次区间、现金是否为负），提示但不阻止——留给你最终决策。
T 批次（`--batch T`）只动现金不动仓位，并跟踪「今日 T 未对冲股数」。

**注意**：`monitor` 依赖 `data/trade_calendar.parquet` 判断交易日（缺失时退化为周一~周五）；
提醒只在交易时段的轮询中产生，`status`/`record`/`report` 任意时刻可用。

## analyzer 股票分析工具

交互式可视化 + 统计分析，读取 `build` 产物（零额外依赖，ECharts 已内嵌可离线）。

```bash
# 交互式仪表盘（默认端口 8765，浏览器打开 http://127.0.0.1:8765/）
python -m analyzer                  # 等价于: python -m analyzer serve
python -m analyzer serve --port 9000

# 静态报告（matplotlib PNG，输出到 analyzer/output/）
python -m analyzer report --years 10

# 抓取季度/年报基本面（同花顺，缓存到 data/fundamental/）
python -m analyzer fundamental --codes 600519,000001   # 指定股票
python -m analyzer fundamental --all --rate 2          # 全市场批量预取（约 45~60 分钟，断点可重跑）
python -m analyzer minute                              # 同步全部自选股分钟线（--codes 600519 可指定）
```

**仪表盘三个标签页**（深色主题，红涨绿跌）：

- **市场概览**：指数对比（7 大指数归一化对数轴）、市场宽度（涨跌家数）、
  全市场日成交额、等权全市场累计净值 vs 沪深300；区间可选 1/3/5/10 年/全部；
- **个股分析**：代码/名称模糊搜索（含联想），K线+MA5/20/60/120+成交量，
  **点击任意日K线查看当日分时**（弹窗：1 分钟K线价格/均价 VWAP/分钟量双图、
  昨收基准线与当日摘要卡；读本地分钟数据（自选股自动后台同步），无数据时可
  「立即拉取」在线抓取最近约 8 个交易日），
  区间统计（涨跌幅/年化收益/年化波动/最大回撤/夏普）；**统计区间切换同步缩放下方
  图表**（K线/走势对比/模型图统一缩放到所选时间范围；模型图数据窗口固定为最近
  250 日，更长区间显示全部 250 日）；K 线下方为**模型子标签页**
  （走势对比 / 多因子突破 / GARCH 回归 / 筹码分布 / 布林通道 / 财报），今日触发
  信号的模型标签上亮红/绿圆点（看多/看空），一眼扫出哪个模型在发信号；
  **多因子复合突破**（子标签页「多因子突破」：VAM/VPD/量比/波动压缩四因子综合 z 分、三道硬门
  通过状态（突破/放量≥1.8×/波动压缩/共振）、价格+突破线+信号星标复合图、
  量比与 ATR% 副图、历史信号明细表与回测胜率，来自 `multi_factor_breakout` 插件）；
  **GARCH 波动均值回归**（子标签页「GARCH 回归」：dev_z 偏离指标/市场状态 regime/次日σ̂/
  次日区间高低点预测/区间历史命中率/回测胜率/回归半衰期八张指标卡，四道状态门
  （偏离达阈值/震荡市/非黑天鹅/回归幅度足够），价格+MA60 均衡线+95%区间包络+
  超卖★/超买▼信号标复合图、dev_z 副图（±2 阈值线）、σ̂% 波动聚集副图、
  历史信号明细表，来自 `garch_mean_reversion` 插件）；
  **筹码分布与成本均线**（子标签页「筹码分布」：模型评分/获利比例/成本均线/70%集中度/
  支撑阻力(P15/P85)/换手率/回测胜率/支撑阻力质量八张指标卡，四道信号条件门
  （S1 获利≤10% / S2 获利≥90% / S3 集中建仓 / 非超高换手），价格×成本均线×
  P15/P85 支撑阻力带+信号标复合图、获利比例副图（10/90 阈值线）、集中度副图、
  历史信号明细表（独立事件口径），来自 `chip_distribution` 插件）；
  **布林带通道与相对位置（%b）**（子标签页「布林通道」：模型评分/%b 相对位置/中轨 MA20/
  上下轨±2σ/带宽 120 日分位/通道校准/回测胜率/挤压扩张检验八张指标卡，四道
  信号条件门（S1 %b≤0 下沿买点 / S2 %b≥1 上沿卖点 / S3 挤压+破轨突破形态 /
  非贴轨运行防强趋势提前离场），价格×中轨×上下轨+信号标复合图、%b 副图
  （0/0.5/1 阈值线）、带宽% 副图（挤压态紫点）、历史信号明细表（独立事件口径），
  来自 `bollinger_channel` 插件）；
  **筹码分布成本图**（「筹码分布」子标签页内：换手衰减法，红=获利盘/蓝=套牢盘，含获利比例、90%成本区间、集中度）；
  **季度/年报**（子标签页「财报」：营收/净利润/同比/EPS/ROE/毛利率/净利率/资产负债率，季度+年度视图，
  首次打开在线抓取约 1~2s 后走本地缓存）；**自选股**（个股页一键 ★ 加自选，
  切到个股标签页自动展示自选列表，点行跳转，持久化到 `data/watchlist.json`；
  列表内直接展示每只股票**最新一期财报摘要**（报告期/营收/营收同比/净利/净利同比/ROE，
  读本地缓存；无缓存时行内「抓取财报」一键拉取），点行内「📰 消息」展开该股票
  **最新新闻流**（东方财富个股新闻，标题可点原文，`data/news/` 30 分钟缓存，
  网络失败回退旧缓存，支持强制刷新））；**自选股分钟线后台同步**（加自选即在后台
  下载该股 1 分钟 K 线，新浪源、真实价口径，存 `data/minute/1/{code}.parquet`；
  服务启动时自动补齐已有自选、运行期每 30 分钟增量刷新，本地历史随时间累积；
  自选列表「分钟线」列实时显示 本地根数 · 最新时间，同步中显示「同步中…」；
  手动同步：`python -m analyzer minute`）；**买卖记录**（独立「交易记录」页维护
  买卖台账：「➕ 新增记录」登记 股票(代码/名称联想)/日期/方向/成交价/股数/费用/备注，
  「📋 批量录入」粘贴多行文本（日期 方向 价格 股数 [费用] [备注]，空格/逗号/Tab 分隔，
  支持 买/卖/buy/sell、YYYY-MM-DD 等多种日期写法，# 注释与表头自动跳过）先预览后导入，
  单条失败不影响其余；
  流水表支持逐笔 编辑✎/删除✕，可按 全部/仅自选 过滤、按代码或名称搜索；
  持仓汇总表按股票聚合，并列展示两种成本口径——成本价（移动加权平均：卖出
  只记已实现盈亏）与 摊薄成本 = (累计买入金额+买入费用−累计卖出金额+卖出费用)÷持仓
  （多数股票 APP 的口径），另有 最新价/笔数/持仓/总费用/已实现盈亏/浮动盈亏，
  点行跳转个股；费用默认按费率模型逐笔估算（佣金‱与每笔最低元可在页面调整并
  记忆，印花税 0.05% 仅卖出、过户费 0.001%），也可填券商成交单的实际费用覆盖
  估算（流水表 ≈ 前缀为估算值）；个股页 K 线面板「管理交易 →」跳本页并预填该股，
  K 线上以 ▲红=买、▼绿=卖 标在成交价处，tooltip 显示当日每笔明细，下方为只读
  流水表；自选列表同步显示「持仓」「成本·浮盈」两列，持久化到 `data/trades.json`
  （API：`GET/POST /api/trades`，action = add/add_batch/update/remove，可选 fee =
  该笔实际费用；add_batch 传 trades 数组，单条失败逐条报错不阻断）；非交易日登记自动
  吸附到之前最近交易日，早于该股首个数据日的记录仅在流水表展示、不画标记）；
  支持 `#stock` / `#rank` / `#trades` 深链接；
- **排行榜**：任意区间 × 区间涨跌幅/年化波动/最大回撤/平均换手 的 Top/Bottom N，
  点击行跳转个股。

**数据口径说明**：

- 全部基于后复权(hfq)日线；等权统计的日收益先做 ±100% 截断，规避 hfq 因子
  跳变与新股首日极端值（全市场约 718 个异常日）对均值/净值的污染；
- 排行榜的波动率同样截断到 ±100%：部分老股 hfq 价格被多年分红因子缩到 <0.01 元，
  最小报价单位（1 分钱）的相对跳变可达数百%，不截断会污染波动率排名；
- 个股数据覆盖不一致（部分股票仅近期数据，见 `data/daily/hfq/`），仪表盘按实际
  可用区间展示，"全部数据"统计以该股票首个数据日为起点；
- **财报数据**来自同花顺财务摘要（akshare），口径为累计值（Q1/Q2(中报)/Q3/年报为
  年初至报告期末累计），"同比"为同口径同比增长率；金额单位自适应（亿/万亿），
  比率以小数存储；有缓存后不再自动重抓，`analyzer fundamental --codes X --force`
  可强制重抓（财报季结束后建议 --force 刷新一次）；
- **analyzer JSON API 口径**：所有比率（涨跌幅/同比/ROE 等）统一为小数
  （0.0125 = 1.25%），与 parquet 原始数据的百分数口径（1.25 = 1.25%）不同，
  网页端已统一换算；腾讯源缺失的 `pct_chg` 会按收盘价自动补算；
- **价格双口径显示**：主显示为**前复权（真实价单位）**——由后复权序列按
  `最新真实价/后复权最新收盘` 折算，最新价=快照真实价，历史走势形状与后复权
  完全一致；个股头部 / K线 / 筹码 / 自选列表 / 搜索联想均以此为单位，并在头部、
  自选列表、搜索联想并列展示后复权价作参考；涨跌幅、收益、回撤、获利盘等
  比率不受单位影响；
- **筹码分布**基于换手衰减（与通达信/同花顺筹码指标同源思路），
  换手率缺失的股票按 成交量/当前流通股本 估算（历史近似，页面有提示）。

## stock_analysis 插件式个股分析框架

对个股做分析模块的**插件化架构**：设计目标是让多个程序员并行开发、互不影响。
**加一个分析模块 = 在 `stock_analysis/plugins/` 下加一个 .py 文件**，不改任何
共享代码（`plugins/__init__.py` 永远为空，自动发现）。插件只依赖稳定的契约
（`core/base.py`）和数据上下文（`AnalysisContext`），不碰 DataStore、不发网络请求、
不互相 import；单插件异常/加载失败被隔离，不影响其他插件和进程。

```bash
python -m stock_analysis list                          # 列出全部插件
python -m stock_analysis show momentum                 # 查看插件参数声明
python -m stock_analysis run momentum --code 600519.SH # 真实数据运行
python -m stock_analysis run momentum risk_profile --code 600519.SH \
    --set window=30 --set index=000905.SH --json       # 多插件 + 传参 + JSON
python -m stock_analysis run momentum --code 600001.SH --demo   # 合成数据演示（无需数据）

.venv/bin/python tests/test_plugins.py                 # 框架自测（含真实数据冒烟）
```

内置示例插件：`momentum`（动量/趋势，技术面）、`risk_profile`（波动/回撤/beta，
风险面）、`fundamental_quality`（ROE/成长/杠杆，基本面，本地财报缓存）、
`multi_factor_breakout`（**多因子复合突破**，技术面：VAM 波动率调整动量 + VPD
量价背离 + 量比 + 波动压缩四因子滚动 z-score 加权合成，价格突破 N 日高点时以
"放量≥1.8 倍 + ATR% 低位压缩 + 多因子共振"三道硬门过滤假突破，输出当日信号
拆解与历史回测胜率，单测 `tests/test_multi_factor_breakout.py`）、
`garch_mean_reversion`（**ARMA-GARCH 波动均值回归**，数理统计：ARMA(2,1)
条件均值 + GARCH(1,1) 波动聚集（纯 numpy 实现 Hannan-Rissanen / 网格+NM 极大
似然，零新增依赖），偏离指标 dev_z = ln(P/MA60)/(σ̂·√H) 触发超卖/超买回归信号，
regime 检测（强趋势 2.5σ / 黑天鹅单日 6σ / 回归半衰期门）针对"均值回归失效"
弱点自动降权，次日区间高低点预测（high/low 包络，历史命中率实测约 95%），
历史信号 5/10/20 日方向胜率回测（实测 12 只代表股合计 win5≈55%、win20≈60%，
n>1000，诚实输出不调参），单测 `tests/test_garch_mean_reversion.py`）、
`chip_distribution`（**筹码分布与成本均线**，资金筹码微观结构：换手衰减法逐日
构建筹码成本结构，获利比例/平均持仓成本（成本均线）/70%·90%筹码集中度/支撑
(P15)/阻力(P85)；信号 S1 超跌支撑（获利比例≤10% 卖压衰竭）/ S2 获利抛压
（≥90% 兑现压力）/ S3 集中建仓（conc70 二十日改善≥15% 且价>成本），超高换手
（单日≥10%）成本时滞 regime 自动压制信号；支撑/阻力位质量双口径回测（10日
包络击穿率 + 触及后2日收回率，均对照等宽基线），历史信号方向胜率按日/独立
事件双口径。实测 12 只代表股（默认参数、不调参）：独立事件 10 日胜率约 50%，
其中 S1 超跌支撑 55%（64/116）、S3 建仓 52%、S2 抛压 46%；P15/P85 位质量
与朴素等宽基线相当或略弱——"关键支撑阻力位精确预测"弱于宣称，模型价值主要
在全市场盈亏状态分类与超跌反弹信号，单测 `tests/test_chip_distribution.py`）、
`bollinger_channel`（**布林带通道与相对位置（%b）**，波动率与通道：MA20 ± 2σ
（滚动标准差 ddof=0）构建通道，%b=(close-下轨)/(上轨-下轨) 相对位置因子、带宽
与 120 日分位；信号 S1 区间下沿（%b≤0 震荡买点）/ S2 区间上沿（%b≥1 震荡卖点）/
S3 挤压突破（近 10 日带宽处 120 日最低 10% 分位后破轨，S3 优先），贴轨运行
（连续 5 日收盘在轨外）regime 自动压制区间信号——针对"强单边趋势贴轨导致
提前离场"的已知弱点；通道校准双口径（次日收盘通道内比例 + 10 日包络，对照
以现价为心的等宽基线）、挤压扩张检验（S3 后 10 日 |收益| vs 全样本）、历史信号
方向胜率按日/独立事件/无贴轨对照三口径。实测 12 只代表股（默认 K=2.0、
不调参）：独立事件 10 日胜率 51.9%（宣称 50%~55% 区间下沿达成），分类型
S1 下沿 65%（52/80）、挤压向下 55%、S2 上沿 46%、挤压向上 40%（A 股近年
下行突破更可靠的不对称）；次日通道内比例均值 81.6%（经典"≈95%"宣称在
A 股不成立，跳空/厚尾所致），10 日包络 25.4% 弱于等宽基线 39.7%（MA 滞后）；
挤压后 10 日 |收益| 3.6% vs 全样本 4.7%——"挤压后波动扩张"未获实测支持。
创业板 K 敏感性（300750/300687，K 2.0→2.2）：事件数 -15%、hit1 82%→86%
（通道校准改善）、10 日事件胜率 58.3→55.0 / 53.8→52.2 略降——宣称的
"高波动板块上调 K 减少虚假触发"在事件数与校准上成立、胜率上不成立，
单测 `tests/test_bollinger_channel.py`）。

批量选股扫描示例（今日触发信号 / 突破待确认 / 股票池回测胜率三张表）：

```bash
python examples/multi_factor_screen.py                 # 用 data/watchlist.json
python examples/multi_factor_screen.py 600519 300750   # 指定代码
python examples/multi_factor_screen.py --random 200    # 全市场随机抽 200 只
```

仪表盘已接入（`python -m analyzer` 启动后）：

```
GET /api/plugins                              # 插件清单（含参数声明/加载失败清单）
GET /api/plugins/momentum                     # 单插件元数据
GET /api/analysis?plugin=momentum&code=600519.SH&window=30   # 运行并返回结果 JSON
```

**插件热发现**：服务器运行期间向 `stock_analysis/plugins/` 新增/删除 .py 插件文件，
下一次 API 请求即自动生效（无需重启）；修改已有插件文件的内容仍需重启进程
（Python import 缓存）。前端静态资源（HTML/JS/CSS）每次请求从磁盘直读，
浏览器刷新即可拿到最新版。

新成员请先读 **`stock_analysis/plugins/DEVELOPING.md`**（含插件模板、单测模板、
并行开发五条铁律）；架构说明见 `stock_analysis/README.md`。

## 建模使用

`build` 之后，`data/dataset/daily_hfq.parquet` 即是一张长表
（code × date 一行），pandas/polars/duckdb 直接读：

```python
import pandas as pd

df = pd.read_parquet("data/dataset/daily_hfq.parquet")
df = df.sort_values(["code", "date"])

# 个股日收益（后复权 close 的 pct_change ≈ 含分红再投资的日总收益）
df["ret"] = df.groupby("code")["close"].pct_change()

# 截面：每个交易日的全市场等权收益
mkt = df.groupby("date")["ret"].mean()

# 某只股票近一年的波动率
one = df[df["code"] == "600519.SH"].tail(244)
vol_annual = one["ret"].std() * 244 ** 0.5
```

duckdb 一行聚合（适合快速探索）：

```bash
duckdb -c "SELECT code, count(*), min(date), max(date) FROM 'data/dataset/daily_hfq.parquet' GROUP BY code ORDER BY 3 DESC LIMIT 10"
```

## 定时更新（cron 示例）

```cron
# 每个交易日 17:30 增量更新并重建数据集
30 17 * * 1-5  cd /path/to/quant-trading && .venv/bin/python -m downloader all && .venv/bin/python -m downloader build >> data/cron.log 2>&1
```

## 常见问题

- **大量请求失败 / `failed_*.txt` 很长**：行情接口对高频访问有 IP 级惩罚，
  几分钟到几小时自动恢复。降低 `--rate`/`--workers` 等待后**重跑同一命令**即可
  （增量机制保证已下好的部分不重复）。
- **`没有任何可用的 HTTP 引擎`**：本环境对 Python `requests` 的 TLS 指纹做了过滤，
  工具会自动尝试 `curl_cffi`/`tls_client`/系统 `curl` 引擎；确认机器上有 `curl` 命令即可。
- **北交所股票缺失**：备用源（腾讯）不覆盖北交所；东财恢复后重跑 `list`+`stocks` 补齐。
- **退市股票**：列表只含当前上市股票，退市股的历史不覆盖（数据源限制）。
- **接口字段/行为变化**：数据来自东财/腾讯公开行情接口，若某天大面积异常，
  先用 `curl` 手动访问 `push2his.eastmoney.com` / `web.ifzq.gtimg.cn` 判断是网络还是接口变更。

## 免责声明

数据来自公开行情接口，仅供个人研究学习，可能存在延迟、错误或缺失；
不构成任何投资建议。请遵守相关网站的服务条款，控制访问频率。
