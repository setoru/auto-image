"""回合执行：send 起一个回合级 asyncio.Task，SDK 连接只包住一个回合。

无输入队列、无常驻 worker：回合结束连接即还（挂起会话零 CLI 进程），
回合失败后下一条指令天然是新连接，无需重连机制。会话对象来自可注入工厂
（生产包装 ClaudeSDKClient，测试注入脚本化假实现），工厂以续接源
session_id 调用、返回支持 async with 的对象。

停止的服务端语义：request_stop 置 stop_requested 后由 HTTP 层调
interrupt；run_turn 在回合收尾按该标记区分 turn.stopped 与
turn.completed（真 SDK 被打断的回合以 result=None 的 error Result 收尾，
但判定以本端标记为权威）。

回合四收尾：turn.started（与 user.message 配对）→ turn.completed（成功
Result）/ turn.stopped（用户 interrupt）/ turn.failed（错误 Result、连接
异常、其他错误——一律回 READY，会话不因回合失败终结）。回合执行不设
服务端超时：单回合即一条完整部署流水线，四阶段串行 + 云操作轮询可远超
小时级，主动掐断会把已提交的云操作留在中间态；回合收尾依赖 SDK 侧最终
产出 Result 或异常，CLI 挂死时 run 停 RUNNING，由用户停止/结束兜底。

end 的收尾序列（RUNNING 中结束）：调用方先 end 校验、task.cancel 取消在
飞回合 → run_turn 的 CancelledError 分支不再补回合收尾事件 → 调用方补
session.ended 作为流的最后一条事件。
"""
import asyncio

from .normalize import is_final_result, normalize_message
from .redact import redact_text
from .runs import READY


class TurnFailure(Exception):
    """回合终局失败（Result 错误 subtype）：run_turn 的异常收尾转 turn.failed。"""


async def run_turn(run, text, session_factory, store, on_change=None):
    """执行一个回合：起按回合连接 → 推指令 → 消费消息流至 Result → 收尾。

    收尾即回 READY（completed / stopped / failed 三途同归，会话存续）；
    被取消（end 打断）是唯一例外——状态交调用方处置（ENDED 收尾）。
    on_change 在回合收尾后回调（服务端挂簿记落盘用，None 为无簿记）。
    """
    def changed():
        if on_change is not None:
            on_change()

    try:
        store.append(run.run_id, "turn.started", {})
        store.append(run.run_id, "user.message", {"text": text})
        # resume 源：本会话已建立的 SDK 身份（上回合 Result 提取）优先，
        # 首回合（克隆/恢复带入的源 session）用 resume_session_id
        async with session_factory(run.session_id or run.resume_session_id) as session:
            run.session = session
            await session.query(text)
            await _drain(run, session, store)
        run.status = READY
        changed()
    except asyncio.CancelledError:
        # end 打断在飞回合：回合不补收尾事件，状态由调用方置 ENDED
        raise
    except Exception as exc:  # noqa: BLE001 —— 回合内任何异常都落到 turn.failed，错误摘要过脱敏
        run.status = READY
        store.append(run.run_id, "turn.failed", {"message": redact_text(str(exc))})
        changed()


async def _drain(run, session, store):
    """消费一个回合的消息流至 Result（或流结束），收尾交 _finish。"""
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
    _finish(run, store, final)


def _finish(run, store, message):
    """回合收尾：停止请求优先（turn.stopped）；Result 的错误 subtype 以
    TurnFailure 抛给 run_turn 的异常收尾（turn.failed）。

    与 request_stop 的标记置位同为同步块，在单线程事件循环上互斥执行，
    不存在「半停半完成」的交错。subtype 判定取兜底：只有 success 是正常
    完成，其余（error_max_turns、执行错误等文案不可穷尽）一律失败。
    """
    if run.stop_requested:
        run.stop_requested = False
        store.append(run.run_id, "turn.stopped", {})
    elif message is None or message.get("subtype") == "success":
        result = message.get("result", "") if message else ""
        store.append(run.run_id, "turn.completed", {"result": redact_text(result)})
    else:
        subtype = message.get("subtype") or "unknown"
        detail = redact_text(str(message.get("result") or "")).strip()
        raise TurnFailure(f"回合以 {subtype} 终止" + (f"：{detail}" if detail else ""))
