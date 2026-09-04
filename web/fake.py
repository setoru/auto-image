"""脚本化假会话：按剧本推 SDK 形状的消息，测试注入用，不触真 SDK。

剧本是消息列表（形状见 normalize_message）；条目为异常实例时在该点抛出，
用于演练会话异常终止。剧本含敏感样例，验证事件出口的脱敏层。

打断行为对齐真 SDK 实测形态（web/README.md 实测记录第 5 条）：回合执行中
interrupt 后流终止，尾随一条 subtype=error_during_execution、result 为空
的 Result；新回合 query 时打断状态清零。Result 消息的 session_id 由假会话
注入（工厂按创建次序分配），供 resume_from 的传参断言使用。
"""
import asyncio
import itertools

# 华为云 AK/SK 与密码样例：任何一条漏遮都应被测试捕获
DEFAULT_SCRIPT = [
    {
        "type": "assistant",
        "message": {"content": [
            {"type": "thinking", "thinking": "用户要部署 nginx。scope 里的 AK HWPFEJ9AB3CDEFGHIJKL 与 SK f3a9c81d0b7e46f2a5d8c3b1e9470ad6c2f5b831 不能出现在输出。"},
        ]},
    },
    {
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "收到，开始执行部署流水线。"},
        ]},
    },
    {
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": "toolu_01", "name": "Task", "input": {"subagent_type": "deploy-guide", "prompt": "生成 nginx 1.25 的部署与验证指南"}},
        ]},
    },
    {
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_01", "content": "指南已生成：deploy/nginx/1.25/install.md，机器 password: Xk9$mPq2LwzR 已配置。"},
        ]},
    },
    {
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "指南阶段完成，机器 root password: Xk9$mPq2LwzR 已就绪，等待安装指令。"},
        ]},
    },
    {
        "type": "result",
        "subtype": "success",
        "result": "回合完成：GUIDE 阶段产物已落盘。secret=topsecret-token 已由脱敏层遮蔽。",
    },
]


class FakeSession:
    """与会话抽象同形的假实现：query 记录文本，receive_response 逐条产出剧本。"""

    def __init__(self, script=None, delay=0.0, session_id="sess_fake"):
        self.script = list(script if script is not None else DEFAULT_SCRIPT)
        self.delay = delay
        self.session_id = session_id
        self.queries = []
        self.interrupted = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def query(self, text):
        self.queries.append(text)
        # 打断状态随回合生效：新回合从清零开始
        self.interrupted = False

    async def interrupt(self):
        self.interrupted = True

    async def receive_response(self):
        for step in self.script:
            await asyncio.sleep(self.delay)
            if self.interrupted:
                yield {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": "",
                    "session_id": self.session_id,
                }
                return
            if isinstance(step, BaseException):
                raise step
            if step.get("type") == "result":
                step = {**step, "session_id": self.session_id}
            yield step


class FakeSessionFactory:
    """按创建次序给假会话分配 session_id，并记录工厂与 query 调用。"""

    def __init__(self, script=None, delay=0.0):
        self.script = script
        self.delay = delay
        self._ids = itertools.count(1)
        self.session_ids = []
        self.sessions = []

    @property
    def queries(self):
        """全部已创建会话收到的 query，按会话创建与调用顺序展平。"""
        return [query for session in self.sessions for query in session.queries]

    def __call__(self, session_id=None):
        self.session_ids.append(session_id)
        session = FakeSession(
            script=self.script,
            delay=self.delay,
            session_id=f"sess_fake_{next(self._ids)}",
        )
        self.sessions.append(session)
        return session
