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
    get_session_messages,
    list_sessions,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 方案第 9 节「固定上限」的取值：一个完整部署回合的 turns 预算与
# wall-clock 上限（秒）；超时的终局语义在 session 层表达（run.failed）
MAX_TURNS = 200
TURN_TIMEOUT_SECONDS = 3600.0
THINKING_BUDGET_TOKENS = 10000

# 方案第 9 节「系统提示词至少要求」的六要素原文基线
SYSTEM_PROMPT = """你是 auto-image 部署流水线的 Web 会话执行者，与部署使用者在浏览器会话里交互。

- 只处理本项目的部署任务，不执行与部署无关的命令。
- 部署一律按当前 deploy skill（.claude/skills/deploy/SKILL.md）编排执行，四个阶段依次推进、不得合并或内联替做。
- 子 agent 同一时刻至多一个在跑；派发后必须阻塞等待其完成（TaskOutput 等待）并校验产物落盘，不得派发后结束回合等通知；四阶段全部完成、汇总呈现后才收尾回合。
- 不向输出暴露凭据：API Key、密码、SSH 私钥等不在消息、思维链与工具摘要中出现。
- 未经用户明确确认，验证未通过不得归档；用户显式要求跳过门禁时，先复述风险、取得用户确认后再执行。
- 已提交的云操作（创建 ECS、制镜像等）不可撤销；用户要求停止或调整时，如实告知这一边界。"""

# guide 阶段联网链路：SDK 会话内内置 WebFetch 被域名安全校验拦截、
# WebSearch 被权限层拒（实测记录见 web/README.md），显式接入既有
# exa MCP（与本机 ~/.claude.json 全局配置同源），不依赖运行者个人配置
EXA_MCP_SERVER = {"type": "stdio", "command": "npx", "args": ["-y", "exa-mcp-server"], "env": {}}


def default_options(resume_session_id=None):
    """SDK options 全配：cwd=项目根，setting_sources 不设（SDK 默认
    user/project/local，project source 从 cwd 发现 .claude/ 与 CLAUDE.md）。

    tools 必须显式给 claude_code 预设（--tools default）：SDK 不传 --tools
    时 CLI 的基础工具集不含子 agent 工具，四阶段流水线无从推进。
    permission_mode 必须给 bypassPermissions：无值守会话无人批准，SDK 默认
    权限下 Write 与 Bash 写路径一律被拒（真部署实测），产物无法落盘；信任
    边界由运行形态承担（只监听 127.0.0.1 + 系统提示词任务边界）。
    resume_session_id 给定时从该 SDK 会话的 transcript 续接（resume_from）。"""
    return ClaudeAgentOptions(
        cwd=str(PROJECT_ROOT),
        resume=resume_session_id,
        system_prompt=SYSTEM_PROMPT,
        permission_mode="bypassPermissions",
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
        return {
            "type": "result",
            "subtype": message.subtype,
            "result": message.result,
            "session_id": getattr(message, "session_id", None),
        }
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

    def __init__(self, session_id=None):
        self._client = ClaudeSDKClient(options=default_options(session_id))

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
    def __call__(self, session_id=None):
        return SDKSession(session_id)


def list_project_sessions(project_root=None):
    """项目根目录下的 SDK 会话清单（重启重建的发现源；cwd 过滤由 SDK 完成）。"""
    return list_sessions(directory=str(project_root or PROJECT_ROOT))


def project_session_messages(session_id, project_root=None):
    """单条 SDK 会话的可见消息链（重启重建的事件映射源，只读 transcript）。"""
    return get_session_messages(session_id, directory=str(project_root or PROJECT_ROOT))
