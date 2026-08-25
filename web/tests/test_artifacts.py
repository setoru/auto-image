#!/usr/bin/env python3
"""产物端点主缝测试 —— deploy/ 全量浏览、目录分组排序、内容读取、路径约束。

缝：同 test_api 的 ASGI 测试客户端；产物目录在临时目录造桩
（artifact_root 注入），浏览不依赖会话，无需剧本推进。纯 assert，无 pytest。

运行：python web/tests/test_artifacts.py
"""
import asyncio
import json
import os
import sys
import tempfile
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


def make_output_tree(root, rel, names, mtime=None):
    """在 artifact_root 下造一个产物目录（rel 相对路径），写入给定产物文件。

    mtime 显式给定：分组排序按组内文件 mtime 最大值，固定时间戳保证
    断言确定（不与真实时钟竞速）。
    """
    out = root / rel if rel else root
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


# 与生产同构：config 在根、产物在其下 deploy/ 子目录（config 不进浏览清单）
def make_app(root):
    (root / "deploy.config.yaml").write_text(DEPLOY_CONFIG_STUB, encoding="utf-8")
    (root / "deploy").mkdir(exist_ok=True)
    return create_app(
        session_factory=FakeSessionFactory(script=[]),
        heartbeat_interval=0.05,
        artifact_root=root / "deploy",
        deploy_config=root / "deploy.config.yaml",
        list_sessions_fn=lambda: [],  # 不读本机真实 transcript
        scope_config=root / "scope-absent.yaml",  # 不载真实凭据（脱敏已知值清单隔离）
    )


async def run_with_client(root):
    return async_client(make_app(root))


async def test_browse_groups_by_dir_latest_mtime_desc():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deploy = root / "deploy"
        deploy.mkdir()
        make_output_tree(deploy, "nginx/1.25", GUIDE_FILES + INSTALL_FILES, mtime=2000)
        make_output_tree(deploy, "redis/7.2", GUIDE_FILES, mtime=3000)
        # 非约定文件（.v1 备份、杂项）与根下散落文件同样在场
        make_output_tree(deploy, "pi/0.84", ("pi-config", "pi-install-result.md.v1", "pi-install-result.md"), mtime=1000)
        (deploy / "README.md").write_text("根散落", encoding="utf-8")
        os.utime(deploy / "README.md", (4000, 4000))
        async with await run_with_client(root) as client:
            r = await client.get("/api/artifacts")
            assert r.status_code == 200, r.text
            groups = r.json()["groups"]
            # 组序 = 组内最新落盘时间降序（根散落文件最晚 → 在前）
            assert [g["dir"] for g in groups] == ["", "redis/7.2", "nginx/1.25", "pi/0.84"], groups
            by_dir = {g["dir"]: g for g in groups}
            # 约定文件带正确阶段；组内文件名升序
            nginx = by_dir["nginx/1.25"]["files"]
            assert [f["name"] for f in nginx] == sorted(f["name"] for f in nginx)
            stages = {f["name"]: f["stage"] for f in nginx}
            assert stages["nginx-install.md"] == "GUIDE"
            assert stages["nginx-install-meta.json"] == "INSTALL"
            # 非约定文件无徽标（stage None）且如实列出
            pi_stages = {f["name"]: f["stage"] for f in by_dir["pi/0.84"]["files"]}
            assert pi_stages["pi-config"] is None
            assert pi_stages["pi-install-result.md.v1"] is None
            assert pi_stages["pi-install-result.md"] == "INSTALL"
            # size 如实（内容长度）
            by_name = {f["name"]: f for f in nginx}
            assert by_name["nginx-install.md"]["size"] == (deploy / "nginx/1.25/nginx-install.md").stat().st_size


async def test_browse_empty_root():
    with tempfile.TemporaryDirectory() as tmp:
        async with await run_with_client(Path(tmp)) as client:
            r = await client.get("/api/artifacts")
            assert r.status_code == 200, r.text
            assert r.json() == {"groups": []}, r.json()


async def test_read_returns_content():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deploy = root / "deploy"
        deploy.mkdir()
        make_output_tree(deploy, "nginx/1.25", GUIDE_FILES + INSTALL_FILES, mtime=1000)
        make_output_tree(deploy, "pi/0.84", ("pi-config",), mtime=1000)
        async with await run_with_client(root) as client:
            r = await client.get("/api/artifacts/file/nginx/1.25/nginx-install.md")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["name"] == "nginx-install.md"
            assert body["dir"] == "nginx/1.25"
            assert body["stage"] == "GUIDE"
            assert body["content"] == (deploy / "nginx/1.25/nginx-install.md").read_text(encoding="utf-8")
            # json 产物与非约定文件同样原文返回（后者 stage 为 None）
            r = await client.get("/api/artifacts/file/nginx/1.25/nginx-install-meta.json")
            assert r.status_code == 200, r.text
            assert r.json()["stage"] == "INSTALL"
            r = await client.get("/api/artifacts/file/pi/0.84/pi-config")
            assert r.status_code == 200, r.text
            assert r.json()["stage"] is None


async def test_read_traversal_and_missing_404():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        deploy = root / "deploy"
        deploy.mkdir()
        make_output_tree(deploy, "nginx/1.25", GUIDE_FILES, mtime=1000)
        async with await run_with_client(root) as client:
            # 越界、缺失、指向目录、二进制/空路径一律 404；明文 `..` 段在客户端
            # 即被规范化，到服务端的是编码形式
            for rel in (
                "..%2F..%2Fdeploy.config.yaml",  # URL 编码的越界路径
                "%2e%2e%2f%2e%2e%2fscope.yaml",
                "%2Fetc%2Fpasswd",               # 绝对路径（Path / "/abs" 会丢根）
                "nginx/1.25",                    # 指向目录本身
                "nginx/1.25/nginx-absent.md",    # 不存在
                "nginx%2F..%2F..%2FREADME.md",   # 目录段内夹带 ..
            ):
                r = await client.get(f"/api/artifacts/file/{rel}")
                assert r.status_code == 404, (rel, r.text)
            # 空 rel（路由命中但路径为空）
            r = await client.get("/api/artifacts/file/")
            assert r.status_code == 404, r.text


async def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        await fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
