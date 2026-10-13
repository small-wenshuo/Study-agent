"""本地账号、持久化登录令牌和请求级身份边界。"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from contextvars import ContextVar
from http.cookies import SimpleCookie
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

LEGACY_ADMIN_ID = 'legacy-admin'
COOKIE_NAME = 'study_agent_session'
SESSION_SECONDS = 7 * 24 * 3600
_identity: ContextVar[str | None] = ContextVar('study_agent_user_id', default=None)


def current_user_id() -> str | None:
    """读取当前 HTTP 请求的身份；服务内部和旧单元测试可没有请求上下文。"""
    return _identity.get()


def owner_for_new_record() -> str:
    """新 HTTP 数据属于已登录用户，迁移前的服务调用归初始管理员。"""
    return _identity.get() or LEGACY_ADMIN_ID


class AuthStore:
    """不依赖模型服务的独立账号库，数据库仅保存口令和令牌的摘要。"""

    def __init__(self, path: Path):
        """初始化账号与会话表；旧业务数据的迁移在各自存储完成。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.db:
            self.db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS login_sessions_user ON login_sessions(user_id);
            ''')
        try:
            path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _password_hash(password: str, salt: bytes | None = None) -> str:
        """使用独立随机盐和有限成本的 PBKDF2，绝不保存明文口令。"""
        salt = salt or secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 600_000)
        return f'pbkdf2_sha256$600000${salt.hex()}${digest.hex()}'

    @staticmethod
    def _verify(password: str, encoded: str) -> bool:
        """比较摘要时使用常数时间比较。"""
        try:
            name, rounds, salt, expected = encoded.split('$')
            if name != 'pbkdf2_sha256' or int(rounds) != 600_000:
                return False
            actual = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), int(rounds))
            return hmac.compare_digest(actual, bytes.fromhex(expected))
        except (ValueError, TypeError):
            return False

    def create_user(self, username: str, password: str, *, admin: bool = False) -> dict:
        """账号仅由服务器命令创建；第一人固定拥有旧数据。"""
        username = username.strip().lower()
        if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,63}', username):
            raise ValueError('账号须为 3～64 位小写字母、数字、点、下划线或连字符。')
        if len(password) < 12 or len(password) > 1024:
            raise ValueError('密码长度须为 12～1024 字符。')
        with self.lock, self.db:
            count = self.db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
            if not count and not admin:
                raise ValueError('先创建初始管理员。')
            if count and admin:
                raise ValueError('初始管理员已存在；新增账号应为普通用户。')
            user_id = LEGACY_ADMIN_ID if admin else str(uuid4())
            self.db.execute('INSERT INTO users(id,username,password_hash,is_admin,created_at) VALUES(?,?,?,?,?)',
                            (user_id, username, self._password_hash(password), int(admin), int(time.time())))
        return {'id': user_id, 'username': username, 'is_admin': admin}

    def authenticate(self, username: str, password: str) -> tuple[dict, str] | None:
        """失败次数持久化限流，成功后签发随机令牌且只保存摘要。"""
        username = username.strip().lower()
        now = int(time.time())
        with self.lock, self.db:
            row = self.db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
            if row is None or row['locked_until'] > now:
                return None
            if not self._verify(password, row['password_hash']):
                attempts = row['failed_attempts'] + 1
                self.db.execute('UPDATE users SET failed_attempts=?,locked_until=? WHERE id=?',
                                (attempts, now + 900 if attempts >= 5 else 0, row['id']))
                return None
            self.db.execute('UPDATE users SET failed_attempts=0,locked_until=0 WHERE id=?', (row['id'],))
            token = secrets.token_urlsafe(32)
            self.db.execute('INSERT INTO login_sessions VALUES(?,?,?,?)',
                            (hashlib.sha256(token.encode()).hexdigest(), row['id'], now + SESSION_SECONDS, now))
            self.db.execute('DELETE FROM login_sessions WHERE expires_at<=?', (now,))
            return {'id': row['id'], 'username': row['username'], 'is_admin': bool(row['is_admin'])}, token

    def user_for_token(self, token: str | None) -> dict | None:
        """会话可吊销、可过期；未知令牌不能推断任何用户信息。"""
        if not token or len(token) > 200:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            row = self.db.execute('SELECT u.id,u.username,u.is_admin FROM login_sessions s '
                                  'JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?',
                                  (digest, int(time.time()))).fetchone()
        return {'id': row['id'], 'username': row['username'], 'is_admin': bool(row['is_admin'])} if row else None

    def revoke(self, token: str | None):
        """退出时立即使当前浏览器的服务器端登录失效。"""
        if token:
            with self.lock, self.db:
                self.db.execute('DELETE FROM login_sessions WHERE token_hash=?',
                                (hashlib.sha256(token.encode()).hexdigest(),))

    def reset_password(self, username: str, password: str):
        """管理员在服务器终端重置密码，并吊销此账号全部旧设备会话。"""
        if len(password) < 12 or len(password) > 1024:
            raise ValueError('密码长度须为 12～1024 字符。')
        with self.lock, self.db:
            row = self.db.execute('SELECT id FROM users WHERE username=?', (username.strip().lower(),)).fetchone()
            if row is None:
                raise KeyError('账号不存在。')
            self.db.execute('UPDATE users SET password_hash=?,failed_attempts=0,locked_until=0 WHERE id=?',
                            (self._password_hash(password), row['id']))
            self.db.execute('DELETE FROM login_sessions WHERE user_id=?', (row['id'],))

    def close(self):
        """释放登录数据库连接。"""
        self.db.close()


def cookie_token(scope) -> str | None:
    """只读取指定 Cookie；忽略客户端伪造的用户 ID 请求头。"""
    headers = dict(scope.get('headers', []))
    try:
        cookies = SimpleCookie(headers.get(b'cookie', b'').decode('latin-1'))
        return cookies[COOKIE_NAME].value if COOKIE_NAME in cookies else None
    except Exception:
        return None


class AuthMiddleware:
    """纯 ASGI 边界覆盖普通响应与 SSE 的整个执行周期。"""

    def __init__(self, app, store_getter):
        """延迟取得存储，便于隔离测试并避免在导入时固定实例。"""
        self.app, self.store_getter = app, store_getter

    async def __call__(self, scope, receive, send):
        """所有私有 API 必须登录；跨来源带 Cookie 的修改请求被拒绝。"""
        if scope['type'] != 'http' or not scope.get('path', '').startswith('/api/') or scope['path'] in {
            '/api/health', '/api/auth/login'}:
            await self.app(scope, receive, send)
            return
        token = cookie_token(scope)
        user = self.store_getter().user_for_token(token)
        if user is None:
            await JSONResponse({'detail': '请先登录。'}, status_code=401, headers={'Cache-Control': 'no-store'})(scope, receive, send)
            return
        if scope.get('method') not in {'GET', 'HEAD', 'OPTIONS'}:
            headers = dict(scope.get('headers', []))
            origin = headers.get(b'origin', b'').decode('latin-1')
            host = headers.get(b'host', b'').decode('latin-1')
            parsed_origin = urlsplit(origin)
            if origin and (parsed_origin.scheme != scope.get('scheme') or parsed_origin.netloc.lower() != host.lower()):
                await JSONResponse({'detail': '请求来源不匹配。'}, status_code=403)(scope, receive, send)
                return
        scope.setdefault('state', {})['auth_user'] = user
        marker = _identity.set(user['id'])
        closed = False
        streaming = False
        async def secure_send(message):
            """私人响应不缓存；已吊销令牌的无定长流不再传出新数据。"""
            nonlocal closed, streaming
            if closed:
                return
            if message['type'] == 'http.response.start':
                streaming = not any(key.lower() == b'content-length' for key, _ in message.get('headers', []))
                message = {**message, 'headers': [*(item for item in message.get('headers', [])
                              if item[0].lower() not in {b'cache-control', b'vary'}),
                              (b'cache-control', b'no-store'), (b'vary', b'Cookie'),
                              (b'x-content-type-options', b'nosniff')]}
            if message['type'] == 'http.response.body' and streaming and self.store_getter().user_for_token(token) is None:
                closed = True
                message = {**message, 'body': b'', 'more_body': False}
            await send(message)
        try:
            await self.app(scope, receive, secure_send)
        finally:
            _identity.reset(marker)
