---
name: ims-skill
version: 0.3.0
description: "CRITICAL: 华为云 IMS 制系统盘镜像。把一台已有 ECS 制作为 IMS 系统盘镜像（create_image → job SUCCESS），纯 JSON 输出。支持按镜像 ID / 名称 / job_id 查询。非交互，--dry-run 当确认杠杆，job_id 落盘先于轮询（中断可用 show --job-id 恢复），用完保留不自动删除。当用户要求『从这台 ecs 制镜像』『把服务器做成镜像』『查镜像状态』『查制镜像任务』时使用。Triggers: 制镜像, 做镜像, 创建镜像, ims, ims-skill, image, 镜像, system disk image, create image, show image, 查镜像, job, 系统盘镜像。需要：仓库根 scope.yaml 已配置（ak/sk/region）或 HUAWEICLOUD_SDK_* 环境变量。"
allowed-tools: Bash, Read
keywords: 华为云, ims, 镜像, 制镜像, image, system disk, 系统盘, create, show, job, huawei
---

# IMS Skill —— 华为云 IMS 制系统盘镜像 + 查询

把一台已有 ECS **制作为系统盘镜像**：`create`（create_image → 轮询 job SUCCESS → 返回 image_id）+ `show`（按 ID / 名称 / job_id 查询）。纯 JSON 输出，`logs/` 归档。基于官方 `huaweicloudsdkims` SDK；脚本拆为 `scripts/ims.py`（入口/CLI/编排）+ `ims_client.py`（客户端+凭证）+ `ims_ops.py`（请求构造）；单测在 `tests/`，覆盖请求构造、`--dry-run` 端到端、poll_job 轮询、cmd_show 三条查询路径。

**就绪 = job SUCCESS**：`show_job` 返回 `SUCCESS` 时 `entities.image_id` 即出现——此刻镜像已可使用。不额外轮询 image status。

**职责边界**：只做**系统盘镜像**（ECS 类型），**不**做整机镜像（WholeImage，需 CBR vault_id）、**不**按名称查找源 ECS（接受 `--instance-id`）、**不**自动停机、**不**做 deploy 编排、**不**提供 `delete`。

> 路径：本 skill 当前在仓库内开发，命令用仓库相对路径。迁到 `~/.claude/skills/` 后，把下列命令前缀换成 `~/.claude/skills/ims-skill/`。

## 快捷命令

```bash
# 制镜像（scope 默认配置；名字自动 img-<rand>；轮询到 job SUCCESS 返回 image_id）
python .claude/skills/ims-skill/scripts/ims.py create --instance-id <ecs-id>

# 看看「将提交什么请求」，不调 API（确认杠杆）
python .claude/skills/ims-skill/scripts/ims.py create --instance-id <ecs-id> --dry-run

# 指定镜像名 / 描述
python .claude/skills/ims-skill/scripts/ims.py create --instance-id <ecs-id> --image-name web-img --description "daily backup"

# 控制轮询超时 / 频率（大盘放宽超时）
python .claude/skills/ims-skill/scripts/ims.py create --instance-id <ecs-id> --timeout 3600 --poll-interval 15

# 查镜像（按 ID）
python .claude/skills/ims-skill/scripts/ims.py show --id <image-id>

# 查镜像（按名称）
python .claude/skills/ims-skill/scripts/ims.py show --name web-img

# 查制镜像任务（create 超时后恢复）
python .claude/skills/ims-skill/scripts/ims.py show --job-id <job-id>
```

## 配置（仓库根共享 scope.yaml + 环境变量）

凭证在仓库根 `scope.yaml`（已 gitignore；范本见仓库根 `scope.yaml.example`）。该文件为 ecs-skill 与 ims-skill 的**唯一凭证源**。优先级（高 → 低）：

| 维度 | 优先级 |
|------|--------|
| AK/SK | `HUAWEICLOUD_SDK_AK`/`_SK` 环境变量 → scope.ak/sk |
| region | `HUAWEICLOUD_SDK_REGION` env → scope.region |
| project_id | `HUAWEICLOUD_SDK_PROJECT_ID` env → scope.project_id（可省略，SDK 按 region 自动推导） |
| 镜像名 | CLI `--image-name` → 自动生成 `img-<8hex>` |
| 描述 | CLI `--description` → 不下发 |
| 企业项目 ID | scope `ims_create.enterprise_project_id`（可省略，省略则不下发） |

`--scope <路径>` 可指定其它 scope 文件。ims-skill 只读顶层 `ak`/`sk`/`region`/`project_id` + `ims_create.enterprise_project_id`；`ecs_create` 段对 ims-skill 无影响。

## 输出（纯 JSON，退出码区分成败）

```json
// create 成功
{"ok": true, "action": "create", "image_name": "img-a1b2c3d4", "image_id": "...",
 "instance_id": "...", "status": "SUCCESS", "job_id": "...", "region": "...",
 "log": ".claude/skills/ims-skill/logs/img-a1b2c3d4-<时间>.json"}
// create 超时（job 仍在 RUNNING——用 show --job-id 复查）
{"ok": false, ..., "status": "RUNNING", "error": "轮询超时（1800s）：job 仍在运行",
 "hint": "用 `ims.py show --job-id <job_id>` 复查。", "log": "..."}
// create 失败（job FAIL）
{"ok": false, ..., "status": "FAIL", "error": "job 失败：...", "hint": "...", "log": "..."}
// show（--id 成功）
{"ok": true, "action": "show",
 "image": {"id","name","status","size","os_type","disk_format","min_disk"}}
// show（--name 成功，可能多个匹配）
{"ok": true, "action": "show", "images": [{...}, ...]}
// show（--job-id SUCCESS → 返回完整镜像详情）
{"ok": true, "action": "show", "job_id": "...", "status": "SUCCESS",
 "image": {"id","name","status","size","os_type","disk_format","min_disk"}}
// show（--job-id RUNNING → 返回进度 + 提示）
{"ok": false, "action": "show", "job_id": "...", "status": "RUNNING",
 "process_percent": 42.0,
 "hint": "任务仍在运行，请稍后用 `ims.py show --job-id <job_id>` 复查。"}
// show（--job-id FAIL → 返回失败原因）
{"ok": false, "action": "show", "job_id": "...", "status": "FAIL",
 "fail_reason": "...", "error_code": "IMG.0001"}
```

镜像不涉及登录凭证，输出无密码/鉴权字段。

## 约束（强制）

- **非交互**：`create` 直接执行不 prompt。先看「将提交什么」→ `--dry-run`（不调 API）。真正的 go/no-go 归人或编排层。
- **不做 --validate**：IMS `create_image` API 无 `dry_run=true` 服务端预检参数（ECS 有而 IMS 没有），无法做等价的 `--validate`。`--dry-run` 是唯一确认杠杆。
- **就绪 = job SUCCESS**：`show_job` 返回 `SUCCESS` 时 `entities.image_id` 即出现——此刻镜像已可使用。不额外轮询 image status（`active` 等），因为 job 状态就是最终状态。
- **调用方超时要够长**：镜像创建耗时取决于源 ECS 系统盘大小——40GB 约 2-5 分钟，100GB 约 5-15 分钟。默认 `--timeout 1800`（30 分钟）、`--poll-interval 10`（秒）。用 Bash 工具调用时超时须设到 ≥ `--timeout` + 少量裕量，否则进程会被上层杀掉。真被杀也不丢 job_id：提交后立即落 `logs/` 并打到 stderr，用 `show --job-id` 复查。
- **stdout 纯 JSON**：进度/告警（如 `[ims] job=... status=RUNNING 42%`）只走 stderr，解析 stdout 不受影响。
- **用完保留**：无论成败都**不自动删除**镜像。失败时输出当前状态，用 `show --job-id` 复查或人工处置。
- **仅系统盘镜像**：type 固定为 `ECS`（系统盘镜像），不暴露给 CLI。整机镜像（WholeImage，需 CBR vault_id）不在此版范围。
- **依赖**：系统级已装 `huaweicloudsdkcore` + `huaweicloudsdkims`（与 ecs-skill 的 `huaweicloudsdkecs` 同版本、同安装方式）。新机器需 `python3 -m pip install --break-system-packages huaweicloudsdkcore==3.1.208 huaweicloudsdkims==3.1.208`。
- **前置**：源 ECS `--instance-id` 必填（不做名称→ID 查找，用 ecs-skill `show --name` 拿 id）；华为允许运行中制镜像（不自动停机，数据一致性由使用者负责）。

## 依赖

- 系统 python3（3.12）+ `huaweicloudsdkcore` 3.1.208 + `huaweicloudsdkims` 3.1.208 + `pyyaml`。
- 用官方 IMS 服务 SDK（`ImsClient`/`CreateImageRequest`/`ShowJobRequest`/`ListImagesRequest`），不手搓签名 HTTP。
- 单测（纯逻辑的请求构造 + 不触网的 `--dry-run` 端到端 + mock client 的轮询与查询）：`python .claude/skills/ims-skill/tests/test_ims_ops.py`（迁移后路径同上换前缀）。
