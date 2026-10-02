"""有容量和总时限的本地学习任务执行器，浏览器断开不取消任务。"""
from __future__ import annotations

import asyncio
import time
from uuid import uuid4
from app.papers.store import now

TERMINAL = {'complete', 'partial', 'failed', 'cancelled', 'interrupted'}


class TaskConflict(RuntimeError):
    """资源或模式版本冲突。"""


class TaskCapacityError(RuntimeError):
    """执行器的活跃加等待任务已满。"""


class TaskBudgetError(RuntimeError):
    """模型调用或任务时间预算已耗尽。"""


class TaskBudget:
    """在调用前及发布前检查模式，防止关闭后继续生成。"""

    def __init__(self, manager, task: dict):
        """记录运行时截止点和调用数。"""
        self.manager, self.task = manager, task
        self.deadline = time.monotonic() + manager.settings.paper_task_timeout_seconds
        self.calls = 0

    def check(self):
        """模式失效立即取消；截止点不足抛出预算错误。"""
        if not self.manager.valid(self.task):
            raise asyncio.CancelledError()
        if time.monotonic() >= self.deadline:
            raise TaskBudgetError('任务总时限已耗尽。')

    def check_call(self):
        """每次尝试及修复都消耗一次调用额度。"""
        self.check()
        if self.calls >= self.manager.settings.paper_max_model_calls:
            raise TaskBudgetError('模型调用预算已耗尽。')
        self.calls += 1
        self.manager.store.event(self.task['id'], 'model', 'calling', '正在调用模型。',
                                 {'attempt': self.calls})

    async def emit(self, stage: str, state: str, message: str, data=None):
        """保存事件并让出调度，模式切换可及时生效。"""
        self.check()
        self.manager.store.event(self.task['id'], stage, state, message, data)
        await asyncio.sleep(0)


class TaskManager:
    """单进程任务容量、取消和恢复由数据库状态统一表达。"""

    def __init__(self, store, settings, valid):
        """恢复由存储负责；运行句柄仅保留本进程活跃任务。"""
        self.store, self.settings, self.valid = store, settings, valid
        self.running = {}
        self.semaphore = asyncio.Semaphore(settings.paper_active_tasks)
        self.closing = False

    def available(self, excluding_session: str | None = None):
        """创建前检查容量，避免假装启动成功。"""
        live = 0
        for key, handle in self.running.items():
            if handle.done():
                continue
            try:
                task = self.store.task(key)
            except KeyError:
                continue
            if task['status'] not in TERMINAL and task['session_id'] != excluding_session:
                live += 1
        if self.closing or live >= self.settings.paper_active_tasks + self.settings.paper_queue_capacity:
            raise TaskCapacityError('论文任务队列已满，请稍后再试。')

    def submit(self, study_id: str, session_id: str, revision: int, kind: str, inputs: dict, worker) -> dict:
        """任务先落盘；同一学习记录拒绝并发，完全相同请求复用。"""
        import json
        pending = self.store.many("SELECT id FROM tasks WHERE study_id=? AND status IN ('queued','running')", (study_id,))
        for row in pending:
            task = self.store.task(row['id'])
            if task['session_id'] == session_id and task['mode_revision'] == revision and task['kind'] == kind and task['input'] == inputs:
                return task
            raise TaskConflict('这篇论文已有任务正在执行。')
        self.available()
        key = str(uuid4())
        self.store.execute('INSERT INTO tasks(id,study_id,session_id,mode_revision,kind,status,input_json,created_at) '
                           "VALUES(?,?,?,?,?,'queued',?,?)", (key, study_id, session_id, revision, kind,
                           json.dumps(inputs, ensure_ascii=False), now()))
        task = self.store.task(key)
        self.store.event(key, 'queue', 'queued', '论文学习任务已排队。')
        handle = asyncio.create_task(self._run(task, worker))
        self.running[key] = handle
        handle.add_done_callback(lambda _: self.running.pop(key, None))
        return task

    def finish(self, key, status, result_id=None, code=None, message=''):
        """终止状态只写一次，取消不能被迟到的成功覆盖。"""
        try:
            cursor = self.store.execute("UPDATE tasks SET status=?,result_id=?,error_code=?,message=?,finished_at=? "
                                        "WHERE id=? AND status IN ('queued','running')",
                                        (status, result_id, code, message, now(), key))
            if cursor.rowcount:
                self.store.event(key, 'task', status, message or '任务已完成。', {'result_id': result_id})
        except KeyError:
            pass

    async def _run(self, task: dict, worker):
        """等待容量后再次核验，整个执行流程受总时限保护。"""
        budget = None
        try:
            async with self.semaphore:
                budget = TaskBudget(self, task)
                budget.check()
                self.store.execute("UPDATE tasks SET status='running' WHERE id=? AND status='queued'", (task['id'],))
                await budget.emit('task', 'running', '开始论文学习。')
                async with asyncio.timeout(self.settings.paper_task_timeout_seconds):
                    payload = await worker(task, budget)
                    budget.check()
                    artifact = self.store.save_artifact(task['study_id'], payload.pop('snapshot_id', None),
                                                        task['kind'], task['id'], payload)
                    self.finish(task['id'], payload.get('coverage', {}).get('status', 'complete'), artifact['id'])
        except asyncio.CancelledError:
            self.finish(task['id'], 'cancelled', code='cancelled', message='任务已取消，已保存内容仍保留。')
        except TimeoutError:
            total = budget is not None and time.monotonic() >= budget.deadline - .1
            self.finish(task['id'], 'failed', code='task_timeout' if total else 'model_timeout',
                        message='任务总时限已耗尽，请缩小范围后重试。' if total else '模型单次调用连续超时，请稍后重试。')
        except TaskBudgetError:
            self.finish(task['id'], 'failed', code='budget_exhausted', message='任务超时或调用预算耗尽，请缩小范围后重试。')
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning('paper_task_failed type=%s', type(exc).__name__)
            code = ('provider_rate_limited' if getattr(exc, 'status_code', None) == 429 else
                    'invalid_evidence' if isinstance(exc, ValueError) else 'provider_unavailable')
            self.store.event(task['id'], 'error', 'reported', '本次失败类别已记录。',
                             {'error_type': type(exc).__name__, 'error_code': code})
            self.finish(task['id'], 'failed', code=code,
                        message='模型服务限流或额度不足，请稍后或调整额度后主动重试。' if code == 'provider_rate_limited' else
                        '资料、结构或引用校验未通过，请查看覆盖情况后重试。' if code == 'invalid_evidence' else
                        '外部模型或服务暂不可用，请检查配置与额度后重试。')

    def cancel(self, key: str):
        """显式取消幂等，记录先失效，再取消运行中的协程。"""
        self.store.task(key)
        self.finish(key, 'cancelled', code='cancelled', message='任务已取消。')
        handle = self.running.get(key)
        if handle:
            handle.cancel()

    def cancel_session(self, session_id: str):
        """模式关闭只影响当前会话，不影响其他会话。"""
        for row in self.store.many("SELECT id FROM tasks WHERE session_id=? AND status IN ('queued','running')", (session_id,)):
            self.cancel(row['id'])

    async def close(self):
        """服务退出标记中断，等待协程收尾，避免未处理后台异常。"""
        self.closing = True
        handles = list(self.running.items())
        for key, handle in handles:
            self.finish(key, 'interrupted', code='interrupted', message='服务退出，任务已中断。')
            handle.cancel()
        await asyncio.gather(*(handle for _, handle in handles), return_exceptions=True)
