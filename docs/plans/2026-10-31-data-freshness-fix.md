# A股日线数据落后 — 根因修复实施计划（2026-10-31）

根因分析已完成（见最终报告），本文件跟踪实施进度。TDD：先写失败测试 -> 实现 -> 全绿。

## 任务清单

- [x] Phase 1 根因分析（5 条症状，全部有生产取证 + 交叉验证）
- [x] Wave 0 测试基建（conftest store/tmp fixture + test_board_live pytest.skip 语义；42 passed, 3 skipped）
- [x] Wave 1 `downloader/source.py`（5 测试绿：em 指数空 raise / tx 降级端到端 / 列表分页空页 raise / total 不符 raise / 正常分页）
- [x] Wave 2 `downloader/app.py`（8 测试绿：空窗 stale / 停牌 uptodate / 无进展 stale / 窗口无交易日 / 交叉核对失败 fail-safe / `_window_has_trading_day` / 单位断裂自愈 repaired / 仍断裂 skipped；含 `_judge_no_progress` / `_window_has_bars` / `_self_heal_full_refetch` / pending_repair 清单 / `_STAT_LABEL` stale+repaired）
- [x] Wave 3 `update_stocks` 运行内二次重试（stale 收集 + 冷却重跑 + stats 单次计数 + failures 含 stale；顺带修：codes 分支不再无条件取股票列表触网）
- [x] Wave 4 股票列表完整性守卫（`_stock_list_guard` 直测×5 + refresh 拒绝保留旧×1 + 正常写盘×1）+ `update_indices` 空窗/无进展 stale 判定×3（含无交易日窗口不发请求）；顺带修既有 test_refresh_stock_list_writes_basic_copy 假列表过 floor
- [x] Wave 5 `repair_stocks` + CLI `repair` 子命令（app.py 7 测试绿：清单 repaired / 失败留清单 / force 新股 / 无 force 拒绝 / codes 合并清单 / CLI parse / CLI 端到端；cli.py repair 子命令；EOD market_daily 后自动重试待修清单（hasattr 守卫，t_* harness 18 PASS 含新增失败容错））
- [x] Wave 6 `analyzer/flow.py`（retries=4、退避 2/4/8/16s、strict 参数）+ `updates.py`（EOD flow strict、watchlist_info 记错）（5 测试绿：退避序列 / strict raise / 非 strict 回退不变 / _refresh_flow strict / EOD 失败可见）
- [x] 全量 `.venv/bin/pytest tests/ -q` 绿（79 passed, 3 skipped；23 个 harness 脚本全绿/skip）
- [x] 中文报告（docs/reports/2026-10-31-data-freshness-fix-report.md；改动前 file:line 已用 git show HEAD 逐一核对）

## 关键设计决定
- 停牌 vs 源缺数据：adjust="none" 同窗口交叉核对（窗口=失败的那次增量窗口；none 有 bar=有交易活动->stale；none 空->uptodate；核对自身失败->fail-safe stale）
- 窗口无交易日（交易日历判定，超出日历退化工作日，日历缺失保守返回 None）-> 直接 uptodate，不请求
- 无进展定义：df 空 或 所有 bar 早于窗口起点（max(date) < start）；同日重取（start==最后bar日）放行
- 单位断裂自愈：守卫命中 -> pending_repair_{adjust}.txt（code\tdate\treason）+ 立即全量重拉[start,end]再过守卫 -> repaired（合并落盘、移出清单）/ skipped（留清单）；CLI `repair` 手动补修
- 列表守卫：无旧列表 floor 3000；3 位前缀家族整体消失 / 单家族缩水>50% / 总量缩水>30% -> raise 保留旧列表
- 指数：`update_indices` 空窗/无进展 + 有旧数据 -> 日历判 stale/uptodate；`_em_index_daily` 空改 raise（激活 em→tx→bs 降级）
- flow：`_fetch` retries=4、退避 2/4/8/16s；`fetch(strict=True)` 失败 raise（EOD `_refresh_flow` 用）；watchlist_info 循环裸 except 改为记错
- stale 二次重试：默认冷却 180s（参数化 `lag_retry_delay`，测试用 0 表示立即重试）；stats 按最终状态单次计数；failures 清单含 failed+stale

## 生产取证要点（报告素材）
- 5223 hfq 文件分层：3407@10-30 / 943@10-29 / 122@10-28 / 72@10-27 / 62@10-26 / 617@10-23（全 688/689）
- 10-30 运行：3800 updated / 538 uptodate / 127 failed / 140 skipped；失败清单 128 唯一代码（002:109 / 000:12 / 001:6）
- 生产 stock_list.parquet=4605 行，0 个 688/689/BJ（前缀 000:411 / 001:122 / 002:919 / 003:42 / 300:936 / 301:474 / 302:1 / 600:747 / 601:225 / 603:616 / 605:112）
- 550 误判最新抽样 20/20 在 10-30 有真实成交（tx none 口径）
- 393 文件末行扩展字段全 NaN（tx 源），mtime 15:53-17:12
- 600900.SH：本地 10-20..23 东财口径~97.6；10-26/27 腾讯口径~71.5；东财 10-28=97.444 被守卫拒（+36.4%），每日被拒
- 时间线：10-26 两次看门狗杀（列表已坏）；10-27 日历三源全败；10-28 11:22 手动跑；10-29 15:51 杀重跑至 18:56；10-30 15:50 杀重跑（坏列表 15:51:28、坏日历止于 10-29，18:41:42 完成 10117s）

## 改动前 file:line 证据（报告引用，已用 git show HEAD 核对）
- S1-A 旧 app.py:301-302 空窗无条件 uptodate
- S1-B 旧 app.py:308-310 旧 bar 当 updated 写入
- S1-C/S5 旧 source.py:358-359 空页静默 break、:364 len>=total 提前终止；旧 app.py:146-158 无条件覆盖列表
- S2 127 失败=三源全败（em curl56 环境 + tx hfq 键缺失 source.py:525-526 + bs 未装）
- S3 旧 app.py:431-432 + 旧 source.py:337-339 空 df 不 raise -> with_fallback(source.py:207-210) 不降级
- S4 updates.py:583-591 -> 697-700 -> flow.py fetch(refresh=True)；flow.py:157-163 静默回退旧缓存；updates.py:587-590 裸 except；flow.py:87-94 retries=2
