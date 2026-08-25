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


async def wait_replay(client, run_id, ok, timeout_s=8.0):
    """轮询全量事件重放直到 ok(events) 满足（回合收尾是异步的，
    状态转挂起与事件落库之间可能隔着第二回合的启动）。"""
    deadline = time.monotonic() + timeout_s
    events = []
    while time.monotonic() < deadline:
        resp = await open_stream(client, run_id)
        events, _ = await collect_sse(resp, deadline_s=0.6)
        await resp.aclose()
        if events and ok(events):
            return events
    raise AssertionError(f"事件流未满足条件，当前 {[e['event'] for e in events]}")


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
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
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



async def test_stop_returns_to_waiting_and_session_continues():
    # 慢剧本保证 stop 必落在回合执行中（快剧本下时序不稳）
    app = make_app(delay=0.2)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        r = await client.post(f"/api/runs/{run_id}/stop", json={})
        assert r.status_code == 200, r.text
        await wait_status(client, run_id, "WAITING_INPUT", timeout_s=8.0)

        # 会话保留：停止后可继续任意指令
        r = await client.post(f"/api/runs/{run_id}/messages", json={"text": "删掉刚创建的 ECS"})
        assert r.status_code == 200, r.text
        events = await wait_replay(
            client, run_id,
            lambda evs: [e["event"] for e in evs].count("user.message") == 2
            and evs[-1]["event"] == "turn.completed",
        )
        types = [e["event"] for e in events]
        assert types.count("turn.stopped") == 1, types
        assert types.count("turn.completed") == 1, types  # 仅第二回合正常收尾
        ums = [i for i, t in enumerate(types) if t == "user.message"]
        assert ums[0] < types.index("turn.stopped") < ums[1], types


async def test_stop_with_text_stops_then_delivers():
    app = make_app(delay=0.2)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx 1.25 到 server-a"})
        r = await client.post(f"/api/runs/{run_id}/stop", json={"text": "跳过验证直接打包"})
        assert r.status_code == 200, r.text
        events = await wait_replay(
            client, run_id,
            lambda evs: [e["event"] for e in evs].count("user.message") == 2
            and evs[-1]["event"] == "turn.completed",
        )
        types = [e["event"] for e in events]
        assert types.count("turn.stopped") == 1, types
        assert types[-1] == "turn.completed", types
        texts = [e["data"]["text"] for e in events if e["event"] == "user.message"]
        assert texts == ["部署 nginx 1.25 到 server-a", "跳过验证直接打包"], texts
        # 停止与投递的先后在事件流上可分辨
        ums = [i for i, t in enumerate(types) if t == "user.message"]
        assert ums[0] < types.index("turn.stopped") < ums[1], types


async def test_message_while_running_stops_first_then_delivers():
    app = make_app(delay=0.2)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        # 执行中直接发新指令：不再 409，服务端先停止再投递
        r = await client.post(f"/api/runs/{run_id}/messages", json={"text": "删掉刚创建的 ECS"})
        assert r.status_code == 200, r.text
        events = await wait_replay(
            client, run_id,
            lambda evs: [e["event"] for e in evs].count("user.message") == 2
            and evs[-1]["event"] == "turn.completed",
        )
        types = [e["event"] for e in events]
        assert types.count("turn.stopped") == 1, types
        assert types.count("user.message") == 2, types
        assert types[-1] == "turn.completed", types
        ums = [i for i, t in enumerate(types) if t == "user.message"]
        assert ums[0] < types.index("turn.stopped") < ums[1], types


async def test_cancel_terminal_semantics():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_a = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_a}/messages", json={"text": "部署 nginx"})
        r = await client.post(f"/api/runs/{run_a}/cancel", json={})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "CANCELED"
        # 终态后三个干预端点全部拒绝
        for path in ("messages", "stop", "cancel"):
            r = await client.post(f"/api/runs/{run_a}/{path}", json={"text": "继续"} if path == "messages" else {})
            assert r.status_code == 409, (path, r.text)
            assert r.json() == {"detail": "run_not_active"}, (path, r.text)
        # 事件流以 run.canceled 收尾并正常关闭（后续阶段不再推进）
        resp = await open_stream(client, run_a)
        events, pings = await collect_sse(resp, deadline_s=2.0)
        types = [e["event"] for e in events]
        assert types[-1] == "run.canceled", types
        assert pings == 0, pings

        # 挂起会话同样可关闭
        run_b = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_b}/messages", json={"text": "部署 redis"})
        await wait_status(client, run_b, "WAITING_INPUT")
        r = await client.post(f"/api/runs/{run_b}/cancel", json={})
        assert r.status_code == 200, r.text
        assert (await client.get(f"/api/runs/{run_b}")).json()["status"] == "CANCELED"


async def test_stop_on_waiting_run():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 挂起中无回合可停：无 text 幂等无操作
        run_a = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_a}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_a, "WAITING_INPUT")
        r = await client.post(f"/api/runs/{run_a}/stop", json={})
        assert r.status_code == 200, r.text
        assert (await client.get(f"/api/runs/{run_a}")).json()["status"] == "WAITING_INPUT"
        resp = await open_stream(client, run_a)
        events, _ = await collect_sse(resp, deadline_s=1.0)
        assert [e["event"] for e in events].count("turn.stopped") == 0

        # 带 text 时停止目标已达成（无执行），直接投递
        r = await client.post(f"/api/runs/{run_a}/stop", json={"text": "继续"})
        assert r.status_code == 200, r.text
        await wait_status(client, run_a, "WAITING_INPUT")
        resp = await open_stream(client, run_a)
        events, _ = await collect_sse(resp, deadline_s=1.0)
        types = [e["event"] for e in events]
        assert types.count("user.message") == 2, types
        assert types.count("turn.stopped") == 0, types
        assert types[-1] == "turn.completed", types


async def test_resume_from_canceled_run_carries_session():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_a = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_a}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_a, "WAITING_INPUT")
        await client.post(f"/api/runs/{run_a}/cancel", json={})

        r = await client.post("/api/runs", json={"resume_from": run_a})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["resumed_from"] == run_a
        assert body["status"] == "WAITING_INPUT"
        # 工厂收到源 run 的 SDK 会话 id（来自其回合 Result），而非全新会话
        assert app.state.session_factory.session_ids == [None, "sess_fake_1"]

        # 续接会话照常执行首条指令
        run_b = body["run_id"]
        r = await client.post(f"/api/runs/{run_b}/messages", json={"text": "继续之前的部署"})
        assert r.status_code == 200, r.text
        await wait_status(client, run_b, "WAITING_INPUT")
        resp = await open_stream(client, run_b)
        events, _ = await collect_sse(resp, deadline_s=1.0)
        assert [e["event"] for e in events][-1] == "turn.completed"


async def test_resume_from_failed_run_allowed():
    fail_script = list(DEFAULT_SCRIPT) + [RuntimeError("boom")]
    app = make_app(script=fail_script)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_a = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_a}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_a, "FAILED")
        r = await client.post("/api/runs", json={"resume_from": run_a})
        assert r.status_code == 200, r.text
        assert r.json()["resumed_from"] == run_a
        assert app.state.session_factory.session_ids == [None, "sess_fake_1"]


async def test_resume_from_non_terminal_run_conflicts():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 挂起（非终态）不可续接
        run_a = (await client.post("/api/runs", json={})).json()["run_id"]
        r = await client.post("/api/runs", json={"resume_from": run_a})
        assert r.status_code == 409
        assert r.json() == {"detail": "session_in_use"}
        # 执行中的源不可达 session_in_use：有会话执行时任何新建（含续接）
        # 先撞新建互斥，判定顺序如实呈现
        run_b = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_b}/messages", json={"text": "部署 nginx"})
        r = await client.post("/api/runs", json={"resume_from": run_b})
        assert r.status_code == 409
        assert r.json() == {"detail": "deployment_in_progress"}
        await wait_status(client, run_b, "WAITING_INPUT")


async def test_resume_from_unknown_run_404():
    app = make_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        r = await client.post("/api/runs", json={"resume_from": "run_missing"})
        assert r.status_code == 404


async def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        await fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
