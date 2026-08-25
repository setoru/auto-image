# web — 部署会话 Web 服务端

浏览器会话式入口：新建空会话 → 输入部署指令 → SSE 实时看 agent 事件流。
会话为 `ClaudeSDKClient` 真实现（`web/sdk.py` 经工厂注入）；测试注入脚本化
假实现（`web/fake.py`），不触网、不启动真 SDK。

## 运行（本机直跑，单进程单 worker）

```bash
# Ubuntu 24.04 起 pip 受 PEP 668 管控，二选一：
pip install --break-system-packages -r web/requirements.txt   # 装进系统（本机现状）
python3 -m venv .venv && . .venv/bin/activate \
  && pip install -r web/requirements.txt                      # 或 venv 隔离
python -m web            # 默认 127.0.0.1:8123
WEB_PORT=8765 python -m web             # 换端口
WEB_HOST=0.0.0.0 WEB_PORT=8123 python -m web   # 外部可访问（见下）
```

默认只监听 127.0.0.1（无认证服务，能访问即能触发真实云操作）。
需要外部机器的浏览器访问时，`WEB_HOST=0.0.0.0` 绑定全部网卡，
经 `http://<本机IP>:<端口>/` 访问——暴露面由运行者的网络策略
（安全组/防火墙）控制，风险自担。

前端两种打开方式：

- 生产形态：`cd web-ui && npm run build` 后直接访问 `http://127.0.0.1:8123/`（FastAPI 挂载 `web-ui/dist`）；
- 开发形态：`cd web-ui && npm run dev` 后访问 `http://127.0.0.1:5173/`（`/api` 由 Vite 代理到 FastAPI 的 8123 端口）。

## 测试（主缝：HTTP 进、SSE 出）

```bash
python web/tests/test_api.py        # ASGI 主缝（假会话驱动）
python web/tests/test_artifacts.py  # 产物端点（临时目录造桩）
python web/tests/test_history.py    # 列表摘要、只读约束、假 transcript 驱动的重启重建
python web/tests/test_normalize.py  # 消息映射与阶段推导纯函数断言
python web/tests/test_redact.py     # 事件出口脱敏（形状正则 + 已知值清单）
python web/tests/test_sdk.py        # options 契约（系统提示词、固定上限、无值守写权限）
```

## 模块

| 文件 | 职责 |
| --- | --- |
| `app.py` | FastAPI 应用工厂、API 路由（含 `GET /api/runs` 列表）、SSE 流（id=seq、Last-Event-ID 重放、心跳保活）、启动接线（历史重建 + 残留 CLI 告警） |
| `runs.py` | 会话状态机（RUNNING 唯一、挂起并存）、干预（intervene）与 409 判定；ENDED 为重启找回的历史终态 |
| `events.py` | 进程内事件存储：seq 递增、断点重放、订阅唤醒 |
| `session.py` | 会话驱动循环（一条 run = 一条会话） |
| `normalize.py` | SDK 消息 → 内部事件映射、阶段推导 |
| `artifacts.py` | deploy/ 全量产物浏览（目录分组 + 最新落盘排序，约定文件带阶段徽标）、内容读取与路径约束 |
| `redact.py` | 事件出口脱敏（运行时已知值清单 + AK/SK、密码字段、私钥块形状正则） |
| `rebuild.py` | 服务重启后的历史重建：list_sessions / get_session_messages 以 session 粒度找回历史 run（ENDED，只读可续接） |
| `sdk.py` | ClaudeSDKClient 生产实现：options 全配、消息形状适配、工厂、历史读取包装 |
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
   成功。内置工具中只读 Bash 随 `claude_code` 预设放行；写路径需
   `permission_mode`（见第 8 条）。
7. **回合上限**：`max_turns=200` 与回合级 wall-clock 超时（默认 3600 秒，
   `sdk.TURN_TIMEOUT_SECONDS`）都是终局语义：Result 的错误 subtype（兜底
   判定：只有 `success` 是正常完成）或超时都以 `run.failed` 收尾、会话进
   FAILED、断开 SDK 连接，不自动重试。

8. **无值守会话的写权限**（真部署实测）：`claude_code` 工具预设只放行
   只读 Bash，Write 与 Bash 写路径一律被权限系统拦截（guide 只能把指南
   全文以文本返回）——options 必须配 `permission_mode="bypassPermissions"`。
   信任边界由运行形态承担：只监听 127.0.0.1 + 系统提示词任务边界。
9. **凭据值的两层脱敏**（真部署实测）：形状正则防不住自然语言内联
   （`` password `pcb…@@` ``、`password is set (…)` 出现在 thinking），
   redact 层除形状外还维护运行时已知值清单（启动时从 scope.yaml 登记
   ak/sk/password 值，任意上下文整值遮蔽）；实测华为云 SK 为 38 位大小写
   混合，非注释曾以为的 40 位小写。
10. **子 agent 的异步派发失稳**（真部署实测）：CLI 的 Agent 工具支持异步
   启动，模型可能派发后结束回合「等通知」——通知无处投递、run 挂在
   WAITING_INPUT；更早一轮还出现过并发派发上百次 guide 的调度风暴（20 实例
   触发 429，agent 自行终止后恢复）。系统提示词以执行纪律约束：至多一个
   子 agent 在跑、派发后 TaskOutput 阻塞等待、四阶段完成才收尾回合。
11. **interrupt 的终止边界**（干预语义实测，脚本经 `web.sdk` 工厂走生产路径）：
   回合执行中的本地 Bash 子进程**随打断被终止**（实测 `sleep 222` 在
   interrupt 后即刻消失），CLI 子进程保留、连接可续聊。已提交的云操作
   （HTTP API 类：创建 ECS、制镜像等）不受任何影响——打断只作用于后续
   动作，界面在 `turn.stopped` 块与停止按钮上如实提示「已提交的云操作
   不受停止影响，无法撤销」。
12. **cancel（断连）的终止边界**：回合执行中直接断开 SDK 连接（= run_task
   取消后 `__aexit__` 的路径）后 3 秒内：CLI 子进程**全部退出、无残留**
   （配合服务重启语义中的 pgrep 告警兜底），正在执行的本地 Bash 子进程
   同样被终止（实测 `sleep 333` 消失）。远程命令经 ssh 转发：客户端进程
   被杀断开连接，远端进程是否终止取决于远端 shell 配置，**不保证**——
   按「已提交的云操作不可撤销」对待。断连后以 `resume=session_id` 新建
   会话实测可续接，上下文完整（能复述被打断前的指令）。
13. **重启重建的 transcript 形状**（历史列表实测，本机 119 条真实会话、
   全量重建约 2 秒）：`get_session_messages` 只回可见的 user/assistant 链
   （isMeta / isSidechain 已滤），user 行 content 可为字符串（含 CLI 命令
   包装）或块列表（tool_result 回填），无 Result 消息——回合边界由「下一
   条真实用户输入」推导、回合汇总取该回合最后一条 agent 文本；重建的
   run 状态 ENDED（终态，可回看可续接），流以 `run.ended` 收尾后正常
   关闭。`list_sessions(directory=项目根)` 的 first_prompt 即任务名来源。

- CLI stderr 对本环境网关模型名报 `[claude-code:unrecognized_model]`
  警告，不影响会话执行，服务日志如实记录。
- `include_partial_messages=True` 带来大量 partial/system 消息
  （`thinking_tokens` 估算等），`to_dict` 将其适配为零形状、不进事件流。
