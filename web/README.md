# web — 部署会话 Web 服务端

浏览器会话式入口：新建空会话 → 输入部署指令 → SSE 实时看 agent 事件流。
本阶段会话为脚本化假实现（`web/fake.py`），真 SDK 会话随后接入。

## 运行（本机直跑，单进程单 worker，只监听 127.0.0.1）

```bash
pip install -r web/requirements.txt
python -m web            # 默认 8000，WEB_PORT=8765 可覆盖
```

前端两种打开方式：

- 生产形态：`cd web-ui && npm run build` 后直接访问 `http://127.0.0.1:8000/`（FastAPI 挂载 `web-ui/dist`）；
- 开发形态：`cd web-ui && npm run dev` 后访问 `http://127.0.0.1:5173/`（`/api` 由 Vite 代理到 FastAPI 的 8000 端口）。

## 测试（主缝：HTTP 进、SSE 出）

```bash
pip install httpx        # 仅测试需要
python web/tests/test_api.py
```

会话经工厂注入假实现，不触网、不启动真 SDK。

## 模块

| 文件 | 职责 |
| --- | --- |
| `app.py` | FastAPI 应用工厂、API 路由、SSE 流（id=seq、Last-Event-ID 重放、心跳保活） |
| `runs.py` | 会话状态机（RUNNING 唯一、挂起并存）与 409 判定 |
| `events.py` | 进程内事件存储：seq 递增、断点重放、订阅唤醒 |
| `session.py` | 会话驱动循环（一条 run = 一条会话） |
| `normalize.py` | SDK 消息 → 内部事件映射、阶段推导 |
| `redact.py` | 事件出口脱敏（AK/SK、密码字段、私钥块） |
| `fake.py` | 脚本化假会话（默认剧本含敏感样例） |
