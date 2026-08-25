"""FastAPI 应用：创建会话 / 干预（停止·投递·关闭）/ SSE 事件流。

错误统一走 HTTPException 默认响应体；SSE 的 id 即内部事件 seq，
空闲时按 heartbeat_interval 发 `: ping` 注释行保活。

停止的执行动作（session.interrupt）在 intervene 置标记之后由 HTTP 层
调用——标记与 run_agent 的回合收尾在单线程事件循环上互斥，interrupt
晚于回合结束时停止目标已达成，无需把失败放大成错误。
"""
import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import runs as runs_mod
from .events import EventStore
from .runs import RunManager
from .sdk import SDKSessionFactory
from .session import run_agent

# 前端构建产物（vite build 输出），存在才挂载；开发时走 vite dev proxy
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent.parent / "web-ui" / "dist"


def create_app(session_factory=None, heartbeat_interval=15.0, static_dir=None):
    """session_factory 可注入：生产为 ClaudeSDKClient 真实现（默认），
    测试注入按剧本推消息的假实现——注入边界即唯一测试缝。"""
    app = FastAPI(title="auto-image deploy web")
    manager = RunManager()
    store = EventStore()
    factory = session_factory or SDKSessionFactory()
    app.state.run_manager = manager
    app.state.event_store = store
    app.state.session_factory = factory
    app.state.heartbeat_interval = heartbeat_interval

    @app.post("/api/runs")
    async def create_run(body: dict | None = None):
        resume_from = (body or {}).get("resume_from")
        if resume_from is not None and manager.get(resume_from) is None:
            raise HTTPException(status_code=404, detail="run not found")
        try:
            run = manager.create(resume_from)
        except runs_mod.Conflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        store.create(run.run_id)
        # 服务端侧 run 目录（事件日志导出、run 元信息；不参与 Agent 执行）
        _run_dir(run.run_id).mkdir(parents=True, exist_ok=True)
        run.task = asyncio.create_task(run_agent(run, factory, store))
        return {"run_id": run.run_id, "status": run.status, "resumed_from": run.resumed_from}

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
            # 本会话执行中：intervene 先请求停止，本端点返回后执行打断
            manager.intervene(run, text)
        except runs_mod.Conflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        await _interrupt_if_requested(run)
        return {"run_id": run.run_id, "status": run.status}

    @app.post("/api/runs/{run_id}/stop")
    async def stop_run(run_id: str, body: dict | None = None):
        run = _get_run_or_404(manager, run_id)
        text = (body or {}).get("text")
        if text is not None and (not isinstance(text, str) or not text.strip()):
            raise HTTPException(status_code=422, detail="text must be non-empty")
        try:
            manager.intervene(run, text)
        except runs_mod.Conflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        await _interrupt_if_requested(run)
        return {"run_id": run.run_id, "status": run.status}

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str):
        run = _get_run_or_404(manager, run_id)
        try:
            manager.cancel(run)
        except runs_mod.Conflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        run.task.cancel()
        try:
            await run.task  # 等收尾（run.canceled 已入事件流）再返回
        except asyncio.CancelledError:
            pass
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


async def _interrupt_if_requested(run):
    """执行 intervene 排队的打断。回合可能刚好已自然结束（停止目标视为
    达成）、会话可能尚未建立（创建后立即干预的窗口），两种情形均跳过；
    打断本身失败不改变服务端权威状态（回合如何收尾以事件流为准）。"""
    if not run.stop_requested or run.session is None:
        return
    try:
        await run.session.interrupt()
    except Exception:  # noqa: BLE001 —— 外部打断失败不放大为 HTTP 错误
        pass


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
