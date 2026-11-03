---
name: ims-skill
version: 0.4.0
description: "华为云 IMS 私有系统盘镜像操作。用户要求从 ECS 制镜像、跨区域复制镜像、按账号 ID 共享镜像或查询镜像/异步任务时使用。官方 SDK，纯 JSON 输出，非交互，支持 --dry-run，提交后先保存 job_id 再轮询。需要仓库根 scope.yaml 的 ak/sk/region 或 HUAWEICLOUD_SDK_* 环境变量。"
allowed-tools: Bash, Read
keywords: 华为云, ims, 镜像, system disk image, create, copy-cross-region, share, show, job
---

# IMS Skill —— 制镜像、跨区复制、账号共享与查询

所有操作使用 `scripts/ims.py`。脚本分为入口 / 编排、`ims_client.py`（凭证与区域客户端）、`ims_ops.py`（SDK 请求构造），每次写操作独立保存 JSON 日志到 `logs/`。

## 调用选择

| 目标 | 子命令 | 必要输入 | 成功结果 |
| --- | --- | --- | --- |
| 从 ECS 制系统盘镜像 | `create` | ECS 实例 ID | 源区域镜像 ID |
| 跨区域复制私有系统盘镜像 | `copy-cross-region` | 源镜像 ID、目的区域、IMS 委托 | 目的区域的新镜像 ID |
| 共享私有镜像给同区域账号 | `share` | 镜像 ID、接收账号 ID | 共享任务成功，镜像 ID 保持原值 |
| 查镜像 / 任务 | `show` | 镜像 ID、名称或 job ID，三选一 | 镜像详情或任务当前状态 |

根据用户的明确目标选择起点。已有镜像可以直接复制或共享；完整分发由调用方串联 `create → copy-cross-region → share`。每个目的区域复制成功后，用该区域的新镜像 ID 发起共享，保存各步身份和结果。恢复时先查询已记录的 job ID，复用已完成动作的结果，再处理未完成的步骤。

复制自动读取源区域镜像名称并原样沿用。`--source-image-name` 只为不触网的 dry-run 提供源名称：先用现有 show 查得名称，再传入预览；实际复制始终重新读取源名。

本 skill 的制镜像范围是 ECS 系统盘镜像。实例 ID 由调用方提供；停机、部署流程及实例操作沿用对应能力，镜像产物保留。

## 快捷命令

命令使用仓库相对路径；全局 `--scope <路径>` 放在子命令之前。

```bash
# 从 ECS 制镜像；省略名称时自动生成 img-<8hex>
python .claude/skills/ims-skill/scripts/ims.py create --instance-id <ecs-id>

# 打印将提交的创建请求
python .claude/skills/ims-skill/scripts/ims.py create --instance-id <ecs-id> --dry-run

# 指定名称、描述或等待预算
python .claude/skills/ims-skill/scripts/ims.py create --instance-id <ecs-id> \
  --image-name web-img --description "daily backup" --timeout 3600 --poll-interval 15

# 源区域由 scope / 环境变量提供；--region 是目的区域
python .claude/skills/ims-skill/scripts/ims.py copy-cross-region \
  --id <source-image-id> --region cn-north-4 --agency-name ims_copy_image \
  --source-image-name <source-image-name> --dry-run

# 执行复制；委托名可从 scope.ims_copy.agency_name 读取
python .claude/skills/ims-skill/scripts/ims.py copy-cross-region \
  --id <source-image-id> --region cn-north-4

# 在镜像所在区域按账号 ID 共享；支持 --dry-run 查看请求
python .claude/skills/ims-skill/scripts/ims.py share \
  --id <destination-image-id> --region cn-north-4 --account-id <receiver-account-id>

# 查询镜像，--region 缺省沿用配置
python .claude/skills/ims-skill/scripts/ims.py show --id <image-id> --region cn-north-4
python .claude/skills/ims-skill/scripts/ims.py show --name web-img

# 复制任务在源区域查询；共享任务在发起共享的区域查询
python .claude/skills/ims-skill/scripts/ims.py show --job-id <copy-job-id> --region <source-region>
python .claude/skills/ims-skill/scripts/ims.py show --job-id <share-job-id> --region cn-north-4
```

## 配置与区域

唯一凭证源为仓库根 `scope.yaml`，范本为 `scope.yaml.example`。配置优先级：

| 维度 | 优先级 |
| --- | --- |
| AK / SK | `HUAWEICLOUD_SDK_AK` / `_SK` → scope.ak/sk |
| 默认区域 | `HUAWEICLOUD_SDK_REGION` → scope.region |
| 项目 ID | `HUAWEICLOUD_SDK_PROJECT_ID` → scope.project_id；省略由 SDK 按区域解析 |
| 创建镜像名称 | CLI `--image-name`；省略时自动生成 |
| 创建 / 复制描述 | CLI；省略时不下发 |
| 创建时企业项目 ID | scope.ims_create.enterprise_project_id；省略不下发 |
| 复制委托名 | CLI `--agency-name` → scope.ims_copy.agency_name；必须提供一处 |
| 复制目的项目名 | CLI `--project-name` → 目的区域 ID（默认 IAM 项目） |

```yaml
ims_copy:
  agency_name: ims_copy_image
```

`copy-cross-region --region` 仅指定目的区域，提交与任务查询均使用源区域客户端。`share/show --region` 指定本次操作 / 查询区域，CLI 优先于默认区域。区域覆盖改变区域时只复用 AK/SK，不沿用来源配置的 project_id；SDK 解析目标区域默认 IAM 项目。目的为子项目时，复制用 `--project-name`，目的镜像查询 / 共享使用该区域的独立 scope 或现有环境变量指定其 project_id，确保凭证配置区域与目标区域一致。

## 输出与完成条件

stdout 输出 JSON，进度与提示走 stderr。成功退出码 0，失败 / 未完成退出码 1，参数错误沿用 argparse，进程中断退出码 130。

任务状态为 `INIT/RUNNING/SUCCESS/FAIL`。制镜像与复制在 SUCCESS 后返回镜像 ID 即就绪；共享只需任务 SUCCESS，不产生新镜像 ID。镜像详情按需通过 show 查询。

输出示例（实际还含日志路径）：

```json
{"ok": true, "action": "create", "image_name": "img-a1b2c3d4", "image_id": "...",
 "instance_id": "...", "status": "SUCCESS", "job_id": "...", "region": "..."}
{"ok": true, "action": "copy-cross-region", "source_region": "...", "source_image_id": "...",
 "destination_region": "cn-north-4", "destination_image_id": "...", "image_name": "...",
 "status": "SUCCESS", "job_id": "..."}
{"ok": true, "action": "share", "region": "cn-north-4", "image_id": "...",
 "account_id": "...", "status": "SUCCESS", "job_id": "..."}
```

`show --id` 返回 `image` 详情，`show --name` 返回 `images` 列表。按 job 查询成功时，制镜像任务返回完整 `image`，跨区复制任务返回 `destination_image_id`，共享任务返回成功状态；后两者同时返回 `job_type`。尚未完成的 job 查询返回进度与复查命令；失败返回官方 `error_code/fail_reason`。

create / share 默认等待 1800 秒，每 10 秒查询；复制默认等待 3600 秒，每 30 秒查询。跨区传输可能持续数小时，命令等待结束不影响已提交的云任务。调用方工具的运行预算需覆盖 CLI 等待时间及一个轮询间隔；中断后按日志 / stderr 中的 job ID 和提交区域复查。

复制 / 共享超时或查询异常时返回已知 job ID、最近一次云任务状态和复查命令；提交后尚无查询结果时用本地 `SUBMITTED` 状态表示。日志先保存任务身份再轮询；输出和日志仅包含业务请求及结果，不记录 AK/SK。

## 前置与操作契约

- 写操作非交互，按明确指令执行；`--dry-run` 只打印请求，整个路径不调用 API。IMS 没有与 ECS 相同的服务端 dry_run 预检参数。
- 制镜像接受已有 ECS 的实例 ID，固定系统盘类型；ECS 一致性处理由调用方根据实际要求决定。
- 跨区复制使用现成的 IMS 云服务委托，按官方指南配置 `IMS CrossCopyAgencyPolicy` 并授权目的 IAM 项目。`IMG.0151` 时检查源 / 目的区域 IAM 项目权限，`IMG.0149` 时等待已有并发任务完成。
- 每次跨区复制按 OBS 跨区域复制功能收取流量费，dry-run 与提交前均提示费用。
- 账号共享使用 `domains`，同区域、仅部分区域支持；成功后接收方无需接受即可使用。接收账号 ID 由用户提供。
- 镜像属性、名称、描述、大小、加密、区域支持、成员配额及并发限制由 IMS API 校验；保留官方错误信息供处理。恢复时查询已提交任务，不重复提交进行中的操作。

## 依赖与验证

系统 Python 3.12、PyYAML、`huaweicloudsdkcore==3.1.208`、`huaweicloudsdkims==3.1.208`。使用官方 CreateImage、CopyImageCrossRegion、BatchAddMembers、ShowJobProgress、ListImages 模型和客户端方法。

新机器安装：`python3 -m pip install --break-system-packages huaweicloudsdkcore==3.1.208 huaweicloudsdkims==3.1.208 pyyaml`。

行为测试（请求 / CLI dry-run / 假客户端 / 区域凭证）：`python .claude/skills/ims-skill/tests/test_ims_ops.py`。
