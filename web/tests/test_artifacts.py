#!/usr/bin/env python3
"""产物端点主缝测试 —— meta.json 发现、清单解锁、内容读取、路径约束。

缝：同 test_api 的 ASGI 测试客户端；产物目录在临时目录造桩
（artifact_root 注入），会话用假实现按剧本推进 stage，不触网不触云。
纯 assert，无 pytest。

运行：python web/tests/test_artifacts.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.app import create_app  # noqa: E402
from web.fake import FakeSessionFactory  # noqa: E402
from web.tests.support import async_client  # noqa: E402

# deploy.config.yaml 约定文件名的样例集（nginx 1.25 造桩）
GUIDE_FILES = ("nginx-install.md", "nginx-verify.md")
INSTALL_FILES = ("nginx-install-result.md", "nginx-install-issues.md", "nginx-install-meta.json")
VERIFY_FILES = ("nginx-verify-result.md", "nginx-verify-issues.md")
ARCHIVE_FILES = ("nginx-archive-result.md", "nginx-deploy-list.md", "nginx-archive-issues.md")


def stage_script(*subagents):
    """剧本：对每个流水线子 agent 发一次 tool_use / tool_result，尾随 Result。

    stage.changed 由 tool_use + subagent_type 推导（同 normalize 主缝路径）。
    """
    script = []
    for i, subagent in enumerate(subagents, 1):
        script.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"toolu_{i:02d}", "name": "Task",
             "input": {"subagent_type": subagent, "prompt": "执行"}},
        ]}})
        script.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"toolu_{i:02d}", "content": "done"},
        ]}})
    script.append({"type": "result", "subtype": "success", "result": "回合完成"})
    return script


def make_output_tree(root, rel, names, mtime=None):
    """在 artifact_root 下造一个产物目录（rel 相对路径），写入给定产物文件。

    mtime 显式给定：默认的「写入时刻」与 run 创建只差几毫秒，文件系统
    mtime 精度截断会偶发落到 run 开始之前，被发现逻辑的 mtime 过滤误伤。
    """
    out = root / rel
    out.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = out / name
        if name.endswith(".json"):
            path.write_text(json.dumps({"path": "create", "server_alias": "srv"}), encoding="utf-8")
        else:
            path.write_text(f"# {name}\n\n部署产物样例，含 | 表格 | 与 `代码块`。\n", encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
    return out


# 产物文件名约定的契约快照（与项目根 deploy.config.yaml 一致）：权威源
# 改名时此桩不同步、清单测试落空，即提醒两端对齐
DEPLOY_CONFIG_STUB = """\
install_file: "{{software}}-install.md"
verify_file: "{{software}}-verify.md"
install_result_file: "{{software}}-install-result.md"
install_issues_file: "{{software}}-install-issues.md"
install_meta_file: "{{software}}-install-meta.json"
verify_result_file: "{{software}}-verify-result.md"
verify_issues_file: "{{software}}-verify-issues.md"
archive_result_file: "{{software}}-archive-result.md"
deploy_list_file: "{{software}}-deploy-list.md"
archive_issues_file: "{{software}}-archive-issues.md"
"""


def make_app(script, root):
    (root / "deploy.config.yaml").write_text(DEPLOY_CONFIG_STUB, encoding="utf-8")
    return create_app(
        session_factory=FakeSessionFactory(script=script),
        heartbeat_interval=0.05,
        artifact_root=root,
        deploy_config=root / "deploy.config.yaml",
    )


async def start_run(client, text="部署 nginx 1.25"):
    """新建空会话并发首条指令，返回 (run_id, started_at)。"""
    run_id = (await client.post("/api/runs", json={})).json()["run_id"]
    started_at = (await client.get(f"/api/runs/{run_id}")).json()["started_at"]
    await client.post(f"/api/runs/{run_id}/messages", json={"text": text})
    return run_id, started_at


async def wait_stage(client, run_id, want, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        r = await client.get(f"/api/runs/{run_id}")
        assert r.status_code == 200, r.text
        last = r.json()
        if last["stage"] == want:
            return last
        await asyncio.sleep(0.01)
    raise AssertionError(f"stage 未推进到 {want}，当前 {last}")


async def run_with_client(script, root):
    return async_client(make_app(script, root))


async def test_empty_before_install_stage():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        async with await run_with_client(stage_script("deploy-guide"), root) as client:
            run_id, _ = await start_run(client)
            await wait_stage(client, run_id, "GUIDE")
            # 产物目录尚未发现（INSTALL 未开始）：即使目录里已有文件也返回空
            make_output_tree(root, "nginx/1.25", GUIDE_FILES + INSTALL_FILES + VERIFY_FILES + ARCHIVE_FILES)
            r = await client.get(f"/api/runs/{run_id}/artifacts")
            assert r.status_code == 200, r.text
            assert r.json() == {"output_dir": None, "files": []}, r.json()


async def test_install_stage_unlocks_guide_and_install_only():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        async with await run_with_client(stage_script("deploy-guide", "deploy-install"), root) as client:
            run_id, started_at = await start_run(client)
            # 造桩（mtime 明确晚于 run 开始）：四阶段文件齐全，但解锁只到 INSTALL
            out = make_output_tree(root, "nginx/1.25",
                                   GUIDE_FILES + INSTALL_FILES + VERIFY_FILES + ARCHIVE_FILES,
                                   mtime=started_at + 60)
            await wait_stage(client, run_id, "INSTALL")
            r = await client.get(f"/api/runs/{run_id}/artifacts")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["output_dir"] == str(out), body["output_dir"]
            by_name = {f["name"]: f for f in body["files"]}
            assert sorted(by_name) == sorted(GUIDE_FILES + INSTALL_FILES), by_name
            for name in GUIDE_FILES:
                assert by_name[name]["stage"] == "GUIDE", by_name[name]
            for name in INSTALL_FILES:
                assert by_name[name]["stage"] == "INSTALL", by_name[name]
            # size 如实（内容长度）
            assert by_name["nginx-install.md"]["size"] == (out / "nginx-install.md").stat().st_size


async def test_discovery_filters_by_mtime():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        async with await run_with_client(stage_script("deploy-guide", "deploy-install"), root) as client:
            run_id, started_at = await start_run(client)
            # 只有早于本次 run 的旧 meta：被 mtime 过滤，清单为空
            make_output_tree(root, "nginx/old", GUIDE_FILES + INSTALL_FILES, mtime=started_at - 100)
            await wait_stage(client, run_id, "INSTALL")
            r = await client.get(f"/api/runs/{run_id}/artifacts")
            assert r.json() == {"output_dir": None, "files": []}, r.json()
            # 新 meta 落盘（mtime 晚于 run 开始）：目录随即被发现
            fresh = make_output_tree(root, "redis/7.2", (), mtime=started_at + 120)
            (fresh / "redis-install-meta.json").write_text("{}", encoding="utf-8")
            os.utime(fresh / "redis-install-meta.json", (started_at + 120,) * 2)
            r = await client.get(f"/api/runs/{run_id}/artifacts")
            assert r.json()["output_dir"] == str(fresh), r.json()


async def test_full_run_unlocks_all_stages():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        script = stage_script("deploy-guide", "deploy-install", "deploy-verify", "deploy-archive")
        async with await run_with_client(script, root) as client:
            run_id, started_at = await start_run(client)
            out = make_output_tree(
                root, "nginx/1.25",
                GUIDE_FILES + INSTALL_FILES + VERIFY_FILES + ARCHIVE_FILES + ("nginx-pipeline-result.md",),
                mtime=started_at + 60,
            )
            await wait_stage(client, run_id, "ARCHIVE")
            r = await client.get(f"/api/runs/{run_id}/artifacts")
            by_name = {f["name"]: f for f in r.json()["files"]}
            expected = (
                GUIDE_FILES + INSTALL_FILES + VERIFY_FILES + ARCHIVE_FILES + ("nginx-pipeline-result.md",)
            )
            assert sorted(by_name) == sorted(expected), by_name
            assert by_name["nginx-pipeline-result.md"]["stage"] == "ARCHIVE", by_name
            assert by_name["nginx-verify-result.md"]["stage"] == "VERIFY", by_name


async def test_verify_failure_leaves_archive_absent():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # verify 未通过：门禁在 skill 层，ARCHIVE 不执行、文件不存在，清单如实缺席
        script = stage_script("deploy-guide", "deploy-install", "deploy-verify")
        async with await run_with_client(script, root) as client:
            run_id, started_at = await start_run(client)
            make_output_tree(root, "nginx/1.25", GUIDE_FILES + INSTALL_FILES + VERIFY_FILES, mtime=started_at + 60)
            await wait_stage(client, run_id, "VERIFY")
            r = await client.get(f"/api/runs/{run_id}/artifacts")
            by_stage = {f["stage"] for f in r.json()["files"]}
            assert by_stage == {"GUIDE", "INSTALL", "VERIFY"}, by_stage


async def test_content_returns_raw_text():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        async with await run_with_client(stage_script("deploy-guide", "deploy-install"), root) as client:
            run_id, started_at = await start_run(client)
            out = make_output_tree(root, "nginx/1.25", GUIDE_FILES + INSTALL_FILES, mtime=started_at + 60)
            await wait_stage(client, run_id, "INSTALL")
            r = await client.get(f"/api/runs/{run_id}/artifacts/nginx-install.md")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["name"] == "nginx-install.md"
            assert body["stage"] == "GUIDE"
            assert body["content"] == (out / "nginx-install.md").read_text(encoding="utf-8")
            # json 产物同样原文返回
            r = await client.get(f"/api/runs/{run_id}/artifacts/nginx-install-meta.json")
            assert r.status_code == 200, r.text
            assert r.json()["stage"] == "INSTALL"


async def test_traversal_and_unknown_names_404():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        async with await run_with_client(stage_script("deploy-guide", "deploy-install"), root) as client:
            run_id, started_at = await start_run(client)
            make_output_tree(root, "nginx/1.25", GUIDE_FILES + INSTALL_FILES, mtime=started_at + 60)
            await wait_stage(client, run_id, "INSTALL")
            # 清单外的名字（未解锁、非产物模式、不存在、越界编码）一律 404；
            # 明文 `..` 段在客户端即被规范化，到服务端的是编码形式
            for name in (
                "%2e%2e",
                "nginx-verify-result.md",       # 存在但未解锁
                "nginx-random.md",              # 不匹配产物文件名约定
                "deploy.config.yaml",           # 目录外真实存在的文件
                "..%2F..%2Fdeploy.config.yaml", # URL 编码的越界路径
                "%2e%2e%2f%2e%2e%2fscope.yaml",
            ):
                r = await client.get(f"/api/runs/{run_id}/artifacts/{name}")
                assert r.status_code == 404, (name, r.text)


async def test_unknown_run_404():
    with tempfile.TemporaryDirectory() as tmp:
        async with await run_with_client(stage_script("deploy-guide"), Path(tmp)) as client:
            r = await client.get("/api/runs/run_missing/artifacts")
            assert r.status_code == 404
            r = await client.get("/api/runs/run_missing/artifacts/x.md")
            assert r.status_code == 404


async def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        await fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
