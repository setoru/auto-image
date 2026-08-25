"""服务重启后的历史重建：以 session 粒度从 CLI 侧 transcript 找回历史 run。

内存 run 记录随重启丢失，SDK 会话的 transcript 仍在：list_sessions(项目根)
给出本项目的全部会话（first_prompt / created_at 等元信息），get_session_messages
给出可见消息链，经 normalize_message 同一条映射路径重放为内部事件。重建的
run 是纯历史记录：状态 ENDED（终态，可回看、可作为续接起点），无协程、
不接受干预——重启前正在执行的任务不会自动重试，执行权不恢复。

transcript 里没有 Result 消息：回合边界由「下一条真实用户输入」推导，回合
汇总取该回合最后一条 agent 文本（CLI 的 result 同源于此）。每条重建的事件
流以 run.ended 收尾，标记这是重启找回的历史——前端据此关闭只读回放流，
不再重连。
"""
import logging
import time

from .normalize import normalize_message
from .redact import redact_text
from .runs import ENDED, Run

logger = logging.getLogger("web")


def rebuild_history(manager, store, list_sessions, get_session_messages):
    """启动时重建全部可找回的历史 run，返回重建的 run 列表（新修改的在前）。

    历史读取失败只跳过对应会话（空 transcript、损坏文件），不阻断服务启动
    ——重建是找回尽量多的历史，不是启动的前置条件。
    """
    try:
        infos = list_sessions()
    except Exception:  # noqa: BLE001 —— 发现层失败不牵连服务本身
        logger.warning("list_sessions 失败，本次启动无历史重建", exc_info=True)
        return []
    rebuilt = []
    for info in infos:
        try:
            messages = get_session_messages(info.session_id)
        except Exception:  # noqa: BLE001 —— 单条会话损坏只跳过该条
            logger.warning("读取会话 %s 的 transcript 失败，跳过该历史", info.session_id, exc_info=True)
            continue
        if not messages:
            continue
        rebuilt.append(_rebuild_run(manager, store, info, messages))
    return rebuilt


def user_prompt_text(message):
    """真实用户指令文本：content 为字符串或纯 text 块；tool_result 行返回 None。

    transcript 的 user 行两类混杂（指令原文与工具结果回填），只有前者构成
    回合边界；空文本视为无指令（与干预端点的非空校验一致）。
    """
    if message.get("type") != "user":
        return None
    content = message.get("message", {}).get("content")
    if isinstance(content, str):
        return content or None
    if not isinstance(content, list):
        return None
    blocks = [b for b in content if isinstance(b, dict)]
    if not blocks or any(b.get("type") != "text" for b in blocks):
        return None
    text = "\n".join(b.get("text", "") for b in blocks if isinstance(b.get("text"), str))
    return text or None


def _rebuild_run(manager, store, info, messages):
    run = Run(_history_run_id(manager, info.session_id))
    run.status = ENDED
    run.session_id = info.session_id
    run.first_prompt = getattr(info, "first_prompt", None)
    started_ms = getattr(info, "created_at", None) or getattr(info, "last_modified", 0)
    run.created_at = started_ms / 1000 if started_ms else time.time()
    run.ended_at = (getattr(info, "last_modified", 0) or 0) / 1000 or None
    manager.register(run)
    store.create(run.run_id)
    store.append(run.run_id, "run.started", {})
    tool_names = {}
    last_text = ""     # 当前回合最后一条 agent 文本（回合汇总来源）
    turn_open = False  # 是否有未收尾的回合（首条用户输入之后为真）
    for message in messages:
        raw = {"type": message.type, "message": message.message}
        prompt = user_prompt_text(raw)
        if prompt is not None:
            if turn_open:
                store.append(run.run_id, "turn.completed", {"result": redact_text(last_text)})
            store.append(run.run_id, "user.message", {"text": prompt})
            if run.first_prompt is None:
                run.first_prompt = prompt
            turn_open, last_text = True, ""
            continue
        for etype, payload in normalize_message(raw, tool_names):
            store.append(run.run_id, etype, payload)
            if etype == "stage.changed":
                run.stage = payload["stage"]
            elif etype == "agent.message":
                last_text = payload["text"]
    if turn_open:
        store.append(run.run_id, "turn.completed", {"result": redact_text(last_text)})
    store.append(run.run_id, "run.ended", {})
    return run


def _history_run_id(manager, session_id):
    """历史 run 的稳定标识：session 前缀派生，重启多次重建同一 run 不改名。

    与 create 的计数 id（纯数字后缀）不冲突；前缀撞车时逐段加长。
    """
    for size in (8, 12, 16, len(session_id)):
        run_id = f"run_hist_{session_id[:size]}"
        if run_id not in manager.runs:
            return run_id
    raise ValueError(f"session {session_id} 无法分配唯一 run id")
