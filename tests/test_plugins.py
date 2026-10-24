"""stock_analysis 插件框架自测（无需网络；真实数据部分自动跳过）。

运行: .venv/bin/python tests/test_plugins.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, ".")

from stock_analysis import (  # noqa: E402
    AnalysisPlugin,
    AnalysisResult,
    ParamSpec,
    PluginRegistry,
    Runner,
    load_plugins,
)
from stock_analysis.core.context import DataUnavailable  # noqa: E402
from stock_analysis.testing import make_context, make_fundamental_frame  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, fn) -> bool:
    global PASSED, FAILED
    print(f"\n===== {name} =====", flush=True)
    try:
        fn()
        print("PASS")
        PASSED += 1
        return True
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        FAILED += 1
        return False


# ------------------------------------------------------------------ 用例


def t_discovery_builtin():
    reg = load_plugins()
    names = reg.names()
    assert "momentum" in names, names
    assert "risk_profile" in names, names
    assert "fundamental_quality" in names, names
    assert reg.discovery_failures == [], reg.discovery_failures
    meta = reg.info("momentum")
    assert meta["category"] == "technical"
    assert "window" in meta["params"] and meta["params"]["window"]["default"] == 20


def t_discovery_isolation():
    """坏文件不影响好文件（多人并行的兜底机制）。"""
    reg = PluginRegistry()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        (p / "zz_broken.py").write_text(
            "raise RuntimeError('boom 故意写坏')\n", encoding="utf-8"
        )
        (p / "aa_good.py").write_text(
            "from stock_analysis import AnalysisPlugin, AnalysisResult\n"
            "class T(AnalysisPlugin):\n"
            "    name = 'aa_good'\n"
            "    display_name = '好插件'\n"
            "    def run(self, ctx, code, **kw):\n"
            "        return AnalysisResult(self.name, code)\n",
            encoding="utf-8",
        )
        from stock_analysis.core.registry import discover_dir

        discover_dir(reg, p)
    assert "aa_good" in reg.names(), reg.names()
    assert len(reg.discovery_failures) == 1
    assert "zz_broken.py" in reg.discovery_failures[0][0]


def t_param_validation():
    ctx = make_context(n_stocks=1, years=3)
    try:
        runner = Runner(load_plugins(), ctx)
        # 低于 min
        r = runner.run("momentum", "600001.SH", window=1)
        assert not r.ok and "最小值" in r.error, r.error
        # 非法类型
        r = runner.run("momentum", "600001.SH", window="abc")
        assert not r.ok and "类型转换失败" in r.error, r.error
        # 未知参数被忽略（不炸）
        r = runner.run("momentum", "600001.SH", window=20, bogus_param=1)
        assert r.ok, r.error
        # choices 校验
        r = runner.run("risk_profile", "600001.SH", index="999999.SH")
        assert not r.ok and "可选值" in r.error, r.error
        # 字符串类型参数强转
        r = runner.run("risk_profile", "600001.SH", window="120")
        assert r.ok, r.error
    finally:
        ctx.cleanup()


def t_error_isolation():
    """一个插件抛异常不影响其他插件（隔离边界）。"""

    class Boom(AnalysisPlugin):
        name = "boom"
        display_name = "必炸"

        def run(self, ctx, code, **kw):
            raise ValueError("模拟插件内部错误")

    ctx = make_context(n_stocks=1, years=3)
    try:
        reg = load_plugins()
        reg.register(Boom)
        runner = Runner(reg, ctx)
        r1 = runner.run("boom", "600001.SH")
        assert not r1.ok and "模拟插件内部错误" in r1.error
        assert "traceback" in r1.extra
        # 同一批次里其他插件照常
        r2 = runner.run("momentum", "600001.SH")
        assert r2.ok, r2.error
        # 错误不缓存：修好后立即生效
        assert runner.run("boom", "600001.SH").ok is False
    finally:
        ctx.cleanup()


def t_data_unavailable():
    ctx = make_context(n_stocks=1, years=3)
    try:
        runner = Runner(load_plugins(), ctx)
        r = runner.run("momentum", "999999.SH")
        assert not r.ok
        assert "hint" in r.extra, "应附带数据缺失提示"
    finally:
        ctx.cleanup()


def t_fundamental_paths():
    ctx = make_context(n_stocks=1, years=3)
    try:
        runner = Runner(load_plugins(), ctx)
        r = runner.run("fundamental_quality", "600001.SH")
        assert r.ok and "无财报" in r.summary
        ctx.set_fundamental("600001.SH", make_fundamental_frame("600001.SH", 40))
        runner.clear_cache()
        r = runner.run("fundamental_quality", "600001.SH")
        assert r.ok and r.score is not None
        assert len(r.tables["recent_periods"]) == 8
    finally:
        ctx.cleanup()


def t_result_json_serializable():
    """所有内置插件在合成数据上的输出必须能 json.dumps。"""
    ctx = make_context(n_stocks=2, years=4, seed=3)
    try:
        runner = Runner(load_plugins(), ctx)
        ctx.set_fundamental("600001.SH", make_fundamental_frame("600001.SH", 40))
        for plugin in ("momentum", "risk_profile", "fundamental_quality"):
            for code in ("600001.SH", "000002.SZ"):
                r = runner.run(plugin, code)
                assert r.ok, f"{plugin}@{code}: {r.error}"
                s = json.dumps(r.to_dict(), ensure_ascii=False)
                assert len(s) > 50
    finally:
        ctx.cleanup()


def t_cache_identity():
    ctx = make_context(n_stocks=1, years=3)
    try:
        runner = Runner(load_plugins(), ctx)
        r1 = runner.run("momentum", "600001.SH", window=20)
        r2 = runner.run("momentum", "600001.SH", window=20)
        assert r1 is r2, "相同参数应命中缓存（同一对象）"
        r3 = runner.run("momentum", "600001.SH", window=30)
        assert r3 is not r1, "不同参数不应命中缓存"
        runner.clear_cache()
        r4 = runner.run("momentum", "600001.SH", window=20)
        assert r4 is not r1, "clear_cache 后应重新计算"
    finally:
        ctx.cleanup()


def t_name_override():
    """重名插件：后注册者覆盖（本地调试覆盖内置）。"""

    class Momentum(AnalysisPlugin):  # 与内置同名
        name = "momentum"
        display_name = "我的覆盖版"
        version = "9.9.9"

        def run(self, ctx, code, **kw):
            r = AnalysisResult(self.name, code)
            r.summary = "overridden"
            return r

    reg = load_plugins()
    reg.register(Momentum)
    assert reg.info("momentum")["display_name"] == "我的覆盖版"
    ctx = make_context(n_stocks=1, years=2)
    try:
        runner = Runner(reg, ctx)
        assert runner.run("momentum", "600001.SH").summary == "overridden"
    finally:
        ctx.cleanup()


def t_code_normalization():
    ctx = make_context(n_stocks=1, years=2)
    try:
        runner = Runner(load_plugins(), ctx)
        # 600001 / sh600001 / 600001.SH 等价
        for c in ("600001", "sh600001", "600001.SH"):
            r = runner.run("momentum", c)
            assert r.ok and r.code == "600001.SH", (c, r.code, r.error)
        # 非法代码 -> 友好错误，不抛异常
        r = runner.run("momentum", "abc")
        assert not r.ok
    finally:
        ctx.cleanup()


def t_context_kline_contract():
    """kline 返回 DataFrame、date 索引、必需列齐全、last 参数生效。"""
    import pandas as pd

    ctx = make_context(n_stocks=1, years=2, seed=5)
    try:
        df = ctx.kline("600001.SH")
        assert isinstance(df, pd.DataFrame)
        assert isinstance(df.index, pd.DatetimeIndex)
        for col in ("open", "high", "low", "close", "volume"):
            assert col in df.columns, col
        assert len(ctx.kline("600001.SH", last=30)) == 30
        # 区间过滤
        sub = ctx.kline("600001.SH", start=str(df.index[-60].date()))
        assert len(sub) <= 61
        # 无数据
        try:
            ctx.kline("123456.SZ")
            raise AssertionError("应当抛 DataUnavailable")
        except DataUnavailable:
            pass
    finally:
        ctx.cleanup()


def t_realdata_smoke():
    """真实数据冒烟（data/ 不存在时跳过）。"""
    daily = Path("data/daily/hfq/600519.SH.parquet")
    if not daily.exists():
        print("SKIP: 无真实数据 data/daily/hfq/600519.SH.parquet")
        return
    from analyzer.datastore import DataStore
    from stock_analysis.core.context import AnalysisContext

    store = DataStore("data")
    ctx = AnalysisContext(store)
    runner = Runner(load_plugins(), ctx)
    for plugin, extra in (
        ("momentum", {}),
        ("risk_profile", {}),
        ("fundamental_quality", {}),
    ):
        r = runner.run(plugin, "600519.SH", **extra)
        assert r.ok, f"{plugin}: {r.error}"
        assert r.summary, f"{plugin} 无 summary"
        json.dumps(r.to_dict(), ensure_ascii=False)
        print(f"  {plugin}: score={r.score} verdict={r.verdict} "
              f"[{r.elapsed_ms}ms] {r.summary}")


# ------------------------------------------------------------------ 主流程


def main() -> int:
    tests = [
        ("发现: 内置插件", t_discovery_builtin),
        ("发现: 坏文件隔离", t_discovery_isolation),
        ("参数校验/强转/choices", t_param_validation),
        ("故障隔离: 异常不扩散", t_error_isolation),
        ("数据缺失: 友好降级", t_data_unavailable),
        ("基本面: 无数据/有数据", t_fundamental_paths),
        ("输出: JSON 可序列化", t_result_json_serializable),
        ("缓存: 命中/失效", t_cache_identity),
        ("重名: 后注册覆盖", t_name_override),
        ("代码规范化", t_code_normalization),
        ("Context 数据契约", t_context_kline_contract),
        ("真实数据冒烟(600519)", t_realdata_smoke),
    ]
    for name, fn in tests:
        check(name, fn)
    print(f"\n===== 汇总: {PASSED} PASS / {FAILED} FAIL =====")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
