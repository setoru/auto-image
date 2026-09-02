"""落盘簿记与重启恢复：挂起会话恢复为可聊（原 run_id、send 时 resume 起新回合）、
RUNNING 降级 + turn.interrupted 提示、transcript 读不到丢弃、恢复占用不与
历史重建重复、id 计数续号、簿记损坏降级。"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from web.app import create_app  # noqa: E402
from web.fake import DEFAULT_SCRIPT, FakeSessionFactory  # noqa: E402
from web.runs import READY, RunManager  # noqa: E402
from web.state import load_state, save_state  # noqa: E402
from web.tests.support import StreamingASGITransport  # noqa: E402
from web.tests.test_api import collect_sse, open_stream, wait_status  # noqa: E402
from web.tests.test_history import session_info  # noqa: E402

HEARTBEAT = 0.05


def tmsg(mtype, content):
    """与 SessionMessage 同形的 transcript 桩条目。"""
    return SimpleNamespace(
        type=mtype,
        message={"role": "user" if mtype == "user" else "assistant", "content": content},
    )


def two_turn_transcript():
    return [
        tmsg("user", "部署 nginx"),
        tmsg("assistant", [{"type": "text", "text": "完成。"}]),
        tmsg("user", "为什么成功"),
        tmsg("assistant", [{"type": "text", "text": "因为流程正确。"}]),
    ]


def write_state(path, records):
    path.write_text(json.dumps({"runs": records}, ensure_ascii=False), encoding="utf-8")


def suspended_record(run_id="run_1", session_id="sess_x", status="READY"):
    return {
        "run_id": run_id, "status": status, "stage": None,
        "first_prompt": "部署 nginx", "created_at": 1000.0,
        "session_id": session_id, "resumed_from": None,
    }


def restore_app(state_path, infos, transcripts, factory=None):
    """簿记 + 假 transcript 装配的重启后应用。"""
    def get_messages(sid):
        if sid not in transcripts:
            raise FileNotFoundError(sid)
        return transcripts[sid]

    return create_app(
        session_factory=factory or FakeSessionFactory(script=DEFAULT_SCRIPT),
        heartbeat_interval=HEARTBEAT,
        list_sessions_fn=lambda: list(infos),
        get_session_messages_fn=get_messages,
        residual_cli_scan=lambda: [],
        scope_config="/nonexistent-scope.yaml",
        state_path=state_path,
    )


async def test_save_load_roundtrip_and_filters():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "state.json"
        manager = RunManager()
        live = manager.create()           # 挂起、有 session：入册
        live.status = READY
        live.session_id = "sess_live"
        live.first_prompt = "部署 nginx"
        no_session = manager.create()     # 首回合未完成：不入册
        no_session.status = READY
        save_state(manager.runs.values(), path)
        records = load_state(path)
        assert [r["run_id"] for r in records] == [live.run_id], records
        assert records[0]["session_id"] == "sess_live"

        (Path(d) / "corrupt.json").write_text("not json{", encoding="utf-8")
        assert load_state(Path(d) / "corrupt.json") == []
        (Path(d) / "badshape.json").write_text(json.dumps({"runs": [42, {"run_id": "x"}]}), encoding="utf-8")
        assert load_state(Path(d) / "badshape.json") == []


async def test_restart_restores_suspended_run_chattable():
    with tempfile.TemporaryDirectory() as d:
        state_path = str(Path(d) / "state.json")
        # 第一个进程：建会话、跑完一回合（fake Result 注入 session_id）后中止
        # ——模拟服务重启，内存记录丢失、簿记与 transcript 留存
        app_a = create_app(
            session_factory=FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.02),
            heartbeat_interval=HEARTBEAT,
            list_sessions_fn=lambda: [],
            get_session_messages_fn=lambda sid: [],
            residual_cli_scan=lambda: [],
            scope_config="/nonexistent-scope.yaml",
            state_path=state_path,
        )
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app_a), base_url="http://testserver") as client:
            run_id = (await client.post("/api/runs", json={})).json()["run_id"]
            await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
            await wait_status(client, run_id, "READY")
        records = load_state(state_path)
        assert [r["run_id"] for r in records] == [run_id], records
        sid = records[0]["session_id"]
        assert sid

        # 第二个进程（重启后形态）：原 run_id 直接恢复、可发消息继续聊
        factory_b = FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.02)
        app_b = restore_app(state_path, [session_info(sid, "部署 nginx", 1000)], {sid: two_turn_transcript()}, factory=factory_b)
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app_b), base_url="http://testserver") as client:
            runs = (await client.get("/api/runs")).json()["runs"]
            assert [r["run_id"] for r in runs] == [run_id], runs  # 原条目，无重复
            assert runs[0]["status"] == "READY"
            events, _ = await collect_sse(await open_stream(client, run_id), deadline_s=1.0)
            types = [e["event"] for e in events]
            assert "user.message" in types and "turn.completed" in types, types
            assert "session.ended" not in types  # 历史重建的只读收尾不出现（恢复路径）
            r = await client.post(f"/api/runs/{run_id}/messages", json={"text": "继续"})
            assert r.status_code == 200, r.text
            await wait_status(client, run_id, "READY")
        assert factory_b.session_ids[0] == sid  # 协程以原 session 续接重建连接


async def test_restart_running_record_downgrades_with_interrupted_event():
    with tempfile.TemporaryDirectory() as d:
        state_path = Path(d) / "state.json"
        write_state(state_path, [suspended_record(status="RUNNING")])
        app = restore_app(str(state_path), [session_info("sess_x", "部署 nginx", 1000)], {"sess_x": two_turn_transcript()})
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app), base_url="http://testserver") as client:
            runs = (await client.get("/api/runs")).json()["runs"]
            assert runs[0]["status"] == "READY", runs  # 未收尾回合不重跑
            events, _ = await collect_sse(await open_stream(client, "run_1"), deadline_s=1.0)
            assert [e["event"] for e in events][-1] == "turn.interrupted"


async def test_restart_drops_record_without_transcript():
    with tempfile.TemporaryDirectory() as d:
        state_path = Path(d) / "state.json"
        write_state(state_path, [suspended_record()])
        app = restore_app(str(state_path), [], {})  # transcript 读不到
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app), base_url="http://testserver") as client:
            assert (await client.get("/api/runs")).json()["runs"] == []


async def test_restored_session_not_duplicated_by_rebuild():
    with tempfile.TemporaryDirectory() as d:
        state_path = Path(d) / "state.json"
        write_state(state_path, [suspended_record(session_id="sess_x")])
        infos = [session_info("sess_x", "部署 nginx", 1000), session_info("sess_z", "老任务", 500)]
        app = restore_app(str(state_path), infos, {"sess_x": two_turn_transcript(), "sess_z": two_turn_transcript()})
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app), base_url="http://testserver") as client:
            runs = (await client.get("/api/runs")).json()["runs"]
            # 恢复占用 sess_x 不再出 run_hist 条目；其余 transcript 照旧重建
            assert [r["run_id"] for r in runs] == ["run_1", "run_hist_sess_z"], runs


async def test_restore_then_new_run_id_continues():
    with tempfile.TemporaryDirectory() as d:
        state_path = Path(d) / "state.json"
        write_state(state_path, [suspended_record(run_id="run_2")])
        app = restore_app(str(state_path), [session_info("sess_x", "部署 nginx", 1000)], {"sess_x": two_turn_transcript()})
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app), base_url="http://testserver") as client:
            new_id = (await client.post("/api/runs", json={})).json()["run_id"]
            assert new_id == "run_3", new_id  # 计数器前拨过已恢复的 run_2


async def test_corrupt_state_degrades_to_rebuild_only():
    with tempfile.TemporaryDirectory() as d:
        state_path = Path(d) / "state.json"
        state_path.write_text("garbage{", encoding="utf-8")
        app = restore_app(str(state_path), [session_info("sess_z", "老任务", 500)], {"sess_z": two_turn_transcript()})
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app), base_url="http://testserver") as client:
            runs = (await client.get("/api/runs")).json()["runs"]
            assert [r["run_id"] for r in runs] == ["run_hist_sess_z"], runs  # 启动不炸，降级纯重建


async def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        await fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
