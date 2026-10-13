"""现有 HTTP 回归使用的显式管理员登录测试夹具。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.auth import AuthStore

PASSWORD = 'test-only-long-password-1234'


def install_auth(main_module, root: Path):
    """使用临时账号库覆盖全局服务，测试不读取生产账号。"""
    store = AuthStore(root / 'auth.db')
    store.create_user('testadmin', PASSWORD, admin=True)
    patcher = patch.object(main_module, 'auth_store', store)
    patcher.start()
    return store, patcher


async def login_admin(client):
    """真实调用登录路由，令 Cookie 由 HTTP 客户端管理。"""
    response = await client.post('/api/auth/login', json={'username': 'testadmin', 'password': PASSWORD})
    assert response.status_code == 200, response.text
