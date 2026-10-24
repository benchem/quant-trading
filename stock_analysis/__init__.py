"""stock_analysis —— 插件式个股分析框架。

设计目标: **多个程序员并行开发分析模块，互不影响**。

- 框架核心（契约）: :mod:`stock_analysis.core` —— 稳定、冻结，仅负责人修改；
- 插件目录: :mod:`stock_analysis.plugins` —— 自动发现，**加插件 = 加文件**；
- 合成数据测试: :mod:`stock_analysis.testing` —— 单测无需真实数据集/网络；
- 命令行: ``python -m stock_analysis list|show|run``。

快速上手::

    from stock_analysis import AnalysisPlugin, AnalysisResult, ParamSpec, Runner, load_plugins
    from stock_analysis.core.context import AnalysisContext

    registry = load_plugins()            # 发现全部插件
    ctx = AnalysisContext(store)         # store: analyzer.datastore.DataStore
    runner = Runner(registry, ctx)
    result = runner.run("momentum", "600519.SH")
    print(result.to_dict())

更多见 ``stock_analysis/README.md`` 与 ``stock_analysis/plugins/DEVELOPING.md``。
"""
from .core import (
    CATEGORIES,
    PARAM_TYPES,
    SIGNAL_LEVELS,
    AnalysisContext,
    AnalysisPlugin,
    AnalysisResult,
    DataUnavailable,
    ParamSpec,
    PluginError,
    PluginRegistry,
    Runner,
    Signal,
    coerce_params,
    load_plugins,
)

__version__ = "0.1.0"

__all__ = [
    "CATEGORIES", "PARAM_TYPES", "SIGNAL_LEVELS",
    "AnalysisPlugin", "AnalysisResult", "AnalysisContext", "DataUnavailable",
    "ParamSpec", "PluginError", "PluginRegistry", "Runner", "Signal",
    "coerce_params", "load_plugins",
    "__version__",
]
