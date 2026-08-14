---
name: deploy
version: 1.2.0
description: "CRITICAL: 端到端 Ubuntu 软件部署编排——把 deploy-guide → deploy-install → deploy-verify → deploy-archive 四个 agent 串成一条流水线：生成部署/验证指南 → 经 ssh-skill 在目标机安装 → 经 ssh-skill 验证 → 经 ssh-skill/ims-skill/ecs-skill 打包归档（清理 → 制镜像 → 切换 OS → 交付清单） → 汇总。当用户要求『一键部署』『端到端部署』『部署并验证』『串联部署』『完整部署某软件到某机器』『部署并打包』时使用。Triggers: 一键部署, 端到端部署, 部署并验证, 完整部署, 串联部署, 部署流水线, deploy pipeline, end-to-end deploy, /deploy, 全流程部署, 部署并打包, 部署并归档。需要同时给出：软件名 + 安装文档/链接；目标服务器别名可选（给了→已有机器安装；没给→install 自动拉起 ECS + 注册）。"
allowed-tools: Agent, Read, Write, Bash, Glob
keywords: 部署, deploy, 一键部署, 端到端, pipeline, 安装, 验证, 串联, 流水线, 全流程, ssh, 远程, 打包, archive, 归档, 制镜像, 交付清单, 创建ecs, 自动拉起
---

# Deploy 端到端部署流水线

把四个 agent 串成一条流水线，完成「出指南 → 远程安装 → 远程验证 → 打包归档」：

```
deploy-guide    生成 install.md + verify.md（只探索，不安装）
       ↓  （产物落盘，路径由 deploy.config.yaml 决定，自动对齐）
deploy-install  读 install.md → ssh-skill 远程安装 → install-result.md (+ issues) + meta.json
       ├─ 有 alias → 在已有机器上安装
       └─ 无 alias → ecs-skill create 拉起 + ssh-skill 注册 → 安装
                     └─ meta.json 记录 instance_id（供 archive 消费）
       ↓
deploy-verify   读 verify.md  → ssh-skill 远程验证 → verify-result.md (+ issues)
       ↓  （verify 通过后才进入 archive）
deploy-archive  清理 → 制镜像 → 切换 OS → archive-result.md + deploy-list.md (+ issues)
                └─ instance_id 从 meta.json 取；password 从 scope.yaml 取
```

四端输入/输出路径都由项目根目录的 **`deploy.config.yaml`（共用）** 决定，按 `{{software}}`/`{{version}}` 解析。**编排层只需把 software / version / server-alias 一致地传给四个 agent**，无需手工对齐路径。

## 强制铁律（不可妥协）

本 skill 是**纯编排层**：只做「解析公共入参 → 按序调用四个子 agent → 校验每步产物 → 汇总」，**不亲自做任何实际工作**。以下三条强制执行，破坏任一条即视为编排失败：

1. **必须依次执行全部 4 个步骤，不得合并、跳过或内联替做**：deploy-guide → deploy-install → deploy-verify → deploy-archive。
   - 唯一允许的提前终止：步骤 1（guide）失败 → 后续无输入，整条停止。
   - 步骤 2（install）失败**仍须执行**步骤 3（verify），以采证机器真实状态。
   - 步骤 3（verify）未通过**不执行**步骤 4（archive）——archive 的前提是软件已装好且验证通过，对未通过验证的机器打包无意义。

2. **4 个步骤各是一个独立的 Agent 子 agent**——编排层**禁止**亲自抓文档、**禁止**任何 `ssh`/ssh-skill 远程执行、**禁止**解析或执行安装/验证/清理命令、**禁止**亲自制镜像或切换 OS；这些只能在各自子 agent 内完成。
   - **为什么必须用子 agent**：**上下文隔离**。文档抓取与远程命令会产出大量上下文，关在各子 agent 内即可；编排层只回收每步的「结论 + 产物路径」，避免主上下文膨胀与互相串扰，也确保各 agent 自身约束（guide 禁装、verify 只读、archive 三步顺序执行）不被绕过。

3. **步骤之间以「输出件」硬约束**：每步都有明确产物文件（见对照表）。进入下一步**前**，编排层必须用 **Read**（或 Bash `[ -f <文件> ]`）确认上一步产物已落盘；产物缺失即视为该步失败。这正是「不可跳过」的 enforcement——没有 guide 的 `install.md`，install 无从读起。

### 步骤产物对照表（enforcement 锚点）

| 步骤 | 子 agent | 必须产出（落盘到 output_dir） | 进入下一步前的校验 |
|------|----------|-------------------------------|--------------------|
| 1 guide | deploy-guide | `<install.md>`、`<verify.md>` | Read 两文件；任一缺失 → 终止整条 |
| 2 install | deploy-install | `<install-result.md>`（有问题另出 `<install-issues.md>`）、`<install-meta.json>` | Read `install-result.md` + `<install-meta.json>`；result 缺失记失败，**仍继续步骤 3**。meta.json 缺失记警告（instance_id 取不到，可能影响步骤 4） |
| 3 verify | deploy-verify | `<verify-result.md>`（未通过项另出 `<verify-issues.md>`） | Read `verify-result.md`；缺失记失败。**verify 未通过 → 不执行步骤 4** |
| 4 archive | deploy-archive | `<archive-result.md>`、`<deploy-list.md>` | Read 两文件；任一缺失 → 记失败 |

## 输入（调用方在 prompt 中提供）

1. **软件名称**（必需）
2. **安装文档或链接**（必需）：URL / GitHub 仓库链接 / 本地文件 / 粘贴内容
3. **目标服务器别名**（**可选**；ssh-skill 已配置的别名）——**双路径触发器**：
   - 给了 alias → 已有机器路径（在指定机器上安装）
   - 没给 alias → 创建路径（install 自动拉起 ECS + 注册 ssh-skill 别名）
4. **软件版本**（可选；不提供则由 deploy-guide 推断，推断结果向后传递）
5. **分支/标签**（可选；GitHub 仓库时）
6. **目标 Ubuntu 版本**（可选；默认 auto）
7. **ECS instance_id**（**条件必需**；仅步骤 4 deploy-archive 使用）：
   - 创建路径（无 alias）→ install 产出 meta.json，编排层自动读取，**调用方无需提供**
   - 已有 alias 路径 → meta.json 的 instance_id 为 null，**仍需调用方提供**；未提供则 archive 跳过（见步骤 4）
8. **ECS 规格**（可选；仅创建路径使用）：如 flavor / image / disk-size / bandwidth 等，自由文本透传给 install → ecs.py

> **密码不需要调用方提供**。archive 步骤切换 OS 时自己从 scope.yaml 取密码（ecs.py change-os 的 `--password` 不给时自动兜底 scope 的 `ecs_create.server.password`）。密码不经过编排层传递。

## 执行步骤

**MUST**（见上方「强制铁律」）：四个步骤**每步都用 Agent 工具调用对应子 agent**，**阻塞完成后再调下一个**，禁止并行——install 依赖 guide 产物，verify 依赖 install 完成，archive 依赖 verify 通过。步骤 0、5 是编排层内部的解析与汇总，不计入这四步。

### 步骤 0 — 解析公共路径

- 用 **Read** 读 `deploy.config.yaml`，取 `output_dir` + `install_meta_file`，代入本次 `software`/`version` 得到产物目录（如 `deploy/nginx/1.25.3`）与 meta.json 路径，用于后续汇总与流水线报告路径。
- 用 **Bash** `date +%F` 取当日日期。
- 若调用方未给 version，先记为待定（以步骤 1 deploy-guide 解析结果为准）。

### 步骤 1 — deploy-guide（生成指南）

用 **Agent** 调用（subagent_type: `deploy-guide`），prompt 传入：软件名、文档/链接、版本（若有）、分支（若有）、Ubuntu 版本（若有）。

完成后（产物校验 = 强制铁律第 3 条）：
- **捕获 deploy-guide 实际使用的 `software` / `version`**（其回复中会告知）→ 作为步骤 2、3、4 的固定入参。
- **用 Read 校验 `<install.md>` 与 `<verify.md>` 已落盘**（路径 = output_dir + 配置文件名）。两者齐备方可进入步骤 2。
- **任一缺失 / guide 失败 → 停止整条流水线**，报告原因（后续阶段无输入，无法进行）。

### 步骤 2 — deploy-install（远程安装）

用 **Agent** 调用（subagent_type: `deploy-install`），prompt 传入：软件名、**步骤 1 解析的 version**、目标服务器别名（若有）、ECS 规格参数（若有；仅创建路径使用）。

agent 会自行按配置路径读 install 指南、经 ssh-skill 在目标机安装，产出 `install-result.md`（及 `install-issues.md`）+ `install-meta.json`。捕获其整体结论（✅ 成功 / ⚠️ 部分 / ❌ 失败）及**实际使用的 server alias**（创建路径下 alias 由 install 自动生成，其回复中会告知）。

完成后：
- **用 Read 校验 `<install-result.md>` 已落盘**；缺失则记为失败，但**不阻塞步骤 3**。
- **用 Read 读取 `<install-meta.json>`**，解析 `instance_id`、`server_alias`、`path`、`auth_method`：
  - `instance_id` **有值**（创建路径）→ **缓存** instance_id + auth_method，步骤 4 直接使用，调用方无需提供。
  - `instance_id` 为 **null**（已有 alias 路径）→ 回退到**调用方在 prompt 中提供的 instance_id**（若有）。
  - 仍**没有** instance_id（已有 alias 路径 + 调用方未给）→ 标记 `archive_skip_reason = "instance_id 缺失（已有 alias 路径且调用方未提供），archive 将跳过"`，步骤 4 据此跳过。
  - meta.json **文件缺失** → 同上标记（instance_id 取不到，archive 将跳过）。
- **捕获 install 实际使用的 server alias**（创建路径下由 install 自动生成）→ 作为步骤 3、4 的 alias 入参（调用方未给 alias 时尤为重要）。

> **即使安装失败也继续步骤 3**：验证可捕获机器当前真实状态，便于诊断。

> **meta.json 中的 alias 优先**：创建路径下 install 自动生成的 alias 是步骤 3（verify）和步骤 4（archive）的目标机器标识，编排层必须使用 meta.json 的 `server_alias`，不猜测。

### 步骤 3 — deploy-verify（远程验证）

用 **Agent** 调用（subagent_type: `deploy-verify`），prompt 传入：软件名、version、**步骤 2 确定的目标服务器别名**（创建路径下使用 meta.json 的 `server_alias`）。

agent 按 verify 指南**只读验证**，产出 `verify-result.md`（及 `verify-issues.md`）。捕获其整体结论（✅ 全部通过 / ⚠️ 无法判定 / ❌ 失败）及**结论原因**（软件验证失败 / 基础设施异常 / 验证指南无效）。

完成后：**用 Read 校验 `<verify-result.md>` 已落盘**；缺失则记为失败，并在汇总中如实标注。

> **verify 非 ✅ 则不执行步骤 4（archive）**：archive 的前提是软件已装好且验证通过。verify 结论非 ✅（⚠️ 无法判定或 ❌ 失败）或产物缺失时，编排层跳过 archive，直接进入步骤 5 汇总。
>
> **跳过原因必须按 verify 的「结论原因」字段如实归因，不得一律写成「验证未通过」**：
> - 原因「软件验证失败」→ 写「archive 因软件验证失败而跳过」
> - 原因「基础设施异常」（前提未满足/命令超时/连接或执行异常）→ 写「archive 因基础设施异常跳过——软件是否装好未能判定，需排除环境问题后重跑 verify」
> - 原因「验证指南无效」→ 写「archive 因验证指南不符合契约而跳过，需重跑 deploy-guide 重新生成指南」
>
> 把基础设施异常或指南问题写成软件未装好，与 deploy-verify 的判定语义冲突。

### 步骤 4 — deploy-archive（打包归档）

**前置条件**：步骤 3 verify 结论为 ✅（全部通过）。verify 未通过 / 产物缺失时**跳过本步**。

**instance_id 缺失时跳过**：若步骤 2 标记了 `archive_skip_reason`（instance_id 缺失），**跳过本步**，直接进入步骤 5 汇总，在汇总中如实标注「instance_id 缺失，archive 未执行」。

用 **Agent** 调用（subagent_type: `deploy-archive`），prompt 传入：软件名、version、目标服务器别名、**instance_id**（从步骤 2 缓存的 meta.json instance_id 取）、**auth_method**（从步骤 2 缓存的 meta.json auth_method 取；已有 alias 路径为 null 时传「未知」，agent 可从 ssh-skill 配置推断）。**不传 password**——archive 自己读 scope.yaml（ecs.py change-os 的 `--password` 不给时自动兜底 scope 的 `ecs_create.server.password`）。

agent 依次执行「清理 → 制镜像 → 切换 OS → 输出交付清单」，产出 `archive-result.md`（执行明细）+ `deploy-list.md`（交付清单）（及有问题的 `archive-issues.md`）。捕获其整体结论（✅ 成功 / ❌ 失败）。

完成后：**用 Read 校验 `<archive-result.md>` 与 `<deploy-list.md>` 已落盘**；任一缺失则记为失败。

> instance_id 来源：创建路径 → meta.json 自动产出；已有 alias 路径 → 调用方提供。编排层不猜测、不编造 instance_id。auth_method 来源：创建路径 → meta.json 自动产出；已有 alias 路径 → null（交付清单记「未知」，agent 可从 ssh-skill 配置推断）。密码不经编排层传递。

### 步骤 5 — 汇总

用 **Write** 落一份流水线汇总 `<output_dir>/<software>-pipeline-result.md`，并在对话中给出整体结论 + 汇总文件路径。

## 流水线汇总模板（`<software>-pipeline-result.md`）

```markdown
# <软件名> 端到端部署汇总

> 软件：<software> <version> | 目标机器：<server alias> | 安装路径：新创建/已有 | 日期：<YYYY-MM-DD>

## 四阶段结论
| 阶段 | 结论 | 产物 |
|------|------|------|
| deploy-guide（生成指南） | ✅/❌ | <install.md> / <verify.md> |
| deploy-install（远程安装） | ✅/⚠️/❌ | <install-result.md> (+ issues) + <install-meta.json> |
| deploy-verify（远程验证） | ✅/⚠️无法判定/❌ | <verify-result.md> (+ issues) |
| deploy-archive（打包归档） | ✅/❌/⊘跳过 | <archive-result.md> / <deploy-list.md> (+ issues) |

## 关键问题摘要
（install / verify / archive 的未通过项；无则写「无」。详见对应 -issues 文件）

## 产物清单
- 指南：<output_dir>/<install.md>、<verify.md>
- 安装结果：<output_dir>/<install-result.md>（+ <install-meta.json> 运行时元数据）
- 验证结果：<output_dir>/<verify-result.md>
- 打包结果：<output_dir>/<archive-result.md>、<deploy-list.md>
- 命令日志：logs/<别名>.log
```

> **archive 被跳过时**，结论列记 `⊘跳过`，产物列写跳过原因：
> - verify 未通过 →「（verify 未通过，archive 未执行）」
> - instance_id 缺失 →「（instance_id 缺失，archive 未执行）」

## 约束

- **四步强制执行（见「强制铁律」）**：不得合并、跳过步骤；install 依赖 guide 产物，verify 依赖 install 完成，archive 依赖 verify 通过，故**顺序执行、禁止并行**。
- **编排层零实操**：编排层**只**解析公共入参 + 调用子 agent + 校验产物 + 汇总。**禁止**亲自抓文档、**禁止**任何 `ssh`/ssh-skill 远程执行、**禁止**解析或执行安装/验证/清理命令、**禁止**亲自制镜像或切换 OS——实际工作一律下放到对应子 agent（上下文隔离）。
- **步骤间产物校验**：每步完成后用 Read 校验对应产物落盘，缺失即失败（步骤 1 缺失终止整条；步骤 2 result 缺失仍继续步骤 3 采证；步骤 3 未通过则跳过步骤 4）。
- **参数一致**：`software` / `version` 在四个 agent 间保持一致；version 以步骤 1 解析结果为准向后传。**目标服务器别名**以步骤 2 meta.json 的 `server_alias` 为准向后传（创建路径下 alias 由 install 自动生成）。
- **instance_id 传递链**：编排层从步骤 2 的 meta.json 读取 instance_id → 传给步骤 4 archive。已有 alias 路径下 meta.json 的 instance_id 为 null，回退到调用方提供的值；仍无则跳过 archive。编排层**不猜测、不编造** instance_id。
- **密码不经编排层**：编排层**不接收、不传递**密码。archive 切换 OS 时由 ecs.py change-os 自动从 scope.yaml 取密码。
- **不绕过各 agent 自身约束**：deploy-guide 禁止安装、install/verify 必须走 ssh-skill、verify 只读、archive 三步顺序执行且前序失败即停等，均由各 agent 自身保证。
- **失败处理**：步骤 1 失败则整条停止；步骤 2 失败仍执行步骤 3；步骤 3 未通过则跳过步骤 4；步骤 4 instance_id 缺失则跳过 archive；任何阶段失败或跳过都在汇总中如实标注。
- 完成后在对话中给出**整体结论 + 汇总文件路径 + 各阶段结论**。

## 调用示例

### 创建路径（全自动，不给 alias）

> 一键部署 nginx，安装文档见 https://nginx.org/en/docs/install.html，版本 1.25.3，打包制镜像

编排层将依次：deploy-guide 生成指南 → deploy-install 自动拉起 ECS + 注册 ssh-skill + 安装（产出 meta.json） → deploy-verify 验证 → deploy-archive 打包归档（instance_id 从 meta.json 取，密码从 scope.yaml 取）→ 输出 `deploy/nginx/1.25.3/nginx-pipeline-result.md` 汇总。

### 已有 alias 路径（指定机器）

> 一键把 nginx 部署到 prod-web-01，安装文档见 https://nginx.org/en/docs/install.html，版本 1.25.3，打包制镜像（instance_id=xxx）

编排层将依次：deploy-guide 生成指南 → deploy-install 在 prod-web-01 安装 → deploy-verify 验证 → deploy-archive 打包归档（instance_id 由调用方提供，密码从 scope.yaml 取）→ 输出 `deploy/nginx/1.25.3/nginx-pipeline-result.md` 汇总。
