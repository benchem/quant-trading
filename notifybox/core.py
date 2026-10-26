"""notifybox —— 统一站内信 / 消息中心核心（analyzer 与 planwatch 共用的中立模块）。

设计目标：把「各模型信号」「交易提醒」「系统进度」汇入同一个收件箱存储，
供 Web 仪表盘整页收件箱 + 顶部快速预览读取，并统一触发 macOS 桌面通知。

- 存储：进程内 deque（上限 _MAX）+ JSONL 全量落盘（默认 data/notifications.jsonl），
  重启可从文件恢复最近消息；跨进程（planwatch CLI / analyzer 常驻）经同一文件读写。
- 来源 source：model（模型信号）/ trade（交易提醒）/ system（系统与更新进度）。
- 级别 level 统一为 info / ok / warn / alert；兼容 planwatch 的 danger / target
  （归一到 alert）。原始级别保留在 raw_level。
- 去重：默认「同一 day 同 key 或同文本」视为重复抑制；调用方已自行去重时
  可传 dedup=False 放行。
- 桌面通知：仅 warn / alert 触发 osascript（静默失败降级），可由 desktop 参数强制。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

_MAX_MESSAGES = 500
_MAC = sys.platform == "darwin"
DISABLE_MAC = False  # 测试/无桌面环境时可置 True 关闭 osascript

LEVELS = ("info", "ok", "warn", "alert")
SOURCES = ("model", "trade", "system")
_DESKTOP_LEVELS = ("warn", "alert")
#: 外部渠道级别 -> 统一级别（planwatch: danger/target 均为需要响动的交易事件）
_LEVEL_ALIASES = {"danger": "alert", "target": "alert", "error": "alert",
                  "critical": "alert", "success": "ok", "info": "info",
                  "ok": "ok", "warn": "warn", "warning": "warn", "alert": "alert"}
_SOURCE_LABEL = {"model": "模型信号", "trade": "交易提醒", "system": "系统"}
_SOURCE_PREFIX = {"model": "⚡ 模型信号", "trade": "🔔 交易提醒", "system": "📈 系统通知"}


def normalize_level(level: str | None) -> str:
    """把任意来源级别归一到 info/ok/warn/alert（未知回落 info）。"""
    return _LEVEL_ALIASES.get(str(level or "").lower(), "info")


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


class NotificationCenter:
    def __init__(self, data_dir: str | Path = "data", max_messages: int = _MAX_MESSAGES):
        self.path = Path(data_dir) / "notifications.jsonl"
        self._max = int(max_messages)
        self._lock = threading.Lock()
        self._items: deque[dict] = deque(maxlen=self._max)
        self._off = 0
        self._load()

    # ------------------------------------------------------------- 内部

    def _load(self) -> None:
        """从 JSONL 恢复最近消息（损坏行 / 旧记录缺字段容错），并记录尾部偏移。"""
        try:
            raw = self.path.read_bytes()
        except (FileNotFoundError, OSError):
            self._off = 0
            return
        lines = raw.splitlines()
        for line in lines[-self._max:]:
            try:
                rec = json.loads(line.decode("utf-8", "replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(rec, dict) and rec.get("key") and rec.get("day"):
                rec.setdefault("source", "system")
                rec.setdefault("read", False)
                self._items.append(rec)
        self._off = len(raw)

    def _sync_locked(self) -> None:
        """增量跟随文件尾部：读取其它进程（如 planwatch CLI）新追加的完整行。

        以字节偏移记录消费进度；文件被截断/重写（clear）时回退全量重读。
        按消息 id 去重（本进程自身 push 也走文件，避免重复入内存）。调用方需持锁。
        """
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size < self._off:            # 轮转/清空重写 -> 从头重读
            self._off = 0
            self._items.clear()
            self._load()
            return
        if size == self._off:
            return
        with open(self.path, "rb") as fh:
            fh.seek(self._off)
            chunk = fh.read()
        nl = chunk.rfind(b"\n")
        if nl < 0:                      # 尚无可消费的完整行
            return
        consumed = chunk[: nl + 1]
        self._off += len(consumed)
        seen = {it.get("id") for it in self._items}
        for line in consumed.splitlines():
            try:
                rec = json.loads(line.decode("utf-8", "replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not (isinstance(rec, dict) and rec.get("key") and rec.get("day")):
                continue
            rid = rec.get("id")
            if rid and rid in seen:
                continue
            rec.setdefault("source", "system")
            rec.setdefault("read", False)
            self._items.append(rec)
            if rid:
                seen.add(rid)


    def _dedup(self, rec: dict) -> bool:
        """当日同 key 或消息文本完全相同 -> 视为重复。调用方需持锁。"""
        for it in reversed(self._items):
            if (it.get("key") == rec["key"] and it.get("day") == rec["day"]) or \
                    (it.get("day") == rec["day"] and it.get("text") == rec["text"]):
                return True
        return False

    def _desktop(self, rec: dict) -> None:
        if not (_MAC and not DISABLE_MAC) or rec.get("level") not in _DESKTOP_LEVELS:
            return
        prefix = _SOURCE_PREFIX.get(rec.get("source", "system"), "📈 提醒")
        title = f"{prefix} · {rec.get('code')}" if rec.get("code") else prefix
        try:
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{_esc(rec["text"])}" with title "{_esc(title)}" '
                 f'sound name "Glass"'],
                timeout=3, check=False, capture_output=True)
        except Exception:  # noqa: BLE001 桌面通知失败不影响消息入库
            pass

    # ------------------------------------------------------------- 对外

    def push(self, key: str, text: str, level: str = "info",
             code: str | None = None, desktop: bool | None = None,
             day: str | None = None, source: str = "system",
             dedup: bool = True, extra: dict | None = None) -> dict | None:
        """写入一条消息；dedup 命中返回 None。

        desktop=None 时按级别自动（warn/alert 响）；source 标注来源渠道；
        extra 附加业务字段（如 plugin / batch），随消息一并持久化。
        """
        now = datetime.now(timezone.utc)
        lv = normalize_level(level)
        rec = {
            "id": uuid.uuid4().hex[:10],
            "key": key,
            "code": code,
            "level": lv,
            "raw_level": level,
            "source": source if source in SOURCES else "system",
            "text": str(text)[:500],
            "time": now.isoformat(timespec="seconds"),
            "day": day or now.astimezone().strftime("%Y-%m-%d"),
            "read": False,
        }
        if extra:
            for k, v in extra.items():
                if k not in rec:
                    rec[k] = v
        with self._lock:
            if dedup and self._dedup(rec):
                return None
            self._items.append(rec)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError:
                pass
        if desktop is True or (desktop is None and lv in _DESKTOP_LEVELS):
            self._desktop(rec)
        return rec

    def list(self, limit: int = 100, since: str | None = None,
             source: str | None = None, level: str | None = None,
             code: str | None = None, unread_only: bool = False) -> dict:
        """按时间倒序返回消息；支持按来源/级别/股票/未读过滤。

        since 为游标（仅返回比它新的消息，用于弹新提醒）。返回附带 counts：
        全量（未过滤）各来源的 total/unread，供侧栏徽标使用。
        """
        with self._lock:
            self._sync_locked()
            allitems = list(self._items)[::-1]
        # 侧栏计数始终基于全量（当前保留的消息），不受过滤影响
        counts = {"total": len(allitems),
                  "unread": sum(1 for x in allitems if not x.get("read")),
                  "by_source": {}}
        for src in SOURCES:
            sub = [x for x in allitems if x.get("source", "system") == src]
            counts["by_source"][src] = {
                "label": _SOURCE_LABEL[src], "total": len(sub),
                "unread": sum(1 for x in sub if not x.get("read")),
            }
        items = allitems
        if since:
            items = [x for x in items if x["time"] > since]
        if source:
            items = [x for x in items if x.get("source", "system") == source]
        if level:
            items = [x for x in items if x.get("level") == normalize_level(level)]
        if code:
            items = [x for x in items if x.get("code") == code]
        if unread_only:
            items = [x for x in items if not x.get("read")]
        if not since:
            items = items[: int(limit)]
        return {
            "messages": items,
            "unread": sum(1 for x in items if not x.get("read")),
            "cursor": (allitems[0]["time"] if allitems else (since or "")),
            "counts": counts,
        }

    def mark_read(self, ids: list[str] | None = None, all: bool = False,
                  source: str | None = None) -> int:
        """标记已读；ids=None 且 all=True 时全部（或指定 source 全部）已读。返回标记条数。"""
        with self._lock:
            n = 0
            for it in self._items:
                if it.get("read"):
                    continue
                if ids and it["id"] in set(ids):
                    it["read"] = True
                    n += 1
                elif all and (source is None or it.get("source", "system") == source):
                    it["read"] = True
                    n += 1
            return n

    def clear(self, before_day: str | None = None, source: str | None = None) -> int:
        """删除消息（默认全清；before_day 只删该日期之前的；source 限渠道）。重写 JSONL。"""
        with self._lock:
            keep = deque(maxlen=self._max)
            removed = 0
            for it in self._items:
                drop = (source is None or it.get("source", "system") == source) and \
                       (before_day is None or it.get("day", "") < before_day)
                if drop:
                    removed += 1
                else:
                    keep.append(it)
            if removed:
                self._items = keep
                self._rewrite()
            return removed

    def _rewrite(self) -> None:
        """把内存全量写回 JSONL（compact；调用方需持锁）。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                for it in self._items:
                    fh.write(json.dumps(it, ensure_ascii=False) + "\n")
        except OSError:
            pass


# --------------------------------------------------------------- 进程级单例

_inboxes: dict[str, NotificationCenter] = {}
_inbox_lock = threading.Lock()


def get_inbox(data_dir: str | Path = "data", max_messages: int = _MAX_MESSAGES) -> NotificationCenter:
    """按 data_dir 缓存的进程级收件箱单例（供 planwatch 等短生命周期 CLI 复用）。"""
    key = str(Path(data_dir))
    with _inbox_lock:
        box = _inboxes.get(key)
        if box is None:
            box = NotificationCenter(data_dir, max_messages)
            _inboxes[key] = box
        return box
