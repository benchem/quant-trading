"""多引擎 HTTP 客户端：自动选择当前环境可用的引擎。

部分国内行情站点（如东方财富）会对标准库 requests 的 TLS 指纹做过滤
（表现为 "Remote end closed connection without response"），而 curl /
浏览器指纹客户端通常可以正常访问。本模块按以下优先级自动探测并选择引擎：

    requests -> curl_cffi -> tls_client -> curl(系统命令)

运行中途某个引擎失效时，get() 会依次尝试其余引擎。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from typing import Any, Optional
from urllib.parse import urlencode

import requests as _requests

from .errors import CooldownBlocked, FetchError, RateLimitedError  # noqa: F401  (re-export)
from .ratelimit import RateLimiter


class HttpClient:
    ENGINES = ("requests", "curl_cffi", "tls_client", "curl")

    def __init__(self, timeout: float = 30.0, verbose: bool = True, rate: float = 6.0):
        self.timeout = timeout
        self.verbose = verbose
        self.engine: Optional[str] = None
        self.limiter = RateLimiter(rate)
        self._engine_lock = threading.Lock()

    def set_rate(self, rate: float) -> None:
        """全局请求速率上限（次/秒），<=0 表示不限速。"""
        self.limiter = RateLimiter(rate)

    # ------------------------------------------------ 各引擎实现
    def _engine_requests(self, url: str, params: dict) -> bytes:
        r = _requests.get(url, params=params, timeout=self.timeout)
        if r.status_code in RateLimitedError.BLOCK_CODES:
            raise RateLimitedError(r.status_code)
        if r.status_code >= 400:
            raise FetchError(f"HTTP {r.status_code}")
        return r.content

    def _engine_curl_cffi(self, url: str, params: dict) -> bytes:
        from curl_cffi import requests as cr

        last: Optional[Exception] = None
        for imp in ("chrome", "chrome124", "chrome120", "chrome110"):
            try:
                r = cr.get(url, params=params, timeout=self.timeout, impersonate=imp)
                if r.status_code in RateLimitedError.BLOCK_CODES:
                    raise RateLimitedError(r.status_code)
                if r.status_code >= 400:
                    raise FetchError(f"HTTP {r.status_code}")
                return r.content
            except Exception as e:  # noqa: BLE001
                last = e
        raise FetchError(f"curl_cffi 全部指纹失败: {last}")

    def _engine_tls_client(self, url: str, params: dict) -> bytes:
        import tls_client

        last: Optional[Exception] = None
        for ident in ("chrome_124", "chrome_120"):
            sess = None
            try:
                sess = tls_client.Session(client_identifier=ident)
                r = sess.get(url, params=params, timeout=self.timeout)
                if r.status_code in RateLimitedError.BLOCK_CODES:
                    raise RateLimitedError(r.status_code)
                if r.status_code >= 400:
                    raise FetchError(f"HTTP {r.status_code}")
                return r.content
            except Exception as e:  # noqa: BLE001
                last = e
            finally:
                if sess is not None:
                    try:
                        sess.close()
                    except Exception:  # noqa: BLE001
                        pass
        raise FetchError(f"tls_client 全部指纹失败: {last}")

    def _engine_curl(self, url: str, params: dict) -> bytes:
        exe = shutil.which("curl")
        if not exe:
            raise FetchError("系统未找到 curl 命令")
        full = url + ("?" + urlencode(params) if params else "")
        cmd = [exe, "-sS", "--compressed", "-m", str(max(5, int(self.timeout))), full]
        p = subprocess.run(cmd, capture_output=True, timeout=self.timeout + 10)
        if p.returncode != 0 or not p.stdout:
            err = p.stderr.decode("utf-8", "ignore")
            # curl 遇到 4xx/5xx 时退出码 22，错误信息形如 "The requested URL returned error: 501"
            if p.returncode == 22:
                m = re.search(r"returned error:\s*(\d{3})", err)
                code = int(m.group(1)) if m else 0
                if code in RateLimitedError.BLOCK_CODES:
                    raise RateLimitedError(code)
            raise FetchError(f"curl exit={p.returncode}: {err[:120]}")
        return p.stdout

    def _fetch(self, engine: str, url: str, params: dict) -> bytes:
        self.limiter.wait()  # 统一限速：所有源、所有引擎、所有重试
        return getattr(self, f"_engine_{engine}")(url, params)

    # ------------------------------------------------ 引擎探测与选择
    @staticmethod
    def _probe_urls() -> list[tuple[str, dict, bytes]]:
        # 探测目的：验证 HTTP 引擎（TLS 指纹）是否可用。限流针对的是具体主机，
        # 因此多个探测地址任一成功即可确认引擎；全部 403/429/501 则视为限流。
        return [
            ("https://ifzq.gtimg.cn/appstock/app/fqkline/get",
             {"param": "sh600519,day,2024-01-02,2024-01-03,10,"}, b'"code":0'),
            ("https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList",
             {"board_code": "aStock", "sort_type": "price", "direct": "down",
              "offset": "0", "count": "5"}, b'"code":0'),
            ("https://push2his.eastmoney.com/api/qt/stock/kline/get",
             {"secid": "1.600519", "ut": "7eea3edcaed734bea9cbfc24409ed989",
              "klt": "101", "fqt": "0", "beg": "20240102", "end": "20240103",
              "fields1": "f1,f2,f3", "fields2": "f51,f53"}, b'"data"'),
        ]

    def select_engine(self) -> str:
        if self.engine:
            return self.engine
        with self._engine_lock:
            if self.engine:
                return self.engine
            saw_block = False
            for name in self.ENGINES:
                for url, params, marker in self._probe_urls():
                    host = url.split("/")[2]
                    try:
                        body = self._fetch(name, url, params)
                        if marker in body:
                            self.engine = name
                            if self.verbose:
                                print(f"[http] 当前环境使用引擎: {name}")
                            return name
                    except RateLimitedError:
                        saw_block = True
                    except Exception as e:  # noqa: BLE001
                        if self.verbose:
                            print(f"[http] 引擎 {name} 探测 {host} 失败: {str(e)[:80]}")
            if saw_block:
                raise RateLimitedError(429)
            raise FetchError("没有任何可用的 HTTP 引擎（requests / curl_cffi / tls_client / curl 均失败）")

    # ------------------------------------------------ 对外接口
    def get(self, url: str, params: Optional[dict] = None) -> bytes:
        params = params or {}
        first = self.select_engine()
        order = [first] + [e for e in self.ENGINES if e != first]
        last: Optional[Exception] = None
        for name in order:
            try:
                return self._fetch(name, url, params)
            except RateLimitedError:
                # 限流针对 IP，换引擎（同 IP）无意义：立即上抛，交给上层冷却
                raise
            except Exception as e:  # noqa: BLE001
                last = e
        raise FetchError(f"请求失败 {url}: {last}")

    def get_json(self, url: str, params: Optional[dict] = None) -> Any:
        return json.loads(self.get(url, params).decode("utf-8"))
