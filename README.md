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
| `board`  | 刷新行业板块成分表（东财行业板块；东财不可用时降级 baostock 证监会行业分类；`--with-concept` 追加概念板块，仅东财源；`data/board_members.parquet`） |
| `build`  | 合并全部日线 → 单个建模数据集 |
| `status` | 查看本地数据状态 |
| `all`    | 一键更新：list + calendar + board + stocks + index（`--skip-board` 可跳过板块） |

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
├── board_members.parquet           # 板块成分表（code→board_type/board_code/board_name；行业=划分每股一行，概念=重叠每股多行，downloader board 生成，可选）
├── trade_calendar.parquet          # 交易日历
├── daily/
│   ├── hfq/600000.SH.parquet       # 个股日线，每股一个文件（增量追加）
│   ├── qfq/...                     # 前复权数据（独立目录，互不干扰）
│   └── none/...
├── fundamental/600519.SH.parquet   # 季度/年报基本面（analyzer fundamental 抓取，可选）
├── news/600519.SH.json             # 个股新闻缓存（东方财富，30 分钟 TTL，可选）
├── news/us_market/2026-10-26.md    # 每日美股简报（外部/AI 生成，overseas 校验用，可选）
├── news/us_market/.push_state.json # 美股简报邮件推送状态（防重启重发，可选）
├── valuation/600519.SH.json        # 个股估值快照缓存（东方财富估值分析，30 分钟 TTL，可选）
├── flow/600519.SH.json             # 个股资金流缓存（东财：主力/超大单净流入，近120日，30 分钟 TTL，可选）
├── announce/600519.SH.json         # 个股公告缓存（巨潮资讯标题+链接，近180日，30 分钟 TTL，可选）
├── sentiment/600519.SH.json        # 个股情绪缓存（东财千股千评：评分历史+当日快照，可选）
├── watchlist.json                  # 自选股列表（免登录共享模式用；登录模式下首用户注册时迁入 data/users/，可选）
├── trades.json                     # 自选股买卖流水（同上，可选）
├── users/                          # 多用户登录（默认启用）：每用户数据隔离，可选
│   ├── users.json                  # 用户表（用户名 -> scrypt 密码哈希/创建时间）
│   ├── sessions.json               # 活跃会话（token sha256 -> 用户/过期时间，30 天）
│   └── alice/                      # 每用户一个目录
│       ├── watchlist.json          # 该用户自选股
│       ├── trades.json             # 该用户买卖流水
│       ├── notifications.jsonl     # 该用户站内信
│       └── journal/                # 该用户交易日记（记忆与复盘，按用户隔离，可选）
│           ├── journal_events.jsonl   # 事件流：trade/model/system 三类，append-only
│           ├── journal_notes.jsonl    # 用户笔记（观察/计划/感想，可编辑删除）
│           └── journal_reviews.jsonl  # 每日复盘（盘后自动规则版 + 手动 LLM 版）
├── minute/1/600519.SH.parquet      # 自选股 1 分钟 K 线（新浪，真实价；后台自动同步全用户自选并集，可选）
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
 analyzer/agent/                     # 智能体层（进化区）：prompt/LLM 调用/上下文组装，
                                     #   见「软件架构与分层契约」；换模型/改措辞只动这里
stock_analysis/                     # 插件式个股分析框架（多人并行开发分析模块）
```

## 软件架构与分层契约

代码分三层，依赖**只能向下**，由 `tests/test_architecture.py` 静态扫描
（AST import 分析）机器强制，违反即测试失败：

```
数据面        datastore / news / valuation / flow / announce / sentiment / overseas
              （行情·资讯抓取与缓存，TTL/降级/原子写）
   ↑ 被依赖
模型/规则面   stock_analysis（量化插件）/ journal（记忆与复盘规则）/ updates（EOD 流程）
              / report / notify —— 纯确定性逻辑，零 LLM、零"进化"
   ↑ 被依赖
智能体面      analyzer/agent/ —— 唯一允许碰 LLM 的包（进化区）
              llm.py 传输配置 · prompts.py prompt 文本+版本 · context.py 组装策略
              analysis.py 报告入口+缓存 · review.py LLM 复盘
```

- **隔离的意义**：智能体能力（换模型、改 prompt、加数据源进上下文）频繁进化；
  规则/模型面必须稳定。LLM 升级**不改任何规则代码**——规则版复盘在 LLM 不可用
  时照常工作，journal 不 import agent 包；
- **接线点**：`analyzer/server.py` 是组合根（唯一同时引用两层的模块）；
  `analyzer/ai.py` 为旧 import 路径保留的兼容壳（re-export agent 包），新代码
  请直接 `from analyzer.agent import …`；
- **新增 LLM 能力**：模板加 `agent/prompts.py`（递增 `*_PROMPT_VERSION` 使缓存
  失效），调用逻辑进 agent 包内对应文件；**不要**在规则面 import agent；
- **新增数据源进 AI 上下文**：数据面加模块（仿 flow.py），策略在
  `agent/context.py` 里接线——prompt 与传输层不动。

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
python -m analyzer serve --no-auth  # 免登录共享模式（旧行为，单人本地用）

# 多用户管理（登录模式下）
python -m analyzer user list                    # 列出用户
python -m analyzer user add bob                 # 新建用户（交互设置密码）
python -m analyzer user passwd bob              # 重置密码（忘记密码时）

# 静态报告（matplotlib PNG，输出到 analyzer/output/）
python -m analyzer report --years 10

# 抓取季度/年报基本面（同花顺，缓存到 data/fundamental/）
python -m analyzer fundamental --codes 600519,000001   # 指定股票
python -m analyzer fundamental --all --rate 2          # 全市场批量预取（约 45~60 分钟，断点可重跑）
python -m analyzer minute                              # 同步全部自选股分钟线（--codes 600519 可指定）
```

### 多用户登录与数据隔离

仪表盘**默认启用多用户登录**（`analyzer/server.py` + `analyzer/auth.py`，纯标准库、
无额外依赖）：打开页面先看到登录/注册浮层，登录后才能使用。不同用户之间的
**自选股、买卖交易流水、站内信（消息中心）完全隔离**，各存各的目录，互不可见；
行情/K线/排行等公共市场数据仍共享（只读，无需隔离）。

- **登录/注册**：首个访问者「注册新用户」即创建账户（密码 scrypt 加盐哈希，明文不落盘）；
  之后可用「登录」进入，右上角可切换用户（退出后重登）。会话 cookie 有效期 30 天，
  服务端持久化（重启不掉登录态）。
- **平滑迁移**：**首个注册的用户自动继承**升级前根级的 `watchlist.json` / `trades.json` /
  `notifications.jsonl`（迁移到其用户目录），保证单人使用→多人使用数据不丢；
  其后注册的用户从空白开始。
- **数据布局**（每用户独立目录）：

  ```
  data/users/{用户名}/
  ├── watchlist.json            # 该用户自选股
  ├── trades.json               # 该用户买卖流水
  └── notifications.jsonl       # 该用户站内信
  data/users/users.json         # 用户表（用户名 -> 密码哈希/创建时间）
  data/users/sessions.json      # 活跃会话（token 的 sha256 -> 用户/过期时间）
  ```

- **消息分发规则**：盘后更新产生的**模型信号只推送给「自选股包含该股票」的用户**
  （谁的自选触发了信号谁收到）；系统进度类消息广播给全部用户。
- **后台任务视角**：分钟线后台同步与盘后更新扫描的是**全部用户自选股的并集**
  （去重），因此任一用户加自选都会触发该股分钟线/日线更新，共享数据缓存不重复下载。
- **免登录共享模式**：`python -m analyzer serve --no-auth` 关闭登录，回到旧的
  单用户共享行为（沿用根级 `watchlist.json`/`trades.json`），适合纯本地单机。
- **管理**：`user list/add/passwd` 在命令行增删查用户、重置密码（见上）。
- **鉴权**：登录态由 `dsh_session` cookie（HttpOnly/SameSite=Lax）承载，业务 API
  未登录一律 401；`/api/auth/*` 与静态页面公开。`data/` 内所有行情/数据文件仍为只读共享。
- 注意：第三方 CLI（如 `planwatch`）写入的**根级** `data/notifications.jsonl` 在
  多用户模式下不会出现在各用户站内信里（那是共享收件箱）；需要用户可见的提醒
  走 analyzer 自身的消息中心。

### 数据面扩展：资金流 / 公告 / 情绪（可选）

个股分析页新增「资金/公告」子标签，三个新数据模块（均 30 分钟 TTL、断网回退
旧缓存标 `stale`、单块失败不影响其余）：

- **资金流**（`analyzer/flow.py`，`data/flow/{code}.json`）：东财个股每日
  主力/超大单/大单/中单/小单 净流入额与占比，近 120 日；前端主图=主力/超大单
  柱状 + 收盘价线（红流入/绿流出）；
- **公告**（`analyzer/announce.py`，`data/announce/{code}.json`）：巨潮资讯
  （法定披露渠道）近 180 日公告标题+原文链接，表格展示最新 50 条；
- **情绪**（`analyzer/sentiment.py`，`data/sentiment/{code}.json`）：东财
  千股千评——个股综合评分历史（近 120 日，同日去重）+ 当日快照（综合得分/
  关注指数/机构参与度/排名）。**噪声大，仅作辅助参考**，不进任何买卖规则。

盘后更新（`watchlist_info` 阶段）会对全部自选股强制刷新三块数据（绕过 TTL）。
API：`GET /api/flow?code=`、`GET /api/announce?code=`、`GET /api/sentiment?code=`
（均可加 `refresh=1` 强制刷新）。

三块数据同时进入 **AI 综合分析的上下文**（近5日资金流摘要 + 近10条公告标题 +
近10日情绪摘要），prompt 中明确了防幻觉规则：公告效力高于新闻但同样只有标题
不得推测正文；资金流只描述已发生的资金行为；情绪是散户信号禁止据此给方向。

### 交易日记：记忆与复盘（按用户隔离）

新增「交易日记」标签页（`analyzer/journal.py`），**按用户完全隔离**（登录模式
下存 `data/users/{user}/journal/`，免登录共享模式存 `data/journal/`），三个视图：

- **事件流**（自动，append-only）：三类来源自动写入——
  `trade`（网页登记买卖时自动落一条）、`model`（盘后信号扫描命中的 alert/warn，
  按自选股归属写入对应用户）、`system`（每交易日盘后收盘快照：收盘价/涨跌幅/
  量/换手）。事件带唯一 id，供笔记与复盘引用；
- **笔记**（手动）：观察/计划/教训，可编辑删除；LLM 复盘会引用它们；
- **复盘**：每交易日 EOD 更新最后一步（`journal` 阶段）自动为每只自选股生成
  **规则版复盘**（纯本地统计：当日事件 + 近10日行情回顾 + 近10日交易统计 +
  近期笔记，零 LLM 调用）；「交易日记」页可按需生成 **LLM 版复盘**（复用
  AI_API_KEY，上下文=规则版复盘+该股笔记+模型信号事件，聚焦"决策与信号的
  对应/行为模式/下一步观察"，同样禁止编造与买卖指令）。

API：`GET /api/journal?code=`（事件+笔记+复盘一次取回）、
`POST /api/journal` `{"action":"note_add|note_update|note_delete|review_generate|review_llm",…}`。

### 个股 AI 综合分析（可选）

个股分析页新增「AI 分析」子标签：点击「生成 AI 分析」后，服务端并行运行
多因子突破 / GARCH 回归 / 筹码分布 / 布林通道 / 动量 / 风险 / 财报 7 个既有插件，
把各模型的评分、结论、指标、信号（图表序列不上送）与行情快照、近期新闻标题、
资金流/公告/情绪摘要、当日美股简报中的风险偏好分数/核心要闻/当前个股映射聚合为上下文，调用 OpenAI 兼容大模型生成结构化中文报告
（综合结论 / 各模型要点与分歧 / 关键价位 / 基本面与新闻 / 风险提示，末尾固定免责声明）。

配置（仅服务端环境变量，key 不下发前端、不写文件；未配置时该功能停用，其余功能不受影响）：

```bash
export AI_API_KEY=sk-...                            # 必填
export AI_BASE_URL=https://api.deepseek.com/v1      # 可选，任意 OpenAI 兼容端点
export AI_MODEL=deepseek-chat                       # 可选
export AI_TIMEOUT=60                                # 可选，请求超时秒数
python -m analyzer serve
```

- 只在点击按钮时调用大模型（切股票不自动调用）；报告缓存到 `data/ai/{code}.json`，
  TTL 6 小时，且以「prompt 版本 + 模型名 + 上下文指纹（含数据截止日期）」为键，
  模型数据或新闻变化即自动失效；「重新生成」强制忽略缓存。
- **异步任务模式**（生成实测约 3~8 分钟，不占住 HTTP 请求）：缓存命中时
  `POST /api/ai_analysis` 同步秒回报告（`cached: true`）；未命中立即返回
  `202 {"job_id", "queued": true, "status", "poll_ms"}`，后台线程执行生成——
  全局 LLM 并发上限 2（共享网关同时供美股简报/LLM 复盘使用，防限流），
  超出排队（排队上限 16）；同一股票在途任务去重，重复提交返回已有任务。
  任务状态存内存（服务重启丢任务，重新提交即可；报告本体在磁盘缓存中，
  指纹未变重提交会直接命中秒回）。前端轮询展示进度，等待期间可切换股票，
  报告生成后切回即可查看（页面刷新后进入该股也会自动恢复展示）。
- 单个模型或新闻不可用只降级为报告内的数据警告，不阻塞生成。
- 海外信号读取 `data/news/us_market/YYYY-MM-DD.md`，只接受北京时间当天、明确标记
  `状态：有效` 且含合法风险偏好分数和核心要闻的简报；周末、休市、获取失败、日期不符或
  文件缺失均注入“无有效海外信号”，不沿用旧数据。海外上下文变化同样会使 AI 报告缓存失效。
- 隐私/费用：上送第三方服务的是模型指标、行情快照、新闻标题与海外简报提取内容（不含交易流水等账户数据），
  调用会产生对应服务商的 token 费用。
- AI 报告仅供研究参考，不构成任何投资建议。
- API：`POST /api/ai_analysis`，body `{"code": "600519.SH", "refresh": false}`
  （缓存命中 200 返回报告，否则 202 返回任务信息）；
  `GET /api/ai_analysis/status?code=600519.SH`（或 `?job_id=…`）查询任务：
  `queued` / `running` / `done`（附完整报告 payload）/ `failed`（附 `error`
  与 `error_status`）；该股无任务时返回 `{"status": "none"}`。

### 盘后自动更新与消息提醒

`analyzer serve` 启动后自带一个每日盘后更新调度器（`analyzer/updates.py`，
守护线程，无需外部 cron）：

- **触发**：每个交易日 **15:30（CST）** 按交易日历（`data/trade_calendar.parquet`，
  缺文件退化为工作日）自动触发一轮全量更新；错过触发点（如 15:31 后启动）当日补跑一次，
  同一日不重复执行；
- **顺序（自选股优先）**：股票列表/交易日历 → **自选股日线增量** → 自选股新闻/估值/财报/资金流/公告/情绪
  强制刷新 → 全市场剩余日线（自选股刚更新过，自动判定「已是最新」跳过）→ 指数 →
  板块成分（行业+概念）→ `build_dataset` 合并；单阶段失败记录错误并继续后续阶段；
- **缓存失效**：更新完成后自动 `DataStore.reload()`（重载 basic/日历/指数/市场聚合/板块
  并清空日线/筹码/排行缓存）+ 清空插件 Runner 结果缓存与 Context kline 缓存——
  数据更新后**无需重启服务**，下一请求即基于新数据重算；
- **信号提醒**：更新后逐只自选股跑 multi_factor_breakout / garch_mean_reversion /
  chip_distribution / bollinger_channel，提取 `alert` 级信号与 `signal_today=1` 指标，
  写入消息中心（`data/notifications.jsonl`，当日去重）并弹 macOS 桌面通知；
- **交易日记收尾**：信号事件（按自选股归属写入对应用户 journal）+ 每用户每自选股
  盘后快照事件 + 自动生成当日**规则版复盘**（详见「交易日记」节；当日已生成则跳过）；
- **前端**：顶栏「盘后更新」状态点（空闲/更新中·当前阶段/完成/错误）与 🔔 消息中心
  抽屉（进度条 + 消息列表 + 未读角标，15s 轮询，warn/alert 新消息弹 toast），
  点状态点可手动触发一轮更新；
- **API**：`GET /api/update/status`（进度查询）、`POST /api/update/run`（手动触发，
  运行中拒绝并发）、`GET /api/notifications?limit=100` 或 `?since=游标`（增量拉新消息）、
  `POST /api/notifications` body `{"action":"read","all":true}`（标记已读）。

### 昨夜美股简报邮件推送（可选）

`analyzer serve` 自带美股简报邮件推送调度器（`analyzer/overseas_push.py` +
`analyzer/mailer.py`，守护线程）。它读取 AI 分析同源的每日美股简报
`data/news/us_market/YYYY-MM-DD.md`（`analyzer.overseas.load_daily_brief` 严格校验：
仅推**当天、状态有效、日期一致、分数在界、含核心要闻**的简报；周末/休市/缺失/
校验失败一律不推，绝不沿用旧内容），渲染为内联样式的 HTML 邮件（纯文本回退），
经 SMTP 发送。**邮件渠道沿用 quant.example.com 服务器上 github-trending-push 项目的
email 通道**（QQ 邮箱 SMTP 587 STARTTLS），但发信账号由本系统自己的环境变量配置：

```bash
export SMTP_HOST=smtp.qq.com    # 默认
export SMTP_PORT=587            # 465 走 SSL，其余 STARTTLS
export SMTP_USER=xxx@qq.com     # 发件邮箱
export SMTP_PASS=<授权码>       # QQ 邮箱设置里生成的 SMTP 授权码
export MAIL_TO=a@x.com,b@y.com  # 收件人（逗号分隔）
export US_BRIEF_PUSH_HOUR=8     # 推送时刻（CST），默认 08:40
export US_BRIEF_PUSH_MINUTE=40
export US_BRIEF_PUSH_RETRY=30,30,30  # 简报未就绪时的重试间隔（分钟）
export MAIL_DRY_RUN=false       # true 只记日志不真发（调试）
python -m analyzer serve
```

- **触发**：每工作日 08:40（CST，可配）推送；简报尚未生成或临时无效时按
  `30,30,30` 分钟节奏重试（锚定上次实际尝试时刻），当日成功或重试耗尽即止；
  进程启动时若已过触发时刻且当日未推过则立即补推一次；推送状态持久化到
  `data/news/us_market/.push_state.json`（防重启重发，只留 60 天）；
- **防重发**：同一天最多成功推送一次（手动触发亦幂等跳过）；
- **结果可见**：成功/失败都会写站内信（系统来源）；失败不阻塞 analyzer 其它功能；
- **未配置时自动停用**（`SMTP_USER/SMTP_PASS/MAIL_TO` 任一缺失），启动日志会提示；
- **API**：`GET /api/us_brief_push/status`、`POST /api/us_brief_push/run`（手动推送）；
- 邮件主题形如 `🇺🇸 昨夜美股 2026-10-26 · 风险偏好 -42（偏空）`，正文含
  风险偏好分数/情绪判断/置信度、核心要闻、自选股映射、今日交易输入等小节。

### 站内信（统一收件箱）

`notifybox/` 是 analyzer 与 planwatch **共用的中立站内信模块**，把「各模型信号」
「交易提醒」「系统进度」汇入同一个收件箱（`data/notifications.jsonl`），Web 仪表盘
的整页「站内信」Tab 与顶栏 🔔 抽屉都是它的视图：

- **统一来源 `source`**：`model`（模型信号，盘后扫描写入）/ `trade`（planwatch 交易提醒：
  价格位、软/硬止损、阶梯止盈、T 信号候选、时间钩子）/ `system`（更新启动/完成/错误）；
- **统一级别 `level`**：`info / ok / warn / alert`；兼容 planwatch 原 `danger / target`
  （归一到 `alert`），原始级别留存于 `raw_level`；桌面弹窗按统一级别 warn/alert 触发；
- **跨进程共享（免依赖对方常驻）**：planwatch CLI 的 `emit()` 追加写同一 JSONL，
  analyzer 读接口时按**字节偏移 tail 跟随 + 消息 id 去重**增量消费另一进程新写的行，
  故盯盘提醒产生后 Web **轮询即时可见**，无需重启、无需 HTTP 互联；文件被 `clear`
  截断重写时自动回退全量重读；
- **整页收件箱**（`#inbox` Tab）：左侧来源分栏（全部 / 模型信号 / 交易提醒 / 系统，
  带 total + 未读徽标），右侧消息流支持「只看未读 / 按级别筛选」，点 `code ↗ 看个股`
  跳转个股分析并自动标已读，点整条标记已读，「当前分类全部已读 / 清空」按来源作用；
  顶栏 🔔 抽屉保留为快速预览（同数据源），Tab 与抽屉共享未读角标。
- **API**：`GET /api/notifications?source=trade&level=alert&unread=1&code=…&since=游标`
  （过滤 + 返回 `counts.by_source` 各来源 total/unread）；`POST /api/notifications`
  `{"action":"read","ids":[…]|"all":true,"source":"trade"}`（标记已读，可按来源）、
  `{"action":"clear","source":"…","before_day":"YYYY-MM-DD"}`（清空）。

**仪表盘四个标签页**（深色主题，红涨绿跌）：

- **市场**（原「市场概览」+「排行榜」已合并，图表与排行榜共用同一区间，可选
  1月/3月/6月/1年/3年/5年/10年/全部，默认 1年）：指数对比（7 大指数归一化对数轴）、
  市场宽度（涨跌家数）、全市场日成交额、等权全市场累计净值 vs 沪深300；
  下方为排行榜（`#rank` 深链接滚动至此区，见下条）；
- **个股分析**：代码/名称模糊搜索（含联想），搜索框旁展示**搜索历史记录**
  （仅记录搜索框发起的查询：联想点击/回车/点历史chip，去重置顶、最多保留
  10 条，点击快速查看、✕ 单条删除，localStorage 记忆），
  K线+MA5/20/60/120+成交量，
  **K线周期一键切换 时/日/周/月**（日=本地日线；周/月由日线按 ISO 周/自然月聚合，
  开=首日开、收=末日收、高/低=区间极值、量额=区间和，买卖标记自动映射到所属
  周期K线；时=本地 1 分钟线聚合为 60 分钟K线（09:30/10:30/13:00/14:00 四档，
  真实价，仅自选股有本地分钟数据，标题栏实时显示覆盖范围，无数据时自动回退
  日K线并提示；周期选择记忆在浏览器），**点击任意日K线查看当日分时**（弹窗：
  1 分钟K线价格/均价 VWAP/分钟量双图、
  昨收基准线与当日摘要卡；读本地分钟数据（自选股自动后台同步），近期无数据
  时可「立即拉取」在线抓取；注意 1 分钟线源只保留最近约 8 个交易日，更早的
  日期无法补拉——本地分钟历史从加入自选当日起累积；周/月K线点击查看该周期
  末日分时），
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
  切到个股标签页自动展示自选列表，点行跳转，持久化到 `data/users/{用户}/watchlist.json`（登录模式，按用户隔离）；
  **个股分析页默认展示第一只自选股**（无自选时回退预设；第一只无数据时自动
  回退茅台）；列表可**一键折叠/展开**（▾/▸ 按钮，状态记忆在浏览器）；
  行首 **⠿ 手柄拖拽调整顺序**（拖到目标行上/下半部决定插前/插后，明细行折算到
  其主行，顺序即时乐观更新并持久化，失败自动回滚；`POST /api/watchlist`
  action=reorder 传完整 codes 数组，未知代码忽略、缺项按原顺序追加尾部）；
  列表内直接展示每只股票**最新一期财报摘要**（报告期/营收/营收同比/净利/净利同比/ROE，
  读本地缓存；无缓存时行内「抓取财报」一键拉取），点行内「📰 消息」展开该股票
  **最新新闻流**（东方财富个股新闻，标题可点原文，`data/news/` 30 分钟缓存，
  网络失败回退旧缓存，支持强制刷新）；点行内「详情」展开**明细行**——**完整财报**（营业总收入/净利/扣非及各自同比、EPS/ROE/毛利率/净利率/资产负债率）、**个股信息**（流通市值/换手率/52周区间及位置/年初至今/1年涨跌幅，纯本地数据）、**估值**（PE(TTM)/PE(静)/PB/PEG/PS/PCF，东财估值分析，`data/valuation/` 30 分钟缓存））；**自选股分钟线后台同步**（加自选即在后台
  下载该股 1 分钟 K 线，新浪源、真实价口径，存 `data/minute/1/{code}.parquet`；
  服务启动时自动补齐已有自选、运行期每 30 分钟增量刷新，本地历史随时间累积；
  自选列表「分钟线」列实时显示 本地根数 · 最新时间，同步中显示「同步中…」；
  手动同步：`python -m analyzer minute`）；**买卖记录**（独立「交易记录」页维护
  买卖台账：「➕ 新增记录」登记 股票(代码/名称联想)/日期/方向/成交价/股数/费用/备注，
  「📋 批量录入」粘贴多行文本（日期 方向 价格 股数 [费用] [备注]，空格/逗号/Tab 分隔，
  支持 买/卖/buy/sell、YYYY-MM-DD 等多种日期写法，# 注释与表头自动跳过）先预览后导入，
  单条失败不影响其余；
  流水表为按股票分组的记账式（组头=代码/名称/笔数/期末持仓，组内按时间倒序；
  借=买入金额/贷=卖出金额 分列 + 逐笔持仓余额，余额为负标红提示缺买入记录；
  每组带小计行，页脚吸底总计：净股数/借方/贷方/费用/持仓合计/净额），支持逐笔
  编辑✎/删除✕，可按 全部/仅自选 过滤、按代码或名称搜索；
  持仓汇总表按股票聚合，并列展示两种成本口径——成本价（移动加权平均：卖出
  只记已实现盈亏）与 摊薄成本 = (累计买入金额+买入费用−累计卖出金额+卖出费用)÷持仓
  （多数股票 APP 的口径），另有 最新价/笔数/持仓/总费用/已实现盈亏/浮动盈亏，
  点行跳转个股；费用默认按费率模型逐笔估算（佣金‱与每笔最低元可在页面调整并
  记忆，印花税 0.05% 仅卖出、过户费 0.001%），也可填券商成交单的实际费用覆盖
  估算（流水表 ≈ 前缀为估算值）；个股页 K 线面板「管理交易 →」跳本页并预填该股，
  K 线上以 ▲红=买、▼绿=卖 标在成交价处（周/月K线自动映射到所属周期K线，时K线
  不展示），tooltip 显示当日每笔明细，下方为只读流水表，标题栏「▾/▸ 交易明细」
  按钮**点击展开/隐藏**（状态记忆在浏览器）；自选列表同步显示「持仓」「成本·浮盈」
  两列，持久化到 `data/users/{用户}/trades.json`（登录模式按用户隔离）
  （API：`GET/POST /api/trades`，action = add/add_batch/update/remove，可选 fee =
  该笔实际费用；add_batch 传 trades 数组，单条失败逐条报错不阻断）；非交易日登记自动
  吸附到之前最近交易日，早于该股首个数据日的记录仅在流水表展示、不画标记）；
  支持 `#stock` / `#rank` / `#trades` / `#inbox` 深链接；
- **排行榜**（位于「市场」页图表下方，与图表共用同一区间）：任意区间 ×
  区间涨跌幅/年化波动/最大回撤/平均换手 的 Top/Bottom N，点击行跳转个股。
  分 **个股 / 板块** 两个视图：
  - **个股视图**：每行含「板块」列（行业归属），工具栏可按 **市场板块**（沪主板/
    深主板/创业板/科创板/北交所，按代码前缀本地推导、无需外部数据）与 **板块**
    （行业/概念下拉，含成分股数）叠加筛选个股池，点击「板块」列标签快速筛选；
  - **板块视图**：按板块排行，板块指标=成分股**等权**聚合（涨跌幅=成分股
    区间收益均值，波动/回撤/换手取均值，另列 涨/跌家数 与 板块总市值）；满足最低
    样本量的成分股不足 5 只的板块自动过滤（统计噪声）。可在 **行业 / 概念** 间切换：
    - **行业板块**（划分，每股恰好归属一个）：东财行业板块（约 496 个细粒度行业）；
      东财不可用时自动降级 baostock 证监会行业分类（约 84 个行业，口径更粗，东财
      恢复后重跑 board 可换回）；
    - **概念板块**（题材/风格标签约 500 个，成分**有重叠**——同一股票可属多个概念，
      跨概念无加总意义）：仅东财源（`python -m downloader board --with-concept`），
      无兜底；无数据时「概念」选项自动隐藏。
    未下载板块数据时板块视图与「板块」列/筛选自动降级不可用。
- **站内信**（统一收件箱 Tab，见上节「站内信（统一收件箱）」）：左栏按来源分栏
  （全部 / 模型信号 / 交易提醒 / 系统，带 total+未读徽标），右栏消息流支持只看未读、
  按级别筛选，点 `code ↗ 看个股` 跳个股并标已读，「当前分类全部已读 / 清空」按来源作用；
  汇聚 analyzer 模型信号 + planwatch 交易提醒（价格位/止损止盈/T 信号/时间钩子）+
  盘后更新进度，与顶栏 🔔 抽屉同源、共享未读角标。

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
