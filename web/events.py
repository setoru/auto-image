"""进程内事件存储：seq 递增、Last-Event-ID 断点重放、订阅者唤醒。

单进程单 worker 前提下的最简实现（dict + asyncio.Event 广播）。
"""
import asyncio
import contextlib
from collections import defaultdict


class EventStore:
    def __init__(self):
        self._events = defaultdict(list)
        self._subscribers = defaultdict(set)

    def create(self, run_id):
        self._events[run_id] = []
        self._subscribers[run_id] = set()

    def append(self, run_id, etype, payload):
        """追加一条内部事件，返回带递增 seq 的完整事件。"""
        event = {
            "seq": len(self._events[run_id]) + 1,
            "run_id": run_id,
            "type": etype,
            "payload": payload,
        }
        self._events[run_id].append(event)
        for flag in self._subscribers[run_id]:
            flag.set()
        return event

    def replay_from(self, run_id, after_seq):
        """返回 seq 严格大于 after_seq 的全部事件（断点重放用）。"""
        return [ev for ev in self._events[run_id] if ev["seq"] > after_seq]

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
