# A股日线数据大面积落后 — 根因分析与修复报告（2026-10-31）

生产环境 `/opt/quant-trading`（只读取证）| 修复实施于仓库 `/root/workspaces/quant-trading`
测试：`.venv/bin/pytest tests/ -q` → **79 passed, 3 skipped**（3 skipped 为东财不可达的 live 用例，环境性）

---

## 一、背景

10-30 EOD 运行（15:50 起跑、18:41 完成、10117s）后核查发现全市场 hfq 日线大面积落后：
5223 个文件中仅 3407 个止于 10-30，943 止于 10-29、122 止于 10-28、72 止于 10-27、
62 止于 10-26，另有 **617 个止于 10-23（全部为 688/689 科创板）**。

本次修复覆盖五条症状（全部有生产取证 + 旧代码 file:line 证据 + 离线单测回归）：

| # | 症状 | 生产取证规模 |
|---|------|-------------|
| S1 | 空窗/无进展被误判「已是最新」 | 550 只误判 uptodate；393 只「写入却无新 bar」 |
| S2 | 127 只三源全败 | 失败清单 128 唯一代码（002:109 / 000:12 / 001:6） |
| S3 | 指数新鲜度误判 | 000688.SH 停在 10-23 |
| S4 | 资金流静默陈旧 | flow/*.json 长期回退旧缓存、status 无痕 |
| S5 | 股票列表截断 | stock_list.parquet=4605 行，0 个 688/689/BJ |

关联症状：140 只「单位断裂」被 skipped 后**永久阻塞**（每日增量 +36% 假跳变被守卫拒绝）。

### 时间线（生产取证）
- 10-26：clist 分页中途空页，列表截断至 4605 行落盘（688/689/BJ 整段缺失）；当日两次看门狗杀
- 10-27：交易日历三源全败，日历止于 10-29 后再未更新
- 10-28 11:22：手动跑；10-29 15:51 杀重跑至 18:56
- 10-30 15:50 杀重跑：坏列表 15:51:28 被读用、坏日历（止于 10-29）、18:41:42 完成
- 东财 push2/push2his 10-28 起间歇/整晚不可达（curl exit=56）——**已知环境问题，非本次修复对象**

---

## 二、逐症状根因（旧代码 file:line 已用 `git show HEAD:` 核对）

### S1 空窗/无进展误判「已是最新」——数据落后的直接放大器
**现象**：550 只 10-30 有真实成交的股票被标 uptodate（抽样 20/20 用腾讯 none 口径证实有 10-30 bar）；
393 个文件 mtime 15:53–17:12 更新但末行扩展字段全 NaN（腾讯源回退数据），即「写入却无新 bar」。

**根因**：旧 `downloader/app.py:301-302`——增量窗口返回空 df 时，只要本地有旧数据就**无条件**
`return (code, "uptodate", "")`；旧 `app.py:308-310` 对返回的 bar **不校验是否落在窗口内**，
源回退给的 ≤10-29 旧 bar 被 `merge_daily` 去重保留新值后当 `updated` 落盘。
东财 hfq 在 10-30 当天生成延迟（18:41 前后才有数据），EOD 15:50–18:41 窗口内取到的就是空/旧数据，
两缺陷叠加把整批缺口洗成「已是最新」。

**修复**（`downloader/app.py`，Wave 2/3）：
- 新增 `_judge_no_progress`：df 空或所有 bar 早于窗口起点（`max(date) < start`，同日重取放行）时——
  ① 窗口无交易日（`_window_has_trading_day`：日历覆盖内查日历、超出日历 max 退化工作日、
  日历缺失返回 None 保守处理）→ 直接 `uptodate` 不请求；
  ② 否则用 `adjust="none"` **同窗口交叉核对**（`_window_has_bars`）：none 有 bar → `stale`
  （源缺数据，入失败清单）；none 也空 → `uptodate`（停牌，判定依据明确）；核对自身失败 →
  fail-safe 按 `stale`（宁误报不漏报）。stale **不写盘**。
- 运行内自愈：`update_stocks(lag_retry_delay=180.0)` 主遍结束后对 stale 批次冷却重跑一遍
  （源 18:41 才生成数据的场景在同一轮 EOD 内恢复），stats 按最终状态单次计数，
  failures 清单含 failed+stale（次日重跑同一命令自动重试）。
- 顺带修：`codes` 分支不再无条件 `self._stock_list()` 触网取列表（旧 app.py:318 行为）。

### S2 127 只三源全败
**根因**（环境为主、代码放大）：东财 push2his 整晚 exit=56（已知环境问题）；腾讯源对缺复权键
的窗口按旧 `downloader/source.py:525-526` 正确 raise（防 10-25 未复权价混入）→ tx 也不可用；
baostock 未安装。三源全败本属环境故障，但旧代码**无运行内重试机制**，当天只能等次日重跑。

**修复**：即 S1 的 stale 二次重试（对空窗批次）+ 失败清单可见（failed+stale 均入
`failed_stocks_hfq.txt`）。对「源数据延迟生成」这一可自愈子类，EOD 内 180s 冷却重跑后自愈。

### S3 指数新鲜度误判（000688.SH 停在 10-23）
**根因**：旧 `downloader/source.py:337-339`——`_em_index_daily` 三个市场前缀都取空时
`return pd.DataFrame(columns=OHLCV_COLUMNS)`（**空 df 不 raise**）；旧 `source.py:207-210`
`with_fallback` 收到不抛异常的 em 结果直接返回，**降级链 em→tx→bs 不被激活**；
旧 `app.py:431-432` `update_indices` 空窗+有旧数据无条件计 `uptodate`。
000688.SH 的东财 secid 市场前缀恰好查无数据（全市场前缀空），腾讯/北交所口径从未被尝试，
指数永久停在旧数据。

**修复**（Wave 1/4）：
- `_em_index_daily` 所有市场前缀均无 K 线 → `raise FetchError`（激活 em→tx→bs 降级，端到端测试验证）；
- `update_indices`：空窗/无进展（`max(date) < start`）+ 有旧数据 → 按交易日历判定
  （`_index_judge_no_progress`：主要指数每个交易日必有 bar，无停牌概念；窗口无交易日 →
  uptodate 且**不发请求**；有交易日 → stale 入 `failed_index_hfq.txt` 重试；日历不可用保守 stale）。

### S4 资金流静默陈旧
**根因**：旧 `analyzer/flow.py:157-163`——`fetch(refresh=True)` 抓取失败**静默回退旧缓存**
（`stale=True` 正常返回）；旧 `analyzer/updates.py:583-591` watchlist_info 循环裸
`except Exception: pass`，EOD 对失败**零记录**；旧 `flow.py:87-94` `_fetch` 仅 retries=2、
退避 1.5/3.0s，对东财整晚故障窗口覆盖不足。资金流可无限期陈旧且 status 无痕。

**修复**（Wave 6）：
- `_fetch` retries=4、指数退避 2/4/8/16s；
- `fetch(..., strict=False)`：EOD 链路 `_refresh_flow` 改用 `strict=True`——失败必须 raise，
  不再静默回退旧缓存；交互路径默认 strict=False 行为不变（回退 stale 缓存）；
- watchlist_info 循环裸 except 改为 `self._error(f"自选资讯刷新失败 ({c} {fn.__name__}): {e}")`，
  失败记入 `status.errors`（主链路不中断）。

### S5 股票列表截断（688/689/BJ 整段缺失 → 617 只科创板数日无更新）
**根因**：旧 `downloader/source.py:358-360`——clist 分页**中途空页静默 `break`**（服务端 total
虚高时翻页提前遇空页，实际数据 < total 也不报错）；旧 `source.py:364` `len(items) >= total`
提前终止；旧 `app.py:146-148` `refresh_stock_list` 对取回内容**无条件覆盖落盘**。
10-26 截断出的 4605 行列表（前缀 000:411/001:122/002:919/003:42/300:936/301:474/302:1/
600:747/601:225/603:616/605:112，688/689/BJ=0）此后被所有全市场运行照单使用：
617 只科创板股票不在队列里，永远轮不到，直到 10-30 仍停在 10-23。

**修复**（Wave 1/4）：
- `_em_stock_list`：中途空页重试同一页一次仍空 → raise；循环后 `len(items) < total` → raise
  （禁止静默截断）；
- 写盘前守卫 `_stock_list_guard`（`refresh_stock_list` 内，拒绝即 `raise FetchError` 保留旧列表）：
  无旧列表（首次写入）总量 <3000 拒绝；有旧列表时任一 3 位前缀家族整体消失 / 单家族缩水 >50% /
  总量缩水 >30% 拒绝；扩容与新家族放行。EOD 两处调用本就 try/except 记错，不阻塞；
  CLI `all` 命令中列表刷新失败降级为记错后继续用旧列表。

### 关联症状：140 只「单位断裂」永久阻塞
**根因**：旧 `app.py:305-307`——增量守卫命中仅标 `skipped` 落盘清单外，无任何自愈路径。
600900.SH 实证：本地 10-20..23 为东财口径（~97.6），10-26/27 为腾讯口径（~71.5），
东财 10-28 恢复后增量窗口 +36.4% 假跳变每日被守卫拒绝、本地永远停在断裂前一日。
复权因子跨供应商有 ~1% 差异，增量窗口恰跨口径切换日时**必中守卫**，只有整段重下
（单一供应商连续序列）能恢复。

**修复**（Wave 2/5）：
- `_self_heal_full_refetch`：守卫命中 → 登记 `data/pending_repair_{adjust}.txt`
  （code\tdate\treason）+ **立即**全量重拉 `[start,end]` 再过守卫 → `repaired`
  （合并落盘、移出清单）/ `skipped`（留清单）；
- `repair_stocks` + CLI `python -m downloader repair [--codes ...] [--force]`：
  默认处理待修清单，--codes 追加（失败留清单），--force 允许无清单/无本地文件的任意代码；
- EOD `market_daily` 之后自动重试待修清单（清单为空 no-op），140 只中源已恢复的次日自动愈合。

---

## 三、改动清单（`git diff --stat`：8 文件 +485/-65，另新增 3 文件）

| 文件 | 改动 |
|------|------|
| `downloader/app.py` | Wave 2：`_STAT_LABEL` +stale/repaired、`_load_trade_calendar`（mtime 缓存）、`_window_has_trading_day`、`_judge_no_progress`、`_window_has_bars`、`_pending_repair_*` 四方法、`_full_refetch_one`、`_self_heal_full_refetch`、`_update_stock_one` 重写；Wave 3：`update_stocks` lag_retry_delay 二次重试 + results 单次计数 + failures 含 stale + codes 分支惰性取列表；Wave 4：`MIN_STOCK_LIST_SIZE`、`_stock_list_family`、`_stock_list_guard`、`refresh_stock_list` 写盘前守卫、`_index_judge_no_progress`、`update_indices` stale 判定+无交易日窗口免请求；Wave 5：`repair_stocks` |
| `downloader/source.py` | Wave 1：`_em_index_daily` 全市场前缀无数据 raise FetchError；`_em_stock_list` 空页重试一次仍空 raise、`len(items)<total` raise |
| `downloader/cli.py` | Wave 5：`repair` 子命令（--codes/--force/--end/--adjust/--start）+ main 分派 + 用法文档 |
| `analyzer/flow.py` | Wave 6：`_fetch` retries=4、退避 2.0*2^n（2/4/8/16s）；`fetch` 增加 `strict` 参数 |
| `analyzer/updates.py` | Wave 5：`_run` market_daily 后 4b 步自动重试待修清单（hasattr 守卫）；Wave 6：`_refresh_flow` strict=True；watchlist_info 裸 except 改 `self._error(...)` |
| `tests/test_data_freshness.py` | 新增，37 个离线测试（S1×8、S3×3、S5×7、自愈×2、repair×7、flow/EOD×5、列表守卫直测×5） |
| `tests/conftest.py` | 新增，store/tmp 共享 fixture（离线合成数据） |
| `tests/test_eod_updates.py` | FakeDownloader +repair_stocks、阶段顺序断言更新、新增 `t_repair_step_failure_continues` |
| `tests/test_daily_incremental.py` | `test_refresh_stock_list_writes_basic_copy` 假列表扩充至过 floor（测试目的不变：stock_basic 副本同步） |
| `tests/test_board_live.py` | pytest 下 live 用例改 `pytest.skip` 语义（东财不可达时不 error） |
| `docs/plans/2026-10-31-data-freshness-fix.md` | 进度跟踪 + 关键设计决定 + 生产取证要点 |

未改动：`/opt/quant-trading` 生产环境（只读取证）；未执行 git commit/push。

## 四、测试结果

| 套件 | 结果 |
|------|------|
| `.venv/bin/pytest tests/ -q`（仓库根） | **79 passed, 3 skipped**（13.4s，无触网；3 skipped=东财不可达的 live 用例） |
| `tests/test_data_freshness.py`（本次新增 37 用例） | 37 passed |
| harness 脚本（`t_*` 风格，pytest 不收集）：test_eod_updates 18 PASS、test_daily_incremental 7、test_data_guards 5、test_board 14、test_board_live skip（东财不可达）、test_market_data 6、test_price_unify 10、test_watchlist 2、test_source_live ALL PASS（tx 源可达）、其余 12 个 harness 全部 0 失败 | 全绿 |

## 五、遗留与建议（部署后观察）

1. **东财 push2/push2his 整晚不可达是环境问题**（curl exit=56），代码层已做降级/重试/可见性，
   但数据新鲜度上限取决于该环境恢复。建议排查生产机出口 IP 是否被东财风控（10-26 前后开始恶化）。
2. 部署到 `/opt/quant-trading` 后建议顺序：先 `python -m downloader list`（坏列表会被守卫拒绝、
   保留旧表；网络恢复后重跑即可取回含 688 的完整列表）→ `python -m downloader repair`
   （处理 140 只待修清单）→ 次日观察 EOD 状态 `stats` 中 `stale`/`repaired` 键。
3. 交易日历三源全败（10-27）未单独修复（属环境故障），但 `_window_has_trading_day` 已做
   「超出日历退化工作日」兜底，日历滞后不再导致误判。
