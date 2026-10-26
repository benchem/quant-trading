"""用户认证与多用户会话管理（纯标准库，无额外依赖）。

设计：
- 用户与密码：``data/users/users.json``，密码用 scrypt 加盐哈希（N=2^14, r=8, p=1），
  明文不落盘；
- 会话：登录成功签发随机 token（cookie 下发），服务端以 token 的 sha256 为键
  持久化到 ``data/users/sessions.json``（重启后登录态保持），30 天过期，惰性清理；
- 数据隔离：每个用户的自选股/交易流水/站内信存放在独立目录 ``data/users/{name}/``，
  由 DataStore 按用户名分桶访问（见 analyzer.datastore）；
- 平滑迁移：首个注册用户创建时，把旧的根级 ``watchlist.json`` / ``trades.json`` /
  ``notifications.jsonl`` 迁入该用户目录（原文件保留为 ``*.migrated.bak`` 备份，
  由调用方 DataStore.migrate_legacy_to 执行）。

认证关闭（serve --no-auth）时不走本模块，全部请求按「共享」单用户处理，
沿用根级 watchlist.json / trades.json（与升级前行为完全一致）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path

#: 用户名规则：2~32 位字母/数字/_/-
USER_RE = re.compile(r"^[a-zA-Z0-9_-]{2,32}$")
#: 密码长度下限
MIN_PASSWORD = 6
#: 会话有效期（秒）：30 天
SESSION_TTL = 30 * 86400
#: scrypt 参数（N 取 2 的幂）
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1

SESSION_COOKIE = "dsh_session"


# ---------------------------------------------------------------- 密码哈希


def hash_password(password: str) -> str:
    """scrypt 加盐哈希，格式 ``scrypt$N$r$p$salt_hex$hash_hex``。"""
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码与存储的哈希是否一致（常数时间比较）。"""
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        h = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=32,
        )
        return secrets.compare_digest(h.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------- 辅助


def _atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        "w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
    )
    try:
        json.dump(obj, tmp, ensure_ascii=False, indent=1)
        tmp.close()
        os.replace(tmp.name, path)
    except BaseException:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def all_watchlist_codes(data_dir: str | Path) -> list[str]:
    """合并所有用户的自选股（含升级前的根级 watchlist.json），去重保持首现顺序。

    分钟线后台同步与盘后更新需要「全用户并集」视角；直接读文件（小文件、
    低频调用），不依赖 DataStore 实例。
    """
    root = Path(data_dir)
    out: list[str] = []
    seen: set[str] = set()

    def _merge(codes) -> None:
        for c in codes or []:
            c = str(c)
            if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", c) and c not in seen:
                seen.add(c)
                out.append(c)

    legacy = root / "watchlist.json"
    if legacy.exists():
        try:
            _merge(json.loads(legacy.read_text(encoding="utf-8")).get("codes", []))
        except (json.JSONDecodeError, OSError):
            pass
    users_dir = root / "users"
    if users_dir.is_dir():
        for d in sorted(users_dir.iterdir()):
            f = d / "watchlist.json"
            if d.is_dir() and f.is_file():
                try:
                    _merge(json.loads(f.read_text(encoding="utf-8")).get("codes", []))
                except (json.JSONDecodeError, OSError):
                    pass
    return out


# ---------------------------------------------------------------- AuthManager


class AuthManager:
    """用户注册/登录/会话管理（线程安全，用户与会话状态均落盘）。"""

    def __init__(self, data_dir: str | Path = "data"):
        self.users_dir = Path(data_dir) / "users"
        self._users_path = self.users_dir / "users.json"
        self._sessions_path = self.users_dir / "sessions.json"
        self._lock = threading.Lock()
        self._users: dict[str, dict] = self._load_users()
        self._sessions: dict[str, dict] = self._load_sessions()

    # ---------------------------------------------------------- 持久化

    def _load_users(self) -> dict[str, dict]:
        try:
            data = json.loads(self._users_path.read_text(encoding="utf-8"))
            users = data.get("users", {})
            return users if isinstance(users, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _load_sessions(self) -> dict[str, dict]:
        """加载会话（顺手丢弃过期项）。"""
        now = time.time()
        try:
            data = json.loads(self._sessions_path.read_text(encoding="utf-8"))
            sessions = data.get("sessions", {})
            out = {
                k: v for k, v in sessions.items()
                if isinstance(v, dict) and v.get("expires", 0) > now
            }
            if len(out) != len(sessions):
                _atomic_write_json(self._sessions_path, {"sessions": out})
            return out
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save_users(self) -> None:
        _atomic_write_json(self._users_path, {"users": self._users})

    def _save_sessions(self) -> None:
        _atomic_write_json(self._sessions_path, {"sessions": self._sessions})

    # ---------------------------------------------------------- 用户管理

    def users(self) -> list[str]:
        """已注册用户列表（按创建时间排序）。"""
        with self._lock:
            return sorted(self._users, key=lambda n: self._users[n].get("created", ""))

    def user_exists(self, name: str) -> bool:
        with self._lock:
            return name in self._users

    def create_user(self, name: str, password: str) -> dict:
        """注册新用户；返回用户信息。重名/非法名/弱密码抛 ValueError。"""
        name = (name or "").strip()
        if not USER_RE.fullmatch(name):
            raise ValueError("用户名应为 2~32 位字母/数字/_/-")
        if not isinstance(password, str) or len(password) < MIN_PASSWORD:
            raise ValueError(f"密码至少 {MIN_PASSWORD} 位")
        with self._lock:
            if name in self._users:
                raise ValueError(f"用户名 {name} 已被占用")
            rec = {
                "hash": hash_password(password),
                "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._users[name] = rec
            self._save_users()
            return {"name": name, "created": rec["created"]}

    def verify(self, name: str, password: str) -> bool:
        with self._lock:
            rec = self._users.get(name)
        return bool(rec) and verify_password(password or "", rec["hash"])

    def change_password(self, name: str, password: str) -> None:
        """重置密码（网页改密与 CLI user passwd 共用；校验由调用方负责）。"""
        if not isinstance(password, str) or len(password) < MIN_PASSWORD:
            raise ValueError(f"密码至少 {MIN_PASSWORD} 位")
        with self._lock:
            if name not in self._users:
                raise ValueError(f"用户 {name} 不存在")
            self._users[name]["hash"] = hash_password(password)
            self._save_users()

    # ---------------------------------------------------------- 会话

    @staticmethod
    def _session_key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _prune_sessions_locked(self) -> bool:
        now = time.time()
        dead = [k for k, v in self._sessions.items() if v.get("expires", 0) <= now]
        for k in dead:
            del self._sessions[k]
        return bool(dead)

    def login(self, name: str, password: str) -> str | None:
        """校验成功返回新 token，失败返回 None。"""
        if not self.verify(name, password or ""):
            return None
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune_sessions_locked()
            self._sessions[self._session_key(token)] = {
                "user": name,
                "expires": time.time() + SESSION_TTL,
            }
            self._save_sessions()
        return token

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(self._session_key(token), None)
            self._save_sessions()

    def user_of_token(self, token: str | None) -> str | None:
        """token -> 用户名；无效/过期返回 None（过期顺手清理并落盘）。"""
        if not token:
            return None
        key = self._session_key(token)
        with self._lock:
            rec = self._sessions.get(key)
            if rec is None:
                return None
            if rec.get("expires", 0) <= time.time():
                del self._sessions[key]
                self._save_sessions()
                return None
            return rec.get("user")


def all_trade_codes(data_dir: str | Path) -> list[str]:
    """合并所有用户交易流水中涉及的股票代码（含根级 legacy trades.json）。

    供 CLI 工具判断「哪些股票有本地交易记录」，与 all_watchlist_codes 同理直接读文件。
    """
    root = Path(data_dir)
    seen: set[str] = set()

    def _merge(path: Path) -> None:
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for t in data.get("trades", []):
            c = str(t.get("code", "")).upper()
            if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", c):
                seen.add(c)

    _merge(root / "trades.json")
    users_dir = root / "users"
    if users_dir.is_dir():
        for d in sorted(users_dir.iterdir()):
            if d.is_dir():
                _merge(d / "trades.json")
    return sorted(seen)


__all__ = [
    "AuthManager", "SESSION_COOKIE", "SESSION_TTL", "MIN_PASSWORD",
    "hash_password", "verify_password", "all_watchlist_codes", "all_trade_codes",
]
