#!/usr/bin/env python3
"""自动任务信息主缝回归：原始 SDK 消息经 HTTP/SSE 与任务持久化。"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from web.fake import FakeSessionFactory  # noqa: E402
from web.tests.support import async_client, make_test_app  # noqa: E402
from web.tests.test_api import collect_sse, open_stream, wait_status  # noqa: E402


def task_info(title, software=None, version=None, output_dir=None, meta=None):
    return {
        "title": title,
        "software": software,
        "actual_version": version,
        "output_dir": output_dir,
        "install_meta_file": meta,
    }


def dispatch(agent, info, *, tool_id, tool="Agent", prompt=""):
    header = ("<task_info>\n" + json.dumps(info, ensure_ascii=False)
              + "\n</task_info>\n") if isinstance(info, dict) else str(info or "")
    return {
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use", "id": tool_id, "name": tool,
            "input": {"subagent_type": agent, "prompt": header + prompt,
                      "description": info["title"] if isinstance(info, dict) else "执行部署阶段"},
        }]},
    }


def result(**overrides):
    return {"type": "result", "subtype": "success", "result": "本回合完成", **overrides}


class ScriptFactory(FakeSessionFactory):
    """每个执行回合一份剧本；可在首次阶段落流后暂停两个并行回合。"""

    def __init__(self, scripts, *, pause_guides=False):
        super().__init__(delay=0)
        self._scripts = iter(scripts)
        self.pause_guides = pause_guides
        self.guide_count = 0
        self.first_dispatch = asyncio.Event()
        self.both_guides = asyncio.Event()
        self.release_guides = asyncio.Event()

    def __call__(self, start):
        self.script = next(self._scripts)
        session = super().__call__(start)
        if self.pause_guides:
            receive = session.receive_response

            async def paused():
                first = True
                async for step in receive():
                    yield step
                    if first:
                        first = False
                        self.guide_count += 1
                        self.first_dispatch.set()
                        if self.guide_count == 2:
                            self.both_guides.set()
                        await self.release_guides.wait()

            session.receive_response = paused
        return session


async def new_run(client):
    response = await client.post("/api/runs", json={})
    assert response.status_code == 200, response.text
    return response.json()["run_id"]


async def send(client, run_id, text):
    response = await client.post(f"/api/runs/{run_id}/messages", json={"text": text})
    assert response.status_code == 200, response.text


async def test_parallel_names_and_explicit_meta_reach_api_and_sse():
    scenarios = (
        ("hbase", "2.5.16", "部署 HBase 镜像", "latest", "historical-hbase-install-meta.json"),
        ("spark", "4.3.0", "部署 Spark 镜像", "reference-3.4.4", "historical-spark-install-meta.json"),
    )
    scripts = []
    for software, version, title, directory, filename in scenarios:
        output_dir = f"deploy/{software}/{directory}"
        scripts.append([
            dispatch("deploy-guide", task_info(title, software), tool_id=f"{software}-guide",
                     prompt=f"使用 Huawei Cloud EulerOS 2.0 ARM64，部署 {software} 最新稳定版本"),
            dispatch("deploy-install", task_info(title, software, version, output_dir),
                     tool_id=f"{software}-install"),
            dispatch("deploy-verify", task_info(title, software, version, output_dir,
                     f"{output_dir}/{filename}"), tool_id=f"{software}-verify", tool="Task"),
            result(),
        ])
    factory = ScriptFactory(scripts, pause_guides=True)
    titles = FakeSessionFactory(script=[])
    app = make_test_app(session_factory=factory, title_factory=titles, max_parallel_runs=2)
    roots = app.state.test_root
    for software, _version, _title, directory, filename in scenarios:
        path = roots / "deploy" / software / directory
        path.mkdir(parents=True)
        (path / filename).write_text(json.dumps({
            "instance_id": f"i-{software}", "server_alias": f"{software}-target",
        }))
    other = roots / "deploy/docker/29.9.3"
    other.mkdir(parents=True)
    (other / "docker-install-meta.json").write_text(json.dumps({"instance_id": "i-unrelated"}))
    try:
        async with async_client(app) as client:
            run_ids = [await new_run(client), await new_run(client)]
            await send(client, run_ids[0], "一键部署Apache HBase。使用 Huawei Cloud EulerOS 2.0 ARM64，最新稳定版本")
            await send(client, run_ids[1], "一键部署Apache Spark。使用 Huawei Cloud EulerOS 2.0 ARM64，最新稳定版本")
            await asyncio.wait_for(factory.both_guides.wait(), timeout=3)
            pending = {t["run_id"]: t for t in (await client.get("/api/tasks")).json()["tasks"]}
            assert len(pending) == 2
            for run_id, (_software, _version, title, _directory, _filename) in zip(run_ids, scenarios):
                assert pending[run_id]["name"].startswith(title + " ("), pending[run_id]
                assert pending[run_id]["version"] is None
                assert pending[run_id]["instance_id"] is None
            factory.release_guides.set()
            await asyncio.gather(*(wait_status(client, run_id, "READY") for run_id in run_ids))
            finished = {t["run_id"]: t for t in (await client.get("/api/tasks")).json()["tasks"]}
            for run_id, (software, version, title, directory, _filename) in zip(run_ids, scenarios):
                task = finished[run_id]
                assert task["task_id"] == pending[run_id]["task_id"]
                assert task["name"] == pending[run_id]["name"]
                assert (task["software"], task["version"], task["instance_id"]) == (
                    software, version, f"i-{software}"), task
                assert task["output_dir"] == f"deploy/{software}/{directory}"
                assert task["status"] == "DONE" and task["outcome"] == "success"
                events, _ = await collect_sse(await open_stream(client, run_id),
                    stop=lambda event: event["event"] == "turn.completed")
                stages = [e["data"] for e in events if e["event"] == "stage.changed"]
                assert [s["stage"] for s in stages] == ["GUIDE", "INSTALL", "VERIFY"]
                assert stages[0]["task_info"]["title"] == title
                assert stages[-1]["task_info"]["actual_version"] == version
            assert len(factory.sessions) == 2
            assert len(titles.sessions) == 2  # 既有会话标题调用，自动任务没有额外命名调用。
            recovered = make_test_app(task_dir=roots / "task", artifact_roots={
                "deploy": roots / "deploy", "rpm": roots / "rpm",
            })
            async with async_client(recovered) as restored_client:
                restored = {t["task_id"]: t for t in (await restored_client.get("/api/tasks")).json()["tasks"]}
            assert {t["task_id"]: t["name"] for t in finished.values()} == {
                task_id: t["name"] for task_id, t in restored.items()}
            assert recovered.state.session_factory.sessions == []
            assert recovered.state.title_factory.sessions == []
    finally:
        factory.release_guides.set()


async def test_continuation_reuses_task_and_new_sessions_keep_distinct_identity():
    scripts = [
        [dispatch("deploy-guide", task_info("部署 HBase 镜像", "hbase"), tool_id="first"), result()],
        [dispatch("deploy-archive", task_info("继续归档 HBase 镜像", "hbase", "2.5.16"), tool_id="continued"), result()],
        [dispatch("deploy-install", task_info("安装 Docker", "docker"), tool_id="docker"), result()],
        [dispatch("rpm-build", task_info("构建三个 JavaCPP RPM 包"), tool_id="batch"), result()],
        [dispatch("deploy-guide", "<task_info>{bad JSON}</task_info>", tool_id="invalid",
                  prompt="部署 HBase，系统 Huawei Cloud EulerOS 2.0"), result()],
    ]
    factory = ScriptFactory(scripts)
    titles = FakeSessionFactory(script=[])
    app = make_test_app(session_factory=factory, title_factory=titles)
    async with async_client(app) as client:
        prompts = ("部署 HBase，EulerOS 2.0", "接着做", "帮我安装docker", "继续制作其他三个包", "续跑")
        hbase_run = await new_run(client)
        await send(client, hbase_run, prompts[0])
        await wait_status(client, hbase_run, "READY")
        (first,) = (await client.get("/api/tasks")).json()["tasks"]
        await send(client, hbase_run, prompts[1])
        await wait_status(client, hbase_run, "READY")
        continued = (await client.get("/api/tasks")).json()["tasks"]
        assert len(continued) == 1, continued
        assert continued[0]["task_id"] == first["task_id"]
        assert continued[0]["name"] == first["name"]
        assert continued[0]["created_at"] == first["created_at"]
        assert continued[0]["turn_text"] == prompts[0]
        assert continued[0]["version"] == "2.5.16"
        assert [stage["stage"] for stage in continued[0]["stages"]] == ["GUIDE", "ARCHIVE"]
        runs = [hbase_run]
        for text in prompts[2:]:
            run_id = await new_run(client)
            runs.append(run_id)
            await send(client, run_id, text)
            await wait_status(client, run_id, "READY")
        tasks = {t["run_id"]: t for t in (await client.get("/api/tasks")).json()["tasks"]}
        assert len(tasks) == 4 and len({t["task_id"] for t in tasks.values()}) == 4
        assert all(task["status"] == "DONE" for task in tasks.values())
        docker, batch, invalid = (tasks[run_id] for run_id in runs[1:])
        assert docker["name"].startswith("安装 Docker (") and docker["version"] is None
        assert batch["type"] == "rpm" and batch["name"].startswith("构建三个 JavaCPP RPM 包 (")
        assert batch["software"] is None and batch["version"] is None
        assert invalid["software"] is None and invalid["version"] is None
        assert "EulerOS" not in invalid["name"]
        events, _ = await collect_sse(await open_stream(client, hbase_run), max_pings=1)
        assert len([e for e in events if e["event"] == "turn.completed"]) == 2
        assert [e["data"]["text"] for e in events if e["event"] == "user.message"] == list(prompts[:2])
        events, _ = await collect_sse(await open_stream(client, runs[-1]), max_pings=1)
        stages = [e["data"] for e in events if e["event"] == "stage.changed"]
        assert "task_info" not in stages[-1]
        assert len(factory.sessions) == 5 and len(titles.sessions) == 4


async def test_failed_task_reopens_after_plain_chat_and_keeps_cumulative_usage():
    initial_usage = {"input_tokens": 100, "output_tokens": 50,
                     "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 7}
    continued_usage = {"input_tokens": 140, "output_tokens": 60,
                       "cache_read_input_tokens": 8000, "cache_creation_input_tokens": 12}
    factory = ScriptFactory([
        [dispatch("deploy-guide", task_info("部署 HBase 镜像", "hbase"), tool_id="failed-guide"),
         result(subtype="error_during_execution", is_error=True, result="指南暂未完成", usage=initial_usage)],
        [{"type": "assistant", "message": {"content": [{"type": "text", "text": "已解释当前结果"}]}},
         result(usage={"input_tokens": 120, "output_tokens": 55})],
        [dispatch("deploy-verify", task_info("继续验证 HBase", "hbase", "2.5.16"), tool_id="resumed-verify"),
         result(usage=continued_usage)],
    ])
    titles = FakeSessionFactory(script=[])
    app = make_test_app(session_factory=factory, title_factory=titles)
    try:
        async with async_client(app) as client:
            run_id = await new_run(client)
            await send(client, run_id, "部署 HBase 最新稳定版本")
            await wait_status(client, run_id, "READY")
            (first,) = (await client.get("/api/tasks")).json()["tasks"]
            assert first["status"] == "DONE" and first["outcome"] == "failed"
            await send(client, run_id, "解释一下刚才的结果")
            await wait_status(client, run_id, "READY")
            assert (await client.get("/api/tasks")).json()["tasks"] == [first]

            factory.pause_guides = True
            await send(client, run_id, "继续验证")
            await asyncio.wait_for(factory.first_dispatch.wait(), timeout=3)
            (running,) = (await client.get("/api/tasks")).json()["tasks"]
            assert running["task_id"] == first["task_id"]
            assert running["status"] == "RUNNING" and running["current_stage"] == "VERIFY"
            assert running["outcome"] is None and running["ended_at"] is None
            assert running["name"] == first["name"] and running["created_at"] == first["created_at"]
            assert running["turn_text"] == first["turn_text"]
            assert running["usage"] == first["usage"]
            assert [stage["stage"] for stage in running["stages"]] == ["GUIDE", "VERIFY"]
            assert running["stages"][0]["ended_at"] == first["ended_at"]
            assert running["stages"][-1]["ended_at"] is None

            factory.release_guides.set()
            await wait_status(client, run_id, "READY")
            (finished,) = (await client.get("/api/tasks")).json()["tasks"]
            assert finished["task_id"] == first["task_id"]
            assert finished["status"] == "DONE" and finished["outcome"] == "success"
            assert finished["ended_at"] > first["ended_at"]
            assert finished["version"] == "2.5.16"
            assert finished["usage"] == {"input_tokens": 140, "output_tokens": 60,
                                          "cache_read_tokens": 9000, "cache_creation_tokens": 12}
            path = app.state.test_root / "task" / (finished["task_id"] + ".json")
            assert json.loads(path.read_text()) == finished
            events, _ = await collect_sse(await open_stream(client, run_id), max_pings=1)
            assert len([event for event in events if event["event"] == "turn.failed"]) == 1
            assert len([event for event in events if event["event"] == "turn.completed"]) == 2
            assert [event["data"]["stage"] for event in events if event["event"] == "stage.changed"] == ["GUIDE", "VERIFY"]
            assert len(factory.sessions) == 3 and len(titles.sessions) == 1
    finally:
        factory.release_guides.set()


async def test_manual_task_remains_bound_when_its_session_continues():
    factory = ScriptFactory([
        [dispatch("rpm-guide", task_info("先制作指南", "redis"), tool_id="manual-guide"), result()],
        [dispatch("rpm-build", task_info("继续构建 Redis", "redis", "7.2"), tool_id="manual-build"), result()],
    ])
    titles = FakeSessionFactory(script=[])
    app = make_test_app(session_factory=factory, title_factory=titles)
    async with async_client(app) as client:
        response = await client.post("/api/tasks", json={"software": "redis", "version": "7.2",
                                                        "task_type": "rpm", "ecs_mode": "create"})
        assert response.status_code == 200, response.text
        manual = response.json()
        response = await client.post(f"/api/tasks/{manual['task_id']}/run", json={})
        assert response.status_code == 200, response.text
        run_id = response.json()["run_id"]
        await wait_status(client, run_id, "READY")
        (first,) = (await client.get("/api/tasks")).json()["tasks"]
        await send(client, run_id, "继续构建 Redis RPM 包")
        await wait_status(client, run_id, "READY")
        (continued,) = (await client.get("/api/tasks")).json()["tasks"]
        assert continued["task_id"] == manual["task_id"]
        assert continued["origin"] == "manual" and continued["type"] == "rpm"
        assert continued["run_id"] == run_id
        assert continued["name"] == manual["name"] and continued["created_at"] == manual["created_at"]
        assert continued["spec"] == manual["spec"] and continued["turn_text"] == first["turn_text"]
        assert [stage["stage"] for stage in continued["stages"]] == ["GUIDE", "BUILD"]
        assert continued["status"] == "DONE" and continued["outcome"] == "success"
        assert len(list((app.state.test_root / "task").glob("task-*.json"))) == 1
        assert len(factory.sessions) == 2 and len(titles.sessions) == 1


async def test_restart_continues_first_task_and_preserves_later_legacy_record():
    factory = ScriptFactory([
        [dispatch("deploy-guide", task_info("部署 HBase 镜像", "hbase"), tool_id="before-restart"), result()],
        [dispatch("deploy-verify", task_info("重启后继续验证", "hbase", "2.5.16"), tool_id="after-restart"), result()],
    ])
    app = make_test_app(session_factory=factory)
    root = app.state.test_root
    async with async_client(app) as client:
        run_id = await new_run(client)
        await send(client, run_id, "部署 HBase 镜像")
        await wait_status(client, run_id, "READY")
        (first,) = (await client.get("/api/tasks")).json()["tasks"]
    later = {**first, "task_id": "task-00000000000000-later", "title": "曾经的归档回合",
             "name": "曾经的归档回合 (202611171500)", "created_at": first["created_at"] + 100,
             "updated_at": first["updated_at"] + 100, "ended_at": first["ended_at"] + 100,
             "stages": []}
    later_path = root / "task" / (later["task_id"] + ".json")
    later_path.write_text(json.dumps(later, ensure_ascii=False))
    original_bytes = {path.name: path.read_bytes() for path in (root / "task").glob("task-*.json")}
    restored_titles = FakeSessionFactory(script=[])
    restored = make_test_app(session_factory=factory, title_factory=restored_titles,
        state_path=root / "state.json", task_dir=root / "task",
        artifact_roots={"deploy": root / "deploy", "rpm": root / "rpm"},
        list_sessions_fn=factory.list_sessions, get_session_messages_fn=factory.get_session_messages)
    async with async_client(restored) as client:
        response = await client.get(f"/api/runs/{run_id}")
        assert response.status_code == 200 and response.json()["status"] == "READY", response.text
        tasks = {task["task_id"]: task for task in (await client.get("/api/tasks")).json()["tasks"]}
        assert tasks == {first["task_id"]: first, later["task_id"]: later}
        assert {path.name: path.read_bytes() for path in (root / "task").glob("task-*.json")} == original_bytes
        await send(client, run_id, "继续验证 HBase")
        await wait_status(client, run_id, "READY")
        tasks = {task["task_id"]: task for task in (await client.get("/api/tasks")).json()["tasks"]}
        assert set(tasks) == {first["task_id"], later["task_id"]}
        continued = tasks[first["task_id"]]
        assert continued["name"] == first["name"] and continued["created_at"] == first["created_at"]
        assert continued["turn_text"] == first["turn_text"] and continued["version"] == "2.5.16"
        assert [stage["stage"] for stage in continued["stages"]] == ["GUIDE", "VERIFY"]
        assert continued["status"] == "DONE" and continued["outcome"] == "success"
        assert tasks[later["task_id"]] == later
        assert later_path.read_bytes() == original_bytes[later_path.name]
        assert restored_titles.sessions == []


async def test_fork_creates_its_own_task_only_when_it_executes_a_stage():
    factory = ScriptFactory([
        [dispatch("deploy-guide", task_info("部署 HBase 镜像", "hbase"), tool_id="source-guide"), result()],
        [dispatch("deploy-archive", task_info("归档另一份 HBase 镜像", "hbase", "2.5.16"), tool_id="fork-archive"), result()],
    ])
    app = make_test_app(session_factory=factory)
    async with async_client(app) as client:
        source = await new_run(client)
        await send(client, source, "部署 HBase 镜像")
        await wait_status(client, source, "READY")
        (first,) = (await client.get("/api/tasks")).json()["tasks"]
        response = await client.post(f"/api/runs/{source}/clone", json={})
        assert response.status_code == 200, response.text
        fork = response.json()["run_id"]
        assert fork != source
        assert (await client.get("/api/tasks")).json()["tasks"] == [first]
        await send(client, fork, "归档另一个镜像目标")
        await wait_status(client, fork, "READY")
        tasks = {task["run_id"]: task for task in (await client.get("/api/tasks")).json()["tasks"]}
        assert set(tasks) == {source, fork}
        assert tasks[source] == first
        assert tasks[fork]["task_id"] != first["task_id"]
        assert [stage["stage"] for stage in tasks[fork]["stages"]] == ["ARCHIVE"]


async def test_missing_transcript_does_not_reuse_a_persisted_tasks_run_id():
    factory = ScriptFactory([
        [dispatch("deploy-guide", task_info("部署 HBase 镜像", "hbase", "2.5.16"), tool_id="missing-history"), result()],
        [dispatch("deploy-guide", task_info("部署 nginx 镜像", "nginx", "1.25.3"), tool_id="new-session"), result()],
    ])
    app = make_test_app(session_factory=factory)
    root = app.state.test_root
    async with async_client(app) as client:
        old_run = await new_run(client)
        await send(client, old_run, "部署 HBase 镜像")
        await wait_status(client, old_run, "READY")
        (historical,) = (await client.get("/api/tasks")).json()["tasks"]
    historical_path = root / "task" / (historical["task_id"] + ".json")
    original_bytes = historical_path.read_bytes()
    restored = make_test_app(session_factory=factory, state_path=root / "state.json",
        task_dir=root / "task", artifact_roots={"deploy": root / "deploy", "rpm": root / "rpm"},
        list_sessions_fn=lambda: [], get_session_messages_fn=lambda _session_id: [])
    async with async_client(restored) as client:
        assert (await client.get("/api/runs")).json()["runs"] == []
        assert (await client.get("/api/tasks")).json()["tasks"] == [historical]
        fresh_run = await new_run(client)
        await send(client, fresh_run, "部署 nginx 镜像")
        await wait_status(client, fresh_run, "READY")
        tasks = (await client.get("/api/tasks")).json()["tasks"]
        assert fresh_run != old_run, (fresh_run, old_run, tasks)
        assert len(tasks) == 2, tasks
        by_run = {task["run_id"]: task for task in tasks}
        assert by_run[old_run] == historical
        assert historical_path.read_bytes() == original_bytes
        fresh = by_run[fresh_run]
        assert fresh["task_id"] != historical["task_id"]
        assert fresh["name"].startswith("部署 nginx 镜像 (")
        assert (fresh["software"], fresh["version"]) == ("nginx", "1.25.3")
        assert fresh["status"] == "DONE" and fresh["outcome"] == "success"
        assert len(list((root / "task").glob("task-*.json"))) == 2


async def test_legacy_task_name_is_read_without_reclassification():
    legacy = {
        "task_id": "task-20261117151347-legacy", "run_id": "run_old", "type": "image",
        "name": "镜像 EulerOS 2.0 (202611171513)", "software": "EulerOS", "version": "2.0",
        "confirmed": False, "status": "DONE", "outcome": "success", "stages": [],
        "current_stage": None, "usage": None, "server_alias": None, "instance_id": None,
        "turn_text": "一键部署 HBase，使用 EulerOS 2.0", "created_at": 1794899627.0,
        "updated_at": 1794903000.0, "ended_at": 1794903000.0,
    }
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        (path / (legacy["task_id"] + ".json")).write_text(json.dumps(legacy))
        app = make_test_app(task_dir=path)
        async with async_client(app) as client:
            response = await client.get("/api/tasks")
            assert response.status_code == 200
            assert response.json()["tasks"] == [legacy]
        assert app.state.session_factory.sessions == []
        assert app.state.title_factory.sessions == []


async def test_multi_application_title_can_reference_one_verified_machine():
    """当前 Trivy/Docker 补救回合没有单一软件身份，但有明确已核对机器。"""
    info = task_info("复验 Trivy 和 Docker 镜像", output_dir="deploy/docker/29.9.3",
                     meta="deploy/docker/29.9.3/docker-install-meta.json")
    factory = ScriptFactory([[dispatch("deploy-verify", info, tool_id="dual-software"), result()]])
    app = make_test_app(session_factory=factory)
    path = app.state.test_root / "deploy/docker/29.9.3"
    path.mkdir(parents=True)
    (path / "docker-install-meta.json").write_text(json.dumps({
        "instance_id": "i-dual-deliverable", "server_alias": "dual-target",
    }))
    async with async_client(app) as client:
        run_id = await new_run(client)
        await send(client, run_id, "帮我修改下，使用最新的密码复杂度规则处理")
        await wait_status(client, run_id, "READY")
        (task,) = (await client.get("/api/tasks")).json()["tasks"]
        assert task["name"].startswith(info["title"] + " (")
        assert task["software"] is None and task["version"] is None
        assert (task["instance_id"], task["server_alias"]) == ("i-dual-deliverable", "dual-target")


async def test_invalid_meta_path_keeps_task_persistence_and_recovery():
    info = task_info("部署 HBase 镜像", "hbase", meta="deploy/hbase/\0meta.json")
    factory = ScriptFactory([[dispatch("deploy-install", info, tool_id="invalid-path"), result()]])
    app = make_test_app(session_factory=factory)
    async with async_client(app) as client:
        run_id = await new_run(client)
        await send(client, run_id, "继续部署 HBase")
        await wait_status(client, run_id, "READY")
        (task,) = (await client.get("/api/tasks")).json()["tasks"]
        assert task["status"] == "DONE" and task["outcome"] == "success"
        assert task["instance_id"] is None and task["server_alias"] is None
        path = app.state.test_root / "task" / (task["task_id"] + ".json")
        assert json.loads(path.read_text()) == task
        restored = make_test_app(task_dir=path.parent)
        async with async_client(restored) as restored_client:
            assert (await restored_client.get("/api/tasks")).json()["tasks"] == [task]


if __name__ == "__main__":
    async def main():
        for test in (test_parallel_names_and_explicit_meta_reach_api_and_sse,
                     test_continuation_reuses_task_and_new_sessions_keep_distinct_identity,
                     test_failed_task_reopens_after_plain_chat_and_keeps_cumulative_usage,
                     test_manual_task_remains_bound_when_its_session_continues,
                     test_restart_continues_first_task_and_preserves_later_legacy_record,
                     test_fork_creates_its_own_task_only_when_it_executes_a_stage,
                     test_missing_transcript_does_not_reuse_a_persisted_tasks_run_id,
                     test_legacy_task_name_is_read_without_reclassification,
                     test_multi_application_title_can_reference_one_verified_machine,
                     test_invalid_meta_path_keeps_task_persistence_and_recovery):
            await test()
            print(f"PASS {test.__name__}")
    asyncio.run(main())
