"""复权锚定换算：把供应商序列折算到本地存量 hfq 基准（2026-11 多源扩展）。

数学基础：对前复权序列 qfq 与后复权序列 hfq，任意日期 d 恒有
    hfq_d / qfq_d = 常数（= 当前累计复权因子）
除权事件不改变该比值——因此可以用「重叠日期上的收盘价比值」把同花顺的 qfq
全序列一次性折算成与存量文件精确同基准的 hfq（整文件替换写，修复单位断裂）。

质量门（防混基准/脏数据写入）：
- 重叠日期数 >= min_overlap（默认 5）；
- 比值变异系数 CV = std/mean（总体标准差，ddof=0）< max_cv（默认 0.5%）；
- 不满足即抛 AnchorError，调用方走失败路径（重派其他源 / 回退旧链路）。

raw 续接（新浪/搜狐，仅不复权）：hfq_d / raw_d = F_d 随除权变化，历史不可比，
但「存量最后一根 bar 的 F」可由 ratio = 存量末根 hfq_close / 该日 raw_close 求出，
窗口内后续 bar × ratio 即为存量基准的 hfq 延续；窗口内若恰有除权事件，接缝
涨跌幅会爆炸，由 app 层既有单位断裂守卫拦截并升级 THS 锚定全量重拉。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from .errors import FetchError

#: 锚定质量门默认参数
MAX_OVERLAP = 20        # 取最近 ≤20 根重叠日期 bar 计算比值
MIN_OVERLAP = 5         # 重叠少于 5 根视为不可锚定
MAX_CV = 0.005          # 比值变异系数上限 0.5%


class AnchorError(FetchError):
    """锚定换算不可信（重叠不足 / CV 超标 / 锚定日缺失等），不得写入文件。"""


class InsufficientOverlapError(AnchorError):
    """重叠日期不足（缺口超出源窗口/一侧为空）。

    与 CV 超标不同：这不是「锚定不可信」，而是「增量窗口锚不了」——
    调用方可升级为 THS 年度全史锚定（覆盖任意缺口）。
    """


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    out = df[["date", "close"]].copy()
    out["date"] = pd.to_datetime(out["date"])
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    return out[out["close"].notna() & (out["close"] > 0)]


def compute_anchor_ratio(old: pd.DataFrame, other: pd.DataFrame,
                         max_overlap: int = MAX_OVERLAP,
                         min_overlap: int = MIN_OVERLAP,
                         max_cv: float = MAX_CV) -> tuple[float, int]:
    """存量 hfq vs 供应商序列（qfq）重叠日期收盘价比值 -> (中位数 ratio, 重叠根数)。

    不可锚定抛 AnchorError。中位数对个别离群 bar 稳健（存量末根恰为断裂价的场景），
    CV 门保证整体一致（跨供应商复权因子差异 ~1% 或窗口内混入异基准数据时拒绝）。
    """
    if old is None or len(old) == 0 or other is None or len(other) == 0:
        raise InsufficientOverlapError("锚定需要存量与供应商两侧数据（有一侧为空）")
    m = (_clean(old).rename(columns={"close": "old_close"})
         .merge(_clean(other).rename(columns={"close": "other_close"}), on="date"))
    m = m.sort_values("date").tail(max_overlap)
    if len(m) < min_overlap:
        raise InsufficientOverlapError(f"重叠日期不足: {len(m)} < {min_overlap}，不可锚定")
    ratios = m["old_close"] / m["other_close"]
    ratio = float(ratios.median())
    if not (ratio > 0 and pd.notna(ratio)):
        raise AnchorError(f"锚定比值非法: {ratio}")
    cv = float(ratios.std(ddof=0) / ratios.mean())
    if not pd.notna(cv) or cv >= max_cv:
        raise AnchorError(f"锚定比值变异系数超标: CV={cv:.4%} >= {max_cv:.2%}，疑似混基准")
    return ratio, len(m)


def compute_tail_ratio(old: pd.DataFrame, raw: pd.DataFrame) -> float:
    """raw 续接锚定：ratio = 存量最后 bar 的 hfq_close / 该日 raw_close。

    raw 序列必须包含存量最后一根 bar 的日期（新浪窗口 ~1023 根 / 搜狐任意区间
    均覆盖最近历史）；缺失即无法锚定。
    """
    if old is None or len(old) == 0:
        raise AnchorError("raw 续接锚定需要存量数据（无本地历史）")
    if raw is None or len(raw) == 0:
        raise InsufficientOverlapError("供应商 raw 序列为空，无法锚定")
    last_day = pd.Timestamp(pd.to_datetime(old["date"]).max())
    r = _clean(raw)
    r = r[r["date"] == last_day]
    if len(r) == 0:
        raise InsufficientOverlapError(f"raw 序列缺锚定日 {last_day.date()}，无法续接")
    ratio = float(r["close"].iloc[0])
    old_close = pd.to_numeric(old["close"], errors="coerce").iloc[-1]
    if not (old_close > 0 and pd.notna(old_close)):
        raise AnchorError(f"存量末根 close 非法: {old_close}")
    return float(old_close) / ratio


def scale_prices(df: pd.DataFrame, ratio: float) -> pd.DataFrame:
    """价格列 × ratio（open/high/low/close/change 是价差也缩放）。

    volume/amount/turnover/pct_chg/amplitude 与价格基准无关，保持不变。
    返回副本，不修改入参。
    """
    out = df.copy()
    for c in ("open", "high", "low", "close", "change"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce") * ratio
    return out


def bars_after(df: pd.DataFrame, day) -> pd.DataFrame:
    """严格晚于 day 的 bar（增量续接窗口；同日 bar 由 merge_daily 去重覆盖）。"""
    if df is None or len(df) == 0:
        return df
    return df[pd.to_datetime(df["date"]) > pd.Timestamp(day)].reset_index(drop=True)


def bars_from(df: pd.DataFrame, day) -> pd.DataFrame:
    """不早于 day 的 bar（含锚定日本身）。"""
    if df is None or len(df) == 0:
        return df
    return df[pd.to_datetime(df["date"]) >= pd.Timestamp(day)].reset_index(drop=True)


def last_date(df: Optional[pd.DataFrame]):
    """序列最后日期；空序列返回 None。"""
    if df is None or len(df) == 0:
        return None
    return pd.Timestamp(pd.to_datetime(df["date"]).max())
