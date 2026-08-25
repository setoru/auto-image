"""测试基建：支持真流式的 ASGI 传输。

httpx 自带的 ASGITransport 会把整个响应体收完才返回，SSE 这种
挂起会话上的无限流会挂死；这里换成边推边读——响应体块进 asyncio.Queue，
响应对象以异步迭代器消费，aclose 时取消应用协程（等效客户端断开）。
"""
import asyncio

import httpx


def async_client(app):
    """app → 挂真流式 ASGI 传输的 AsyncClient（各主缝测试共用的装配）。"""
    return httpx.AsyncClient(transport=StreamingASGITransport(app=app), base_url="http://testserver")


class StreamingASGITransport(httpx.AsyncBaseTransport):
    def __init__(self, app):
        self.app = app

    async def handle_async_request(self, request):
        body = await request.aread()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "method": request.method,
            "path": request.url.path,
            "raw_path": request.url.raw_path,
            "query_string": request.url.query,
            "headers": [(k.lower(), v) for k, v in request.headers.raw],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "scheme": "http",
            "http_version": "1.1",
            "app": self.app,
        }
        queue: asyncio.Queue = asyncio.Queue()
        started = asyncio.Event()
        disconnected = asyncio.Event()
        state = {"status": 500, "headers": []}
        body_sent = False

        async def receive():
            # 请求体只给一次；此后必须挂起等待，直到客户端断开
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["headers"] = message.get("headers", [])
                started.set()
            elif message["type"] == "http.response.body":
                if message.get("body"):
                    await queue.put(message["body"])
                if not message.get("more_body", False):
                    await queue.put(None)

        async def run_app():
            try:
                await self.app(scope, receive, send)
            finally:
                await queue.put(None)

        app_task = asyncio.create_task(run_app())
        await started.wait()

        async def body_iter():
            try:
                while True:
                    chunk = await queue.get()
                    if chunk is None:
                        break
                    yield chunk
            finally:
                # 客户端不再消费：等效断开连接，让应用侧停止推流
                disconnected.set()
                if not app_task.done():
                    app_task.cancel()

        headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in state["headers"]]
        return httpx.Response(state["status"], headers=headers, content=body_iter(), request=request)
