"""自选股分钟线（新浪源，1 分钟 K 线）。

- 存储: ``data/minute/1/{code}.parquet``，列 [datetime, code, open, high, low, close, volume, amount]
- 价格为**不复权真实价**（与网页「真实价」口径一致）；volume 单位股，amount 单位元
- 新浪单次返回最近约 1970 根（约 8 个交易日）；重复抓取按 datetime 去重增量合并，
  本地历史会随时间累积（远超单次窗口）
- 后台同步: :class:`MinuteSyncer` 单线程队列，加自选即入队，运行期周期性刷新自选列表
"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

import pandas as pd

MINUTE_COLUMNS = ["datetime", "code", "open", "high", "low", "close", "volume", "amount"]


def _log(msg: str) -> None:
    print(f"[minute {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def minute_dir(data_dir, period: str = "1") -> Path:
    return Path(data_dir) / "minute" / period


def to_sina_symbol(code: str) -> str:
    """600519.SH -> sh600519（新浪仅支持沪深）。"""
    num, _, mkt = code.partition(".")
    mkt = mkt.lower()
    if mkt not in ("sh", "sz"):
        raise ValueError(f"新浪分钟线仅支持沪深股票: {code}")
    return f"{mkt}{num}"


def load(code: str, data_dir="data", period: str = "1") -> pd.DataFrame:
    f = minute_dir(data_dir, period) / f"{code}.parquet"
    if not f.exists():
        return pd.DataFrame(columns=MINUTE_COLUMNS)
    return pd.read_parquet(f, engine="pyarrow")


def fetch(code: str, data_dir="data", period: str = "1", retries: int = 1) -> pd.DataFrame:
    """在线抓取并增量合并落盘，返回合并后的全量数据。"""
    import akshare as ak

    raw = None
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            raw = ak.stock_zh_a_minute(symbol=to_sina_symbol(code), period=period)
            if raw is None or raw.empty:
                raise ValueError("数据源返回为空")
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries:
                time.sleep(1.5)
    if last_err is not None and (raw is None or (hasattr(raw, "empty") and raw.empty)):
        raise RuntimeError(f"{code}: 分钟线抓取失败 {type(last_err).__name__}: {str(last_err)[:100]}")

    new = pd.DataFrame({
        "datetime": pd.to_datetime(raw["day"]),
        "code": code,
        "open": pd.to_numeric(raw["open"], errors="coerce"),
        "high": pd.to_numeric(raw["high"], errors="coerce"),
        "low": pd.to_numeric(raw["low"], errors="coerce"),
        "close": pd.to_numeric(raw["close"], errors="coerce"),
        "volume": pd.to_numeric(raw["volume"], errors="coerce"),
        "amount": pd.to_numeric(raw["amount"], errors="coerce"),
    })
    new = new.dropna(subset=["close"]).sort_values("datetime", kind="stable").reset_index(drop=True)

    old = load(code, data_dir, period)
    merged = new if old.empty else pd.concat([old, new], ignore_index=True)
    merged = (
        merged.drop_duplicates(subset=["code", "datetime"], keep="last")
        .sort_values("datetime", kind="stable")
        .reset_index(drop=True)
    )
    f = minute_dir(data_dir, period) / f"{code}.parquet"
    f.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(f, index=False, engine="pyarrow")
    return merged


def info(code: str, data_dir="data", period: str = "1") -> dict | None:
    """本地分钟线概况 {bars, first, last}；无数据返回 None。"""
    f = minute_dir(data_dir, period) / f"{code}.parquet"
    if not f.exists():
        return None
    try:
        dt = pd.read_parquet(f, columns=["datetime"])["datetime"]
    except Exception:  # noqa: BLE001
        return None
    if dt.empty:
        return None
    return {
        "bars": int(len(dt)),
        "first": dt.iloc[0].strftime("%Y-%m-%d %H:%M"),
        "last": dt.iloc[-1].strftime("%Y-%m-%d %H:%M"),
    }


class MinuteSyncer:
    """后台分钟线同步线程：submit() 入队即时抓取；每 refresh_every 秒自动刷新自选列表。

    用法::

        syncer = MinuteSyncer("data")
        syncer.set_watchlist_reader(lambda: ["600519.SH", ...])
        syncer.submit("600519.SH")   # 立即触发（去重：在途不重复）
    """

    def __init__(self, data_dir="data", interval: float = 1.5, refresh_every: float = 1800.0):
        self.data_dir = data_dir
        self._interval = interval
        self._refresh_every = refresh_every
        self._q: queue.Queue[str] = queue.Queue()
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._watchlist_reader = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_ok: dict[str, float] = {}
        self.last_err: dict[str, str] = {}

    # ------------------------------------------------------------------ api

    def set_watchlist_reader(self, fn) -> None:
        self._watchlist_reader = fn

    def submit(self, code: str) -> None:
        code = code.upper()
        with self._lock:
            if code in self._pending:
                return
            self._pending.add(code)
        self._q.put(code)
        self._ensure_thread()

    def pending(self) -> list[str]:
        with self._lock:
            return sorted(self._pending)

    def stop(self) -> None:
        self._stop.set()

    # ---------------------------------------------------------------- thread

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="minute-sync", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        _log("后台同步线程启动")
        last_refresh = 0.0
        while not self._stop.is_set():
            if self._watchlist_reader and time.time() - last_refresh >= self._refresh_every:
                last_refresh = time.time()
                try:
                    codes = [c for c in self._watchlist_reader() if c]
                    if codes:
                        _log(f"周期刷新自选 {len(codes)} 只: {', '.join(codes[:8])}{'…' if len(codes) > 8 else ''}")
                        for c in codes:
                            self.submit(c)
                except Exception as e:  # noqa: BLE001
                    _log(f"读取自选列表失败: {e}")
            try:
                code = self._q.get(timeout=5.0)
            except queue.Empty:
                continue
            try:
                df = fetch(code, self.data_dir)
                self.last_ok[code] = time.time()
                self.last_err.pop(code, None)
                _log(f"{code} 同步完成: {len(df)} 根, 最新 {df['datetime'].iloc[-1]}")
            except Exception as e:  # noqa: BLE001
                self.last_err[code] = f"{type(e).__name__}: {str(e)[:80]}"
                _log(f"{code} 同步失败: {self.last_err[code]}")
            finally:
                with self._lock:
                    self._pending.discard(code)
            if self._interval > 0:
                time.sleep(self._interval)
