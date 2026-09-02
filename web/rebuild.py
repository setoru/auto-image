"""服务重启后的恢复：挂起会话续命（state 簿记）+ 历史只读重建（transcript）。

内存 run 记录随重启丢失，两个恢复源互补：
- state 落盘簿记（state.py）里的挂起 run → restore_active_runs 恢复为可聊
  会话（原 run_id，send 时以 resume 起新回合）；
- CLI 侧 transcript（~/.claude/projects）→ rebuild_history 以 session 粒度
  找回纯历史 run：状态 ENDED（终态，可回看、可作为克隆起点），不接受干预。

transcript 里没有 Result 消息：回合边界由「下一条真实用户输入」推导，回合
汇总取该回合最后一条 agent 文本（CLI 的 result 同源于此）。历史重建的
事件流以 session.ended 收尾，标记这是重启找回的历史——前端据此关闭只读回放流，
不再重连。（恢复双路径合流为 transcript 单一路径见后续演进。）
"""
import logging
import time

from .normalize import normalize_message
from .redact import redact_text
from .runs import ENDED, RUNNING, READY, Run

logger = logging.getLogger("web")


def rebuild_history(manager, store, list_sessions, get_session_messages, skip_sessions=()):
    """启动时重建全部可找回的历史 run，返回重建的 run 列表（新修改的在前）。

    skip_sessions 为已被挂起恢复占用的 session（同一 transcript 不重复成
    条）。历史读取失败只跳过对应会话（空 transcript、损坏文件），不阻断
    服务启动——重建是找回尽量多的历史，不是启动的前置条件。
    """
    try:
        infos = list_sessions()
    except Exception:  # noqa: BLE001 —— 发现层失败不牵连服务本身
        logger.warning("list_sessions 失败，本次启动无历史重建", exc_info=True)
        return []
    rebuilt = []
    for info in infos:
        if info.session_id in skip_sessions:
            continue
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


def restore_active_runs(manager, store, records, get_session_messages):
    """落盘簿记里的挂起 run → 恢复为可聊会话（原 run_id、事件流从 transcript
    重放），返回恢复的 run 列表（回合任务由 send 按需起：resume_session_id
    带自身 session，回合连接按回合开合）。

    恢复语义：簿记里的 RUNNING 一律降级 READY——重启前的未收尾
    回合不自动重跑（已提交的云操作不可重复执行），以 turn.interrupted 事件
    如实呈现。transcript 读不到（被删/损坏）的记录丢弃，只降级不阻断。
    """
    restored = []
    for record in records:
        try:
            messages = get_session_messages(record["session_id"])
        except Exception:  # noqa: BLE001 —— 单条读不到只丢该条（transcript 为准）
            logger.warning("恢复 %s：session %s 的 transcript 读不到，丢弃",
                           record["run_id"], record["session_id"], exc_info=True)
            continue
        if not messages:
            logger.warning("恢复 %s：session %s 无可见消息，丢弃",
                           record["run_id"], record["session_id"])
            continue
        run = Run(record["run_id"])
        run.status = READY
        run.session_id = record["session_id"]
        run.resume_session_id = record["session_id"]  # 回合以自身 session 续接
        run.resumed_from = record.get("resumed_from")
        run.stage = record.get("stage")
        run.first_prompt = record.get("first_prompt")
        run.title = record.get("title")
        run.created_at = record["created_at"]
        run.last_event_at = record.get("last_event_at")
        manager.register(run)
        manager.adopt_ids([run.run_id])
        store.create(run.run_id)
        store.append(run.run_id, "session.started", {})
        replay_messages(run, store, messages)
        if record.get("status") == RUNNING:
            store.append(run.run_id, "turn.interrupted", {})
        restored.append(run)
        # 上面重放的事件不是真实活动（都是重启当下的时刻），恢复簿记值
        run.last_event_at = record.get("last_event_at")
    return restored


def _rebuild_run(manager, store, info, messages):
    run = Run(_history_run_id(manager, info.session_id))
    run.status = ENDED
    run.session_id = info.session_id
    run.first_prompt = getattr(info, "first_prompt", None)
    # 标题优先读 transcript 的 custom-title 行（title.py 生成后写回），
    # 没有则维持 first_prompt 截断
    run.title = getattr(info, "custom_title", None)
    started_ms = getattr(info, "created_at", None) or getattr(info, "last_modified", 0)
    run.created_at = started_ms / 1000 if started_ms else time.time()
    run.ended_at = (getattr(info, "last_modified", 0) or 0) / 1000 or None
    manager.register(run)
    store.create(run.run_id)
    store.append(run.run_id, "session.started", {})
    replay_messages(run, store, messages)
    store.append(run.run_id, "session.ended", {})
    # 历史无逐事件时刻：最后活动以 transcript 落盘时刻近似（= ended_at）。
    # 上面重建事件流的 ts 都是重启当下的时刻，不是真实活动，恢复后覆盖。
    run.last_event_at = run.ended_at
    return run


def replay_messages(run, store, messages):
    """transcript 可见消息链 → 内部事件流（first_prompt / stage 随重放恢复），
    不含生命周期起止事件——新起点与收尾由调用方决定（历史重建补 session.ended，
    挂起恢复不补）。"""
    tool_names = {}
    last_text = ""     # 当前回合最后一条 agent 文本（回合汇总来源）
    turn_open = False  # 是否有未收尾的回合（首条用户输入之后为真）
    for message in messages:
        raw = {"type": message.type, "message": message.message}
        prompt = user_prompt_text(raw)
        if prompt is not None:
            if turn_open:
                store.append(run.run_id, "turn.completed", {"result": redact_text(last_text)})
            # turn.started 与 user.message 配对（与实时回合一致），SSE 消费端
            # 不用区分实时流与重放流
            store.append(run.run_id, "turn.started", {})
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


def _history_run_id(manager, session_id):
    """历史 run 的稳定标识：session 前缀派生，重启多次重建同一 run 不改名。

    与 create 的计数 id（纯数字后缀）不冲突；前缀撞车时逐段加长。
    """
    for size in (8, 12, 16, len(session_id)):
        run_id = f"run_hist_{session_id[:size]}"
        if run_id not in manager.runs:
            return run_id
    raise ValueError(f"session {session_id} 无法分配唯一 run id")
