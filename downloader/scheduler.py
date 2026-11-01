"""多源分批调度器（2026-11）：探测 -> round-robin 分批 -> 失败重派 -> 源熔断。

背景: 东财出口 IP 被 WAF 拉黑（快速失败）、腾讯对科创板 688/689 返回 501、
baostock 黑名单——串行降级链 em->tx->bs 大面积停摆。接入同花顺/新浪/搜狐后，
把「每股固定走串行链」升级为批处理池:

- 启动每源 1 次轻量探测（并行，baostock 恒不可用不建单元）；全部不可用抛
  AllSourcesUnavailableError（CLI exit 2），绝不静默空跑；
- 健康源 round-robin 均衡分批（跳过不支持该代码的源，如腾讯之于科创板）；
- process_one(code, name, unit) 由应用层实现（拉取 + 锚定换算 + 单位守卫 +
  落盘），返回 (code, status, msg)；failed/stale 触发重派，每股最多跨
  max_sources_per_stock 个源（重派即重试，fetch 不再叠加 with_retry）；
- 源连续失败/空窗达 circuit_threshold 触发熔断：该源剩余队列立即重派给
  其他源（防腾讯对科创板全 501 空转打满限速）。

线程模型: 每源 1 个工作线程（源内串行，受各自 RateLimiter 约束；多线程只会
互相争抢源内限速配额，无意义）。重派按「波次」进行：一波全部结束后，失败股
换源进入下一波，直到全部股票到达终态或无源可派。
"""
from __future__ import annotations

import threading
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

from . import source, vendors
from .errors import FetchError


class AllSourcesUnavailableError(FetchError):
    """全部数据源探测失败：无源可用，CLI 必须非零退出（不得静默空跑）。"""


#: 触发重派的状态（拉取失败 / 源空窗）；其余（new/updated/uptodate/repaired/skipped）均为终态
_REDISPATCH = frozenset({"failed", "stale"})


def probe_units(units) -> list[SourceUnit]:
    """并行探测单元（每源 1 次轻量请求），返回健康单元（保持原顺序）。"""
    units = list(units)

    def _ok(u: SourceUnit) -> bool:
        try:
            return bool(u.probe_fn())
        except Exception:  # noqa: BLE001 探测异常等同不可用
            return False

    with ThreadPoolExecutor(max_workers=len(units) or 1) as pool:
        flags = list(pool.map(_ok, units))
    healthy = [u for u, ok in zip(units, flags) if ok]
    down = [u.name for u, ok in zip(units, flags) if not ok]
    if down:
        print(f"[scheduler] 探测不可用（本轮跳过）: {', '.join(down)}", flush=True)
    return healthy


class SourceUnit:
    """可调度数据源单元: fetch(code, start, end, old, full) + 探测 + 支持判断。

    needs_old: 该源依赖存量数据做锚定换算（ths/sina/sohu）。新股（无本地文件）
    无法锚定，应用层据此为新股单独过滤出 em/tx 池，避免浪费跨源次数。
    """

    def __init__(self, name, fetch_fn, probe_fn, supports=None, needs_old: bool = False):
        self.name = str(name)
        self.fetch = fetch_fn
        self.probe_fn = probe_fn
        self.supports = supports or (lambda code: True)
        self.needs_old = needs_old


class MultiSourceScheduler:
    """多源批处理调度器（一次 run 即一轮完整更新；实例不跨运行复用）。"""

    def __init__(self, units, max_sources_per_stock: int = 2, circuit_threshold: int = 8):
        self.units = list(units)
        self.max_sources_per_stock = int(max_sources_per_stock)
        self.circuit_threshold = int(circuit_threshold)

    # ------------------------------------------------------------ 探测
    def _probe_all(self) -> list[SourceUnit]:
        """并行探测全部单元（每源 1 次轻量请求），返回健康单元（保持原顺序）。"""
        return probe_units(self.units)

    # ------------------------------------------------------------ 主流程
    def run(self, targets, process_one, on_progress=None):
        """targets: [(code, name), ...]; process_one(code, name, unit) -> (code, status, msg)。

        返回 (results, per_source): results[code] = (status, msg)；
        per_source[name] = {"ok": n, "fail": n}（仅含本轮实际尝试过的源）。
        on_progress(done, total): 每只股票到达终态回调一次（EOD 看门狗心跳）。
        """
        healthy = self._probe_all()
        if not healthy:
            raise AllSourcesUnavailableError(
                "全部数据源不可用（探测失败）: " + ", ".join(u.name for u in self.units))
        order = [u.name for u in healthy]
        names = {u.name: u for u in healthy}
        alive = set(order)
        tried: dict[str, list[str]] = {}          # code -> 已尝试源名（不重复）
        next_idx: dict[str, int] = {}             # code -> 下一波选源扫描起点
        results: dict[str, tuple[str, str]] = {}
        per_source: dict[str, dict[str, int]] = {}
        streak = defaultdict(int)                 # 源名 -> 连续 failed/stale 计数
        total = len(targets)

        for i, (code, _name) in enumerate(targets):  # 初始 round-robin 指针
            next_idx[code] = i % len(order)

        def _finish(code: str, status: str, msg: str) -> None:
            results[code] = (status, msg)
            if on_progress is not None:
                try:
                    on_progress(len(results), total)
                except Exception:  # noqa: BLE001 心跳回调绝不影响主链路
                    pass

        pending: list[tuple[str, str]] = list(targets)
        wave = 0
        while pending:
            wave += 1
            # ---- 分批: 每只待处理股票选一个「存活 / 未试过 / 支持」的源
            queues: dict[str, deque] = {n: deque() for n in order if n in alive}
            for code, name in pending:
                picked, pj = None, -1
                s = next_idx.get(code, 0)
                for k in range(len(order)):
                    j = (s + k) % len(order)
                    u = names[order[j]]
                    if u.name not in alive or u.name in tried.get(code, ()):
                        continue
                    if not u.supports(code):
                        continue
                    picked, pj = u, j
                    break
                if picked is None:
                    hist = tried.get(code) or []
                    why = f"已试 {hist}" if hist else "无源支持该代码"
                    _finish(code, "failed", f"无可用源（{why}）")
                    continue
                queues[picked.name].append((code, name))
                next_idx[code] = (pj + 1) % len(order)
            pending = []

            # ---- 每源 1 工作线程处理自己的队列
            leftover: list[tuple[str, str]] = []  # 熔断源未处理完的（重派）
            failed: list[tuple[str, str]] = []    # 本波失败/空窗待重派的
            lock = threading.Lock()

            def worker(u: SourceUnit) -> None:
                q = queues[u.name]
                while q:
                    if u.name not in alive:  # 本源已被熔断/中断：余量转重派
                        with lock:
                            while q:
                                leftover.append(q.popleft())
                        return
                    code, name = q.popleft()
                    try:
                        _c, status, msg = process_one(code, name, u)
                    except Exception as e:  # noqa: BLE001 应用层异常按失败重派
                        status, msg = "failed", f"{type(e).__name__}: {e}"
                    with lock:
                        tried.setdefault(code, []).append(u.name)
                        st = per_source.setdefault(u.name, {"ok": 0, "fail": 0})
                        if status in _REDISPATCH:
                            streak[u.name] += 1
                            st["fail"] += 1
                            if (u.name in alive
                                    and streak[u.name] >= self.circuit_threshold):
                                alive.discard(u.name)
                                print(f"[scheduler] 源 {u.name} 连续失败 "
                                      f"{streak[u.name]} 次，熔断并重排其剩余队列",
                                      flush=True)
                                while queues[u.name]:
                                    leftover.append(queues[u.name].popleft())
                            if len(tried[code]) >= self.max_sources_per_stock:
                                _finish(code, "failed", msg)
                            else:
                                failed.append((code, name))
                        else:
                            streak[u.name] = 0
                            st["ok"] += 1
                            _finish(code, status, msg)

            threads = [threading.Thread(target=worker, args=(u,), daemon=True)
                       for u in healthy if u.name in queues]
            try:
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
            except KeyboardInterrupt:
                alive.clear()  # 工作线程在队首检查后尽快退出
                for t in threads:
                    t.join(timeout=5)
                raise
            pending = failed + leftover
            if pending and not alive:
                continue  # 全源熔断：下一波分批会把余量全部判 failed 收尾
        if wave:
            print(f"[scheduler] {wave} 波完成 | 每源统计: "
                  + ", ".join(f"{n}(ok={s['ok']},fail={s['fail']})"
                              for n, s in per_source.items()), flush=True)
        return results, per_source


# ============================================================ 单元工厂
def build_source_units(adjust: str = "hfq") -> list[SourceUnit]:
    """构建五源调度单元（baostock 恒不可用，不参与）。

    em/tx 直接复用 source 层内部实现（沿用全局 client 限速器，改其调用链有
    回归风险）；ths/sina/sohu 走 vendors 适配器（每源独立限速器 + 锚定换算，
    依赖存量数据 needs_old=True）。tx 明确不支持科创板 688/689（返回 501）、
    新浪不支持北交所（返回 null）。
    """
    def _em_fetch(code, start, end, old, full=False):
        return source._em_daily(code, start, end, adjust)

    def _tx_fetch(code, start, end, old, full=False):
        return source._tx_daily(code, start, end, adjust)

    def _probe_em() -> bool:
        try:
            source._em_probe()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _probe_tx() -> bool:
        try:
            data = source._client.get_json(
                source._TX_KLINE_URLS[0],
                {"param": "sh600519,day,2024-01-02,2024-01-03,10,"})
            sym = (data.get("data") or {}).get("sh600519") or {}
            return bool(sym.get("day"))
        except Exception:  # noqa: BLE001
            return False

    def _tx_supports(code: str) -> bool:
        return not code.split(".")[0].startswith(("688", "689"))

    def _not_bj(code: str) -> bool:
        return not code.endswith(".BJ")

    return [
        SourceUnit("em", _em_fetch, _probe_em),
        SourceUnit("tx", _tx_fetch, _probe_tx, _tx_supports),
        SourceUnit("ths", vendors.fetch_ths_hfq, vendors.probe_ths, needs_old=True),
        SourceUnit("sina", vendors.fetch_sina_hfq_cont, vendors.probe_sina,
                   _not_bj, needs_old=True),
        SourceUnit("sohu", vendors.fetch_sohu_hfq_cont, vendors.probe_sohu,
                   needs_old=True),
    ]
