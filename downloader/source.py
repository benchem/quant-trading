"""数据源层：东方财富为主源，腾讯行情为备用源（自动降级）。

东财（主源，字段全）:
- K线:  https://push2his.eastmoney.com/api/qt/stock/kline/get  (股票/指数, 不复权/前/后复权)
- 列表: https://push2.eastmoney.com/api/qt/clist/get           (沪深京A股快照+市值)
- 日历: 上证指数(1.000001)全部历史K线日期

腾讯（备用源，OHLCV，部分字段缺失为 NaN）:
- K线:  https://web.ifzq.gtimg.cn/appstock/app/fqkline/get     (单页最多640条, 自动倒序翻页)
- 列表: https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList (沪深A股)
- 日历: 上证指数(sh000001)历史K线日期

baostock（兜底源，字段全，自有服务器，2026-10 起启用）:
- 腾讯 fqkline API 被 WAF 全量封锁(501)、东财全网停用时自动降级至此；
- 仅支持沪深（北交所不支持）；后复权因子与腾讯 hfq 有 ~1% 的供应商差异。

策略：东财连续失败达到阈值后，本次运行内切换到腾讯为主；两源失败或被限流时
降级 baostock；baostock 也失败才抛错。
HTTP 传输由 downloader.http.HttpClient 处理（自动适配 TLS 指纹过滤环境）。
"""
from __future__ import annotations

import datetime as dt
import multiprocessing
import threading
import time

import pandas as pd

from . import codes
from .errors import SourceUnsupportedError
from .http import CooldownBlocked, FetchError, HttpClient, RateLimitedError
from .store import OHLCV_COLUMNS

# ------------------------------------------------------------ 东财
_EM_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_EM_CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
_EM_UT_KLINE = "7eea3edcaed734bea9cbfc24409ed989"
_EM_UT_CLIST = "bd1d9ddb04089700cf9c27f6f7426281"
_EM_FQT = {"none": "0", "qfq": "1", "hfq": "2"}
_EM_KLINE_FIELDS = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
_EM_KLINE_COLS = [
    "date", "open", "close", "high", "low",
    "volume", "amount", "amplitude", "pct_chg", "change", "turnover",
]
_EM_FS = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"  # 沪深京A股
_EM_LIST_FIELDS = "f12,f14,f2,f3,f8,f20,f21"  # 代码,名称,最新价,涨跌幅,换手率,总市值,流通市值
_EM_LIST_MAP = {
    "f12": "code_raw", "f14": "name", "f2": "latest_price",
    "f3": "pct_chg", "f8": "turnover", "f20": "total_mv", "f21": "float_mv",
}
# 板块列表（东财行情中心「板块」页）:
#   行业板块 m:90 t:2（细粒度行业分类 ~496 个，划分：每只A股恰好归属一个）
#   概念板块 m:90 t:3（题材/风格标签 ~500 个，动态增减，成分有重叠：一股可属多个）
#   地域板块 m:90 t:1（~38 个，按注册地）
# 行业/概念共用 BK 代码空间，成分股列表均为 b:BKxxxx
_EM_BOARD_LIST_FS = {"industry": "m:90 t:2", "concept": "m:90 t:3", "region": "m:90 t:1"}

# ------------------------------------------------------------ 腾讯
# K线同一 API 有多个主机；WAF 限流/拦截按主机区分，因此多主机自动切换:
# 实测 web.ifzq.gtimg.cn 曾被 WAF 整站 501，而 ifzq.gtimg.cn 同 API 正常。
_TX_KLINE_URLS = (
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
)
_TX_LIST_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
_TX_PAGE = 640  # 单页上限
_TX_FQ = {"none": "", "qfq": "qfq", "hfq": "hfq"}
_tx_kline_url_idx = [0]  # 当前首选主机（模块级，线程内更新，冲突无害）


def _em_probe():
    """轻量探测东财是否可用（一条小K线请求）。"""
    _em_klines("1.600519", "20240102", "20240103", "0")


class _SourceManager:
    """数据源容错管理:
    - 东财连续失败达到阈值 → 本次运行切换腾讯主源;
    - 腾讯模式下定期回探东财, 恢复后切回;
    - 任一源返回 403/429/501（IP 被限流）→ 进入冷却期, 期间零请求快速失败,
      冷却时长按指数退避(60s→120s→...→15min), 避免持续打请求加重惩罚。
    """

    def __init__(self, switch_threshold: int = 5, reprobe_every: int = 100,
                 cooldown_base: float = 60.0, cooldown_max: float = 900.0):
        self.switch_threshold = switch_threshold
        self.reprobe_every = reprobe_every
        self.cooldown_base = cooldown_base
        self.cooldown_max = cooldown_max
        self.em_failures = 0
        self.tx_calls = 0
        self.primary = "eastmoney"
        self._cooldown_until = 0.0
        self._cooldown_len = cooldown_base
        self._cooldown_hits = 0
        self._bs_used = False
        self.rotator = None  # ProxyRotator（--rotate 时由 app 层注入）

    def _announce_bs(self) -> None:
        if not self._bs_used:
            self._bs_used = True
            print("[source] 主源不可用/限流，本次运行降级使用 baostock 兜底源"
                  "（自有服务器，不走 Clash；后复权因子与腾讯 hfq 有 ~1% 供应商差异）",
                  flush=True)

    def configure_rotator(self, rotator) -> None:
        self.rotator = rotator

    # ---- 冷却
    def _in_cooldown(self) -> bool:
        if time.monotonic() < self._cooldown_until:
            return True
        self._cooldown_until = 0.0
        self._cooldown_len = self.cooldown_base
        self._cooldown_hits = 0
        return False

    def trip_cooldown(self) -> None:
        # 优先：切换 Clash 节点换出口 IP（--rotate 时）
        if self.rotator is not None and self.rotator.rounds_left > 0:
            if self.rotator.rotate():
                self._cooldown_until = time.monotonic() + 5  # 短暂等待新路由生效
                self._cooldown_len = self.cooldown_base
                self._cooldown_hits = 0
                return
            print("[source] 节点轮换无可用节点，转入冷却等待", flush=True)
        cur = self._cooldown_len
        self._cooldown_until = time.monotonic() + cur
        self._cooldown_len = min(self.cooldown_max, self._cooldown_len * 2)
        self._cooldown_hits += 1
        if self._cooldown_hits <= 3:
            print(
                f"[source] 检测到限流(403/429/501)，暂停请求 {int(cur)}s"
                f"（反复触发会指数退避）。本次运行中失败的股票稍后重跑同一命令即可补齐。",
                flush=True,
            )

    def _blocked(self, desc: str) -> CooldownBlocked:
        return CooldownBlocked(f"数据源限流冷却中，跳过 [{desc}]（稍后重跑命令即可补齐）")

    # ---- 主逻辑
    def _bs_result(self, bs_fn, desc: str):
        """尝试 baostock 兜底。
        - 成功: 返回 DataFrame
        - 源不支持该证券(如北交所): 抛 FetchError（不触发限流冷却）
        - 其他失败: 返回 None（调用方决定冷却/报错）
        """
        if bs_fn is None:
            return None
        self._announce_bs()
        try:
            return bs_fn()
        except SourceUnsupportedError as e:
            raise FetchError(f"[{desc}] {e}") from e
        except Exception:  # noqa: BLE001
            return None

    def with_fallback(self, desc: str, em_fn, tx_fn, bs_fn=None):
        if self._in_cooldown():
            raise self._blocked(desc)
        if self.primary == "tencent":
            self.tx_calls += 1
            if self.tx_calls % self.reprobe_every == 0:
                try:
                    _em_probe()
                    self.primary = "eastmoney"
                    self.em_failures = 0
                    self.tx_calls = 0
                    print("[source] 东财已恢复，主源切回: 东财", flush=True)
                except RateLimitedError:
                    self.trip_cooldown()
                    raise self._blocked(desc)
                except Exception:  # noqa: BLE001
                    pass
        if self.primary == "tencent":
            try:
                return tx_fn()
            except RateLimitedError:
                r = self._bs_result(bs_fn, desc)
                if r is not None:
                    return r
                self.trip_cooldown()
                raise self._blocked(desc)
            except Exception as e_tx:  # noqa: BLE001
                try:
                    return em_fn()
                except RateLimitedError:
                    r = self._bs_result(bs_fn, desc)
                    if r is not None:
                        return r
                    self.trip_cooldown()
                    raise self._blocked(desc)
                except Exception as e_em:  # noqa: BLE001
                    r = self._bs_result(bs_fn, desc)
                    if r is not None:
                        return r
                    if bs_fn is None:
                        raise FetchError(
                            f"两个数据源均失败 [{desc}]: 腾讯={e_tx} | 东财={e_em}"
                        ) from e_em
                    raise FetchError(
                        f"三个数据源均失败 [{desc}]: 腾讯={e_tx} | 东财={e_em} | baostock=失败"
                    ) from e_em
        try:
            result = em_fn()
            self.em_failures = 0
            return result
        except RateLimitedError:
            r = self._bs_result(bs_fn, desc)
            if r is not None:
                return r
            self.trip_cooldown()
            raise self._blocked(desc)
        except Exception as e:  # noqa: BLE001
            self.em_failures += 1
            if self.em_failures >= self.switch_threshold:
                print(f"[source] 东财连续失败 {self.em_failures} 次，本次运行切换为主源: 腾讯",
                      flush=True)
                self.primary = "tencent"
            try:
                return tx_fn()
            except RateLimitedError:
                r = self._bs_result(bs_fn, desc)
                if r is not None:
                    return r
                self.trip_cooldown()
                raise self._blocked(desc)
            except Exception as e2:  # noqa: BLE001
                r = self._bs_result(bs_fn, desc)
                if r is not None:
                    return r
                if bs_fn is None:
                    raise FetchError(
                        f"两个数据源均失败 [{desc}]: 东财={e} | 腾讯={e2}"
                    ) from e2
                raise FetchError(
                    f"三个数据源均失败 [{desc}]: 东财={e} | 腾讯={e2} | baostock=失败"
                ) from e2


_mgr = _SourceManager()
_client = HttpClient()


def configure_http(rate: float | None = None, timeout: float | None = None) -> None:
    """由 CLI 层调用：设置全局请求速率/超时。"""
    if rate is not None:
        _client.set_rate(float(rate))
    if timeout is not None:
        _client.timeout = float(timeout)


# ============================================================ 通用工具
def _d(s: str) -> dt.date:
    return dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _iso(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


# ============================================================ 东财
def _em_secid(wind_code: str) -> str:
    num, mkt = wind_code.split(".")
    return f"{'1' if mkt == 'SH' else '0'}.{num}"


def _em_klines(secid: str, beg: str, end: str, fqt: str) -> list[str]:
    params = {
        "secid": secid,
        "ut": _EM_UT_KLINE,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": _EM_KLINE_FIELDS,
        "klt": "101",
        "fqt": fqt,
        "beg": beg,
        "end": end,
    }
    data = _client.get_json(_EM_KLINE_URL, params)
    d = data.get("data") or {}
    k = d.get("klines")
    if not d:
        raise FetchError(f"东财K线返回空 (secid={secid})")
    return k or []


def _em_klines_to_df(klines: list[str], wind_code: str) -> pd.DataFrame:
    if not klines:
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    df = pd.DataFrame([k.split(",") for k in klines], columns=_EM_KLINE_COLS)
    df["date"] = pd.to_datetime(df["date"])
    for c in ("open", "close", "high", "low", "volume", "amount",
              "amplitude", "pct_chg", "change", "turnover"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["volume"] = df["volume"].fillna(0).astype("int64")
    df["code"] = wind_code
    return df[[c for c in OHLCV_COLUMNS if c in df.columns]]


def _em_daily(wind_code: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    klines = _em_klines(_em_secid(wind_code), start, end, _EM_FQT.get(adjust, "2"))
    return _em_klines_to_df(klines, wind_code)


def _em_index_daily(wind_code: str, start: str, end: str) -> pd.DataFrame:
    num, mkt = wind_code.split(".")
    preferred = "1" if mkt == "SH" else "0"
    ordered = []
    for m in [_EM_INDEX_MARKET.get(num), preferred, "0", "1", "2"]:
        if m and m not in ordered:
            ordered.append(m)
    last_err: Exception | None = None
    for m in ordered:
        try:
            klines = _em_klines(f"{m}.{num}", start, end, "0")
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
        if klines:
            _EM_INDEX_MARKET[num] = m
            return _em_klines_to_df(klines, wind_code)
    if last_err is not None:
        raise last_err
    return pd.DataFrame(columns=OHLCV_COLUMNS)


_EM_INDEX_MARKET: dict[str, str] = {}  # 指数 secid 市场前缀缓存


def _em_stock_list() -> pd.DataFrame:
    page, pz, total, items = 1, 500, None, []
    while True:
        params = {
            "pn": str(page), "pz": str(pz), "po": "1", "np": "1",
            "ut": _EM_UT_CLIST, "fltt": "2", "invt": "2", "fid": "f12",
            "fs": _EM_FS, "fields": _EM_LIST_FIELDS,
        }
        data = _client.get_json(_EM_CLIST_URL, params)
        d = data.get("data") or {}
        if not d:
            raise FetchError("东财列表返回空")
        total = d.get("total") or 0
        chunk = d.get("diff") or []
        if not chunk:
            break
        field_names = _EM_LIST_FIELDS.split(",")
        for it in chunk:
            items.append(it if isinstance(it, dict) else dict(zip(field_names, it)))
        if len(items) >= total or page > 200:
            break
        page += 1
    rows = []
    for it in items:
        rec = {_EM_LIST_MAP[k]: v for k, v in it.items() if k in _EM_LIST_MAP}
        if rec.get("code_raw"):
            rows.append(rec)
    if not rows:
        raise FetchError("东财股票列表为空")
    out = pd.DataFrame(rows)
    out["code"] = [codes.to_wind_code(str(c)) for c in out["code_raw"].astype(str)]
    out = out.drop(columns=["code_raw"])
    for c in ("latest_price", "pct_chg", "turnover", "total_mv", "float_mv"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64")
    return out.sort_values("code").reset_index(drop=True)


def _em_clist_page(fs: str, fields: str, page: int, pz: int = 200) -> tuple[int, list]:
    """东财 clist 通用分页：返回 (total, 本页 diff 列表)。"""
    params = {
        "pn": str(page), "pz": str(pz), "po": "1", "np": "1",
        "ut": _EM_UT_CLIST, "fltt": "2", "invt": "2", "fid": "f12",
        "fs": fs, "fields": fields,
    }
    data = _client.get_json(_EM_CLIST_URL, params)
    d = data.get("data") or {}
    if not d:
        raise FetchError(f"东财列表返回空 (fs={fs})")
    return (d.get("total") or 0), (d.get("diff") or [])


def _em_board_list(board_type: str = "industry") -> pd.DataFrame:
    """东财板块列表（行业约 496 个细粒度行业 / 概念约 500 个题材标签 / 地域约 38 个）。"""
    fs = _EM_BOARD_LIST_FS.get(board_type)
    if not fs:
        raise FetchError(f"未知板块类型: {board_type}")
    rows, page = [], 1
    while True:
        total, diff = _em_clist_page(fs, "f12,f14", page)
        if not diff:
            break
        rows.extend({"board_code": str(it["f12"]), "board_name": str(it["f14"])} for it in diff)
        if len(rows) >= total or page > 60:
            break
        page += 1
    if not rows:
        raise FetchError(f"东财{'概念' if board_type == 'concept' else '行业'}板块列表为空")
    out = pd.DataFrame(rows).drop_duplicates("board_code")
    return out.sort_values("board_code").reset_index(drop=True)


def _em_board_members(board_code: str) -> list[str]:
    """单个板块的全部成分股（原始 6 位代码）。"""
    rows, page = [], 1
    while True:
        total, diff = _em_clist_page(f"b:{board_code}", "f12", page)
        if not diff:
            break
        rows.extend(str(it["f12"]) for it in diff)
        if len(rows) >= total or page > 50:
            break
        page += 1
    return rows


def fetch_board_members(
    valid_codes: set[str] | None = None, board_type: str = "industry"
) -> tuple[pd.DataFrame, list[str]]:
    """板块成分表 [code, board_type, board_code, board_name]。

    board_type: industry 行业板块（东财约 496 个细粒度行业；东财板块列表不可用时
    降级 baostock 证监会行业分类，约 84 个行业，board_code 为行业代码+名称，
    口径更粗，东财恢复后重跑可换回）| concept 概念板块（东财约 500 个题材/风格
    标签，成分有重叠，仅东财源，无兜底）。
    valid_codes: 股票列表代码集合（Wind 风格），提供时剔除已退市代码
    （baostock 行业表含历史退市股，东财成分表天然为在市股票）。

    东财路径逐板块拉成分（每类约 500 次请求，受全局限速约束，默认约 2 分钟/类）；
    单板块失败（含一次即时重试）不中断整体：计入返回的失败列表，
    由调用方保留旧缓存对应板块、重跑同一命令自动补齐。
    数据源不可用、或过半板块失败时抛错。
    """
    if board_type == "industry":
        try:
            boards = _em_board_list("industry")
        except Exception as e:  # noqa: BLE001 东财不可用 -> baostock 行业分类兜底
            print(f"[source] 东财行业板块不可用（{str(e)[:80]}），降级 baostock 证监会行业分类"
                  "（口径更粗，东财恢复后重跑本命令可换回细粒度行业）", flush=True)
            return _bs_board_members(valid_codes)
        return _fetch_board_members_em(valid_codes, "industry", boards)
    return _fetch_board_members_em(valid_codes, board_type)


def _fetch_board_members_em(
    valid_codes: set[str] | None, board_type: str = "industry", boards: pd.DataFrame | None = None
) -> tuple[pd.DataFrame, list[str]]:
    """东财板块成分（行业=划分每股一行；概念=有重叠每股每板块一行）。"""
    if boards is None:
        boards = _em_board_list(board_type)
    rows: list[dict] = []
    failed: list[str] = []
    for _, b in boards.iterrows():
        codes_raw: list[str] = []
        for attempt in range(2):
            try:
                codes_raw = _em_board_members(b["board_code"])
                break
            except Exception:  # noqa: BLE001 单板块失败不中断整体
                if attempt == 1:
                    failed.append(str(b["board_code"]))
        for c in codes_raw:
            try:
                rows.append({
                    "code": codes.to_wind_code(c),
                    "board_type": board_type,
                    "board_code": str(b["board_code"]),
                    "board_name": str(b["board_name"]),
                })
            except ValueError:
                continue  # 极少量非A股代码（如 B 股）直接跳过
    if len(failed) > len(boards) // 2:
        raise FetchError(f"板块成分抓取大面积失败（{len(failed)}/{len(boards)} 个板块），请稍后重跑")
    if not rows:
        raise FetchError("板块成分列表为空")
    # 行业为划分（每股唯一板块，按 code 去重）；概念有重叠（按 code+board_code 去重）
    dedup = "code" if board_type == "industry" else ["code", "board_code"]
    out = pd.DataFrame(rows).drop_duplicates(dedup, keep="first")
    if valid_codes:
        out = out[out["code"].isin(valid_codes)]
        if out.empty:
            raise FetchError("板块成分表过滤后为空（股票列表与成分表不匹配？）")
    return out.sort_values(["code", "board_code"]).reset_index(drop=True), failed


# ============================================================ 腾讯
def _tx_symbol(wind_code: str) -> str:
    num, mkt = wind_code.split(".")
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}[mkt] + num


def _tx_kline_page(sym: str, fq: str, start_dt: dt.date, end_dt: dt.date) -> list:
    """单页K线请求；多主机自动切换（WAF 限流按主机区分）。"""
    params = {"param": f"{sym},day,{_iso(start_dt)},{_iso(end_dt)},{_TX_PAGE},{fq}"}
    key = {"": "day", "qfq": "qfqday", "hfq": "hfqday"}[fq]
    last_err: Exception | None = None
    n = len(_TX_KLINE_URLS)
    for i in range(n):
        idx = (_tx_kline_url_idx[0] + i) % n
        try:
            data = _client.get_json(_TX_KLINE_URLS[idx], params)
            _tx_kline_url_idx[0] = idx
            d = (data.get("data") or {}).get(sym) or {}
            rows = d.get(key)
            if rows:
                return rows
            # ⚠️ 复权键缺失时绝不能退回 d["day"]（不复权原始价）。
            # 2026-10-25 事故: 对无交易日窗口(如周五收盘后取 周六~周日)腾讯会回退
            # 返回最新一根【未复权】bar，曾静默混入 hfq 序列导致末根单位断裂、
            # 前复权折算因子归 1，全市场图表价格整体错位。此时应报错走兜底源。
            if fq and d.get("day"):
                raise FetchError(f"腾讯返回缺失 {key} 键(复权数据不可用): {sym}")
            return []
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise FetchError(f"腾讯K线全部主机失败 (symbol={sym}): {last_err}")


def _tx_klines(wind_code: str, start: str, end: str, adjust: str) -> list[list]:
    """腾讯K线（倒序翻页拉取 [start, end] 全区间）。行: [date,open,close,high,low,volume手]"""
    sym = _tx_symbol(wind_code)
    fq = _TX_FQ.get(adjust, "hfq")
    start_dt, cur_end_dt = _d(start), _d(end)
    end_dt = cur_end_dt  # 原始请求区间上界（cur_end_dt 会随翻页前移）
    rows: list[list] = []
    for _ in range(60):  # 安全上限 ~60*640 个交易日
        k = _tx_kline_page(sym, fq, start_dt, cur_end_dt)
        if not k:
            if not rows:
                raise FetchError(f"腾讯K线返回空 (symbol={sym}, {start}~{end})")
            break
        rows = k + rows
        oldest = dt.date.fromisoformat(str(k[0][0])[:10])
        if oldest <= start_dt:
            break
        cur_end_dt = oldest - dt.timedelta(days=1)
        if cur_end_dt < start_dt:
            break
    # 腾讯对无交易日窗口会「回退」返回最新数据（可能早于请求起点），统一裁剪到区间内
    lo, hi = str(start_dt), str(end_dt)
    return [r for r in rows if lo <= str(r[0])[:10] <= hi]


def _tx_klines_to_df(rows: list[list], wind_code: str) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    df = pd.DataFrame(
        [r[:6] for r in rows],
        columns=["date", "open", "close", "high", "low", "volume"],
    )
    df["date"] = pd.to_datetime(df["date"])
    for c in ("open", "close", "high", "low", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["volume"] = df["volume"].fillna(0).astype("int64")
    # 腾讯源没有以下字段，置 NaN（建模时可直接用 close 计算收益）
    for c in ("amount", "amplitude", "pct_chg", "change", "turnover"):
        df[c] = float("nan")
    df["code"] = wind_code
    return df[[c for c in OHLCV_COLUMNS if c in df.columns]]


def _tx_daily(wind_code: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    return _tx_klines_to_df(_tx_klines(wind_code, start, end, adjust), wind_code)


def _tx_index_daily(wind_code: str, start: str, end: str) -> pd.DataFrame:
    return _tx_klines_to_df(_tx_klines(wind_code, start, end, "none"), wind_code)


def _tx_stock_list() -> pd.DataFrame:
    items: list[dict] = []
    offset, total = 0, None
    while True:
        params = {
            "board_code": "aStock", "sort_type": "price", "direct": "down",
            "offset": str(offset), "count": "200",
        }
        data = _client.get_json(_TX_LIST_URL, params)
        d = data.get("data") or {}
        if not d.get("rank_list"):
            break
        total = d.get("total") or 0
        items.extend(d["rank_list"])
        offset += len(d["rank_list"])
        if offset >= total or offset > 100000:
            break
    if not items:
        raise FetchError("腾讯股票列表为空")
    rows = []
    for it in items:
        rows.append(
            {
                "code": codes.to_wind_code(str(it["code"])),
                "name": str(it.get("name", "")),
                "latest_price": _num(it.get("zxj")),
                "pct_chg": _num(it.get("zdf")),
                "turnover": _num(it.get("hsl")),
                "total_mv": _num(it.get("zsz")) * 1e8,
                "float_mv": _num(it.get("ltsz")) * 1e8,
            }
        )
    out = pd.DataFrame(rows).drop_duplicates(subset=["code"], keep="first")
    return out.sort_values("code").reset_index(drop=True)


def _tx_trade_dates() -> pd.Series:
    rows = _tx_klines("000001.SH", "19901201", "20500101", "none")
    if not rows:
        raise FetchError("腾讯交易日历为空")
    return pd.Series(pd.to_datetime([str(r[0])[:10] for r in rows]))


# ============================================================ baostock（兜底源）
# 2026-10 实测背景：腾讯 fqkline API 被 WAF 全量封锁（501）、东财全网停用时启用。
# ⚠️ adjustflag 映射（2026-10-24 实测，与官方文档描述不一致，以实测为准）:
#     "1" = 后复权(hfq)    "2" = 前复权(qfq)    "3" = 不复权
# 注意：baostock 后复权因子与腾讯 hfq 有 ~1% 差异（两家除权除息舍入口径不同），
# 同一只股票跨供应商的 hfq 绝对价位不完全一致，收益率序列基本一致。
# 仅支持沪深（北交所会报“股票代码未标识sh或sz”）。
#
# baostock SDK 的硬伤（2026-10-26 事故复盘）：
#   1) 全进程共享一条全局 TCP 连接（baostock.common.context.default_socket），
#      且 SDK 自带全局锁语义 -> 全部调用必须持 _BS_LOCK 串行执行；
#   2) SDK 的 socket 不设超时（socketutil.recv 死循环等数据），一条半开连接
#      就会让持锁线程永久挂起 -> 4 个 worker 全部卡死、EOD 无声停摆 1 小时。
# 因此这里用「spawn 子进程池」跑 baostock：每进程一条独立连接 + socket 超时 +
# 父进程侧 result(timeout) 兜底，把「单点挂死」和「串行吞吐低」一起解决；
# 池不可用（import 失败/创建失败）或查询超时/损坏时回退进程内老路径（CLI 单股查询仍可用）。
_BS_ADJ = {"hfq": "1", "qfq": "2", "none": "3"}
_BS_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg"
_BS_LOCK = threading.Lock()
_BS_LOGGED_IN = False
_BS_SOCKET_TIMEOUT = 60.0        # SDK socket 读超时（秒）
_BS_POOL_QUERY_TIMEOUT = 90.0    # 单次查询父进程侧兜底超时（秒）；> socket 超时，
                                 # 只有 worker 真僵死才触发，触发即击杀池
_BS_POOL_SIZE = 4                # 子进程数（即并发连接数）
_BS_POOL: object | None = None   # ProcessPoolExecutor（懒加载）
_BS_POOL_DEAD = False            # 池创建失败/超时被击杀 -> 本进程内只走进程内路径
_BS_PATCHED = False              # SocketUtil.connect 已打超时补丁

try:  # py3.10: concurrent.futures.TimeoutError 不是内建 TimeoutError
    from concurrent.futures import TimeoutError as _PoolTimeoutError
except ImportError:  # pragma: no cover
    _PoolTimeoutError = TimeoutError


def _bs_apply_socket_timeout() -> None:
    """给 baostock 全局 socket 设读超时（SDK 自身不设，recv 会永久阻塞）。

    必须在 login() 之后调用：登录才会创建 context.default_socket。
    子进程入口与进程内路径共用。
    """
    try:
        import baostock.common.context as bs_ctx

        sock = getattr(bs_ctx, "default_socket", None)
        if sock is not None:
            sock.settimeout(_BS_SOCKET_TIMEOUT)
    except Exception:  # noqa: BLE001
        pass


def _bs_patch_socket_connect() -> None:
    """给 SocketUtil.connect 打补丁：连接一建立就设超时（幂等，进程内一次）。

    覆盖 login 阶段以及仍走进程内的日历/行业查询——SDK 原版 connect 后
    socket 无超时，半开连接会让 recv 永久阻塞并占死 _BS_LOCK。
    """
    global _BS_PATCHED
    if _BS_PATCHED:
        return
    try:
        import baostock.util.socketutil as bs_sock

        _orig_connect = bs_sock.SocketUtil.connect

        def _connect_with_timeout(self, api_key):
            _orig_connect(self, api_key)
            _bs_apply_socket_timeout()

        bs_sock.SocketUtil.connect = _connect_with_timeout
        _BS_PATCHED = True
    except Exception:  # noqa: BLE001
        pass


def _bs_pool_get():
    """懒加载 spawn 进程池；失败置 _BS_POOL_DEAD 永久回退进程内。"""
    global _BS_POOL, _BS_POOL_DEAD
    if _BS_POOL_DEAD or _BS_POOL is not None:
        return _BS_POOL
    try:
        from concurrent.futures import ProcessPoolExecutor

        pool = ProcessPoolExecutor(
            max_workers=_BS_POOL_SIZE, mp_context=multiprocessing.get_context("spawn"))
        pool.submit(int, 0).result(timeout=30)  # 预热首进程，失败视为不可用
        _BS_POOL = pool
        print(f"[source] baostock 兜底源已启用进程池 x{_BS_POOL_SIZE}"
              f"（独立连接 + {int(_BS_SOCKET_TIMEOUT)}s socket 超时）", flush=True)
    except Exception as e:  # noqa: BLE001
        _BS_POOL_DEAD = True
        _BS_POOL = None
        print(f"[source] baostock 进程池不可用（{str(e)[:60]}），回退进程内串行", flush=True)
    return _BS_POOL


def _bs_pool_kill(reason: str) -> None:
    """击杀进程池：本进程后续全部走进程内路径（防泄漏槽位拖慢每只查询）。"""
    global _BS_POOL, _BS_POOL_DEAD
    if _BS_POOL_DEAD:
        return
    _BS_POOL_DEAD = True
    pool, _BS_POOL = _BS_POOL, None
    print(f"[source] baostock 进程池异常（{reason}），本轮剩余查询回退进程内串行", flush=True)
    if pool is not None:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            pass


def _bs_pool_worker(code: str, start: str, end: str, flag: str) -> list[list]:
    """子进程入口：打补丁 -> 登录 -> 查询 -> 返回原始行。

    每个子进程一条独立 TCP 连接，互不共享，无需加锁。
    数据级错误（登录失败/查询失败）以异常上抛，由父进程按老语义处理。
    """
    import baostock as bs

    _bs_patch_socket_connect()
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock 登录失败: {lg.error_msg}")
    rs = bs.query_history_k_data_plus(
        code, _BS_FIELDS,
        start_date=_bs_iso(start), end_date=_bs_iso(end),
        frequency="d", adjustflag=flag,
    )
    if rs.error_code != "0":
        raise RuntimeError(f"baostock 查询失败 ({code}): {rs.error_msg}")
    rows: list[list] = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    return rows


def _bs_query_via_pool(code: str, start: str, end: str, flag: str):
    """经进程池查询。

    返回 None 表示池基础设施不可用（懒加载失败/超时被击杀），调用方回退进程内；
    worker 的数据级异常正常上抛（与进程内路径同语义）。
    """
    pool = _bs_pool_get()
    if pool is None:
        return None
    try:
        fut = pool.submit(_bs_pool_worker, code, start, end, flag)
        return fut.result(timeout=_BS_POOL_QUERY_TIMEOUT)
    except _PoolTimeoutError:
        _bs_pool_kill(f"查询超时 >{int(_BS_POOL_QUERY_TIMEOUT)}s")
        return None
    except BrokenProcessPool:
        _bs_pool_kill("子进程池损坏")
        return None


def _bs_iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def _bs_code(wind_code: str) -> str:
    num, mkt = wind_code.split(".")
    if mkt not in ("SH", "SZ"):
        raise SourceUnsupportedError(f"baostock 不支持 {mkt} 市场（仅沪深，北交所需等腾讯/东财恢复）")
    return ("sh." if mkt == "SH" else "sz.") + num


def _bs_ensure_login(relogin: bool = False) -> None:
    """baostock 登录（调用方需已持有 _BS_LOCK）。"""
    global _BS_LOGGED_IN
    import baostock as bs

    if _BS_LOGGED_IN and not relogin:
        return
    _bs_patch_socket_connect()
    try:
        bs.logout()
    except Exception:  # noqa: BLE001
        pass
    lg = bs.login()
    if lg.error_code != "0":
        raise FetchError(f"baostock 登录失败: {lg.error_msg}")
    _bs_apply_socket_timeout()  # login 创建 socket 后立即设超时，防 recv 永久阻塞
    _BS_LOGGED_IN = True


def _bs_query(code: str, start: str, end: str, flag: str) -> list[list]:
    """baostock 查询入口：优先 spawn 子进程池（并发+隔离挂死），回退进程内串行。"""
    rows = _bs_query_via_pool(code, start, end, flag)
    if rows is not None:
        return rows
    return _bs_query_inproc(code, start, end, flag)


def _bs_query_inproc(code: str, start: str, end: str, flag: str, _attempt: int = 1) -> list[list]:
    """进程内串行查询（持 _BS_LOCK，一次断线重登录重试）。"""
    global _BS_LOGGED_IN
    import baostock as bs

    with _BS_LOCK:
        _bs_ensure_login(relogin=_attempt > 1)
        rs = bs.query_history_k_data_plus(
            code, _BS_FIELDS,
            start_date=_bs_iso(start), end_date=_bs_iso(end),
            frequency="d", adjustflag=flag,
        )
        if rs.error_code != "0":
            if _attempt == 1:
                _BS_LOGGED_IN = False  # 连接可能已断，重新登录后重试一次
                return _bs_query_inproc(code, start, end, flag, _attempt + 1)
            raise FetchError(f"baostock 查询失败 ({code}): {rs.error_msg}")
        rows: list[list] = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
    return rows


def _bs_rows_to_df(rows: list[list], wind_code: str) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    df = pd.DataFrame(rows, columns=_BS_FIELDS.split(","))
    df["date"] = pd.to_datetime(df["date"])
    for c in ("open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pctChg"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[df["close"].notna()].reset_index(drop=True)
    if df.empty:
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    df["change"] = df["close"] - df["preclose"]
    df["amplitude"] = (df["high"] - df["low"]) / df["preclose"] * 100
    out = pd.DataFrame({
        "date": df["date"],
        "code": wind_code,
        "open": df["open"],
        "high": df["high"],
        "low": df["low"],
        "close": df["close"],
        "volume": (df["volume"] / 100).fillna(0).astype("int64"),  # 股 -> 手
        "amount": df["amount"],
        "amplitude": df["amplitude"],
        "pct_chg": df["pctChg"],
        "change": df["change"],
        "turnover": df["turn"],
    })
    return out[[c for c in OHLCV_COLUMNS if c in out.columns]]


def _bs_daily(wind_code: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    rows = _bs_query(_bs_code(wind_code), start, end, _BS_ADJ.get(adjust, "1"))
    return _bs_rows_to_df(rows, wind_code)


def _bs_index_daily(wind_code: str, start: str, end: str) -> pd.DataFrame:
    num, mkt = wind_code.split(".")
    code = ("sh." if mkt == "SH" else "sz.") + num
    rows = _bs_query(code, start, end, "3")  # 指数不复权
    return _bs_rows_to_df(rows, wind_code)


def _bs_trade_dates() -> pd.Series:
    import baostock as bs

    year = dt.date.today().year
    start, end = f"{year - 6}-01-01", f"{year + 1}-12-31"
    with _BS_LOCK:
        _bs_ensure_login()
        rs = bs.query_trade_dates(start_date=start, end_date=end)
        if rs.error_code != "0":
            raise FetchError(f"baostock 交易日历失败: {rs.error_msg}")
        rows: list[list] = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
    dates = [r[0] for r in rows if r[1] == "1"]
    if not dates:
        raise FetchError("baostock 交易日历为空")
    return pd.Series(pd.to_datetime(dates))


def _bs_board_members(valid_codes: set[str] | None) -> tuple[pd.DataFrame, list[str]]:
    """baostock 证监会行业分类（东财板块兜底）：code -> 行业。

    返回与东财路径同构的 [code, board_code, board_name]：
    board_code = 行业代码+名称（如 "J66货币金融服务"），board_name = 行业名（如 "货币金融服务"）。
    行业表含历史退市股：valid_codes 提供时仅保留在市股票；仅覆盖沪深（北交所无行业，自然缺失）。
    """
    import baostock as bs

    with _BS_LOCK:
        _bs_ensure_login()
        rs = bs.query_stock_industry()
        if rs.error_code != "0":
            raise FetchError(f"baostock 行业分类查询失败: {rs.error_msg}")
        fields = list(rs.fields)
        rows: list[list] = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
    if not rows:
        raise FetchError("baostock 行业分类为空")
    df = pd.DataFrame(rows, columns=fields)
    df = df[df["industry"].astype(str).str.strip() != ""]
    out = []
    for _, r in df.iterrows():
        ind = str(r["industry"]).strip()
        name = ind.split(" ", 1)[1].strip() if " " in ind else ind
        try:
            code = codes.to_wind_code(str(r["code"]).replace(".", ""))
        except ValueError:
            continue
        if valid_codes and code not in valid_codes:
            continue
        out.append({"code": code, "board_type": "industry", "board_code": ind, "board_name": name})
    if not out:
        raise FetchError("baostock 行业分类过滤后为空")
    res = pd.DataFrame(out).drop_duplicates("code", keep="first")
    return res.sort_values("code").reset_index(drop=True), []


# ============================================================ 对外接口
def fetch_daily(wind_code: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    """单只A股日线。adjust: hfq / qfq / none。"""
    return _mgr.with_fallback(
        f"日线 {wind_code}",
        lambda: _em_daily(wind_code, start, end, adjust),
        lambda: _tx_daily(wind_code, start, end, adjust),
        bs_fn=lambda: _bs_daily(wind_code, start, end, adjust),
    )


def fetch_index_daily(wind_code: str, start: str, end: str) -> pd.DataFrame:
    """单只指数日线（不复权）。"""
    return _mgr.with_fallback(
        f"指数 {wind_code}",
        lambda: _em_index_daily(wind_code, start, end),
        lambda: _tx_index_daily(wind_code, start, end),
        bs_fn=lambda: _bs_index_daily(wind_code, start, end),
    )


def fetch_stock_list() -> pd.DataFrame:
    """全部沪深A股列表（含北交所，若主源支持）。"""
    return _mgr.with_fallback(
        "股票列表",
        _em_stock_list,
        _tx_stock_list,
    )


def fetch_trade_dates() -> pd.Series:
    """A股交易日历（上证指数历史K线日期 / baostock 兜底）。"""
    return _mgr.with_fallback(
        "交易日历",
        lambda: pd.Series(
            pd.to_datetime([k.split(",")[0] for k in _em_klines("1.000001", "19901201", "20500101", "0")])
        ),
        _tx_trade_dates,
        bs_fn=_bs_trade_dates,
    )
