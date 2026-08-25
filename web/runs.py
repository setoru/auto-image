"""Run 状态机与并发规则。

RUNNING 至多一个：新建会话、向挂起会话发指令都要求当前无会话在执行；
WAITING_INPUT（挂起）的会话可多个并存。回合与会话分离：回合完成、被停止
或部署失败都不结束会话，会话只能被用户关闭（CANCELED）或异常终止（FAILED）。

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
TERMINAL = {CANCELED, FAILED}


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
        self.output_dir = None        # 产物目录（INSTALL 后发现，见 artifacts.py）

    def summary(self):
        return {
            "run_id": self.run_id,
            "status": self.status,
            "stage": self.stage,
            "first_prompt": self.first_prompt,
            "started_at": self.created_at,
            "resumed_from": self.resumed_from,
        }


class RunManager:
    def __init__(self):
        self.runs = {}
        self._ids = itertools.count(1)

    def get(self, run_id):
        return self.runs.get(run_id)

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
