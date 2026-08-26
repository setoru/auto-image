"""Run 状态机与并发规则。

RUNNING 至多一个：新建会话、向挂起会话发指令都要求当前无会话在执行；
WAITING_INPUT（挂起）的会话可多个并存。回合与会话分离：回合完成、被停止
或部署失败都不结束会话，会话只能被用户关闭（CANCELED）或异常终止（FAILED）。

ENDED 是重启找回的历史 run（见 rebuild.py）：上个进程生命周期的记录，
可回看、可作为续接起点，但不接受干预——终态语义与 CANCELED / FAILED 一致。

活跃 = RUNNING / WAITING_INPUT；干预端点（stop / messages / cancel）对
非活跃（终态）run 一律拒绝（run_not_active）。
"""
import asyncio
import itertools
import time

WAITING_INPUT = "WAITING_INPUT"
RUNNING = "RUNNING"
CANCELED = "CANCELED"
FAILED = "FAILED"
ENDED = "ENDED"
TERMINAL = {CANCELED, FAILED, ENDED}


class Conflict(Exception):
    """并发规则拒绝（转成 HTTP 409，detail 即判定值）。"""

    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


class Run:
    def __init__(self, run_id):
        self.run_id = run_id
        self.status = WAITING_INPUT
        self.stage = None
        self.created_at = time.time()
        self.first_prompt = None
        self.pending_prompt = None
        self.next_input = asyncio.Event()
        self.session = None
        self.task = None
        self.session_id = None        # SDK 会话 id（回合 Result 提取，resume_from 用）
        self.resume_session_id = None  # 创建时携带的续接源会话 id（工厂参数）
        self.resumed_from = None      # 续接来源 run_id（对外呈现）
        self.stop_requested = False   # 停止请求标记：run_agent 在回合收尾消费
        self.ended_at = None          # 终态时刻（历史回看的时长上限；非终态为 None）

    def summary(self):
        return {
            "run_id": self.run_id,
            "status": self.status,
            "stage": self.stage,
            "first_prompt": self.first_prompt,
            "started_at": self.created_at,
            "ended_at": self.ended_at,
            "resumed_from": self.resumed_from,
        }


class RunManager:
    def __init__(self):
        self.runs = {}
        self._ids = itertools.count(1)

    def get(self, run_id):
        return self.runs.get(run_id)

    def register(self, run):
        """注册重启重建的历史 run（不经 create 的并发校验：启动时无执行）。"""
        self.runs[run.run_id] = run

    def adopt_ids(self, run_ids):
        """恢复既有 run_id 后把计数器前拨过已用号，新建不撞号（run_N 解析
        数字取 max；解析不出的 id 与计数序列无关，跳过）。"""
        top = 0
        for run_id in run_ids:
            _, _, num = run_id.rpartition("run_")
            if num.isdigit():
                top = max(top, int(num))
        if top >= next(self._ids):
            self._ids = itertools.count(top + 1)

    def summaries(self):
        """全部 run 摘要，最后活跃在前：终态按结束时刻（重建 run 即
        transcript 的 last_modified，续接过一次的会话浮到最新），活跃按
        创建时刻（无结束时刻）。"""
        return [r.summary() for r in sorted(self.runs.values(), key=lambda r: r.ended_at or r.created_at, reverse=True)]

    def running(self):
        return next((r for r in self.runs.values() if r.status == RUNNING), None)

    def create(self, resume_from=None):
        """新建空会话；resume_from 指向终态 run 时携带其 session_id 续接
        （run 的存在性由调用方先行判定，此处只校验终态）。"""
        if self.running() is not None:
            raise Conflict("deployment_in_progress")
        source = None
        if resume_from is not None:
            source = self.runs[resume_from]
            if source.status not in TERMINAL:
                raise Conflict("session_in_use")
        run = Run(f"run_{next(self._ids)}")
        if source is not None:
            run.resumed_from = source.run_id
            run.resume_session_id = source.session_id
            # 任务名继承源头最早标题：接续会话与源是同一任务的延续，
            # 不随接续后的首条新消息改名（intervene 只对无名 run 命名）
            run.first_prompt = source.first_prompt
        self.runs[run.run_id] = run
        return run

    def intervene(self, run, text=None):
        """干预：本会话回合执行中先请求停止；text 给定时随后投递。

        只同步完成状态与标记置位（与 run_agent 的回合收尾互斥、无交错），
        打断动作（session.interrupt）由调用方在本方法返回后执行。
        挂起且无 text 时无回合可停，幂等无操作。
        """
        if run.status in TERMINAL:
            raise Conflict("run_not_active")
        if run.status == RUNNING:
            run.stop_requested = True
        elif text is None:
            return
        elif self.running() is not None:
            raise Conflict("execution_in_progress")
        if text is not None:
            run.status = RUNNING
            if run.first_prompt is None:
                run.first_prompt = text
            run.pending_prompt = text
            run.next_input.set()

    def cancel(self, run):
        """关闭会话的前置校验；取消动作（task.cancel 与收尾）由调用方执行。"""
        if run.status in TERMINAL:
            raise Conflict("run_not_active")
