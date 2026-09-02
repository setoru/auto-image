"""进程内事件存储：seq 递增、Last-Event-ID 断点重放、订阅者唤醒。

单进程单 worker 前提下的最简实现（dict + asyncio.Event 广播）。
每条事件带 ts（服务端时刻，秒）——前端时长的冻结点（最后活动时刻）与
累计执行时长的回合分段都以它为准，不受页面刷新/SSE 全量重放影响。
"""
import asyncio
import contextlib
import time
from collections import defaultdict


class EventStore:
    def __init__(self):
        self._events = defaultdict(list)
        self._subscribers = defaultdict(set)
        self._runs = None

    def bind_runs(self, runs):
        """挂接 run 表（app 装配时调用）：append 即同步 run.last_event_at，
        时长冻结点只此一处更新，摘要与簿记落盘都读它。"""
        self._runs = runs

    def create(self, run_id):
        self._events[run_id] = []
        self._subscribers[run_id] = set()

    def append(self, run_id, etype, payload):
        """追加一条内部事件，返回带递增 seq 与 ts 的完整事件。"""
        event = {
            "seq": len(self._events[run_id]) + 1,
            "ts": time.time(),
            "run_id": run_id,
            "type": etype,
            "payload": payload,
        }
        self._events[run_id].append(event)
        if self._runs is not None:
            run = self._runs.get(run_id)
            if run is not None:
                run.last_event_at = event["ts"]
        for flag in self._subscribers[run_id]:
            flag.set()
        return event

    def replay_from(self, run_id, after_seq):
        """返回 seq 严格大于 after_seq 的全部事件（断点重放用）。"""
        return [ev for ev in self._events[run_id] if ev["seq"] > after_seq]

    def adopt_history(self, dst_run_id, src_run_id, skip_types=()):
        """把源 run 的全部事件转录进目标 run（seq 重新递增、唤醒订阅者）——
        克隆创建的新会话由此自带源会话历史（CLI resume 的浏览体验）。
        skip_types 排除源流的生命周期事件（session.started / session.ended：
        起点会与新流重复，终态收尾会被前端当成本流终态关流判死）。"""
        for ev in self._events[src_run_id]:
            if ev["type"] not in skip_types:
                self.append(dst_run_id, ev["type"], ev["payload"])

    def is_complete(self, run_id, seen_seq):
        """seen_seq 已追上存储末尾（无更多事件可重放）。"""
        return not self.replay_from(run_id, seen_seq)

    @contextlib.contextmanager
    def subscribe(self, run_id):
        """注册一个唤醒信号；append 时被 set，订阅方自行 clear 后等待。

        SSE 循环须先 clear 再重放再 wait：clear 后到达的事件经 set 唤醒等待方，
        clear 前到达的事件由随后的重放覆盖，两个方向都不丢。
        """
        flag = asyncio.Event()
        self._subscribers[run_id].add(flag)
        try:
            yield flag
        finally:
            self._subscribers[run_id].discard(flag)
