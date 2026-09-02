"""落盘的 run 簿记：挂起会话的服务重启恢复（可继续对话，不用手动接续）。

对话内容的单一事实源是 CLI 侧 transcript（~/.claude/projects），本模块只存
transcript 里没有的服务簿记：run ↔ session 映射与状态机状态。只入册活跃
run（READY / RUNNING）——终态 run（ENDED）由 rebuild 从 transcript 找回，
无 session_id 的 run（首回合未完成即中断）无法 resume，均不入册。

写入为全量原子替换（tmp + rename），每次状态变更即写：run 数量小、字段
十来项，代价可忽略；kill -9 的窗口内最多丢最后一次变更，由 transcript
存在性校验兜底（读不到即丢弃该条）。文件缺失/损坏/形状不对一律返回空册，
服务照常启动（降级为纯 rebuild 语义，不阻断）。
"""
import json
import logging
import os
import tempfile
from pathlib import Path

from .runs import READY, RUNNING

ACTIVE = {READY, RUNNING}
logger = logging.getLogger("web")


def save_state(runs, path):
    """全部 run 里的活跃者全量落盘（原子替换）。失败只告警不抛——落盘是
    恢复增强，不能反过来打断会话执行。无 session_id 的活跃 run（首回合
    未完成）无法 resume，不入册。"""
    records = [_record(r) for r in runs if r.status in ACTIVE and r.session_id]
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"runs": records}, f, ensure_ascii=False)
            os.replace(tmp, target)
        except BaseException:
            os.unlink(tmp)
            raise
    except OSError:
        logger.warning("状态落盘失败（重启恢复能力降级，不影响会话执行）", exc_info=True)


def load_state(path):
    """读回记录列表；缺失/损坏/形状不对返回 []（降级，不阻断启动）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    records = data.get("runs") if isinstance(data, dict) else None
    if not isinstance(records, list):
        return []
    return [r for r in records if _well_formed(r)]


def _record(run):
    return {
        "run_id": run.run_id,
        "status": run.status,
        "stage": run.stage,
        "first_prompt": run.first_prompt,
        "title": run.title,
        "created_at": run.created_at,
        "last_event_at": run.last_event_at,
        "session_id": run.session_id,
        "resumed_from": run.resumed_from,
    }


def _well_formed(record):
    """可恢复的最低形状：run_id / session_id / 状态 / 时间齐备且类型正确。
    session_id 为空的活跃 run 无法 resume，恢复侧无从处理，视同不入册。"""
    return (
        isinstance(record, dict)
        and isinstance(record.get("run_id"), str)
        and record.get("run_id") != ""
        and isinstance(record.get("session_id"), str)
        and record.get("session_id") != ""
        and record.get("status") in ACTIVE
        and isinstance(record.get("created_at"), (int, float))
    )
