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

        self._wl_path = self.root / "watchlist.json"
        self._wl_lock = threading.Lock()
        self._wl_codes: list[str] = self._load_watchlist()

        self._trades_path = self.root / "trades.json"
        self._trades_lock = threading.Lock()
        self._trades: list[dict] = self._load_trades()

        t0 = time.time()
        self.basic = self._load_basic()
        self.calendar = self._load_calendar()
        self.indexes: dict[str, pd.DataFrame] = self._load_indexes()
        self.market = self._build_market_daily()
        self.meta = {
            "n_stocks": int(self.basic.shape[0]),
            "date_min": str(self.market.index.min().date()),
            "date_max": str(self.market.index.max().date()),
            "last_trade_date": str(self.market.index.max().date()),
            "indexes": [
                {"code": c, "name": INDEX_NAMES.get(c, c)} for c in sorted(self.indexes)
            ],
            "load_seconds": round(time.time() - t0, 2),
        }

    # ------------------------------------------------------------------ load

    def _load_basic(self) -> pd.DataFrame:
        for name in ("dataset/stock_basic.parquet", "stock_list.parquet"):
            p = self.root / name
            if p.exists():
                df = pd.read_parquet(p)
                df = df.drop_duplicates("code").set_index("code")
                for col in ("latest_price", "pct_chg", "total_mv", "float_mv"):
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                return df
        raise SystemExit(f"{self.root} 下未找到股票基础信息表")

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

    def _build_market_daily(self) -> pd.DataFrame:
        """全市场日度聚合：涨跌家数 / 等权收益 / 成交额 / 等权累计净值。"""
        p = self.root / "dataset" / "daily_hfq.parquet"
        cols = ["date", "code", "close", "amount"]
        if p.exists():
            t = pd.read_parquet(p, columns=cols)
        else:
            # 兜底：用单股文件现场合并（较慢）
            files = list(self.daily_dir.glob("*.parquet"))
            t = pd.concat((pd.read_parquet(f) for f in files), ignore_index=True)
            t = t[["date", "code", "close", "amount"]]

        t["date"] = pd.to_datetime(t["date"])
        t = t.sort_values(["code", "date"])
        prev = t.groupby("code")["close"].shift(1)
        t["ret"] = t["close"] / prev - 1.0

        adv = (t["ret"] > 0).groupby(t["date"]).sum()
        dec = (t["ret"] < 0).groupby(t["date"]).sum()
        flat = (t["ret"] == 0).groupby(t["date"]).sum()
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
                "mean_ret": mean_ret,
                "med_ret": med_ret,
                "amount_yi": amount / 1e8,  # 亿元
            }
        )
        m["nav_eqw"] = (1.0 + m["mean_ret"].fillna(0.0)).cumprod()
        m.index = m.index.normalize()
        m.index.name = "date"
        return m

    # ----------------------------------------------------------------- query

    # ------------------------------------------------------------ watchlist

    def _load_watchlist(self) -> list[str]:
        try:
            data = json.loads(self._wl_path.read_text())
            codes = data.get("codes", [])
            return [c for c in codes if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(c))]
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return []

    def _save_watchlist(self) -> None:
        tmp = tempfile.NamedTemporaryFile(
            "w", dir=self._wl_path.parent, suffix=".tmp", delete=False, encoding="utf-8"
        )
        try:
            json.dump({"codes": self._wl_codes, "updated": time.strftime("%Y-%m-%d %H:%M:%S")},
                      tmp, ensure_ascii=False, indent=2)
            tmp.close()
            os.replace(tmp.name, self._wl_path)
        except BaseException:
            tmp.close()
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise

    def watchlist(self) -> list[dict]:
        """自选股列表（含基础信息），按加入顺序。"""
        out = []
        for code in self._wl_codes:
            if code in self.basic.index:
                r = self.basic.loc[code]
                lb = self.hfq_last(code)
                m = minute_info(code)
                out.append({
                    "code": code,
                    "name": str(r.get("name", code)),
                    "latest_price": _r(r.get("latest_price")),
                    "pct_chg": _pct(r.get("pct_chg")),
                    "total_mv": _r(r.get("total_mv")),
                    "hfq_close": lb["close"] if lb else None,
                    "hfq_date": lb["date"] if lb else None,
                    "minute_bars": m["bars"] if m else None,
                    "minute_last": m["last"] if m else None,
                    "fund": self.fund_latest(code),
                })
        return out

    def fund_latest(self, code: str) -> dict | None:
        """最新报告期财报摘要（仅读本地缓存，不发网络请求；无缓存返回 None）。"""
        code = code.upper()
        f = self.root / "fundamental" / f"{code}.parquet"
        if not f.exists():
            return None
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
            "roe": g("roe"),
            "debt_ratio": g("debt_ratio"),
        }

    def news(self, code: str, refresh: bool = False) -> dict:
        """个股最新新闻（本地缓存优先，TTL 30 分钟；失败回退旧缓存，见 news.py）。"""
        from .news import fetch

        return fetch(code, self.root, refresh=refresh)

    def watchlist_add(self, code: str) -> tuple[list[str], bool]:
        code = code.upper()
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
            raise ValueError(f"代码格式错误: {code}")
        with self._wl_lock:
            added = False
            if code not in self._wl_codes:
                if len(self._wl_codes) >= 300:
                    raise ValueError("自选股上限 300 只")
                self._wl_codes.append(code)
                added = True
            self._save_watchlist()
            return list(self._wl_codes), added

    def watchlist_remove(self, code: str) -> tuple[list[str], bool]:
        code = code.upper()
        with self._wl_lock:
            removed = code in self._wl_codes
            self._wl_codes = [c for c in self._wl_codes if c != code]
            self._save_watchlist()
            return list(self._wl_codes), removed

    # ------------------------------------------------------------- trades
    # 自选股买卖流水：手工登记（date/side/price/shares/note），持久化到
    # data/trades.json，网页 K 线上以 ▲买 / ▼卖 标记展示，并据此计算持仓成本。

    def _load_trades(self) -> list[dict]:
        try:
            data = json.loads(self._trades_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
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

    def _save_trades(self) -> None:
        tmp = tempfile.NamedTemporaryFile(
            "w", dir=self._trades_path.parent, suffix=".tmp", delete=False, encoding="utf-8"
        )
        try:
            json.dump({"trades": self._trades, "updated": time.strftime("%Y-%m-%d %H:%M:%S")},
                      tmp, ensure_ascii=False, indent=1)
            tmp.close()
            os.replace(tmp.name, self._trades_path)
        except BaseException:
            tmp.close()
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise

    def trades(self, code: str | None = None) -> list[dict]:
        """交易流水（按日期+登记时间升序）；code 为空时返回全部。"""
        with self._trades_lock:
            ts = [dict(t) for t in self._trades]
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

        with self._trades_lock:
            if len(self._trades) >= _MAX_TRADES:
                raise ValueError(f"交易记录上限 {_MAX_TRADES} 笔")
            rec = self._make_trade_rec(code, date, side, price, shares, note, fee)
            self._trades.append(rec)
            self._trades.sort(key=lambda t: (t["date"], t["created"]))
            self._save_trades()
            return [dict(t) for t in self._trades], dict(rec)

    def trade_add_batch(
        self,
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

        added: list[dict] = []
        errors: list[dict] = []
        with self._trades_lock:
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
                if len(self._trades) + len(added) >= _MAX_TRADES:
                    errors.append({"index": i, "raw": raw, "error": f"交易记录上限 {_MAX_TRADES} 笔"})
                    continue
                added.append(self._make_trade_rec(code, date, side, price, shares, note, fee))
            if added:
                self._trades.extend(added)
                self._trades.sort(key=lambda t: (t["date"], t["created"]))
                self._save_trades()
        return [dict(t) for t in self._trades], [dict(r) for r in added], errors

    def trade_remove(self, code: str, tid: str) -> tuple[list[dict], bool]:
        code = (code or "").strip().upper()
        tid = str(tid or "")
        with self._trades_lock:
            before = len(self._trades)
            self._trades = [t for t in self._trades if not (t["id"] == tid and t["code"] == code)]
            removed = len(self._trades) < before
            if removed:
                self._save_trades()
            return [dict(t) for t in self._trades], removed

    def trade_update(
        self,
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

        with self._trades_lock:
            rec = next((t for t in self._trades if t["id"] == tid and t["code"] == code), None)
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
            self._trades.sort(key=lambda t: (t["date"], t["created"]))
            self._save_trades()
            return [dict(t) for t in self._trades], dict(rec)

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

    def positions(self) -> dict[str, dict]:
        """按代码汇总的持仓（最新价取基础信息表快照；无交易记录的代码不出现在结果中）。"""
        by_code: dict[str, list[dict]] = {}
        for t in self.trades():
            by_code.setdefault(t["code"], []).append(t)
        out = {}
        for code, lst in by_code.items():
            last_price = _r(self.basic.loc[code].get("latest_price")) if code in self.basic.index else None
            out[code] = self.position_of(lst, last_price)
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
                    "latest_price": _r(r.get("latest_price")),
                    "pct_chg": _pct(r.get("pct_chg")),
                    "total_mv": _r(r.get("total_mv")),
                    "hfq_close": lb["close"] if lb else None,
                }
            )
        return rows

    def hfq_last(self, code: str) -> dict | None:
        """最近一根后复权 K 线 {date, close}（带缓存；真实价请用 basic.latest_price）。"""
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
        # 价格单位：按 最新真实价/后复权最新收盘 折算整条序列（等价于前复权，最新价=真实价）
        real = _r(basic_row["latest_price"]) if basic_row is not None else None
        hfq_last = _r(df["close"].iloc[-1])
        factor = real / hfq_last if (real is not None and hfq_last) else None
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

    def rank(
        self,
        start: str,
        end: str,
        metric: str = "ret",
        n: int = 30,
        ascending: bool = False,
    ) -> dict:
        """区间横截面排行：ret 区间收益 / vol 年化波动 / maxdd 最大回撤 / turnover 平均换手。"""
        if metric not in _RANK_METRICS:
            metric = "ret"
        key = (start, end)
        table = self._rank_cache.get(key)
        if table is None:
            table = self._compute_rank_table(start, end)
            self._rank_cache[key] = table
            self._rank_order.append(key)
            if len(self._rank_order) > _RANK_CACHE_MAX:
                self._rank_cache.pop(self._rank_order.pop(0), None)

        # 区间内交易日数（作为最低样本量）
        cal = self.calendar
        n_days = int(((cal >= pd.Timestamp(start)) & (cal <= pd.Timestamp(end))).sum())
        need = max(5, min(20, n_days))
        t = table[table["n_days"] >= need].copy()
        t = t.sort_values(metric, ascending=ascending).head(int(n))
        rows = []
        for code, r in t.iterrows():
            rows.append(
                {
                    "code": code,
                    "name": str(r["name"]),
                    "ret": _r(r["ret"]),
                    "vol": _r(r["vol"]),
                    "maxdd": _r(r["maxdd"]),
                    "turnover": _r(r["turnover"]),
                    "total_mv": _r(r["total_mv"]),
                    "n_days": int(r["n_days"]),
                }
            )
        return {"start": start, "end": end, "metric": metric, "n_days": n_days, "rows": rows}

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
        # 与 kline 一致：按 最新真实价/后复权最新收盘 折算到前复权（真实价）单位
        real = _r(self.basic.loc[code, "latest_price"]) if code in self.basic.index else None
        factor = real / res["last_close"] if (real is not None and res["last_close"]) else None
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
