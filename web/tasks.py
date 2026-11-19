"""流水线任务跟踪：rpm-*/deploy-* 子 agent 派发即建任务，落 task/ 目录。

任务 = 一个会话的流水线跟踪单元（镜像：guide→install→verify→archive；
RPM：guide→build→verify→archive），跨回合续作复用。识别信号是 stage.changed 事件
（normalize.STAGE_BY_SUBAGENT，payload 带 subagent 用于分流类型）；
rpm-check / hce-rpm-check 是 skill 不是子 agent，天然不产生任务。

数据来源与局限（如实）：
- 自动任务标题、软件、实际版本及路径：执行模型随阶段派发提供 task_info。
  未核实值保持空；明确的安装元数据文件提供机器身份，不扫描产物猜归属。
  手动任务沿用表单字段及原有产物确认行为。
- token：回合收尾事件的 usage（CLI 的 Result 累计值；resume 会话可能
  为全程累计而非本回合增量，按键取 max 幂等合并）。尚无收尾 usage
  的任务为 null（前端显示 —），续作保留此前累计值。
- 机器：镜像任务的 install-meta.json（instance_id/server_alias）；
  RPM 流水线不落 meta，instance_id 恒 null。

观察者在事件流上同步运行：文件系统扫描（产物确认）只在阶段推进与
回合收尾时触发，量级为百级文件；持久化失败只记日志绝不抛——旁路
消费绝不影响回合执行。克隆转录（adopt_history）与服务重启恢复重放
都不进观察者，历史不是新事实；重启后 RUNNING 任务按 interrupted
收尾（recover）。
"""
import json
import logging
import math
import os
import secrets
import tempfile
import time
from datetime import datetime
from pathlib import Path

import yaml

from .artifacts import CONFIG_KEY_STAGES, RPM_FILE_STAGES, resolve
from .normalize import ARCHIVE, BUILD, GUIDE, INSTALL, VERIFY

ALL_STAGES = (GUIDE, INSTALL, BUILD, VERIFY, ARCHIVE)

# usage 的 CLI 键 → 任务存储键（cache 两键分开存，前端汇总展示）
USAGE_KEYS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_input_tokens": "cache_read_tokens",
    "cache_creation_input_tokens": "cache_creation_tokens",
}

TURN_OUTCOMES = {
    "turn.completed": "success",
    "turn.failed": "failed",
    "turn.stopped": "stopped",
    "turn.interrupted": "interrupted",
}

TYPE_LABELS = {"image": "镜像", "rpm": "RPM"}

# 手动任务 ECS 参数的展示序与中文名（build_task_prompt 的 k=v 列表用）
ECS_PARAM_LABELS = (
    ("image", "镜像"),
    ("flavor", "规格"),
    ("name", "名称"),
    ("disk_size", "系统盘GB"),
    ("bandwidth", "带宽Mbit/s"),
)

def _task_name(task):
    """自动任务用模型标题；手动任务用表单字段，时间始终取原创建时刻。"""
    label = TYPE_LABELS.get(task["type"], task["type"])
    stamp = datetime.fromtimestamp(task["created_at"]).strftime("%Y%m%d%H%M")
    if task.get("origin") != "manual":
        title = task.get("title") or f"{label}任务"
        return f"{title} ({stamp})"
    parts = [label]
    if task["software"]:
        parts.append(task["software"])
        if task["version"]:
            parts.append(task["version"])
    else:
        parts.append("未知")
    return f"{' '.join(parts)} ({stamp})"


def build_task_prompt(spec):
    """手动任务 spec → 部署指令（发给新建会话的首条 user 消息）。

    指令带 deploy/rpm skill 的触发词与参数表述：类型决定流水线；已有
    ECS 给 ssh 别名（流水线命名规约 ecs-<别名>，去前缀即别名；外来机器
    无从确定别名，给 name+IP 让 agent 经 ssh-skill 解析）；按需创建把
    填写过的 ECS 参数以 k=v 透传（其余用 scope 默认）。
    """
    software = str(spec.get("software") or "").strip()
    version = str(spec.get("version") or "").strip() or "最新稳定版"
    ttype = spec.get("task_type") or "image"
    install_doc = str(spec.get("install_doc") or "").strip()

    if ttype == "rpm":
        head = f"制作 {software} {version} 的 RPM 包，并完成构建、验证与归档。"
    else:
        head = f"一键部署 {software} {version}，部署并打包制镜像。"

    machine = ""
    if spec.get("ecs_mode") == "existing":
        inst = spec.get("ecs_instance") or {}
        name = str(inst.get("name") or "").strip()
        ip = str(inst.get("ip") or "").strip()
        if name.startswith("ecs-") and len(name) > 4:
            machine = f"目标服务器别名：{name[4:]}。"
        else:
            machine = (f"目标机器：已有 ECS「{name}」（IP {ip}）。"
                       "请用 ssh-skill 解析其登录方式（已注册别名则用别名，"
                       "否则按 IP 注册后再执行）。")
        instance_id = str(inst.get("id") or "").strip()
        if instance_id:
            machine += f"已有 ECS instance_id：{instance_id}，IP：{ip}。"
            machine += "执行前核对登录目标与此实例身份，将已知身份写入本版本安装元数据并传给后续阶段。"
    else:
        params = spec.get("ecs_params") or {}
        parts = [f"{label}={params[key]}" for key, label in ECS_PARAM_LABELS
                 if params.get(key) not in (None, "")]
        if parts:
            machine = f"目标机器：按需创建 ECS（ECS 规格：{'；'.join(parts)}，其余用 scope 默认）。"
        else:
            machine = "目标机器：按需创建 ECS（规格用 scope 默认）。"

    doc = f"安装文档：{install_doc}。" if install_doc else "安装文档：无现成链接，请检索官方最新稳定版文档。"
    prompt = f"{head}{machine}{doc}"
    extra = str(spec.get("extra_instructions") or "").strip()
    return f"{prompt}\n\n补充要求：\n{extra}" if extra else prompt


def _manual_fields(spec):
    """完整表单替换任务的请求字段与目标展示，不保留旧 ECS 模式的派生值。"""
    software = str(spec.get("software") or "").strip()
    version = str(spec.get("version") or "").strip() or None
    ttype = spec.get("task_type") or "image"
    fields = {
        "type": ttype,
        "software": software or None,
        "version": version,
        "confirmed": bool(software),
        "server_alias": None,
        "instance_id": None,
        "spec": {
            "software": software,
            "version": version,
            "task_type": ttype,
            "ecs_mode": spec.get("ecs_mode") or "create",
            "ecs_instance": spec.get("ecs_instance"),
            "ecs_params": spec.get("ecs_params"),
            "install_doc": str(spec.get("install_doc") or "").strip() or None,
            "extra_instructions": str(spec.get("extra_instructions") or "").strip(),
        },
    }
    if ttype == "image" and spec.get("ecs_mode") == "existing":
        inst = spec.get("ecs_instance") or {}
        fields["instance_id"] = str(inst.get("id") or "").strip() or None
        name = str(inst.get("name") or "")
        if name.startswith("ecs-") and len(name) > 4:
            fields["server_alias"] = name[4:]
    return fields


class TaskStore:
    """任务注册表：事件观察者驱动 + task/ 目录持久化（每任务一个 JSON）。"""

    def __init__(self, task_dir, artifact_roots=None, deploy_config=None):
        self._dir = Path(task_dir)
        roots = artifact_roots or {}
        self._roots = {name: Path(p) for name, p in roots.items()}
        config_path = Path(deploy_config) if deploy_config is not None else Path(__file__).resolve().parent.parent / "deploy.config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self._deploy_patterns = tuple(config[key] for key in CONFIG_KEY_STAGES)
        self._meta_pattern = config["install_meta_file"]
        self._tasks = {}            # task_id → 任务记录
        self._by_run = {}           # run_id → 首个 task_id（跨回合保留）
        self._turn_text = {}        # run_id → 本回合原始指令（执行记录）

    # ---- 手动任务（面板「+ 新建任务」→ INIT → 「▶ 运行」起会话）----

    def get(self, task_id):
        return self._tasks.get(task_id)

    def create_manual(self, spec):
        """表单 spec → INIT 任务（待运行；点运行才建会话发指令）。"""
        now = time.time()
        task_id = ("task-"
                   + datetime.fromtimestamp(now).strftime("%Y%m%d%H%M%S")
                   + "-" + secrets.token_hex(2))
        task = {
            "task_id": task_id,
            "run_id": None,
            "origin": "manual",
            "status": "INIT",
            "outcome": None,
            "name": "",  # _task_name 填
            "stages": [],
            "current_stage": None,
            "usage": None,
            "turn_text": "",
            "created_at": now,
            "updated_at": now,
            "ended_at": None,
            **_manual_fields(spec),
        }
        task["name"] = _task_name(task)
        self._save(task, strict=True)
        self._tasks[task_id] = task
        return task

    def update_manual(self, task_id, spec):
        """完整替换待运行表单；先落盘再发布，失败时仍保留原来的共享任务。"""
        task = self._tasks.get(task_id)
        if task is None or task.get("status") != "INIT":
            return None
        updated = {**task, **_manual_fields(spec)}
        # updated_at 同时供共享编辑检查使用，每次保存都必须获得新值。
        updated["updated_at"] = max(time.time(), math.nextafter(task["updated_at"], math.inf))
        updated["name"] = _task_name(updated)
        self._save(updated, strict=True)
        self._tasks[task_id] = updated
        return updated

    def mark_running(self, task_id, run_id, prompt):
        """INIT → RUNNING：绑定会话并预登记观察者映射（先于 start_turn 调用，
        首个 stage.changed 即认领本任务而非新建 auto 任务）。"""
        task = self._tasks.get(task_id)
        if task is None or task.get("status") != "INIT":
            return None
        task["status"] = "RUNNING"
        task["run_id"] = run_id
        task["turn_text"] = prompt
        task["updated_at"] = time.time()
        self._by_run[run_id] = task_id
        self._turn_text[run_id] = prompt
        self._save(task)
        return task

    # ---- 事件入口（EventStore 观察者）----

    def handle_event(self, event):
        etype = event.get("type")
        run_id = event.get("run_id")
        ts = event.get("ts") or time.time()
        payload = event.get("payload") or {}
        if not run_id:
            return
        if etype == "user.message":
            self._turn_text[run_id] = str(payload.get("text") or "")
            return
        if etype == "stage.changed":
            self._on_stage(run_id, payload, ts)
            return
        if etype in TURN_OUTCOMES:
            self._on_turn_end(run_id, TURN_OUTCOMES[etype], payload, ts)
            return
        if etype == "session.ended":
            # RUNNING 中结束会话：回合被取消且不补收尾事件——在此收尾任务
            self._on_turn_end(run_id, "interrupted", payload, ts)

    def _on_stage(self, run_id, payload, ts):
        stage = payload.get("stage")
        if stage not in ALL_STAGES:
            return
        task_id = self._by_run.get(run_id)
        if task_id:
            task = self._tasks[task_id]
            task["status"] = "RUNNING"
            task["outcome"] = None
            task["ended_at"] = None
            stages = task["stages"]
            # 相邻同阶段重复派发（guide 重试）：不新增条目
            if stages and stages[-1]["stage"] == stage and stages[-1]["ended_at"] is None:
                task["updated_at"] = ts
            else:
                if stages and stages[-1]["ended_at"] is None:
                    stages[-1]["ended_at"] = ts
                stages.append({"stage": stage, "started_at": ts, "ended_at": None})
                task["current_stage"] = stage
                task["updated_at"] = ts
        else:
            task = self._create(run_id, payload.get("subagent"), stage, ts)
        self._apply_task_info(task, payload.get("task_info"))
        self._maybe_confirm(task)
        self._save(task)

    def _on_turn_end(self, run_id, outcome, payload, ts):
        task_id = self._by_run.get(run_id)
        self._turn_text.pop(run_id, None)
        if not task_id:
            return
        task = self._tasks[task_id]
        # 普通聊天没有重开流水线；重复收尾也不覆盖已有结果。
        if task["status"] != "RUNNING":
            return
        stages = task["stages"]
        if stages and stages[-1]["ended_at"] is None:
            stages[-1]["ended_at"] = ts
        task["status"] = "DONE"
        task["outcome"] = outcome
        task["current_stage"] = None
        task["ended_at"] = ts
        task["updated_at"] = ts
        usage = payload.get("usage")
        if isinstance(usage, dict):
            self._merge_usage(task, usage)
        self._maybe_confirm(task)
        self._save(task)

    def _create(self, run_id, subagent, stage, ts):
        ttype = "rpm" if str(subagent or "").startswith("rpm-") else "image"
        task_id = ("task-"
                   + datetime.fromtimestamp(ts).strftime("%Y%m%d%H%M%S")
                   + "-" + secrets.token_hex(2))
        task = {
            "task_id": task_id,
            "run_id": run_id,
            "origin": "auto",
            "type": ttype,
            "status": "RUNNING",
            "outcome": None,
            "name": "",
            "title": None,
            "software": None,
            "version": None,
            "output_dir": None,
            "install_meta_file": None,
            "confirmed": False,
            "stages": [{"stage": stage, "started_at": ts, "ended_at": None}],
            "current_stage": stage,
            "usage": None,
            "server_alias": None,
            "instance_id": None,
            "turn_text": self._turn_text.get(run_id, ""),
            "created_at": ts,
            "updated_at": ts,
            "ended_at": None,
        }
        task["name"] = _task_name(task)
        self._tasks[task_id] = task
        self._by_run[run_id] = task_id
        return task

    def _apply_task_info(self, task, info):
        """采用 normalize 已解析的模型信息，首个有效标题保持稳定。"""
        if task.get("origin") == "manual" or info is None:
            return
        if task.get("title") is None:
            task["title"] = info["title"]
            task["name"] = _task_name(task)
        for source, target in (
            ("software", "software"),
            ("actual_version", "version"),
            ("output_dir", "output_dir"),
            ("install_meta_file", "install_meta_file"),
        ):
            if info[source] is not None:
                task[target] = info[source]
        task["confirmed"] = bool(task["software"] and task["version"])

    # ---- 产物确认（软件/版本/机器）----

    def _maybe_confirm(self, task):
        """自动任务读明确机器记录；手动任务沿用原产物确认规则。"""
        if task.get("origin") != "manual":
            if task["type"] == "image" and task.get("install_meta_file"):
                self._read_install_meta(task)
            return
        if task["confirmed"] and task["software"] and task["version"]:
            if task["type"] == "image":
                self._read_install_meta(task)
            return
        found = self._scan_candidate(task)
        if found is not None:
            _root, software, version = found
            task["software"] = software
            task["version"] = version
            task["confirmed"] = True
            task["name"] = _task_name(task)
        if task["type"] == "image" and task["confirmed"] and task["software"] and task["version"]:
            self._read_install_meta(task)

    def _scan_candidate(self, task):
        """手动任务只扫描同类型真实阶段产物；多个近期目录时不选择。"""
        root_name = "rpm" if task["type"] == "rpm" else "deploy"
        root = self._roots.get(root_name)
        if root is None or not root.is_dir():
            return None
        candidates = []
        for sw_dir in _iterdirs(root):
            for ver_dir in _iterdirs(sw_dir):
                if root_name == "deploy":
                    names = {pattern.replace("{{software}}", sw_dir.name).replace("{{version}}", ver_dir.name)
                             for pattern in self._deploy_patterns}
                    files = (ver_dir / name for name in names)
                else:
                    files = (path for path in ver_dir.rglob("*")
                             if any(path.name.endswith(suffix) for suffix, _stage in RPM_FILE_STAGES)
                             or path.name == "install-rpm.sh")
                newest = _newest_mtime(files)
                if newest is not None and newest >= task["created_at"] - 2:
                    candidates.append((root_name, sw_dir.name, ver_dir.name))
        return candidates[0] if len(candidates) == 1 else None

    def _read_install_meta(self, task):
        """镜像任务：自动任务明确文件、手动任务配置路径 → 机器标识。

        create 路径有 instance_id；已有别名路径只有 server_alias（名字兜底
        join 靠它）。解析失败静默跳过（下次阶段推进再试）。
        """
        base = self._roots.get("deploy")
        if base is None:
            return
        try:
            if task.get("origin") == "manual":
                software, version = str(task["software"]), str(task["version"])
                if any(Path(part).name != part or part in (".", "..") for part in (software, version)):
                    return
                filename = self._meta_pattern.replace("{{software}}", software).replace("{{version}}", version)
                meta_file = base / software / version / filename
                if not meta_file.resolve().is_relative_to(base.resolve()):
                    return
            else:
                found = resolve({"deploy": base}, (), task.get("install_meta_file"))
                if found is None:
                    return
                _entry, meta_file = found
            data = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        if any(task[key] and data.get(key) and task[key] != data[key]
               for key in ("instance_id", "server_alias")):
            return
        for key in ("instance_id", "server_alias"):
            if task[key] is None and isinstance(data.get(key), str) and data[key]:
                task[key] = data[key]

    # ---- usage / 持久化 / 恢复 --------

    def _merge_usage(self, task, usage):
        """usage dict 并入任务（键取 max 幂等——CLI 在 resume 上可能重复累计）。"""
        merged = dict(task["usage"] or {})
        for src_key, dst_key in USAGE_KEYS.items():
            try:
                value = int(usage.get(src_key) or 0)
            except (TypeError, ValueError):
                continue
            if value > merged.get(dst_key, 0):
                merged[dst_key] = value
        # 全零 usage 也如实存（回合跑完就有数字）
        task["usage"] = {k: merged.get(k, 0) for k in USAGE_KEYS.values()}
        task["updated_at"] = max(task["updated_at"], task.get("ended_at") or 0)

    def _save(self, task, strict=False):
        """原子落盘；手动保存向调用方报错，事件观察者仍只记日志。"""
        tmp_path = None
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            fd_tmp, tmp_path = tempfile.mkstemp(dir=self._dir, prefix="task-", suffix=".tmp")
            with open(fd_tmp, "w", encoding="utf-8") as fh:
                json.dump(task, fh, ensure_ascii=False, indent=2)
            target = self._dir / f"{task['task_id']}.json"
            os.replace(tmp_path, target)
        except OSError:
            if strict:
                raise
            logging.getLogger("web").warning("任务落盘失败（忽略）：%s",
                                             task.get("task_id"), exc_info=True)
        finally:
            if tmp_path is not None:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except OSError:
                    pass

    def recover(self):
        """启动恢复：加载 task/ 目录；RUNNING 一律按 interrupted 收尾。

        观察者在恢复重放之后才注册，重放事件不进任务面——重启前未收尾
        的任务以 interrupted 定格（已提交的云操作不受影响，与回合语义
        一致）。损坏文件跳过不阻断启动。
        """
        self._tasks = {}
        self._by_run = {}
        self._turn_text = {}
        if not self._dir.is_dir():
            return
        now = time.time()
        for path in sorted(self._dir.glob("task-*.json")):
            try:
                task = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logging.getLogger("web").warning("任务文件损坏（跳过）：%s", path)
                continue
            if not isinstance(task, dict) or not task.get("task_id"):
                continue
            # 旧版本任务文件可能含制作/调用身份；当前模型不再暴露或继续写入这些字段。
            for legacy_key in ("production_id", "revision_id", "call_id"):
                task.pop(legacy_key, None)
            if task.get("status") == "RUNNING":
                stages = task.get("stages") or []
                if stages and stages[-1].get("ended_at") is None:
                    stages[-1]["ended_at"] = now
                task["status"] = "DONE"
                task["outcome"] = "interrupted"
                task["current_stage"] = None
                task["ended_at"] = task.get("updated_at") or now
            self._tasks[task["task_id"]] = task
        # 旧会话可能已有多条按回合创建的记录；保留文件，续作绑定首次任务。
        for task in sorted(self._tasks.values(), key=lambda item: item.get("created_at") or 0):
            if task.get("run_id"):
                self._by_run.setdefault(task["run_id"], task["task_id"])

    def list(self):
        """全部任务（新→旧）；run_id 映射供前端会话互跳。"""
        return sorted(self._tasks.values(),
                      key=lambda t: t.get("created_at") or 0, reverse=True)


def _iterdirs(base):
    try:
        return [p for p in base.iterdir() if p.is_dir()]
    except OSError:
        return []


def _newest_mtime(files):
    """指定阶段产物的最新 mtime；空/不可读返回 None。"""
    newest = None
    try:
        for f in files:
            if f.is_file():
                m = f.stat().st_mtime
                if newest is None or m > newest:
                    newest = m
    except OSError:
        pass
    return newest
