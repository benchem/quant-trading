"""下载编排：股票列表、日线增量更新、指数、数据集构建、状态查看。"""
from __future__ import annotations

import datetime as dt
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import pandas as pd

from . import config, source, store
from .codes import to_wind_code
from .ratelimit import with_retry

try:
    from tqdm import tqdm
except Exception:  # pragma: no cover
    tqdm = None


def _log(msg: str) -> None:
    print(msg, flush=True)


def _norm_end(end) -> str:
    if not end:
        return dt.date.today().strftime("%Y%m%d")
    return str(end).replace("-", "")


def _split_codes(s: str) -> list[str]:
    return [x for x in re.split(r"[,，;\s]+", str(s).strip()) if x]


#: 复权序列单日|跳变|超过该值视为复权单位断裂（涨跌停上限: 主板10%/创业板科创板20%/北交所30%）
UNIT_JUMP_LIMIT = 0.32


def _reject_unit_anomaly(old: Optional[pd.DataFrame], new: pd.DataFrame,
                         limit: float = UNIT_JUMP_LIMIT) -> str:
    """复权(hfq/qfq)序列按构造是连续的；校验新数据并入后是否出现不可能的大跳变。

    2026-10-25 事故: 腾讯源对无交易日窗口回退返回最新一根【未复权】bar 混入 hfq 序列，
    末根出现 -35%~-98% 的假跳变，前复权折算因子塌缩为 1。这里在合并前拦截：
    只检查「相邻对中后一行来自新数据」的跳变（不检查旧数据内部，避免历史遗留异常
    永久阻塞增量更新）。旧数据不足 6 根不校验（上市初期无涨跌停限制）。
    大比例缩股除权日 hfq 也会出现 >32% 的真实跳变（罕见），被误拦时仅跳过当日更新。
    返回非空原因字符串表示拒绝；空串放行。仅对 hfq/qfq 调用（none 除权跳空正常）。
    """
    if new is None or len(new) == 0 or old is None or len(old) < 6:
        return ""
    o = old[["date", "close"]].copy()
    o["_new"] = False
    n = new[["date", "close"]].copy()
    n["_new"] = True
    m = pd.concat([o, n], ignore_index=True)
    m["date"] = pd.to_datetime(m["date"])
    m = m.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    c = pd.to_numeric(m["close"], errors="coerce").astype(float)
    ret = c.pct_change().abs()
    bad = ret.where(m["_new"]) > limit
    if bad.any():
        i = int(bad.idxmax())
        return (f"疑似复权单位断裂: {m['date'].iloc[i].date()} 单日|涨跌|"
                f"{ret.iloc[i] * 100:.1f}% 超过 {limit * 100:.0f}%"
                f"（前收 {c.iloc[i - 1]:.3f} -> {c.iloc[i]:.3f}），已保留旧数据")
    return ""


def _make_rotator():
    """探测 Clash 并创建节点轮换器；不可用时返回 None（降级为纯冷却）。"""
    from .proxy import ClashAPI, ProxyRotator

    clash = ClashAPI.find()
    if clash is None:
        _log("警告: --rotate 已开启但未找到 Clash API，限流时将退化为冷却等待")
        return None
    try:
        rot = ProxyRotator(clash)
        source._mgr.configure_rotator(rot)
        return rot
    except Exception as e:  # noqa: BLE001
        _log(f"警告: 节点轮换器初始化失败（{e}），限流时将退化为冷却等待")
        return None


_STAT_LABEL = {
    "new": "新下载",
    "updated": "增量更新",
    "uptodate": "已是最新",
    "nodata": "区间无数据",
    "skipped": "疑似单位异常已跳过",
    "failed": "失败",
}


class Downloader:
    def __init__(self, data_dir="data", adjust=None, start=None, workers=None, rate=None,
                 rotate: bool = False):
        self.data_dir = Path(data_dir)
        self.adjust = adjust or config.DEFAULT_ADJUST
        self.start = (start or config.DEFAULT_START).replace("-", "")
        self.workers = int(workers) if workers else config.DEFAULT_WORKERS
        self.rate = config.DEFAULT_RATE if rate is None else float(rate)
        # 限速统一收敛到 HTTP 客户端（所有源/引擎/重试共用）
        source.configure_http(rate=self.rate)
        # 可选：限流时自动轮换 Clash 节点
        self.rotator = None
        if rotate:
            self.rotator = _make_rotator()

    # ------------------------------------------------ 股票列表 / 交易日历
    def refresh_stock_list(self) -> pd.DataFrame:
        df = with_retry(source.fetch_stock_list, desc="股票列表")
        store.save_parquet(store.stock_list_path(self.data_dir), df)
        _log(f"股票列表已刷新: {len(df)} 只 -> {store.stock_list_path(self.data_dir)}")
        return df

    def _stock_list(self) -> pd.DataFrame:
        p = store.stock_list_path(self.data_dir)
        if p.exists():
            return pd.read_parquet(p, engine="pyarrow")
        return self.refresh_stock_list()

    def refresh_boards(self, with_concept: bool = False) -> pd.DataFrame:
        """刷新板块成分表（行业板块必抓；概念板块 --with-concept 时一并抓取）。

        行业板块：东财行业分类（每板块一次请求约 2 分钟），东财不可用时自动降级
        baostock 证监会行业分类。概念板块：仅东财源（约 500 个题材/风格标签，
        成分有重叠），东财不可用时保留旧缓存的概念行。
        单板块抓取失败时保留本地旧缓存中该板块的行（重跑同一命令自动补齐）。
        """
        valid = None
        lp = store.stock_list_path(self.data_dir)
        if lp.exists():
            valid = set(pd.read_parquet(lp, engine="pyarrow")["code"])
        path = store.board_members_path(self.data_dir)
        old = None
        if path.exists():
            old = pd.read_parquet(path, engine="pyarrow")
            if "board_type" not in old.columns:
                old["board_type"] = "industry"

        ind, failed_ind = with_retry(
            lambda: source.fetch_board_members(valid, "industry"), desc="行业板块成分")
        if failed_ind:
            _log(f"行业板块: {len(failed_ind)} 个板块本次抓取失败"
                 f"（沿用旧缓存，重跑可补齐）: {', '.join(failed_ind[:15])}"
                 f"{' …' if len(failed_ind) > 15 else ''}")
            if old is not None:
                keep = old[(old["board_type"] == "industry")
                           & (old["board_code"].isin(set(failed_ind)))]
                ind = pd.concat([ind, keep], ignore_index=True)
                ind = ind.drop_duplicates("code", keep="first")

        parts = [ind]
        if with_concept:
            try:
                cpt, failed_cpt = with_retry(
                    lambda: source.fetch_board_members(valid, "concept"), desc="概念板块成分")
                parts.append(cpt)
                if failed_cpt:
                    _log(f"概念板块: {len(failed_cpt)} 个板块本次抓取失败"
                         f"（沿用旧缓存，重跑可补齐）: {', '.join(failed_cpt[:15])}"
                         f"{' …' if len(failed_cpt) > 15 else ''}")
                    if old is not None:
                        keep = old[(old["board_type"] == "concept")
                                   & (old["board_code"].isin(set(failed_cpt)))]
                        cpt = pd.concat([cpt, keep], ignore_index=True)
                        cpt = cpt.drop_duplicates(["code", "board_code"], keep="first")
                        parts[-1] = cpt
            except Exception as e:  # noqa: BLE001 概念失败不阻塞行业部分
                _log(f"概念板块抓取失败（保留旧缓存概念行）: {e}")
                if old is not None:
                    old_cpt = old[old["board_type"] == "concept"]
                    if len(old_cpt):
                        parts.append(old_cpt)
        elif old is not None:
            old_cpt = old[old["board_type"] == "concept"]
            if len(old_cpt):
                parts.append(old_cpt)  # 未要求刷新概念时保留既有概念数据

        df = pd.concat(parts, ignore_index=True)
        df = df.drop_duplicates(["code", "board_code"], keep="first")
        df = df.sort_values(["code", "board_code"]).reset_index(drop=True)
        store.save_parquet(path, df)
        n_ind = int(df.loc[df["board_type"] == "industry", "board_code"].nunique())
        n_cpt = int(df.loc[df["board_type"] == "concept", "board_code"].nunique())
        _log(f"板块成分已刷新: {n_ind} 个行业板块 / {n_cpt} 个概念板块"
             f"（{df['code'].nunique()} 只股票）-> {path}")
        return df

    def refresh_trade_calendar(self) -> pd.Series:
        dates = with_retry(source.fetch_trade_dates, desc="交易日历")
        store.save_parquet(store.trade_cal_path(self.data_dir), pd.DataFrame({"trade_date": dates}))
        _log(f"交易日历已刷新: {len(dates)} 天")
        return dates

    def last_trade_date(self, refresh: bool = False) -> Optional[pd.Timestamp]:
        p = store.trade_cal_path(self.data_dir)
        dates = None
        if refresh or not p.exists():
            try:
                dates = self.refresh_trade_calendar()
            except Exception as e:  # noqa: BLE001
                _log(f"交易日历刷新失败（{e}），尝试使用本地缓存")
        if dates is None:
            if not p.exists():
                return None
            dates = pd.read_parquet(p, engine="pyarrow")["trade_date"]
        s = pd.Series(pd.to_datetime(dates))
        s = s[s <= pd.Timestamp.now().normalize()]
        if len(s) == 0:
            return None
        return s.max()

    # ------------------------------------------------ A股日线
    def _next_start(self, old, end: str, full: bool, incremental_ok: bool) -> tuple[str, str]:
        """返回 (起始日期, 跳过状态)。跳过状态非空表示无需下载。

        前复权(qfq)历史数据会随新的分红除权重写，因此每次都需要整段重下；
        后复权(hfq)与不复权历史稳定，可以按「最后日期+1」增量追加。
        """
        if old is None or len(old) == 0:
            return self.start, ""
        last = pd.Timestamp(old["date"].max())
        nxt = (last + pd.Timedelta(days=1)).strftime("%Y%m%d")
        if nxt > end:
            return self.start, "uptodate"
        if full or not incremental_ok:
            return self.start, ""
        return nxt, ""

    def _update_stock_one(self, code: str, name: str, end: str, full: bool) -> tuple[str, str, str]:
        path = store.daily_dir(self.data_dir, self.adjust) / f"{code}.parquet"
        old = store.load_daily(path)
        start, skip = self._next_start(old, end, full, incremental_ok=self.adjust != "qfq")
        if skip:
            return code, skip, ""
        try:
            df = with_retry(
                source.fetch_daily,
                kwargs=dict(wind_code=code, start=start, end=end, adjust=self.adjust),
                desc=code,
            )
        except Exception as e:  # noqa: BLE001
            return code, "failed", f"{type(e).__name__}: {e}"
        if df is None or len(df) == 0:
            return (code, "uptodate", "") if old is not None and len(old) else (code, "nodata", "")
        if self.adjust in ("hfq", "qfq"):
            reason = _reject_unit_anomaly(old, df)
            if reason:
                _log(f"跳过 {code}: {reason}")
                return code, "skipped", reason
        merged = store.merge_daily(old, df)
        store.save_parquet(path, merged)
        return code, ("new" if old is None or len(old) == 0 else "updated"), ""

    def update_stocks(
        self,
        codes=None,
        end=None,
        full: bool = False,
        market: str = "all",
        limit: Optional[int] = None,
    ) -> dict:
        end = _norm_end(end)
        stock_list = self._stock_list()
        if codes:
            uniq = list(dict.fromkeys(_split_codes(codes)))
            targets = [(to_wind_code(c), "") for c in uniq]
        else:
            base = stock_list
            if market and market != "all":
                base = base[base["code"].str.endswith("." + market.upper())]
            targets = list(zip(base["code"].tolist(), base["name"].tolist()))
        if limit:
            targets = targets[: int(limit)]
        if not targets:
            _log("没有需要下载的股票。")
            return {}

        _log(
            f"目标 {len(targets)} 只股票 | 复权: {self.adjust}({config.ADJUST_LABEL[self.adjust]}) "
            f"| 截至: {end} | 并发: {self.workers} | 限速: {self.rate:g} req/s"
        )
        if self.adjust == "qfq":
            _log("提示: 前复权数据每次都会整段重新下载（历史随分红重写），建模推荐用后复权(hfq)。")

        stats = {"new": 0, "updated": 0, "uptodate": 0, "nodata": 0, "failed": 0, "skipped": 0}
        failures: list[tuple[str, str]] = []
        t0 = time.time()
        pool = ThreadPoolExecutor(max_workers=self.workers)
        try:
            futs = {
                pool.submit(self._update_stock_one, c, n, end, full): c
                for c, n in targets
            }
            prog = as_completed(futs)
            if tqdm is not None:
                prog = tqdm(prog, total=len(futs), desc="A股日线", unit="只")
            for fut in prog:
                code, status, msg = fut.result()
                stats[status] = stats.get(status, 0) + 1
                if status == "failed":
                    failures.append((code, msg))
        except KeyboardInterrupt:
            _log("用户中断，取消剩余任务……")
            for f in futs:
                f.cancel()
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        self._report(stats, failures, kind="stocks")
        _log(f"耗时 {time.time() - t0:.0f}s")
        return stats

    def _report(self, stats: dict, failures: list, kind: str) -> None:
        parts = [f"{_STAT_LABEL[k]} {v}" for k, v in stats.items() if v]
        _log("完成: " + (" | ".join(parts) if parts else "无任何变更"))
        if failures:
            path = self.data_dir / f"failed_{kind}_{self.adjust}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(f"{c}\t{m}" for c, m in failures), encoding="utf-8")
            _log(f"失败 {len(failures)} 只，详情见 {path}（重跑同一命令即可自动重试）")
            blocked = sum("冷却" in m or "限流" in m for _, m in failures)
            if blocked and blocked >= max(1, len(failures) // 2):
                _log(
                    "  提示: 大量失败源于数据源限流(403/429/501)，工具已进入冷却。"
                    "请稍后（数分钟~1小时）重跑同一命令补齐，或先降低 --rate/--workers。"
                )

    # ------------------------------------------------ 指数日线
    def update_indices(self, indices=None, end=None, full: bool = False) -> dict:
        end = _norm_end(end)
        if indices:
            targets = list(dict.fromkeys(to_wind_code(i) for i in _split_codes(indices)))
        else:
            targets = list(config.MAJOR_INDICES)
        stats = {"new": 0, "updated": 0, "uptodate": 0, "nodata": 0, "failed": 0}
        failures = []
        for code in targets:
            name = config.MAJOR_INDICES.get(code, "")
            path = store.index_dir(self.data_dir) / f"{code}.parquet"
            old = store.load_daily(path)
            start, skip = self._next_start(old, end, full, incremental_ok=True)
            if skip:
                stats[skip] += 1
                _log(f"  {code} {name}: 已是最新")
                continue
            try:
                df = with_retry(
                    source.fetch_index_daily,
                    kwargs=dict(wind_code=code, start=start, end=end),
                    desc=code,
                )
            except Exception as e:  # noqa: BLE001
                stats["failed"] += 1
                failures.append((code, f"{type(e).__name__}: {e}"))
                continue
            if df is None or len(df) == 0:
                stats["nodata" if old is None else "uptodate"] += 1
                continue
            merged = store.merge_daily(old, df)
            store.save_parquet(path, merged)
            stats["new" if old is None or len(old) == 0 else "updated"] += 1
            _log(f"  {code} {name}: {len(merged)} 行")
        self._report(stats, failures, kind="index")
        return stats

    # ------------------------------------------------ 合并建模数据集
    def build_dataset(self) -> Optional[Path]:
        import pyarrow as pa
        import pyarrow.parquet as pq

        src = store.daily_dir(self.data_dir, self.adjust)
        files = sorted(src.glob("*.parquet"))
        if not files:
            _log(f"没有日线数据可合并（{src} 为空）。先运行: python -m downloader stocks")
            return None

        sl_p = store.stock_list_path(self.data_dir)
        if sl_p.exists():
            sl = pd.read_parquet(sl_p, engine="pyarrow")
            name_map = dict(zip(sl["code"], sl["name"].astype(str)))
        else:
            name_map = {}

        out_dir = store.dataset_dir(self.data_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"daily_{self.adjust}.parquet"

        schema = pa.schema(
            [
                ("date", pa.timestamp("ns")),
                ("code", pa.string()),
                ("name", pa.string()),
                ("open", pa.float64()),
                ("high", pa.float64()),
                ("low", pa.float64()),
                ("close", pa.float64()),
                ("volume", pa.int64()),
                ("amount", pa.float64()),
                ("amplitude", pa.float64()),
                ("pct_chg", pa.float64()),
                ("change", pa.float64()),
                ("turnover", pa.float64()),
            ]
        )
        col_order = [f.name for f in schema]

        writer: Optional[pq.ParquetWriter] = None
        total_rows = 0
        batch: list[pd.DataFrame] = []
        batch_files = 0

        def flush() -> None:
            nonlocal writer, total_rows, batch, batch_files
            if not batch:
                return
            df = pd.concat(batch, ignore_index=True)
            df["name"] = df["code"].map(name_map).fillna("")
            df = df[col_order]
            df["date"] = pd.to_datetime(df["date"])
            df["volume"] = df["volume"].astype("int64")
            for c in ("open", "high", "low", "close", "amount", "amplitude", "pct_chg", "change", "turnover"):
                df[c] = df[c].astype("float64")
            table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
            if writer is None:
                writer = pq.ParquetWriter(out, schema, compression="zstd")
            writer.write_table(table)
            total_rows += len(df)
            batch = []
            batch_files = 0

        try:
            for f in files:
                batch.append(pd.read_parquet(f, engine="pyarrow"))
                batch_files += 1
                if batch_files >= 300:
                    flush()
            flush()
        finally:
            if writer is not None:
                writer.close()
        _log(f"股票日线合并: {len(files)} 只, {total_rows:,} 行 -> {out}")

        idx_files = sorted(store.index_dir(self.data_dir).glob("*.parquet"))
        if idx_files:
            out_idx = out_dir / "index_daily.parquet"
            frames = [pd.read_parquet(f, engine="pyarrow") for f in idx_files]
            pd.concat(frames, ignore_index=True).sort_values(["code", "date"]).to_parquet(
                out_idx, index=False, engine="pyarrow"
            )
            _log(f"指数合并: {len(idx_files)} 个 -> {out_idx}")

        if sl_p.exists():
            store.save_parquet(out_dir / "stock_basic.parquet", pd.read_parquet(sl_p, engine="pyarrow"))
            _log(f"股票基础信息 -> {out_dir / 'stock_basic.parquet'}")
        return out

    # ------------------------------------------------ 状态查看
    def status(self) -> None:
        import pyarrow.parquet as pq

        _log(f"数据目录: {self.data_dir.resolve()}")

        sl = store.stock_list_path(self.data_dir)
        if sl.exists():
            df = pd.read_parquet(sl, engine="pyarrow")
            mtime = dt.datetime.fromtimestamp(sl.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            _log(f"股票列表: {len(df)} 只 (更新于 {mtime})")
        else:
            _log("股票列表: 未下载（运行: python -m downloader list）")

        cal = store.trade_cal_path(self.data_dir)
        ltd: Optional[pd.Timestamp] = None
        if cal.exists():
            dates = pd.to_datetime(pd.read_parquet(cal, engine="pyarrow")["trade_date"])
            s = pd.Series(dates)
            s = s[s <= pd.Timestamp.now().normalize()]
            if len(s):
                ltd = s.max()
                _log(f"交易日历: 最近交易日 {ltd.date()}")

        for adj in config.ADJUST_CHOICES:
            d = store.daily_dir(self.data_dir, adj)
            if not d.exists():
                continue
            files = sorted(d.glob("*.parquet"))
            if not files:
                continue
            n_rows = 0
            dmin = dmax = None
            stale = 0
            by_market = {"SH": 0, "SZ": 0, "BJ": 0}
            for f in files:
                pf = pq.ParquetFile(f)
                n_rows += pf.metadata.num_rows
                r = store.parquet_date_range(f)
                if r:
                    mn, mx = r
                    dmin = mn if dmin is None or mn < dmin else dmin
                    dmax = mx if dmax is None or mx > dmax else dmax
                    if ltd is not None and mx < ltd.normalize():
                        stale += 1
                mkt = f.stem.split(".")[-1]
                if mkt in by_market:
                    by_market[mkt] += 1
            label = config.ADJUST_LABEL.get(adj, adj)
            _log(
                f"日线({label}): {len(files)} 个文件 / {n_rows:,} 行 | "
                f"{dmin.date() if dmin else '-'} ~ {dmax.date() if dmax else '-'}"
            )
            _log(f"  沪 {by_market['SH']} | 深 {by_market['SZ']} | 北 {by_market['BJ']}")
            if ltd is not None and stale:
                _log(f"  落后最近交易日: {stale} 只（可能停牌或已退市）")

        bd = store.board_members_path(self.data_dir)
        if bd.exists():
            df = pd.read_parquet(bd, engine="pyarrow")
            mtime = dt.datetime.fromtimestamp(bd.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            if "board_type" in df.columns:
                n_ind = int(df.loc[df["board_type"] == "industry", "board_code"].nunique())
                n_cpt = int(df.loc[df["board_type"] == "concept", "board_code"].nunique())
                desc = f"{n_ind} 个行业板块" + (f" / {n_cpt} 个概念板块" if n_cpt else "")
            else:
                desc = f"{df['board_code'].nunique()} 个行业板块"
            _log(f"板块成分: {desc} / {df['code'].nunique()} 只股票 (更新于 {mtime})")
        else:
            _log("板块成分: 未下载（运行: python -m downloader board）")

        idx_files = sorted(store.index_dir(self.data_dir).glob("*.parquet"))
        if idx_files:
            _log(f"指数: {len(idx_files)} 个 -> {', '.join(f.stem for f in idx_files)}")
        ds = store.dataset_dir(self.data_dir)
        ds_files = sorted(ds.glob("*.parquet")) if ds.exists() else []
        if ds_files:
            for f in ds_files:
                _log(f"数据集: {f} ({f.stat().st_size / 1024 / 1024:.1f} MB)")
        else:
            _log("数据集: 未生成（运行: python -m downloader build）")
