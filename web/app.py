"""FastAPI 应用：创建会话 / 投递指令 / SSE 事件流。

错误统一走 HTTPException 默认响应体；SSE 的 id 即内部事件 seq，
空闲时按 heartbeat_interval 发 `: ping` 注释行保活。
"""
import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import runs as runs_mod
from .events import EventStore
from .fake import FakeSessionFactory
from .runs import RunManager
from .session import run_agent

# 前端构建产物（vite build 输出），存在才挂载；开发时走 vite dev proxy
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent.parent / "web-ui" / "dist"


def create_app(session_factory=None, heartbeat_interval=15.0, static_dir=None):
    app = FastAPI(title="auto-image deploy web")
    manager = RunManager()
    store = EventStore()
    factory = session_factory or FakeSessionFactory()
    app.state.run_manager = manager
    app.state.event_store = store
    app.state.heartbeat_interval = heartbeat_interval

    @app.post("/api/runs")
    async def create_run():
        try:
            run = manager.create()
        except runs_mod.Conflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        store.create(run.run_id)
        # 服务端侧 run 目录（事件日志导出、run 元信息；不参与 Agent 执行）
        _run_dir(run.run_id).mkdir(parents=True, exist_ok=True)
        run.task = asyncio.create_task(run_agent(run, factory, store))
        return {"run_id": run.run_id, "status": run.status, "resumed_from": None}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        return _get_run_or_404(manager, run_id).summary()

    @app.post("/api/runs/{run_id}/messages")
    async def send_message(run_id: str, body: dict):
        run = _get_run_or_404(manager, run_id)
        text = (body or {}).get("text")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=422, detail="text required")
        try:
            manager.send(run, text)
        except runs_mod.Conflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        return {"run_id": run.run_id, "status": run.status}

    @app.get("/api/runs/{run_id}/events")
    async def event_stream(run_id: str, request: Request):
        run = _get_run_or_404(manager, run_id)
        seen = _parse_last_event_id(request.headers.get("Last-Event-ID"))

        async def generate():
            nonlocal seen
            with store.subscribe(run_id) as flag:
                while True:
                    flag.clear()
                    for event in store.replay_from(run_id, seen):
                        seen = event["seq"]
                        yield _sse_chunk(event)
                    # 终态且历史重放完毕：正常结束流
                    if run.status in runs_mod.TERMINAL and store.is_complete(run_id, seen):
                        return
                    try:
                        await asyncio.wait_for(flag.wait(), timeout=heartbeat_interval)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    directory = static_dir or DEFAULT_STATIC_DIR
    if Path(directory).is_dir():
        app.mount("/", StaticFiles(directory=str(directory), html=True), name="ui")

    return app


def _sse_chunk(event):
    data = json.dumps(event["payload"], ensure_ascii=False)
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {data}\n\n"


def _parse_last_event_id(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _get_run_or_404(manager, run_id):
    run = manager.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


def _run_dir(run_id):
    return Path("/tmp/auto-image-runs") / run_id
