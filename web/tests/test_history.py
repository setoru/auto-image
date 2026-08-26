#!/usr/bin/env python3
"""历史列表与重启重建主缝测试 —— 列表摘要、只读约束、假 transcript 驱动的重建。

缝：同 test_api 的 ASGI 测试客户端；list_sessions / get_session_messages
注入假实现（SDKSessionInfo / SessionMessage 同形的 SimpleNamespace），
不读本机 ~/.claude 的真实 transcript。纯 assert，无 pytest。

运行：python web/tests/test_history.py
"""
import asyncio
import tempfile
import logging
import os
import sys
from types import SimpleNamespace

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.app import create_app  # noqa: E402
from web.fake import DEFAULT_SCRIPT, FakeSessionFactory  # noqa: E402
from web.tests.support import StreamingASGITransport  # noqa: E402
from web.tests.test_api import collect_sse, open_stream, wait_status  # noqa: E402

HEARTBEAT = 0.05

# 与 SDKSessionInfo 同形：重启重建只读这些字段（first_prompt / created_at /
# last_modified / session_id）
def session_info(session_id, first_prompt, created_ms, last_ms=None):
    return SimpleNamespace(
        session_id=session_id,
        summary=first_prompt,
        last_modified=last_ms if last_ms is not None else created_ms + 5_000,
        file_size=1024,
        custom_title=None,
        first_prompt=first_prompt,
        git_branch="feat/web-mvp",
        cwd="/proj",
        tag=None,
        created_at=created_ms,
    )


# 与 SessionMessage 同形：type + 原始 API message dict（role/content）
def msg(mtype, content):
    return SimpleNamespace(
        type=mtype,
        uuid=f"u_{mtype}_{abs(hash(str(content))) % 10**8}",
        session_id="sess",
        message={"role": "user" if mtype == "user" else "assistant", "content": content},
        parent_tool_use_id=None,
        parent_agent_id=None,
    )


# 一段两回合的部署 transcript：首回合走 GUIDE 子 agent 工具调用，
# 第二回合是纯文本追问——覆盖回合边界推导与阶段推导两条映射路径
def deploy_transcript():
    return [
        msg("user", "部署 nginx 1.25 到 server-a"),
        msg("assistant", [{"type": "thinking", "thinking": "先查文档。scope AK HWPFEJ9AB3CDEFGHIJKL 不外泄。"}]),
        msg("assistant", [{"type": "text", "text": "开始生成部署指南。"}]),
        msg("assistant", [{"type": "tool_use", "id": "toolu_01", "name": "Task",
                           "input": {"subagent_type": "deploy-guide", "prompt": "生成指南"}}]),
        msg("user", [{"type": "tool_result", "tool_use_id": "toolu_01", "content": "指南已生成"}]),
        msg("assistant", [{"type": "text", "text": "指南阶段完成，等待安装指令。"}]),
        msg("user", "跳过验证直接打包"),
        msg("assistant", [{"type": "text", "text": "按要求复述风险并继续。"}]),
    ]


def history_app(infos, messages_fn, **kwargs):
    """以假 list_sessions / get_session_messages 装配的应用（重启后形态）。"""
    kwargs.setdefault("residual_cli_scan", lambda: [])  # pgrep 路径由专门测试覆盖
    kwargs.setdefault("scope_config", "/nonexistent-scope.yaml")  # 不载真实凭据（脱敏已知值清单隔离）
    kwargs.setdefault("state_path", tempfile.mkdtemp() + "/state.json")  # 簿记隔离（恢复见 test_state）
    return create_app(
        session_factory=FakeSessionFactory(script=DEFAULT_SCRIPT),
        heartbeat_interval=HEARTBEAT,
        list_sessions_fn=lambda: list(infos),
        get_session_messages_fn=messages_fn,
        **kwargs,
    )


def plain_app(**kwargs):
    """无历史的常规应用（列表摘要等行为测试用）。"""
    kwargs.setdefault("residual_cli_scan", lambda: [])
    kwargs.setdefault("scope_config", "/nonexistent-scope.yaml")
    kwargs.setdefault("state_path", tempfile.mkdtemp() + "/state.json")
    return create_app(
        session_factory=FakeSessionFactory(script=DEFAULT_SCRIPT),
        heartbeat_interval=HEARTBEAT,
        list_sessions_fn=lambda: [],
        **kwargs,
    )


async def test_list_endpoint_returns_summaries_without_clearing_old_runs():
    app = plain_app()
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        empty = (await client.post("/api/runs", json={})).json()["run_id"]
        busy = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{busy}/messages", json={"text": "部署 nginx 1.25 到 server-a"})
        await wait_status(client, busy, "WAITING_INPUT")

        r = await client.get("/api/runs")
        assert r.status_code == 200, r.text
        runs = r.json()["runs"]
        assert [x["run_id"] for x in runs] == [busy, empty], runs  # 新启动的在前
        for key in ("run_id", "status", "stage", "first_prompt", "started_at"):
            assert key in runs[0], key
        assert runs[0]["first_prompt"] == "部署 nginx 1.25 到 server-a"
        assert runs[0]["stage"] == "GUIDE"
        assert runs[1]["first_prompt"] is None  # 空会话尚无任务名

        # 提交新任务不清理旧 run 记录
        third = (await client.post("/api/runs", json={})).json()["run_id"]
        runs = (await client.get("/api/runs")).json()["runs"]
        assert {x["run_id"] for x in runs} == {empty, busy, third}


async def test_rebuild_restores_history_viewable():
    infos = [session_info("11111111-2222-3333-4444-555555555555", "部署 nginx 1.25 到 server-a",
                          created_ms=1_700_000_000_000)]
    app = history_app(infos, lambda sid: deploy_transcript())
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        runs = (await client.get("/api/runs")).json()["runs"]
        assert len(runs) == 1, runs
        run = runs[0]
        assert run["status"] == "ENDED"  # 重启找回的历史：终态，非执行中
        assert run["first_prompt"] == "部署 nginx 1.25 到 server-a"  # 名字来自 SDK first_prompt
        assert run["started_at"] == 1_700_000_000.0
        assert run["stage"] == "GUIDE"
        run_id = run["run_id"]

        # 事件流从 transcript 消息重新映射：run.started 起步、run.ended 收尾
        resp = await open_stream(client, run_id)
        events, pings = await collect_sse(resp, deadline_s=2.0)
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
            "user.message",
            "agent.message",
            "turn.completed",
            "run.ended",
        ], types
        seqs = [int(e["id"]) for e in events]
        assert seqs == list(range(1, len(events) + 1)), seqs
        assert events[1]["data"]["text"] == "部署 nginx 1.25 到 server-a"
        # 回合边界由下一条用户输入推导；汇总取该回合最后一条 agent 文本
        assert "指南阶段完成" in events[7]["data"]["text"]
        assert events[8]["data"]["result"] == events[7]["data"]["text"]
        # transcript 重放同样过脱敏与映射路径
        assert "HWPFEJ9AB3CDEFGHIJKL" not in str(events)
        # 终态重放完毕流正常关闭（只读回放不靠心跳保活）
        assert pings == 0, pings


async def test_rebuilt_run_is_readonly_and_not_auto_retried():
    sid = "11111111-2222-3333-4444-555555555555"
    app = history_app([session_info(sid, "部署 nginx", 1_700_000_000_000)], lambda s: deploy_transcript())
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.get("/api/runs")).json()["runs"][0]["run_id"]
        # 历史 run 无干预入口：stop / messages / cancel 一律 run_not_active
        for path in ("messages", "stop", "cancel"):
            r = await client.post(f"/api/runs/{run_id}/{path}", json={"text": "继续"} if path == "messages" else {})
            assert r.status_code == 409, (path, r.text)
            assert r.json() == {"detail": "run_not_active"}, (path, r.text)
        # 重启前在执行的任务不自动重试：不占执行权，新建不受阻
        r = await client.post("/api/runs", json={})
        assert r.status_code == 200, r.text


async def test_rebuilt_run_serves_as_resume_source():
    sid = "11111111-2222-3333-4444-555555555555"
    app = history_app([session_info(sid, "部署 nginx", 1_700_000_000_000)], lambda s: deploy_transcript())
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        run_id = (await client.get("/api/runs")).json()["runs"][0]["run_id"]
        r = await client.post("/api/runs", json={"resume_from": run_id})
        assert r.status_code == 200, r.text
        assert r.json()["resumed_from"] == run_id
        # 工厂收到重建 run 找回的 SDK 会话 id（transcript 里的 session_id）
        assert app.state.session_factory.session_ids == [sid]

        # 续接会话照常执行首条指令
        new_id = r.json()["run_id"]
        await client.post(f"/api/runs/{new_id}/messages", json={"text": "继续之前的部署"})
        await wait_status(client, new_id, "WAITING_INPUT")
        resp = await open_stream(client, new_id)
        events, _ = await collect_sse(resp, deadline_s=1.0)
        assert [e["event"] for e in events][-1] == "turn.completed"


async def test_rebuild_skips_messageless_and_broken_sessions():
    ok_sid = "11111111-2222-3333-4444-555555555555"
    empty_sid = "99999999-8888-7777-6666-555555555555"
    broken_sid = "77777777-6666-5555-4444-333333333333"
    infos = [
        session_info(ok_sid, "部署 nginx", 1_700_000_000_000),
        session_info(empty_sid, None, 1_700_000_100_000),
        session_info(broken_sid, "损坏会话", 1_700_000_200_000),
    ]

    def messages(sid):
        if sid == broken_sid:
            raise OSError("transcript unreadable")
        return deploy_transcript() if sid == ok_sid else []

    app = history_app(infos, messages)
    transport = StreamingASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        runs = (await client.get("/api/runs")).json()["runs"]
        assert [r["first_prompt"] for r in runs] == ["部署 nginx"], runs  # 空与损坏的都跳过


async def test_residual_cli_processes_warned_not_killed():
    logs = []

    class Capture(logging.Handler):
        def emit(self, record):
            logs.append(record.getMessage())

    logger = logging.getLogger("web")
    handler = Capture()
    logger.addHandler(handler)
    try:
        # 发现残留：写日志告警（不自动杀，由人工处置）
        plain_app(residual_cli_scan=lambda: ["123", "456"])
        assert any("123" in m and "456" in m and "人工" in m for m in logs), logs
        # 无残留：安静启动
        logs.clear()
        plain_app(residual_cli_scan=lambda: [])
        assert logs == [], logs
    finally:
        logger.removeHandler(handler)


async def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        await fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
