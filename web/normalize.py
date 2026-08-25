"""SDK 消息 → 内部事件映射与阶段推导。

SDK 流没有独立的 tool started/finished 事件，从消息结构推导：
- Assistant 的 thinking / text / tool_use 块 → agent.thinking / agent.message / agent.tool_started
- User 的 tool_result 块 → agent.tool_finished
- Task 工具调用带流水线 subagent_type 时，先发 stage.changed

tool_use 与 tool_result 仅以 id 关联，工具名映射由调用方逐回合维护。
"""
import json

from .redact import redact_text

STAGE_BY_SUBAGENT = {
    "deploy-guide": "GUIDE",
    "deploy-install": "INSTALL",
    "deploy-verify": "VERIFY",
    "deploy-archive": "ARCHIVE",
}

TOOL_SUMMARY_LIMIT = 120


def summarize(value):
    """工具入参/出参的脱敏摘要：截断的紧凑表示，不转发原始内容。"""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str)
    if len(value) > TOOL_SUMMARY_LIMIT:
        value = value[:TOOL_SUMMARY_LIMIT] + "…"
    return redact_text(value)


def stage_from_tool_use(block):
    """Task 调用带流水线 subagent_type 时返回对应阶段名，否则 None。"""
    if block.get("name") != "Task":
        return None
    subagent = (block.get("input") or {}).get("subagent_type")
    return STAGE_BY_SUBAGENT.get(subagent)


def is_final_result(message):
    return message.get("type") == "result"


def normalize_message(message, tool_names):
    """一条 SDK 消息 → [(事件类型, payload)]；tool_names 为 id→名字映射，随处理更新。"""
    events = []
    mtype = message.get("type")
    if mtype == "assistant":
        for block in message.get("message", {}).get("content") or []:
            btype = block.get("type")
            if btype == "thinking":
                events.append(("agent.thinking", {"text": redact_text(block.get("thinking", ""))}))
            elif btype == "text":
                events.append(("agent.message", {"text": redact_text(block.get("text", ""))}))
            elif btype == "tool_use":
                if block.get("id"):
                    tool_names[block["id"]] = block.get("name", "")
                stage = stage_from_tool_use(block)
                if stage:
                    events.append(("stage.changed", {"stage": stage, "status": "running"}))
                events.append((
                    "agent.tool_started",
                    {"tool": block.get("name", ""), "summary": summarize(block.get("input"))},
                ))
    elif mtype == "user":
        for block in message.get("message", {}).get("content") or []:
            if block.get("type") == "tool_result":
                events.append((
                    "agent.tool_finished",
                    {
                        "tool": tool_names.get(block.get("tool_use_id"), ""),
                        "summary": summarize(block.get("content")),
                    },
                ))
    return events
