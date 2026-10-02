"""V4 学习任务 HTTP/SSE 接口，查询不会偷偷触发模型。"""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from app.services.tasks import TaskConflict, TaskCapacityError, TERMINAL


@contextmanager
def business_errors():
    """把业务错误映射到安全响应，供应商异常不直接返回浏览器。"""
    try:
        yield
    except KeyError as exc:
        raise HTTPException(404, '资源不存在。') from exc
    except TaskCapacityError as exc:
        raise HTTPException(429, str(exc)) from exc
    except TaskConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        status = 409 if '版本' in str(exc) or '更新' in str(exc) else 503
        raise HTTPException(status, str(exc)) from exc


class StudyRequest(BaseModel):
    """仅接受文件 ID，不能由客户端指定服务器路径。"""
    knowledge_base_id: str = Field(min_length=1, max_length=100)
    file_id: str = Field(min_length=1, max_length=100)


class ModeRequest(BaseModel):
    """显式会话模式变更携带乐观锁版本。"""
    enabled: bool
    study_id: str | None = Field(None, max_length=100)
    expected_revision: int = Field(ge=0)


class TaskRequest(BaseModel):
    """生成与重试绑定当前会话模式版本。"""
    session_id: str = Field(min_length=1, max_length=100)
    paper_mode_revision: int = Field(ge=0)


class OverviewRequest(TaskRequest):
    """重新生成需要用户显式选择 force。"""
    force: bool = False


class ExplanationRequest(TaskRequest):
    """单次追问的内容、目标和联网许可彼此独立。"""
    question: str = Field(min_length=1, max_length=12000)
    snapshot_id: str | None = Field(None, max_length=100)
    target_block_id: str | None = Field(None, max_length=200)
    style: Literal['自由追问', '解释概念', '拆解公式', '举例说明', '外部补充'] = '自由追问'
    allow_web: bool = False

    @field_validator('question')
    @classmethod
    def nonblank(cls, value):
        """空白问题不能消耗任务容量。"""
        if not value.strip():
            raise ValueError('问题不能为空。')
        return value.strip()


class SelfTestRequest(TaskRequest):
    """自测仅针对用户选定的学习产物。"""
    artifact_ids: list[str] = Field(min_length=1, max_length=10)


class NoteRequest(BaseModel):
    """保存笔记只能引用产物 ID，不接受客户端伪造的来源对象。"""
    title: str = Field(min_length=1, max_length=100)
    artifact_ids: list[str] = Field(default_factory=list, max_length=10)
    user_text: str = Field('', max_length=12000)

    @field_validator('title')
    @classmethod
    def title_not_blank(cls, value):
        """空白标题不能创建无法识别的学习记录。"""
        if not value.strip():
            raise ValueError('笔记标题不能为空。')
        return value.strip()


class NoteUpdate(BaseModel):
    """只更新文本，来源及编辑状态由服务端维护。"""
    expected_revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=100)
    texts: list[str] = Field(min_length=1, max_length=100)

    @field_validator('title')
    @classmethod
    def title_not_blank(cls, value):
        """编辑同样拒绝空白标题。"""
        if not value.strip():
            raise ValueError('笔记标题不能为空。')
        return value.strip()


class OutlineRequest(TaskRequest):
    """固定笔记版本后生成大纲。"""
    note_revision: int = Field(ge=1)
    page_count: int = Field(10, ge=3, le=30)
    focus: str = Field('', max_length=2000)


class OutlineUpdate(BaseModel):
    """大纲编辑创建新版本。"""
    expected_revision: int = Field(ge=1)
    slides: list[dict] = Field(min_length=1, max_length=30)


def create_router(get_service):
    """依赖 getter 在请求时解析，生产和 ASGI 测试使用同一套路由。"""
    router = APIRouter(prefix='/api')

    @router.post('/paper-studies', status_code=201)
    async def create_study(body: StudyRequest, service=Depends(get_service)):
        """创建学习记录不读取全文。"""
        with business_errors():
            return service.create_study(body.knowledge_base_id, body.file_id)

    @router.get('/paper-studies/{study_id}')
    async def study_detail(study_id: str, service=Depends(get_service)):
        """回看已有学习记录。"""
        with business_errors():
            return service.study(study_id)

    @router.patch('/sessions/{session_id}/paper-mode')
    async def mode(session_id: str, body: ModeRequest, service=Depends(get_service)):
        """仅用户显式开启并选择论文才触发一次导读。"""
        with business_errors():
            return service.change_mode(session_id, body.enabled, body.study_id if body.enabled else None, body.expected_revision)

    @router.post('/paper-studies/{study_id}/overview', status_code=202)
    async def overview(study_id: str, body: OverviewRequest, service=Depends(get_service)):
        """显式重建导读。"""
        with business_errors():
            return service.generate(study_id, body.session_id, body.paper_mode_revision, 'overview', {'force': body.force})

    @router.post('/paper-studies/{study_id}/explanations', status_code=202)
    async def explain(study_id: str, body: ExplanationRequest, service=Depends(get_service)):
        """针对当前问题生成一次解释。"""
        with business_errors():
            return service.generate(study_id, body.session_id, body.paper_mode_revision, 'explanation',
                                    body.model_dump(exclude={'session_id', 'paper_mode_revision'}))

    @router.post('/paper-studies/{study_id}/self-tests', status_code=202)
    async def self_test(study_id: str, body: SelfTestRequest, service=Depends(get_service)):
        """按用户选定内容生成自测。"""
        with business_errors():
            return service.generate(study_id, body.session_id, body.paper_mode_revision, 'self_test', {'artifact_ids': body.artifact_ids})

    @router.get('/tasks/{task_id}')
    async def task(task_id: str, service=Depends(get_service)):
        """查询状态不创建或重试任务。"""
        with business_errors():
            return service.store.task(task_id)

    @router.get('/tasks/{task_id}/events')
    async def events(task_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100), service=Depends(get_service)):
        """分页回放已保存事件。"""
        with business_errors():
            items = service.store.events(task_id, after, limit)
            return {'items': items, 'next_after': items[-1]['seq'] if items else after}

    @router.get('/tasks/{task_id}/stream')
    async def stream(task_id: str, after: int = Query(0, ge=0), last_event_id: str | None = Header(None), service=Depends(get_service)):
        """基于数据库回放 SSE，不持有无限增长的订阅队列。"""
        with business_errors():
            service.store.task(task_id)
            if last_event_id is not None:
                try:
                    sequence = int(last_event_id)
                    if sequence < 0:
                        raise ValueError()
                    after = max(after, sequence)
                except ValueError as exc:
                    raise HTTPException(422, 'Last-Event-ID 必须是非负序号。') from exc
        async def iterator():
            """慢客户端通过游标重连；完成后发送终止状态。"""
            cursor = after
            while True:
                try:
                    items = service.store.events(task_id, cursor)
                    for item in items:
                        cursor = item['seq']
                        yield f"id: {cursor}\nevent: progress\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                    current = service.store.task(task_id)
                    if current['status'] in TERMINAL and len(items) < 100:
                        yield 'event: done\ndata: ' + json.dumps(current, ensure_ascii=False) + '\n\n'
                        break
                except KeyError:
                    yield 'event: done\ndata: {"status":"cancelled","message":"来源已删除。"}\n\n'
                    break
                if len(items) < 100:
                    yield ': heartbeat\n\n'
                    await asyncio.sleep(0.5)
        return StreamingResponse(iterator(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache'})

    @router.post('/tasks/{task_id}/cancel')
    async def cancel(task_id: str, service=Depends(get_service)):
        """取消幂等，不改写已完成任务。"""
        with business_errors():
            service.tasks.cancel(task_id)
            return service.store.task(task_id)

    @router.post('/tasks/{task_id}/retry', status_code=202)
    async def retry(task_id: str, body: TaskRequest, service=Depends(get_service)):
        """重试始终需要显式请求和当前模式版本。"""
        with business_errors():
            return service.retry(task_id, body.session_id, body.paper_mode_revision)

    @router.get('/artifacts/{artifact_id}')
    async def artifact(artifact_id: str, service=Depends(get_service)):
        """返回结构化内容，前端用纯文本显示来源。"""
        with business_errors():
            return service.artifact(artifact_id)

    @router.get('/artifacts/{artifact_id}/sources/{evidence_id}')
    async def source(artifact_id: str, evidence_id: str, service=Depends(get_service)):
        """只能访问该产物本次登记的实际来源。"""
        with business_errors():
            artifact = service.store.artifact(artifact_id)
            evidence = next((e for e in artifact.get('evidence', []) if e['id'] == evidence_id), None)
            if evidence is None:
                raise KeyError()
            return evidence

    @router.post('/paper-studies/{study_id}/notes', status_code=201)
    async def note_create(study_id: str, body: NoteRequest, service=Depends(get_service)):
        """主动保存选定产物或用户文本，模式关闭也可整理历史内容。"""
        with business_errors():
            return service.notes.create(study_id, body.title, body.artifact_ids, body.user_text)

    @router.get('/notes/{note_id}')
    async def note_detail(note_id: str, revision: int | None = Query(None, ge=1), service=Depends(get_service)):
        """恢复指定笔记版本。"""
        with business_errors():
            return service.store.note(note_id, revision)

    @router.put('/notes/{note_id}')
    async def note_update(note_id: str, body: NoteUpdate, service=Depends(get_service)):
        """编辑版本检查失败返回冲突。"""
        with business_errors():
            return service.notes.update(note_id, body.expected_revision, body.title, body.texts)

    @router.get('/notes/{note_id}/export')
    async def note_export(note_id: str, revision: int | None = Query(None, ge=1), format: Literal['md'] = 'md', service=Depends(get_service)):
        """导出 Markdown，无需启用生成模式。"""
        with business_errors():
            text = service.notes.export(note_id, revision)
            return Response(text, media_type='text/markdown', headers={'Content-Disposition': 'attachment; filename="study-note.md"'})

    @router.post('/notes/{note_id}/presentation-outline', status_code=202)
    async def outline(note_id: str, body: OutlineRequest, service=Depends(get_service)):
        """固定笔记版本生成大纲，不自动执行文件导出。"""
        with business_errors():
            note = service.store.note(note_id, body.note_revision)
            return service.generate(note['study_id'], body.session_id, body.paper_mode_revision, 'presentation_outline',
                                    {'note_id': note_id, **body.model_dump(exclude={'session_id', 'paper_mode_revision'})})

    @router.put('/presentation-outlines/{artifact_id}')
    async def outline_update(artifact_id: str, body: OutlineUpdate, service=Depends(get_service)):
        """保存新大纲产物，原大纲版本不变。"""
        with business_errors():
            return service.edit_outline(artifact_id, body.expected_revision, body.slides)

    @router.post('/presentation-outlines/{artifact_id}/render', status_code=202)
    async def outline_render(artifact_id: str, body: TaskRequest, service=Depends(get_service)):
        """仅在用户确认当前大纲后生成 PPTX。"""
        with business_errors():
            artifact = service.store.artifact(artifact_id)
            if artifact['kind'] != 'presentation_outline':
                raise ValueError('该产物不是 PPT 大纲。')
            return service.generate(artifact['study_id'], body.session_id, body.paper_mode_revision, 'presentation',
                                    {'outline_id': artifact_id, 'outline_revision': artifact['revision']})

    @router.get('/artifacts/{artifact_id}/download')
    async def download(artifact_id: str, service=Depends(get_service)):
        """只下载已生成并登记的文件。"""
        with business_errors():
            return FileResponse(service.download(artifact_id), filename='study-paper.pptx',
                                media_type='application/vnd.openxmlformats-officedocument.presentationml.presentation')

    return router
