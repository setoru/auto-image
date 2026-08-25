"""会话驱动：一条 Run 即一条 SDK 会话的生命周期。

run_agent 只接收参数、不引用全局状态——被关闭的旧会话不能让随后新建的
Run 被旧协程的收尾分支改写状态。会话对象来自可注入工厂（生产包装
ClaudeSDKClient，测试注入脚本化假实现），工厂需返回支持 async with
的对象，暴露 query / receive_response / interrupt（interrupt 由干预
语义接入）。
"""
import asyncio

from .normalize import is_final_result, normalize_message
from .redact import redact_text
from .runs import CANCELED, FAILED, WAITING_INPUT


async def run_agent(run, session_factory, store):
    """驱动一条会话：等指令 → 执行回合 → 回挂起，直到会话被关闭或异常。"""
    store.append(run.run_id, "run.started", {})
    try:
        async with session_factory() as session:
            run.session = session
            while True:
                await run.next_input.wait()
                run.next_input.clear()
                text = run.pending_prompt
                store.append(run.run_id, "user.message", {"text": text})
                await session.query(text)
                tool_names = {}
                async for message in session.receive_response():
                    for etype, payload in normalize_message(message, tool_names):
                        store.append(run.run_id, etype, payload)
                        if etype == "stage.changed":
                            run.stage = payload["stage"]
                    if is_final_result(message):
                        run.status = WAITING_INPUT
                        store.append(run.run_id, "turn.completed", {"result": redact_text(message.get("result", ""))})
    except asyncio.CancelledError:
        # 关闭会话 = 取消本协程：会话记录保留，可供后续新会话续接
        run.status = CANCELED
        store.append(run.run_id, "run.canceled", {})
    except Exception as exc:  # noqa: BLE001 —— 会话内任何异常都落到 run.failed，错误摘要过脱敏
        run.status = FAILED
        store.append(run.run_id, "run.failed", {"message": redact_text(str(exc))})
