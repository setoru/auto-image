"""ClaudeSDKClient 生产实现：包装成与会话抽象同形的工厂。

真 SDK 把 CLI JSON 行解析成 dataclass 消息（AssistantMessage 等），
服务端的映射层消费的是 CLI JSON 形状的 dict——to_dict 在此适配，
使假剧本（fake.py 的 dict）与真会话走同一条 normalize 路径。
"""
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 方案第 9 节「固定上限」的取值：一个完整部署回合的 turns 预算；
# 回合级 wall-clock 超时属终局语义（run.failed），不在 options 层表达
MAX_TURNS = 200
THINKING_BUDGET_TOKENS = 10000

# guide 阶段联网链路：SDK 会话内内置 WebFetch 被域名安全校验拦截、
# WebSearch 被权限层拒（实测记录见 web/README.md），显式接入既有
# exa MCP（与本机 ~/.claude.json 全局配置同源），不依赖运行者个人配置
EXA_MCP_SERVER = {"type": "stdio", "command": "npx", "args": ["-y", "exa-mcp-server"], "env": {}}


def default_options():
    """SDK options 全配：cwd=项目根，setting_sources 不设（SDK 默认
    user/project/local，project source 从 cwd 发现 .claude/ 与 CLAUDE.md）。

    tools 必须显式给 claude_code 预设（--tools default）：SDK 不传 --tools
    时 CLI 的基础工具集不含子 agent 工具，四阶段流水线无从推进。"""
    return ClaudeAgentOptions(
        cwd=str(PROJECT_ROOT),
        tools={"type": "preset", "preset": "claude_code"},
        mcp_servers={"exa-search": EXA_MCP_SERVER},
        # 非交互会话无人批准：Bash 等内置工具随 claude_code 预设放行，
        # MCP 工具默认要审批，exa 通配单独放行（guide 联网唯一路径）
        allowed_tools=["mcp__exa-search__*"],
        forward_subagent_text=True,
        include_partial_messages=True,
        thinking={"type": "enabled", "budget_tokens": THINKING_BUDGET_TOKENS},
        max_turns=MAX_TURNS,
    )


def to_dict(message):
    """SDK dataclass 消息 → CLI JSON 形状 dict；不认识的消息为零形状
    （无 type 字段，映射层自然忽略：partial 增量、system、限流等）。"""
    if isinstance(message, AssistantMessage):
        return {
            "type": "assistant",
            "message": {"content": _blocks(message.content)},
            "parent_tool_use_id": message.parent_tool_use_id,
        }
    if isinstance(message, UserMessage):
        content = message.content if isinstance(message.content, list) else []
        return {
            "type": "user",
            "message": {"content": _blocks(content)},
            "parent_tool_use_id": message.parent_tool_use_id,
        }
    if isinstance(message, ResultMessage):
        return {"type": "result", "subtype": message.subtype, "result": message.result}
    return {}


def _block(block):
    """内容块 → CLI JSON 形状；四类已知块之外返回 None（调用处丢弃）。"""
    if isinstance(block, ThinkingBlock):
        return {"type": "thinking", "thinking": block.thinking}
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
            "is_error": block.is_error,
        }
    return None


def _blocks(content):
    return [b for b in (_block(x) for x in content or []) if b]


class SDKSession:
    """与会话抽象同形：async with 连接/断开，query/interrupt 透传，
    receive_response 把每回合消息适配成 CLI JSON 形状再产出。"""

    def __init__(self):
        self._client = ClaudeSDKClient(options=default_options())

    async def __aenter__(self):
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc_info):
        return await self._client.__aexit__(*exc_info)

    async def query(self, text):
        await self._client.query(text)

    async def interrupt(self):
        await self._client.interrupt()

    async def receive_response(self):
        async for message in self._client.receive_response():
            yield to_dict(message)


class SDKSessionFactory:
    def __call__(self):
        return SDKSession()
