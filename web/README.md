# web — 部署会话 Web 服务端

浏览器会话式入口：新建空会话 → 输入部署指令 → SSE 实时看 agent 事件流。
会话为 `ClaudeSDKClient` 真实现（`web/sdk.py` 经工厂注入）；测试注入脚本化
假实现（`web/fake.py`），不触网、不启动真 SDK。

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
python web/tests/test_api.py        # ASGI 主缝（假会话驱动）
python web/tests/test_normalize.py  # 消息映射与阶段推导纯函数断言
```

## 模块

| 文件 | 职责 |
| --- | --- |
| `app.py` | FastAPI 应用工厂、API 路由、SSE 流（id=seq、Last-Event-ID 重放、心跳保活） |
| `runs.py` | 会话状态机（RUNNING 唯一、挂起并存）、干预（intervene）与 409 判定 |
| `events.py` | 进程内事件存储：seq 递增、断点重放、订阅唤醒 |
| `session.py` | 会话驱动循环（一条 run = 一条会话） |
| `normalize.py` | SDK 消息 → 内部事件映射、阶段推导 |
| `redact.py` | 事件出口脱敏（AK/SK、密码字段、私钥块） |
| `sdk.py` | ClaudeSDKClient 生产实现：options 全配、消息形状适配、工厂 |
| `fake.py` | 脚本化假会话（默认剧本含敏感样例），测试注入用 |

## SDK 真会话实测记录（claude-agent-sdk 0.2.144 + CLI 2.1.220）

接入真实现时逐项实测的结论，均为实际运行观察、非文档推断：

1. **消息形状**：`receive_response` 产出 dataclass（`AssistantMessage` 等），
   `sdk.to_dict` 适配成 CLI JSON 形状 dict 后进 `normalize_message`，
   与假剧本同一条映射路径。每回合终止于 `ResultMessage`，下一轮 `query`
   在同一连接续聊，与 `session.run_agent` 的循环结构一致。
2. **子 agent thinking 转发**（方案风险点一）：`forward_subagent_text=True`
   下子 agent 的 thinking 块**会**随文本一并转发（parent_tool_use_id 非空的
   assistant 消息里实测出现 ThinkingBlock），子 agent 思维链在前端可见。
3. **子 agent 工具名**：CLI 现名 `Agent`（system init 的工具注册表里仍可见
   旧名 `Task`）。阶段推导两者都接受（`normalize.SUBAGENT_TOOL_NAMES`），
   真实 `Agent` 调用带四类 subagent_type 时已实测发出 `stage.changed`。
4. **tools 必须显式给 `claude_code` 预设**：SDK 不配 `tools` 时 CLI 基础
   工具集不含子 agent 工具（agent 自查工具目录无 Task/Agent），四阶段
   流水线无从推进——`--tools default` 后才有。
5. **interrupt 行为**：回合执行中 `interrupt()` 后消息流**自然终止**
   （`receive_response` 迭代器结束），尾随一条 `subtype="error_during_execution"`、
   `is_error=True`、`result=None` 的 Result（后接的 UserMessage 为被中断
   工具的错误 tool_result，照常走 tool_finished 映射）；同一会话随后
   `query` 续聊正常，打断前的上下文保留。服务端以自己的 `stop_requested`
   标记区分 `turn.stopped` 与 `turn.completed`，不解析该 Result 的文案。
6. **联网链路**（方案风险点三）：SDK 会话内内置 `WebFetch` 被域名安全校验
   拦截（"Unable to verify if domain ... is safe to fetch"）、`WebSearch`
   在权限层被拒——与仓库 CLAUDE.md 记录一致。已按方案经 options 的
   `mcp_servers` 接入既有 exa MCP，并因非交互会话无人批准 MCP 工具而
   配 `allowed_tools=["mcp__exa-search__*"]` 放行；复测抓取 nginx.org
   成功。内置工具中 Bash 等随 `claude_code` 预设默认放行，无需额外配置。
7. **回合上限**：`max_turns=200` 已配；回合级 wall-clock 超时属终局语义
   （`run.failed` 路径），尚未接入（干预语义已就绪，超时随需要补）。

8. **interrupt 的终止边界**（干预语义实测，脚本经 `web.sdk` 工厂走生产路径）：
   回合执行中的本地 Bash 子进程**随打断被终止**（实测 `sleep 222` 在
   interrupt 后即刻消失），CLI 子进程保留、连接可续聊。已提交的云操作
   （HTTP API 类：创建 ECS、制镜像等）不受任何影响——打断只作用于后续
   动作，界面在 `turn.stopped` 块与停止按钮上如实提示「已提交的云操作
   不受停止影响，无法撤销」。
9. **cancel（断连）的终止边界**：回合执行中直接断开 SDK 连接（= run_task
   取消后 `__aexit__` 的路径）后 3 秒内：CLI 子进程**全部退出、无残留**
   （配合服务重启语义中的 pgrep 告警兜底），正在执行的本地 Bash 子进程
   同样被终止（实测 `sleep 333` 消失）。远程命令经 ssh 转发：客户端进程
   被杀断开连接，远端进程是否终止取决于远端 shell 配置，**不保证**——
   按「已提交的云操作不可撤销」对待。断连后以 `resume=session_id` 新建
   会话实测可续接，上下文完整（能复述被打断前的指令）。

- CLI stderr 对本环境网关模型名报 `[claude-code:unrecognized_model]`
  警告，不影响会话执行，服务日志如实记录。
- `include_partial_messages=True` 带来大量 partial/system 消息
  （`thinking_tokens` 估算等），`to_dict` 将其适配为零形状、不进事件流。
