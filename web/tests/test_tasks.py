#!/usr/bin/env python3
"""TaskStore 状态机单测 —— 创建/类型分流/阶段推进/收尾/产物确认/恢复。

直接以完整事件 dict 驱动 handle_event（与 EventStore 观察者同形），
不触 HTTP、不触云。纯 assert，无 pytest。

运行：python web/tests/test_tasks.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.tasks import TaskStore, build_task_prompt  # noqa: E402


def ev(run_id, etype, payload, ts):
    return {"seq": 1, "ts": ts, "run_id": run_id, "type": etype, "payload": payload}


def make_store(task_dir, roots=None):
    return TaskStore(Path(task_dir), roots or {})


def task_info(title, software=None, version=None, output_dir=None, meta=None):
    return {"title": title, "software": software, "actual_version": version,
            "output_dir": output_dir, "install_meta_file": meta}


def stage_event(run_id, stage, subagent, ts, info=None):
    payload = {"stage": stage, "status": "running", "subagent": subagent}
    if info is not None:
        payload["task_info"] = info
    return ev(run_id, "stage.changed", payload, ts)


def test_create_image_task_from_deploy_guide():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        t0, t1 = 1_700_000_000.0, 1_700_000_030.0
        store.handle_event(ev("r1", "user.message", {"text": "一键部署 nginx，版本 1.25.3"}, t0))
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", t1,
                                      task_info("部署 nginx 镜像", "nginx")))
        (task,) = store.list()
        assert task["type"] == "image" and task["origin"] == "auto"
        assert task["status"] == "RUNNING"
        assert task["software"] == "nginx" and task["version"] is None, task
        assert task["name"].startswith("部署 nginx 镜像 ("), task["name"]
        assert task["stages"] == [{"stage": "GUIDE", "started_at": t1, "ended_at": None}]
        assert task["current_stage"] == "GUIDE"
        files = list(Path(td).glob("task-*.json"))
        assert len(files) == 1 and json.loads(files[0].read_text())["task_id"] == task["task_id"]

def test_create_rpm_task_from_rpm_build():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        store.handle_event(ev("r1", "user.message", {"text": "制作 redis 7.2 的 RPM 包"}, 1.0))
        store.handle_event(stage_event("r1", "BUILD", "rpm-build", 2.0,
                                      task_info("制作 redis RPM 包", "redis", "7.2")))
        (task,) = store.list()
        assert task["type"] == "rpm" and task["software"] == "redis"
        assert task["stages"][0]["stage"] == "BUILD"

def test_stage_progression_and_dedupe():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        base = 1_700_000_000.0
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", base))
        # 相邻同阶段重派（guide 重试）：不新增条目
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", base + 10))
        task = store.list()[0]
        assert len(task["stages"]) == 1
        assert task["stages"][0]["ended_at"] is None
        # 阶段推进：关上 GUIDE，开 INSTALL
        store.handle_event(stage_event("r1", "INSTALL", "deploy-install", base + 20))
        task = store.list()[0]
        assert [s["stage"] for s in task["stages"]] == ["GUIDE", "INSTALL"]
        assert task["stages"][0]["ended_at"] == base + 20
        assert task["current_stage"] == "INSTALL"
        # 非相邻重入（INSTALL 后回炉 GUIDE）：合法追加第二条 GUIDE
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", base + 30))
        task = store.list()[0]
        assert [s["stage"] for s in task["stages"]] == ["GUIDE", "INSTALL", "GUIDE"]


def test_turn_end_outcomes_and_usage():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        base = 1_700_000_000.0
        usage = {"input_tokens": 100, "output_tokens": 50,
                 "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 7}
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", base))
        store.handle_event(ev("r1", "turn.completed", {"result": "ok", "usage": usage}, base + 60))
        task = store.list()[0]
        assert task["status"] == "DONE" and task["outcome"] == "success"
        assert task["current_stage"] is None and task["ended_at"] == base + 60
        assert task["stages"][-1]["ended_at"] == base + 60
        assert task["usage"] == {"input_tokens": 100, "output_tokens": 50,
                                 "cache_read_tokens": 9000, "cache_creation_tokens": 7}

    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        base = 1_700_000_100.0
        store.handle_event(stage_event("r1", "GUIDE", "rpm-guide", base))
        store.handle_event(ev("r1", "turn.failed", {"message": "boom"}, base + 5))
        task = store.list()[0]
        assert task["outcome"] == "failed"

    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        base = 1_700_000_200.0
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", base))
        store.handle_event(ev("r1", "turn.stopped", {}, base + 5))
        assert store.list()[0]["outcome"] == "stopped"


def test_session_ended_mid_task_interrupts():
    """RUNNING 中结束会话：回合被取消且不补收尾事件，session.ended 收尾。"""
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        base = 1_700_000_000.0
        store.handle_event(stage_event("r1", "INSTALL", "deploy-install", base))
        store.handle_event(ev("r1", "session.ended", {}, base + 5))
        task = store.list()[0]
        assert task["status"] == "DONE" and task["outcome"] == "interrupted"
        # 重复结束会话不改写已收尾任务。
        ended = json.loads(json.dumps(task))
        store.handle_event(ev("r1", "session.ended", {}, base + 10))
        assert store.list() == [ended]


def test_plain_and_continuation_turns_reuse_task():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        base = 1_700_000_000.0
        store.handle_event(ev("r1", "user.message", {"text": "你好"}, base))
        store.handle_event(ev("r1", "turn.completed", {"result": "ok"}, base + 1))
        assert store.list() == []
        store.handle_event(ev("r1", "user.message", {"text": "部署 nginx 1.25"}, base + 10))
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", base + 11,
                                      task_info("部署 nginx 镜像", "nginx")))
        store.handle_event(ev("r1", "turn.completed", {"usage": {"input_tokens": 100,
                                                                "output_tokens": 50}}, base + 20))
        (task,) = store.list()
        first = json.loads(json.dumps(task))
        # 普通问答不重开任务，也不覆盖上轮流水线结果。
        store.handle_event(ev("r1", "user.message", {"text": "解释一下结果"}, base + 21))
        store.handle_event(ev("r1", "turn.failed", {"usage": {"input_tokens": 200}}, base + 22))
        assert store.list() == [first]
        store.handle_event(ev("r1", "user.message", {"text": "继续归档"}, base + 30))
        store.handle_event(stage_event("r1", "ARCHIVE", "deploy-archive", base + 31,
                                      task_info("继续归档 nginx", "nginx", "1.25")))
        assert len(store.list()) == 1
        assert task["task_id"] == first["task_id"]
        assert task["status"] == "RUNNING" and task["outcome"] is None and task["ended_at"] is None
        assert task["current_stage"] == "ARCHIVE"
        for key in ("name", "title", "created_at", "turn_text", "type"):
            assert task[key] == first[key], key
        assert task["version"] == "1.25"
        assert task["usage"] == first["usage"]
        store.handle_event(ev("r1", "turn.completed", {"usage": {"input_tokens": 250,
                                                                "output_tokens": 40}}, base + 40))
        assert task["status"] == "DONE" and task["outcome"] == "success"
        assert task["ended_at"] == base + 40 and task["current_stage"] is None
        assert task["stages"] == [
            {"stage": "GUIDE", "started_at": base + 11, "ended_at": base + 20},
            {"stage": "ARCHIVE", "started_at": base + 31, "ended_at": base + 40},
        ]
        assert task["usage"]["input_tokens"] == 250 and task["usage"]["output_tokens"] == 50
        completed = json.loads(json.dumps(task))
        store.handle_event(ev("r1", "turn.completed", {"usage": {"input_tokens": 999}}, base + 41))
        assert store.list() == [completed]
        files = list(Path(td).glob("task-*.json"))
        assert len(files) == 1 and json.loads(files[0].read_text()) == task


def test_failed_and_stopped_turns_reuse_task():
    for ending in ("turn.failed", "turn.stopped"):
        with tempfile.TemporaryDirectory() as td:
            store = make_store(td)
            base = 1_700_000_000.0
            store.handle_event(stage_event("r1", "VERIFY", "deploy-verify", base,
                                          task_info("验证 nginx 镜像", "nginx")))
            (task,) = store.list()
            task_id, name = task["task_id"], task["name"]
            store.handle_event(ev("r1", ending, {}, base + 1))
            store.handle_event(ev("r1", "user.message", {"text": "修正后重新验证"}, base + 10))
            store.handle_event(stage_event("r1", "VERIFY", "deploy-verify", base + 11,
                                          task_info("重新验证 nginx", "nginx", "1.25")))
            assert len(store.list()) == 1 and task["task_id"] == task_id and task["name"] == name
            assert task["status"] == "RUNNING" and task["outcome"] is None and task["ended_at"] is None
            # 上一回合的 VERIFY 已结束，因此这次重试追加阶段记录。
            assert len(task["stages"]) == 2
            store.handle_event(ev("r1", "turn.completed", {}, base + 12))
            assert task["outcome"] == "success" and task["stages"][-1]["ended_at"] == base + 12


def test_new_session_creates_distinct_task():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        for offset, run_id in enumerate(("r1", "forked-r2")):
            store.handle_event(stage_event(run_id, "GUIDE", "deploy-guide", 100.0 + offset,
                                          task_info("部署 nginx 镜像", "nginx")))
        tasks = store.list()
        assert len(tasks) == 2 and len({task["task_id"] for task in tasks}) == 2
        assert {task["run_id"] for task in tasks} == {"r1", "forked-r2"}

def test_explicit_historical_meta_path_supplies_machine():
    with tempfile.TemporaryDirectory() as td:
        deploy = Path(td) / "roots/deploy"
        directory = deploy / "nginx/1.25.3"
        directory.mkdir(parents=True)
        (directory / "old-alias-install-meta.json").write_text(json.dumps({
            "path": "create", "server_alias": "nginx-2026091414",
            "instance_id": "i-123", "auth_method": "password",
        }))
        store = make_store(Path(td) / "task", {"deploy": deploy})
        base = time.time()
        store.handle_event(ev("r1", "user.message", {"text": "部署 nginx"}, base))
        store.handle_event(stage_event("r1", "INSTALL", "deploy-install", base + 1,
                                      task_info("部署 nginx 1.25.3", "nginx", "1.25.3",
                                                "deploy/nginx/1.25.3",
                                                "deploy/nginx/1.25.3/old-alias-install-meta.json")))
        task = store.list()[0]
        assert task["confirmed"] is True
        assert task["software"] == "nginx" and task["version"] == "1.25.3"
        assert task["instance_id"] == "i-123" and task["server_alias"] == "nginx-2026091414"
        assert "nginx 1.25.3" in task["name"]

def test_explicit_machine_survives_unrelated_newer_outputs():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        selected = root / "deploy/nginx/1.3.0"
        selected.mkdir(parents=True)
        (selected / "nginx-install-meta.json").write_text(json.dumps({
            "instance_id": "i-selected", "server_alias": "selected",
        }))
        store = make_store(root / "task", {"deploy": root / "deploy"})
        now = time.time()
        store.handle_event(ev("r1", "user.message", {"text": "部署 nginx 1.3.0"}, now))
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", now,
                                      task_info("部署 nginx 镜像", "nginx")))
        task = store.list()[0]
        name = task["name"]
        assert task["instance_id"] is None
        for software, version in (("nginx", "1.4.0"), ("redis", "7.2")):
            other = root / "deploy" / software / version
            other.mkdir(parents=True)
            (other / f"{software}-install-meta.json").write_text(json.dumps({"instance_id": "i-wrong"}))
            (other / f"{software}-install.md").write_text("new output")
        store.handle_event(stage_event("r1", "INSTALL", "deploy-install", now + 2,
                                      task_info("阶段标题不替换整体标题", "nginx", "1.3.0",
                                                "deploy/nginx/1.3.0",
                                                "deploy/nginx/1.3.0/nginx-install-meta.json")))
        task = store.list()[0]
        assert (task["software"], task["version"], task["instance_id"], task["server_alias"]) == (
            "nginx", "1.3.0", "i-selected", "selected"), task
        assert task["name"] == name and task["created_at"] == now
        saved = json.loads((root / "task" / f"{task['task_id']}.json").read_text())
        assert saved["run_id"] == "r1" and saved["title"] == "部署 nginx 镜像"

def test_missing_explicit_meta_does_not_switch_to_other_directory():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for software, version in (("nginx", "1.4.0"), ("redis", "7.2")):
            other = root / "deploy" / software / version
            other.mkdir(parents=True)
            (other / f"{software}-install-meta.json").write_text(json.dumps({"instance_id": "i-wrong"}))
        store = make_store(root / "task", {"deploy": root / "deploy"})
        now = time.time()
        store.handle_event(ev("r1", "user.message", {"text": "部署 nginx 1.3.0"}, now))
        store.handle_event(stage_event("r1", "INSTALL", "deploy-install", now + 1,
                                      task_info("部署 nginx 镜像", "nginx",
                                                output_dir="deploy/nginx/1.3.0",
                                                meta="deploy/nginx/1.3.0/nginx-install-meta.json")))
        task = store.list()[0]
        assert (task["software"], task["version"]) == ("nginx", None), task
        assert task["instance_id"] is None and not task["confirmed"], task

def test_missing_metadata_does_not_guess_from_any_outputs():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        now = time.time()
        for software, version in (("nginx", "1.3.0"), ("redis", "7.2")):
            directory = root / "deploy" / software / version
            directory.mkdir(parents=True)
            for filename in (f"{software}-summary.md", f"{software}-state.json", "config.json",
                             "manual.pdf", f"{software}-install.md.tmp", ".deploy-summary.tmp"):
                (directory / filename).write_text("supplemental material")
            old_guide = directory / f"{software}-install.md"
            old_guide.write_text("old stage output")
            os.utime(old_guide, (now - 1000, now - 1000))
        store = make_store(root / "task", {"deploy": root / "deploy"})
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", now))
        assert store.list()[0]["software"] is None
        current = root / "deploy/nginx/1.3.0/nginx-verify.md"
        current.write_text("current stage output")
        store.handle_event(stage_event("r1", "INSTALL", "deploy-install", now + 1))
        task = store.list()[0]
        assert task["software"] is None and task["version"] is None and not task["confirmed"], task

def test_ambiguous_stage_outputs_are_not_selected_by_mtime():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for software, version in (("nginx", "1.3.0"), ("redis", "7.2")):
            directory = root / "deploy" / software / version
            directory.mkdir(parents=True)
            (directory / f"{software}-install.md").write_text("current stage output")
        store = make_store(root / "task", {"deploy": root / "deploy"})
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", time.time()))
        task = store.list()[0]
        assert task["software"] is None and task["version"] is None and not task["confirmed"], task


def test_explicit_identity_ignores_requested_and_directory_versions():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        directory = root / "deploy/nodejs/latest"
        directory.mkdir(parents=True)
        (directory / "nodejs-install-meta.json").write_text(json.dumps({"instance_id": "i-node"}))
        store = make_store(root / "task", {"deploy": root / "deploy"})
        now = time.time()
        store.handle_event(ev("r1", "user.message", {"text": "部署 Node.js 20"}, now))
        store.handle_event(stage_event("r1", "INSTALL", "deploy-install", now + 1,
                                      task_info("部署 Node.js 镜像", "nodejs", "20.18.1",
                                                "deploy/nodejs/latest",
                                                "deploy/nodejs/latest/nodejs-install-meta.json")))
        task = store.list()[0]
        assert task["confirmed"] and (task["software"], task["version"]) == ("nodejs", "20.18.1"), task
        assert task["instance_id"] == "i-node", task

def test_environment_and_old_meta_cannot_supply_machine_at_guide():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        now = time.time()
        old_directory = root / "deploy/Ubuntu/22.04"
        old_directory.mkdir(parents=True)
        (old_directory / "Ubuntu-install-meta.json").write_text(json.dumps({
            "instance_id": "i-unrelated", "server_alias": "unrelated",
        }))
        directory = root / "deploy/nginx/1.25.3"
        directory.mkdir(parents=True)
        (directory / "nginx-install-meta.json").write_text(json.dumps({
            "instance_id": "i-nginx", "server_alias": "nginx-target",
        }))
        store = make_store(root / "task", {"deploy": root / "deploy"})
        store.handle_event(ev("r1", "user.message", {"text": "在 Ubuntu 22.04 上部署 nginx 1.25.3"}, now))
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", now + 1,
                                      task_info("部署 nginx 镜像", "nginx")))
        task = store.list()[0]
        assert task["software"] == "nginx" and task["version"] is None
        assert task["instance_id"] is None
        store.handle_event(stage_event("r1", "INSTALL", "deploy-install", now + 2,
                                      task_info("部署 nginx 镜像", "nginx", "1.25.3",
                                                "deploy/nginx/1.25.3",
                                                "deploy/nginx/1.25.3/nginx-install-meta.json")))
        assert (task["software"], task["version"], task["instance_id"], task["server_alias"]) == (
            "nginx", "1.25.3", "i-nginx", "nginx-target"), task

def test_ambiguous_stage_directories_do_not_use_raw_input_to_pick_a_machine():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        now = time.time()
        for software in ("nginx", "redis"):
            directory = root / "deploy" / software / "1.3.0"
            directory.mkdir(parents=True)
            (directory / f"{software}-install.md").write_text("current stage output")
            (directory / f"{software}-install-meta.json").write_text(json.dumps({"instance_id": f"i-{software}"}))
        store = make_store(root / "task", {"deploy": root / "deploy"})
        store.handle_event(ev("r1", "user.message", {"text": "部署 nginx 1.3.0"}, now))
        store.handle_event(stage_event("r1", "INSTALL", "deploy-install", now + 1))
        store.handle_event(ev("r1", "turn.completed", {"result": "执行完成"}, now + 2))
        task = store.list()[0]
        assert not task["confirmed"] and task["instance_id"] is None and task["server_alias"] is None, task
        assert task["status"] == "DONE" and task["outcome"] == "success", task


def test_image_task_does_not_read_rpm_meta():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        rpm = root / "rpm/nginx/1.3.0"
        rpm.mkdir(parents=True)
        (rpm / "nginx-install-meta.json").write_text(json.dumps({"instance_id": "i-rpm"}))
        store = make_store(root / "task", {"deploy": root / "deploy", "rpm": root / "rpm"})
        store.handle_event(stage_event("r1", "INSTALL", "deploy-install", time.time(),
                                      task_info("部署 nginx 镜像", "nginx", "1.3.0",
                                                "rpm/nginx/1.3.0",
                                                "rpm/nginx/1.3.0/nginx-install-meta.json")))
        assert store.list()[0]["instance_id"] is None, store.list()

def test_rpm_identity_comes_from_metadata_with_nested_outputs():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        packages = root / "rpm/nginx/1.3.0/rpms/binary"
        packages.mkdir(parents=True)
        (packages / "nginx-1.3.0.aarch64.rpm").write_bytes(b"package")
        store = make_store(root / "task", {"rpm": root / "rpm"})
        store.handle_event(stage_event("r1", "BUILD", "rpm-build", time.time(),
                                      task_info("制作 nginx RPM 包", "nginx", "1.3.0", "rpm/nginx/1.3.0")))
        task = store.list()[0]
        assert task["type"] == "rpm" and (task["software"], task["version"]) == ("nginx", "1.3.0"), task

def test_summary_turn_does_not_create_task():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        store.handle_event(ev("r1", "user.message", {"text": "/deploy-summary nginx 1.3.0"}, 1))
        store.handle_event(ev("r1", "turn.completed", {"result": "总结已保存"}, 2))
        assert store.list() == []


def test_manual_instance_id_is_preserved_when_old_meta_conflicts():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        directory = root / "deploy/nginx/1.3.0"
        directory.mkdir(parents=True)
        (directory / "nginx-install-meta.json").write_text(json.dumps({
            "instance_id": "i-old", "server_alias": "old-target",
        }))
        store = make_store(root / "task", {"deploy": root / "deploy"})
        task = store.create_manual({"software": "nginx", "version": "1.3.0", "ecs_mode": "existing",
                                    "ecs_instance": {"id": "i-current", "name": "external-name", "ip": "203.0.113.10"}})
        store.mark_running(task["task_id"], "r1", "部署 nginx 1.3.0")
        store.handle_event(stage_event("r1", "INSTALL", "deploy-install", time.time()))
        assert task["instance_id"] == "i-current" and task["server_alias"] is None, task


def test_recover_ignores_extra_identity_fields_from_prior_task_format():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        task = store.create_manual({"software": "nginx", "version": "1.3.0"})
        path = Path(td) / f"{task['task_id']}.json"
        prior = json.loads(path.read_text())
        prior.update(call_id="legacy-call", production_id="legacy-production")
        path.write_text(json.dumps(prior))
        recovered = make_store(td)
        recovered.recover()
        assert recovered.mark_running(task["task_id"], "r1", "部署 nginx 1.3.0") is not None
        recovered.handle_event(stage_event("r1", "INSTALL", "deploy-install", time.time()))
        assert recovered.list()[0]["software"] == "nginx"
        assert all(key not in recovered.list()[0] for key in ("production_id", "revision_id", "call_id"))


def test_recover_interrupts_running_and_no_dup():
    with tempfile.TemporaryDirectory() as td:
        task_dir = Path(td) / "task"
        store = make_store(task_dir)
        base = 1_700_000_000.0
        store.handle_event(stage_event("r1", "INSTALL", "deploy-install", base))
        store.handle_event(ev("r1", "turn.completed", {"result": "done"}, base + 60))
        finished_id = store.list()[0]["task_id"]  # 已收尾：success
        store.handle_event(stage_event("r2", "GUIDE", "deploy-guide", base + 100))
        running_id = store.list()[0]["task_id"]   # 进行中：重启后被 interrupted 定格
        assert running_id != finished_id

        # 坏文件：不阻断恢复
        (task_dir / "task-broken.json").write_text("{oops", encoding="utf-8")

        recovered = make_store(task_dir)
        recovered.recover()
        tasks = {t["task_id"]: t for t in recovered.list()}
        assert len(tasks) == 2  # 坏文件跳过、两条任务不重复
        assert tasks[running_id]["status"] == "DONE"
        assert tasks[running_id]["outcome"] == "interrupted"
        assert tasks[running_id]["stages"][-1]["ended_at"] is not None
        assert tasks[finished_id]["outcome"] == "success"
        recovered.handle_event(ev("r2", "user.message", {"text": "继续安装"}, base + 200))
        recovered.handle_event(stage_event("r2", "INSTALL", "deploy-install", base + 201))
        assert len(recovered.list()) == 2
        assert tasks[running_id]["status"] == "RUNNING" and tasks[running_id]["outcome"] is None
        recovered.handle_event(ev("r2", "turn.completed", {}, base + 210))
        recovered.handle_event(stage_event("r1", "ARCHIVE", "deploy-archive", base + 211))
        assert len(recovered.list()) == 2 and tasks[finished_id]["status"] == "RUNNING"
        assert tasks[finished_id]["created_at"] == base


def test_recover_old_multiple_tasks_keeps_files_and_reuses_first():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        base = 1_700_000_000.0
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", base,
                                      task_info("部署 nginx 镜像", "nginx")))
        store.handle_event(ev("r1", "turn.completed", {}, base + 1))
        (first,) = store.list()
        later = json.loads(json.dumps(first))
        later.update(task_id="task-legacy-later", created_at=base + 10,
                     title="继续归档 nginx", name="继续归档 nginx (202311142213)")
        later_path = Path(td) / f"{later['task_id']}.json"
        later_path.write_text(json.dumps(later), encoding="utf-8")
        before = {path.name: path.read_bytes() for path in Path(td).glob("task-*.json")}
        recovered = make_store(td)
        recovered.recover()
        assert len(recovered.list()) == 2
        assert {path.name: path.read_bytes() for path in Path(td).glob("task-*.json")} == before
        recovered.handle_event(stage_event("r1", "ARCHIVE", "deploy-archive", base + 20,
                                          task_info("再次归档 nginx", "nginx", "1.25")))
        assert len(recovered.list()) == 2
        original = recovered.get(first["task_id"])
        assert original["status"] == "RUNNING" and original["name"] == first["name"]
        assert original["created_at"] == base and original["current_stage"] == "ARCHIVE"
        assert recovered.get(later["task_id"]) == later and later_path.read_bytes() == before[later_path.name]


def test_missing_title_is_filled_once_without_changing_created_at():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        base = 1_700_000_000.0
        store.handle_event(ev("r1", "user.message", {"text": "帮我安装docker"}, base))
        store.handle_event(stage_event("r1", "GUIDE", "deploy-guide", base + 1))
        task = store.list()[0]
        assert task["title"] is None and task["name"].startswith("镜像任务 (")
        store.handle_event(stage_event("r1", "INSTALL", "deploy-install", base + 2,
                                      task_info("部署 Docker 镜像", "docker")))
        named = task["name"]
        assert named.startswith("部署 Docker 镜像 (")
        store.handle_event(stage_event("r1", "VERIFY", "deploy-verify", base + 3,
                                      task_info("只读验证 Docker", "docker", "29.9.3")))
        assert task["name"] == named and task["created_at"] == base + 1
        assert task["version"] == "29.9.3"

def test_create_manual_init_task():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        task = store.create_manual({
            "software": "nginx", "version": "1.25.3", "task_type": "image",
            "ecs_mode": "existing",
            "ecs_instance": {"id": "i-1", "name": "ecs-nginx-20260914", "ip": "1.2.3.4"},
            "install_doc": "https://nginx.org/en/docs.html",
        })
        assert task["status"] == "INIT" and task["origin"] == "manual"
        assert task["type"] == "image"
        assert task["software"] == "nginx" and task["version"] == "1.25.3"
        assert task["confirmed"] is True  # 手动任务的软件/版本来自表单即权威
        assert task["name"].startswith("镜像 nginx 1.25.3 (")
        assert task["stages"] == [] and task["run_id"] is None
        assert len(list(Path(td).glob("task-*.json"))) == 1


def test_mark_running_claims_stage_events():
    """mark_running 先于回合执行：首个 stage.changed 认领手动任务而非新建
    auto 任务；回合收尾按既有状态机走。"""
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        task = store.create_manual({"software": "redis", "version": "7.2",
                                    "task_type": "rpm", "ecs_mode": "create"})
        prompt = "制作 redis 7.2 的 RPM 包…"
        base = 1_700_000_000.0
        store.handle_event(ev("run_9", "user.message", {"text": prompt}, base))
        assert store.mark_running(task["task_id"], "run_9", prompt) is not None
        store.handle_event(stage_event("run_9", "GUIDE", "rpm-guide", base + 1))
        store.handle_event(stage_event("run_9", "BUILD", "rpm-build", base + 2))
        store.handle_event(ev("run_9", "turn.completed", {"result": "ok"}, base + 60))
        tasks = store.list()
        assert len(tasks) == 1  # 不新建第二个
        t = tasks[0]
        assert t["status"] == "DONE" and t["outcome"] == "success"
        assert [s["stage"] for s in t["stages"]] == ["GUIDE", "BUILD"]


def test_mark_running_rejects_non_init():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        task = store.create_manual({"software": "a"})
        assert store.mark_running(task["task_id"], "r1", "x") is not None
        assert store.mark_running(task["task_id"], "r2", "x") is None  # 非 INIT 拒绝
        assert store.mark_running("task-nope", "r1", "x") is None


def test_manual_continuation_and_recovery_reuse_task():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        task = store.create_manual({"software": "redis", "version": "7.2", "task_type": "rpm"})
        original = {key: task[key] for key in ("task_id", "name", "created_at", "type")}
        prompt = "制作 redis 7.2 的 RPM 包"
        store.mark_running(task["task_id"], "r1", prompt)
        store.handle_event(stage_event("r1", "BUILD", "rpm-build", 100.0))
        store.handle_event(ev("r1", "turn.completed", {}, 101.0))
        store.handle_event(ev("r1", "user.message", {"text": "继续验证"}, 110.0))
        store.handle_event(stage_event("r1", "VERIFY", "rpm-verify", 111.0,
                                      task_info("验证 redis RPM", "other", "1")))
        assert len(store.list()) == 1 and task["status"] == "RUNNING"
        assert task["origin"] == "manual" and (task["software"], task["version"]) == ("redis", "7.2")
        store.handle_event(ev("r1", "turn.completed", {}, 112.0))
        recovered = make_store(td)
        recovered.recover()
        recovered.handle_event(stage_event("r1", "ARCHIVE", "rpm-archive", 120.0))
        (continued,) = recovered.list()
        assert {key: continued[key] for key in original} == original
        assert continued["turn_text"] == prompt and continued["status"] == "RUNNING"
        assert [item["stage"] for item in continued["stages"]] == ["BUILD", "VERIFY", "ARCHIVE"]


def test_build_task_prompt_cases():
    # 镜像 + 已有 ECS（流水线命名规约：ecs-<别名> → 别名直给）
    p = build_task_prompt({"software": "nginx", "version": "1.25.3", "task_type": "image",
                           "ecs_mode": "existing",
                           "ecs_instance": {"id": "i-existing", "name": "ecs-nginx-20260914", "ip": "1.2.3.4"},
                           "install_doc": "https://doc.example"})
    assert p.startswith("一键部署 nginx 1.25.3")
    assert "目标服务器别名：nginx-20260914" in p
    assert "安装文档：https://doc.example" in p
    assert "instance_id：i-existing" in p and "IP：1.2.3.4" in p
    # 外来机器（无 ecs- 前缀）：name+IP 让 agent 解析
    p = build_task_prompt({"software": "redis", "task_type": "image", "ecs_mode": "existing",
                           "ecs_instance": {"name": "my-server", "ip": "5.6.7.8"}})
    assert "已有 ECS「my-server」（IP 5.6.7.8）" in p and "ssh-skill" in p
    assert "redis 最新稳定版" in p  # 版本缺省
    assert "请检索官方最新稳定版文档" in p
    # 按需创建：只列填写项
    p = build_task_prompt({"software": "mysql", "version": "8.0", "task_type": "image",
                           "ecs_mode": "create",
                           "ecs_params": {"flavor": "c6.xlarge.2", "disk_size": 100}})
    assert "按需创建 ECS" in p and "规格=c6.xlarge.2" in p and "系统盘GB=100" in p
    assert "镜像=" not in p  # 未填写项不透传
    # RPM 类型
    p = build_task_prompt({"software": "postgresql", "version": "16.1", "task_type": "rpm",
                           "ecs_mode": "create", "ecs_params": {}})
    assert p.startswith("制作 postgresql 16.1 的 RPM 包") and "scope 默认" in p


def test_recover_keeps_init():
    with tempfile.TemporaryDirectory() as td:
        store = make_store(td)
        store.create_manual({"software": "nginx", "version": "1.25.3"})
        recovered = make_store(td)
        recovered.recover()
        (task,) = recovered.list()
        assert task["status"] == "INIT"  # 待运行任务不受重启影响，仍可运行


def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    main()
