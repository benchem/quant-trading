"""多用户登录与数据隔离自测（无需网络）。

运行: .venv/bin/python tests/test_auth.py

覆盖：
- AuthManager：scrypt 密码哈希校验 / 注册校验 / 登录会话（签发、失效、
  登出、过期、重启持久化）/ 改密
- DataStore 分桶：两个用户自选股与交易流水互不可见、文件落 data/users/{user}/、
  共享桶(user=None) 用根级文件、全部用户自选并集、legacy 数据首用户迁移
- NotificationHub：系统消息广播全部用户；带 code 信号只推给自选含该股的用户
- HTTP 级完整流程（真实 ThreadingHTTPServer + Cookie）：
  未登录 401 -> 注册(首用户继承 legacy) -> 登录/登出 -> 两用户自选/交易/站内信隔离
"""
from __future__ import annotations

import http.cookiejar
import json
import sys
import tempfile
import threading
import traceback
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pandas as pd

sys.path.insert(0, ".")

from analyzer.auth import (  # noqa: E402
    AuthManager, all_watchlist_codes, hash_password, verify_password,
)
from analyzer.datastore import DataStore  # noqa: E402
from analyzer.notify import NotificationHub  # noqa: E402
from analyzer.server import make_handler  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, fn) -> bool:
    global PASSED, FAILED
    print(f"\n===== {name} =====", flush=True)
    try:
        fn()
        print("PASS")
        PASSED += 1
        return True
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        FAILED += 1
        return False


# ---------------------------------------------------------------- fixtures


def _make_store(tmp: Path) -> DataStore:
    """最小可用 data 目录：两只股票的日线 + 基础信息表。"""
    (tmp / "daily" / "hfq").mkdir(parents=True, exist_ok=True)
    for code, name in (("600000.SH", "浦发银行"), ("000001.SZ", "平安银行")):
        pd.DataFrame({
            "date": pd.to_datetime(["2026-10-20", "2026-10-21", "2026-10-22"]),
            "code": [code] * 3,
            "open": [10.0, 10.1, 10.2],
            "high": [10.3, 10.4, 10.5],
            "low": [9.9, 10.0, 10.1],
            "close": [10.2, 10.3, 10.4],
            "volume": [1000, 1100, 1200],
            "amount": [1e7, 1.1e7, 1.2e7],
        }).to_parquet(tmp / "daily" / "hfq" / f"{code}.parquet", index=False)
    pd.DataFrame({
        "code": ["600000.SH", "000001.SZ"],
        "name": ["浦发银行", "平安银行"],
        "latest_price": [10.4, 12.0],
        "pct_chg": [0.97, -0.5],
        "total_mv": [1e10, 2e11],
    }).to_parquet(tmp / "stock_list.parquet", index=False)
    pd.DataFrame(
        {"cal_date": pd.to_datetime(["2026-10-20", "2026-10-21", "2026-10-22"])}
    ).to_parquet(tmp / "trade_calendar.parquet", index=False)
    return DataStore(tmp)


def _legacy_files(tmp: Path) -> None:
    """写入升级前的根级 legacy 数据（自选 + 一笔交易 + 一条站内信）。"""
    (tmp / "watchlist.json").write_text(
        json.dumps({"codes": ["600000.SH"]}, ensure_ascii=False), encoding="utf-8")
    (tmp / "trades.json").write_text(
        json.dumps({"trades": [{
            "id": "legacy-1", "code": "600000.SH", "date": "2026-10-20",
            "side": "buy", "price": 10.0, "shares": 100, "note": "旧数据",
            "created": "2026-10-20 10:00:00",
        }]}, ensure_ascii=False), encoding="utf-8")
    (tmp / "notifications.jsonl").write_text(
        json.dumps({
            "id": "legacy-msg", "key": "k", "code": None, "level": "info",
            "source": "system", "text": "旧站内信", "time": "2026-10-20T10:00:00+00:00",
            "day": "2026-10-20", "read": False,
        }, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- AuthManager


def t_password_hash():
    h = hash_password("p@ssw0rd")
    assert h.startswith("scrypt$")
    assert verify_password("p@ssw0rd", h)
    assert not verify_password("p@ssw0rd!", h)
    assert not verify_password("p@ssw0rd", "garbage")
    assert hash_password("x") != hash_password("x")  # 盐随机


def t_user_crud():
    with tempfile.TemporaryDirectory() as td:
        am = AuthManager(td)
        assert am.users() == []
        for bad_name in ("a", "x" * 33, "bad name", "中文用户"):
            try:
                am.create_user(bad_name, "secret123")
                raise AssertionError(f"应拒绝用户名: {bad_name!r}")
            except ValueError:
                pass
        try:
            am.create_user("alice", "123")
            raise AssertionError("应拒绝短密码")
        except ValueError:
            pass
        am.create_user("alice", "secret123")
        try:
            am.create_user("alice", "another99")
            raise AssertionError("应拒绝重名")
        except ValueError:
            pass
        assert am.users() == ["alice"]
        assert am.verify("alice", "secret123")
        assert not am.verify("alice", "wrong")
        assert not am.verify("bob", "secret123")
        am.change_password("alice", "newpass456")
        assert not am.verify("alice", "secret123")
        assert am.verify("alice", "newpass456")
        try:
            am.change_password("ghost", "whatever1")
            raise AssertionError("应拒绝不存在的用户")
        except ValueError:
            pass
        # 重启持久化
        am2 = AuthManager(td)
        assert am2.users() == ["alice"]
        assert am2.verify("alice", "newpass456")


def t_sessions():
    with tempfile.TemporaryDirectory() as td:
        am = AuthManager(td)
        am.create_user("alice", "secret123")

        assert am.login("alice", "wrong") is None
        token = am.login("alice", "secret123")
        assert token and am.user_of_token(token) == "alice"
        assert am.user_of_token("bogus-token") is None
        assert am.user_of_token(None) is None

        # 登出后失效
        am.logout(token)
        assert am.user_of_token(token) is None

        # 重启后会话保持
        token2 = am.login("alice", "secret123")
        am2 = AuthManager(td)
        assert am2.user_of_token(token2) == "alice"

        # 过期会话拒绝
        am2._sessions[am2._session_key(token2)]["expires"] = 1.0
        assert am2.user_of_token(token2) is None
        # 过期项已被清理落盘
        am3 = AuthManager(td)
        assert am3.user_of_token(token2) is None


# ------------------------------------------------------- DataStore 分桶隔离


def t_store_isolation():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _make_store(td)
        store = DataStore(td)

        # 两用户各自加自选 / 交易，互不可见
        store.watchlist_add("alice", "600000.SH")
        store.watchlist_add("bob", "000001.SZ")
        store.trade_add("alice", "600000.SH", "2026-10-20", "buy", 10.0, 100)
        store.trade_add("bob", "000001.SZ", "2026-10-21", "sell", 12.0, 50)

        assert store.watchlist_codes("alice") == ["600000.SH"]
        assert store.watchlist_codes("bob") == ["000001.SZ"]
        assert store.trades("alice")[0]["code"] == "600000.SH"
        assert store.trades("bob")[0]["code"] == "000001.SZ"
        assert len(store.trades("alice")) == 1 and len(store.trades("bob")) == 1

        # 文件落独立目录
        assert (td / "users" / "alice" / "watchlist.json").is_file()
        assert (td / "users" / "bob" / "trades.json").is_file()
        assert not (td / "watchlist.json").exists()

        # 全部用户自选并集（分钟线/盘后更新视角）
        assert store.watchlist_codes_all() == ["600000.SH", "000001.SZ"]
        assert all_watchlist_codes(td) == ["600000.SH", "000001.SZ"]

        # 新实例读回（持久化隔离）
        store2 = DataStore(td)
        assert store2.watchlist_codes("alice") == ["600000.SH"]
        assert store2.watchlist_codes("bob") == ["000001.SZ"]
        assert len(store2.trades("alice")) == 1

        # bob 删不掉 alice 的交易（code+id 双限定）
        aid = store.trades("alice")[0]["id"]
        _, removed = store.trade_remove("bob", "600000.SH", aid)
        assert not removed
        assert len(store.trades("alice")) == 1

        # 持仓按用户汇总：alice 只看到自己的 600000.SH，bob 看不到
        pos_a = store.positions("alice")
        pos_b = store.positions("bob")
        assert "600000.SH" in pos_a and "000001.SZ" not in pos_a
        assert "000001.SZ" in pos_b and "600000.SH" not in pos_b


def t_store_shared_bucket():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _make_store(td)
        store = DataStore(td)
        # user=None：共享桶，沿用根级文件
        store.watchlist_add(None, "600000.SH")
        assert (td / "watchlist.json").is_file()
        assert not (td / "users").exists()
        assert store.watchlist_codes(None) == ["600000.SH"]
        # 共享桶与用户桶互不影响
        store.watchlist_add("alice", "000001.SZ")
        assert store.watchlist_codes(None) == ["600000.SH"]
        assert store.watchlist_codes("alice") == ["000001.SZ"]
        assert store.watchlist_codes_all() == ["600000.SH", "000001.SZ"]


def t_store_legacy_migration():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _make_store(td)
        _legacy_files(td)
        store = DataStore(td)
        # 迁移前：根级文件可读（共享桶视角）
        assert store.watchlist_codes(None) == ["600000.SH"]

        moved = store.migrate_legacy_to("alice")
        assert moved == ["watchlist.json", "trades.json", "notifications.jsonl"]
        assert not (td / "watchlist.json").exists()
        assert not (td / "trades.json").exists()
        assert (td / "users" / "alice" / "watchlist.json").is_file()
        assert (td / "users" / "alice" / "trades.json").is_file()
        assert (td / "users" / "alice" / "notifications.jsonl").is_file()

        # 迁入后 alice 看到 legacy 数据；共享桶不再可见
        assert store.watchlist_codes("alice") == ["600000.SH"]
        assert store.trades("alice")[0]["id"] == "legacy-1"
        assert store.watchlist_codes(None) == []
        # 幂等：无 legacy 文件时不再迁移
        assert store.migrate_legacy_to("alice") == []


# ------------------------------------------------------- NotificationHub


def t_notification_hub():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _make_store(td)
        store = DataStore(td)
        store.watchlist_add("alice", "600000.SH")
        store.watchlist_add("bob", "000001.SZ")
        hub = NotificationHub(
            td,
            users_provider=lambda: ["alice", "bob"],
            watchlist_provider=store.watchlist_codes,
        )
        # 系统消息（无 code）广播全部用户
        hub.push("sys-1", "更新完成", level="ok")
        assert len(hub.center("alice").list()["messages"]) == 1
        assert len(hub.center("bob").list()["messages"]) == 1
        # 信号（code=600000.SH）只推给自选含该股的 alice
        hub.push("sig-1", "600000 触发", level="alert", code="600000.SH")
        alice_msgs = [m["text"] for m in hub.center("alice").list()["messages"]]
        bob_msgs = [m["text"] for m in hub.center("bob").list()["messages"]]
        assert "600000 触发" in alice_msgs
        assert "600000 触发" not in bob_msgs
        # 文件落各自目录
        assert (td / "users" / "alice" / "notifications.jsonl").is_file()
        assert (td / "users" / "bob" / "notifications.jsonl").is_file()
        # 未读标记按用户隔离（bob 只收到 1 条广播，未读到信号）
        n = hub.mark_read("alice", all=True)
        assert n == 2 and hub.center("alice").list()["unread"] == 0
        assert hub.center("bob").list()["unread"] == 1


# ---------------------------------------------------------------- HTTP 流程


def _http_client(port: int):
    """返回 call(path, body=None) -> (status, json)，独立 CookieJar 模拟一个浏览器。"""
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    base = f"http://127.0.0.1:{port}"

    def call(path, body=None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(base + path, data=data)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            r = opener.open(req, timeout=10)
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")

    return call


def t_http_full_flow():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _make_store(td)
        _legacy_files(td)
        store = DataStore(td)
        auth = AuthManager(td)
        hub = NotificationHub(td, users_provider=auth.users,
                              watchlist_provider=store.watchlist_codes)
        handler = make_handler(store, runner=None, syncer=None, notifier=hub,
                               updater=None, auth=auth)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            alice = _http_client(port)
            bob = _http_client(port)

            # 状态端点公开：未启用用户、未登录
            st, j = alice("/api/auth/status")
            assert st == 200 and j == {"auth": True, "user": None, "n_users": 0}, j

            # 未登录访问业务 API -> 401
            st, j = alice("/api/watchlist")
            assert st == 401 and j.get("auth") is True, (st, j)
            st, j = alice("/api/trades")
            assert st == 401

            # 注册 alice（首用户，触发 legacy 迁移）
            st, j = alice("/api/auth/register",
                          {"username": "alice", "password": "secret123"})
            assert st == 200 and j["user"] == "alice" and j["first"] is True, j
            assert not (td / "watchlist.json").exists()
            assert (td / "users" / "alice" / "watchlist.json").exists()

            # alice 已登录，看到继承的 legacy 自选与交易
            st, j = alice("/api/auth/status")
            assert j["user"] == "alice"
            st, j = alice("/api/watchlist")
            assert st == 200 and [c["code"] for c in j["codes"]] == ["600000.SH"]
            st, j = alice("/api/trades")
            assert st == 200 and [t["id"] for t in j["trades"]] == ["legacy-1"]

            # alice 再加一只自选 + 一笔交易
            st, j = alice("/api/watchlist", {"action": "add", "code": "000001.SZ"})
            assert st == 200 and len(j["codes"]) == 2
            st, j = alice("/api/trades", {"action": "add", "code": "600000.SH",
                                          "date": "2026-10-21", "side": "buy",
                                          "price": 10.2, "shares": 100})
            assert st == 200 and j["added"]["code"] == "600000.SH"

            # 注册 bob：全新空白数据
            st, j = bob("/api/auth/register",
                        {"username": "bob", "password": "secret456"})
            assert st == 200 and j["first"] is False
            st, j = bob("/api/watchlist")
            assert st == 200 and j["codes"] == []
            st, j = bob("/api/trades")
            assert st == 200 and j["trades"] == []
            # alice 的数据不受 bob 注册影响
            st, j = alice("/api/watchlist")
            assert len(j["codes"]) == 2 and len(alice("/api/trades")[1]["trades"]) == 2

            # 错误密码登录 -> 401
            st, j = bob("/api/auth/login", {"username": "alice", "password": "nope"})
            assert st == 401

            # 站内信隔离：信号 code=000001.SZ（alice 自选、bob 未选）
            hub.push("sig-x", "触发测试信号", level="alert", code="000001.SZ")
            st, j = alice("/api/notifications")
            assert any(m["text"] == "触发测试信号" for m in j["messages"])
            st, j = bob("/api/notifications")
            assert not any(m["text"] == "触发测试信号" for m in j["messages"])
            # 系统消息广播：bob 也能看到
            hub.push("sys-x", "系统广播", level="ok")
            st, j = bob("/api/notifications")
            assert any(m["text"] == "系统广播" for m in j["messages"])

            # 登出后 cookie 失效 -> 401；重新登录恢复
            st, j = alice("/api/auth/logout", {})
            assert st == 200
            st, j = alice("/api/watchlist")
            assert st == 401
            st, j = alice("/api/auth/login", {"username": "alice", "password": "secret123"})
            assert st == 200
            st, j = alice("/api/watchlist")
            assert st == 200 and len(j["codes"]) == 2

            # 无 cookie 的静态页面仍可访问（登录界面在 index.html 内）
            import urllib.request as _ur
            r = _ur.urlopen(f"http://127.0.0.1:{port}/", timeout=10)
            html = r.read().decode("utf-8")
            assert "auth-overlay" in html
        finally:
            httpd.shutdown()
            httpd.server_close()


def t_http_shared_mode():
    """--no-auth 共享模式：无登录，业务 API 直接可用（旧行为兼容）。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _make_store(td)
        store = DataStore(td)
        handler = make_handler(store, runner=None, syncer=None, notifier=None,
                               updater=None, auth=None)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            call = _http_client(port)
            st, j = call("/api/auth/status")
            assert st == 200 and j["auth"] is False
            st, j = call("/api/watchlist")
            assert st == 200 and j["codes"] == []
            st, j = call("/api/watchlist", {"action": "add", "code": "600000.SH"})
            assert st == 200 and len(j["codes"]) == 1
            assert (td / "watchlist.json").is_file()
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    check("密码哈希 scrypt 往返/拒绝", t_password_hash)
    check("用户注册/校验/改密/持久化", t_user_crud)
    check("会话签发/登出/重启保持/过期清理", t_sessions)
    check("DataStore 两用户自选+交易隔离", t_store_isolation)
    check("DataStore 共享桶(None) 根级文件", t_store_shared_bucket)
    check("legacy 数据首用户迁移", t_store_legacy_migration)
    check("NotificationHub 广播/按自选归属/未读隔离", t_notification_hub)
    check("HTTP 全流程：登录/注册/登出/数据隔离", t_http_full_flow)
    check("HTTP 共享模式（--no-auth）兼容", t_http_shared_mode)
    print(f"\n{PASSED} 通过 / {FAILED} 失败")
    sys.exit(1 if FAILED else 0)
