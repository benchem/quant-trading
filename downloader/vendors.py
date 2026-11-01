"""新增三源（2026-11）：同花顺 v6 / 新浪 CN_MarketDataService / 搜狐 hisHq。

背景: 东财出口 IP 被 WAF 拉黑（快速失败）、腾讯对科创板(688/689)返回 501、
baostock 黑名单——科创板日线大面积停滞。三源均已从本机直连验证可用：

- 同花顺 v6（唯一提供前复权，用于锚定换算/修复）:
    增量(最近~140根): https://d.10jqka.com.cn/v6/line/hs_688001/02/last.js
    年度全史:          https://d.10jqka.com.cn/v6/line/hs_688001/02/2019.js (逐年拼)
    路径段 02=前复权 01=不复权 03=无数据；沪深+北交所统一 hs_ 前缀。
    bar: date,open,high,low,close,volume(股),amount(元),turnover%,?,?（可空列）
    响应为 JSONP（函数名与 URL 对应，含全史 meta: total/start/year）。
- 新浪（仅不复权，窗口上限 1023 根 ≈ 4 年，必须带 Referer: finance.sina.com.cn）:
    https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20t=/CN_MarketDataService.getKLineData
    响应 var t=([{day,open,high,low,close,volume(股)},...])；北交所返回 null。
- 搜狐 hisHq（仅不复权，任意日期区间，纯 JSON，覆盖北交所）:
    https://q.stock.sohu.com/hisHq?code=cn_688001&start=...&end=...&stat=1&order=D&period=d
    ⚠ 两个坑: order=D 返回降序（新→旧）；列序 date,open,close,change,pct,low,high,
    volume(手),amount(万),turnover%,? —— close 在第 2 列、low 在 high 前面。

本地文件口径 = 后复权(hfq)：新浪/搜狐的不复权数据与同花顺的前复权数据都必须经
 downloader.anchor 锚定换算后才能落盘，绝不许混基准写入。

HTTP 层复用 downloader.http.HttpClient（经 source._client，共享引擎探测与心跳）；
每源独立 RateLimiter（默认 2 req/s，configure_source_rate 可配）。
"""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd

from . import source as _source
from .anchor import (AnchorError, InsufficientOverlapError, MAX_CV,
                     compute_anchor_ratio, compute_tail_ratio, last_date,
                     scale_prices)
from .errors import FetchError, SourceUnsupportedError
from .ratelimit import RateLimiter
from .store import OHLCV_COLUMNS

THS_REFERER = "https://stockpage.10jqka.com.cn/"
SINA_REFERER = "https://finance.sina.com.cn"
_THS_LINE_URL = "https://d.10jqka.com.cn/v6/line/{symbol}/02/{segment}.js"
_SINA_KLINE_URL = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20t=/"
                   "CN_MarketDataService.getKLineData")
_SOHU_HIS_URL = "https://q.stock.sohu.com/hisHq"

# 每源独立限速器（多源分批调度用；configure_source_rate 重设）
DEFAULT_SOURCE_RATE = 2.0
#: 同花顺实测（2026-11-01）：连发第二个请求即可能拿到空响应（无 "data" 字段），
#: 请求间隔须 ≥1.5s —— ths 单独用更保守的默认限速。
THS_SOURCE_RATE = 0.66
ths_limiter = RateLimiter(THS_SOURCE_RATE)
sina_limiter = RateLimiter(DEFAULT_SOURCE_RATE)
sohu_limiter = RateLimiter(DEFAULT_SOURCE_RATE)


def configure_source_rate(rate: float, ths_rate: "float | None" = None) -> None:
    """重设三源独立限速器（调用方在 CLI 层；<=0 不限速）。

    ths_rate 缺省随 rate（--source-rate 显式指定时视为用户选择，含 ths 一起改）。
    """
    global ths_limiter, sina_limiter, sohu_limiter
    ths_limiter = RateLimiter(float(ths_rate if ths_rate is not None else rate))
    sina_limiter = RateLimiter(float(rate))
    sohu_limiter = RateLimiter(float(rate))


def _client():
    """复用 source 模块的 HttpClient（共享引擎探测、TLS 指纹适配与心跳）。"""
    return _source._client


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


# ============================================================ 符号映射
def ths_symbol(wind_code: str) -> str:
    """同花顺符号：沪深 + 北交所统一 hs_ 前缀（hs_688001 / hs_920002）。"""
    num = wind_code.split(".")[0]
    return f"hs_{num}"


def sina_symbol(wind_code: str) -> str:
    """新浪符号：sh/sz 前缀；北交所不支持（返回 null）。"""
    num, mkt = wind_code.split(".")
    if mkt == "BJ":
        raise SourceUnsupportedError("新浪不支持北交所（920/430/8xx 返回 null）")
    return ("sh" if mkt == "SH" else "sz") + num


def sohu_symbol(wind_code: str) -> str:
    """搜狐符号：cn_ 前缀（cn_688001 / cn_920002，覆盖北交所）。"""
    num = wind_code.split(".")[0]
    return f"cn_{num}"


# ============================================================ 解析器（纯函数，离线可测）
def _finalize(rows: list[dict], wind_code: str) -> pd.DataFrame:
    """行列表 -> OHLCV_COLUMNS 规范 DataFrame（升序、按日期去重、volume int64）。"""
    if not rows:
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["date"])
    df = df.drop_duplicates(subset=["date"], keep="last")
    df = df.sort_values("date", kind="stable").reset_index(drop=True)
    for c in ("open", "high", "low", "close", "volume", "amount",
              "amplitude", "pct_chg", "change", "turnover"):
        if c not in df.columns:
            df[c] = float("nan")
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["volume"] = df["volume"].fillna(0).astype("int64")
    df["code"] = wind_code
    return df[[c for c in OHLCV_COLUMNS if c in df.columns]]


def _jsonp_payload(text: str) -> str:
    """JSONP -> 括号内 JSON 文本（函数名任意：取首个 '(' 与末个 ')' 之间）。"""
    s = text.strip()
    if not s:
        raise FetchError("JSONP 响应为空")
    i, j = s.find("("), s.rfind(")")
    if i < 0 or j <= i:
        raise FetchError(f"JSONP 格式异常（无括号包裹）: {s[:60]}")
    return s[i + 1:j]


def parse_ths_jsonp(text: str, wind_code: str = "") -> pd.DataFrame:
    """解析同花顺 v6 line JSONP（last.js / {year}.js 同构）。

    - data 内 bar 用 ';' 分隔、字段逗号分隔，部分列可能为空；
    - close 是第 5 列（date,open,high,low,close,...）；volume 单位为股（->手）；
    - 缺 data 字段/畸形 JSON 抛 FetchError（不得静默返回错数据）。
    """
    try:
        obj = json.loads(_jsonp_payload(text))
    except (FetchError, json.JSONDecodeError) as e:
        raise FetchError(f"同花顺响应解析失败: {e}") from e
    if not isinstance(obj, dict) or "data" not in obj:
        raise FetchError("同花顺响应缺 data 字段")
    rows: list[dict] = []
    for bar in str(obj.get("data") or "").split(";"):
        f = bar.split(",")
        if len(f) < 5 or not f[0]:
            continue
        d = str(f[0])
        if len(d) != 8 or not d.isdigit():
            continue
        try:
            o, h, l, c = float(f[1]), float(f[2]), float(f[3]), float(f[4])
        except ValueError:
            continue  # 价格列残缺的 bar 跳过（挂牌前占位等）
        rows.append({
            "date": f"{d[:4]}-{d[4:6]}-{d[6:8]}",
            "open": o, "high": h, "low": l, "close": c,
            "volume": (_f(f[5]) / 100) if len(f) > 5 and f[5] else 0.0,  # 股 -> 手
            "amount": _f(f[6]) if len(f) > 6 else float("nan"),          # 元
            "turnover": _f(f[7]) if len(f) > 7 else float("nan"),        # %
        })
    return _finalize(rows, wind_code)


def parse_sina_jsonp(text: str, wind_code: str = "") -> pd.DataFrame:
    """解析新浪 getKLineData JSONP（var t=([...])，字段全字符串，volume 股->手）。

    不支持的代码（北交所）返回 null / 空数组 -> 空 DataFrame（不抛错）。
    """
    s = text.strip()
    if not s:
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    i, j = s.find("["), s.rfind("]")
    if i < 0 or j <= i:
        return pd.DataFrame(columns=OHLCV_COLUMNS)  # var t=(null) 等
    try:
        arr = json.loads(s[i:j + 1])
    except json.JSONDecodeError as e:
        raise FetchError(f"新浪响应解析失败: {e}") from e
    rows: list[dict] = []
    for it in arr or []:
        if not isinstance(it, dict) or not it.get("day"):
            continue
        rows.append({
            "date": str(it["day"]),
            "open": _f(it.get("open")), "high": _f(it.get("high")),
            "low": _f(it.get("low")), "close": _f(it.get("close")),
            "volume": _f(it.get("volume")) / 100,  # 股 -> 手
        })
    return _finalize(rows, wind_code)


def parse_sohu_json(text: str, wind_code: str = "") -> pd.DataFrame:
    """解析搜狐 hisHq JSON（纯 JSON，status!=0/空 hq -> 空 DataFrame）。

    ⚠ 列序: date,open,close,change,pct,low,high,volume(手),amount(万),turnover%,?
       close 在第 2 列、low 在 high 前面；order=D 返回降序（这里统一转升序）。
    """
    try:
        arr = json.loads(text)
    except (json.JSONDecodeError, TypeError) as e:
        raise FetchError(f"搜狐响应解析失败: {e}") from e
    rows: list[dict] = []
    for item in arr or []:
        if not isinstance(item, dict) or item.get("status") != 0:
            continue
        for r in item.get("hq") or []:
            if not isinstance(r, (list, tuple)) or len(r) < 8:
                continue
            try:
                date = pd.Timestamp(str(r[0]))
            except Exception:  # noqa: BLE001 日期列异常的行跳过
                continue
            rows.append({
                "date": date,
                "open": _f(r[1]), "close": _f(r[2]),
                "change": _f(r[3]), "pct_chg": _f(r[4]),
                "low": _f(r[5]), "high": _f(r[6]),
                "volume": _f(r[7]),                    # 手（原单位）
                "amount": _f(r[8]) * 1e4 if len(r) > 8 else float("nan"),  # 万 -> 元
                "turnover": _f(r[9]) if len(r) > 9 else float("nan"),      # %
            })
    return _finalize(rows, wind_code)


def parse_ths_meta(text: str) -> dict:
    """同花顺 JSONP 响应 meta（symbol/total/start/year 计数；data 之外的字段）。"""
    try:
        obj = json.loads(_jsonp_payload(text))
    except (FetchError, json.JSONDecodeError) as e:
        raise FetchError(f"同花顺响应解析失败: {e}") from e
    return obj if isinstance(obj, dict) else {}


# ============================================================ 拉取器
def _ymd(s) -> str:
    return str(s).replace("-", "")


def _window(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """裁剪到 [start, end]（YYYYMMDD 或 ISO 日期）区间（杀源端回退/越界数据）。"""
    if df is None or len(df) == 0:
        return df
    lo, hi = pd.Timestamp(_ymd(start)), pd.Timestamp(_ymd(end))
    return df[(df["date"] >= lo) & (df["date"] <= hi)].reset_index(drop=True)


def _ths_get(segment: str, wind_code: str, timeout: float | None = None) -> str:
    url = _THS_LINE_URL.format(symbol=ths_symbol(wind_code), segment=segment)
    return _client().get(url, headers={"Referer": THS_REFERER},
                         limiter=ths_limiter, timeout=timeout).decode("utf-8", "ignore")


def fetch_ths_recent_qfq(wind_code: str, timeout: float | None = None) -> pd.DataFrame:
    """同花顺增量：last.js（最近 ~140 根前复权 bar，单次请求）。"""
    df = parse_ths_jsonp(_ths_get("last", wind_code, timeout), wind_code)
    if df.empty:
        raise FetchError(f"同花顺 last.js 无数据 ({wind_code})")
    return df


def fetch_ths_qfq_full(wind_code: str, start: str, end: str,
                       timeout: float | None = None) -> pd.DataFrame:
    """同花顺年度全史（qfq）：{year}.js 逐年拼接 + last.js，按日期去重。

    防御式设计（2026-11 实测样本歧义）：年度文件的 data 有「仅当年 bar」与
    「 IPO 起全史」两种可能解释——降序逐年拉取并 union，一旦覆盖满足
    [max(start, 上市日), end] 即提前停止，两种解释下都得到正确全史。
    末年（end 所在年）文件失败直接上抛；更早年份失败跳过（由调用方守卫兜底）。
    """
    last_text = _ths_get("last", wind_code, timeout)
    meta = parse_ths_meta(last_text)
    frames = [parse_ths_jsonp(last_text, wind_code)]

    ms = str(meta.get("start") or "")
    if len(ms) == 8 and ms.isdigit():
        start_year = int(ms[:4])
    else:
        first = frames[0]["date"].min() if len(frames[0]) else None
        start_year = int(first.year) if first is not None else dt.date.today().year

    s, e = _ymd(start), _ymd(end)
    need_from = max(int(s[:4]), start_year)
    end_year = min(int(e[:4]), dt.date.today().year)
    need_lo = pd.Timestamp(max(s, ms) if len(ms) == 8 else s)
    need_hi = min(pd.Timestamp(e), pd.Timestamp(dt.date.today()))

    def _covered() -> bool:
        bars = pd.concat([f for f in frames if len(f)], ignore_index=True)
        if not len(bars):
            return False
        return bars["date"].min() <= need_lo and bars["date"].max() >= need_hi

    for year in range(end_year, need_from - 1, -1):
        try:
            ydf = parse_ths_jsonp(_ths_get(str(year), wind_code, timeout), wind_code)
        except Exception:  # noqa: BLE001
            if year == end_year:
                raise
            continue  # 早年文件失败跳过（覆盖检查兜底）
        frames.append(ydf)
        if _covered():
            break

    allf = [f for f in frames if len(f)]
    if not allf:
        raise FetchError(f"同花顺全史无数据 ({wind_code})")
    out = pd.concat(allf, ignore_index=True)
    out = out.drop_duplicates(subset=["date"], keep="last")
    out = out.sort_values("date", kind="stable").reset_index(drop=True)
    out = _window(out, s, e)
    if out.empty:
        raise FetchError(f"同花顺全史在 [{s}..{e}] 区间无数据 ({wind_code})")
    return out


def fetch_sina_raw(wind_code: str, start: str, end: str,
                   timeout: float | None = None) -> pd.DataFrame:
    """新浪不复权日线（窗口上限 1023 根 ≈ 4 年，必须带 Referer）。"""
    params = {"symbol": sina_symbol(wind_code), "scale": "240",
              "ma": "no", "datalen": "1023"}
    text = _client().get(_SINA_KLINE_URL, params=params,
                         headers={"Referer": SINA_REFERER},
                         limiter=sina_limiter,
                         timeout=timeout).decode("utf-8", "ignore")
    return _window(parse_sina_jsonp(text, wind_code), start, end)


def fetch_sohu_raw(wind_code: str, start: str, end: str,
                   timeout: float | None = None) -> pd.DataFrame:
    """搜狐不复权日线（任意日期区间，纯 JSON 无需请求头，覆盖北交所）。"""
    params = {"code": sohu_symbol(wind_code), "start": _ymd(start), "end": _ymd(end),
              "stat": "1", "order": "D", "period": "d"}
    text = _client().get(_SOHU_HIS_URL, params=params, limiter=sohu_limiter,
                         timeout=timeout).decode("utf-8", "ignore")
    return _window(parse_sohu_json(text, wind_code), start, end)


# ============================================================ hfq 调度适配器
def _synthesize_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """从 OHLC 合成 pct_chg/change/amplitude（THS/新浪无此列；搜狐有但统一重算）。

    必须在 scale_prices 之后、_window 裁剪之前调用：帧内保留的前一根 bar
    恰是输出窗口首根的前收（锚定重叠段/锚定日 bar），链式合成保证拼接后
    pct_chg 与价格同基准连续（stock_analysis 直接读最新 bar 的 pct_chg，
    NaN 会一路穿透到 AI 分析）。首根（IPO/帧首）无前收 -> NaN，与 em 行为一致。
    """
    if df is None or len(df) == 0:
        return df
    out = df.copy()
    c = out["close"].astype("float64")
    prev = c.shift(1)
    out["change"] = c - prev
    out["pct_chg"] = (c / prev - 1.0) * 100.0
    hi = out["high"].astype("float64")
    lo = out["low"].astype("float64")
    out["amplitude"] = (hi - lo) / prev * 100.0
    return out


def fetch_ths_hfq(wind_code: str, start: str, end: str, old,
                  full: bool = False, anchor_before: "str | None" = None) -> pd.DataFrame:
    """同花顺调度单元: qfq -> 锚定换算 -> 存量基准 hfq。

    - full=True: 年度全史锚定，返回覆盖 [start, end] 的整段序列
      （调用方整文件替换写，修复单位断裂）；
    - 增量: last.js（最近 ~140 根）；与存量重叠不足（长停牌缺口等）时升级
      年度文件锚定——把全史窗口前移到存量末端前 120 天（1~2 个年度文件）
      保证锚定重叠，返回窗口仍裁剪到 [start, end]；
    - 存量为空（新股）不可锚定 -> AnchorError（调度器重派 em/tx）；
    - anchor_before（YYYYMMDD/ISO，修复路径传断裂日）: 比值只用该日之前的
      存量好 bar 计算（断裂日及之后的存量 bar 基准已被污染，混入会撑爆
      CV 门）；full 拉取窗口自动前移 120 天保证锚定重叠，输出仍裁剪到
      [start, end]——merge_daily 只覆盖窗口内日期，断裂日之前的存量历史
      原值保留（2026-11-01 实测：存量多供应商拼接，全史比值漂移 ±2%，
      整段缩放会打歪历史段，必须按断裂日局部锚定）。
    """
    if old is None or len(old) == 0:
        raise AnchorError(f"同花顺锚定需要存量数据（新股 {wind_code} 请走 em/tx）")
    anchor_old = old
    replace_from = None
    if anchor_before is not None:
        cut = pd.Timestamp(_ymd(anchor_before))
        anchor_old = old[pd.to_datetime(old["date"]) < cut].reset_index(drop=True)
        # 断口前收 bar（截尾段末根）最可能是脏 bar：腾讯除权日回 raw 价混入 hfq
        # （2026-11-01 实测 000525：10-27 脏 bar -26.4% 恰好压在 32% 守卫线下、
        # 次日回正常值反被拦）。锚定与替换窗口都再避开一根：
        # 锚定用 [.., 断裂日前2根]，替换从「断裂日前一根」起（脏 bar 一并换掉）。
        if len(anchor_old) > 1:
            anchor_old = anchor_old.iloc[:-1]
        if len(anchor_old) == 0:
            raise AnchorError(f"锚定窗口（<{anchor_before}）内无存量好 bar（{wind_code}）")
        replace_from = (last_date(anchor_old)
                        + pd.Timedelta(days=1)).strftime("%Y%m%d")
    if full:
        fetch_start = _ymd(start)
        if replace_from is not None:
            # 拉取窗口须覆盖锚定段往回一段（≥5 根重叠可算比值）+ 脏 bar 起的替换窗口
            lo120 = (last_date(anchor_old)
                     - pd.Timedelta(days=120)).strftime("%Y%m%d")
            fetch_start = min(fetch_start, lo120, replace_from)
        elif anchor_before is not None:
            lo120 = (pd.Timestamp(_ymd(anchor_before))
                     - pd.Timedelta(days=120)).strftime("%Y%m%d")
            fetch_start = min(fetch_start, lo120)
        qfq = fetch_ths_qfq_full(wind_code, fetch_start, end)
    else:
        lo = _ymd(start)
        try:
            qfq = fetch_ths_recent_qfq(wind_code)
            compute_anchor_ratio(anchor_old, qfq)  # 预检: last.js 是否够得到存量末端
        except InsufficientOverlapError:
            tail = (last_date(anchor_old) - pd.Timedelta(days=120)).strftime("%Y%m%d")
            lo = min(lo, tail)
            qfq = fetch_ths_qfq_full(wind_code, lo, end)
    # 修复路径 CV 门放宽到 0.8%：去尾已避开脏 bar，剩余重叠的供应商慢性微漂
    # （腾讯/THS 复权因子精度差，实测 0.5~0.7%）不是混基准；阶跃型混基准 CV >3%
    # 依然全拦，且最终还有 app 层 32% 守卫把关接缝。
    max_cv = 0.008 if anchor_before is not None else MAX_CV
    ratio, _overlap = compute_anchor_ratio(anchor_old, qfq, max_cv=max_cv)
    out = _synthesize_metrics(scale_prices(qfq, ratio))
    return _window(out, replace_from or start, end)


def _raw_continuation(fetch_raw, wind_code: str, start: str, end: str,
                      old, full: bool) -> pd.DataFrame:
    """raw 尾部续接共用逻辑（新浪/搜狐）: 锚定存量最后一根 bar 的比值。

    窗口内若恰有除权事件，接缝涨跌幅会爆炸，由 app 层单位断裂守卫拦截并
    升级 THS 锚定全量重拉。full=True 抛 SourceUnsupportedError（raw 源无法
    整段重拉复权序列，重派 em/tx/ths）。
    """
    if full:
        raise SourceUnsupportedError(
            f"raw 源仅不复权，无法整段重拉复权序列（{wind_code} 请走 em/tx/ths）")
    if old is None or len(old) == 0:
        raise AnchorError(f"raw 续接需要存量数据（新股 {wind_code} 请走 em/tx）")
    anchor_day = last_date(old)
    raw = fetch_raw(wind_code, anchor_day.strftime("%Y%m%d"), end)
    ratio = compute_tail_ratio(old, raw)
    out = _synthesize_metrics(scale_prices(raw, ratio))
    return _window(out, start, end)


def fetch_sina_hfq_cont(wind_code: str, start: str, end: str, old,
                        full: bool = False) -> pd.DataFrame:
    """新浪调度单元: raw 尾部续接（窗口上限 1023 根，需含存量最后一根 bar）。"""
    return _raw_continuation(fetch_sina_raw, wind_code, start, end, old, full)


def fetch_sohu_hfq_cont(wind_code: str, start: str, end: str, old,
                        full: bool = False) -> pd.DataFrame:
    """搜狐调度单元: raw 尾部续接（任意区间，覆盖北交所）。"""
    return _raw_continuation(fetch_sohu_raw, wind_code, start, end, old, full)


# ============================================================ 可用性探测
#: 探测超时（秒）：每源 1 次轻量请求（交接单约定 5s）
PROBE_TIMEOUT = 5.0


def probe_ths(timeout: float = PROBE_TIMEOUT) -> bool:
    """同花顺探测：深主板 last.js 一次轻量请求。"""
    try:
        return len(fetch_ths_recent_qfq("000001.SZ", timeout=timeout)) > 0
    except Exception:  # noqa: BLE001
        return False


def probe_sina(timeout: float = PROBE_TIMEOUT) -> bool:
    """新浪探测：茅台最近窗口一次请求（必须带 Referer 才通）。"""
    try:
        return len(fetch_sina_raw("600519.SH", "19900101", "20501231",
                                  timeout=timeout)) > 0
    except Exception:  # noqa: BLE001
        return False


def probe_sohu(timeout: float = PROBE_TIMEOUT) -> bool:
    """搜狐探测：茅台小日期区间一次请求。"""
    try:
        return len(fetch_sohu_raw("600519.SH", "20240102", "20240105",
                                  timeout=timeout)) > 0
    except Exception:  # noqa: BLE001
        return False
