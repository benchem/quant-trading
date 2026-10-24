"""季度/年报基本面数据：抓取、解析、本地缓存。

数据源: 同花顺财务摘要（akshare ``stock_financial_abstract_ths``，indicator="按报告期"），
单只股票一次请求即可拿到上市以来全部年度+季度报告期，覆盖沪深主板/创业板/科创板。
原始值为 "1.47亿" / "46.84%" / "False" 风格字符串，解析为数值后落地
``data/fundamental/{code}.parquet``（列: code, period + 各指标）。

单位约定: 金额=元；比率=小数（0.4684 表示 46.84%）；EPS=元。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pandas as pd

#: 同花顺源列 -> (内部字段, 单位: yuan/pct/num)
METRICS: dict[str, tuple[str, str]] = {
    "营业总收入": ("rev", "yuan"),
    "营业总收入同比增长率": ("rev_yoy", "pct"),
    "净利润": ("net_profit", "yuan"),
    "净利润同比增长率": ("net_profit_yoy", "pct"),
    "扣非净利润": ("kdj_profit", "yuan"),
    "扣非净利润同比增长率": ("kdj_profit_yoy", "pct"),
    "基本每股收益": ("eps", "num"),
    "净资产收益率": ("roe", "pct"),
    "销售毛利率": ("gross_margin", "pct"),
    "销售净利率": ("net_margin", "pct"),
    "资产负债率": ("debt_ratio", "pct"),
}

FIELDS = [f for f, _ in METRICS.values()]
_MISSING = ("False", "", "-", "—", "NA", "N/A")


class FundamentalError(RuntimeError):
    """抓取失败（网络/源不可用/无数据）。"""


# ------------------------------------------------------------------ parsing

def _parse_num(s) -> float | None:
    if s is None:
        return None
    s = str(s).strip()
    if s in _MISSING:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_amount(s) -> float | None:
    """'1.47亿' -> 1.47e8, '547.03万' -> 5.47e6, '65.66' -> 65.66"""
    if s is None:
        return None
    s = str(s).strip()
    if s in _MISSING:
        return None
    mult = 1.0
    for suf, m in (("亿", 1e8), ("万", 1e4), ("千", 1e3)):
        if s.endswith(suf):
            mult, s = m, s[: -len(suf)]
            break
    try:
        return float(s) * mult
    except ValueError:
        return None


def _parse_pct(s) -> float | None:
    """'46.84%' -> 0.4684"""
    if s is None:
        return None
    s = str(s).strip()
    if s in _MISSING:
        return None
    if s.endswith("%"):
        s = s[:-1]
    try:
        return float(s) / 100.0
    except ValueError:
        return None


_PARSERS = {"yuan": _parse_amount, "pct": _parse_pct, "num": _parse_num}


def _raw_to_frame(raw: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, r in raw.iterrows():
        rec: dict = {"period": pd.Timestamp(r["报告期"])}
        for src, (field, unit) in METRICS.items():
            rec[field] = _PARSERS[unit](r.get(src))
        rows.append(rec)
    df = pd.DataFrame(rows)
    df["period"] = pd.to_datetime(df["period"])
    df = df.sort_values("period").drop_duplicates("period", keep="last").reset_index(drop=True)
    return df


# ------------------------------------------------------------------ fetching

def fetch(code: str, data_dir: str | Path = "data", force: bool = False,
          retries: int = 2) -> pd.DataFrame:
    """获取单只股票基本面（优先缓存），失败抛 FundamentalError。"""
    code = code.upper()
    fund_dir = Path(data_dir) / "fundamental"
    fund_dir.mkdir(parents=True, exist_ok=True)
    f = fund_dir / f"{code}.parquet"
    if f.exists() and not force:
        df = pd.read_parquet(f)
        if "code" not in df.columns:
            df = df.copy()
            df.insert(0, "code", code)
        return df

    symbol = code.split(".")[0]
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            import akshare as ak  # 延迟导入（首次约 0.6s，且仅抓取时需要）

            raw = ak.stock_financial_abstract_ths(symbol=symbol, indicator="按报告期")
            if raw is None or raw.empty:
                raise FundamentalError(f"{code}: 同花顺未返回数据")
            df = _raw_to_frame(raw)
            if df.empty:
                raise FundamentalError(f"{code}: 同花顺未返回数据")
            df.insert(0, "code", code)
            df.to_parquet(f, index=False)
            return df
        except FundamentalError:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    msg = str(last_err)[:100]
    if "NoneType" in type(last_err).__name__ or "NoneType" in msg:
        msg = "数据源未返回内容（代码可能无效/已退市）"
    raise FundamentalError(f"{code}: 抓取失败 {type(last_err).__name__}: {msg}")


def load(code: str, data_dir: str | Path = "data", refresh: bool = False) -> pd.DataFrame:
    """缓存读取（不触发网络）。缺失返回空 DataFrame。"""
    f = Path(data_dir) / "fundamental" / f"{code.upper()}.parquet"
    if not f.exists():
        return pd.DataFrame()
    return pd.read_parquet(f)


# ------------------------------------------------------------------ batch

class RateLimiter:
    """简单的线程安全令牌桶（按最小间隔限速）。"""

    def __init__(self, rps: float):
        self.min_interval = 1.0 / rps if rps > 0 else 0.0
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            gap = self._last + self.min_interval - now
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


def prefetch_all(data_dir: str | Path = "data", codes: list[str] | None = None,
                 force: bool = False, rate: float = 2.0, workers: int = 3,
                 progress_cb=None) -> dict:
    """批量抓取。返回 {ok, failed, skipped}；失败清单写入 data/fundamental/failed.txt。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from .datastore import DataStore

    data_dir = Path(data_dir)
    fund_dir = data_dir / "fundamental"
    fund_dir.mkdir(parents=True, exist_ok=True)

    if codes is None:
        codes = list(DataStore(data_dir).basic.index)
    codes = [c.upper() for c in codes]

    limiter = RateLimiter(rate)
    ok = skipped = 0
    failed: list[str] = []
    total = len(codes)

    def one(code: str):
        nonlocal ok, skipped
        f = fund_dir / f"{code}.parquet"
        if f.exists() and not force:
            return code, "skip", None
        limiter.wait()
        try:
            fetch(code, data_dir, force=force)
            return code, "ok", None
        except FundamentalError as e:
            return code, "fail", str(e)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(one, c): c for c in codes}
        done = 0
        for fut in as_completed(futs):
            code, status, err = fut.result()
            done += 1
            if status == "ok":
                ok += 1
            elif status == "skip":
                skipped += 1
            else:
                failed.append(code)
            if progress_cb:
                progress_cb(done, total, code, status, err)

    if failed:
        (fund_dir / "failed.txt").write_text("\n".join(sorted(failed)) + "\n")
    return {"ok": ok, "skipped": skipped, "failed": len(failed),
            "failed_codes": sorted(failed)}
