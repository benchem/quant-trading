"""AnalysisContext —— 插件访问数据的唯一入口。

插件**不应该** import ``analyzer.datastore.DataStore``：一律通过本类取数。
好处：

1. 底层实现（缓存、数据源、存储格式）可以演进，插件不受影响；
2. 测试时可以用 ``stock_analysis.testing.make_context()`` 构造合成数据上下文，
   插件单测不需要 678MB 的真实数据集，也不需要网络；
3. 框架可以在此统一做权限/审计/限流（当前本地工具用不到，留位置）。

Context 读取 store 的最小接口（duck typing）::

    store.meta                       dict   数据元信息
    store.basic                      DataFrame（index=code: name/latest_price/
                                      pct_chg/total_mv/float_mv）
    store.calendar                   pd.DatetimeIndex
    store.market                     DataFrame（日度市场聚合）
    store.indexes                    dict[code -> DataFrame(date index, close)]
    store.root                       Path   数据目录
    store.chips(code)                dict | None
    store.fundamental_local(code)    DataFrame（仅本地缓存，可为空）  [可选]

未实现 ``fundamental_local`` 时回退到 ``analyzer.fundamental.load``（只读缓存）。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .base import PluginError


class DataUnavailable(RuntimeError):
    """该股票没有本地数据（插件可捕获后给出友好结果）。"""


_KLINE_CACHE_MAX = 128
_KLINE_COLS = ["open", "high", "low", "close", "volume", "amount", "pct_chg", "turnover"]


class AnalysisContext:
    """传给每个插件的数据上下文（只读）。"""

    def __init__(self, store):
        self._store = store
        self._kline_cache: dict[tuple, pd.DataFrame] = {}

    # ------------------------------------------------------------ 基础

    @property
    def store(self):
        """底层数据 store（只读，调试/框架用；插件请走本类方法）。"""
        return self._store

    def cleanup(self):
        """释放资源（合成数据 store 会清理临时目录；真实 store 无操作）。

        注：价格序列方法叫 :meth:`close`（close(code)），资源释放叫 cleanup()，
        两者勿混用。
        """
        c = getattr(self._store, "close", None)
        if callable(c):
            c()

    # ------------------------------------------------------------ 元信息

    @property
    def meta(self) -> dict:
        return self._store.meta

    def name(self, code: str) -> str:
        """股票中文名（无则返回代码）。"""
        code = self.normalize_code(code)
        b = self._store.basic
        return str(b.loc[code, "name"]) if code in b.index else code

    def basic(self, code: str) -> dict:
        """基础信息：name / latest_price / pct_chg / total_mv / float_mv（缺失为 None）。"""
        code = self.normalize_code(code)
        b = self._store.basic
        if code not in b.index:
            return {"code": code, "name": code}
        r = b.loc[code]
        out = {"code": code, "name": str(r.get("name", code))}
        for col in ("latest_price", "pct_chg", "total_mv", "float_mv"):
            v = r.get(col)
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = None
            out[col] = v if v == v else None  # NaN -> None
        return out

    # ------------------------------------------------------------ 行情数据

    def kline(self, code: str, start: str | None = None, end: str | None = None,
              last: int | None = None) -> pd.DataFrame:
        """个股日线（后复权），``date`` 为 DatetimeIndex，列含
        open/high/low/close/volume/amount/pct_chg/turnover（缺失列为 NaN）。

        参数:
            start / end: 'YYYY-MM-DD'
            last: 只取最近 N 个交易日（与 start/end 可同时用）

         Raises: DataUnavailable —— 本地无该股数据或区间为空。
        """
        code = self.normalize_code(code)
        key = (code, start, end, last)
        if key in self._kline_cache:
            return self._kline_cache[key]

        f: Path = self._store.daily_dir / f"{code}.parquet"
        if not f.exists():
            raise DataUnavailable(f"本地无 {code} 的日线数据")
        df = pd.read_parquet(f)
        if "date" not in df.columns or "close" not in df.columns:
            raise DataUnavailable(f"{code} 的日线文件缺少 date/close 列")
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").set_index("date")
        df = df[~df.index.duplicated(keep="last")]
        for c in _KLINE_COLS:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            else:
                df[c] = pd.NA
        if start:
            df = df[df.index >= pd.Timestamp(start)]
        if end:
            df = df[df.index <= pd.Timestamp(end)]
        if last is not None:
            df = df.tail(int(last))
        if df.empty:
            raise DataUnavailable(f"{code} 在指定区间内无数据")
        df.index.name = "date"

        self._kline_cache[key] = df
        if len(self._kline_cache) > _KLINE_CACHE_MAX:
            self._kline_cache.pop(next(iter(self._kline_cache)), None)
        return df

    def close(self, code: str, **kw) -> pd.Series:
        """收盘价序列（date index）。"""
        return self.kline(code, **kw)["close"].astype(float)

    def index_kline(self, code: str, start: str | None = None,
                    end: str | None = None) -> pd.DataFrame | None:
        """指数日线（close 为主）；无该指数返回 None。"""
        idf = self._store.indexes.get(code.upper())
        if idf is None:
            return None
        if start:
            idf = idf[idf.index >= pd.Timestamp(start)]
        if end:
            idf = idf[idf.index <= pd.Timestamp(end)]
        return idf

    def market(self, start: str | None = None, end: str | None = None) -> pd.DataFrame:
        """全市场日度聚合（adv/dec/mean_ret/amount_yi/nav_eqw...）。"""
        m = self._store.market
        if start:
            m = m[m.index >= pd.Timestamp(start)]
        if end:
            m = m[m.index <= pd.Timestamp(end)]
        return m

    # ------------------------------------------------------------ 辅助数据

    def fundamental(self, code: str) -> pd.DataFrame | None:
        """本地缓存的财报数据（**不触发网络**）。缺失返回 None。

        列: period + rev/rev_yoy/net_profit/net_profit_yoy/kdj_profit/
        kdj_profit_yoy/eps/roe/gross_margin/net_margin/debt_ratio
        （金额=元，比率=小数）。
        """
        code = self.normalize_code(code)
        loader = getattr(self._store, "fundamental_local", None)
        if loader is None:
            from analyzer.fundamental import load as _load  # 延迟导入，仅缓存读取

            def loader(code):
                return _load(code, self._store.root)
        try:
            df = loader(code)
        except Exception:  # noqa: BLE001 缓存文件损坏等 -> 当作无数据
            return None
        if df is None or df.empty:
            return None
        df = df.copy()
        df["period"] = pd.to_datetime(df["period"])
        return df.sort_values("period").reset_index(drop=True)

    def chips(self, code: str) -> dict | None:
        """筹码分布（本地计算，带缓存）；无数据返回 None。"""
        code = self.normalize_code(code)
        try:
            return self._store.chips(code)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------ 工具

    def normalize_code(self, code: str) -> str:
        """'600519' / 'sh600519' / '600519.SH' -> '600519.SH'。"""
        from analyzer.datastore import _norm_code

        try:
            return _norm_code(code)
        except ValueError as e:
            raise PluginError(str(e)) from e

    def trading_days(self, start: str | None = None, end: str | None = None) -> pd.DatetimeIndex:
        cal = self._store.calendar
        if start:
            cal = cal[cal >= pd.Timestamp(start)]
        if end:
            cal = cal[cal <= pd.Timestamp(end)]
        return cal

    def annualize(self) -> float:
        """A股年均交易日数。"""
        return 244.0
