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
    工厂创建次序：先部署会话、后标题会话（同工厂两用）。"""
    title_script = [{"type": "result", "subtype": "success", "result": "「部署 nginx」"}]

    class TwoPhaseFactory(FakeSessionFactory):
        """第 1 次调用 = 部署会话；第 2 次 = 标题会话（不同剧本）。"""
        def __init__(self):
            super().__init__(script=DEFAULT_SCRIPT, delay=0.02)
            self.title_script = title_script

        def __call__(self, session_id=None):
            self.session_ids.append(session_id)
            if len(self.session_ids) == 1:
                return FakeSession(script=self.script, delay=self.delay, session_id="sess_deploy")
            return FakeSession(script=self.title_script, session_id="sess_title")

    factory = TwoPhaseFactory()
    app = create_app(
        session_factory=factory,
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
        assert renames == [("sess_deploy", "部署 nginx")], renames
    finally:
        sdk_mod.rename_session = orig_rename


async def test_second_message_does_not_retitle():
    """标题只生成一次：第二回合不再触发（无第二个标题会话）。"""
    seen = []

    class CountingFactory(FakeSessionFactory):
        def __call__(self, session_id=None):
            seen.append(session_id)
            return super().__call__(session_id)

    app = create_app(
        session_factory=CountingFactory(script=DEFAULT_SCRIPT, delay=0.02),
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
    # 部署会话 1 次（两回合同一连接）+ 标题会话 1 次 = 2；第二回合不再生成
    assert len(seen) == 2, seen


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
