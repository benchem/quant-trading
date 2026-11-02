"""本地 Parquet 存储：路径规划、读写、增量合并。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import pandas as pd

# 日线统一字段（英文列名）。字段含义与单位见 README「数据字典」。
OHLCV_COLUMNS = [
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "amplitude",
    "pct_chg",
    "change",
    "turnover",
]


def daily_dir(data_dir, adjust: str) -> Path:
    return Path(data_dir) / "daily" / adjust


def index_dir(data_dir) -> Path:
    return Path(data_dir) / "index"


def dataset_dir(data_dir) -> Path:
    return Path(data_dir) / "dataset"


def stock_list_path(data_dir) -> Path:
    return Path(data_dir) / "stock_list.parquet"


def trade_cal_path(data_dir) -> Path:
    return Path(data_dir) / "trade_calendar.parquet"


def board_members_path(data_dir) -> Path:
    return Path(data_dir) / "board_members.parquet"


def quote_asof_path(data_dir) -> Path:
    """行情快照所属交易日的 sidecar（真值源）。

    2026-11-02 事故：行情源失败时 EOD 仍用旧 stock_list 重写
    dataset/stock_basic 副本（mtime=今天、内容=旧价），mtime 推断的
    「快照所属日」被污染，全站把陈旧快照价当最新。此文件记录行情
    字段真正拉到新数据的交易日；行情源失败时不 bump（保留旧值）。
    """
    return Path(data_dir) / "quote_asof.json"


def read_quote_date(data_dir) -> Optional[str]:
    """读 sidecar 的 quote_date（YYYY-MM-DD）；缺失/损坏/非法返回 None。"""
    try:
        obj = json.loads(quote_asof_path(data_dir).read_text(encoding="utf-8"))
        d = obj.get("quote_date") if isinstance(obj, dict) else None
        d = str(d)[:10] if d else None
        if d and len(d) == 10 and d[4] == "-" and d[7] == "-":
            return d
        return None
    except Exception:  # noqa: BLE001 读取端绝不因 sidecar 问题阻塞
        return None


def write_quote_date(data_dir, quote_date: str) -> None:
    """写 sidecar（临时文件 + rename 原子替换，避免读到半个 JSON）。"""
    p = quote_asof_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"quote_date": str(quote_date)[:10]}, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, p)


def save_parquet(path: Path, df: pd.DataFrame) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False, engine="pyarrow")


def load_daily(path: Path) -> Optional[pd.DataFrame]:
    """读取单只证券的日线文件；文件不存在返回 None。"""
    path = Path(path)
    if not path.exists():
        return None
    return pd.read_parquet(path, engine="pyarrow")


def merge_daily(old: Optional[pd.DataFrame], new: pd.DataFrame) -> pd.DataFrame:
    """合并增量数据：按 (code, date) 去重（保留新值），按日期排序。"""
    if old is None or len(old) == 0:
        merged = new.copy()
    else:
        merged = pd.concat([old, new], ignore_index=True)
    merged = merged.drop_duplicates(subset=["code", "date"], keep="last")
    merged = merged.sort_values(["code", "date"], kind="stable").reset_index(drop=True)
    return merged


def parquet_date_range(path: Path):
    """读取 parquet 中 date 列的 (min, max)，优先用行组统计信息，避免全量读列。"""
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(path)
    names = pf.schema_arrow.names
    if "date" not in names:
        return None
    idx = names.index("date")
    try:
        mn = mx = None
        for rg in range(pf.metadata.num_row_groups):
            st = pf.metadata.row_group(rg).column(idx).statistics
            if st is None or not st.has_min_max:
                raise ValueError("no stats")
            mn = st.min if mn is None else min(mn, st.min)
            mx = st.max if mx is None else max(mx, st.max)
        if mn is None:
            return None
        return pd.Timestamp(mn), pd.Timestamp(mx)
    except Exception:  # noqa: BLE001 - 统计缺失时回退到读列
        col = pf.read(columns=["date"]).column("date")
        if len(col) == 0:
            return None
        return pd.Timestamp(col[0].as_py()), pd.Timestamp(col[len(col) - 1].as_py())
