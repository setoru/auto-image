#!/usr/bin/env python
"""消息映射与阶段推导纯函数断言 —— 缝 A 风格（同 ecs-skill tests）。

两层数据形状在此对齐：假剧本/事件重放的 CLI JSON dict，与真 SDK 的
dataclass 消息（经 to_dict 适配成同一形状）。映射规则只此一处被验证，
主缝（test_api.py）不再重复断言字段细节。

运行：python web/tests/test_normalize.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from web.normalize import is_final_result, normalize_message  # noqa: E402
from web.sdk import to_dict  # noqa: E402


def test_thinking_text_blocks_map_to_events():
    msg = {
        "type": "assistant",
        "message": {"content": [
            {"type": "thinking", "thinking": "先看部署配置"},
            {"type": "text", "text": "开始读取 deploy.config.yaml"},
        ]},
    }
    assert normalize_message(msg, {}) == [
        ("agent.thinking", {"text": "先看部署配置"}),
        ("agent.message", {"text": "开始读取 deploy.config.yaml"}),
    ]


def test_tool_use_maps_to_started_with_summary_not_raw_input():
    msg = {
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": "cat scope.yaml"}},
        ]},
    }
    events = normalize_message(msg, {})
    assert [e[0] for e in events] == ["agent.tool_started"]
    payload = events[0][1]
    assert payload["tool"] == "Bash"
    # 摘要是脱敏后的紧凑表示，原始 input 不整体透出
    assert payload["summary"] == '{"command": "cat scope.yaml"}'
    assert "input" not in payload


def test_tool_result_maps_to_finished_without_raw_output():
    tool_names = {"t1": "Read"}
    msg = {
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ak=HWPFEJ9AB3CDEFGHIJKL\nsk=f3a9c81d0b7e46f2a5d8c3b1e9470ad6c2f5b831"},
        ]},
    }
    events = normalize_message(msg, tool_names)
    assert events == [(
        "agent.tool_finished",
        {"tool": "Read", "summary": "ak=***\nsk=***"},
    )]
    # 未知 id 的 tool_result：没有名字也无妨，事件仍发出（名字空）
    events = normalize_message({
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "??", "content": "x"}]},
    }, {})
    assert events[0][1]["tool"] == ""


def test_stage_derivation_from_task_subagent_type():
    for tool in ("Agent", "Task"):  # CLI 现名 Agent；Task 为旧名/剧本兼容
        for subagent, stage in [
            ("deploy-guide", "GUIDE"), ("deploy-install", "INSTALL"),
            ("deploy-verify", "VERIFY"), ("deploy-archive", "ARCHIVE"),
        ]:
            msg = {
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "id": "t2", "name": tool,
                     "input": {"subagent_type": subagent, "prompt": "干活"}},
                ]},
            }
            events = normalize_message(msg, {})
            assert events[0] == ("stage.changed", {"stage": stage, "status": "running"}), (tool, subagent)
            assert events[1][0] == "agent.tool_started"
    # 子 agent 工具但 subagent_type 不在四类：无 stage，只发工具事件
    events = normalize_message({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": "t3", "name": "Agent",
             "input": {"subagent_type": "general-purpose", "prompt": "x"}},
        ]},
    }, {})
    assert [e[0] for e in events] == ["agent.tool_started"]
    # 非子 agent 工具即使 input 带 subagent_type 也不推阶段
    events = normalize_message({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": "t4", "name": "Bash",
             "input": {"subagent_type": "deploy-guide"}},
        ]},
    }, {})
    assert [e[0] for e in events] == ["agent.tool_started"]


def test_result_message_is_final_and_untouched_types_yield_nothing():
    assert is_final_result({"type": "result", "result": "ok"})
    assert not is_final_result({"type": "assistant", "message": {"content": []}})
    # 主 agent 不认识的形状（stream_event/system/缺 type）一律零事件
    assert normalize_message({"type": "stream_event", "event": {}}, {}) == []
    assert normalize_message({"type": "system", "subtype": "init"}, {}) == []
    assert normalize_message({}, {}) == []


def test_user_message_with_string_content_yields_nothing():
    # CLI JSON 里 user 行的 content 也可能是纯字符串（无工具块）：不迭代字符
    assert normalize_message({"type": "user", "message": {"content": "abc"}}, {}) == []


def test_non_dict_blocks_are_dropped():
    msg = {"type": "assistant", "message": {"content": ["junk", {"type": "text", "text": "hi"}]}}
    assert normalize_message(msg, {}) == [("agent.message", {"text": "hi"})]


# ---- 真 SDK dataclass → CLI JSON 形状适配 ----

def test_to_dict_assistant_message():
    msg = AssistantMessage(
        content=[
            ThinkingBlock(thinking="想法", signature="sig"),
            TextBlock(text="正文"),
            ToolUseBlock(id="t1", name="Task", input={"subagent_type": "deploy-guide"}),
        ],
        model="claude-x",
        parent_tool_use_id="parent-1",
    )
    d = to_dict(msg)
    assert d["type"] == "assistant"
    assert d["parent_tool_use_id"] == "parent-1"
    assert [b["type"] for b in d["message"]["content"]] == ["thinking", "text", "tool_use"]
    # 适配后的形状直接可被 normalize 消费：四类块各归其位
    events = normalize_message(d, {})
    assert [e[0] for e in events] == [
        "agent.thinking", "agent.message", "stage.changed", "agent.tool_started",
    ]
    assert events[2][1] == {"stage": "GUIDE", "status": "running"}
    # signature 签名字段不进事件
    assert "signature" not in str(events[0][1])


def test_to_dict_user_message_tool_result_and_string_content():
    msg = UserMessage(content=[
        ToolResultBlock(tool_use_id="t1", content="输出", is_error=False),
    ])
    d = to_dict(msg)
    assert d["type"] == "user"
    assert d["message"]["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": "输出", "is_error": False},
    ]
    assert normalize_message(d, {"t1": "Read"}) == [
        ("agent.tool_finished", {"tool": "Read", "summary": "输出"}),
    ]
    # content 为纯字符串的用户行：适配后无块，normalize 零事件
    assert to_dict(UserMessage(content="纯文本"))["message"]["content"] == []


def test_to_dict_result_message():
    msg = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=2, session_id="s1", result="回合汇总文本",
    )
    d = to_dict(msg)
    assert d == {"type": "result", "subtype": "success", "result": "回合汇总文本"}
    assert is_final_result(d)


def test_to_dict_partial_and_unknown_messages_are_inert():
    # include_partial_messages 开启后的增量消息：适配为零形状，进不了事件流
    d = to_dict(StreamEvent(uuid="u", session_id="s", event={"type": "content_block_delta"}))
    assert normalize_message(d, {}) == []
    assert normalize_message(to_dict(object()), {}) == []


def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    main()
