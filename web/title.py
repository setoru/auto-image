"""会话标题的 LLM 生成：首条指令到达即起，临时标题占位、生成成功才覆盖。

Codex 同构（research/codex-session-title.md）：一次性无工具会话、输入限
960 字节、6 句硬约束 prompt、客户端二次清洗、失败静默维持临时标题——
降级永不比不做更差。与部署会话完全隔离：独立连接、独立 cwd（/tmp/
auto-image-titles，transcript 不进项目根发现层）、无工具无 MCP、自带
超时。setting_sources 保持默认——认证经 user settings env 注入，清空
即 not logged in（见 sdk.title_options）。

写回经 sdk.rename_session 往 transcript 追加 custom-title 行（customTitle
优先于 aiTitle 被读回），rebuild 据此跨重启找回标题。
"""
import asyncio
import logging

from . import sdk as sdk_mod
from .runs import ENDED, RUNNING
from .task_info import TITLE_MAX_CHARS, clean_title

logger = logging.getLogger("web")

TITLE_TIMEOUT_SECONDS = 60.0
# 输入字节预算：防长指令把标题会话撑大，不切 UTF-8（Codex 同款取值）
TITLE_PROMPT_MAX_BYTES = 960

TITLE_INSTRUCTIONS = (
    f"Generate a concise, single-line task title of at most {TITLE_MAX_CHARS} characters and under "
    "five words where possible. Start with an imperative verb. Preserve ticket "
    "references exactly. Write in the user's language. Do not use quotes, markdown, "
    "or trailing punctuation. Do not answer the request. Output only the title text."
)


def title_prompt(user_text):
    """首条指令 → 标题会话输入：指令 + 字节预算截断的用户消息。"""
    text = user_text[:TITLE_PROMPT_MAX_BYTES]
    return f"{TITLE_INSTRUCTIONS}\n\nUser prompt:\n{text}"


async def generate_title(user_text, session_factory):
    """起一次性会话生成标题，返回清洗后标题或 None（失败/超时/形状不合）。

    session_factory 与部署会话同形（生产 SDKSessionFactory、测试注入）。
    任何异常只记日志——标题是锦上添花，失败不进事件流、不打断会话。
    """
    try:
        async with asyncio.timeout(TITLE_TIMEOUT_SECONDS):
            async with session_factory(None) as session:
                await session.query(title_prompt(user_text))
                async for message in session.receive_response():
                    if message.get("type") == "result":
                        return clean_title(message.get("result"))
    except Exception:  # noqa: BLE001 —— 生成失败静默降级为临时标题
        logger.warning("标题生成失败（维持截断标题）", exc_info=True)
        return None
    return None


async def assign_title(run, user_text, session_factory, store, on_change=None):
    """run 的标题生成入口：新对话首条指令起生成，成功即落三处——
    run.title（内存权威）、事件流 session.title_changed（前端即时改名）、
    transcript custom-title 行（重启 rebuild 找回）。

    幂等：已有 title 或 run 已终态（生成期间被结束）时不写；只对新对话
    调用（调用方以 first_prompt 判定），续聊不再生成。session_id 虽在首回合
    接受时已预分配，写回 transcript 前仍须等 SDK 消息确认身份，避免标题比
    transcript 更早落下；等不到（首回合即失败/结束）只落内存。
    """
    if run.title is not None or run.status == ENDED:
        return
    title = await generate_title(user_text, session_factory)
    # 二次校验：生成期间可能已被命名（克隆继承）或已结束
    if title is None or run.title is not None or run.status == ENDED:
        return
    run.title = title
    store.append(run.run_id, "session.title_changed", {"title": title})
    if on_change is not None:
        on_change()
    if not run.session_confirmed:
        await _wait_session_confirmation(run)
    if not run.session_confirmed:
        return  # 首回合身份未确认即终止：transcript 无法安全定位
    try:
        sdk_mod.rename_session(run.session_id, title, directory=str(sdk_mod.PROJECT_ROOT))
    except Exception:  # noqa: BLE001 —— transcript 写回失败只降级跨重启找回
        logger.warning("标题写回 transcript 失败（重启后回退截断标题）", exc_info=True)


async def _wait_session_confirmation(run, timeout_s=30.0):
    """等 SDK 消息确认预分配身份或回合收尾（不再 RUNNING）。"""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while (
        not run.session_confirmed
        and run.status == RUNNING
        and asyncio.get_running_loop().time() < deadline
    ):
        await asyncio.sleep(0.05)
