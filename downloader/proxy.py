"""Clash (mihomo) 节点控制：数据源按 IP 限流/惩罚时，切换出口节点换 IP。

支持的 API 连接方式（自动发现，也可环境变量指定）:
- unix socket: clash-verge 服务模式
  默认路径 /var/run/clash-verge-service/users/<uid>/verge-mihomo.sock
  （也可用 /tmp/verge/verge-mihomo.sock、verge 用户目录下的 socket）
- TCP external-controller: 127.0.0.1:9097 / 9090 等（支持 secret）

环境变量:
- CLASH_API_UNIX  指定 unix socket 路径
- CLASH_API       指定 "host:port[:secret]"
- CLASH_GROUP     指定要切换的 Selector 分组名（默认自动选选项最多的分组）

用法示例:
    python -m downloader proxy            # 查看当前节点/出口IP
    python -m downloader proxy --list     # 列出可选节点
    python -m downloader proxy --set 日本Z01   # 切换节点（子串匹配）
    python -m downloader stocks --rotate  # 限流时自动轮换节点
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import time
from typing import Optional

from .errors import FetchError


class _UnixHTTPConnection(http.client.HTTPConnection):
    """通过 unix socket 的 HTTP 连接（http.client 标准技巧）。"""

    def __init__(self, sock_path: str, timeout: float = 10.0):
        super().__init__("localhost", timeout=timeout)
        self._sock_path = sock_path

    def connect(self) -> None:  # noqa: D102
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._sock_path)
        self.sock = s


class ClashAPI:
    """mihomo RESTful API 客户端（unix socket 或 TCP）。"""

    def __init__(self, unix_socket: Optional[str] = None,
                 host: str = "127.0.0.1", port: int = 9090, secret: str = "",
                 timeout: float = 10.0):
        self.unix_socket = unix_socket
        self.host = host
        self.port = port
        self.secret = secret
        self.timeout = timeout

    # ---------------- 发现
    @classmethod
    def find(cls, verbose: bool = True) -> Optional["ClashAPI"]:
        """自动发现 Clash API；找不到返回 None。"""
        candidates: list[ClashAPI] = []
        env_unix = os.environ.get("CLASH_API_UNIX", "").strip()
        if env_unix:
            candidates.append(cls(unix_socket=env_unix))
        env_api = os.environ.get("CLASH_API", "").strip()
        if env_api:
            parts = env_api.split(":")
            if len(parts) >= 2:
                candidates.append(cls(host=parts[0], port=int(parts[1]),
                                      secret=parts[2] if len(parts) > 2 else ""))
        uid = os.getuid()
        for path in (
            f"/var/run/clash-verge-service/users/{uid}/verge-mihomo.sock",
            "/var/run/verge-mihomo.sock",
            "/tmp/verge/verge-mihomo.sock",
        ):
            candidates.append(cls(unix_socket=path))
        for port in (9097, 9090, 9091, 9099):
            candidates.append(cls(port=port))
        for c in candidates:
            try:
                if c.version():
                    if verbose:
                        loc = f"unix:{c.unix_socket}" if c.unix_socket else f"tcp:{c.host}:{c.port}"
                        print(f"[proxy] Clash API: {loc} ({c.version()})")
                    return c
            except Exception:  # noqa: BLE001
                continue
        return None

    # ---------------- HTTP
    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        if self.unix_socket:
            conn: http.client.HTTPConnection = _UnixHTTPConnection(self.unix_socket, self.timeout)
        else:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        payload = json.dumps(body).encode() if body is not None else None
        try:
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status >= 400:
                raise FetchError(f"Clash API HTTP {resp.status}: {raw[:120]!r}")
            return json.loads(raw) if raw else {}
        finally:
            conn.close()

    # ---------------- 接口
    def version(self) -> str:
        return str(self._request("GET", "/version").get("version", ""))

    def get_proxies(self) -> dict:
        return self._request("GET", "/proxies").get("proxies", {})

    def selectors(self) -> dict:
        return {n: p for n, p in self.get_proxies().items() if p.get("type") == "Selector"}

    def current(self, group: str) -> str:
        p = self.get_proxies().get(group)  # 字典键是原始名称；URL 路径才需编码
        if not p:
            raise FetchError(f"找不到分组: {group}")
        return p.get("now", "")

    def set_node(self, group: str, name: str) -> None:
        self._request("PUT", f"/proxies/{_quote(group)}", {"name": name})

    def egress_ip(self, check_url: str = "https://ifconfig.me/ip") -> str:
        """经本地代理验证当前出口 IP（失败返回空串）。"""
        import requests

        proxies = {
            "http": os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY") or "http://127.0.0.1:7890",
            "https": os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or "http://127.0.0.1:7890",
        }
        try:
            r = requests.get(check_url, proxies=proxies, timeout=self.timeout)
            return r.text.strip()
        except Exception:  # noqa: BLE001
            return ""


def _quote(s: str) -> str:
    return urllib_quote(s)


def urllib_quote(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")


_SKIP_NODES = {"DIRECT", "REJECT", "PASS", "COMPATIBLE", "GLOBAL", "IP-CIRCLE", "Pod"}


class ProxyRotator:
    """限流时轮换 Clash 节点：切到下一个节点并验证出口 IP 确实变化。"""

    def __init__(self, clash: ClashAPI, group: Optional[str] = None, verbose: bool = True):
        self.clash = clash
        self.verbose = verbose
        env_group = os.environ.get("CLASH_GROUP", "").strip()
        if group:
            self.group = group
        elif env_group:
            self.group = env_group
        else:
            sels = clash.selectors()
            sels = {n: p for n, p in sels.items() if n != "GLOBAL"}
            if not sels:
                raise FetchError("Clash 中没有可用的 Selector 分组")
            # 选项最多的分组通常是"选择节点"
            self.group = max(sels, key=lambda n: len(sels[n].get("all", [])))
        self.pool = [
            n for n in clash.selectors()[self.group].get("all", [])
            if n not in _SKIP_NODES
        ]
        if not self.pool:
            raise FetchError(f"分组 {self.group} 中没有可用节点")
        cur = clash.current(self.group)
        self.idx = self.pool.index(cur) if cur in self.pool else -1
        self.last_ip: Optional[str] = None
        self.rounds_used = 0
        if verbose:
            print(f"[proxy] 轮换分组: {self.group}（{len(self.pool)} 个节点，当前 {cur or '-'}）")

    @property
    def rounds_left(self) -> int:
        """每轮限流事件允许一轮完整轮换（不无限循环打转）。"""
        return len(self.pool) - self.rounds_used

    def rotate(self) -> bool:
        """切到下一个节点；出口 IP 确实变化才算成功。"""
        if self.rounds_left <= 0:
            return False
        for _ in range(len(self.pool)):
            self.idx = (self.idx + 1) % len(self.pool)
            name = self.pool[self.idx]
            self.rounds_used += 1
            self.clash.set_node(self.group, name)
            time.sleep(1.0)  # 等新路由/新连接生效
            ip = self.clash.egress_ip()
            if ip and ip != self.last_ip:
                if self.verbose:
                    print(f"[proxy] 已切换节点: {name}（出口 IP {self.last_ip or '-'} -> {ip}）",
                          flush=True)
                self.last_ip = ip
                return True
            if self.verbose:
                print(f"[proxy] 节点 {name} 出口 IP 未变化，继续尝试…", flush=True)
        return False
