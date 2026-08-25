"""Run 状态机与并发规则。

RUNNING 至多一个：新建会话、向挂起会话发指令都要求当前无会话在执行；
WAITING_INPUT（挂起）的会话可多个并存。回合与会话分离：回合完成或部署
失败都不结束会话，会话只能被用户关闭（CANCELED）或异常终止（FAILED）。
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

    def summary(self):
        return {
            "run_id": self.run_id,
            "status": self.status,
            "stage": self.stage,
            "first_prompt": self.first_prompt,
            "started_at": self.created_at,
        }


class RunManager:
    def __init__(self):
        self.runs = {}
        self._ids = itertools.count(1)

    def get(self, run_id):
        return self.runs.get(run_id)

    def running(self):
        return next((r for r in self.runs.values() if r.status == RUNNING), None)

    def create(self):
        if self.running() is not None:
            raise Conflict("deployment_in_progress")
        run = Run(f"run_{next(self._ids)}")
        self.runs[run.run_id] = run
        return run

    def send(self, run, text):
        """向会话投递一条指令：事件流先 user.message，再回 agent 消息。"""
        if run.status in TERMINAL:
            raise Conflict("run_not_active")
        # 有会话在执行即拒绝（含本会话回合执行中：本版本直接拒绝，
        # 「先停止再投递」随干预语义加入）
        if self.running() is not None:
            raise Conflict("execution_in_progress")
        if run.first_prompt is None:
            run.first_prompt = text
        run.pending_prompt = text
        run.status = RUNNING
        run.next_input.set()
