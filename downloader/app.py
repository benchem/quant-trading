"""下载编排：股票列表、日线增量更新、指数、数据集构建、状态查看。"""
from __future__ import annotations

import datetime as dt
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import pandas as pd

from . import config, source, store, vendors
from .codes import to_wind_code
from .errors import FetchError
from .ratelimit import with_retry
from .scheduler import (AllSourcesUnavailableError, MultiSourceScheduler,
                        build_source_units, probe_units)

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


#: 待修清单读写锁：增量自愈/多源工作线程与 repair 主循环会并发增删登记。
_REPAIR_LOCK = threading.RLock()

#: 守卫原因串里的断裂日（如 "疑似复权单位断裂: 2026-10-28 单日|涨跌|32.1%…"）。
_BREAK_DATE_RE = re.compile(r"疑似复权单位断裂:\s*(\d{4}-\d{2}-\d{2})")


def _break_date_from_reason(reason: str) -> Optional[str]:
    """守卫原因串提取断裂日（YYYYMMDD）；无匹配返回 None（按整段处理）。"""
    m = _BREAK_DATE_RE.search(reason or "")
    return m.group(1).replace("-", "") if m else None


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

    历史真实跳变豁免（2026-11-01）：1996-12 涨跌停制度前、股改复牌（2005-2007）、
    大比例送转除权日的单日 >32% 是真实行情。若【旧序列自身在同一交易日也有
    >limit 跳变】（新旧两源一致），说明不是混基准而是共同的历史事实 -> 放行；
    旧序列该日无跳变（如腾讯 raw bar 混入、断裂接缝）则照常拦截。
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
    # 旧序列自身的逐日跳变（按交易日对齐），用于历史真实跳变豁免
    oo = pd.DataFrame({"date": pd.to_datetime(old["date"]),
                       "close": pd.to_numeric(old["close"], errors="coerce").astype(float)}
                      ).sort_values("date").reset_index(drop=True)
    o_ret = oo["close"].pct_change().abs()
    o_ret.index = oo["date"]
    bad = (ret.where(m["_new"]) > limit).fillna(False)
    if bad.any():
        for i in bad[bad].index:
            d = m["date"].iloc[i]
            prev = o_ret.get(d)
            if prev is not None and not pd.isna(prev) and prev > limit:
                continue  # 新旧两源在该日跳变一致：历史真实行情，豁免
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


#: A股收盘截止时刻：K线写入时刻早于该值且与K线同日 → 视为「盘中写入」。
#: 15:00 收盘集合竞价结束，数据源当日 bar 通常 15:00-15:01 落定，留 5 分钟余量。
CLOSE_CUTOFF = dt.time(15, 5)


def _safe_mtime(path: Path) -> Optional[float]:
    """文件 mtime（epoch 秒）；不存在/不可读返回 None。"""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _written_intraday(last: pd.Timestamp, mtime: Optional[float]) -> bool:
    """最后一根 K 线是否为「盘中写入」：写入时刻与 K 线同日、且早于收盘截止。

    盘中部分更新/补跑会把当日未收盘的 bar 写进本地文件；收盘后的增量更新
    若直接按「最后日期+1」判定为已是最新，当日真实收盘价将永远缺失
    （2026-10-27 事故：约 1975 只股票整天停留在盘中价，自选股信号扫描
    全部基于错误收盘）。mtime 统一按 Asia/Shanghai 判日，服务器时区
    不是 CST 时依然正确。
    """
    if mtime is None:
        return False
    try:
        mt = pd.Timestamp(mtime, unit="s", tz="UTC").tz_convert("Asia/Shanghai")
    except Exception:  # noqa: BLE001 时区/数值异常时按「无法判定」处理
        return False
    return mt.date() == last.date() and mt.time() < CLOSE_CUTOFF


_STAT_LABEL = {
    "new": "新下载",
    "updated": "增量更新",
    "uptodate": "已是最新",
    "nodata": "区间无数据",
    "skipped": "疑似单位异常已跳过",
    "failed": "失败",
    "stale": "源缺数据待重试",
    "repaired": "单位断裂已修复",
}

#: 交易日历缓存 {(data_dir, mtime): Series|None}：空窗交叉核对会大量调用，
#: 按文件 mtime 缓存避免每只股票重读 parquet。
_CAL_CACHE: dict[tuple, Optional[pd.Series]] = {}


def _load_trade_calendar(data_dir) -> Optional[pd.Series]:
    """读交易日历（缓存；文件缺失/损坏返回 None，调用方保守处理）。"""
    p = store.trade_cal_path(data_dir)
    key = (str(p), _safe_mtime(p))
    if key in _CAL_CACHE:
        return _CAL_CACHE[key]
    days: Optional[pd.Series] = None
    try:
        cal = pd.read_parquet(p, engine="pyarrow")
        col = "trade_date" if "trade_date" in cal.columns else cal.columns[0]
        s = pd.Series(pd.to_datetime(cal[col])).dropna()
        days = s if len(s) else None
    except Exception:  # noqa: BLE001
        days = None
    if len(_CAL_CACHE) > 16:
        _CAL_CACHE.clear()
    _CAL_CACHE[key] = days
    return days


#: 股票列表首次落盘（无旧列表可比）时的最小行数：A股总数远超此值，
#: 低于它几乎必然是分页截断等异常，拒绝写盘。
MIN_STOCK_LIST_SIZE = 3000


def _stock_list_family(df: pd.DataFrame) -> dict[str, int]:
    """3 位代码前缀 -> 家数（600/601/300/688/002/430… 各成一族）。"""
    return df["code"].astype(str).str[:3].value_counts().to_dict()


def _stock_list_guard(new: pd.DataFrame, old: Optional[pd.DataFrame]) -> str:
    """股票列表写盘前守卫：拒绝明显异常的列表，保留旧列表。

    2026-10-26 事故: clist 分页中途空页静默截断，4605 行列表落盘（688/689/BJ
    整段缺失），此后所有全市场运行照坏列表跑，科创板股票数日无更新。判定：
    - 无旧列表（首次写入）: 总量 < MIN_STOCK_LIST_SIZE -> 拒绝；
    - 有旧列表: 任一 3 位前缀家族整体消失 / 单家族缩水 >50% / 总量缩水 >30%
      -> 拒绝。新增家族与扩容放行（正常现象）。
    返回非空拒绝原因；空串放行。
    """
    n_new = len(new)
    if old is None or len(old) == 0:
        if n_new < MIN_STOCK_LIST_SIZE:
            return f"列表总量 {n_new} 异常偏小 (<{MIN_STOCK_LIST_SIZE})"
        return ""
    new_fam = _stock_list_family(new)
    old_fam = _stock_list_family(old)
    gone = sorted(f for f in old_fam if f not in new_fam)
    if gone:
        n_gone = sum(old_fam[f] for f in gone)
        return f"代码前缀家族 {gone}（共 {n_gone} 只）整体消失"
    shrunk = sorted(f for f, c in old_fam.items()
                    if f in new_fam and new_fam[f] < c * 0.5)
    if shrunk:
        detail = ", ".join(f"{f}:{old_fam[f]}->{new_fam[f]}" for f in shrunk)
        return f"代码前缀家族缩水超 50%: {detail}"
    if n_new < len(old) * 0.7:
        return f"列表总量由 {len(old)} 缩水至 {n_new}（降幅超 30%）"
    return ""


class Downloader:
    #: 类级默认（供 Downloader.__new__ 直构的离线单测替身继承）：串行链路，
    #: 零探测零回归。生产入口（CLI / EOD）显式传 mode="auto" 启用多源分批。
    mode = "serial"
    #: 同花顺锚定全量重拉是否可用（auto 模式下由探测结果决定；serial 恒 False）
    _ths_enabled = False

    def __init__(self, data_dir="data", adjust=None, start=None, workers=None, rate=None,
                 rotate: bool = False, mode: str = "serial"):
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
        self.mode = mode

    # ------------------------------------------------ 股票列表 / 交易日历
    def refresh_stock_list(self) -> pd.DataFrame:
        df = with_retry(source.fetch_stock_list, desc="股票列表")
        # 写盘前守卫（2026-10-26 事故: 截断列表落盘后被当全市场跑）:
        # 明显异常时拒绝写盘并 raise，本地旧列表原样保留，重跑同一命令重试。
        p = store.stock_list_path(self.data_dir)
        old: Optional[pd.DataFrame] = None
        if p.exists():
            try:
                old = pd.read_parquet(p, engine="pyarrow")
            except Exception:  # noqa: BLE001 旧列表损坏按无旧列表处理
                old = None
        reason = _stock_list_guard(df, old)
        if reason:
            raise FetchError(f"股票列表完整性校验未通过，保留旧列表: {reason}")
        store.save_parquet(p, df)
        # dataset/stock_basic 归档副本同步刷新：原实现只在 build_dataset（EOD
        # 第 7 阶段）里复制，而 EOD 全链路经常在"全市场日线"阶段卡死重启，
        # 副本一滞后就是好几天，依赖它的路径看不到新上市股票（2026-10-27）。
        # 内容与 stock_list 完全一致，DataStore 按 mtime 取新者，行为不变。
        try:
            store.save_parquet(store.dataset_dir(self.data_dir) / "stock_basic.parquet", df)
        except Exception as e:  # noqa: BLE001 副本失败不影响主列表
            _log(f"警告: dataset/stock_basic 副本写入失败: {e}")
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
    def _window_has_trading_day(self, start: str, end: str) -> Optional[bool]:
        """窗口 [start, end] 是否含交易日（按本地交易日历）。

        日历覆盖范围内查日历；超出日历最大值（日历滞后/未来日期）退化工作日
        判断（与 analyzer.updates._is_trading_day 降级策略一致）——2026-10-30
        事故中日历止于 10-29，10-30 窗口的「是否有交易日」判定不能因日历过期
        而丢失；日历不可用返回 None（调用方保守处理）。
        """
        days = _load_trade_calendar(self.data_dir)
        if days is None:
            return None
        s, e = pd.Timestamp(start), pd.Timestamp(end)
        if e <= days.max():
            return bool(((days >= s) & (days <= e)).any())
        d, last_d = s.date(), e.date()
        while d <= last_d:
            if d.weekday() < 5:
                return True
            d += dt.timedelta(days=1)
        return False

    def _judge_no_progress(self, code: str, old, last: Optional[pd.Timestamp],
                           start: str, end: str) -> tuple[str, str, str]:
        """窗口未返回新数据（空/全部早于窗口起点）：区分「停牌」与「源缺数据」。

        2026-10-30 根因A/B: 旧实现空窗口+有旧数据无条件标「已是最新」，550 只
        实际有成交的股票被误判最新、393 只被旧 bar 覆盖写入。判定依据（明确）：
        - 窗口无交易日（日历判定）-> 无数据正常 -> uptodate；
        - 否则用 adjust=none（不复权）同窗口交叉核对：
          none 有 bar -> 有交易活动、复权源缺数据 -> stale（入失败清单，
          运行内二次重试 + 次日重跑重试）；
          none 也空 -> 停牌 -> uptodate；
          交叉核对自身失败 -> fail-safe 按源缺口（stale，宁误报不漏报）。
        """
        if old is None or len(old) == 0 or last is None:
            return code, "nodata", "区间无数据（无本地历史）"
        if self._window_has_trading_day(start, end) is False:
            return code, "uptodate", ""
        if self._window_has_bars(code, start, end):
            return code, "stale", (
                f"{self.adjust} 窗口 {start}~{end} 无新数据但复权交叉核对(不复权)"
                f"有数据（疑似数据源缺口，将重试）")
        return code, "uptodate", ""

    def _window_has_bars(self, code: str, start: str, end: str) -> bool:
        """不复权同窗口交叉核对：窗口内是否有 bar（=有交易活动）。"""
        try:
            df = with_retry(
                source.fetch_daily,
                kwargs=dict(wind_code=code, start=start, end=end, adjust="none"),
                desc=code, retries=2, base_delay=1.0,
            )
        except Exception:  # noqa: BLE001 交叉核对自身失败 -> fail-safe 按源缺口
            return True
        if df is None or len(df) == 0:
            return False
        return pd.Timestamp(df["date"].max()) >= pd.Timestamp(start)

    def _index_judge_no_progress(self, old, start: str, end: str) -> tuple[str, str]:
        """指数窗口未返回新数据（空/全部早于窗口起点）：按交易日历判 stale/uptodate。

        2026-10-30 根因S3: 旧实现空窗+有旧数据无条件「已是最新」，000688.SH 指数
        停在旧数据而不自知。主要指数每个交易日必然有 bar（无停牌概念），故以
        交易日历为判定依据：窗口无交易日 -> uptodate；有交易日但源无 bar ->
        stale（源缺口，入失败清单重试）；日历不可用 -> 保守按 stale（宁误报不漏报）。
        """
        if old is None or len(old) == 0:
            return "nodata", ""
        if self._window_has_trading_day(start, end) is False:
            return "uptodate", ""
        return "stale", (f"指数窗口 {start}~{end} 无新数据（日历含交易日，"
                         f"疑似数据源缺口，将重试）")

    # ------------------------------------------------ 待修清单 / 单位断裂自愈
    def _pending_repair_path(self) -> Path:
        return self.data_dir / f"pending_repair_{self.adjust}.txt"

    def _pending_repair_load(self) -> dict[str, str]:
        """code -> reason（保持文件内顺序）。"""
        p = self._pending_repair_path()
        if not p.exists():
            return {}
        out: dict[str, str] = {}
        for line in p.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if parts and parts[0].strip():
                out[parts[0].strip()] = parts[2] if len(parts) >= 3 else ""
        return out

    def _pending_repair_save(self, items: dict[str, str]) -> None:
        p = self._pending_repair_path()
        if not items:
            if p.exists():
                p.unlink()
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        today = dt.date.today().isoformat()
        p.write_text("\n".join(f"{c}\t{today}\t{r}" for c, r in items.items()) + "\n",
                     encoding="utf-8")

    def _pending_repair_add(self, code: str, reason: str) -> None:
        with _REPAIR_LOCK:
            items = self._pending_repair_load()
            if code not in items:  # 已在清单中保留首次登记的原因/日期
                items[code] = reason
                self._pending_repair_save(items)

    def _pending_repair_remove(self, code: str) -> None:
        with _REPAIR_LOCK:
            items = self._pending_repair_load()
            if code in items:
                del items[code]
                self._pending_repair_save(items)

    def _pending_repair_clear_fixed(self, code: str) -> None:
        """增量更新成功且过守卫 -> 断裂嫌疑解除，移出待修清单（懒加载缓存）。

        多源工作线程并发调用：读侧走实例缓存（首次加载后只查集合），
        写侧复用 _pending_repair_remove 的锁。清单为空时零开销。
        """
        cache = getattr(self, "_repair_fixed_cache", None)
        if cache is None:
            with _REPAIR_LOCK:
                try:
                    cache = set(self._pending_repair_load())
                except Exception:  # noqa: BLE001 清单读失败按空处理
                    cache = set()
            self._repair_fixed_cache = cache
        if code not in cache:
            return
        try:
            self._pending_repair_remove(code)
            cache.discard(code)
            _log(f"{code} 增量过守卫，移出待修清单")
        except Exception:  # noqa: BLE001 清理失败不影响主链路
            pass

    def _ths_anchored_refetch(self, code: str, end: str,
                              from_date: Optional[str] = None) -> tuple[bool, str]:
        """THS 锚定重拉：from_date 起的窗口替换（None = [start, end] 整段）。

        qfq × 断裂前存量比值 -> 存量基准 hfq。from_date（修复路径 = 断裂日）时：
        比值只用 from_date 之前的存量好 bar（断裂后存量基准已污染），返回窗口
        从 from_date 起——merge_daily 只覆盖窗口内日期，断裂日之前的存量历史
        原值保留（2026-11-01 实测：存量多供应商拼接，全史比值漂移 ±2%，
        整段缩放会打歪历史段，必须按断裂日局部锚定）。
        """
        path = store.daily_dir(self.data_dir, self.adjust) / f"{code}.parquet"
        old = store.load_daily(path)
        start = str(from_date).replace("-", "") if from_date else self.start
        try:
            df = vendors.fetch_ths_hfq(code, start, end, old, full=True,
                                       anchor_before=from_date)
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
        if df is None or len(df) == 0:
            return False, "THS 全量重拉无数据"
        if self.adjust in ("hfq", "qfq"):
            reason = _reject_unit_anomaly(old, df)
            if reason:
                return False, reason
        store.save_parquet(path, store.merge_daily(old, df))
        return True, ""

    def _full_refetch_one(self, code: str, end: str,
                          from_date: Optional[str] = None) -> tuple[bool, str]:
        """单股全量重拉 + 复权单位守卫；成功则落盘。

        同花顺可用（auto 模式探测健康）时优先 THS 锚定（from_date 起局部替换，
        断裂前历史原值保留）；失败回退既有 em/tx/bs 全量重拉（合并写，现状行为，
        恒整段 [start, end]）。
        """
        if self._ths_enabled:
            ok, msg = self._ths_anchored_refetch(code, end, from_date)
            if ok:
                return True, ""
            _log(f"{code} THS 锚定全量重拉失败，回退旧链路: {msg}")
        path = store.daily_dir(self.data_dir, self.adjust) / f"{code}.parquet"
        old = store.load_daily(path)
        try:
            df = with_retry(
                source.fetch_daily,
                kwargs=dict(wind_code=code, start=self.start, end=end,
                            adjust=self.adjust),
                desc=code,
            )
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
        if df is None or len(df) == 0:
            return False, "全量重拉无数据"
        if self.adjust in ("hfq", "qfq"):
            reason = _reject_unit_anomaly(old, df)
            if reason:
                return False, reason
        store.save_parquet(path, store.merge_daily(old, df))
        return True, ""

    def _self_heal_full_refetch(self, code: str, end: str,
                                reason: str) -> tuple[str, str, str]:
        """增量路径单位守卫命中：登记待修清单 + 立即全量重拉自愈。

        2026-10-30 根因C: 旧实现守卫命中仅标「skipped」，140 只「单位断裂」
        股票被永久阻塞（每天增量 +36% 假跳变被拒、本地永远停在断裂前一日）。
        复权因子跨供应商有 ~1% 差异，增量窗口恰跨口径切换日时必中守卫，
        只有整段重下（单一供应商连续序列）能恢复：
        1) 记入 data/pending_repair_{adjust}.txt（次日 EOD 自动重试 +
           CLI `python -m downloader repair` 手动补修）；
        2) 立即尝试全量重拉 [start, end] 并再过守卫：通过 -> 合并落盘、
           移出清单、标 repaired；仍失败 -> skipped（留在清单等待 repair）。
        """
        self._pending_repair_add(code, reason)
        ok, msg = self._full_refetch_one(code, end, _break_date_from_reason(reason))
        if ok:
            self._pending_repair_remove(code)
            _log(f"{code} 单位断裂自愈成功（全量重拉通过守卫）")
            return code, "repaired", f"守卫命中（{reason[:60]}）后全量重拉通过"
        return code, "skipped", f"守卫命中且全量重拉仍失败: {msg}"

    def _update_stock_one(self, code: str, name: str, end: str, full: bool,
                          fetch_fn=None) -> tuple[str, str, str]:
        """更新单只股票；fetch_fn 提供时走多源分批单元（否则既有串行链路）。

        fetch_fn 签名 (code, start, end, old, full) -> DataFrame：
        - 不包 with_retry（调度器的跨源重派即重试）；
        - old 为本地存量（锚定换算用），full 表示整段重拉（--full / 修复）。
        """
        path = store.daily_dir(self.data_dir, self.adjust) / f"{code}.parquet"
        old = store.load_daily(path)
        start, skip = self._next_start(old, end, full,
                                       incremental_ok=self.adjust != "qfq",
                                       mtime=_safe_mtime(path))
        if skip:
            return code, skip, ""
        try:
            if fetch_fn is None:
                df = with_retry(
                    source.fetch_daily,
                    kwargs=dict(wind_code=code, start=start, end=end, adjust=self.adjust),
                    desc=code,
                )
            else:
                df = fetch_fn(code, start, end, old, full)
        except Exception as e:  # noqa: BLE001
            return code, "failed", f"{type(e).__name__}: {e}"
        last = pd.Timestamp(old["date"].max()) if (old is not None and len(old)) else None
        # 空窗口，或返回的 bar 全部早于窗口起点（源回退/越界数据）-> 无新数据：
        # 交叉核对区分停牌与源缺口（2026-10-30 根因A/B）。
        # 注: 同日重取（start==最后bar日，盘中价收盘覆盖场景）max==起点 属正常，放行。
        if (df is None or len(df) == 0
                or (last is not None
                    and pd.Timestamp(df["date"].max()) < pd.Timestamp(start))):
            return self._judge_no_progress(code, old, last, start, end)
        if self.adjust in ("hfq", "qfq"):
            reason = _reject_unit_anomaly(old, df)
            if reason:
                _log(f"跳过 {code}: {reason} -> 转全量重拉自愈")
                return self._self_heal_full_refetch(code, end, reason)
        merged = store.merge_daily(old, df)
        store.save_parquet(path, merged)
        self._pending_repair_clear_fixed(code)  # 增量过守卫 -> 断裂嫌疑解除
        return code, ("new" if last is None else "updated"), ""

    def _next_start(self, old, end: str, full: bool, incremental_ok: bool,
                    mtime: Optional[float] = None) -> tuple[str, str]:
        """返回 (起始日期, 跳过状态)。跳过状态非空表示无需下载。

        前复权(qfq)历史数据会随新的分红除权重写，因此每次都需要整段重下；
        后复权(hfq)与不复权历史稳定，可以按「最后日期+1」增量追加。

        例外（2026-10-27 事故）：本地已有 end 当日的 K 线、但它是盘中写入的
        （文件 mtime 早于收盘截止且与 K 线同日）时，收盘后的更新必须重取当日
        覆盖——merge_daily 按 (code, date) 去重保留新值，重取即修正收盘价；
        否则当日真实收盘永远缺失，盘中价会被永久当作收盘价参与复权与信号。
        """
        if old is None or len(old) == 0:
            return self.start, ""
        last = pd.Timestamp(old["date"].max())
        nxt = (last + pd.Timedelta(days=1)).strftime("%Y%m%d")
        if nxt > end:
            if (incremental_ok and not full
                    and last.strftime("%Y%m%d") == end
                    and _written_intraday(last, mtime)):
                return end, ""   # 盘中写入的当日 bar：收盘后重取覆盖
            return self.start, "uptodate"
        if full or not incremental_ok:
            return self.start, ""
        return nxt, ""

    # ------------------------------------------------ 多源分批调度（2026-11）
    def _multisource_ready(self, mode: str) -> bool:
        """auto 模式探测三新源（每源 1 次轻量请求）：任一健康才启用分批。

        - 全部新源不可用 -> 完整退回既有串行链路（零回归路径）；
        - 同花顺健康则记 _ths_enabled，供单位断裂修复优先 THS 锚定全量重拉；
        - qfq/none 文件口径不做锚定换算，保持现状串行路径。
        """
        self._ths_enabled = False
        if mode == "serial":
            return False
        if self.adjust != "hfq":
            return False
        healthy = probe_units([u for u in build_source_units(self.adjust) if u.needs_old])
        self._ths_enabled = any(u.name == "ths" for u in healthy)
        return bool(healthy)

    def _run_serial(self, targets, end, full, on_progress=None,
                    base_done: int = 0, total: Optional[int] = None):
        """既有串行链路一遍（ThreadPoolExecutor + with_retry em->tx->bs 降级）。"""
        total = len(targets) if total is None else total
        results: dict[str, tuple[str, str]] = {}
        done = base_done
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
                results[code] = (status, msg)
                done += 1
                if on_progress is not None:
                    try:
                        on_progress(done, total)
                    except Exception:  # noqa: BLE001 心跳回调绝不影响主链路
                        pass
        except KeyboardInterrupt:
            _log("用户中断，取消剩余任务……")
            for f in futs:
                f.cancel()
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return results

    def _run_batch(self, targets, end, full, on_progress=None):
        """多源分批一遍（2026-11）：探测健康源 round-robin 分批 + 失败跨源重派。

        新股（无本地文件，THS/raw 无法锚定）单独走 em/tx 池（needs_old 过滤），
        避免烧光每股跨源次数；存量股走五源池。
        """
        ddir = store.daily_dir(self.data_dir, self.adjust)
        fresh = [(c, n) for c, n in targets if not (ddir / f"{c}.parquet").exists()]
        existing = [(c, n) for c, n in targets if (ddir / f"{c}.parquet").exists()]
        units = build_source_units(self.adjust)

        def _proc(code, name, unit):
            return self._update_stock_one(code, name, end, full, fetch_fn=unit.fetch)

        results: dict[str, tuple[str, str]] = {}
        per_source: dict[str, dict[str, int]] = {}

        def _merge(res, ps):
            results.update(res)
            for n, s in ps.items():
                agg = per_source.setdefault(n, {"ok": 0, "fail": 0})
                agg["ok"] += s["ok"]
                agg["fail"] += s["fail"]

        if fresh:  # 新股优先；em/tx 全挂时新股本轮失败（不阻塞存量池）
            try:
                res, ps = MultiSourceScheduler(
                    [u for u in units if not u.needs_old]).run(fresh, _proc, on_progress)
                _merge(res, ps)
            except AllSourcesUnavailableError as e:
                _log(f"警告: 新股池（em/tx）不可用，{len(fresh)} 只新股本轮失败: {e}")
                for c, _n in fresh:
                    results[c] = ("failed", f"新股池无可用源: {e}")
        if existing:
            res, ps = MultiSourceScheduler(units).run(existing, _proc, on_progress)
            _merge(res, ps)
        return results, per_source

    def update_stocks(
        self,
        codes=None,
        end=None,
        full: bool = False,
        market: str = "all",
        limit: Optional[int] = None,
        on_progress=None,
        lag_retry_delay: float = 180.0,
        mode: Optional[str] = None,
    ) -> dict:
        """on_progress(done, total): 每完成一只回调一次（供 EOD 看门狗采集心跳）。

        lag_retry_delay: 主遍结束后对「源缺数据(stale)」股票的冷却重试秒数
        （默认 180s；0 表示不冷却立即重试）。EOD 起跑早于数据源生成当日 bar 的
        时段，冷却后仅对 stale 批次再跑一遍，同一轮内自愈（2026-10-30 事故）。

        mode: None -> self.mode（CLI/EOD 构造时指定）；auto=探测新源健康则
        多源分批（同花顺/新浪/搜狐 + em/tx），否则回退串行；serial=强制既有
        串行链路（--serial）。
        """
        end = _norm_end(end)
        if codes:
            uniq = list(dict.fromkeys(_split_codes(codes)))
            targets = [(to_wind_code(c), "") for c in uniq]
        else:
            # 仅在需要全市场队列时才取列表（缺失会触网刷新），codes 分支不用它
            stock_list = self._stock_list()
            base = stock_list
            if market and market != "all":
                base = base[base["code"].str.endswith("." + market.upper())]
            targets = list(zip(base["code"].tolist(), base["name"].tolist()))
            # 新股优先：尚无本地日线文件（新上市/历史缺失）的股票排到队列最前
            # （稳定排序，已有文件的相对顺序不变）。全市场队列默认按代码排序，
            # 新股代码段(300xxx/301xxx/688xxx)天然在队尾，EOD 中途卡死/重启补跑
            # 时它们永远轮不到，上市数日后仍无 K 线（2026-10-27 事故伴生问题）。
            ddir = store.daily_dir(self.data_dir, self.adjust)
            targets.sort(key=lambda t: (ddir / f"{t[0]}.parquet").exists())
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

        m = mode or self.mode
        if m not in ("auto", "serial"):
            raise ValueError(f"未知 mode: {m}")
        batch = self._multisource_ready(m)
        if batch:
            _log(f"模式: 多源分批（同花顺锚定换算{'已启用' if self._ths_enabled else '未启用'}）")
        else:
            _log("模式: 串行链路 em->tx->baostock"
                 + ("（serial 模式）" if m == "serial" else "（新源不可用，自动回退）"))

        results: dict[str, tuple[str, str]] = {}
        per_source: dict[str, dict[str, int]] = {}
        t0 = time.time()
        total = len(targets)
        try:
            if batch:
                results, per_source = self._run_batch(targets, end, full, on_progress)
            else:
                results = self._run_serial(targets, end, full, on_progress)
        except KeyboardInterrupt:
            _log("用户中断，取消剩余任务……")

        # 运行内二次重试: 主遍 stale（源缺数据，非网络故障）的股票，其源窗口
        # 空可能是时段性未生成（EOD 15:50 起跑 vs 数据源 bar 生成时差）。
        # 冷却 lag_retry_delay 秒后仅对该批次按同一模式再跑一遍；仍 stale 的留在
        # 失败清单，次日重跑同一命令自动重试。
        stale_codes = [c for c, (s, _m) in results.items() if s == "stale"]
        if stale_codes:
            if lag_retry_delay > 0:
                _log(f"{len(stale_codes)} 只股票源无新数据（疑似源空窗），"
                     f"冷却 {lag_retry_delay:.0f}s 后二次重试……")
                time.sleep(lag_retry_delay)
            retry = [(c, "") for c in stale_codes]
            base, all_total = len(results), total + len(stale_codes)
            prog2 = None
            if on_progress is not None:
                prog2 = lambda dn, _t: on_progress(base + dn, all_total)  # noqa: E731
            if batch:
                res2, ps2 = self._run_batch(retry, end, full, prog2)
                results.update(res2)
                for n, s in ps2.items():
                    agg = per_source.setdefault(n, {"ok": 0, "fail": 0})
                    agg["ok"] += s["ok"]
                    agg["fail"] += s["fail"]
            else:
                results.update(self._run_serial(retry, end, full, prog2,
                                                base_done=base, total=all_total))

        # 按最终状态单次计数（二次重试覆盖首遍 stale，避免双计）
        stats = {k: 0 for k in _STAT_LABEL}
        for _c, (status, _m) in results.items():
            stats[status] = stats.get(status, 0) + 1
        failures = [(c, m) for c, (s, m) in results.items() if s in ("failed", "stale")]
        self._report(stats, failures, kind="stocks")
        if per_source:
            _log("per-source: " + ", ".join(
                f"{n}(ok={s['ok']},fail={s['fail']})" for n, s in per_source.items()))
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

    # ------------------------------------------------ 单位断裂修复（repair）
    def repair_stocks(self, codes=None, force: bool = False, end=None,
                      mode: Optional[str] = None) -> dict:
        """修复「单位断裂」股票：全量重拉 [start, end] + 复权单位守卫。

        目标 = 待修清单 data/pending_repair_{adjust}.txt ∪ --codes 指定代码。
        - 指定代码无清单记录且无本地数据文件时跳过（防误敲），--force 放行；
        - 成功 -> 合并落盘、移出清单、计 repaired；
        - 失败 -> 登记/保留在清单（次日 EOD 与本命令自动重试）、计 failed。

        mode: None -> self.mode；auto=探测同花顺健康则 THS 锚定全量优先
        （整文件替换写），否则旧链路；serial=强制旧链路全量重拉（--serial）。
        清单为空且未指定代码时不探测不触网（EOD 每日空跑 no-op 契约）。
        """
        m = mode or self.mode
        if m not in ("auto", "serial"):
            raise ValueError(f"未知 mode: {m}")
        end = _norm_end(end)
        ddir = store.daily_dir(self.data_dir, self.adjust)
        items = self._pending_repair_load()
        targets = list(items.keys())
        stats = {"repaired": 0, "failed": 0, "skipped": 0}
        if codes:
            for c in dict.fromkeys(to_wind_code(x) for x in _split_codes(codes)):
                if c in targets:
                    continue
                if not force and not (ddir / f"{c}.parquet").exists():
                    _log(f"跳过 {c}: 无待修清单记录且无本地数据文件（加 --force 强制整段重拉）")
                    stats["skipped"] += 1
                    continue
                targets.append(c)
        if not targets:
            _log("没有需要修复的股票（待修清单为空，或指定代码均被跳过）。")
            if any(stats.values()):
                self._report(stats, [], kind="repair")
            return stats
        # 有修复目标才探测（EOD 空清单 no-op 不触网）；serial 恒走旧链路
        self._ths_enabled = bool(m == "auto" and self.adjust == "hfq"
                                 and vendors.probe_ths())
        if self._ths_enabled:
            _log("修复模式: 优先 THS 锚定全量重拉（探测健康），失败回退旧链路")
        else:
            _log("修复模式: 旧链路全量重拉"
                 + ("（serial 模式）" if m == "serial" else "（THS 不可用或非 hfq）"))
        _log(f"修复 {len(targets)} 只股票（全量重拉 [{self.start}..{end}] + 单位守卫）")
        failures: list[tuple[str, str]] = []
        for code in targets:
            ok, msg = self._full_refetch_one(
                code, end, _break_date_from_reason(items.get(code, "")))
            if ok:
                self._pending_repair_remove(code)
                stats["repaired"] += 1
                _log(f"  {code} 修复成功（全量重拉通过守卫）")
            else:
                self._pending_repair_add(code, f"修复失败: {msg}")
                stats["failed"] += 1
                failures.append((code, msg))
                _log(f"  {code} 修复失败: {msg}（留在待修清单，下次自动重试）")
        self._report(stats, failures, kind="repair")
        return stats

    # ------------------------------------------------ 指数日线
    def update_indices(self, indices=None, end=None, full: bool = False) -> dict:
        end = _norm_end(end)
        if indices:
            targets = list(dict.fromkeys(to_wind_code(i) for i in _split_codes(indices)))
        else:
            targets = list(config.MAJOR_INDICES)
        stats = {"new": 0, "updated": 0, "uptodate": 0, "nodata": 0,
                 "failed": 0, "stale": 0}
        failures = []
        for code in targets:
            name = config.MAJOR_INDICES.get(code, "")
            path = store.index_dir(self.data_dir) / f"{code}.parquet"
            old = store.load_daily(path)
            start, skip = self._next_start(old, end, full, incremental_ok=True,
                                           mtime=_safe_mtime(path))
            if skip:
                stats[skip] += 1
                _log(f"  {code} {name}: 已是最新")
                continue
            # 窗口无交易日（日历给出确定 False）-> 直接 uptodate，不发请求
            if old is not None and len(old) > 0 and not full \
                    and self._window_has_trading_day(start, end) is False:
                stats["uptodate"] += 1
                _log(f"  {code} {name}: 窗口无交易日，已是最新")
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
            no_new = (df is None or len(df) == 0
                      or (old is not None and len(old) > 0
                          and pd.Timestamp(df["date"].max()) < pd.Timestamp(start)))
            if no_new:
                status, msg = self._index_judge_no_progress(old, start, end)
                stats[status] += 1
                if status == "stale":
                    failures.append((code, msg))
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
