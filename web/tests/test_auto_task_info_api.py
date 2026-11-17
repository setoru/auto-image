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


def result():
    return {"type": "result", "subtype": "success", "result": "本回合完成"}


class ScriptFactory(FakeSessionFactory):
    """每个执行回合一份剧本；可在首次阶段落流后暂停两个并行回合。"""

    def __init__(self, scripts, *, pause_guides=False):
        super().__init__(delay=0)
        self._scripts = iter(scripts)
        self.pause_guides = pause_guides
        self.guide_count = 0
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


async def test_continuation_batch_and_invalid_metadata_are_per_turn():
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
        run_id = await new_run(client)
        prompts = ("部署 HBase，EulerOS 2.0", "接着做", "帮我安装docker", "继续制作其他三个包", "续跑")
        for text in prompts:
            await send(client, run_id, text)
            await wait_status(client, run_id, "READY")
        tasks = sorted((await client.get("/api/tasks")).json()["tasks"], key=lambda t: t["created_at"])
        assert len(tasks) == 5 and len({t["task_id"] for t in tasks}) == 5
        assert [t["turn_text"] for t in tasks] == list(prompts)
        assert all(t["run_id"] == run_id and t["status"] == "DONE" for t in tasks)
        assert tasks[0]["name"].startswith("部署 HBase 镜像 (")
        assert tasks[1]["name"].startswith("继续归档 HBase 镜像 (")
        assert tasks[1]["version"] == "2.5.16"
        assert tasks[2]["name"].startswith("安装 Docker (") and tasks[2]["version"] is None
        assert tasks[3]["type"] == "rpm" and tasks[3]["name"].startswith("构建三个 JavaCPP RPM 包 (")
        assert tasks[3]["software"] is None and tasks[3]["version"] is None
        assert tasks[4]["software"] is None and tasks[4]["version"] is None
        assert "EulerOS" not in tasks[4]["name"]
        events, _ = await collect_sse(await open_stream(client, run_id), max_pings=1)
        assert len([e for e in events if e["event"] == "turn.completed"]) == 5
        stages = [e["data"] for e in events if e["event"] == "stage.changed"]
        assert "task_info" not in stages[-1]
        assert len(factory.sessions) == 5 and len(titles.sessions) == 1


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
                     test_continuation_batch_and_invalid_metadata_are_per_turn,
                     test_legacy_task_name_is_read_without_reclassification,
                     test_multi_application_title_can_reference_one_verified_machine,
                     test_invalid_meta_path_keeps_task_persistence_and_recovery):
            await test()
            print(f"PASS {test.__name__}")
    asyncio.run(main())
