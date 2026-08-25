#!/usr/bin/env python
"""web 服务主缝测试 —— HTTP 进、SSE 出。

缝：FastAPI ASGI 测试客户端驱动全部外部行为；会话经工厂注入
脚本化假实现，不触网、不启动真 SDK。纯 assert，无 pytest。

运行：python web/tests/test_api.py
"""
import asyncio
import json
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.app import create_app  # noqa: E402
from web.fake import DEFAULT_SCRIPT, FakeSessionFactory  # noqa: E402
from web.tests.support import StreamingASGITransport  # noqa: E402

HEARTBEAT = 0.05
DELAY = 0.02


def make_app(script=None, delay=DELAY):
    return create_app(
        session_factory=FakeSessionFactory(script=script if script is not None else DEFAULT_SCRIPT, delay=delay),
        heartbeat_interval=HEARTBEAT,
    )


def parse_sse_block(block_lines):
    """一个 SSE 事件块（若干属性行）→ {id, event, data}。"""
    ev = {}
    for line in block_lines:
        key, _, value = line.partition(":")
        value = value.strip()
        if key == "data":
            ev["data"] = json.loads(value)
        elif key in ("id", "event"):
            ev[key] = value
    return ev


async def collect_sse(resp, stop=None, deadline_s=5.0):
    """聚合 SSE 流为 (事件列表, 心跳行数)；stop(ev) 为 True 时停止读取。"""
    pings = 0
    events = []
    block = []
    deadline = time.monotonic() + deadline_s
    async for line in resp.aiter_lines():
        if time.monotonic() > deadline:
            break
        if line == "":
            if block:
                ev = parse_sse_block(block)
                events.append(ev)
                block = []
                if stop and stop(ev):
                    break
            continue
        if line.startswith(":"):
            pings += 1
            continue
        block.append(line)
    return events, pings


async def wait_status(client, run_id, want, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        r = await client.get(f"/api/runs/{run_id}")
        assert r.status_code == 200, r.text
        last = r.json()
        if last["status"] == want:
            return last
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} 未进入 {want}，最后状态 {last}")


async def open_stream(client, run_id, last_event_id=None):
    headers = {"Last-Event-ID": str(last_event_id)} if last_event_id else {}
    return await client.send(
        client.build_request("GET", f"/api/runs/{run_id}/events", headers=headers),
        stream=True,
    )


async def test_create_run_returns_waiting_input_immediately():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post("/api/runs", json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["run_id"].startswith("run_")
        assert body["status"] == "WAITING_INPUT"
        assert body["resumed_from"] is None
        r = await client.get(f"/api/runs/{body['run_id']}")
        assert r.json()["status"] == "WAITING_INPUT"


async def test_first_message_drives_scripted_turn():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        resp = await open_stream(client, run_id)
        events, _ = await collect_sse(resp, deadline_s=1.0)
        assert [e["event"] for e in events][:2] == ["run.started"]

        r = await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx 1.25 到 server-a"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "RUNNING"

        await wait_status(client, run_id, "WAITING_INPUT")
        resp = await open_stream(client, run_id)
        events, _ = await collect_sse(resp, deadline_s=1.0)
        types = [e["event"] for e in events]
        assert types == [
            "run.started",
            "user.message",
            "agent.thinking",
            "agent.message",
            "stage.changed",
            "agent.tool_started",
            "agent.tool_finished",
            "agent.message",
            "turn.completed",
        ], types
        # seq 从 1 起严格递增，SSE 的 id 即 seq
        seqs = [int(e["id"]) for e in events]
        assert seqs == list(range(1, len(events) + 1)), seqs
        # 首条指令原样进入事件流
        assert events[1]["data"]["text"] == "部署 nginx 1.25 到 server-a"
        # 阶段由 Task + subagent_type 推导
        assert events[4]["data"] == {"stage": "GUIDE", "status": "running"}
        # 工具事件只带工具名与摘要
        assert events[5]["data"]["tool"] == "Task"
        assert events[6]["data"]["tool"] == "Task"
        # 回合汇总携带 result 文本
        assert "result" in events[-1]["data"]


async def test_suspended_run_keeps_stream_open_with_heartbeat():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "WAITING_INPUT")

        async with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            # 回合重放完毕后连接保持：等待期间应收到心跳注释行
            _, pings = await collect_sse(resp, deadline_s=0.5)
        assert pings >= 1, "挂起会话的 SSE 流应保持连接并有心跳"


async def test_terminal_run_closes_stream():
    fail_script = list(DEFAULT_SCRIPT) + [RuntimeError("sdk crashed: password=leaked-secret")]
    app = make_app(script=fail_script)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "FAILED")

        async with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
            events, pings = await collect_sse(resp, deadline_s=2.0)
        types = [e["event"] for e in events]
        assert types[-1] == "run.failed", types
        # 错误摘要同样过脱敏层
        assert "leaked-secret" not in json.dumps(events[-1], ensure_ascii=False)
        # 重放完全部历史后流正常关闭：读循环自然结束，不再靠心跳保活
        assert pings == 0, pings
        r = await client.get(f"/api/runs/{run_id}")
        assert r.json()["status"] == "FAILED"


async def test_last_event_id_replays_from_next_seq_without_duplicates():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "WAITING_INPUT")

        # 第一段连接只读到 seq=3 即断开
        resp = await open_stream(client, run_id)
        first, _ = await collect_sse(resp, stop=lambda e: int(e["id"]) == 3)
        assert int(first[-1]["id"]) == 3
        await resp.aclose()

        # 重连携带 Last-Event-ID=3：从 seq+1 重放，已收事件不重发
        resp = await open_stream(client, run_id, last_event_id=3)
        second, _ = await collect_sse(resp, deadline_s=1.0)
        ids = [int(e["id"]) for e in second]
        assert ids[0] == 4, ids
        assert ids == sorted(ids) and len(ids) == len(set(ids))
        first_types = [e["event"] for e in first]
        assert not [e for e in second if int(e["id"]) <= 3]
        assert first_types[-1] != "turn.completed" or "turn.completed" in [e["event"] for e in second]


async def test_running_blocks_new_run_but_suspended_does_not():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_a = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_a}/messages", json={"text": "部署 nginx"})
        # 回合执行中：新建被拒
        r = await client.post("/api/runs", json={})
        assert r.status_code == 409
        assert r.json() == {"detail": "deployment_in_progress"}

        await wait_status(client, run_a, "WAITING_INPUT")
        # 挂起不阻塞新会话
        r = await client.post("/api/runs", json={})
        assert r.status_code == 200, r.text
        run_b = r.json()["run_id"]
        assert run_b != run_a
        # 挂起会话可继续第二条指令
        r = await client.post(f"/api/runs/{run_a}/messages", json={"text": "继续"})
        assert r.status_code == 200
        await wait_status(client, run_a, "WAITING_INPUT")
        resp = await open_stream(client, run_a)
        events, _ = await collect_sse(resp, deadline_s=1.0)
        types = [e["event"] for e in events]
        assert types.count("user.message") == 2, types
        assert types.count("turn.completed") == 2, types


async def test_second_turn_after_completed_turn_replays_new_events_only():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "WAITING_INPUT")
        resp = await open_stream(client, run_id)
        first, _ = await collect_sse(resp, deadline_s=1.0)
        assert first[-1]["event"] == "turn.completed"
        await resp.aclose()

        await client.post(f"/api/runs/{run_id}/messages", json={"text": "删除刚创建的 ECS"})
        await wait_status(client, run_id, "WAITING_INPUT")
        # 以首回合末尾为断点重连：只补发第二回合
        resp = await open_stream(client, run_id, last_event_id=int(first[-1]["id"]))
        second, _ = await collect_sse(resp, deadline_s=1.0)
        types = [e["event"] for e in second]
        assert types[0] == "user.message", types
        assert "run.started" not in types


async def test_messages_conflicts():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        # 不存在的 run
        r = await client.post("/api/runs/run_missing/messages", json={"text": "x"})
        assert r.status_code == 404
        # 回合执行中向同一会话发指令：本版本直接拒绝（先停后发属后续干预语义）
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        r = await client.post(f"/api/runs/{run_id}/messages", json={"text": "再干点别的"})
        assert r.status_code == 409
        assert r.json()["detail"] == "execution_in_progress"
        await wait_status(client, run_id, "WAITING_INPUT")
        # 另一会话执行中向挂起会话发指令
        run_b = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_b}/messages", json={"text": "部署 redis"})
        r = await client.post(f"/api/runs/{run_id}/messages", json={"text": "继续"})
        assert r.status_code == 409
        assert r.json()["detail"] == "execution_in_progress"


async def test_redaction_masks_credentials_everywhere():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "WAITING_INPUT")
        resp = await open_stream(client, run_id)
        events, _ = await collect_sse(resp, deadline_s=1.0)
        raw = json.dumps(events, ensure_ascii=False)
        # thinking（AK/SK）、message（password）、turn.completed 的 result（secret）
        for secret in ("HWPFEJ9AB3CDEFGHIJKL", "f3a9c81d0b7e46f2a5d8c3b1e9470ad6c2f5b831", "Xk9$mPq2LwzR", "topsecret-token"):
            assert secret not in raw, secret
        assert "***" in raw


async def test_message_to_terminal_run_rejected():
    fail_script = list(DEFAULT_SCRIPT) + [RuntimeError("boom")]
    app = make_app(script=fail_script)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "FAILED")
        r = await client.post(f"/api/runs/{run_id}/messages", json={"text": "继续"})
        assert r.status_code == 409
        assert r.json() == {"detail": "run_not_active"}


async def test_unknown_run_events_404():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.get("/api/runs/run_missing/events")
        assert r.status_code == 404


async def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        await fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
