"""数据访问层：读取 downloader 落盘的本地 Parquet 数据。

数据布局（由 `python -m downloader all && python -m downloader build` 生成）::

    data/
    ├── stock_list.parquet / dataset/stock_basic.parquet   # 股票基础信息（含市值）
    ├── trade_calendar.parquet                              # 交易日历
    ├── daily/hfq/{code}.parquet                            # 个股后复权日线（每股一文件）
    ├── index/{code}.parquet                                # 指数日线
    └── dataset/daily_hfq.parquet                           # 全市场合并长表

本层在启动时一次性预计算「市场日度聚合」（涨跌家数/成交额/等权净值），
个股 K 线与排行榜按需查询（带缓存）。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.dataset as pad

from .minute import info as minute_info

ANNUALIZE = 244  # A股年均交易日

#: 指数代码 -> 中文名
INDEX_NAMES = {
    "000001.SH": "上证指数",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "000688.SH": "科创50",
}

_RANK_METRICS = ("ret", "vol", "maxdd", "turnover")
_RANK_CACHE_MAX = 16

#: 市场板块（交易所板块划分，按代码前缀本地推导，无需外部数据）: key -> (名称, 代码前缀)
# 沪市 60x/605=主板、688/689=科创板；深市 00x=主板、300/301/302=创业板；北交所 43/83/87/92x
MARKET_SEGMENTS = (
    ("sh_main", "沪主板", ("600", "601", "603", "605")),
    ("sz_main", "深主板", ("000", "001", "002", "003")),
    ("cyb", "创业板", ("300", "301", "302")),
    ("kcb", "科创板", ("688", "689")),
    ("bj", "北交所", ("43", "83", "87", "88", "92")),
)


def market_of(code: str) -> str | None:
    """代码 -> 市场板块 key（沪主板/深主板/创业板/科创板/北交所）；无法识别返回 None。"""
    num = code.split(".")[0]
    for key, _name, prefixes in MARKET_SEGMENTS:
        if num.startswith(prefixes):
            return key
    return None

#: 交易流水记录上限（全部股票合计）
_MAX_TRADES = 5000
_MAX_BATCH = 500  # 单次批量导入上限
# trade_update 的哨兵值：区分「不改动费用」与「清空费用(None)」
_UNSET = object()


class DataStore:
    def __init__(self, data_dir: str | Path = "data"):
        self.root = Path(data_dir).resolve()
        self.daily_dir = self.root / "daily" / "hfq"
        self.index_dir = self.root / "index"

        if not self.daily_dir.is_dir() or not list(self.daily_dir.glob("*.parquet")):
            raise SystemExit(
                f"未找到个股日线目录 {self.daily_dir}，"
                "请先运行: python -m downloader all && python -m downloader build"
            )

        self._rank_cache: dict[tuple, pd.DataFrame] = {}
        self._rank_order: list[tuple] = []
        self._chips_cache: dict[str, dict] = {}
        self._last_cache: dict[str, dict | None] = {}
        self._daily_cache: dict[str, pd.DataFrame | None] = {}
        self._close_at_cache: dict[tuple, float | None] = {}
        # 本地 parquet 派生数据（财报摘要 / 分钟线概况）的 mtime 失效缓存：
        # 自选列表每次刷新都会逐股调用，避免每请求都整表重读磁盘。
        self._fund_cache: dict[str, tuple[int, dict | None]] = {}
        self._minute_info_cache: dict[str, tuple[int, dict | None]] = {}

        # 多用户数据隔离：自选股/交易流水按用户分桶（data/users/{user}/），
        # 首次访问惰性加载；user=None 为「共享」单用户模式（serve --no-auth），
        # 沿用根级 watchlist.json / trades.json，与升级前行为完全一致。
        self._buckets: dict[str | None, dict] = {}
        self._buckets_lock = threading.Lock()

        t0 = time.time()
        self._load_all(t0)

    # ------------------------------------------------------------- reload

    def _load_all(self, t0: float) -> None:
        """加载/重载全部启动数据（构造与 reload() 共用）。

        各属性整体一次性替换（原子赋值），并发读请求要么看到旧快照、
        要么看到新快照，不会读到半更新状态。
        """
        self.basic = self._load_basic()
        self.calendar = self._load_calendar()
        self.indexes: dict[str, pd.DataFrame] = self._load_indexes()
        self.market = self._build_market_daily()
        self.board_df, self.board_all_df = self._load_board_members()
        # 市场板块映射（代码前缀推导）：code -> key，以及各板块股票数
        self._stock_market = {c: market_of(c) for c in self.basic.index}
        self._market_counts: dict[str, int] = {}
        for k in self._stock_market.values():
            if k:
                self._market_counts[k] = self._market_counts.get(k, 0) + 1
        # 快照所属交易日（真实价折算锚点；日线比快照更旧时退化为日线最大日期）
        snap = self._snapshot_date()
        if snap is None and len(self.market):
            snap = pd.Timestamp(self.market.index.max()).normalize()
        self.snapshot_date = snap
        # 最后一个「覆盖完整」的交易日：部分刷新日（如只刷了 533/5223 只）
        # 的涨跌家数/成交额口径不完整，footer 的「数据截至」应指向最后全量日
        last_full = self.market.index[~self.market["partial"]] if len(self.market) else pd.DatetimeIndex([])
        self._last_full_day = last_full.max() if len(last_full) else None
        self.meta = {
            "n_stocks": int(self.basic.shape[0]),
            "date_min": str(self.market.index.min().date()),
            "date_max": str(self.market.index.max().date()),
            "last_trade_date": str(self._last_full_day.date()) if self._last_full_day is not None
            else str(self.market.index.max().date()),
            "snapshot_date": str(self.snapshot_date.date()) if self.snapshot_date is not None else None,
            "n_stocks_last_day": int(self.market["n_codes"].iloc[-1]) if len(self.market) else 0,
            "indexes": [
                {"code": c, "name": INDEX_NAMES.get(c, c)} for c in sorted(self.indexes)
            ],
            "markets": [
                {"key": k, "name": name, "n_stocks": self._market_counts.get(k, 0)}
                for k, name, _p in MARKET_SEGMENTS
                if self._market_counts.get(k, 0)
            ],
            "boards_available": self.board_df is not None,
            "n_boards": int(self.board_df["board_code"].nunique()) if self.board_df is not None else 0,
            "concept_boards_available": bool(
                self.board_all_df is not None and (self.board_all_df["board_type"] == "concept").any()
            ),
            "load_seconds": round(time.time() - t0, 2),
        }

    def clear_caches(self) -> None:
        """清空进程内查询缓存（日线数据更新后由调度器 / 手动触发调用）。"""
        self._rank_cache.clear()
        self._rank_order.clear()
        self._chips_cache.clear()
        self._last_cache.clear()
        self._daily_cache.clear()
        self._close_at_cache.clear()
        self._fund_cache.clear()
        self._minute_info_cache.clear()

    def reload(self) -> dict:
        """数据落盘后重载启动数据并清空查询缓存，返回新 meta。"""
        self.clear_caches()
        self._load_all(time.time())
        return self.meta

    # ------------------------------------------------------------------ load

    def _load_basic(self) -> pd.DataFrame:
        """基础信息（name/latest_price/市值/涨跌幅）：两个来源按落盘时间取新者。

        - ``stock_list.parquet``：实时快照，`downloader` 的 list/base 阶段刷新
          （含盘中刷新，mtime 新但可能未收盘）；
        - ``dataset/stock_basic.parquet``：build_dataset 从 stock_list 复制的
          归档副本（全量更新完成后刷新，口径更"整"）。

        旧版固定优先 stock_basic，导致「只刷新了 stock_list/部分日线」时
        （部分更新场景）全站取价仍停留在旧快照。现按 mtime 取新，
        并把「快照所属交易日」记入 ``self.snapshot_date``（= 该股日线序列中
        ≤ mtime 的最大交易日；无 mtime 信息或日线更早时退化为日线末根日期），
        供 ``price_info`` 做 快照价×hfq收盘比 的跨日复权。
        """
        p_live = self.root / "stock_list.parquet"
        p_arch = self.root / "dataset" / "stock_basic.parquet"
        cands = [p for p in (p_live, p_arch) if p.exists()]
        if not cands:
            raise SystemExit(f"{self.root} 下未找到股票基础信息表")
        try:
            cands.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        except OSError:
            pass
        df, p, mday = None, None, None
        # 按 mtime 从新到旧尝试：最新的文件可能正被非原子写入（半个 parquet），
        # 读取失败时回退到旧文件，而不是让整个服务起不来
        for cand in cands:
            try:
                df = pd.read_parquet(cand)
                df = df.drop_duplicates("code").set_index("code")
                for col in ("latest_price", "pct_chg", "total_mv", "float_mv"):
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                p = cand
                try:
                    mday = pd.Timestamp.fromtimestamp(cand.stat().st_mtime).tz_localize(None).normalize()
                except (OSError, OverflowError, ValueError):
                    mday = None
                break
            except Exception:  # noqa: BLE001  半写/损坏文件：试下一个来源
                continue
        if df is None:
            raise SystemExit(f"{self.root} 下股票基础信息表均不可读: {[str(c) for c in cands]}")
        self._basic_path = p
        self._snapshot_mtime_day = mday
        return df

    def _snapshot_date(self) -> pd.Timestamp | None:
        """快照所属交易日（近似 mtime→日，再回退到 ≤ 该日的最近交易日）；
        日线数据可能比快照更旧（部分更新只刷了 stock_list）——price_info
        的锚点日 = min(快照日, 日线末根日)，各自取各自可用范围，无需此处校正。"""
        mday = getattr(self, "_snapshot_mtime_day", None)
        if mday is None:
            return None
        if len(self.calendar):
            c = self.calendar
            if mday < c.min():
                mday = c.min()
            elif mday > c.max():
                mday = c.max()
            # 回退到 ≤ mday 的最近交易日（非交易日快照取前一日）
            cands = c[c <= mday]
            if len(cands):
                mday = cands.max()
        return mday

    def _load_board_members(self) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
        """板块成分表（downloader board 生成）；缺失返回 (None, None)（板块功能降级）。

        返回 (行业映射, 全量成分)：
        - 行业映射 code -> (board_code, board_name)，每股唯一（互斥划分），
          用于个股表「板块」列；
        - 全量成分 [board_type, board_code, board_name]（index=code，概念板块成分
          有重叠、一股多行），用于板块排行与板块筛选。
        兼容旧文件（无 board_type 列，全部视为 industry）。
        """
        p = self.root / "board_members.parquet"
        if not p.exists():
            return None, None
        try:
            df = pd.read_parquet(p)
            if "board_type" not in df.columns:
                df["board_type"] = "industry"
            ind = df[df["board_type"] == "industry"]
            ind_map = (
                ind.drop_duplicates("code").set_index("code")[["board_code", "board_name"]]
                if len(ind) else None
            )
            all_df = df.drop_duplicates(["code", "board_code"]).set_index("code")
            return ind_map, all_df
        except Exception:  # noqa: BLE001 文件损坏不阻塞主功能
            return None, None

    def _board_of(self, code: str) -> tuple[str | None, str | None]:
        """代码 -> (行业board_code, board_name)；无行业映射返回 (None, None)。

        个股表「板块」列固定展示行业归属（划分、每股唯一；概念成分有重叠不适合做标签列）。
        """
        if self.board_df is None or code not in self.board_df.index:
            return None, None
        r = self.board_df.loc[code]
        return str(r["board_code"]), str(r["board_name"])

    def _load_calendar(self) -> pd.DatetimeIndex:
        p = self.root / "trade_calendar.parquet"
        if p.exists():
            df = pd.read_parquet(p)
            return pd.DatetimeIndex(pd.to_datetime(df.iloc[:, 0]))
        return pd.DatetimeIndex(sorted(self.market.index))

    def _load_indexes(self) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for f in sorted(self.index_dir.glob("*.parquet")):
            code = f.stem
            df = pd.read_parquet(f)
            if "date" not in df.columns or "close" not in df.columns:
                continue
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").set_index("date")
            df = df[~df.index.duplicated(keep="last")]
            out[code] = df
        return out

    def _scan_daily_max_date(self) -> pd.Timestamp | None:
        """pyarrow 扫描单股目录所有 bar 的最大日期（~0.7s/5千股），供滞后检测。"""
        try:
            ds = pad.dataset(self.daily_dir, format="parquet")
            t = ds.to_table(columns=["date"])
            m = pc.max(t["date"])
            if m is None or m.is_valid is False or m.as_py() is None:
                return None
            return pd.Timestamp(m.as_py()).normalize()
        except Exception:  # noqa: BLE001
            return None

    def _build_market_daily(self) -> pd.DataFrame:
        """全市场日度聚合：涨跌家数 / 等权收益 / 成交额 / 等权累计净值。

        合并表 ``dataset/daily_hfq.parquet`` 由 EOD 的 build_dataset 阶段生成，
        可能滞后于单股文件（部分刷新场景：只刷了部分股票的日线、还没跑
        build_dataset）。这里先扫单股目录的最大日期，若比合并表新，
        直接从单股目录聚合（4 列，~3.5s），保证「今日刚刷新的 K 线」
        立即反映到市场页，而不是等下一次全量构建。

        另加 ``n_codes``（当日有数据的股票数）：部分刷新日只有几百只股票，
        涨跌家数/成交额口径不完整，``last_trade_date`` 应指向最后一个
        「覆盖完整」的交易日，而不是把半截日当成全市场日。
        """
        p = self.root / "dataset" / "daily_hfq.parquet"
        cols = ["date", "code", "close", "amount"]
        t = None
        if p.exists():
            t = pd.read_parquet(p, columns=cols)
        merged_max = pd.Timestamp(t["date"].max()).normalize() if t is not None and len(t) else None
        live_max = self._scan_daily_max_date()
        # 单股目录比合并表新（部分刷新）→ 从单股目录聚合，并做标记
        self._market_from_stocks = bool(live_max and (merged_max is None or live_max > merged_max))
        if t is None or self._market_from_stocks:
            files = list(self.daily_dir.glob("*.parquet"))
            t = pd.concat((pd.read_parquet(f, columns=cols) for f in files), ignore_index=True)

        t["date"] = pd.to_datetime(t["date"])
        t = t.sort_values(["code", "date"])
        prev = t.groupby("code")["close"].shift(1)
        t["ret"] = t["close"] / prev - 1.0

        adv = (t["ret"] > 0).groupby(t["date"]).sum()
        dec = (t["ret"] < 0).groupby(t["date"]).sum()
        flat = (t["ret"] == 0).groupby(t["date"]).sum()
        n_codes = t.groupby("date")["code"].nunique()
        # 等权均值/中位数对单日极端值（hfq 因子跳变、新股首日）做 ±100% 截断
        ret_clip = t["ret"].clip(-1.0, 1.0)
        mean_ret = ret_clip.groupby(t["date"]).mean()
        med_ret = ret_clip.groupby(t["date"]).median()
        amount = t["amount"].groupby(t["date"]).sum()

        m = pd.DataFrame(
            {
                "adv": adv,
                "dec": dec,
                "flat": flat,
                "n_codes": n_codes,
                "mean_ret": mean_ret,
                "med_ret": med_ret,
                "amount_yi": amount / 1e8,  # 亿元
            }
        )
        # 部分覆盖日：涨跌家数/成交额只含已刷新的股票，标记出来供展示层降级
        full_n = int(n_codes.max()) if len(n_codes) else 0
        m["partial"] = n_codes < full_n * 0.9
        m["nav_eqw"] = (1.0 + m["mean_ret"].fillna(0.0)).cumprod()
        m.index = m.index.normalize()
        m.index.name = "date"
        return m

    # ----------------------------------------------------------------- query

    # ------------------------------------------------------ 多用户分桶

    def user_dir(self, user: str | None) -> Path:
        """用户数据目录：None(共享模式) -> 根目录；用户 -> data/users/{user}/。"""
        if user is None:
            return self.root
        return self.root / "users" / user

    def _bucket(self, user: str | None) -> dict:
        """取（并惰性加载）某用户的自选/交易桶。

        桶 = {wl_path, trades_path, wl_lock, trades_lock, wl_codes, trades}；
        不同用户互不共享内存状态与文件，实现数据隔离。
        """
        with self._buckets_lock:
            b = self._buckets.get(user)
            if b is not None:
                return b
            base = self.user_dir(user)
            b = {
                "user": user,
                "wl_path": base / "watchlist.json",
                "trades_path": base / "trades.json",
                "wl_lock": threading.Lock(),
                "trades_lock": threading.Lock(),
                "wl_codes": self._load_watchlist_file(base / "watchlist.json"),
                "trades": self._load_trades_file(base / "trades.json"),
            }
            self._buckets[user] = b
            return b

    def drop_bucket(self, user: str | None) -> None:
        """丢弃某用户的内存桶（legacy 迁移/文件被外部改动后强制重读）。"""
        with self._buckets_lock:
            self._buckets.pop(user, None)

    def known_users(self) -> list[str]:
        """data/users/ 下已存在数据目录的用户名（按名称排序）。"""
        d = self.root / "users"
        if not d.is_dir():
            return []
        return sorted(p.name for p in d.iterdir() if p.is_dir() and not p.name.startswith("."))

    def watchlist_codes_all(self) -> list[str]:
        """全部用户自选股并集（去重、首现顺序）——分钟线后台同步 / 盘后更新视角。"""
        from .auth import all_watchlist_codes

        return all_watchlist_codes(self.root)

    @property
    def _wl_codes(self) -> list[str]:
        """兼容旧调用（EodUpdater 等后台任务）：返回全部用户自选并集。"""
        return self.watchlist_codes_all()

    def migrate_legacy_to(self, user: str) -> list[str]:
        """首个用户注册时，把根级 legacy 数据文件迁入该用户目录。

        迁移 watchlist.json / trades.json / notifications.jsonl（存在才迁；
        目标已存在则跳过不覆盖）。返回实际迁移的文件名列表。
        """
        moved: list[str] = []
        for name in ("watchlist.json", "trades.json", "notifications.jsonl"):
            src = self.root / name
            if not src.is_file():
                continue
            dst_dir = self.root / "users" / user
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / name
            if dst.is_file():
                continue
            os.replace(src, dst)
            moved.append(name)
        if moved:
            self.drop_bucket(None)
            self.drop_bucket(user)
        return moved

    # ------------------------------------------------------------ watchlist

    @staticmethod
    def _load_watchlist_file(path: Path) -> list[str]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            codes = data.get("codes", [])
            return [c for c in codes if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(c))]
        except (FileNotFoundError, json.JSONDecodeError, TypeError, OSError):
            return []

    def _save_watchlist(self, user: str | None) -> None:
        b = self._bucket(user)
        path = b["wl_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            "w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
        )
        try:
            json.dump({"codes": b["wl_codes"], "updated": time.strftime("%Y-%m-%d %H:%M:%S")},
                      tmp, ensure_ascii=False, indent=2)
            tmp.close()
            os.replace(tmp.name, path)
        except BaseException:
            tmp.close()
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise

    def watchlist(self, user: str | None) -> list[dict]:
        """自选股列表（含基础信息），按加入顺序。"""
        b = self._bucket(user)
        out = []
        for code in b["wl_codes"]:
            if code in self.basic.index:
                r = self.basic.loc[code]
                lb = self.hfq_last(code)
                pi = self.price_info(code)
                m = self.minute_info(code)
                out.append({
                    "code": code,
                    "name": str(r.get("name", code)),
                    # 展示价用统一真实价（快照价×hfq收盘比），与K线/持仓同口径；
                    # pct_chg/total_mv 是快照日全市场值，保持快照口径
                    "latest_price": pi["real"] if pi["real"] is not None else _r(r.get("latest_price")),
                    "pct_chg": _pct(r.get("pct_chg")),
                    "total_mv": _r(r.get("total_mv")),
                    "hfq_close": lb["close"] if lb else None,
                    "hfq_date": lb["date"] if lb else None,
                    "minute_bars": m["bars"] if m else None,
                    "minute_last": m["last"] if m else None,
                    "fund": self.fund_latest(code),
                    "info": self.stock_info(code),
                })
        return out

    def fund_latest(self, code: str) -> dict | None:
        """最新报告期财报完整摘要（仅读本地缓存，不发网络请求；无缓存返回 None）。

        结果按文件 (mtime_ns ^ size) 失效缓存：自选列表每次刷新都会
        逐股调用本方法，缓存后同一进程内不再重复整表读盘。
        """
        code = code.upper()
        f = self.root / "fundamental" / f"{code}.parquet"
        try:
            st = f.stat()
            stamp = st.st_mtime_ns ^ st.st_size
        except OSError:
            self._fund_cache.pop(code, None)
            return None
        hit = self._fund_cache.get(code)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        out = self._fund_read(f)
        self._fund_cache[code] = (stamp, out)
        if len(self._fund_cache) > 256:
            self._fund_cache.pop(next(iter(self._fund_cache)), None)
        return out

    @staticmethod
    def _fund_read(f: Path) -> dict | None:
        try:
            df = pd.read_parquet(f)
        except Exception:  # noqa: BLE001
            return None
        if df.empty or "period" not in df.columns:
            return None
        row = df.sort_values("period").iloc[-1]

        def g(col):
            v = row.get(col)
            try:
                if v is None or pd.isna(v):
                    return None
            except (TypeError, ValueError):
                pass
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        return {
            "period": str(row["period"])[:10],
            "rev": g("rev"),
            "rev_yoy": g("rev_yoy"),
            "net_profit": g("net_profit"),
            "net_profit_yoy": g("net_profit_yoy"),
            "kdj_profit": g("kdj_profit"),
            "kdj_profit_yoy": g("kdj_profit_yoy"),
            "eps": g("eps"),
            "roe": g("roe"),
            "gross_margin": g("gross_margin"),
            "net_margin": g("net_margin"),
            "debt_ratio": g("debt_ratio"),
        }

    def minute_info(self, code: str) -> dict | None:
        """自选股本地分钟线概况（{bars, first, last}），按文件 mtime 失效缓存。

        watchlist() 每次刷新对每只自选股都会取一次，缓存后避免重复整表读盘。
        """
        code = code.upper()
        f = self.root / "minute" / "1" / f"{code}.parquet"
        try:
            st = f.stat()
            stamp = st.st_mtime_ns ^ st.st_size
        except OSError:
            self._minute_info_cache.pop(code, None)
            return None
        hit = self._minute_info_cache.get(code)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        out = minute_info(code, self.root)
        self._minute_info_cache[code] = (stamp, out)
        if len(self._minute_info_cache) > 256:
            self._minute_info_cache.pop(next(iter(self._minute_info_cache)), None)
        return out

    def stock_info(self, code: str) -> dict | None:
        """个股信息（仅本地数据）：流通市值/换手率/52周高低/年初至今/一年涨跌幅。

        价格类指标统一折算为真实价单位（与 kline 同口径）。
        """
        code = code.upper()
        if code not in self.basic.index:
            return None
        r = self.basic.loc[code]
        info = {
            "float_mv": _r(r.get("float_mv")),
            "turnover": _pct(r.get("turnover")),  # 数据源百分数 -> 小数
            "week52_high": None,
            "week52_low": None,
            "ytd_chg": None,
            "y1y_chg": None,
        }
        df = self._daily(code)
        if df is None or df.empty:
            return info
        info_p = self.price_info(code)
        factor = info_p["factor"]
        closes = df["close"].astype(float)
        highs = df["high"].astype(float) if "high" in df else closes
        lows = df["low"].astype(float) if "low" in df else closes
        if factor:
            closes, highs, lows = closes * factor, highs * factor, lows * factor
        last_close = closes.iloc[-1]
        if len(df):
            info["week52_high"] = _r(highs.tail(244).max())
            info["week52_low"] = _r(lows.tail(244).min())
        # 年初至今：以上一年最后一个交易日收盘为基准
        this_year = int(df["date"].dt.year.max())
        prev = df[df["date"].dt.year == this_year - 1]
        if len(prev):
            base = _r(closes.loc[prev.index[-1]])
            if base:
                info["ytd_chg"] = _r(last_close / base - 1.0)
        if len(df) > 245:
            info["y1y_chg"] = _r(last_close / closes.iloc[-245] - 1.0)
        return info

    def _daily(self, code: str) -> pd.DataFrame | None:
        """个股日线 (date/high/low/close)，带小缓存；无数据返回 None。"""
        code = code.upper()
        if code in self._daily_cache:
            return self._daily_cache[code]
        f = self.daily_dir / f"{code}.parquet"
        df = None
        if f.exists():
            try:
                # 只读 schema 判列（避免为探测列名而整表读一遍）
                import pyarrow.parquet as ppq

                cols = [c for c in ("date", "high", "low", "close")
                        if c in ppq.read_schema(f).names]
                df = pd.read_parquet(f, columns=cols)
                df["date"] = pd.to_datetime(df["date"])
                df = df.sort_values("date")
            except Exception:  # noqa: BLE001
                df = None
        self._daily_cache[code] = df
        if len(self._daily_cache) > 64:
            self._daily_cache.pop(next(iter(self._daily_cache)), None)
        return df

    def valuation(self, code: str, refresh: bool = False) -> dict:
        """个股估值 PE(TTM)/PE(静)/PB/PEG/PS（东财估值分析，本地缓存 30 分钟）。"""
        from .valuation import fetch

        return fetch(code, self.root, refresh=refresh)

    def news(self, code: str, refresh: bool = False) -> dict:
        """个股最新新闻（本地缓存优先，TTL 30 分钟；失败回退旧缓存，见 news.py）。"""
        from .news import fetch

        return fetch(code, self.root, refresh=refresh)

    def watchlist_codes(self, user: str | None) -> list[str]:
        """某用户的自选股代码列表（按加入顺序）。"""
        b = self._bucket(user)
        with b["wl_lock"]:
            return list(b["wl_codes"])

    def watchlist_add(self, user: str | None, code: str) -> tuple[list[str], bool]:
        code = code.upper()
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
            raise ValueError(f"代码格式错误: {code}")
        b = self._bucket(user)
        with b["wl_lock"]:
            added = False
            if code not in b["wl_codes"]:
                if len(b["wl_codes"]) >= 300:
                    raise ValueError("自选股上限 300 只")
                b["wl_codes"].append(code)
                added = True
            self._save_watchlist(user)
            return list(b["wl_codes"]), added

    def watchlist_remove(self, user: str | None, code: str) -> tuple[list[str], bool]:
        code = code.upper()
        b = self._bucket(user)
        with b["wl_lock"]:
            removed = code in b["wl_codes"]
            b["wl_codes"] = [c for c in b["wl_codes"] if c != code]
            self._save_watchlist(user)
            return list(b["wl_codes"]), removed

    def watchlist_reorder(self, user: str | None, codes: list[str]) -> list[str]:
        """按给定顺序重排自选股（网页拖拽）。

        - 只接受当前已在自选中的代码（未知/已退市代码忽略），重复去重；
        - 未列出的自选股按原相对顺序追加在末尾，保证不丢股；
        """
        b = self._bucket(user)
        with b["wl_lock"]:
            cur = list(b["wl_codes"])
            cur_set = set(cur)
            new: list[str] = []
            for c in codes:
                c = str(c).upper()
                if c in cur_set and c not in new:
                    new.append(c)
            for c in cur:
                if c not in new:
                    new.append(c)
            b["wl_codes"] = new
            self._save_watchlist(user)
            return list(b["wl_codes"])

    def watchlist_clear(self, user: str | None) -> list[str]:
        """清空某用户的全部自选股。"""
        b = self._bucket(user)
        with b["wl_lock"]:
            b["wl_codes"] = []
            self._save_watchlist(user)
            return []

    # ------------------------------------------------------------- trades
    # 自选股买卖流水：手工登记（date/side/price/shares/note），持久化到
    # data/trades.json，网页 K 线上以 ▲买 / ▼卖 标记展示，并据此计算持仓成本。

    def _load_trades_file(self, path: Path) -> list[dict]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, TypeError, OSError):
            return []
        trades = []
        for t in data.get("trades", []):
            if not isinstance(t, dict):
                continue
            code = str(t.get("code", "")).upper()
            if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
                continue
            side = t.get("side")
            if side not in ("buy", "sell"):
                continue
            date = str(t.get("date", ""))[:10]
            try:
                pd.Timestamp(date)
            except (ValueError, TypeError):
                continue
            price, shares = _r(t.get("price")), _r(t.get("shares"))
            if price is None or price <= 0 or shares is None or shares <= 0:
                continue
            fee = _r(t.get("fee"))
            if fee is not None and (fee < 0 or not np.isfinite(fee)):
                fee = None
            trades.append({
                "id": str(t.get("id", "")),
                "code": code,
                "date": date,
                "side": side,
                "price": round(float(price), 4),
                "shares": int(shares),
                "note": str(t.get("note", ""))[:100],
                "created": str(t.get("created", "")),
                "fee": round(fee, 2) if fee is not None else None,
            })
        return sorted(trades, key=lambda t: (t["date"], t["created"]))

    def _save_trades(self, user: str | None) -> None:
        b = self._bucket(user)
        path = b["trades_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            "w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
        )
        try:
            json.dump({"trades": b["trades"], "updated": time.strftime("%Y-%m-%d %H:%M:%S")},
                      tmp, ensure_ascii=False, indent=1)
            tmp.close()
            os.replace(tmp.name, path)
        except BaseException:
            tmp.close()
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise

    def trades(self, user: str | None, code: str | None = None) -> list[dict]:
        """某用户的交易流水（按日期+登记时间升序）；code 为空时返回该用户全部。"""
        b = self._bucket(user)
        with b["trades_lock"]:
            ts = [dict(t) for t in b["trades"]]
        if code:
            code = code.upper()
            ts = [t for t in ts if t["code"] == code]
        return ts

    @staticmethod
    def _validate_trade_fields(
        date: str,
        side: str,
        price: float,
        shares: int,
        note: str = "",
        fee: float | None = None,
    ) -> tuple[str, str, float, int, str, float | None]:
        """单笔交易字段校验；返回规范化后的 (date, side, price, shares, note, fee)。"""
        try:
            date = pd.Timestamp(str(date)).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            raise ValueError(f"日期格式错误: {date}（应为 YYYY-MM-DD）")
        if side not in ("buy", "sell"):
            raise ValueError("side 应为 buy/sell")
        try:
            price = float(price)
        except (TypeError, ValueError):
            raise ValueError("价格应为正数")
        if not np.isfinite(price) or price <= 0:
            raise ValueError("价格应为正数")
        try:
            shares_f = float(shares)
        except (TypeError, ValueError):
            raise ValueError("股数应为正整数")
        if not shares_f.is_integer() or shares_f <= 0:
            raise ValueError("股数应为正整数")
        shares = int(shares_f)
        note = (note or "").strip()[:100]
        if fee is not None:
            try:
                fee = float(fee)
            except (TypeError, ValueError):
                raise ValueError("费用应为非负数")
            if not np.isfinite(fee) or fee < 0:
                raise ValueError("费用应为非负数")
            fee = round(fee, 2)
        return date, side, price, shares, note, fee

    def _make_trade_rec(self, code: str, date, side, price, shares, note, fee) -> dict:
        rec = {
            "id": time.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "code": code,
            "date": date,
            "side": side,
            "price": round(price, 4),
            "shares": shares,
            "note": note,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if fee is not None:
            rec["fee"] = fee
        return rec

    def trade_add(
        self,
        user: str | None,
        code: str,
        date: str,
        side: str,
        price: float,
        shares: int,
        note: str = "",
        fee: float | None = None,
    ) -> tuple[list[dict], dict]:
        code = (code or "").strip().upper()
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
            raise ValueError(f"代码格式错误: {code}")
        date, side, price, shares, note, fee = self._validate_trade_fields(
            date, side, price, shares, note, fee
        )
        b = self._bucket(user)
        with b["trades_lock"]:
            if len(b["trades"]) >= _MAX_TRADES:
                raise ValueError(f"交易记录上限 {_MAX_TRADES} 笔")
            rec = self._make_trade_rec(code, date, side, price, shares, note, fee)
            b["trades"].append(rec)
            b["trades"].sort(key=lambda t: (t["date"], t["created"]))
            self._save_trades(user)
            return [dict(t) for t in b["trades"]], dict(rec)

    def trade_add_batch(
        self,
        user: str | None,
        code: str,
        items: list[dict],
    ) -> tuple[list[dict], list[dict], list[dict]]:
        """批量添加：逐条校验，单条失败不影响其余。

        返回 (全部流水, 成功新增的 rec 列表, 错误列表)。
        错误列表元素形如 {"index": i, "raw": 原文, "error": 原因}。
        单次最多 500 条；超过总上限的条目记为错误而非抛错。
        """
        code = (code or "").strip().upper()
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
            raise ValueError(f"代码格式错误: {code}")
        if not isinstance(items, list):
            raise ValueError("trades 应为数组")
        if not items:
            raise ValueError("trades 不能为空")
        if len(items) > _MAX_BATCH:
            raise ValueError(f"单次批量最多 {_MAX_BATCH} 条")

        b = self._bucket(user)
        added: list[dict] = []
        errors: list[dict] = []
        with b["trades_lock"]:
            for i, it in enumerate(items):
                if not isinstance(it, dict):
                    errors.append({"index": i, "raw": str(it), "error": "记录应为对象"})
                    continue
                raw = f"{it.get('date', '')} {it.get('side', '')} {it.get('price', '')} {it.get('shares', '')}".strip()
                try:
                    date, side, price, shares, note, fee = self._validate_trade_fields(
                        it.get("date"), it.get("side"), it.get("price"),
                        it.get("shares"), it.get("note", ""), it.get("fee"),
                    )
                except ValueError as e:
                    errors.append({"index": i, "raw": raw, "error": str(e)})
                    continue
                if len(b["trades"]) + len(added) >= _MAX_TRADES:
                    errors.append({"index": i, "raw": raw, "error": f"交易记录上限 {_MAX_TRADES} 笔"})
                    continue
                added.append(self._make_trade_rec(code, date, side, price, shares, note, fee))
            if added:
                b["trades"].extend(added)
                b["trades"].sort(key=lambda t: (t["date"], t["created"]))
                self._save_trades(user)
        return [dict(t) for t in b["trades"]], [dict(r) for r in added], errors

    def trade_remove(self, user: str | None, code: str, tid: str) -> tuple[list[dict], bool]:
        code = (code or "").strip().upper()
        tid = str(tid or "")
        b = self._bucket(user)
        with b["trades_lock"]:
            before = len(b["trades"])
            b["trades"] = [t for t in b["trades"] if not (t["id"] == tid and t["code"] == code)]
            removed = len(b["trades"]) < before
            if removed:
                self._save_trades(user)
            return [dict(t) for t in b["trades"]], removed

    def trade_update(
        self,
        user: str | None,
        code: str,
        tid: str,
        date: str | None = None,
        side: str | None = None,
        price: float | None = None,
        shares: int | None = None,
        note: str | None = None,
        fee: float | None = _UNSET,
    ) -> tuple[list[dict], dict]:
        """按 id 更新一笔交易（code+id 定位）；仅更新传入的非 None 字段。
        fee 语义：_UNSET=不改动，None=清空（回到自动估算），数字=设为实际费用。"""
        code = (code or "").strip().upper()
        tid = str(tid or "")
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
            raise ValueError(f"代码格式错误: {code}")
        new_date = None
        if date is not None and str(date) != "":
            try:
                new_date = pd.Timestamp(str(date)).strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                raise ValueError(f"日期格式错误: {date}（应为 YYYY-MM-DD）")
        if side is not None and side != "":
            if side not in ("buy", "sell"):
                raise ValueError("side 应为 buy/sell")
        new_price = None
        if price is not None and price != "":
            try:
                p = float(price)
            except (TypeError, ValueError):
                raise ValueError("价格应为正数")
            if not np.isfinite(p) or p <= 0:
                raise ValueError("价格应为正数")
            new_price = round(p, 4)
        new_shares = None
        if shares is not None and shares != "":
            try:
                s_f = float(shares)
            except (TypeError, ValueError):
                raise ValueError("股数应为正整数")
            if not s_f.is_integer() or s_f <= 0:
                raise ValueError("股数应为正整数")
            new_shares = int(s_f)
        new_note = (note or "").strip()[:100] if note is not None else None
        new_fee = None
        if fee is not _UNSET:
            if fee is not None:
                try:
                    f = float(fee)
                except (TypeError, ValueError):
                    raise ValueError("费用应为非负数")
                if not np.isfinite(f) or f < 0:
                    raise ValueError("费用应为非负数")
                new_fee = round(f, 2)

        b = self._bucket(user)
        with b["trades_lock"]:
            rec = next((t for t in b["trades"] if t["id"] == tid and t["code"] == code), None)
            if rec is None:
                raise ValueError("未找到该交易记录")
            if new_date is not None:
                rec["date"] = new_date
            if side:
                rec["side"] = side
            if new_price is not None:
                rec["price"] = new_price
            if new_shares is not None:
                rec["shares"] = new_shares
            if note is not None:
                rec["note"] = new_note
            if fee is not _UNSET:
                rec["fee"] = new_fee
            b["trades"].sort(key=lambda t: (t["date"], t["created"]))
            self._save_trades(user)
            return [dict(t) for t in b["trades"]], dict(rec)

    @staticmethod
    def position_of(ts: list[dict], last_price: float | None = None) -> dict:
        """移动加权平均法：持仓股数 / 持仓均价 / 已实现盈亏 / 浮动盈亏。"""
        held = 0
        avg = 0.0
        realized = 0.0
        for t in sorted(ts, key=lambda x: (x["date"], x["created"])):
            p, s = float(t["price"]), int(t["shares"])
            if t["side"] == "buy":
                avg = (avg * held + p * s) / (held + s)
                held += s
            else:
                realized += (p - avg) * min(s, held)
                held = max(0, held - s)
        unrealized = (last_price - avg) * held if (last_price is not None and held) else 0.0
        return {
            "held": held,
            "avg_cost": round(avg, 4) if held else None,
            "realized": round(realized, 2),
            "unrealized": round(unrealized, 2),
            "n_trades": len(ts),
        }

    def positions(self, user: str | None) -> dict[str, dict]:
        """某用户按代码汇总的持仓（最新价用统一真实价 real_price；无交易记录的代码不出现）。"""
        by_code: dict[str, list[dict]] = {}
        for t in self.trades(user):
            by_code.setdefault(t["code"], []).append(t)
        out = {}
        for code, lst in by_code.items():
            out[code] = self.position_of(lst, self.real_price(code))
        return out

    def search_stocks(self, q: str, limit: int = 30) -> list[dict]:
        """按代码前缀 / 代码包含 / 名称包含模糊搜索。"""
        q = (q or "").strip().upper()
        b = self.basic
        if not q:
            hit = b
        else:
            codes = b.index
            mask = codes.str.startswith(q) | codes.str.contains(q, na=False)
            mask |= b["name"].str.upper().str.contains(q, na=False)
            hit = b[mask]
        hit = hit.head(limit)
        rows = []
        for code, r in hit.iterrows():
            lb = self.hfq_last(code)
            rows.append(
                {
                    "code": code,
                    "name": str(r.get("name", code)),
                    # 统一真实价（与 K 线/持仓同口径）
                    "latest_price": self.real_price(code),
                    "pct_chg": _pct(r.get("pct_chg")),
                    "total_mv": _r(r.get("total_mv")),
                    "hfq_close": lb["close"] if lb else None,
                }
            )
        return rows

    def hfq_last(self, code: str) -> dict | None:
        """最近一根后复权 K 线 {date, close}（带缓存；真实价请用 price_info）。"""
        code = code.upper()
        if code in self._last_cache:
            return self._last_cache[code]
        f = self.daily_dir / f"{code}.parquet"
        out = None
        if f.exists():
            df = pd.read_parquet(f, columns=["date", "close"])
            if not df.empty:
                row = df.sort_values("date").iloc[-1]
                out = {"date": str(row["date"])[:10], "close": _r(row["close"])}
        self._last_cache[code] = out
        return out

    # ------------------------------------------------- 统一真实价（取价唯一入口）

    def price_info(self, code: str) -> dict:
        """某股的统一「真实价」及其折算因子 —— 全站取价的唯一入口。

        口径（与 K 线「最新价=真实价」的前复权等价）::

            real_now  = 快照真实价 × hfq_close(最新收盘) / hfq_close(快照日收盘)
            factor    = real_now / hfq_close(最新收盘)   # 乘到任意 hfq 价上

        - 快照日 = ``self.snapshot_date``（stock_list 快照所属交易日）；
        - 锚点日 = min(快照日, 该股日线末根日)；
        - 日线末根 ≤ 快照日（常规：快照已含 K 线）→ factor = 快照价/hfq末根，
          real_now = 快照价（与旧口径一致，K 线末根即真实价）；
        - 日线末根 > 快照日（快照后只刷了 K 线）→ factor = 快照价/hfq快照日收盘，
          real_now = 快照价 × hfq末根/hfq快照日收盘，把真实价外推到最新交易日。

        返回 {real, factor, snap_date, last_date}；real/factor 可为 None
        （无快照价或无 K 线）。同一进程内同一股结果一致（读同一快照与日线文件），
        因此 kline/自选/持仓/筹码/排行 各处取价恒等。
        """
        code = code.upper()
        lb = self.hfq_last(code)
        snap_str = str(self.snapshot_date.date()) if self.snapshot_date is not None else None
        real = _r(self.basic.loc[code, "latest_price"]) if code in self.basic.index else None
        if real is None or lb is None or not lb["close"]:
            return {"real": real, "factor": None,
                    "snap_date": snap_str, "last_date": lb["date"] if lb else None}
        anchor_date = lb["date"]
        if snap_str and snap_str < lb["date"]:  # K 线比快照新：锚在快照日
            anchor_date = snap_str
        if anchor_date == lb["date"]:
            anchor_close = lb["close"]
        else:
            anchor_close = self._hfq_close_at(code, anchor_date)
        factor = real / anchor_close if anchor_close else None
        real_now = lb["close"] * factor if factor else None
        return {"real": real_now, "factor": factor,
                "snap_date": snap_str, "last_date": lb["date"]}

    def real_price(self, code: str) -> float | None:
        """最新真实价（统一口径，见 price_info）。"""
        return self.price_info(code)["real"]

    def _hfq_close_at(self, code: str, date_str: str) -> float | None:
        """某股指定交易日的 hfq 收盘（带小缓存；非交易日/缺文件返回 None）。"""
        key = (code, date_str)
        if key in self._close_at_cache:
            return self._close_at_cache[key]
        out = None
        f = self.daily_dir / f"{code}.parquet"
        if f.exists():
            try:
                df = pd.read_parquet(f, columns=["date", "close"])
                row = df.loc[df["date"].astype(str).str[:10] == date_str]
                if len(row):
                    out = _r(row.iloc[-1]["close"])
            except Exception:  # noqa: BLE001
                out = None
        if len(self._close_at_cache) > 512:
            self._close_at_cache.clear()
        self._close_at_cache[key] = out
        return out

    def kline(self, code: str, start: str | None = None, end: str | None = None) -> dict | None:
        f = self.daily_dir / f"{code}.parquet"
        if not f.exists():
            return None
        df = pd.read_parquet(f)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
        # pct_chg 统一为小数口径；数据源缺失时（如部分深市股票）按收盘价补算
        calc = df["close"].astype(float).pct_change()
        if "pct_chg" in df.columns:
            pc = pd.to_numeric(df["pct_chg"], errors="coerce") / 100.0
            pc = pc.where(pc.notna(), calc)
        else:
            pc = calc
        if start:
            df = df[df["date"] >= pd.Timestamp(start)]
        if end:
            df = df[df["date"] <= pd.Timestamp(end)]
        if df.empty:
            return None
        basic_row = self.basic.loc[code] if code in self.basic.index else None
        # 价格单位：统一真实价折算（见 price_info）；整条序列 × factor，最新价=真实价
        info = self.price_info(code)
        real, factor = info["real"], info["factor"]
        hfq_last = _r(df["close"].iloc[-1])
        if factor:
            for col in ("open", "high", "low", "close"):
                df[col] = df[col].astype(float) * factor
        return {
            "code": code,
            "name": str(basic_row["name"]) if basic_row is not None else code,
            "dates": df["date"].dt.strftime("%Y-%m-%d").tolist(),
            "open": _f(df["open"].tolist()),
            "high": _f(df["high"].tolist()),
            "low": _f(df["low"].tolist()),
            "close": _f(df["close"].tolist()),
            "volume": [int(x) for x in df["volume"].tolist()],
            "amount": _f(_nanlist(df["amount"])),
            "pct_chg": _f(_nanlist(pc.reindex(df.index))),
            "turnover": _f(_nanlist(df["turnover"]) if "turnover" in df else [np.nan] * len(df)),
            "total_mv": _r(basic_row["total_mv"]) if basic_row is not None else None,
            "float_mv": _r(basic_row["float_mv"]) if basic_row is not None else None,
            "real_price": real,
            "hfq_close": hfq_last,
            "price_unit": "qfq" if factor else "hfq",
        }

    def market_series(self, start: str | None = None, end: str | None = None) -> dict:
        m = self.market
        if start:
            m = m[m.index >= pd.Timestamp(start)]
        if end:
            m = m[m.index <= pd.Timestamp(end)]
        dates = m.index.strftime("%Y-%m-%d").tolist()
        out = {
            "dates": dates,
            "adv": [int(x) for x in m["adv"]],
            "dec": [int(x) for x in m["dec"]],
            "flat": [int(x) for x in m["flat"]],
            "mean_ret": _f(_nanlist(m["mean_ret"])),
            "amount_yi": _f(_nanlist(m["amount_yi"])),
            "nav_eqw": _f(m["nav_eqw"]),
            "indexes": {},
        }
        idx = m.index
        for code, idf in self.indexes.items():
            close = idf["close"].reindex(idx)
            out["indexes"][code] = {"name": INDEX_NAMES.get(code, code), "close": _f(_nanlist(close))}
        return out

    def _rank_table(self, start: str, end: str) -> pd.DataFrame:
        """逐股区间统计表（带 LRU 缓存），个股/板块排行共用。"""
        key = (start, end)
        table = self._rank_cache.get(key)
        if table is None:
            table = self._compute_rank_table(start, end)
            self._rank_cache[key] = table
            self._rank_order.append(key)
            if len(self._rank_order) > _RANK_CACHE_MAX:
                self._rank_cache.pop(self._rank_order.pop(0), None)
        return table

    @staticmethod
    def _need_days(start: str, end: str, calendar: pd.DatetimeIndex) -> tuple[int, int]:
        """区间内交易日数 + 最低样本量（5~20 之间取区间长度的比例下限）。"""
        n_days = int(((calendar >= pd.Timestamp(start)) & (calendar <= pd.Timestamp(end))).sum())
        return n_days, max(5, min(20, n_days))

    def rank(
        self,
        start: str,
        end: str,
        metric: str = "ret",
        n: int = 30,
        ascending: bool = False,
        board: str | None = None,
        market: str | None = None,
    ) -> dict:
        """个股区间横截面排行：ret 区间收益 / vol 年化波动 / maxdd 最大回撤 / turnover 平均换手。

        board: 板块代码（BKxxxx）过滤，仅返回该板块成分股（行业/概念板块均可）。
        market: 市场板块 key（sh_main/sz_main/cyb/kcb/bj）过滤。
        """
        if metric not in _RANK_METRICS:
            metric = "ret"
        table = self._rank_table(start, end)

        n_days, need = self._need_days(start, end, self.calendar)
        t = table[table["n_days"] >= need].copy()
        if board:
            if self.board_all_df is None:
                t = t.iloc[0:0]
            else:
                in_board = self.board_all_df.index[self.board_all_df["board_code"] == board]
                t = t.loc[t.index.intersection(in_board)]
        if market:
            t = t.loc[[c for c in t.index if self._stock_market.get(c) == market]]
        t = t.sort_values(metric, ascending=ascending).head(int(n))
        rows = []
        for code, r in t.iterrows():
            bc, bn = self._board_of(code)
            rows.append(
                {
                    "code": code,
                    "name": str(r["name"]),
                    "board_code": bc,
                    "board_name": bn,
                    "ret": _r(r["ret"]),
                    "vol": _r(r["vol"]),
                    "maxdd": _r(r["maxdd"]),
                    "turnover": _r(r["turnover"]),
                    "total_mv": _r(r["total_mv"]),
                    "n_days": int(r["n_days"]),
                }
            )
        return {"start": start, "end": end, "metric": metric, "n_days": n_days, "rows": rows}

    def _board_members_of(self, board_type: str) -> pd.DataFrame | None:
        """指定类型（industry/concept）的板块成分表；该类型无数据返回 None。"""
        if self.board_all_df is None:
            return None
        df = self.board_all_df[self.board_all_df["board_type"] == board_type]
        return df if len(df) else None

    def boards(self, board_type: str = "industry") -> list[dict]:
        """板块列表（含成分股数，按板块代码排序）。board_type: industry | concept。"""
        df = self._board_members_of(board_type)
        if df is None:
            return []
        g = df.groupby("board_code").agg(
            board_name=("board_name", "first"),
            n_stocks=("board_code", "size"),
        )
        return [
            {"board_code": str(bc), "board_name": str(r["board_name"]), "n_stocks": int(r["n_stocks"])}
            for bc, r in g.iterrows()
        ]

    def board_rank(
        self,
        start: str,
        end: str,
        metric: str = "ret",
        n: int = 30,
        ascending: bool = False,
        min_stocks: int = 5,
        board_type: str = "industry",
    ) -> dict:
        """板块区间横截面排行：成分股等权聚合（板块涨跌幅=成分股区间收益均值，
        波动/回撤/换手取成分股均值，涨跌家数按区间收益正负统计）。

        board_type: industry 行业板块（划分）| concept 概念板块（成分有重叠，
        同一只股票计入多个概念，跨概念汇总无加总意义）。
        min_stocks: 成分股（满足最低样本量）数下限，过滤过小板块的统计噪声。
        """
        if metric not in _RANK_METRICS:
            metric = "ret"
        members = self._board_members_of(board_type)
        if members is None:
            cmd = "python -m downloader board"
            if board_type == "concept":
                cmd = "python -m downloader board --with-concept（需东财接口可用）"
            return {
                "start": start, "end": end, "metric": metric,
                "error": f"无{'概念' if board_type == 'concept' else '行业'}板块数据，请先运行: {cmd}",
                "rows": [],
            }
        table = self._rank_table(start, end)

        n_days, need = self._need_days(start, end, self.calendar)
        t = table[table["n_days"] >= need].copy()
        # 概念板块成分有重叠：按 (code, board_code) 展开，再按板块聚合
        t = t.join(members[["board_code"]].rename(columns={"board_code": "_bc"}), how="inner")
        t = t.dropna(subset=["_bc"])
        t["board_code"] = t["_bc"]
        t = t.drop(columns=["_bc"]).copy()
        t["adv"] = (t["ret"] > 0).astype(float)
        t["dec"] = (t["ret"] < 0).astype(float)

        grp = t.groupby("board_code")
        agg = pd.DataFrame(
            {
                "ret": grp["ret"].mean(),
                "vol": grp["vol"].mean(),
                "maxdd": grp["maxdd"].mean(),
                "turnover": grp["turnover"].mean(),
                "total_mv": grp["total_mv"].sum(),
                "n_stocks": grp.size(),
                "adv": grp["adv"].sum(),
                "dec": grp["dec"].sum(),
            }
        )
        n_boards = int(len(agg))
        agg = agg[agg["n_stocks"] >= int(min_stocks)]
        agg = agg.sort_values(metric, ascending=ascending).head(int(n))
        names = members.groupby("board_code")["board_name"].first()
        rows = []
        for bc, r in agg.iterrows():
            rows.append(
                {
                    "board_code": str(bc),
                    "board_name": str(names.get(bc, bc)),
                    "ret": _r(r["ret"]),
                    "vol": _r(r["vol"]),
                    "maxdd": _r(r["maxdd"]),
                    "turnover": _r(r["turnover"]),
                    "adv": int(r["adv"]),
                    "dec": int(r["dec"]),
                    "n_stocks": int(r["n_stocks"]),
                    "total_mv": _r(r["total_mv"]),
                }
            )
        return {"start": start, "end": end, "metric": metric, "n_days": n_days,
                "n_boards": n_boards, "rows": rows}

    def _compute_rank_table(self, start: str, end: str) -> pd.DataFrame:
        """扫描区间（前扩 45 天取首日基准收益）内的逐股区间统计。

        日收益先做 ±100% 截断：hfq 价格经多年分红因子缩小后（部分老股 <0.01 元），
        最小报价单位（1 分钱）的相对跳变可达数百%，截断可去除该量化噪声，
        同时不影响真实高波动个股（真实日收益极少超过 ±100%）。
        """
        ds = pad.dataset(self.daily_dir, format="parquet")
        ws = pd.Timestamp(start) - pd.Timedelta(days=45)
        tbl = ds.to_table(
            filter=(pad.field("date") >= ws) & (pad.field("date") <= pd.Timestamp(end)),
            columns=["date", "code", "close", "turnover"],
        ).to_pandas()
        tbl["date"] = pd.to_datetime(tbl["date"])
        tbl = tbl.sort_values(["code", "date"])

        prev = tbl.groupby("code")["close"].shift(1)
        rets = (tbl["close"] / prev - 1.0).clip(-1.0, 1.0)
        in_period = tbl["date"] >= pd.Timestamp(start)
        tbl_p = tbl[in_period]
        g = tbl_p.groupby("code")
        first = g["close"].first()
        last = g["close"].last()
        n_days = g["date"].count()
        vol = rets[in_period].groupby(tbl["code"][in_period]).std() * np.sqrt(ANNUALIZE)
        cummax = g["close"].transform("cummax")
        maxdd = ((tbl_p["close"] - cummax) / cummax).groupby(tbl_p["code"]).min()
        turnover = g["turnover"].mean()

        t = pd.DataFrame(
            {
                "ret": last / first - 1.0,
                "vol": vol,
                "maxdd": maxdd,
                "turnover": turnover,
                "n_days": n_days,
            }
        )
        t["name"] = self.basic["name"]
        t["total_mv"] = self.basic["total_mv"]
        t = t.loc[t.index.intersection(first.index)]
        return t

    def chips(self, code: str) -> dict | None:
        """筹码分布（全历史，带缓存）。"""
        code = code.upper()
        if code in self._chips_cache:
            return self._chips_cache[code]
        f = self.daily_dir / f"{code}.parquet"
        if not f.exists():
            return None
        from .chips import compute_chips, summarize

        df = pd.read_parquet(f, columns=["date", "open", "high", "low", "close", "volume", "amount", "turnover"])
        df = df.sort_values("date")
        if df.empty:
            return None

        float_shares = 0.0
        if code in self.basic.index:
            row = self.basic.loc[code]
            for mv_col in ("float_mv", "total_mv"):
                mv, px = row.get(mv_col), row.get("latest_price")
                if mv == mv and px == px and px and float(px) > 0:
                    float_shares = float(mv) / float(px)
                    break

        res = compute_chips(df, float_shares)
        stats = summarize(res["bins"], res["chips"], res["last_close"])
        total = res["chips"].sum()
        # 与 kline 一致：统一真实价折算（见 price_info）
        info = self.price_info(code)
        factor = info["factor"]
        if factor:
            bins = [b * factor for b in res["bins"].tolist()]
            stats = {
                **stats,
                "avg_cost": stats["avg_cost"] * factor,
                "median_cost": stats["median_cost"] * factor,
                "range90": [x * factor for x in stats["range90"]],
                "range70": [x * factor for x in stats["range70"]],
            }
            last_close = res["last_close"] * factor
        else:
            bins = res["bins"].tolist()
            last_close = res["last_close"]
        out = {
            "code": code,
            "name": str(self.basic.loc[code, "name"]) if code in self.basic.index else code,
            "bins": _f(bins),
            "pct": [round(x / total * 100, 4) if total > 0 else 0.0 for x in res["chips"]],
            "last_close": _r(last_close),
            "n_days": res["n_days"],
            "turnover_estimated": res["turnover_estimated"],
            "price_unit": "qfq" if factor else "hfq",
            "stats": stats,
        }
        self._chips_cache[code] = out
        if len(self._chips_cache) > 64:
            self._chips_cache.pop(next(iter(self._chips_cache)), None)
        return out

    def fundamental(self, code: str, refresh: bool = False) -> pd.DataFrame:
        """财报基本面（缓存优先；refresh 强制重新抓取）。"""
        from .fundamental import fetch, load as load_cache

        code = code.upper()
        if not refresh:
            df = load_cache(code, self.root)
            if not df.empty:
                return df
        return fetch(code, self.root, force=refresh)

    def index_kline(self, code: str, start: str | None = None, end: str | None = None) -> dict | None:
        idf = self.indexes.get(code)
        if idf is None:
            return None
        if start:
            idf = idf[idf.index >= pd.Timestamp(start)]
        if end:
            idf = idf[idf.index <= pd.Timestamp(end)]
        return {
            "code": code,
            "name": INDEX_NAMES.get(code, code),
            "dates": idf.index.strftime("%Y-%m-%d").tolist(),
            "close": _f(idf["close"].tolist()),
        }


# ----------------------------------------------------------------- helpers

def _norm_code(code: str) -> str:
    """'600519' / 'sh600519' / '600519.SH' -> '600519.SH'（市场缺失时按代码段推断）。"""
    c = (code or "").strip().upper().replace(".", "").replace(" ", "")
    m = re.fullmatch(r"((?:SH|SZ|BJ)?)(\d{6})((?:SH|SZ|BJ)?)", c)
    if not m:
        raise ValueError(f"无法识别的代码: {code}")
    prefix, num = m.group(1) or m.group(3), m.group(2)
    if not prefix:
        if num.startswith(("92",)):
            prefix = "BJ"
        elif num.startswith(("4", "8")):
            prefix = "BJ"
        elif num.startswith(("60", "68", "9")):
            prefix = "SH"
        else:
            prefix = "SZ"
    return f"{num}.{prefix}"


def _r(x):
    try:
        if x is None or (isinstance(x, float) and np.isnan(x)):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _pct(x):
    """数据源百分数(1.25 = 1.25%) -> API 统一小数口径(0.0125)；无效返回 None。"""
    x = _r(x)
    return None if x is None else x / 100.0


def _f(lst):
    out = []
    for x in lst:
        try:
            x = float(x)
        except (TypeError, ValueError):
            out.append(None)
            continue
        out.append(None if np.isnan(x) else (round(x, 4) if abs(x) < 1e5 else round(x, 2)))
    return out


def _nanlist(s: pd.Series):
    return s.where(s.notna(), np.nan).tolist()
