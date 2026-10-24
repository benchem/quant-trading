# 插件开发指南（并行开发约定）

> 目标：你只管写 `plugins/<你的名字或模块>.py`，不碰任何共享文件，
> 也不影响任何人。

## 0. 五条铁律

1. 新增插件 = **新增一个 .py 文件**。`plugins/__init__.py` 永远不要动。
2. 插件**只 import** `stock_analysis`（契约）和 pandas/numpy；
   **禁止** import 其他插件、`analyzer.datastore`、发起网络请求。
3. 所有数据通过 `ctx` 取（见 `core/context.py` 的方法表）。
4. 算不出来/数据不够：**返回 ok 结果 + 说明**（或 `ok=False` + 原因），
   不要静默返回空，也不要让异常裸奔（框架会兜底，但友好的降级体验更好）。
5. 提交前跑两遍验证：合成数据单测 + 真实数据 CLI（下面有模板）。

## 1. 新插件模板（整段复制，改名字就能用）

```python
"""一句话说明这个插件算什么。

作者: 你的名字
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from stock_analysis import AnalysisPlugin, AnalysisResult, ParamSpec


class MyModulePlugin(AnalysisPlugin):
    name = "my_module"                 # 全局唯一（小写下划线）
    display_name = "我的模块"            # 界面显示名
    description = "一句话：算什么、怎么用"
    version = "0.1.0"
    category = "technical"              # technical/risk/fundamental/chips/flow/sentiment/other
    params = {
        # 参数声明：CLI 的 --set、前端表单都靠它渲染与校验
        "window": ParamSpec(type="int", default=20, min=5, max=120, help="回看交易日数"),
        "mode": ParamSpec(type="str", default="a", choices=("a", "b"), help="模式"),
    }
    # cache_ttl = 0       # 默认：进程内缓存 ok 结果
    # timeout = 60        # 软超时（秒）

    def run(self, ctx, code: str, window: int = 20, mode: str = "a") -> AnalysisResult:
        # 1) 取数（只走 ctx）
        df = ctx.kline(code, last=window * 3)     # DataFrame, date 索引
        # if ctx.kline 抛 DataUnavailable -> 框架转成 ok=False + hint，无需你处理
        close = df["close"].astype(float)
        last = float(close.iloc[-1])
        ret = last / float(close.iloc[0]) - 1.0

        # 2) 计算（纯函数、无副作用、幂等）
        vol = float(close.pct_change().std() * np.sqrt(ctx.annualize()))

        # 3) 组装结果（有什么放什么；前端按字段渲染）
        r = AnalysisResult(self.name, code)
        r.metric("ret", ret, "区间收益", 4)        # key, value, 中文标签, 精度
        r.metric("vol", vol, "年化波动", 4)
        r.score = 50.0                             # 可选：0~100 综合分
        r.verdict = "示例结论"                      # 可选：一句话定性
        r.summary = f"区间收益 {ret * 100:+.1f}%"   # 必写：给人看的一句话
        r.signal("warn", "波动偏高")                # 可选：info/ok/warn/alert
        r.add_table("detail", df.tail(10))         # 可选：DataFrame 明细
        r.add_series("close", df.index, close, title="收盘价")  # 可选：图表序列
        r.extra["note"] = "自由扩展字段"            # 可选
        return r
```

保存为 `stock_analysis/plugins/my_module.py`。**完成。** 无需任何其他改动。

## 2. 本地验证

```bash
# a) 发现是否正常（末尾不应出现你的文件 ✗）
python -m stock_analysis list

# b) 真实数据
python -m stock_analysis run my_module --code 600519.SH
python -m stock_analysis run my_module --code 600519.SH --set window=60 --json

# c) 合成数据（无需数据目录）
python -m stock_analysis run my_module --code 600001.SH --demo
```

## 3. 单测模板（零数据依赖，放进 tests/test_my_module.py）

```python
import sys
sys.path.insert(0, ".")
from stock_analysis import Runner, load_plugins
from stock_analysis.testing import make_context


def test_my_module():
    ctx = make_context(n_stocks=2, years=5, seed=42)   # 可复现合成数据
    try:
        runner = Runner(load_plugins(), ctx)
        r = runner.run("my_module", "600001.SH")
        assert r.ok, r.error
        assert r.summary
        assert 0 <= (r.score or 0) <= 100
        # 需要财报数据的插件:
        # from stock_analysis.testing import make_fundamental_frame
        # ctx.set_fundamental("600001.SH", make_fundamental_frame("600001.SH", 40))
        # runner.clear_cache()
        # r = runner.run("my_module", "600001.SH")
    finally:
        ctx.cleanup()


if __name__ == "__main__":
    test_my_module()
    print("PASS")
```

要点：

- `make_context(seed=...)` 固定种子 → 断言可复现；
- 合成股票代码池：`600001.SH / 000002.SZ / 300001.SZ / 688001.SH / 000001.SZ`
  （`n_stocks` 控制数量，`years` 控制历史长度，默认 5 年≈1220 交易日）；
- 需要指数：合成上下文自带 `000300.SH`（等权合成指数），beta 类计算直接可用。

## 4. 常见问题

| 问题 | 回答 |
|---|---|
| 两个插件都想算同一个指标（如 RSI） | 各自在自己文件里写 `_rsi()`。重复几行没关系；真的高频复用再提 PR 进 `core/`（负责人 review） |
| 我的插件要财报 | `ctx.fundamental(code)` 只读本地缓存，返回 `None` 就按"无数据"优雅降级，并在 `signal` 里提示 `analyzer fundamental --codes ...` |
| 我的插件很慢（>10s） | 设置 `cache_ttl`；重计算考虑收窄窗口；`--no-cache` 调试。批量场景另议（路线图） |
| 想临时改内置插件调试 | 新建同名插件（`name` 相同），后注册者覆盖内置并告警；别直接改别人的文件 |
| 我的插件依赖新库 | 先提出来：框架依赖保持最小（pandas/numpy/pyarrow）。确需新库走 `pyproject` optional-dependencies + 插件内延迟 import，缺失时 `signal` 提示 |
| 代码写坏了一行 | `list` 会显示 `✗ 加载失败 <file>: <错误>`，**其他人不受影响**；修好即恢复 |

## 5. 提交流程

1. 分支 `feature/plugin-<name>`（或直接在主分支加文件——新文件不冲突）；
2. 文件头注释写清：算什么、作者、依赖哪些数据；
3. 附上：`list` 输出、真实数据 `run` 输出截图/文本、单测通过记录；
4. review 只关注：是否违反五条铁律、`name` 是否撞名、参数声明是否完整。
