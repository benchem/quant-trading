# stock_analysis —— 插件式个股分析框架

对**个股**做分析模块的插件化架构：多个程序员并行开发，互不影响。

## 设计目标

> 加一个分析模块 = 加一个文件。不改任何共享代码，git 冲突趋近于零。

```
        ┌────────────────────────────────────────────────────────────┐
        │  analyzer 仪表盘 / CLI / 未来调度                            │
        │   GET /api/plugins  ·  GET /api/analysis?plugin=&code=      │
        └──────────────────────────────▲─────────────────────────────┘
                                       │ to_dict() (JSON)
        ┌──────────────────────────────┴─────────────────────────────┐
        │  Runner：参数校验/强转 · 故障隔离 · 计时 · 结果缓存            │  core/runner.py
        └──────────────────────────────▲─────────────────────────────┘
                                       │ AnalysisResult
   ┌──────────────┬────────────────────┴─────────────────┬──────────────────┐
   │ plugins/     │  自动发现：一个 .py = 一个（组）插件      │  互相隔离，        │
   │ momentum.py  │  禁止跨插件 import；禁止 import DataStore │  异常不外抛       │
   │ risk_profile │                                            │                │
   │ fundamental  │                                            │                │
   └──────┬───────┘                                            └────────────────┘
          │ 只依赖这两个入口
   ┌──────┴──────────────────────────────────────────────────────┐
   │ core/base.py（契约，冻结）    core/context.py（数据上下文）     │
   │ AnalysisPlugin/Result/      AnalysisContext: kline/close/    │
   │ ParamSpec/Signal            index_kline/market/fundamental/  │
   └─────────────────────────────────┬────────────────────────────┘
                                     │ duck typing
   ┌─────────────────────────────────┴────────────────────────────┐
   │ analyzer.datastore.DataStore（本地 Parquet，可换实现）          │
   └───────────────────────────────────────────────────────────────┘
```

## 目录结构

```
stock_analysis/
├── core/                    # 框架核心 = 契约（稳定、冻结，仅负责人修改，需 review）
│   ├── base.py              # AnalysisPlugin / AnalysisResult / ParamSpec / Signal
│   ├── context.py           # AnalysisContext —— 插件取数的唯一入口
│   ├── registry.py          # 插件发现与注册（目录扫描、坏文件隔离）
│   └── runner.py            # 执行：参数校验、故障隔离、计时、缓存
├── plugins/                 # ★ 插件目录（自动发现）
│   ├── __init__.py          #   永远为空，任何人不需要改
│   ├── momentum.py          #   示例：技术面（评分/信号/图表序列）
│   ├── risk_profile.py      #   示例：风险（波动/回撤/beta/明细表）
│   ├── fundamental_quality.py  # 示例：基本面（数据缺失时优雅降级）
│   └── DEVELOPING.md        #   ★ 开发指南（新成员必读）
├── testing.py               # 合成数据 Context（单测零依赖：无数据目录、无网络）
├── cli.py / __main__.py     # list / show / run
└── README.md
```

## 快速上手

```bash
# 列全部插件
python -m stock_analysis list

# 看某插件的参数声明
python -m stock_analysis show momentum

# 真实数据运行（需先 python -m downloader all && build）
python -m stock_analysis run momentum risk_profile --code 600519.SH
python -m stock_analysis run fundamental_quality --code 600519.SH --set periods=12 --json

# 合成数据演示（无需下载数据）
python -m stock_analysis run momentum --code 600001.SH --demo
```

在代码中作为库使用：

```python
from analyzer.datastore import DataStore
from stock_analysis import Runner, load_plugins
from stock_analysis.core.context import AnalysisContext

store = DataStore("data")
runner = Runner(load_plugins(), AnalysisContext(store))
result = runner.run("momentum", "600519.SH", window=30)
print(result.score, result.verdict, result.summary)
```

## 数据契约（插件能看到什么）

插件**只能**通过 `ctx: AnalysisContext` 取数：

| 方法 | 说明 |
|---|---|
| `ctx.kline(code, start=, end=, last=)` | 个股日线 DataFrame（date 索引；open/high/low/close/volume/amount/pct_chg/turnover）。无数据抛 `DataUnavailable` |
| `ctx.close(code, ...)` | 收盘价 Series |
| `ctx.index_kline(code)` | 指数日线（000001.SH/000300.SH/000905.SH/399001.SZ/399006.SZ 等） |
| `ctx.market(start=, end=)` | 全市场日度聚合（涨跌家数/等权净值/成交额） |
| `ctx.fundamental(code)` | **本地缓存**财报（不触发网络；无则 `None`） |
| `ctx.chips(code)` | 筹码分布结果（本地计算；无则 `None`） |
| `ctx.basic(code)` / `ctx.name(code)` / `ctx.meta` | 基础信息（名称/市值/最新价等） |
| `ctx.trading_days()` / `ctx.annualize()` | 交易日历 / 年交易日数 |
| `ctx.normalize_code(code)` | 代码规范化（600519 → 600519.SH） |

**输出契约** `AnalysisResult`（`to_dict()` 保证 JSON 可序列化）：

| 字段 | 用途 |
|---|---|
| `score` (0~100) / `verdict` / `summary` | 结论性输出（可省） |
| `metrics` {k: 标量} | 关键指标卡片 |
| `tables` {名: [行]} | 明细表（`r.add_table("名", df)`） |
| `series` {名: {dates, values}} | 图表序列（`r.add_series("名", index, values)`） |
| `signals` [{level, text}] | 提示/预警（info/ok/warn/alert） |
| `extra` {…} | 自由扩展 |

## 并行开发规则（为什么互不影响）

1. **加插件 = 加文件**。`plugins/__init__.py` 永远是空的；发现是自动扫描，
   不需要登记、不需要改 server/registry。
2. **插件之间禁止互相 import**；需要共享逻辑时，把工具函数写在自己文件里
   （`_` 前缀私有），或提 PR 给负责人加进 `core/`（走 review）。
3. **插件只依赖 `core.base` + `AnalysisContext`**，不碰 DataStore 内部、
   不发网络请求（财报要数据先跑 `analyzer fundamental --codes ...` 预热缓存）。
4. **故障隔离**：你的插件抛异常只会让自己的结果 `ok=False`（带 traceback），
   不会拖垮别人的插件、CLI 批次或仪表盘进程。
5. **加载隔离**：你的文件 import 失败（哪怕写坏一行）只会在
   `list` 末尾出现一条 `✗ 加载失败`，其他插件照常。
6. **重名覆盖**：调试时可以临时用同名插件覆盖内置实现（后注册者生效，有告警）。
7. **单测零依赖**：`stock_analysis.testing.make_context()` 生成合成数据
   （可复现、零网络），你的插件单测不需要 678MB 数据集。
8. **外部插件仓库**：环境变量 `STOCK_ANALYSIS_PLUGINS=/path/to/dir` 可指向
   独立目录，该目录下每个 `.py` 同样被自动发现——插件甚至可以完全放在
   另一个仓库里开发。

## 仪表盘 API（analyzer 已接入）

```
GET /api/plugins                  -> {"plugins": [元数据...], "failures": [...]}
GET /api/plugins/momentum         -> 单插件元数据（含参数声明）
GET /api/analysis?plugin=momentum&code=600519.SH&window=30
                                  -> 完整 AnalysisResult JSON
```

## 路线图（有意未做的）

- 前端"分析"页签（消费 /api/analysis 的通用渲染器：metrics 卡片 + 表格 +
  series 折线 + signals 标签）——后端契约已就绪；
- 批量模式（插件对自选股/全市场跑横截面，输出排行榜）；
- 插件间组合（因子合成/多插件加权评分）——等首批插件稳定后再定契约。
