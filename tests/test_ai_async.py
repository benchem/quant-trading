"""个股 AI 分析异步任务调度（analyzer.agent.jobs）+ HTTP 接口契约测试。

全部离线：LLM 调用经 mock / 注入假 generate_fn，不触网。

运行: .venv/bin/pytest tests/test_ai_async.py -q

覆盖（ai-ux-brief 任务3）：
- 缓存命中 -> submit 同步返回报告（零 LLM 调用），且 peek 与 generate 缓存规则一致
- 未命中 -> 返回 job（job_id/queued/status），后台执行
- status 状态机：queued -> running -> done（带完整报告 payload）/ failed（错误透传）
- 同 code 去重（重复 submit 返回已有 job，不重复扣费）
- 全局并发上限（超出排队）、排队上限（超出拒绝）、完成任务数修剪
- 未配置 AI_API_KEY 提交即拒绝
"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, ".")

from analyzer import ai  # noqa: E402
from analyzer.agent import AIError  # noqa: E402
from analyzer.server import make_handler  # noqa: E402
from test_ai_analysis import (_CONFIG, _FakeRunner, _FakeStore,  # noqa: E402
                              _ok_resp, _patch_extra_data)

# ------------------------------------------------------------------ 工具


def _payload(code: str) -> dict:
    """假报告 payload（镜像 generate() 返回结构的关键字段）。"""
    return {"code": code, "model": "test-model",
            "created": "2026-10-31 12:00:00", "created_ts": 1_800_000_000.0,
            "fingerprint": "fp-" + code, "report": f"### 综合结论\n{code} 测试报告",
            "warnings": [], "usage": {"total_tokens": 42}, "cached": False}


def _wait_until(fn, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.01)
    return False


def _mk_mgr(store, runner, generate_fn, **kw) -> "AIJobManager":
    from analyzer.agent.jobs import AIJobManager

    kw.setdefault("config_provider", lambda: _CONFIG)
    return AIJobManager(store, runner, generate_fn=generate_fn, **kw)


# ------------------------------------------------------------------ 缓存命中（同步秒回）


def test_submit_cache_hit_returns_cached_report_without_llm():
    """缓存命中：submit 直接返回 generate() 写下的报告，且不再调 LLM。

    捕捉的破坏：peek 与 generate 缓存规则不一致（指纹/TTL/路径任一不同），
    或命中路径被误改成入队轮询。
    """
    with tempfile.TemporaryDirectory() as tmp, _patch_extra_data():
        store, runner = _FakeStore(tmp), _FakeRunner()
        with mock.patch("analyzer.ai.requests.post") as post:
            post.return_value = _ok_resp()
            r1 = ai.generate(store, runner, "600000.SH", config=_CONFIG)
        assert r1["cached"] is False

        def boom(*a, **kw):
            raise AssertionError("缓存命中时不得调用大模型")

        mgr = _mk_mgr(store, runner, boom)
        out = mgr.submit("600000.SH")
        assert out["cached"] is True
        assert out["report"] == r1["report"]
        # 没有创建任务：按 code 查询无事发生
        assert mgr.status(code="600000.SH")["status"] == "none"


def test_peek_respects_ttl_expiry():
    """缓存指纹一致但已过 TTL：不得同步返回，应创建任务重跑。"""
    with tempfile.TemporaryDirectory() as tmp, _patch_extra_data():
        store, runner = _FakeStore(tmp), _FakeRunner()
        with mock.patch("analyzer.ai.requests.post") as post:
            post.return_value = _ok_resp()
            ai.generate(store, runner, "600000.SH", config=_CONFIG)
        cache_file = Path(tmp) / "ai" / "600000.SH.json"
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        data["created_ts"] = time.time() - 7 * 3600   # 6h TTL 已过
        cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        mgr = _mk_mgr(store, runner, lambda *a, **kw: _payload("600000.SH"))
        out = mgr.submit("600000.SH")
        assert out.get("job_id"), "过期缓存应走异步任务而不是同步返回"
        assert _wait_until(lambda: mgr.status(job_id=out["job_id"])["status"] == "done")


# ------------------------------------------------------------------ 未命中 -> job + 状态机


def test_submit_cache_miss_returns_job_and_runs():
    """未命中：返回 job 信息（job_id/queued/status/code），后台完成生成。"""
    with tempfile.TemporaryDirectory() as tmp, _patch_extra_data():
        calls = []

        def gen(store, runner, code, refresh=False, config=None):
            calls.append((code, refresh))
            return _payload(code)

        mgr = _mk_mgr(_FakeStore(tmp), _FakeRunner(), gen)
        out = mgr.submit("600000.SH")
        assert out["job_id"] and out["code"] == "600000.SH"
        assert out["queued"] is True and out["status"] in ("queued", "running")

        assert _wait_until(lambda: mgr.status(job_id=out["job_id"])["status"] == "done")
        assert calls == [("600000.SH", False)]
        st = mgr.status(code="600000.SH")
        assert st["job_id"] == out["job_id"]
        assert st["report"]["report"] == "### 综合结论\n600000.SH 测试报告"


def test_status_lifecycle_running_to_done():
    """状态机：running（started_at 已置）-> done（report/finished_at 落位，时间有序）。"""
    with tempfile.TemporaryDirectory() as tmp, _patch_extra_data():
        entered, release = threading.Event(), threading.Event()

        def gen(store, runner, code, refresh=False, config=None):
            entered.set()
            release.wait(5)
            return _payload(code)

        mgr = _mk_mgr(_FakeStore(tmp), _FakeRunner(), gen)
        out = mgr.submit("600000.SH")
        assert entered.wait(5)
        st = mgr.status(job_id=out["job_id"])
        assert st["status"] == "running" and st["started_at"] is not None
        assert st["finished_at"] is None

        release.set()
        assert _wait_until(lambda: mgr.status(job_id=out["job_id"])["status"] == "done")
        st = mgr.status(code="600000.SH")
        assert st["status"] == "done" and st["report"]["code"] == "600000.SH"
        assert st["finished_at"] >= st["started_at"] >= st["queued_at"]


# ------------------------------------------------------------------ 同 code 去重


def test_same_code_dedup_while_running():
    """同一 code 在途时重复 submit：返回同一 job（dedup 标记），LLM 只调一次。"""
    with tempfile.TemporaryDirectory() as tmp, _patch_extra_data():
        calls = []
        release = threading.Event()

        def gen(store, runner, code, refresh=False, config=None):
            calls.append(code)
            release.wait(5)
            return _payload(code)

        mgr = _mk_mgr(_FakeStore(tmp), _FakeRunner(), gen)
        out1 = mgr.submit("600000.SH")
        out2 = mgr.submit("600000.SH")
        assert out2["job_id"] == out1["job_id"] and out2.get("dedup") is True
        # 不同 code 不受去重影响
        out3 = mgr.submit("000001.SZ")
        assert out3["job_id"] != out1["job_id"]

        release.set()
        assert _wait_until(lambda: mgr.status(code="000001.SZ")["status"] == "done")
        assert _wait_until(lambda: mgr.status(code="600000.SH")["status"] == "done")
        assert sorted(calls) == ["000001.SZ", "600000.SH"]


# ------------------------------------------------------------------ 失败透传


def test_aierror_failure_propagates_to_status():
    """generate 抛 AIError：status=failed，错误文案与 HTTP 语义状态码透传。"""
    with tempfile.TemporaryDirectory() as tmp, _patch_extra_data():

        def gen(store, runner, code, refresh=False, config=None):
            raise AIError("大模型限流（429）：请求过于频繁或额度不足", 429)

        mgr = _mk_mgr(_FakeStore(tmp), _FakeRunner(), gen)
        out = mgr.submit("600000.SH")
        assert _wait_until(lambda: mgr.status(job_id=out["job_id"])["status"] == "failed")
        st = mgr.status(code="600000.SH")
        assert st["status"] == "failed" and st["report"] is None
        assert "限流" in st["error"] and st["error_status"] == 429


def test_unexpected_exception_marks_failed():
    """generate 抛未知异常：标记 failed（不崩溃 worker），给通用文案。"""
    with tempfile.TemporaryDirectory() as tmp, _patch_extra_data():

        def gen(store, runner, code, refresh=False, config=None):
            raise RuntimeError("boom")

        mgr = _mk_mgr(_FakeStore(tmp), _FakeRunner(), gen)
        out = mgr.submit("600000.SH")
        assert _wait_until(lambda: mgr.status(job_id=out["job_id"])["status"] == "failed")
        st = mgr.status(job_id=out["job_id"])
        assert st["status"] == "failed" and st["error"] and st["error_status"] == 500
        # worker 存活：后续任务仍能完成
        mgr2_out = mgr.submit("000001.SZ")
        assert _wait_until(lambda: mgr.status(job_id=mgr2_out["job_id"])["status"] == "failed")


# ------------------------------------------------------------------ 并发与排队上限


def test_global_concurrency_capped_at_max_workers():
    """全局并发不超过 max_workers=2：前两个 running 时，后续必须 queued。"""
    with tempfile.TemporaryDirectory() as tmp, _patch_extra_data():
        guard, stats, release = threading.Lock(), {"cur": 0, "max": 0}, threading.Event()

        def gen(store, runner, code, refresh=False, config=None):
            with guard:
                stats["cur"] += 1
                stats["max"] = max(stats["max"], stats["cur"])
            release.wait(5)
            with guard:
                stats["cur"] -= 1
            return _payload(code)

        mgr = _mk_mgr(_FakeStore(tmp), _FakeRunner(), gen, max_workers=2)
        outs = [mgr.submit(c) for c in ("600000.SH", "000001.SZ", "600519.SH", "300750.SZ")]
        assert _wait_until(lambda: stats["max"] >= 2)
        assert mgr.status(job_id=outs[2]["job_id"])["status"] == "queued"
        assert mgr.status(job_id=outs[3]["job_id"])["status"] == "queued"

        release.set()
        assert _wait_until(lambda: all(mgr.status(job_id=o["job_id"])["status"] == "done"
                                       for o in outs))
        assert stats["max"] == 2, "并发从未超过上限 2"


def test_pending_queue_cap_rejects_with_429():
    """排队上限：queued 任务数达 max_pending 后，新 submit 被拒（429 语义）。"""
    with tempfile.TemporaryDirectory() as tmp, _patch_extra_data():
        entered, release = threading.Event(), threading.Event()

        def gen(store, runner, code, refresh=False, config=None):
            entered.set()
            release.wait(5)
            return _payload(code)

        mgr = _mk_mgr(_FakeStore(tmp), _FakeRunner(), gen, max_workers=1, max_pending=2)
        a = mgr.submit("600000.SH")
        assert entered.wait(5)               # 唯一 worker 已被占用
        b = mgr.submit("000001.SZ")          # queued 1
        c = mgr.submit("600519.SH")          # queued 2（达上限）
        with pytest.raises(AIError) as ei:
            mgr.submit("300750.SZ")
        assert ei.value.status == 429

        release.set()
        for o in (a, b, c):
            assert _wait_until(lambda: mgr.status(job_id=o["job_id"])["status"] == "done")


# ------------------------------------------------------------------ 配置与查询边界


def test_submit_rejected_when_ai_disabled():
    """未配置 AI_API_KEY：submit 即拒绝（503），不创建任务。"""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _mk_mgr(_FakeStore(tmp), _FakeRunner(),
                      lambda *a, **kw: _payload("600000.SH"),
                      config_provider=lambda: ai.AIConfig())
        with pytest.raises(AIError) as ei:
            mgr.submit("600000.SH")
        assert ei.value.status == 503 and "AI_API_KEY" in str(ei.value)
        assert mgr.status(code="600000.SH")["status"] == "none"


def test_status_none_for_code_without_jobs():
    """无任务查询：按 code 返回 none；按未知 job_id 返回 None（404 语义）。"""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _mk_mgr(_FakeStore(tmp), _FakeRunner(), lambda *a, **kw: _payload("600000.SH"))
        st = mgr.status(code="600000.SH")
        assert st["status"] == "none" and st["code"] == "600000.SH"
        assert mgr.status(job_id="no-such-job") is None


def test_finished_jobs_pruned_beyond_cap():
    """完成任务只保留最近 max_finished 个（内存有界），被清掉的按 code 查询回到 none。"""
    with tempfile.TemporaryDirectory() as tmp, _patch_extra_data():
        mgr = _mk_mgr(_FakeStore(tmp), _FakeRunner(),
                      lambda s, r, code, refresh=False, config=None: _payload(code),
                      max_finished=2)
        outs = []
        for c in ("600000.SH", "000001.SZ", "600519.SH", "300750.SZ"):
            o = mgr.submit(c)
            assert _wait_until(lambda: mgr.status(job_id=o["job_id"])["status"] == "done")
            outs.append(o)
        assert mgr.status(job_id=outs[0]["job_id"]) is None
        assert mgr.status(job_id=outs[1]["job_id"]) is None
        assert mgr.status(job_id=outs[2]["job_id"])["status"] == "done"
        assert mgr.status(code="600000.SH")["status"] == "none"
        assert mgr.status(code="300750.SZ")["status"] == "done"


# ------------------------------------------------------------------ HTTP 契约（server 接线）


class _TestServer:
    """进程内测试服务器（随机端口、免登录）；POST/GET 返回 (status, json)。"""

    def __init__(self, **handler_kwargs):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                         make_handler(**handler_kwargs))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def post(self, path: str, body: dict):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("POST", path, body=json.dumps(body),
                         headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            return r.status, json.loads(r.read() or b"{}")
        finally:
            conn.close()

    def get(self, path: str):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            r = conn.getresponse()
            return r.status, json.loads(r.read() or b"{}")
        finally:
            conn.close()


def test_http_cache_hit_returns_200_sync():
    """HTTP 层：缓存命中保持同步 200 秒回（含 cached=true 与完整报告）。"""
    with tempfile.TemporaryDirectory() as tmp, _patch_extra_data():
        store, runner = _FakeStore(tmp), _FakeRunner()
        with mock.patch("analyzer.ai.requests.post") as post:
            post.return_value = _ok_resp()
            r1 = ai.generate(store, runner, "600000.SH", config=_CONFIG)

        def boom(*a, **kw):
            raise AssertionError("缓存命中时不得调用大模型")

        srv = _TestServer(store=store, runner=runner,
                          ai_jobs=_mk_mgr(store, runner, boom))
        try:
            status, body = srv.post("/api/ai_analysis", {"code": "600000.SH"})
            assert status == 200
            assert body["cached"] is True and body["report"] == r1["report"]
        finally:
            srv.close()


def test_http_async_job_202_and_status_polling():
    """HTTP 层：未命中返回 202 + job 信息；status 按 code/job_id 轮询到 done。"""
    with tempfile.TemporaryDirectory() as tmp, _patch_extra_data():
        store, runner = _FakeStore(tmp), _FakeRunner()
        release = threading.Event()

        def gen(s, r, code, refresh=False, config=None):
            release.wait(5)
            return _payload(code)

        srv = _TestServer(store=store, runner=runner,
                          ai_jobs=_mk_mgr(store, runner, gen))
        try:
            status, body = srv.post("/api/ai_analysis", {"code": "600000.SH"})
            assert status == 202
            assert body["job_id"] and body["queued"] is True
            assert body["status"] in ("queued", "running")

            # 轮询初期：running（by code —— 前端刷新后恢复用的路径）
            status, st = srv.get("/api/ai_analysis/status?code=600000.SH")
            assert status == 200 and st["status"] == "running"
            assert st["job_id"] == body["job_id"]
            # by job_id —— 前端精确轮询路径
            status, st = srv.get(f"/api/ai_analysis/status?job_id={body['job_id']}")
            assert status == 200 and st["status"] == "running"

            release.set()

            def _latest():
                _, st = srv.get(f"/api/ai_analysis/status?job_id={body['job_id']}")
                return st

            assert _wait_until(lambda: _latest()["status"] == "done")
            status, st = srv.get("/api/ai_analysis/status?code=600000.SH")
            assert status == 200 and st["status"] == "done"
            assert "测试报告" in st["report"]["report"]
        finally:
            srv.close()


def test_http_status_validation_and_unknown():
    """HTTP 层：status 缺参/坏 code 400；未知 job_id 404；无任务 code 返回 none。"""
    with tempfile.TemporaryDirectory() as tmp:
        store, runner = _FakeStore(tmp), _FakeRunner()
        srv = _TestServer(store=store, runner=runner,
                          ai_jobs=_mk_mgr(store, runner,
                                          lambda *a, **kw: _payload("600000.SH")))
        try:
            status, body = srv.get("/api/ai_analysis/status")
            assert status == 400 and "error" in body
            status, body = srv.get("/api/ai_analysis/status?code=600519")
            assert status == 400 and "error" in body
            status, body = srv.get("/api/ai_analysis/status?job_id=no-such-job")
            assert status == 404 and "error" in body
            status, body = srv.get("/api/ai_analysis/status?code=600000.SH")
            assert status == 200 and body["status"] == "none"
            # POST 的 code 校验保持不变
            status, body = srv.post("/api/ai_analysis", {"code": "600519"})
            assert status == 400 and "格式" in body["error"]
        finally:
            srv.close()


def test_http_disabled_when_runner_or_jobs_missing():
    """HTTP 层：runner 未启用 / 调度器未接线时 503（沿用既有守卫语义）。"""
    with tempfile.TemporaryDirectory() as tmp:
        store, runner = _FakeStore(tmp), _FakeRunner()
        mgr = _mk_mgr(store, runner, lambda *a, **kw: _payload("600000.SH"))
        srv = _TestServer(store=store, runner=None, ai_jobs=mgr)
        try:
            status, body = srv.post("/api/ai_analysis", {"code": "600000.SH"})
            assert status == 503 and "插件分析" in body["error"]
        finally:
            srv.close()
        srv = _TestServer(store=store, runner=runner, ai_jobs=None)
        try:
            status, body = srv.post("/api/ai_analysis", {"code": "600000.SH"})
            assert status == 503 and "error" in body
            status, body = srv.get("/api/ai_analysis/status?code=600000.SH")
            assert status == 503 and "error" in body
        finally:
            srv.close()
