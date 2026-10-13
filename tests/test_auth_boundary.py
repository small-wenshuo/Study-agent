"""公开网站的身份与跨用户隔离回归。"""
from __future__ import annotations

import unittest
import tempfile
import asyncio
from pathlib import Path
from unittest.mock import patch
import httpx

import app.main as main
from app.main import app
from app.auth import AuthStore, _identity
from app.services.sessions import SessionStore
from app.services.memory import MemoryIndex
from app.knowledge_base import KnowledgeBaseService, ChromaVectorStore
from app.parsers import ParserRegistry
from app.papers.service import PaperService
from app.config import Settings
from app.services.tasks import TERMINAL
from tests.test_v2_api import ApiEmbeddings
from tests.test_paper_learning import TestModel, pdf_bytes


class AnonymousBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """匿名访客连历史列表的存在性都不应取得。"""

    async def test_anonymous_cannot_read_or_create_private_data(self):
        """复现任何打开网站的访客都能看到聊天记录的漏洞。"""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for method, path in [('GET', '/api/sessions'), ('POST', '/api/sessions'),
                                 ('GET', '/api/knowledge-bases'), ('GET', '/api/memories')]:
                response = await client.request(method, path)
                self.assertEqual(response.status_code, 401, (method, path, response.text[:100]))


class AccountIsolationTests(unittest.IsolatedAsyncioTestCase):
    """真实 HTTP Cookie、SQLite、Chroma 与论文任务同时验证两个账号。"""

    async def asyncSetUp(self):
        """在独立临时目录中创建初始管理员、普通用户及旧数据。"""
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.auth = AuthStore(root / 'auth.db')
        self.auth.create_user('admin', 'admin-test-password-1234', admin=True)
        self.auth.create_user('alice', 'alice-test-password-1234')
        self.sessions = SessionStore(root / 'sessions.db')
        self.legacy_session = self.sessions.create()
        await self.sessions.save_memory('旧管理员资料')
        self.kb = KnowledgeBaseService(root / 'knowledge.db', root / 'uploads', ParserRegistry.default(),
                                       ApiEmbeddings(), ChromaVectorStore(root / 'vectors'))
        self.legacy_kb = self.kb.create_knowledge_base('旧知识库')
        self.paper = PaperService(Settings.from_env(), root, lambda: self.sessions, lambda: self.kb, TestModel())
        self.patches = [patch.object(main, name, value) for name, value in
                        [('auth_store', self.auth), ('sessions', self.sessions),
                         ('knowledge_bases', self.kb), ('papers', self.paper)]]
        for item in self.patches:
            item.start()
        self.admin = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://test')
        self.alice = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://test')
        assert (await self.admin.post('/api/auth/login', json={'username':'admin','password':'admin-test-password-1234'})).status_code == 200
        assert (await self.alice.post('/api/auth/login', json={'username':'alice','password':'alice-test-password-1234'})).status_code == 200

    async def asyncTearDown(self):
        """等待任务释放，再关闭文件与临时数据库。"""
        await self.admin.aclose()
        await self.alice.aclose()
        await self.paper.close()
        for item in reversed(self.patches):
            item.stop()
        self.auth.close()
        self.sessions.close()
        self.kb.close()
        self.temp.cleanup()

    async def test_sessions_knowledge_memories_and_ids_are_private(self):
        """列表与已知 ID 两条路径都不得跨用户读写。"""
        admin_session = (await self.admin.post('/api/sessions')).json()['session_id']
        alice_session = (await self.alice.post('/api/sessions')).json()['session_id']
        own_kb = (await self.alice.post('/api/knowledge-bases', json={'name':'Alice 的库'})).json()['id']
        admin_memory = (await self.admin.post('/api/memories', json={'content':'管理员新资料'})).json()['id']
        self.assertEqual({s['id'] for s in (await self.alice.get('/api/sessions')).json()}, {alice_session})
        self.assertIn(self.legacy_session, {s['id'] for s in (await self.admin.get('/api/sessions')).json()})
        self.assertEqual({k['id'] for k in (await self.alice.get('/api/knowledge-bases')).json()}, {own_kb})
        self.assertIn(self.legacy_kb['id'], {k['id'] for k in (await self.admin.get('/api/knowledge-bases')).json()})
        self.assertEqual((await self.alice.get('/api/memories')).json()['items'], [])
        for method, path, payload in [
            ('GET', f'/api/sessions/{admin_session}', None),
            ('POST', '/api/chat', {'session_id':admin_session,'message':'读取旧会话'}),
            ('GET', f"/api/knowledge-bases/{self.legacy_kb['id']}/files", None),
            ('DELETE', f"/api/knowledge-bases/{self.legacy_kb['id']}", None),
            ('PUT', f'/api/memories/{admin_memory}', {'content':'跨用户修改'}),
            ('DELETE', f'/api/memories/{admin_memory}', None),
            ('POST', '/api/chat', {'session_id':alice_session,'message':'访问别人的库',
                                   'knowledge_base_id':self.legacy_kb['id']}),
        ]:
            response = await self.alice.request(method, path, json=payload)
            self.assertEqual(response.status_code, 404, (method, path, response.text[:100]))
        self.assertEqual((await self.admin.get(f'/api/sessions/{admin_session}')).status_code, 200)
        self.assertEqual((await self.alice.get(f'/api/sessions/{alice_session}')).status_code, 200)

    async def test_paper_tasks_notes_and_sources_inherit_knowledge_owner(self):
        """论文详情、任务 SSE、笔记和来源下载均沿用知识库归属。"""
        kb = (await self.admin.post('/api/knowledge-bases', json={'name':'论文库'})).json()['id']
        uploaded = await self.admin.post(f'/api/knowledge-bases/{kb}/files',
                                         files={'file':('paper.pdf', pdf_bytes(), 'application/pdf')})
        self.assertEqual(uploaded.status_code, 201, uploaded.text[:100])
        file_id = uploaded.json()['id']
        study = (await self.admin.post('/api/paper-studies', json={'knowledge_base_id':kb,'file_id':file_id})).json()['id']
        session = (await self.admin.post('/api/sessions')).json()['session_id']
        mode = await self.admin.patch(f'/api/sessions/{session}/paper-mode',
                                      json={'enabled':True,'study_id':study,'expected_revision':0})
        self.assertEqual(mode.status_code, 200, mode.text[:100])
        task_id = mode.json()['task_id']
        async with asyncio.timeout(10):
            while self.paper.store.task(task_id)['status'] not in TERMINAL:
                await asyncio.sleep(.01)
        task = self.paper.store.task(task_id)
        self.assertEqual(task['status'], 'complete', task)
        artifact_id = task['result_id']
        note_id = (await self.admin.post(f'/api/paper-studies/{study}/notes',
                                          json={'title':'个人笔记','artifact_ids':[artifact_id]})).json()['id']
        for path in [f'/api/paper-studies/{study}',f'/api/tasks/{task_id}',
                     f'/api/tasks/{task_id}/events',f'/api/tasks/{task_id}/stream',
                     f'/api/artifacts/{artifact_id}',f'/api/notes/{note_id}',
                     f'/api/notes/{note_id}/export',f'/api/artifacts/{artifact_id}/download']:
            response = await self.alice.get(path)
            self.assertEqual(response.status_code, 404, (path, response.text[:100]))
        self.assertEqual((await self.alice.post('/api/paper-studies',
                         json={'knowledge_base_id':kb,'file_id':file_id})).status_code, 404)

    async def test_cookie_logout_origin_and_public_http(self):
        """服务器端吊销 Cookie，跨来源写入与明文公网登录均被拒绝。"""
        response = await self.alice.post('/api/sessions', headers={'Origin':'https://other.example'})
        self.assertEqual(response.status_code, 403)
        logout = await self.alice.post('/api/auth/logout')
        self.assertEqual(logout.status_code, 200)
        self.assertEqual(logout.json(), {'ok': True})
        self.assertEqual((await self.alice.get('/api/sessions')).status_code, 401)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://public.example') as plain:
            response = await plain.post('/api/auth/login', json={'username':'admin','password':'admin-test-password-1234'})
            self.assertEqual(response.status_code, 403)
        # Host 是客户端可伪造的；只有真实 loopback 来源才允许开发机明文登录。
        spoofed = httpx.ASGITransport(app=app, client=('203.0.113.10', 12345))
        async with httpx.AsyncClient(transport=spoofed, base_url='http://localhost') as plain:
            response = await plain.post('/api/auth/login', json={'username':'admin','password':'admin-test-password-1234'})
            self.assertEqual(response.status_code, 403)


class AccountLifecycleTests(unittest.TestCase):
    """验证初始管理员、密码重置与令牌吊销。"""

    def test_first_admin_owns_legacy_identity_and_reset_revokes_sessions(self):
        """旧数据身份固定，重置密码后旧浏览器不能继续访问。"""
        with tempfile.TemporaryDirectory() as directory:
            store = AuthStore(Path(directory) / 'auth.db')
            try:
                with self.assertRaises(ValueError):
                    store.create_user('alice', 'alice-test-password-1234')
                admin = store.create_user('admin', 'admin-test-password-1234', admin=True)
                self.assertEqual(admin['id'], 'legacy-admin')
                store.create_user('alice', 'alice-test-password-1234')
                _, old_token = store.authenticate('alice', 'alice-test-password-1234')
                store.reset_password('alice', 'new-alice-password-1234')
                self.assertIsNone(store.user_for_token(old_token))
                self.assertIsNone(store.authenticate('alice', 'alice-test-password-1234'))
                self.assertIsNotNone(store.authenticate('alice', 'new-alice-password-1234'))
            finally:
                store.close()

    def test_bad_passwords_lock_account_temporarily(self):
        """同一账号连续失败后拒绝正确口令，防止无限在线猜测。"""
        with tempfile.TemporaryDirectory() as directory:
            store = AuthStore(Path(directory) / 'auth.db')
            try:
                store.create_user('admin', 'admin-test-password-1234', admin=True)
                for _ in range(5):
                    self.assertIsNone(store.authenticate('admin', 'wrong-password'))
                self.assertIsNone(store.authenticate('admin', 'admin-test-password-1234'))
            finally:
                store.close()


class MemoryVectorIsolationTests(unittest.IsolatedAsyncioTestCase):
    """新账号记忆召回不能被管理员的旧向量挤出结果。"""

    async def test_each_account_has_its_own_vector_collection(self):
        """相同查询只返回当前账号的向量，旧管理员集合继续可用。"""
        with tempfile.TemporaryDirectory() as directory:
            index = MemoryIndex(Path(directory) / 'vectors', ApiEmbeddings())
            try:
                for owner, memory_id in [('legacy-admin', 'old-memory'), ('alice-id', 'alice-memory')]:
                    marker = _identity.set(owner)
                    try:
                        await index.upsert({'id': memory_id, 'content': '同一主题', 'scope': 'global',
                                            'revision': 'r1', 'owner_id': owner})
                        hits = await index.search('同一主题', 'global')
                        self.assertEqual([hit['id'] for hit in hits], [memory_id])
                    finally:
                        _identity.reset(marker)
            finally:
                index.close()


if __name__ == '__main__':
    unittest.main()
