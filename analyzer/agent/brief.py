"""每日美股简报自动生成（"昨夜美股"的写信环节）。

链路（与 overseas_push 的"寄信"环节互补）：
    东财全球指数快照 + 全球财经快讯（akshare）
        → LLM（OpenAI 兼容，agent.llm.chat_completion）
        → 严格校验（复用 analyzer.overseas.load_daily_brief 的口径）
        → 原子落盘 data/news/us_market/YYYY-MM-DD.md
        → 由既有 us-brief-push 线程在 08:40 接手推邮件

架构契约（tests/test_architecture.py 强制）：本文件属智能体层，只依赖
数据面（analyzer.overseas）+ 标准库/第三方；notifier / watchlist_provider
由组合根（server.py）注入（duck typing）。

调度：守护线程（随 analyzer serve 存活），每工作日 GEN_HOUR:GEN_MINUTE
（默认 08:00 CST）触发；失败按 RETRY_INTERVALS（默认 15,15,15 分钟，
锚定上次尝试时刻）重试；当日成功或重试耗尽即止；进程启动时若已过
触发时刻且当日未生成过则补生成一次。已存在且校验通过的当日简报
（例如人工上传）不覆盖——人工优先，自动兜底。

环境变量（.env；AI_API_KEY 未配置时整体停用）：
    US_BRIEF_GEN_HOUR=8        # 生成时刻（CST），默认 8
    US_BRIEF_GEN_MINUTE=0      # 默认 0（推送在 08:40，留足缓冲）
    US_BRIEF_GEN_RETRY=15,15,15
    US_BRIEF_MAX_TOKENS=4000   # 简报较长，独立于 AI_MAX_TOKENS
"""
from __future__ import annotations

import datetime as dt
import re
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from ..overseas import load_daily_brief
from .llm import AIConfig, AIError, chat_completion
from .prompts import BRIEF_SYSTEM_PROMPT, BRIEF_USER_TEMPLATE, BRIEF_PROMPT_VERSION

CST = ZoneInfo("Asia/Shanghai")

DEFAULT_GEN_HOUR = 8
DEFAULT_GEN_MINUTE = 0
DEFAULT_RETRY_MINUTES = (15, 15, 15)
CHECK_INTERVAL = 60.0

#: 关注的全球指数（东财 index_global_spot_em 名称列）
US_INDEXES = ("道琼斯", "纳斯达克", "标普500")
#: 快讯条数上限（东财 stock_info_global_em，多取再裁剪）
MAX_NEWS = 40
#: prompt 里快讯条数（控制上下文规模）
MAX_NEWS_IN_PROMPT = 30


class BriefGenError(RuntimeError):
    """简报生成失败（数据源 / LLM / 校验）。"""


# ------------------------------------------------------------------ 数据采集


def collect_market_snapshot() -> dict:
    """抓美股三大指数收盘快照 + 全球财经快讯标题（东财，经 akshare）。

    返回 {indexes: [{name, price, pct_chg, time}], news: [{title, time, url}]}。
    指数缺失不致命（LLM 会注明），一条都没有才抛 BriefGenError。
    """
    import akshare as ak  # 延迟导入（仅生成时需要）

    out: dict = {"indexes": [], "news": []}

    try:
        df = ak.index_global_spot_em()
        for _, r in df.iterrows():
            name = str(r.get("名称") or "").strip()
            if any(k in name for k in US_INDEXES):
                out["indexes"].append({
                    "name": name,
                    "price": str(r.get("最新价") or ""),
                    "pct_chg": str(r.get("涨跌幅") or ""),
                    "time": str(r.get("最新行情时间") or ""),
                })
    except Exception as e:  # noqa: BLE001 数据源失败降级为空列表
        out["index_error"] = f"{type(e).__name__}: {str(e)[:80]}"

    try:
        df = ak.stock_info_global_em()
        rows = []
        for _, r in df.head(MAX_NEWS).iterrows():
            title = str(r.get("标题") or "").strip()
            if not title:
                continue
            rows.append({
                "title": title,
                "time": str(r.get("发布时间") or "")[:16],
                "url": str(r.get("链接") or ""),
            })
        out["news"] = rows[:MAX_NEWS_IN_PROMPT]
    except Exception as e:  # noqa: BLE001
        out["news_error"] = f"{type(e).__name__}: {str(e)[:80]}"

    if not out["indexes"] and not out["news"]:
        raise BriefGenError(
            f"数据源全部不可用（{out.get('index_error', '')} / {out.get('news_error', '')}）")
    return out


# ------------------------------------------------------------------ 生成


def _strip_code_fence(text: str) -> str:
    """LLM 偶尔用 ```markdown 包裹，剥掉围栏。"""
    m = re.search(r"```(?:markdown|md)?\s*\n(.*?)```", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()


def _brief_path(data_dir: str | Path, day: dt.date) -> Path:
    return Path(data_dir) / "news" / "us_market" / f"{day.isoformat()}.md"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def generate_brief(data_dir: str | Path, cfg: AIConfig, day: dt.date,
                   watchlist: list[dict] | None = None,
                   max_tokens: int = 4000) -> dict:
    """生成一天的简报并落盘；成功返回 overseas.load_daily_brief 的 valid 结果。

    watchlist: [{code, name}]（组合根注入，用于"自选股映射"节）。
    校验不通过则抛 BriefGenError（不会落盘半成品，避免污染下游推送）。
    """
    snap = collect_market_snapshot()

    def _fmt_index(x: dict) -> str:
        return f"{x['name']} {x['price']}（{x['pct_chg']}%）@{x['time']}"

    lines = [
        f"- {_fmt_index(x)}" for x in snap["indexes"]
    ] or [f"- 指数快照不可用（{snap.get('index_error', '无数据')}）"]
    news_lines = [
        f"- [{n['time']}] {n['title']}（{n['url']}）" for n in snap["news"]
    ] or [f"- 快讯不可用（{snap.get('news_error', '无数据')}）"]
    wl_lines = [
        f"- {w['code']}（{w['name']}）" for w in (watchlist or [])
    ] or ["- （自选股列表为空，跳过个股映射，输出整体性一节即可）"]

    user = BRIEF_USER_TEMPLATE.format(
        day=day.isoformat(),
        weekday="一二三四五六日"[day.weekday()],
        now=dt.datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
        indexes="\n".join(lines),
        news="\n".join(news_lines),
        watchlist="\n".join(wl_lines),
    )

    try:
        out = chat_completion(
            cfg,
            [{"role": "system", "content": BRIEF_SYSTEM_PROMPT},
             {"role": "user", "content": user}],
        )
    except AIError as e:
        raise BriefGenError(f"LLM 调用失败: {e}") from e

    text = _strip_code_fence(out["content"])

    # 先写临时位置校验，通过才正式落盘（防止无效简报污染推送）
    tmp_dir = Path(data_dir) / "news" / "us_market"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    probe = tmp_dir / f".gen_check_{day.isoformat()}.md"
    try:
        probe.write_text(text, encoding="utf-8")
        result = load_daily_brief(data_dir, None, on_date=day,
                                  _filename=f".gen_check_{day.isoformat()}.md")
        if result.get("status") != "valid":
            raise BriefGenError(f"校验未通过: {result.get('message', '未知')}")
    finally:
        probe.unlink(missing_ok=True)

    _atomic_write(_brief_path(data_dir, day), text)
    result["usage"] = out.get("usage") or {}
    result["path"] = str(_brief_path(data_dir, day))
    return result


# ------------------------------------------------------------------ 调度器


class USBriefGenerator:
    """简报生成调度器（守护线程，随 analyzer serve 存活）。"""

    def __init__(self, data_dir: str | Path, cfg: AIConfig,
                 watchlist_provider=None, notifier=None,
                 gen_hour: int = DEFAULT_GEN_HOUR,
                 gen_minute: int = DEFAULT_GEN_MINUTE,
                 retry_minutes: tuple[int, ...] = DEFAULT_RETRY_MINUTES,
                 max_tokens: int = 4000):
        self.data_dir = Path(data_dir)
        self.cfg = cfg
        self.watchlist_provider = watchlist_provider   # () -> [{code, name}]
        self.notifier = notifier
        self.gen_hour = gen_hour
        self.gen_minute = gen_minute
        self.retry_minutes = tuple(retry_minutes) or DEFAULT_RETRY_MINUTES
        self.max_tokens = max_tokens
        self.status = {
            "enabled": cfg.enabled,
            "trigger": f"{gen_hour:02d}:{gen_minute:02d}",
            "prompt_version": BRIEF_PROMPT_VERSION,
        }
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._today: str = ""
        self._attempts = 0
        self._last_msg = ""

    # ---- 状态 ----------------------------------------------------------

    def get_status(self) -> dict:
        with self._lock:
            out = dict(self.status)
            out.update({
                "day": self._today,
                "attempts": self._attempts,
                "last_message": self._last_msg,
            })
            day = dt.datetime.now(CST).date()
            out["brief_exists"] = _brief_path(self.data_dir, day).exists()
            return out

    # ---- 触发一次 -------------------------------------------------------

    def gen_once(self, source: str = "manual", force: bool = False) -> dict:
        """生成今天的简报。当日已有有效简报时跳过（force=True 覆盖）。"""
        day = dt.datetime.now(CST).date()
        if day.weekday() >= 5:
            return {"ok": False, "reason": "周末（美股无收盘）", "day": day.isoformat()}
        if not self.cfg.enabled:
            return {"ok": False, "reason": "未配置 AI_API_KEY", "day": day.isoformat()}

        path = _brief_path(self.data_dir, day)
        if path.exists() and not force:
            existing = load_daily_brief(self.data_dir, None, on_date=day)
            if existing.get("status") == "valid":
                return {"ok": True, "skipped": True, "reason": "当日简报已存在且有效",
                        "day": day.isoformat(), "path": str(path)}

        try:
            wl = []
            if self.watchlist_provider:
                for w in self.watchlist_provider() or []:
                    wl.append({"code": str(w.get("code", "")),
                               "name": str(w.get("name", ""))})
            result = generate_brief(self.data_dir, self.cfg, day,
                                    watchlist=wl, max_tokens=self.max_tokens)
        except (BriefGenError, AIError) as e:
            self._record(day, False, str(e))
            self._notify(f"美股简报自动生成失败：{e}", level="alert")
            return {"ok": False, "reason": str(e), "day": day.isoformat()}

        self._record(day, True, result.get("message", "已生成"))
        self._notify(
            f"美股简报已自动生成（{day.isoformat()}，风险偏好 "
            f"{result.get('risk_score')}，等待 08:40 推送）", level="ok")
        return {"ok": True, "day": day.isoformat(), "path": result.get("path"),
                "risk_score": result.get("risk_score")}

    def _record(self, day: dt.date, ok: bool, msg: str) -> None:
        with self._lock:
            self._today = day.isoformat()
            self._attempts += 1
            self._last_msg = msg

    def _notify(self, text: str, level: str = "info") -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.push(
                key=f"us_brief_gen:{dt.datetime.now(CST).date().isoformat()}:{level}",
                text=text, level=level, desktop=False, source="system")
        except Exception:  # noqa: BLE001 站内信失败不影响生成
            pass

    # ---- 调度循环 -------------------------------------------------------

    @staticmethod
    def _next_fire(now: dt.datetime, hour: int, minute: int) -> dt.datetime:
        """下一个触发时刻（含今天；供启动补生成判断）。"""
        fire = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return fire

    def _sleep_until(self, target: dt.datetime) -> bool:
        """睡到 target（CST）；被 stop 打断返回 False。"""
        while True:
            remain = (target - dt.datetime.now(CST)).total_seconds()
            if remain <= 0:
                return True
            if self._stop.wait(min(remain, CHECK_INTERVAL)):
                return False

    def _run(self) -> None:
        # 启动补生成：已过触发时刻且当日无简报则先试一次
        now = dt.datetime.now(CST)
        fire = self._next_fire(now, self.gen_hour, self.gen_minute)
        day = now.date()
        if now >= fire and day.weekday() < 5 \
                and not _brief_path(self.data_dir, day).exists():
            self.gen_once(source="startup")

        while not self._stop.is_set():
            now = dt.datetime.now(CST)
            day = now.date()
            # 计算下一个工作日触发时刻
            fire = self._next_fire(now, self.gen_hour, self.gen_minute)
            if fire <= now or day.weekday() >= 5:
                # 已过今天触发时刻（或周末）→ 推到下一个工作日同一时刻
                d = day
                while True:
                    d += dt.timedelta(days=1)
                    if d.weekday() < 5:
                        break
                fire = dt.datetime.combine(d, dt.time(self.gen_hour, self.gen_minute),
                                           tzinfo=CST)
            if not self._sleep_until(fire):
                break

            # 触发 + 重试（锚定上次实际尝试时刻）
            target_day = fire.date()
            done = False
            for i in range(len(self.retry_minutes) + 1):
                if self._stop.is_set():
                    return
                now = dt.datetime.now(CST)
                if now.date() != target_day or now.weekday() >= 5:
                    break  # 跨天（极慢重试兜底）→ 放弃当日
                if _brief_path(self.data_dir, target_day).exists():
                    done = True
                    break  # 人工上传已就位，自动让位
                result = self.gen_once(source="schedule")
                if result.get("ok"):
                    done = True
                    break
                if i >= len(self.retry_minutes):
                    break
                if not self._sleep_until(
                        dt.datetime.now(CST) + dt.timedelta(minutes=self.retry_minutes[i])):
                    return
            if not done:
                self._notify(
                    f"美股简报自动生成重试耗尽（{target_day.isoformat()}），"
                    f"今日无简报可推——请检查 AI 配额或数据源", level="alert")

    def start(self) -> None:
        if not self.cfg.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="us-brief-gen",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def parse_retry_minutes(raw: str) -> tuple[int, ...]:
    """解析 US_BRIEF_GEN_RETRY（逗号分隔分钟数），非法值回落默认。"""
    try:
        vals = tuple(int(x) for x in raw.split(",") if x.strip())
        return vals if vals else DEFAULT_RETRY_MINUTES
    except ValueError:
        return DEFAULT_RETRY_MINUTES


def generator_from_env(data_dir: str | Path, watchlist_provider=None,
                       notifier=None) -> USBriefGenerator:
    """按环境变量构建调度器（未配置 AI_API_KEY 时返回 enabled=False 实例）。"""
    import os

    cfg = AIConfig.from_env()
    return USBriefGenerator(
        data_dir, cfg, watchlist_provider=watchlist_provider, notifier=notifier,
        gen_hour=int(os.environ.get("US_BRIEF_GEN_HOUR", DEFAULT_GEN_HOUR)),
        gen_minute=int(os.environ.get("US_BRIEF_GEN_MINUTE", DEFAULT_GEN_MINUTE)),
        retry_minutes=parse_retry_minutes(os.environ.get("US_BRIEF_GEN_RETRY", "")),
        max_tokens=int(os.environ.get("US_BRIEF_MAX_TOKENS", 4000)),
    )
