"""个股 AI 分析的异步任务调度（POST /api/ai_analysis 的调度/接口层）。

analyzer/agent/analysis.py 的 generate() 是同步生成入口（插件聚合 -> LLM ->
落盘缓存），实测单次约 6.5 分钟（~19k tokens），HTTP 同步等待会拖死前端。
本模块把它包成后台任务 + 状态查询，接口语义：

    POST /api/ai_analysis            缓存命中 -> 200 同步返回报告（秒回）
                                     未命中 -> 202 {job_id, queued, status}
    GET  /api/ai_analysis/status     ?code= 或 ?job_id= 查询
                                     queued -> running -> done(带报告) / failed(带错误)

设计取舍（为什么要这样做）：
- **任务状态只存内存，服务重启即丢**——可接受：报告本体有 6 小时 TTL 的磁盘
  缓存（data/ai/{code}.json，fingerprint 一致即秒回）。重启后客户端重新 POST，
  指纹未变则直接命中缓存，不需要重跑 LLM；指纹已变本来就要重跑。若要跨重启
  恢复需引入落盘任务表 + 请求去重，对这个单机自用工具收益不值复杂度。
- **全局并发上限 max_workers=2**——LLM 端点是共享网关（同一 AI_BASE_URL 配置
  同时供：本功能、每日美股简报 08:00、LLM 复盘 review_llm），单次推理 ~6.5
  分钟、~19k tokens；无上限并发会打满网关（llm.py 已将 429 映射为「限流」
  错误，说明网关确有限流约束）并饿死其他消费方。2 = 第 3 个任务最晚 ~13
  分钟开跑（可接受）且给简报/复盘留出余量。
- **排队上限 max_pending=16**——防止脚本/连点把无限个 LLM 调用排进队列
  （每个排队任务最终都会真实扣费）。
- **同 code 去重**——与 generate() 进程内 per-code 锁互补：HTTP 层直接复用
  在途任务，避免重复 POST 重复扣费；重复提交返回已有 job（dedup=true）。
- **缓存探测不取 analysis 的 per-code 锁**——generate() 持锁期间（6.5 分钟）
  若 peek 也取同一把锁，POST 会被拖到生成结束才返回，违背秒回目标；去重
  在 manager 锁内先做，正常流程不会出现「peek 与 generate 同 code 并发」。

架构契约：本文件属 agent 包（LLM 调度），只依赖 .analysis/.llm 与标准库；
被 server.py（组合根）接线。generate() 的生成逻辑本身不在本文件改动范围。
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
import uuid

from .analysis import (CACHE_TTL_SECONDS, _cache_path, _fingerprint,
                       _load_cache, generate)
from .context import _fit_context, collect_context
from .llm import AIConfig, AIError

#: 建议客户端的轮询间隔（毫秒）；仅作为提示下发，不强制
POLL_INTERVAL_MS = 3000


def peek_cached_report(store, runner, code: str, config: AIConfig) -> dict | None:
    """缓存探测：与 generate() 的缓存命中分支同规则（fingerprint 一致且未过期）。

    只做「上下文 -> 指纹 -> 比对缓存文件」，不调用 LLM、不取 per-code 锁
    （原因见模块 docstring）。注意：这三行指纹计算必须与 analysis.generate()
    保持一致，analysis.py 的缓存键规则变更时需同步此处。
    """
    code = code.upper()
    context, _warnings = collect_context(store, runner, code)
    context = _fit_context(context)
    fingerprint = _fingerprint(context, config.model)
    cached = _load_cache(_cache_path(store.root, code))
    now = time.time()
    if (cached and cached.get("fingerprint") == fingerprint
            and now - cached.get("created_ts", 0) <= CACHE_TTL_SECONDS):
        out = dict(cached)
        out["cached"] = True
        return out
    return None


class AIJobManager:
    """个股 AI 分析任务调度器（每个 analyzer 进程一个实例）。

    参数:
        store / runner:     透传给 generate() 的数据面依赖
        config_provider:    () -> AIConfig，任务执行与缓存探测时取当前配置
        generate_fn:        可注入的生成函数（默认 analysis.generate；测试替身用）
        max_workers:        全局并发上限（同时跑几个 LLM 生成）
        max_pending:        排队中（未开跑）任务上限，超出 submit 抛 AIError(429)
        max_finished:       内存中保留的已完成（done/failed）任务数，防无界增长
    """

    def __init__(self, store, runner, config_provider=None, generate_fn=None,
                 max_workers: int = 2, max_pending: int = 16, max_finished: int = 50):
        self._store = store
        self._runner = runner
        self._config_provider = config_provider or AIConfig.from_env
        self._generate = generate_fn or generate
        self.max_workers = max(1, int(max_workers))
        self.max_pending = max(1, int(max_pending))
        self.max_finished = max(1, int(max_finished))
        self._jobs: dict[str, dict] = {}        # job_id -> 任务记录
        self._by_code: dict[str, str] = {}      # code -> 最近一次 job_id（含已完成）
        self._lock = threading.Lock()           # 保护 _jobs/_by_code/任务字段
        self._queue: queue.Queue = queue.Queue()
        self._workers_started = False

    # ------------------------------------------------------ 提交

    def submit(self, code: str, refresh: bool = False) -> dict:
        """提交生成请求。

        返回：
        - 缓存命中（且非 refresh）：完整报告 payload（含 cached=true）——同步秒回；
        - 否则：{"job_id", "code", "status", "queued": true, "refresh", "poll_ms"}；
          同 code 已有在途任务时返回该任务（附 dedup=true）。
        抛 AIError：未配置 key（503）/ 排队已满（429）。
        """
        code = str(code).upper()
        config = self._config_provider()
        if not config.enabled:
            raise AIError("AI 分析未配置：请在服务端设置环境变量 "
                          "AI_API_KEY（可选 AI_BASE_URL / AI_MODEL）后重启 analyzer", 503)

        # 1) 同 code 去重：在途任务直接复用（不重复扣费）
        with self._lock:
            active = self._active_for(code)
            if active is not None:
                out = self._job_response(active)
                out["dedup"] = True
                return out

        # 2) 缓存探测（refresh 强制跳过）。不持 manager 锁：collect_context
        #    要跑插件（秒级），持锁会卡住高频的 status 轮询。
        if not refresh:
            cached = peek_cached_report(self._store, self._runner, code, config)
            if cached is not None:
                return cached

        # 3) 双检后入队：探测期间可能有同 code 任务先行创建
        with self._lock:
            active = self._active_for(code)
            if active is not None:
                out = self._job_response(active)
                out["dedup"] = True
                return out
            n_pending = sum(1 for j in self._jobs.values() if j["status"] == "queued")
            if n_pending >= self.max_pending:
                raise AIError(f"AI 生成任务排队已满（{n_pending} 个待处理），请稍后再试", 429)
            job = {
                "job_id": f"{code}-{uuid.uuid4().hex[:10]}",
                "code": code, "status": "queued", "refresh": bool(refresh),
                "queued_at": time.time(), "started_at": None, "finished_at": None,
                "error": None, "error_status": None, "report": None,
            }
            self._jobs[job["job_id"]] = job
            self._by_code[code] = job["job_id"]
            response = self._job_response(job)   # 锁内快照，避免与 worker 状态变迁竞争

        self._ensure_workers()
        self._queue.put(job["job_id"])
        return response

    # ------------------------------------------------------ 查询

    def status(self, code: str | None = None, job_id: str | None = None) -> dict | None:
        """查询任务状态：job_id 精确查询（未知返回 None）；code 查最近任务，
        无任务返回 {"code", "status": "none"}。done 状态附带完整报告 payload。"""
        with self._lock:
            if job_id:
                job = self._jobs.get(job_id)
                return dict(job) if job is not None else None
            if code:
                code = str(code).upper()
                jid = self._by_code.get(code)
                job = self._jobs.get(jid) if jid else None
                if job is not None:
                    return dict(job)
                return {"code": code, "status": "none"}
        return None

    # ------------------------------------------------------ 内部

    def _active_for(self, code: str) -> dict | None:
        """该 code 最近的任务若仍在途（queued/running）则返回之。须持 self._lock。"""
        jid = self._by_code.get(code)
        if not jid:
            return None
        job = self._jobs.get(jid)
        if job is not None and job["status"] in ("queued", "running"):
            return job
        return None

    @staticmethod
    def _job_response(job: dict) -> dict:
        """POST 返回的 job 摘要（不含报告本体，报告经 status 获取）。"""
        return {"job_id": job["job_id"], "code": job["code"],
                "status": job["status"], "queued": True,
                "refresh": job["refresh"], "poll_ms": POLL_INTERVAL_MS}

    def _ensure_workers(self) -> None:
        with self._lock:
            if self._workers_started:
                return
            self._workers_started = True
            for i in range(self.max_workers):
                threading.Thread(target=self._worker, name=f"ai-job-worker-{i + 1}",
                                 daemon=True).start()

    def _worker(self) -> None:
        """消费队列：queued -> running -> done/failed。任何异常都落到任务状态，
        worker 线程绝不退出（一个任务失败不能拖垮后续任务）。"""
        while True:
            job_id = self._queue.get()
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None or job["status"] != "queued":
                    continue   # 已被清理/异常态，跳过
                job["status"] = "running"
                job["started_at"] = time.time()
            try:
                payload = self._generate(self._store, self._runner, job["code"],
                                         refresh=job["refresh"],
                                         config=self._config_provider())
            except AIError as e:
                self._finish(job, "failed", error=str(e), error_status=e.status)
                continue
            except Exception:  # noqa: BLE001 未知异常：通用文案，避免内部细节外泄
                traceback.print_exc()
                self._finish(job, "failed", error="AI 分析生成失败", error_status=500)
                continue
            self._finish(job, "done", report=payload)

    def _finish(self, job: dict, status: str, report: dict | None = None,
                error: str | None = None, error_status: int | None = None) -> None:
        with self._lock:
            job["status"] = status
            job["report"] = report
            job["error"] = error
            job["error_status"] = error_status
            job["finished_at"] = time.time()
            self._prune_finished()

    def _prune_finished(self) -> None:
        """已完成任务只保留最近 max_finished 个（含报告 payload，防内存无界）。
        须持 self._lock。"""
        finished = [j for j in self._jobs.values() if j["status"] in ("done", "failed")]
        if len(finished) <= self.max_finished:
            return
        finished.sort(key=lambda j: j["finished_at"] or 0)
        for job in finished[: len(finished) - self.max_finished]:
            self._jobs.pop(job["job_id"], None)
            if self._by_code.get(job["code"]) == job["job_id"]:
                del self._by_code[job["code"]]
