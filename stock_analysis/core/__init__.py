"""stock_analysis 框架核心（稳定契约层）。

修改本目录 = 修改契约，需要 code review；只做向后兼容的加法。
"""
from .base import (
    CATEGORIES,
    PARAM_TYPES,
    SIGNAL_LEVELS,
    AnalysisPlugin,
    AnalysisResult,
    ParamSpec,
    PluginError,
    Signal,
)
from .context import AnalysisContext, DataUnavailable
from .registry import (
    DEFAULT_PLUGIN_PACKAGE,
    PluginRegistry,
    discover_dir,
    discover_package,
    extra_plugin_dirs,
    load_plugins,
)
from .runner import Runner, coerce_params

__all__ = [
    "CATEGORIES", "PARAM_TYPES", "SIGNAL_LEVELS",
    "AnalysisPlugin", "AnalysisResult", "ParamSpec", "PluginError", "Signal",
    "AnalysisContext", "DataUnavailable",
    "PluginRegistry", "PluginRegistry", "load_plugins", "discover_package",
    "discover_dir", "extra_plugin_dirs", "DEFAULT_PLUGIN_PACKAGE",
    "Runner", "coerce_params",
]
