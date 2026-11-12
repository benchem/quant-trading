"""问股 Agent：多轮会话 + 策略注入 + 本地工具 ReAct 循环。

吸收自 DSA daily_stock_analysis 的 agent chat（链路设计见
/root/.hermes/cache/scratch/absorb_agent_chat.md）：
- ReAct 循环结构抄 runner.py:326（砍 provider trace/并行池/cancel）；
- system prompt 骨架抄 executor.py:423-458 四阶段工作流；
- 策略 instructions 注入抄 executor.py:536-541（skills_section）；
- 意图层两级裁剪（正则 code/名称匹配 + active_code 继承，DSA
  chat_object_decision 的 maintain/switch/compare 语义）；
- 预算守卫：max_steps=6、总预算 90s、剩余<10s 时 BUDGET_SKIP（学
  runner.py:381 _MIN_STEP_BUDGET_S 语义——不再发起注定超时的调用）。

会话存储：data/agent_chat/{user}/{session_id}.json（不引数据库）；
多轮上下文：尾 20 条（DSA 关闭压缩时的降级策略 chat_context.py:252-258）。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
import time
import uuid
from pathlib import Path

from .llm import AIConfig, AIError, chat_completion
from . import tools as agent_tools

MAX_STEPS = 6
CHAT_TIMEOUT_S = 90.0          # 总预算（独立于报告用 AI_TIMEOUT）
MIN_STEP_BUDGET_S = 10.0       # 剩余低于此值不再发起工具轮，要求直接作答
HISTORY_TAIL = 20              # 多轮历史截尾（DSA 降级策略同款）
STRATEGIES_DIR = Path(__file__).resolve().parents[2] / "strategies"

# DSA executor.py:423-458 CHAT_SYSTEM_PROMPT 骨架（工具名换我方，删技能切换段）
CHAT_SYSTEM_PROMPT = """你是一位严谨的 A 股分析助手，服务单个投资者的自选股研究。

## 工作流程（四阶段）
1. **理解问题**：确认用户问的标的与关注点；模糊时先调用工具取数据再判断，不要凭空假设。
2. **收集数据**：按需调用工具（行情/K线/技术面/筹码/基本面/资金流/板块/大盘/新闻/持仓）。
   每轮最多调用必要的工具；数据已在历史消息中的不要重复取。
3. **分析**：结合所选策略框架与数据给出结构化分析：趋势判断、关键价位、量价配合、风险点。
4. **回答**：中文，条理清晰，先结论后依据；引用具体数字（价格、涨跌幅、比例）。

## 硬规则
- **禁止编造数据**：所有数字必须来自工具返回或用户消息；工具失败/无数据时如实说明并继续分析其余部分。
- 不做收益承诺，不给确定性买卖指令；给条件化建议（"若站稳X则…，跌破Y则…"）。
- 数据时效：若 K 线末根日期明显落后，须在回答中声明数据截止日。
- 回答保持精炼（500字内），重点突出。"""

# DSA defaults.py CORE_TRADING_SKILL_POLICY_ZH 精简版（默认技能基线）
CORE_POLICY_ZH = """## 默认交易纪律基线（未选策略时也生效）
- 严进：只在量价与趋势共振时给积极评价；追高（乖离 MA20 > 8%）须提示风险。
- 趋势优先：均线多头排列 + 放量为积极；空头排列下反弹按弱信号处理。
- 回踩买点优于突破追买；跌破前低/关键支撑必须提示止损位。
- 利空（业绩下滑、高位放量滞涨）一票否决多头叙事。"""

_STRATEGY_CACHE: dict | None = None
_STRATEGY_LOCK = threading.Lock()


def load_strategies() -> dict[str, dict]:
    """加载 strategies/*.yaml（DSA 15 个策略，MIT，零改动 COPY）。"""
    global _STRATEGY_CACHE
    with _STRATEGY_LOCK:
        if _STRATEGY_CACHE is not None:
            return _STRATEGY_CACHE
        import yaml  # downloader 已依赖 PyYAML

        out: dict[str, dict] = {}
        p = STRATEGIES_DIR
        if p.is_dir():
            for f in sorted(p.glob("*.yaml")):
                try:
                    d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                except Exception:  # noqa: BLE001 坏文件跳过
                    continue
                name = str(d.get("name") or f.stem)
                d["_file"] = f.name
                out[name] = d
        _STRATEGY_CACHE = out
        return out


def strategy_section(strategy: str | None) -> str:
    """DSA executor.py:536-541 skills_section 同构：选中策略 instructions 注入。"""
    if not strategy:
        return CORE_POLICY_ZH
    s = load_strategies().get(strategy)
    if not s or not s.get("instructions"):
        return CORE_POLICY_ZH
    return (f"## 激活的分析策略：{s.get('display_name') or strategy}\n\n"
            f"{s['instructions']}\n\n---\n{CORE_POLICY_ZH}")


# ---------------------------------------------------------------- 意图层

_CODE_RE = re.compile(r"\b(\d{6})(?:\.(SH|SZ|BJ))?\b", re.IGNORECASE)
_SWITCH_RE = re.compile(r"换成|切换到|看看(.*)吧|改为")
_COMPARE_RE = re.compile(r"对比|比较|和.{1,12}比|哪个好|vs", re.IGNORECASE)


def detect_codes(text: str, store) -> list[str]:
    """正则识别消息中的股票代码 + 自选股/全市场名称匹配（两级裁剪的规则层）。"""
    out: list[str] = []
    for m in _CODE_RE.finditer(text or ""):
        num, suf = m.group(1), (m.group(2) or "").upper()
        if not suf:
            suf = "SH" if num.startswith(("6", "9", "5")) else \
                  ("BJ" if num.startswith(("4", "8")) else "SZ")
        code = f"{num}.{suf}"
        if code not in out:
            out.append(code)
    # 自选股名称精确匹配（基本名词表有限，防误伤）
    try:
        for code in store.watchlist_codes_all:
            name = ""
            try:
                name = str(store.basic.loc[code, "name"])
            except Exception:  # noqa: BLE001
                continue
            if name and name in (text or "") and code not in out:
                out.append(code)
    except Exception:  # noqa: BLE001
        pass
    return out


def resolve_intent(message: str, session: dict, store) -> dict:
    """DSA chat_object_decision 语义裁剪：maintain/switch/compare + active_code 继承。"""
    codes = detect_codes(message, store)
    active = session.get("active_code")
    compare = bool(_COMPARE_RE.search(message or ""))
    if len(codes) >= 2 or (compare and codes):
        return {"mode": "compare", "codes": codes[:3], "active_code": codes[0]}
    if codes:
        # 首轮（无 active）为 new；换标的为 switch（DSA chat_object_decision 语义）
        if not active:
            return {"mode": "new", "codes": codes, "active_code": codes[0]}
        switched = codes[0] != active
        return {"mode": "switch" if switched else "maintain", "codes": codes,
                "active_code": codes[0]}
    # 无新标的：继承会话标的；代词（它/这只）不失效（DSA 负向不失效语义）
    return {"mode": "maintain" if active else "new", "codes": [],
            "active_code": active}


# ---------------------------------------------------------------- 会话

class ChatSession:
    """data/agent_chat/{user}/{session_id}.json；进程内锁对齐并发提交语义。"""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        d.setdefault("id", self.path.stem)
        d.setdefault("messages", [])
        d.setdefault("active_code", None)
        d.setdefault("strategy", None)
        d.setdefault("created", dt.datetime.now().isoformat(timespec="seconds"))
        d["updated"] = dt.datetime.now().isoformat(timespec="seconds")
        return d

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.path)


def _session_path(root, user: str | None, session_id: str) -> Path:
    base = Path(root) / "agent_chat" / (user or "_shared")
    return base / f"{session_id}.json"


# ---------------------------------------------------------------- 主循环

def run_chat_turn(store, user: str | None, session_id: str | None,
                  message: str, strategy: str | None = None,
                  config: AIConfig | None = None,
                  on_event=None) -> dict:
    """一轮问股：意图 → system+策略 → ReAct(≤6步) → 写回会话。

    返回 {session_id, reply, active_code, mode, steps, elapsed, usage}。
    on_event(step, name) 可选进度回调（前端"正在获取K线…"）。
    """
    t0 = time.time()
    config = config or AIConfig.from_env()
    # 持仓等按登录用户分桶（get_positions 无显式 user 时回落）
    try:
        store._agent_user = user
    except Exception:  # noqa: BLE001 只读 store 视为共享
        pass
    session_id = session_id or uuid.uuid4().hex[:12]
    sess = ChatSession(_session_path(store.root, user, session_id))
    with sess.lock:
        data = sess.load()
    # 策略：显式传入 > 会话继承（DSA skills 会话级持久化语义）
    if strategy is not None:
        data["strategy"] = strategy or None
    strategy = data.get("strategy")

    intent = resolve_intent(message, data, store)
    data["active_code"] = intent["active_code"]

    system = (f"{CHAT_SYSTEM_PROMPT}\n\n{strategy_section(strategy)}\n\n"
              f"当前登录用户：{user or '共享模式'}（查持仓无需传 user 参数）。\n"
              f"当前讨论标的：{intent['active_code'] or '（未指定，可请用户明确）'}")
    history = data["messages"][-HISTORY_TAIL:]
    messages: list[dict] = [{"role": "system", "content": system}, *history,
                            {"role": "user", "content": message}]

    steps = 0
    usage_total = 0
    reply = ""
    while steps < MAX_STEPS:
        remaining = CHAT_TIMEOUT_S - (time.time() - t0)
        if remaining <= 0:
            break
        resp = chat_completion(config, messages, tools=agent_tools.TOOL_SPECS)
        usage_total += (resp.get("usage") or {}).get("total_tokens") or 0
        calls = resp.get("tool_calls") or []
        if not calls:
            reply = resp["content"]
            break
        # 预算守卫：剩余不足以再跑一轮工具+回答 → 要求直接作答
        if remaining < MIN_STEP_BUDGET_S:
            messages.append({"role": "user", "content":
                             "（系统提示：剩余时间不足，请立即基于已获取的数据给出最终回答，不要再调用工具）"})
            resp = chat_completion(config, messages)  # 不带 tools
            usage_total += (resp.get("usage") or {}).get("total_tokens") or 0
            reply = resp["content"]
            break
        messages.append({"role": "assistant", "content": resp["content"] or "",
                         "tool_calls": [{"id": c["id"], "type": "function",
                                         "function": {"name": c["name"],
                                                      "arguments": c["arguments"]}}
                                        for c in calls]})
        for c in calls:
            if on_event:
                try:
                    on_event(steps, c["name"])
                except Exception:  # noqa: BLE001
                    pass
            result = agent_tools.execute_tool(store, c["name"], c["arguments"])
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": result[:8000]})
        steps += 1
    else:
        # max_steps 打满：强制收口（DSA runner.py:589-601 语义）
        messages.append({"role": "user", "content":
                         "（系统提示：已达到工具调用步数上限，请基于以上全部数据立即给出最终回答）"})
        resp = chat_completion(config, messages)
        usage_total += (resp.get("usage") or {}).get("total_tokens") or 0
        reply = resp["content"]

    # 写回会话（assistant 失败也要落 user 侧，DSA [分析失败] 语义）
    now = dt.datetime.now().isoformat(timespec="seconds")
    data["messages"].append({"role": "user", "content": message, "ts": now})
    data["messages"].append({"role": "assistant", "content": reply, "ts": now,
                             "strategy": strategy, "steps": steps})
    data["messages"] = data["messages"][-200:]  # 会话文件防膨胀
    with sess.lock:
        sess.save(data)
    return {"session_id": session_id, "reply": reply,
            "active_code": intent["active_code"], "mode": intent["mode"],
            "steps": steps, "elapsed": round(time.time() - t0, 1),
            "usage": usage_total}
