"""插件契约（稳定 API）—— 整个框架中唯一允许插件 import 的"框架模块"。

设计原则（多人并行开发不冲突的关键）::

    ┌────────────────────────────────────────────────────────┐
    │  插件文件（每人一个，只增不改）                          │
    │      只依赖: base.py + AnalysisContext                  │
    └───────────────▲──────────────────────▲─────────────────┘
                    │ 实现                 │ 调用
        ┌───────────┴───────────┐  ┌───────┴────────────────┐
        │ base.py（契约，冻结）  │  │ AnalysisContext（数据口）│
        └───────────────────────┘  └────────────────────────┘

- 新增插件 = 在 ``plugins/`` 下新增一个 .py 文件，不需要改动任何共享文件；
- 插件之间禁止互相 import（防止耦合，也防止一人改文件影响另一人）;
- 契约修改走 code review，且只做向后兼容的加法。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

import numpy as np
import pandas as pd

#: 建议分类（前端可据此分组展示）
CATEGORIES = ("technical", "fundamental", "risk", "chips", "flow", "sentiment", "other")

#: 参数类型
PARAM_TYPES = ("int", "float", "str", "bool")

#: 信号级别
SIGNAL_LEVELS = ("info", "ok", "warn", "alert")


class PluginError(ValueError):
    """参数校验失败 / 插件声明不合法。"""


# --------------------------------------------------------------------- 参数


@dataclass(frozen=True)
class ParamSpec:
    """插件参数声明：用于 CLI/前端渲染与运行时校验。

    >>> ParamSpec(type="int", default=20, min=5, max=120, help="回看交易日数")
    """

    type: str = "int"  # int / float / str / bool
    default: Any = None
    min: float | None = None
    max: float | None = None
    choices: tuple | None = None
    help: str = ""

    def __post_init__(self):
        if self.type not in PARAM_TYPES:
            raise PluginError(f"ParamSpec.type 应为 {PARAM_TYPES}，收到 {self.type!r}")
        if self.choices is not None and self.default is not None and self.default not in self.choices:
            raise PluginError(f"默认值 {self.default!r} 不在 choices {self.choices} 中")

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "default": self.default,
            "min": self.min,
            "max": self.max,
            "choices": list(self.choices) if self.choices else None,
            "help": self.help,
        }


# ------------------------------------------------------------------ 结果结构


@dataclass(frozen=True)
class Signal:
    """一条提示/预警（前端渲染为彩色标签）。"""

    level: str  # info / ok / warn / alert
    text: str

    def __post_init__(self):
        if self.level not in SIGNAL_LEVELS:
            raise PluginError(f"Signal.level 应为 {SIGNAL_LEVELS}，收到 {self.level!r}")

    def to_dict(self) -> dict:
        return {"level": self.level, "text": self.text}


def _py(x: Any, nd: int = 4) -> Any:
    """numpy 标量 -> Python 原生类型（NaN -> None），用于 JSON 序列化。"""
    if x is None:
        return None
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if isinstance(x, (int, np.integer)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        f = float(x)
        if not np.isfinite(f):
            return None
        return round(f, nd) if abs(f) < 1e6 else round(f, 2)
    return x


def _clean(obj: Any, _depth: int = 0) -> Any:
    """递归清洗为 JSON 可序列化对象（dict/list/str/int/float/bool/None）。"""
    if _depth > 12:
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _clean(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v, _depth + 1) for v in obj]
    if isinstance(obj, pd.Timestamp):
        return obj.strftime("%Y-%m-%d")
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        f = float(obj)
        return None if not np.isfinite(f) else (round(f, 4) if abs(f) < 1e6 else round(f, 2))
    if isinstance(obj, np.ndarray):
        return [_clean(v, _depth + 1) for v in obj.tolist()]
    if isinstance(obj, float):
        return None if not np.isfinite(obj) else obj
    if obj is None or isinstance(obj, (str, int, bool)):
        return obj
    return str(obj)


class AnalysisResult:
    """单个插件的统一输出结构（``to_dict()`` 保证 JSON 可序列化）。

    所有字段都是可选的 —— 不同插件填不同组合，前端/CLI 按"有什么渲染什么"::

        score / verdict / summary   结论性输出（可为 None）
        metrics                     关键指标（标量，用于对比/排序/卡片）
        tables                      具名表格（list[dict]，用于明细展示）
        series                      具名时间序列（用于画图）
        signals                     提示/预警
        extra                       自由扩展（必须可 JSON 序列化）
    """

    def __init__(self, plugin: str, code: str):
        self.plugin = plugin
        self.code = code
        self.ok: bool = True
        self.error: str | None = None
        self.plugin_version: str | None = None

        self.score: float | None = None  # 0~100 综合分（建议）
        self.verdict: str | None = None  # 结论文字，如 "偏强" / "中性" / "风险偏高"
        self.summary: str = ""
        self.metrics: dict[str, Any] = {}
        self.metric_labels: dict[str, str] = {}  # 指标名 -> 中文名（可选）
        self.tables: dict[str, list[dict]] = {}
        self.series: dict[str, dict] = {}
        self.signals: list[Signal] = []
        self.extra: dict[str, Any] = {}
        self.elapsed_ms: float = 0.0

    # ------------------------------------------------------------ 构造助手

    def metric(self, key: str, value: Any, label: str | None = None, nd: int = 4) -> None:
        self.metrics[key] = _py(value, nd)
        if label:
            self.metric_labels[key] = label

    def add_table(self, name: str, df: pd.DataFrame, nd: int = 4, max_rows: int = 1000) -> None:
        """DataFrame -> 表格。索引若为日期则转为字符串列；数值四舍五入。"""
        t = df.copy()
        if isinstance(t.index, pd.DatetimeIndex):
            t = t.reset_index()
            t.columns = [t.index.name or "index"] + list(t.columns[1:])
            t[t.columns[0]] = pd.to_datetime(t[t.columns[0]]).dt.strftime("%Y-%m-%d")
        t = t.head(max_rows)
        rows = []
        for _, r in t.iterrows():
            rows.append({str(k): _py(v, nd) for k, v in r.items()})
        self.tables[name] = rows

    def add_series(self, name: str, index: pd.Index | list, values: pd.Series | list,
                   title: str | None = None) -> None:
        """时间序列 -> {"dates": [...], "values": [...], "title": ...}。"""
        idx = pd.Index(index)
        if isinstance(idx, pd.DatetimeIndex):
            dates = idx.strftime("%Y-%m-%d").tolist()
        else:
            dates = [str(x) for x in idx]
        vals = list(pd.Series(values).astype(float))
        self.series[name] = {
            "title": title or name,
            "dates": dates,
            "values": [_py(v, 4) for v in vals],
        }

    def signal(self, level: str, text: str) -> None:
        self.signals.append(Signal(level, text))

    def fail(self, msg: str) -> "AnalysisResult":
        self.ok = False
        self.error = msg
        return self

    # ------------------------------------------------------------ 序列化

    def to_dict(self) -> dict:
        d = {
            "plugin": self.plugin,
            "code": self.code,
            "ok": self.ok,
            "score": self.score,
            "verdict": self.verdict,
            "summary": self.summary,
            "metrics": _clean(self.metrics),
            "metric_labels": self.metric_labels,
            "tables": _clean(self.tables),
            "series": _clean(self.series),
            "signals": [s.to_dict() for s in self.signals],
            "extra": _clean(self.extra),
            "elapsed_ms": self.elapsed_ms,
        }
        if self.error:
            d["error"] = self.error
        if self.plugin_version:
            d["plugin_version"] = self.plugin_version
        return d


# ------------------------------------------------------------------ 插件基类


class AnalysisPlugin:
    """个股分析插件基类。子类在模块顶层定义，声明元数据并实现 :meth:`run`。

    最小可用示例::

        class MyPlugin(AnalysisPlugin):
            name = "my_plugin"
            display_name = "我的分析"
            description = "一句话说清楚算什么"
            category = "technical"
            params = {"window": ParamSpec(type="int", default=20, min=5, max=120, help="回看天数")}

            def run(self, ctx, code, window=20):
                df = ctx.kline(code, last=window * 3)
                r = AnalysisResult("my_plugin", code)
                r.metric("close", df["close"].iloc[-1])
                r.summary = "..."
                return r

    约定:
    - ``run`` 内所有异常都会被框架捕获（隔离），但请尽量返回 ok 结果并说明原因；
    - 只通过 ``ctx`` 访问数据，不要 import DataStore / 其他插件 / 发起网络请求；
    - 纯计算插件对同一 (code, params) 应当幂等（框架会缓存 ok 结果）。
    """

    #: 唯一标识（小写下划线）。全局唯一，冲突时后加载者覆盖并告警。
    name: ClassVar[str] = ""
    display_name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    version: ClassVar[str] = "0.1.0"
    category: ClassVar[str] = "other"  # 见 CATEGORIES
    #: 参数声明 {参数名: ParamSpec}
    params: ClassVar[dict[str, ParamSpec]] = {}
    #: 结果缓存有效期（秒）；0 = 常驻内存缓存（进程内），None = 不缓存
    cache_ttl: ClassVar[float | None] = 0
    #: 软超时（秒）；超时结果仍返回，但标记 extra["timed_out"]=True
    timeout: ClassVar[float] = 60.0

    def __init__(self):
        self._validate()

    def _validate(self):
        if not self.name or not self.name.isidentifier():
            raise PluginError(f"{type(self).__name__}: name 必须是非空标识符")
        for k, v in self.params.items():
            if not isinstance(v, ParamSpec):
                raise PluginError(f"{self.name}: 参数 {k} 的声明必须是 ParamSpec")

    def run(self, ctx: "AnalysisContext", code: str, **params) -> AnalysisResult:  # noqa: F821
        """对单只股票执行分析，返回 :class:`AnalysisResult`。"""
        raise NotImplementedError
