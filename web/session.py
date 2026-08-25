"""会话驱动：一条 Run 即一条 SDK 会话的生命周期。

run_agent 只接收参数、不引用全局状态——被关闭的旧会话不能让随后新建的
Run 被旧协程的收尾分支改写状态。会话对象来自可注入工厂（生产包装
ClaudeSDKClient，测试注入脚本化假实现），工厂以续接源 session_id 调用、
返回支持 async with 的对象，暴露 query / interrupt / receive_response。

停止的服务端语义：intervene 置 stop_requested 后由 HTTP 层调 interrupt；
run_agent 在回合收尾按该标记区分 turn.stopped 与 turn.completed（真 SDK
被打断的回合以 result=None 的 error Result 收尾，但判定以本端标记为权威）。
"""
import asyncio

from .normalize import is_final_result, normalize_message
from .redact import redact_text
from .runs import CANCELED, FAILED, WAITING_INPUT


async def run_agent(run, session_factory, store):
    """驱动一条会话：等指令 → 执行回合 → 回挂起，直到会话被关闭或异常。"""
    store.append(run.run_id, "run.started", {})
    try:
        async with session_factory(run.resume_session_id) as session:
            run.session = session
            while True:
                await run.next_input.wait()
                run.next_input.clear()
                # 回合间到达的停止请求：目标回合已结束，随新回合开始作废
                run.stop_requested = False
                text = run.pending_prompt
                store.append(run.run_id, "user.message", {"text": text})
                await session.query(text)
                tool_names = {}
                finished = False
                async for message in session.receive_response():
                    for etype, payload in normalize_message(message, tool_names):
                        store.append(run.run_id, etype, payload)
                        if etype == "stage.changed":
                            run.stage = payload["stage"]
                    if is_final_result(message):
                        sid = message.get("session_id")
                        if sid:
                            run.session_id = sid
                        _finish_turn(run, store, message)
                        finished = True
                if not finished:
                    # 流结束而无 Result（未观察到的 SDK 形态）：仍须收尾，
                    # 否则状态卡在 RUNNING、会话永久占用执行权
                    _finish_turn(run, store, None)
    except asyncio.CancelledError:
        # 关闭会话 = 取消本协程：会话记录保留，可供后续新会话续接
        run.status = CANCELED
        store.append(run.run_id, "run.canceled", {})
    except Exception as exc:  # noqa: BLE001 —— 会话内任何异常都落到 run.failed，错误摘要过脱敏
        run.status = FAILED
        store.append(run.run_id, "run.failed", {"message": redact_text(str(exc))})


def _finish_turn(run, store, message):
    """回合收尾：停止请求优先（turn.stopped），否则以 Result 正常完成。

    与 intervene 的标记置位同为同步块，在单线程事件循环上互斥执行，
    不存在「半停半完成」的交错。
    """
    run.status = WAITING_INPUT
    if run.stop_requested:
        run.stop_requested = False
        store.append(run.run_id, "turn.stopped", {})
    else:
        result = message.get("result", "") if message else ""
        store.append(run.run_id, "turn.completed", {"result": redact_text(result)})
