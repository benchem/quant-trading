"""个股 AI 综合分析（analyzer.ai）自测（全部 mock，无需网络/真实 API key）。

运行: .venv/bin/python tests/test_ai_analysis.py

覆盖：
- collect_context 字段裁剪、表格截尾、插件/新闻失败降级为 warnings
- _fit_context 超长裁剪优先级、build_prompt 内容
- chat_completion 成功/401/429/500/超时/未配置 key
- generate 缓存命中、refresh 强制重建、API key 不出现在返回值与缓存文件
"""
from __future__ import annotations

import json
import sys
import tempfile
import traceback
from datetime import date
from pathlib import Path
from unittest import mock

import pandas as pd
import requests

sys.path.insert(0, ".")

from analyzer import ai, overseas  # noqa: E402
from stock_analysis import AnalysisResult  # noqa: E402

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


# ------------------------------------------------------------------ 测试替身


class _FakeRegistry:
    def info(self, plugin):
        return {"display_name": f"模型-{plugin}"}


class _FakeRunner:
    def __init__(self, fail=(), raise_=()):
        self.registry = _FakeRegistry()
        self.fail = set(fail)
        self.raise_ = set(raise_)

    def run(self, plugin, code, **params):
        r = AnalysisResult(plugin, code)
        if plugin in self.raise_:
            raise RuntimeError("boom")
        if plugin in self.fail:
            return r.fail("数据不足")
        r.score = 66.6
        r.verdict = "偏强"
        r.summary = f"{plugin} 结论"
        r.metric("close", 10.4, "最新收盘")
        r.metric("score_z", 1.23)
        r.signal("ok", f"{plugin} 信号")
        r.add_series("drawdown", [1, 2], [0.1, 0.2])  # 图表序列不应进入 AI 上下文
        r.add_table("signals", pd.DataFrame({
            "date": [f"2026-01-{i:02d}" for i in range(1, 13)],
            "ret": list(range(12)),
        }))
        r.extra["note"] = "extra"
        return r


class _FakeStore:
    def __init__(self, root, news_error=False):
        self.root = Path(root)
        self.news_error = news_error

    def kline(self, code):
        return {"code": code, "name": "测试银行",
                "dates": ["2026-10-20", "2026-10-21", "2026-10-22"],
                "close": [10.2, 10.3, 10.4], "real_price": 10.4,
                "total_mv": 1e11}

    def news(self, code):
        if self.news_error:
            raise RuntimeError("网络异常")
        return {"stale": False, "items": [
            {"title": f"新闻标题{i}", "time": "2026-10-22 10:00",
             "source": "东方财富", "digest": "不应上送的摘要"} for i in range(12)
        ]}


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload
        self.text = "" if payload is None else json.dumps(payload, ensure_ascii=False)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _ok_resp(text="### 综合结论\n测试报告"):
    return _Resp(200, {"choices": [{"message": {"content": text}}],
                       "usage": {"total_tokens": 42}})


_CONFIG = ai.AIConfig(base_url="https://example.test/v1", api_key="sk-test-secret",
                      model="test-model", timeout=1)


def _write_brief(root, day="2026-10-22", status="有效", score="+35"):
    path = Path(root) / "news" / "us_market" / f"{day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""# 每日美股简报
状态：{status}
简报日期：{day}
风险偏好分数：{score} / 100
情绪判断：偏多
置信度：中

## 核心要闻
- 纳指上涨，半导体板块领涨（来源：示例财经媒体）
- 美债收益率回落，风险偏好改善

## 自选股映射
- 600519.SH：消费情绪间接映射
- 300687.SZ：科技硬件供应链映射
- 科技板块：通用行业情绪映射

## 风险提示
- 隔夜信号可能在 A 股开盘前反转
""", encoding="utf-8")
    return path


# ------------------------------------------------------------------ 用例


def t_overseas_brief():
    with tempfile.TemporaryDirectory() as tmp:
        day = date(2026, 10, 22)
        _write_brief(tmp)
        valid = overseas.load_daily_brief(tmp, "600519.SH", on_date=day)
        assert valid["status"] == "valid" and valid["risk_score"] == 35
        assert valid["risk_view"] == "偏多" and valid["confidence"] == "中"
        assert len(valid["headlines"]) == 2
        assert len(valid["watchlist_mapping"]) == 2
        assert all("300687.SZ" not in x for x in valid["watchlist_mapping"])

        other = overseas.load_daily_brief(tmp, "300750.SZ", on_date=day)
        assert other["watchlist_mapping"] == ["科技板块：通用行业情绪映射"]

        stale = overseas.load_daily_brief(tmp, "600519.SH", on_date=date(2026, 10, 23))
        assert stale["status"] == "unavailable" and "当日简报缺失" in stale["message"]
        assert stale["risk_score"] is None and stale["headlines"] == []

        _write_brief(tmp, day="2026-10-24")
        weekend = overseas.load_daily_brief(tmp, "600519.SH", on_date=date(2026, 10, 24))
        assert weekend["status"] == "unavailable" and "周末" in weekend["message"]
        assert weekend["headlines"] == []

        for status in ("无有效海外信号（休市）", "获取失败"):
            _write_brief(tmp, status=status)
            invalid = overseas.load_daily_brief(tmp, "600519.SH", on_date=day)
            assert invalid["status"] == "unavailable" and invalid["risk_score"] is None
            assert invalid["headlines"] == [] and invalid["watchlist_mapping"] == []

        _write_brief(tmp, score="+101")
        assert overseas.load_daily_brief(tmp, "600519.SH", on_date=day)["status"] == "unavailable"
        path = _write_brief(tmp)
        path.write_text(path.read_text(encoding="utf-8").replace("状态：有效\n", ""),
                        encoding="utf-8")
        unmarked = overseas.load_daily_brief(tmp, "600519.SH", on_date=day)
        assert unmarked["status"] == "unavailable" and "未明确标记" in unmarked["message"]


def t_collect_context():
    with tempfile.TemporaryDirectory() as tmp:
        store = _FakeStore(tmp)
        runner = _FakeRunner(fail={"momentum"}, raise_={"risk_profile"})
        ctx, warns = ai.collect_context(store, runner, "600000.SH")
        assert ctx["code"] == "600000.SH" and ctx["name"] == "测试银行"
        assert ctx["as_of"] == "2026-10-22"
        assert ctx["market"]["latest_close"] == 10.4
        assert len(ctx["models"]) == len(ai.DEFAULT_PLUGINS)
        ok = [m for m in ctx["models"] if m["ok"]]
        assert len(ok) == len(ai.DEFAULT_PLUGINS) - 2
        for m in ok:
            assert "series" not in m and "elapsed_ms" not in m
            assert m["verdict"] == "偏强" and m["name"].startswith("模型-")
            assert len(m["tables"]["signals"]) == ai.MAX_TABLE_ROWS
            assert m["tables"]["signals"][0]["date"] == "2026-01-05"  # 末尾 8 行
            assert m["metric_labels"]["close"] == "最新收盘"
        assert len(ctx["news"]) == ai.MAX_NEWS
        assert all("digest" not in it for it in ctx["news"])
        assert ctx["overseas"]["status"] == "unavailable"
        assert ctx["overseas"]["risk_score"] is None
        assert sum("不可用" in w for w in warns) == 2, warns
        assert any("无有效海外信号" in w for w in warns)

        # 新闻失败只降级为 warning，不阻塞
        ctx2, warns2 = ai.collect_context(_FakeStore(tmp, news_error=True), _FakeRunner(), "600000.SH")
        assert ctx2["news"] == [] and any("新闻不可用" in w for w in warns2)
        # 无日线数据 -> 404 AIError
        try:
            bad = _FakeStore(tmp)
            bad.kline = lambda code: None
            ai.collect_context(bad, _FakeRunner(), "600000.SH")
            raise AssertionError("无日线应当报错")
        except ai.AIError as e:
            assert e.status == 404


def t_fit_and_prompt():
    with tempfile.TemporaryDirectory() as tmp:
        ctx, warns = ai.collect_context(_FakeStore(tmp), _FakeRunner(), "600000.SH")
        for m in ctx["models"]:
            for i in range(60):
                m["metrics"][f"padding_metric_{i}"] = "x" * 200
            m["tables"]["big"] = [{"text": "y" * 500} for _ in range(20)]
        assert len(ai._context_json(ctx)) > ai.MAX_CONTEXT_CHARS
        fit = ai._fit_context(ctx)
        assert len(ai._context_json(fit)) <= ai.MAX_CONTEXT_CHARS
        assert all("tables" not in m for m in fit["models"])  # 表格最先被裁剪
        assert len(ai._context_json(ctx)) > ai.MAX_CONTEXT_CHARS  # 原对象不被改动

        messages = ai.build_prompt(fit, warns)
        assert [m["role"] for m in messages] == ["system", "user"]
        user = messages[1]["content"]
        for needle in ("600000.SH", "测试银行", "2026-10-22", "偏强", "### 综合结论", "### 风险提示"):
            assert needle in user, needle
        assert "不得编造" in messages[0]["content"]
        assert "禁止把不同模型" in messages[0]["content"]  # score 不可跨模型直接比较
        assert "数据质量提示：无有效海外信号" in user
        assert "无有效海外信号" in messages[0]["content"]

        # 数据质量警告注入 prompt：模型能感知新闻/模型异常，避免把"无新闻"误读为"无消息"
        user_w = ai.build_prompt(fit, ["新闻不可用: 网络异常"])[1]["content"]
        assert "数据质量提示：新闻不可用: 网络异常" in user_w


def t_chat_completion():
    messages = [{"role": "user", "content": "hi"}]
    with mock.patch("analyzer.ai.requests.post") as post:
        post.return_value = _ok_resp()
        out = ai.chat_completion(_CONFIG, messages)
        assert out["content"].startswith("### 综合结论")
        assert out["usage"]["total_tokens"] == 42
        args, kwargs = post.call_args
        assert args[0] == "https://example.test/v1/chat/completions"
        assert kwargs["json"]["model"] == "test-model"
        assert kwargs["json"]["stream"] is False
        assert kwargs["headers"]["Authorization"] == "Bearer sk-test-secret"
        assert "sk-test-secret" not in json.dumps(out)

        for status, needle in ((401, "认证失败"), (429, "限流")):
            post.return_value = _Resp(status, {"error": {"message": "bad"}})
            try:
                ai.chat_completion(_CONFIG, messages)
                raise AssertionError(f"HTTP {status} 应当报错")
            except ai.AIError as e:
                assert needle in str(e) and "sk-test-secret" not in str(e)

        post.return_value = _Resp(500, {"error": {"message": "server boom"}})
        try:
            ai.chat_completion(_CONFIG, messages)
            raise AssertionError("HTTP 500 应当报错")
        except ai.AIError as e:
            assert "500" in str(e) and "server boom" in str(e)

        post.side_effect = requests.Timeout()
        try:
            ai.chat_completion(_CONFIG, messages)
            raise AssertionError("超时应当报错")
        except ai.AIError as e:
            assert "超时" in str(e)

    try:
        ai.chat_completion(ai.AIConfig(), messages)
        raise AssertionError("未配置 key 应当报错")
    except ai.AIError as e:
        assert e.status == 503


def t_generate_cache():
    with tempfile.TemporaryDirectory() as tmp:
        store = _FakeStore(tmp)
        runner = _FakeRunner(fail={"momentum"})
        with mock.patch("analyzer.ai.requests.post") as post:
            post.return_value = _ok_resp()
            r1 = ai.generate(store, runner, "600000.SH", config=_CONFIG)
            assert r1["cached"] is False and post.call_count == 1
            assert ai.DISCLAIMER in r1["report"]
            assert r1["model"] == "test-model"
            assert r1["context_meta"]["models_ok"] == len(ai.DEFAULT_PLUGINS) - 1
            assert r1["context_meta"]["models_failed"] == 1
            assert r1["context_meta"]["news_count"] == ai.MAX_NEWS
            assert any("momentum" in w for w in r1["warnings"])

            r2 = ai.generate(store, runner, "600000.SH", config=_CONFIG)
            assert r2["cached"] is True and post.call_count == 1  # 第二次零 HTTP
            assert r2["report"] == r1["report"]

            r3 = ai.generate(store, runner, "600000.SH", refresh=True, config=_CONFIG)
            assert r3["cached"] is False and post.call_count == 2

            # key 不出现在返回值与缓存文件中
            assert "sk-test-secret" not in json.dumps(r3, ensure_ascii=False)
            cache_file = Path(tmp) / "ai" / "600000.SH.json"
            assert cache_file.exists()
            assert "sk-test-secret" not in cache_file.read_text(encoding="utf-8")

            # 上下文变化（新闻更新）使旧缓存失效
            store.news = lambda code: {"stale": False, "items": [
                {"title": "全新新闻", "time": "2026-10-23 09:00", "source": "东方财富"}]}
            r4 = ai.generate(store, runner, "600000.SH", config=_CONFIG)
            assert r4["cached"] is False and post.call_count == 3
            assert r4["context_meta"]["overseas_status"] == "unavailable"

            # 海外简报变化同样进入上下文指纹，并使旧报告缓存失效
            valid = {"status": "valid", "date": "2026-10-23", "risk_score": 35,
                     "risk_view": "偏多", "confidence": "中",
                     "headlines": ["半导体领涨"], "watchlist_mapping": [],
                     "message": "当日海外信号有效"}
            with mock.patch("analyzer.ai.load_daily_brief", return_value=valid):
                r5 = ai.generate(store, runner, "600000.SH", config=_CONFIG)
            assert r5["cached"] is False and post.call_count == 4
            assert r5["context_meta"]["overseas_status"] == "valid"
            assert r5["context_meta"]["overseas_risk_score"] == 35


def t_generate_disabled():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            ai.generate(_FakeStore(tmp), _FakeRunner(), "600000.SH", config=ai.AIConfig())
            raise AssertionError("未配置 key 应当报错")
        except ai.AIError as e:
            assert e.status == 503 and "AI_API_KEY" in str(e)


if __name__ == "__main__":
    check("美股简报有效性、提取与个股映射", t_overseas_brief)
    check("collect_context 聚合与降级", t_collect_context)
    check("上下文裁剪与 Prompt", t_fit_and_prompt)
    check("chat_completion 错误处理", t_chat_completion)
    check("generate 缓存与密钥安全", t_generate_cache)
    check("未配置 key 停用", t_generate_disabled)
    print(f"\n{PASSED} 通过 / {FAILED} 失败")
    sys.exit(1 if FAILED else 0)
