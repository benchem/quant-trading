"""合成数据 Context —— 插件单测不需要真实数据目录，也不需要网络。

与真实路径完全一致：合成 K线/财报写成**临时 parquet 文件**，走 ``AnalysisContext``
的真实读取代码（含缓存与 fundamental 缓存回退），而不是 mock。``seed`` 固定后
结果完全可复现。

用法::

    from stock_analysis import Runner, load_plugins
    from stock_analysis.testing import make_context, make_fundamental_frame

    ctx = make_context(n_stocks=3, years=5, seed=7)
    try:
        runner = Runner(load_plugins(), ctx)
        result = runner.run("momentum", "600001.SH")
        assert result.ok
        # 基本面类插件测试：给某只股票挂一份财报
        ctx.set_fundamental("600001.SH", make_fundamental_frame(40))
    finally:
        ctx.cleanup()        # 清理临时目录
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from .core.context import AnalysisContext

_SYN_CODES = ["600001.SH", "000002.SZ", "300001.SZ", "688001.SH", "000001.SZ"]


# ------------------------------------------------------------------ 数据生成


def _synth_kline(rng: np.random.Generator, dates: pd.DatetimeIndex,
                 s0: float, drift: float, vol: float, jump_p: float) -> pd.DataFrame:
    """几何随机游走 + 趋势项 + 偶发跳跃；OHLC/量额/换手自洽（列与真实落盘一致）。"""
    n = len(dates)
    logp = np.log(s0) + np.cumsum(rng.normal(drift, vol, n))
    if jump_p > 0:
        logp = logp + np.where(rng.random(n) < jump_p, rng.normal(0, 0.03, n), 0.0)
    close = np.exp(logp)
    spread = np.abs(rng.normal(0, 0.008, n)) + 0.002
    open_ = close * (1 + rng.normal(0, 0.004, n))
    high = np.maximum(open_, close) * (1 + spread)
    low = np.minimum(open_, close) * (1 - spread)
    volume = np.round(np.exp(rng.normal(10.5, 0.6, n))).astype(int)  # 手
    amount = volume * 100.0 * (open_ + close) / 2.0
    pct = np.empty(n)
    pct[0] = 0.0
    pct[1:] = close[1:] / close[:-1] - 1.0
    turnover = np.clip(rng.normal(3.0, 1.5, n), 0.2, 25.0)
    return pd.DataFrame(
        {
            "date": dates,
            "open": open_, "high": high, "low": low, "close": close,
            "volume": volume, "amount": amount,
            "pct_chg": pct * 100.0, "turnover": turnover,
        }
    )


def make_fundamental_frame(code: str = "600001.SH", n_periods: int = 40,
                           seed: int = 11) -> pd.DataFrame:
    """合成一份财报序列（列与真实 ``data/fundamental/*.parquet`` 一致）。"""
    rng = np.random.default_rng(seed)
    end = pd.Timestamp.today().normalize().to_period("Q")
    periods = [end - i for i in range(n_periods - 1, -1, -1)]
    rev = 5e8
    rows = []
    for p in periods:
        rev *= 1 + rng.normal(0.03, 0.06)
        np_ = rev * float(np.clip(rng.normal(0.12, 0.04), -0.2, 0.4))
        rows.append({
            "code": code,
            "period": p.to_timestamp(how="end"),
            "rev": rev,
            "rev_yoy": rng.normal(0.10, 0.12),
            "net_profit": np_,
            "net_profit_yoy": rng.normal(0.12, 0.15),
            "kdj_profit": np_ * 0.95,
            "kdj_profit_yoy": rng.normal(0.10, 0.15),
            "eps": np_ / 1e9,
            "roe": rng.normal(0.13, 0.03),
            "gross_margin": rng.normal(0.35, 0.05),
            "net_margin": np_ / rev,
            "debt_ratio": float(np.clip(rng.normal(0.45, 0.08), 0.05, 0.95)),
        })
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ 内存 store


class _MemoryStore:
    """最小 DataStore 替身：K线/财报落临时 parquet，其余属性内存化。

    与真实 DataStore 暴露相同的属性/方法（duck typing，见 context.py 文档），
    因此 ``AnalysisContext`` 对它走的是与生产完全一致的代码路径。
    """

    def __init__(self, tmp: Path, daily: dict[str, pd.DataFrame], basic, calendar,
                 market, indexes, meta):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="stock_analysis_test_")
        self.root: Path = Path(self._tmpdir.name)
        self.daily_dir = self.root / "daily"
        self.daily_dir.mkdir(parents=True)
        for code, df in daily.items():
            df_out = df.reset_index()
            df_out["code"] = code
            df_out.to_parquet(self.daily_dir / f"{code}.parquet", index=False)
        self.basic = basic
        self.calendar = calendar
        self.market = market
        self.indexes = indexes
        self.meta = meta
        self._chips: dict[str, dict] = {}

    def chips(self, code: str):
        return self._chips.get(code.upper())

    def close(self):
        try:
            self._tmpdir.cleanup()
        except OSError:
            pass


# ------------------------------------------------------------------ 入口


def make_context(n_stocks: int = 2, years: int = 5, seed: int = 42,
                 codes: list[str] | None = None) -> AnalysisContext:
    """构造合成数据的 AnalysisContext（可复现、零网络、用完 ``ctx.cleanup()``）。

    额外方法（测试辅助）:
        ctx.set_fundamental(code, df)   写入该股财报缓存（真实 parquet 路径）
        ctx.set_chips(code, dict)       注入筹码分布结果
    """
    rng = np.random.default_rng(seed)
    n_stocks = min(max(n_stocks, 1), len(_SYN_CODES))
    codes = codes or _SYN_CODES[:n_stocks]

    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=int(years * 244))
    daily: dict[str, pd.DataFrame] = {}
    for code in codes:
        df = _synth_kline(
            rng, dates,
            s0=float(rng.uniform(8, 80)),
            drift=float(rng.normal(0.0004, 0.0006)),
            vol=float(rng.uniform(0.015, 0.035)),
            jump_p=0.01,
        )
        daily[code] = df.set_index("date")

    # basic（与 stock_basic 同构）
    basic_rows = []
    for i, code in enumerate(codes):
        last_close = float(daily[code]["close"].iloc[-1])
        basic_rows.append({
            "code": code,
            "name": f"合成股{i + 1}",
            "latest_price": last_close,
            "pct_chg": float(daily[code]["pct_chg"].iloc[-1]),
            "turnover": 2.0,
            "total_mv": last_close * 1e9,
            "float_mv": last_close * 8e8,
        })
    basic = pd.DataFrame(basic_rows).set_index("code")

    # market 日度聚合（用合成面板真实计算，保证内部自洽）
    frames = []
    for code, df in daily.items():
        d2 = df.reset_index()
        d2["code"] = code
        frames.append(d2)
    panel = pd.concat(frames, ignore_index=True).sort_values(["code", "date"])
    prev = panel.groupby("code")["close"].shift(1)
    panel["ret"] = (panel["close"] / prev - 1.0).clip(-1, 1)
    m = pd.DataFrame({
        "adv": (panel["ret"] > 0).groupby(panel["date"]).sum(),
        "dec": (panel["ret"] < 0).groupby(panel["date"]).sum(),
        "flat": (panel["ret"] == 0).groupby(panel["date"]).sum(),
        "mean_ret": panel["ret"].groupby(panel["date"]).mean(),
        "med_ret": panel["ret"].groupby(panel["date"]).median(),
        "amount_yi": panel["amount"].groupby(panel["date"]).sum() / 1e8,
    })
    m["nav_eqw"] = (1 + m["mean_ret"].fillna(0)).cumprod()
    m.index.name = "date"

    # 指数：等权合成（供 beta 等跨市场计算）
    idx_close = panel.groupby("date")["close"].mean()
    index_df = pd.DataFrame({"close": idx_close / float(idx_close.iloc[0]) * 3000.0})

    meta = {
        "n_stocks": len(codes),
        "date_min": str(dates[0].date()),
        "date_max": str(dates[-1].date()),
        "last_trade_date": str(dates[-1].date()),
        "indexes": [{"code": "000300.SH", "name": "沪深300(合成)"}],
        "load_seconds": 0.0,
        "synthetic": True,
    }

    store = _MemoryStore(Path(tempfile.gettempdir()), daily, basic, dates, m,
                         {"000300.SH": index_df}, meta)
    ctx = AnalysisContext(store)

    def set_fundamental(code: str, df: pd.DataFrame):
        d = store.root / "fundamental"
        d.mkdir(parents=True, exist_ok=True)
        df = df.copy()
        df["code"] = code.upper()
        df.to_parquet(d / f"{code.upper()}.parquet", index=False)

    def set_chips(code: str, data: dict):
        store._chips[code.upper()] = data  # noqa: SLF001 测试注入

    ctx.set_fundamental = set_fundamental  # type: ignore[attr-defined]
    ctx.set_chips = set_chips  # type: ignore[attr-defined]
    return ctx
