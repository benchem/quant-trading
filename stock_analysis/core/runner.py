"""执行层：参数校验/类型强转、故障隔离、计时、结果缓存。

故障隔离原则（多人并行开发的兜底）::

    插件A抛异常/声明错误/超时
        -> 返回 ok=False 的 AnalysisResult（带 error 摘要）
        -> 不影响 插件B、不影响进程、不影响 API 服务

缓存策略:
- 仅缓存 ok=True 的结果（错误不缓存，修好立即生效）；
- key = (插件名, 插件version, code, 全部参数)；
- 进程内 LRU（本地工具量级足够，默认上限 512）。
"""
from __future__ import annotations

import logging
import time
import traceback

from .base import AnalysisPlugin, AnalysisResult, ParamSpec, PluginError
from .context import AnalysisContext
from .registry import PluginRegistry

log = logging.getLogger("stock_analysis")

_CACHE_MAX = 512


# ------------------------------------------------------------------ 参数处理


def _coerce(spec: ParamSpec, value):
    if value is None:
        return None
    t = spec.type
    try:
        if t == "int":
            return int(round(float(value)))
        if t == "float":
            return float(value)
        if t == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on", "是")
            return bool(value)
        if t == "str":
            return str(value)
    except (TypeError, ValueError) as e:
        raise PluginError(f"参数 {spec.type} 类型转换失败: {value!r}") from e
    raise PluginError(f"未知参数类型 {t!r}")


def _check(spec: ParamSpec, key: str, value) -> None:
    if value is None:
        return
    if spec.min is not None and value < spec.min:
        raise PluginError(f"参数 {key}={value} 小于最小值 {spec.min}")
    if spec.max is not None and value > spec.max:
        raise PluginError(f"参数 {key}={value} 大于最大值 {spec.max}")
    if spec.choices and value not in spec.choices:
        raise PluginError(f"参数 {key}={value!r} 不在可选值 {list(spec.choices)} 中")


def coerce_params(cls: type[AnalysisPlugin], raw: dict) -> dict:
    """按插件 params 声明做默认填充 + 类型强转 + 范围校验。

    未声明的参数被忽略（仅记日志）—— 避免他人升级参数名时老调用方炸掉。
    """
    out: dict = {}
    for key, spec in cls.params.items():
        v = raw.get(key)
        if v is None or (isinstance(v, str) and v == ""):
            v = spec.default
        out[key] = _coerce(spec, v)
        _check(spec, key, out[key])
    for k in raw:
        if k not in cls.params:
            log.warning("插件 %s 收到未声明参数 %r（已忽略）", cls.name, k)
    return out


# ------------------------------------------------------------------ 执行器


class Runner:
    def __init__(self, registry: PluginRegistry, ctx: AnalysisContext,
                 use_cache: bool = True, cache_max: int = _CACHE_MAX):
        self.registry = registry
        self.ctx = ctx
        self.use_cache = use_cache
        self._cache: dict[tuple, AnalysisResult] = {}
        self._cache_max = cache_max

    def clear_cache(self):
        self._cache.clear()

    # ------------------------------------------------------------ 单个执行

    def run(self, plugin: str, code: str, **params) -> AnalysisResult:
        cls = self.registry.get(plugin)
        if cls is None:
            r = AnalysisResult(plugin, code)
            r.fail(f"未知插件: {plugin}（可用: {', '.join(self.registry.names()) or '无'}）")
            return r

        try:
            code = self.ctx.normalize_code(code)
        except PluginError as e:
            r = AnalysisResult(plugin, code)
            r.fail(str(e))
            return r

        try:
            p = coerce_params(cls, params)
        except PluginError as e:
            r = AnalysisResult(plugin, code)
            r.fail(f"参数错误: {e}")
            return r

        key = (plugin, cls.version, code, tuple(sorted(p.items())))
        if self.use_cache and key in self._cache:
            return self._cache[key]

        t0 = time.perf_counter()
        try:
            inst = cls()
            r = inst.run(self.ctx, code, **p)
            if not isinstance(r, AnalysisResult):
                raise TypeError(
                    f"run() 应返回 AnalysisResult，实际返回 {type(r).__name__}"
                )
            r.plugin = plugin
            r.code = code
            r.ok = True
            r.plugin_version = cls.version
        except Exception as e:  # noqa: BLE001 —— 隔离边界：任何异常都不外抛
            log.exception("插件 %s(%s) 执行失败", plugin, code)
            r = AnalysisResult(plugin, code)
            r.plugin_version = cls.version
            r.fail(f"{type(e).__name__}: {e}")
            if "DataUnavailable" in type(e).__name__:
                r.extra["hint"] = "本地缺少该股数据，请先运行 python -m downloader"
            r.extra["traceback"] = traceback.format_exc(limit=6)

        r.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        if self.use_cache and r.ok:
            self._cache[key] = r
            if len(self._cache) > self._cache_max:
                self._cache.pop(next(iter(self._cache)), None)
        return r

    # ------------------------------------------------------------ 批量执行

    def run_many(self, plugins: list[str], code: str, **params) -> list[AnalysisResult]:
        """顺序执行多个插件（隔离：每个插件独立成结果）。"""
        return [self.run(p, code, **params) for p in plugins]

    # ------------------------------------------------------------ 元数据

    def list_plugins(self) -> list[dict]:
        return self.registry.meta_list()
