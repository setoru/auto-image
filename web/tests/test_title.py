#!/usr/bin/env python3
"""标题生成纯函数与主缝断言 —— prompt 形状、输出清洗、事件流接线。

LLM 会话经 FakeSessionFactory 注入（工厂对标题生成与部署会话同形）；
transcript 写回在生产为 sdk.rename_session，测试以 monkeypatch 造桩。
纯 assert，无 pytest。

运行：python web/tests/test_title.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from web import sdk as sdk_mod  # noqa: E402
from web import title as title_mod  # noqa: E402
from web.app import create_app  # noqa: E402
from web.fake import DEFAULT_SCRIPT, FakeSession, FakeSessionFactory  # noqa: E402
from web.runs import RunManager  # noqa: E402
from web.tests.support import StreamingASGITransport  # noqa: E402
from web.tests.test_api import collect_sse, open_stream, wait_status  # noqa: E402


def test_prompt_shape():
    """指令拼装：指令 + 截断的用户消息，超预算不切 UTF-8 之外的部分。"""
    prompt = title_mod.title_prompt("部署 nginx 1.25")
    assert title_mod.TITLE_INSTRUCTIONS in prompt
    assert prompt.endswith("User prompt:\n部署 nginx 1.25")
    long = "x" * 2000
    assert len(title_mod.title_prompt(long)) <= len(title_mod.TITLE_INSTRUCTIONS) + 20 + 960


def test_clean_title_variants():
    """清洗：引号/空白/尾标点剥除、超长截断、空与非字符串为 None。"""
    assert title_mod.clean_title("  部署 nginx。  ") == "部署 nginx"
    assert title_mod.clean_title('"Deploy nginx"') == "Deploy nginx"
    assert title_mod.clean_title("“制作 RPM 包”") == "制作 RPM 包"
    assert title_mod.clean_title("a  b\tc?") == "a b c"
    assert title_mod.clean_title("x" * 100) == "x" * title_mod.TITLE_MAX_CHARS
    assert title_mod.clean_title(None) is None
    assert title_mod.clean_title("") is None
    assert title_mod.clean_title("   ") is None


def test_clean_title_redacts_credentials():
    """生成结果同样过脱敏层：内联凭据不进标题。"""
    cleaned = title_mod.clean_title("部署 nginx，password: Xk9$mPq2LwzR")
    assert "Xk9$mPq2LwzR" not in cleaned
    assert "***" in cleaned


async def test_generate_title_uses_one_shot_session():
    """标题会话独立于部署会话：一次性 query、取 Result 文本、无续接参数。"""
    script = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "部署 nginx"}]}},
        {"type": "result", "subtype": "success", "result": "部署 nginx"},
    ]

    class OneShot(FakeSession):
        def __init__(self):
            super().__init__(script=script)

    class Factory:
        def __init__(self):
            self.calls = []

        def __call__(self, session_id=None):
            self.calls.append(session_id)
            return OneShot()

    factory = Factory()
    out = await title_mod.generate_title("部署 nginx 到 server-a", factory)
    assert out == "部署 nginx"
    assert factory.calls == [None]  # 无续接、全新会话


async def test_generate_title_failure_returns_none():
    """会话异常与无 Result 均静默返回 None（维持临时标题）。"""

    class Boom(FakeSession):
        async def receive_response(self):
            raise RuntimeError("sdk crashed")
            yield  # pragma: no cover

    assert await title_mod.generate_title("x", lambda sid=None: Boom()) is None
    empty = FakeSession(script=[])
    assert await title_mod.generate_title("x", lambda sid=None: empty) is None


def make_app(script=None):
    return create_app(
        session_factory=FakeSessionFactory(script=script if script is not None else DEFAULT_SCRIPT, delay=0.02),
        heartbeat_interval=0.05,
        list_sessions_fn=lambda: [],
        scope_config="/nonexistent-scope.yaml",
        state_path=tempfile.mkdtemp() + "/state.json",
    )


async def test_first_message_assigns_title_and_emits_event():
    """首条消息 → 事件流出现 run.title_changed，摘要与 transcript 写回到位。
    标题会话经独立 title_factory（生产为隔离 cwd 配置，不落项目根 transcript）。"""
    title_script = [{"type": "result", "subtype": "success", "result": "「部署 nginx」"}]
    title_calls = []

    class TitleFactory:
        def __call__(self, session_id=None):
            title_calls.append(session_id)
            return FakeSession(script=title_script, session_id="sess_title")

    app = create_app(
        session_factory=FakeSessionFactory(script=DEFAULT_SCRIPT, delay=0.02),
        title_factory=TitleFactory(),
        heartbeat_interval=0.05,
        list_sessions_fn=lambda: [],
        scope_config="/nonexistent-scope.yaml",
        state_path=tempfile.mkdtemp() + "/state.json",
    )
    renames = []
    orig_rename = sdk_mod.rename_session

    def fake_rename(session_id, t, directory=None):
        renames.append((session_id, t))

    sdk_mod.rename_session = fake_rename
    try:
        async with httpx.AsyncClient(transport=StreamingASGITransport(app=app), base_url="http://testserver") as client:
            run_id = (await client.post("/api/runs", json={})).json()["run_id"]
            await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx 1.25 到 server-a"})
            await wait_status(client, run_id, "WAITING_INPUT")
            events, _ = await collect_sse(await open_stream(client, run_id), deadline_s=1.0)
            types = [e["event"] for e in events]
            # 标题事件在场（清洗剥掉了引号）
            title_events = [e for e in events if e["event"] == "run.title_changed"]
            assert len(title_events) == 1, types
            assert title_events[0]["data"]["title"] == "部署 nginx"
            # 摘要带 title 字段
            summary = (await client.get(f"/api/runs/{run_id}")).json()
            assert summary["title"] == "部署 nginx"
        # transcript 写回：以部署会话的 session_id、清洗后的标题
        assert renames == [("sess_fake_1", "部署 nginx")], renames
        assert title_calls == [None]  # 标题会话全新起、无续接
    finally:
        sdk_mod.rename_session = orig_rename


async def test_title_session_isolated_from_discovery():
    """标题会话 options 与部署会话隔离：独立 cwd（transcript 落服务私有项目
    目录，不进项目根的 list_sessions 发现层）、无工具、上限收紧——
    重启后任务列表不会冒出标题会话条目。setting_sources 保持默认：认证经
    user settings env 注入，清空会导致 CLI not logged in（实测踩坑）。"""
    options = sdk_mod.title_options()
    assert options.cwd == sdk_mod.TITLE_SESSION_CWD != str(sdk_mod.PROJECT_ROOT)
    assert options.tools == []
    assert options.max_turns == 1
    assert options.setting_sources is None  # 认证依赖 user settings，不可清空
    # cwd 对应的项目目录与项目根不同名（transcript 分流验证）
    import re

    sanitize = lambda p: re.sub(r"[^A-Za-z0-9]", "-", p)  # noqa: E731
    assert sanitize(sdk_mod.TITLE_SESSION_CWD) != sanitize(str(sdk_mod.PROJECT_ROOT))


async def test_second_message_does_not_retitle():
    """标题只生成一次：第二回合不再触发（无第二个标题会话）。"""
    deploy_calls = []
    title_calls = []

    class CountingFactory(FakeSessionFactory):
        def __call__(self, session_id=None):
            deploy_calls.append(session_id)
            return super().__call__(session_id)

    class TitleFactory:
        def __call__(self, session_id=None):
            title_calls.append(session_id)
            return FakeSession(script=[{"type": "result", "subtype": "success", "result": "部署 nginx"}])

    app = create_app(
        session_factory=CountingFactory(script=DEFAULT_SCRIPT, delay=0.02),
        title_factory=TitleFactory(),
        heartbeat_interval=0.05,
        list_sessions_fn=lambda: [],
        scope_config="/nonexistent-scope.yaml",
        state_path=tempfile.mkdtemp() + "/state.json",
    )
    async with httpx.AsyncClient(transport=StreamingASGITransport(app=app), base_url="http://testserver") as client:
        run_id = (await client.post("/api/runs", json={})).json()["run_id"]
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "部署 nginx"})
        await wait_status(client, run_id, "WAITING_INPUT")
        await asyncio.sleep(0.2)  # 等标题会话（无 delay）跑完
        await client.post(f"/api/runs/{run_id}/messages", json={"text": "继续"})
        await wait_status(client, run_id, "WAITING_INPUT")
        await asyncio.sleep(0.1)
    # 部署会话 1 次（两回合同一连接）+ 标题会话 1 次；第二回合不再生成
    assert len(deploy_calls) == 1 and len(title_calls) == 1, (deploy_calls, title_calls)


async def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        out = fn()
        if asyncio.iscoroutine(out):
            await out
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
