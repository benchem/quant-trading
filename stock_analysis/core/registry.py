"""插件发现与注册。

并行开发约定（关键）::

    stock_analysis/plugins/<你的名字或模块>.py   # 一个文件 = 一个（组）插件

- 新增插件 = 新增文件。不修改任何共享文件，git 冲突趋近于零；
- ``plugins/__init__.py`` 保持空文件，任何人都不需要改它；
- 发现顺序按文件名排序，结果确定（谁先谁后不依赖文件系统）；
- 单个插件 import 失败/声明错误只记录告警，不影响其他插件；
- 重名插件：后加载者覆盖先加载者（便于本地调试覆盖内置插件），并记录告警；
- 外部插件目录：环境变量 ``STOCK_ANALYSIS_PLUGINS``（逗号分隔路径），
  支持把插件放进独立仓库/目录，进一步隔离开发。
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import os
from pathlib import Path

from .base import AnalysisPlugin, PluginError

log = logging.getLogger("stock_analysis")

DEFAULT_PLUGIN_PACKAGE = "stock_analysis.plugins"
ENV_EXTRA_DIRS = "STOCK_ANALYSIS_PLUGINS"


class PluginRegistry:
    def __init__(self):
        self._plugins: dict[str, type[AnalysisPlugin]] = {}
        #: 发现失败的 [(文件名/模块名, 错误信息)] —— 展示用，不致命
        self.discovery_failures: list[tuple[str, str]] = []

    # ------------------------------------------------------------- 注册

    def register(self, cls: type[AnalysisPlugin]) -> bool:
        """注册一个插件类。重名时覆盖（记录告警）。返回是否生效。"""
        if not (isinstance(cls, type) and issubclass(cls, AnalysisPlugin)):
            raise TypeError(f"不是 AnalysisPlugin 子类: {cls!r}")
        name = getattr(cls, "name", "")
        if not name or not isinstance(name, str):
            raise PluginError(f"{cls.__name__} 未声明合法的 name")
        try:
            probe = cls()  # 触发 _validate（name/params 声明检查）
        except Exception as e:  # noqa: BLE001
            raise PluginError(f"插件 {name} 声明校验失败: {e}") from e
        _ = probe
        if name in self._plugins and self._plugins[name] is not cls:
            log.warning("插件重名: %s 被 %s.%s 覆盖（原 %s.%s）",
                        name, cls.__module__, cls.__name__,
                        self._plugins[name].__module__, self._plugins[name].__name__)
        self._plugins[name] = cls
        return True

    # ------------------------------------------------------------- 查询

    def get(self, name: str) -> type[AnalysisPlugin] | None:
        return self._plugins.get(name)

    def names(self) -> list[str]:
        return sorted(self._plugins)

    def all(self) -> list[type[AnalysisPlugin]]:
        return [self._plugins[n] for n in self.names()]

    def by_category(self, category: str) -> list[type[AnalysisPlugin]]:
        return [c for c in self.all() if c.category == category]

    def clear(self):
        self._plugins.clear()
        self.discovery_failures.clear()

    def info(self, name: str) -> dict | None:
        """单个插件的元数据（供 /api/plugins 等消费）。"""
        cls = self._plugins.get(name)
        if cls is None:
            return None
        return self._meta_of(cls)

    def meta_list(self) -> list[dict]:
        return [self._meta_of(c) for c in self.all()]

    @staticmethod
    def _meta_of(cls: type[AnalysisPlugin]) -> dict:
        return {
            "name": cls.name,
            "display_name": cls.display_name or cls.name,
            "description": cls.description,
            "version": cls.version,
            "category": cls.category,
            "params": {k: v.to_dict() for k, v in cls.params.items()},
        }


# ------------------------------------------------------------------ 发现


def _collect_from_module(mod) -> list[type[AnalysisPlugin]]:
    """收集模块内所有可实例化的 AnalysisPlugin 子类。"""
    found = []
    for attr in vars(mod).values():
        if (
            isinstance(attr, type)
            and issubclass(attr, AnalysisPlugin)
            and attr is not AnalysisPlugin
            and getattr(attr, "name", "")
        ):
            found.append(attr)
    return found


def discover_package(registry: PluginRegistry, package: str = DEFAULT_PLUGIN_PACKAGE) -> int:
    """扫描包目录下所有 .py（__init__ 除外），import 并注册。返回注册数。"""
    pkg = importlib.import_module(package)
    pkg_dir = Path(pkg.__file__).resolve().parent
    n = 0
    for f in sorted(pkg_dir.glob("*.py")):
        if f.name == "__init__.py":
            continue
        modname = f"{package}.{f.stem}"
        try:
            mod = importlib.import_module(modname)
        except Exception as e:  # noqa: BLE001 隔离：单文件失败不影响整体
            msg = f"{type(e).__name__}: {e}"
            registry.discovery_failures.append((f.name, msg))
            log.error("插件 %s 加载失败: %s", f.name, msg)
            continue
        for cls in _collect_from_module(mod):
            try:
                if registry.register(cls):
                    n += 1
            except PluginError as e:
                registry.discovery_failures.append((f.name, str(e)))
                log.error("插件 %s 注册失败: %s", f.name, e)
    return n


def discover_dir(registry: PluginRegistry, path: str | Path) -> int:
    """扫描**外部目录**下的 .py 文件（非包，直接按文件 import）。

    用于 ``STOCK_ANALYSIS_PLUGINS`` 指向的独立插件仓库。
    """
    d = Path(path).resolve()
    if not d.is_dir():
        registry.discovery_failures.append((str(d), "目录不存在"))
        return 0
    n = 0
    for f in sorted(d.glob("*.py")):
        if f.name.startswith("_"):
            continue
        modname = f"stock_analysis_ext_{f.stem}"
        try:
            spec = importlib.util.spec_from_file_location(modname, f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            registry.discovery_failures.append((str(f), msg))
            log.error("外部插件 %s 加载失败: %s", f, msg)
            continue
        for cls in _collect_from_module(mod):
            try:
                if registry.register(cls):
                    n += 1
            except PluginError as e:
                registry.discovery_failures.append((str(f), str(e)))
                log.error("外部插件 %s 注册失败: %s", f, e)
    return n


def extra_plugin_dirs() -> list[Path]:
    raw = os.environ.get(ENV_EXTRA_DIRS, "")
    return [Path(p.strip()) for p in raw.split(",") if p.strip()]


def load_plugins(registry: PluginRegistry | None = None,
                 package: str = DEFAULT_PLUGIN_PACKAGE,
                 include_extra_dirs: bool = True) -> PluginRegistry:
    """一站式加载：内置插件包 (+ 环境变量外部目录)。幂等（已注册的不会重复）。"""
    if registry is None:
        registry = PluginRegistry()
    discover_package(registry, package)
    if include_extra_dirs:
        for d in extra_plugin_dirs():
            discover_dir(registry, d)
    return registry
