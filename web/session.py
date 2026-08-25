"""会话驱动：一条 Run 即一条 SDK 会话的生命周期。

run_agent 只接收参数、不引用全局状态——被关闭的旧会话不能让随后新建的
Run 被旧协程的收尾分支改写状态。会话对象来自可注入工厂（生产包装
ClaudeSDKClient，测试注入脚本化假实现），工厂以续接源 session_id 调用、
返回支持 async with 的对象，暴露 query / interrupt / receive_response。

停止的服务端语义：intervene 置 stop_requested 后由 HTTP 层调 interrupt；
run_agent 在回合收尾按该标记区分 turn.stopped 与 turn.completed（真 SDK
被打断的回合以 result=None 的 error Result 收尾，但判定以本端标记为权威）。

回合终局语义：turn_timeout（wall-clock 秒）超时、Result 的错误 subtype
（max_turns 触发、执行错误等）都落到 run.failed——会话进 FAILED 终态、
断开 SDK 连接，不自动重试（服务重启语义一致）。
"""
import asyncio
import time

from .normalize import is_final_result, normalize_message
from .redact import redact_text
from .runs import CANCELED, FAILED, WAITING_INPUT


class TurnFailure(Exception):
    """回合终局失败（Result 错误 subtype）：run_agent 的异常收尾转 run.failed。"""


async def run_agent(run, session_factory, store, turn_timeout=None):
    """驱动一条会话：等指令 → 执行回合 → 回挂起，直到会话被关闭或异常。

    turn_timeout 由服务端固定传入（sdk.TURN_TIMEOUT_SECONDS）；None 仅限
    测试直接驱动，表示不限时。run.started 与接续历史由 create_run 同步段
    先行写入（见 app.py），本协程从等输入开始。"""
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
                try:
                    await asyncio.wait_for(
                        _drain_turn(run, session, store), timeout=turn_timeout
                    )
                except asyncio.TimeoutError:
                    raise TimeoutError(
                        f"回合执行超过 {turn_timeout:.0f} 秒上限，会话已终止"
                    ) from None
    except asyncio.CancelledError:
        # 关闭会话 = 取消本协程：会话记录保留，可供后续新会话续接
        run.status = CANCELED
        run.ended_at = time.time()
        store.append(run.run_id, "run.canceled", {})
    except Exception as exc:  # noqa: BLE001 —— 会话内任何异常都落到 run.failed，错误摘要过脱敏
        run.status = FAILED
        run.ended_at = time.time()
        store.append(run.run_id, "run.failed", {"message": redact_text(str(exc))})


async def _drain_turn(run, session, store):
    """消费一个回合的消息流至 Result（或流结束），收尾交 _finish_turn。"""
    tool_names = {}
    final = None
    async for message in session.receive_response():
        for etype, payload in normalize_message(message, tool_names):
            store.append(run.run_id, etype, payload)
            if etype == "stage.changed":
                run.stage = payload["stage"]
        if is_final_result(message):
            sid = message.get("session_id")
            if sid:
                run.session_id = sid
            final = message
    _finish_turn(run, store, final)


def _finish_turn(run, store, message):
    """回合收尾：停止请求优先（turn.stopped）；Result 的错误 subtype 属
    终局失败，以 TurnFailure 抛给 run_agent 的异常收尾（run.failed）。

    与 intervene 的标记置位同为同步块，在单线程事件循环上互斥执行，
    不存在「半停半完成」的交错。subtype 判定取兜底：只有 success 是正常
    完成，其余（error_max_turns、执行错误等文案不可穷尽）一律失败。
    """
    if run.stop_requested:
        run.stop_requested = False
        run.status = WAITING_INPUT
        store.append(run.run_id, "turn.stopped", {})
    elif message is None or message.get("subtype") == "success":
        result = message.get("result", "") if message else ""
        run.status = WAITING_INPUT
        store.append(run.run_id, "turn.completed", {"result": redact_text(result)})
    else:
        subtype = message.get("subtype") or "unknown"
        detail = redact_text(str(message.get("result") or "")).strip()
        raise TurnFailure(f"回合以 {subtype} 终止" + (f"：{detail}" if detail else ""))
