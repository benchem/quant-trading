"""架构守卫：智能体（LLM/进化区）与规则/模型层的依赖方向强制。

背景：智能体能力（prompt、LLM 调用、上下文组装策略）会频繁进化升级；
规则面（journal 复盘规则、EOD 流程、数据面、量化插件）必须稳定。
本测试用 AST 静态扫描全仓 import，强制单向依赖：

    数据面（datastore/news/flow/announce/sentiment/overseas…）
        → 模型/规则面（stock_analysis/journal/updates/report…）
            → 智能体面（analyzer/agent：唯一允许碰 LLM 的包）

规则：
  A. 规则面/数据面禁止 import analyzer.agent 或 analyzer.ai（兼容壳）
     —— 升级/更换 LLM 不得改动任何规则代码。
  B. 只有 agent 包自身、ai.py 兼容壳、server.py（组合根）、tests 可引用 agent。
  C. agent 包禁止 import 上层（server/notify/updates/journal/report…），
     只能依赖数据面 + 标准库 + 第三方（保持 agent 可独立演进）。
  D. 裸 LLM HTTP 调用（chat/completions）只能出现在 agent 包内。
  E. 运行时隔离：analyzer.journal 不持有 LLM 函数（已迁至 agent.review）。

新增 LLM 能力时：加在 analyzer/agent/ 内（按 __init__ 分工），勿改规则面。
新增文件若触发本测试，说明依赖方向错了，改 import 位置而不是改白名单。

运行: .venv/bin/python tests/test_architecture.py
"""
from __future__ import annotations

import ast
import sys
import traceback
from pathlib import Path

sys.path.insert(0, ".")

REPO = Path(".").resolve()

# 允许引用智能体层（agent/ai）的模块 —— 白名单必须显式，新增即审
AGENT_IMPORTERS_ALLOWED = {
    "analyzer/server.py",        # 组合根：唯一同时接线规则面与智能体层的地方
    "analyzer/ai.py",            # 兼容壳（re-export，供旧 import 路径过渡）
}
AGENT_PKG_FILES = "analyzer/agent/"
TESTS_DIR = "tests/"

# agent 包允许依赖的下层（数据面 + 第三方/标准库；模型面按需在 context.py 用 duck typing）
AGENT_FORBIDDEN_IMPORTS = {
    "analyzer.server", "analyzer.notify", "analyzer.updates", "analyzer.journal",
    "analyzer.report", "analyzer.auth", "analyzer.chips", "analyzer.minute",
    "analyzer.mailer", "analyzer.ai",  # 壳也是 agent，禁止 agent 反过来走壳
}

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


# ------------------------------------------------------------------ 扫描


def _iter_py_files():
    for d in ("analyzer", "stock_analysis", "planwatch", "notifybox",
              "downloader", "tests"):
        base = REPO / d
        if base.is_dir():
            yield from base.rglob("*.py")


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def _imports_of(path: Path) -> list[str]:
    """收集一个文件里所有 import 的顶层模块名（含函数内延迟 import）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    pkg_prefix = "analyzer." if str(path).replace("\\", "/").startswith("analyzer/") else ""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.append(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:  # 相对 import：按文件包前缀还原
                base = "analyzer" if pkg_prefix else None
                if base:
                    out.append(f"{base}.{node.module}" if node.module else base)
            elif node.module:
                out.append(node.module)
    return out


def _all_agent_refs(path: Path) -> list[str]:
    """该文件里指向智能体层的引用（analyzer.agent* / analyzer.ai* / 相对 .agent / .ai）。"""
    rel = _rel(path)
    refs = []
    in_analyzer = rel.startswith("analyzer/")
    for name in _imports_of(path):
        top = name.split(".")[0]
        if name.startswith("analyzer.agent") or name == "analyzer.agent":
            refs.append(name)
        elif name == "analyzer.ai" or name.startswith("analyzer.ai."):
            refs.append(name)
        elif in_analyzer and top == "analyzer":
            # 上面已覆盖；其余 analyzer.* 不算
            pass
        elif in_analyzer and name in ("agent", "ai"):
            # from . import agent / from .agent import ...（module 为 None 时已还原为 analyzer）
            refs.append(f"analyzer.{name} (relative)")
    # 相对 import 还原为 "analyzer" 且 module 为 None 的情况（from . import agent 在 module 里是 None）
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level and node.level > 0 \
                and node.module is None:
            for a in node.names:
                if a.name in ("agent", "ai"):
                    refs.append(f"analyzer.{a.name} (relative from-import)")
    return refs


def _normalized_imports(path: Path) -> list[str]:
    """还原后的完整模块名（用于 C 规则）。"""
    rel = _rel(path)
    out = []
    for name in _imports_of(path):
        if rel.startswith("analyzer/") and not name.startswith(("analyzer", "stock_analysis",
                                                                "planwatch", "notifybox",
                                                                "downloader")):
            out.append(f"analyzer.{name}")
        else:
            out.append(name)
    return out


# ------------------------------------------------------------------ 用例


def t_rule_a_rule_layer_clean():
    """A. 规则面/数据面文件不得引用智能体层。"""
    violations = []
    for f in _iter_py_files():
        rel = _rel(f)
        if rel.startswith(AGENT_PKG_FILES) or rel in AGENT_IMPORTERS_ALLOWED \
                or rel.startswith(TESTS_DIR):
            continue
        for ref in _all_agent_refs(f):
            violations.append(f"{rel}: {ref}")
    assert not violations, "规则面/数据面引用了智能体层:\n" + "\n".join(violations)


def t_rule_b_agent_importers_whitelisted():
    """B. 引用 agent 的文件必须在白名单内（= 规则 A 的镜像，双保险）。"""
    offenders = []
    for f in _iter_py_files():
        rel = _rel(f)
        if rel.startswith(AGENT_PKG_FILES) or rel in AGENT_IMPORTERS_ALLOWED \
                or rel.startswith(TESTS_DIR):
            continue
        if _all_agent_refs(f):
            offenders.append(rel)
    assert not offenders, f"白名单外的文件引用了智能体层: {offenders}"


def t_rule_c_agent_downward_only():
    """C. agent 包只依赖下层（数据面+第三方），禁止 import 上层/兄弟业务模块。"""
    violations = []
    for f in (REPO / AGENT_PKG_FILES).rglob("*.py"):
        rel = _rel(f)
        for name in _normalized_imports(f):
            top = name.split(".")[0]
            if top == "analyzer" and any(
                    name == b or name.startswith(b + ".") for b in AGENT_FORBIDDEN_IMPORTS):
                violations.append(f"{rel}: {name}")
    assert not violations, "agent 包依赖了上层模块（进化区必须只向下依赖）:\n" + "\n".join(violations)


def t_rule_d_no_bare_llm_http():
    """D. 裸 LLM HTTP 端点调用只能出现在 agent 包内。"""
    offenders = []
    for f in _iter_py_files():
        rel = _rel(f)
        if rel.startswith(AGENT_PKG_FILES) or rel.startswith(TESTS_DIR):
            continue
        if "chat/completions" in f.read_text(encoding="utf-8"):
            offenders.append(rel)
    assert not offenders, f"agent 包外发现裸 LLM HTTP 调用: {offenders}"


def t_rule_e_runtime_isolation():
    """E. 运行时隔离：journal（规则面）不持有 LLM 能力，agent 包可独立导入。"""
    import importlib

    j = importlib.import_module("analyzer.journal")
    assert not hasattr(j, "build_llm_review"), "journal 仍持有 LLM 复盘函数"
    assert not hasattr(j, "REVIEW_PROMPT_VERSION"), "journal 仍持有 prompt 版本"
    assert not hasattr(j, "LLM_NOTES_LIMIT"), "journal 仍持有 LLM 聚合常量"

    a = importlib.import_module("analyzer.agent")
    for name in ("AIConfig", "AIError", "chat_completion", "collect_context",
                 "build_prompt", "generate", "build_llm_review",
                 "PROMPT_VERSION", "REVIEW_PROMPT_VERSION"):
        assert hasattr(a, name), f"agent 包缺少对外导出 {name}"

    # 兼容壳指向同一批对象（旧 import 路径不炸且不产生双份实现）
    ai = importlib.import_module("analyzer.ai")
    assert ai.AIConfig is a.AIConfig
    assert ai.chat_completion is a.chat_completion
    assert ai.generate is a.generate
    assert ai.build_llm_review is a.build_llm_review
    assert ai.PROMPT_VERSION == a.PROMPT_VERSION
    assert ai.build_prompt is a.build_prompt
    assert ai.collect_context is a.collect_context

    # 规则版复盘在 journal 内且离线可跑（不触网、不碰 agent）
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        jj = j.Journal(tmp, None)
        jj.add_event(j.eod_snapshot_event("600519.SH", 10.0, 10.5, 5.0, None, None))
        class _Store:
            root = Path(tmp)

            def kline(self, code, start=None, end=None):
                return {"dates": ["2026-10-26"], "close": [10.5]}
        r = j.build_daily_review(jj, _Store(), "600519.SH", "2026-10-26")
        assert r is not None and r["version"] == "rule"


def main() -> int:
    check("A. 规则面/数据面不引用智能体层", t_rule_a_rule_layer_clean)
    check("B. agent 引用者限于白名单", t_rule_b_agent_importers_whitelisted)
    check("C. agent 只向下依赖", t_rule_c_agent_downward_only)
    check("D. 无裸 LLM HTTP 调用（agent 外）", t_rule_d_no_bare_llm_http)
    check("E. 运行时隔离（journal 纯规则 / 壳同源）", t_rule_e_runtime_isolation)
    print(f"\n===== 汇总: {PASSED} PASS / {FAILED} FAIL =====")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
