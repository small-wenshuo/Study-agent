"""SQLite 保存学习快照、任务、事件及不可变笔记版本。"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, UTC
from pathlib import Path
from uuid import uuid4


def now() -> str:
    """统一使用 UTC ISO 时间保存记录。"""
    return datetime.now(UTC).isoformat()


class LearningStore:
    """独立学习数据库，不读取知识库或会话服务的私有表。"""

    def __init__(self, path: Path, authorize_study=None):
        """兼容建表并把上次进程遗留任务标为中断。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.authorize_study = authorize_study
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.db:
            self.db.executescript('''
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS paper_studies (
                    id TEXT PRIMARY KEY, knowledge_base_id TEXT NOT NULL, file_id TEXT NOT NULL,
                    title TEXT NOT NULL, active_snapshot_id TEXT, created_at TEXT NOT NULL,
                    UNIQUE(knowledge_base_id,file_id));
                CREATE TABLE IF NOT EXISTS paper_snapshots (
                    id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES paper_studies(id) ON DELETE CASCADE,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES paper_studies(id) ON DELETE CASCADE,
                    session_id TEXT NOT NULL, mode_revision INTEGER NOT NULL, kind TEXT NOT NULL,
                    status TEXT NOT NULL, input_json TEXT NOT NULL, result_id TEXT, error_code TEXT,
                    message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, finished_at TEXT);
                CREATE TABLE IF NOT EXISTS task_events (
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, seq INTEGER NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(task_id,seq));
                CREATE TABLE IF NOT EXISTS reading_batches (
                    snapshot_id TEXT NOT NULL REFERENCES paper_snapshots(id) ON DELETE CASCADE,
                    batch_index INTEGER NOT NULL, prompt_version TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(snapshot_id,batch_index,prompt_version));
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES paper_studies(id) ON DELETE CASCADE,
                    snapshot_id TEXT, kind TEXT NOT NULL, task_id TEXT, payload TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notes (
                    id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES paper_studies(id) ON DELETE CASCADE,
                    title TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS note_versions (
                    note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE, revision INTEGER NOT NULL,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(note_id,revision));
                UPDATE tasks SET status='interrupted',error_code='interrupted',
                    message='服务重启，任务已中断，请主动重试。'
                    WHERE status IN ('queued','running');
            ''')

    def one(self, sql: str, args=()) -> dict:
        """查询单个资源，不存在时返回统一业务错误。"""
        with self.lock:
            row = self.db.execute(sql, args).fetchone()
        if row is None:
            raise KeyError('资源不存在。')
        return dict(row)

    def many(self, sql: str, args=()) -> list[dict]:
        """读取有界列表并复制数据，不泄露连接对象。"""
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args)]

    def execute(self, sql: str, args=()):
        """单个数据库变更在锁和事务内完成。"""
        with self.lock, self.db:
            return self.db.execute(sql, args)

    def create_study(self, kb_id: str, file_id: str, title: str) -> dict:
        """同一文件复用学习记录，仅保存元数据，不自动处理全文。"""
        self.execute('INSERT OR IGNORE INTO paper_studies VALUES(?,?,?,?,NULL,?)',
                     (str(uuid4()), kb_id, file_id, title, now()))
        return self.one('SELECT * FROM paper_studies WHERE knowledge_base_id=? AND file_id=?', (kb_id, file_id))

    def study(self, study_id: str) -> dict:
        """获取学习记录及对应资源归属。"""
        value = self.one('SELECT * FROM paper_studies WHERE id=?', (study_id,))
        if self.authorize_study:
            self.authorize_study(study_id, value)
        return value

    def snapshot(self, snapshot_id: str, study_id: str) -> dict:
        """快照身份由文件内容确定，查询时严格限定学习记录。"""
        self.study(study_id)
        row = self.one('SELECT payload FROM paper_snapshots WHERE id=? AND study_id=?', (snapshot_id, study_id))
        return json.loads(row['payload'])

    def save_snapshot(self, study_id: str, snapshot: dict) -> dict:
        """相同文件跨知识库也拥有不同快照，避免相同内容串归属。"""
        import hashlib
        value = dict(snapshot)
        value['id'] = hashlib.sha256((study_id + snapshot['id']).encode()).hexdigest()
        self.execute('INSERT OR IGNORE INTO paper_snapshots VALUES(?,?,?,?)',
                     (value['id'], study_id, json.dumps(value, ensure_ascii=False), now()))
        self.execute('UPDATE paper_studies SET active_snapshot_id=? WHERE id=?', (value['id'], study_id))
        return value

    def artifact(self, artifact_id: str) -> dict:
        """读取结构化产物及本次实际证据。"""
        row = self.one('SELECT * FROM artifacts WHERE id=?', (artifact_id,))
        self.study(row['study_id'])
        payload = json.loads(row.pop('payload'))
        return {**row, **payload}

    def save_artifact(self, study_id: str, snapshot_id: str | None, kind: str, task_id: str, payload: dict) -> dict:
        """成功结果创建新产物，失败不覆盖历史版本。"""
        key = str(uuid4())
        self.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)',
                     (key, study_id, snapshot_id, kind, task_id, json.dumps(payload, ensure_ascii=False), now()))
        return self.artifact(key)

    def latest(self, study_id: str, kind: str, snapshot_id: str | None = None) -> dict | None:
        """只复用同一快照的成功导读，不复用部分结果。"""
        rows = self.many('SELECT id FROM artifacts WHERE study_id=? AND kind=? '
                         'AND (? IS NULL OR snapshot_id=?) ORDER BY created_at DESC,rowid DESC LIMIT 30',
                         (study_id, kind, snapshot_id, snapshot_id))
        for row in rows:
            artifact = self.artifact(row['id'])
            if artifact.get('coverage', {}).get('status', 'complete') == 'complete':
                return artifact
        return None

    def batch(self, snapshot_id: str, index: int, version: str) -> list | None:
        """读取可复用的校验通过批次。"""
        rows = self.many('SELECT payload FROM reading_batches WHERE snapshot_id=? AND batch_index=? AND prompt_version=?',
                         (snapshot_id, index, version))
        return json.loads(rows[0]['payload']) if rows else None

    def save_batch(self, snapshot_id: str, index: int, version: str, cards: list):
        """批次复用键包含提示词和预算策略版本。"""
        self.execute('INSERT OR REPLACE INTO reading_batches VALUES(?,?,?,?)',
                     (snapshot_id, index, version, json.dumps(cards, ensure_ascii=False)))

    def task(self, task_id: str) -> dict:
        """获取安全的任务状态和公开输入。"""
        row = self.one('SELECT * FROM tasks WHERE id=?', (task_id,))
        self.study(row['study_id'])
        row['input'] = json.loads(row.pop('input_json'))
        return row

    def event(self, task_id: str, stage: str, state: str, message: str, data: dict | None = None):
        """先保存事件，达到上限后省略重复进度但保证最终事件可见。"""
        with self.lock, self.db:
            count = self.db.execute('SELECT COALESCE(MAX(seq),0) FROM task_events WHERE task_id=?', (task_id,)).fetchone()[0]
            if count >= 499 and state not in {'complete', 'partial', 'failed', 'cancelled', 'interrupted'}:
                return
            payload = {'task_id': task_id, 'seq': count + 1, 'stage': stage, 'state': state,
                       'message': message, 'data': data or {}, 'created_at': now()}
            self.db.execute('INSERT INTO task_events VALUES(?,?,?)',
                            (task_id, count + 1, json.dumps(payload, ensure_ascii=False)))

    def events(self, task_id: str, after: int, limit: int = 100) -> list[dict]:
        """按连续序号回放事件，支持分页与断线恢复。"""
        self.task(task_id)
        return [json.loads(row['payload']) for row in self.many(
            'SELECT payload FROM task_events WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?', (task_id, after, limit))]

    def create_note(self, study_id: str, title: str, blocks: list[dict]) -> dict:
        """初次保存笔记，内容与证据关系作为版本一起保存。"""
        key = str(uuid4())
        with self.lock, self.db:
            self.db.execute('INSERT INTO notes VALUES(?,?,?,?,?)', (key, study_id, title, 1, now()))
            self.db.execute('INSERT INTO note_versions VALUES(?,?,?,?)',
                            (key, 1, json.dumps({'title': title, 'blocks': blocks}, ensure_ascii=False), now()))
        return self.note(key)

    def note(self, note_id: str, revision: int | None = None) -> dict:
        """读取当前或指定的不可变笔记版本。"""
        row = self.one('SELECT * FROM notes WHERE id=?', (note_id,))
        self.study(row['study_id'])
        revision = revision or row['revision']
        version = self.one('SELECT payload FROM note_versions WHERE note_id=? AND revision=?', (note_id, revision))
        payload = json.loads(version['payload'])
        # 兼容早期列表格式，新版本连标题一起固定，旧笔记不会因改名而变动。
        return {**row, 'revision': revision,
                'title': payload['title'] if isinstance(payload, dict) else row['title'],
                'blocks': payload['blocks'] if isinstance(payload, dict) else payload}

    def update_note(self, note_id: str, expected: int, title: str, blocks: list[dict]) -> dict:
        """乐观锁保护用户编辑，不静默覆盖其他窗口的新版本。"""
        with self.lock, self.db:
            count = self.db.execute('UPDATE notes SET revision=revision+1,title=? WHERE id=? AND revision=?',
                                    (title, note_id, expected)).rowcount
            if not count:
                self.note(note_id)
                raise RuntimeError('笔记已更新，请重新读取后编辑。')
            self.db.execute('INSERT INTO note_versions VALUES(?,?,?,?)',
                            (note_id, expected + 1, json.dumps({'title': title, 'blocks': blocks}, ensure_ascii=False), now()))
        return self.note(note_id)

    def delete_study(self, study_id: str):
        """级联删除快照、任务、笔记和产物。"""
        self.execute('DELETE FROM paper_studies WHERE id=?', (study_id,))

    def close(self):
        """测试或进程退出时释放数据库句柄。"""
        self.db.close()
