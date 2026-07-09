---
name: deploy
version: 1.0.0
description: "CRITICAL: 端到端 Ubuntu 软件部署编排——把 deploy-guide → deploy-install → deploy-verify 三个 agent 串成一条流水线：生成部署/验证指南 → 经 ssh-skill 在目标机安装 → 经 ssh-skill 验证 → 汇总。当用户要求『一键部署』『端到端部署』『部署并验证』『串联部署』『完整部署某软件到某机器』时使用。Triggers: 一键部署, 端到端部署, 部署并验证, 完整部署, 串联部署, 部署流水线, deploy pipeline, end-to-end deploy, /deploy, 全流程部署。需要同时给出：软件名 + 安装文档/链接 + 目标服务器别名。"
allowed-tools: Agent, Read, Write, Bash, Glob
keywords: 部署, deploy, 一键部署, 端到端, pipeline, 安装, 验证, 串联, 流水线, 全流程, ssh, 远程
---

# Deploy 端到端部署流水线

把三个 agent 串成一条流水线，完成「出指南 → 远程安装 → 远程验证」：

```
deploy-guide    生成 install.md + verify.md（只探索，不安装）
       ↓  （产物落盘，路径由 deploy.config.yaml 决定，自动对齐）
deploy-install  读 install.md → ssh-skill 远程安装 → install-result.md (+ issues)
       ↓
deploy-verify   读 verify.md  → ssh-skill 远程验证 → verify-result.md (+ issues)
```

三端输入/输出路径都由项目根目录的 **`deploy.config.yaml`（共用）** 决定，按 `{{software}}`/`{{version}}` 解析。**编排层只需把 software / version / server-alias 一致地传给三个 agent**，无需手工对齐路径。

## 强制铁律（不可妥协）

本 skill 是**纯编排层**：只做「解析公共入参 → 按序调用三个子 agent → 校验每步产物 → 汇总」，**不亲自做任何实际工作**。以下三条强制执行，破坏任一条即视为编排失败：

1. **必须依次执行全部 3 个步骤，不得合并、跳过或内联替做**：deploy-guide → deploy-install → deploy-verify。
   - 唯一允许的提前终止：步骤 1（guide）失败 → 后续无输入，整条停止。
   - 步骤 2（install）失败**仍须执行**步骤 3（verify），以采证机器真实状态。

2. **3 个步骤各是一个独立的 Agent 子 agent**——编排层**禁止**亲自抓文档、**禁止**任何 `ssh`/ssh-skill 远程执行、**禁止**解析或执行安装命令；这些只能在各自子 agent 内完成。
   - **为什么必须用子 agent**：**上下文隔离**。文档抓取与远程命令会产出大量上下文，关在各子 agent 内即可；编排层只回收每步的「结论 + 产物路径」，避免主上下文膨胀与互相串扰，也确保各 agent 自身约束（guide 禁装、verify 只读）不被绕过。

3. **步骤之间以「输出件」硬约束**：每步都有明确产物文件（见对照表）。进入下一步**前**，编排层必须用 **Read**（或 Bash `[ -f <文件> ]`）确认上一步产物已落盘；产物缺失即视为该步失败。这正是「不可跳过」的 enforcement——没有 guide 的 `install.md`，install 无从读起。

### 步骤产物对照表（enforcement 锚点）

| 步骤 | 子 agent | 必须产出（落盘到 output_dir） | 进入下一步前的校验 |
|------|----------|-------------------------------|--------------------|
| 1 guide | deploy-guide | `<install.md>`、`<verify.md>` | Read 两文件；任一缺失 → 终止整条 |
| 2 install | deploy-install | `<install-result.md>`（有问题另出 `<install-issues.md>`） | Read `install-result.md`；缺失记失败，**仍继续步骤 3** |
| 3 verify | deploy-verify | `<verify-result.md>`（未通过项另出 `<verify-issues.md>`） | Read `verify-result.md`；缺失记失败 |

## 输入（调用方在 prompt 中提供）

1. **软件名称**（必需）
2. **安装文档或链接**（必需）：URL / GitHub 仓库链接 / 本地文件 / 粘贴内容
3. **目标服务器别名**（必需；ssh-skill 已配置的别名）
4. **软件版本**（可选；不提供则由 deploy-guide 推断，推断结果向后传递）
5. **分支/标签**（可选；GitHub 仓库时）
6. **目标 Ubuntu 版本**（可选；默认 auto）

## 执行步骤

**MUST**（见上方「强制铁律」）：三个步骤**每步都用 Agent 工具调用对应子 agent**，**阻塞完成后再调下一个**，禁止并行——install 依赖 guide 产物，verify 依赖 install 完成。步骤 0、4 是编排层内部的解析与汇总，不计入这三步。

### 步骤 0 — 解析公共路径

- 用 **Read** 读 `deploy.config.yaml`，取 `output_dir`，代入本次 `software`/`version` 得到产物目录（如 `deploy/nginx/1.25.3`），用于后续汇总与流水线报告路径。
- 用 **Bash** `date +%F` 取当日日期。
- 若调用方未给 version，先记为待定（以步骤 1 deploy-guide 解析结果为准）。

### 步骤 1 — deploy-guide（生成指南）

用 **Agent** 调用（subagent_type: `deploy-guide`），prompt 传入：软件名、文档/链接、版本（若有）、分支（若有）、Ubuntu 版本（若有）。

完成后（产物校验 = 强制铁律第 3 条）：
- **捕获 deploy-guide 实际使用的 `software` / `version`**（其回复中会告知）→ 作为步骤 2、3 的固定入参。
- **用 Read 校验 `<install.md>` 与 `<verify.md>` 已落盘**（路径 = output_dir + 配置文件名）。两者齐备方可进入步骤 2。
- **任一缺失 / guide 失败 → 停止整条流水线**，报告原因（后续阶段无输入，无法进行）。

### 步骤 2 — deploy-install（远程安装）

用 **Agent** 调用（subagent_type: `deploy-install`），prompt 传入：软件名、**步骤 1 解析的 version**、目标服务器别名。

agent 会自行按配置路径读 install 指南、经 ssh-skill 在目标机安装，产出 `install-result.md`（及 `install-issues.md`）。捕获其整体结论（✅ 成功 / ⚠️ 部分 / ❌ 失败）。

完成后：**用 Read 校验 `<install-result.md>` 已落盘**；缺失则记为失败，但**不阻塞步骤 3**。

> **即使安装失败也继续步骤 3**：验证可捕获机器当前真实状态，便于诊断。

### 步骤 3 — deploy-verify（远程验证）

用 **Agent** 调用（subagent_type: `deploy-verify`），prompt 传入：软件名、version、目标服务器别名。

agent 按 verify 指南**只读验证**，产出 `verify-result.md`（及 `verify-issues.md`）。捕获其整体结论（✅ 全部通过 / ⚠️ 部分 / ❌ 失败）。

完成后：**用 Read 校验 `<verify-result.md>` 已落盘**；缺失则记为失败，并在汇总中如实标注。

### 步骤 4 — 汇总

用 **Write** 落一份流水线汇总 `<output_dir>/<software>-pipeline-result.md`，并在对话中给出整体结论 + 汇总文件路径。

## 流水线汇总模板（`<software>-pipeline-result.md`）

```markdown
# <软件名> 端到端部署汇总

> 软件：<software> <version> | 目标机器：<server alias> | 日期：<YYYY-MM-DD>

## 三阶段结论
| 阶段 | 结论 | 产物 |
|------|------|------|
| deploy-guide（生成指南） | ✅/❌ | <install.md> / <verify.md> |
| deploy-install（远程安装） | ✅/⚠️/❌ | <install-result.md> (+ issues) |
| deploy-verify（远程验证） | ✅/⚠️/❌ | <verify-result.md> (+ issues) |

## 关键问题摘要
（install / verify 的未通过项；无则写「无」。详见对应 -issues 文件）

## 产物清单
- 指南：<output_dir>/<install.md>、<verify.md>
- 安装结果：<output_dir>/<install-result.md>
- 验证结果：<output_dir>/<verify-result.md>
- 命令日志：logs/<别名>.log
```

## 约束

- **三步强制执行（见「强制铁律」）**：不得合并、跳过步骤；install 依赖 guide 产物，verify 依赖 install 完成，故**顺序执行、禁止并行**。
- **编排层零实操**：编排层**只**解析公共入参 + 调用子 agent + 校验产物 + 汇总。**禁止**亲自抓文档、**禁止**任何 `ssh`/ssh-skill 远程执行、**禁止**解析或执行安装/验证命令——实际工作一律下放到对应子 agent（上下文隔离）。
- **步骤间产物校验**：每步完成后用 Read 校验对应产物落盘，缺失即失败（步骤 1 缺失终止整条；步骤 2 缺失仍继续步骤 3 采证）。
- **参数一致**：`software` / `version` 在三个 agent 间保持一致；version 以步骤 1 解析结果为准向后传。
- **不绕过各 agent 自身约束**：deploy-guide 禁止安装、install/verify 必须走 ssh-skill、verify 只读等，均由各 agent 自身保证。
- **别名缺失**：步骤 2/3 的 agent 会自行停止并报告；编排层如实汇总，不猜测机器。
- **失败处理**：步骤 1 失败则整条停止；步骤 2 失败仍执行步骤 3；任何阶段失败都在汇总中如实标注。
- 完成后在对话中给出**整体结论 + 汇总文件路径 + 各阶段结论**。

## 调用示例

> 一键把 nginx 部署到 prod-web-01，安装文档见 https://nginx.org/en/docs/install.html，版本 1.25.3

编排层将依次：deploy-guide 生成指南 → deploy-install 在 prod-web-01 安装 → deploy-verify 验证 → 输出 `deploy/nginx/1.25.3/nginx-pipeline-result.md` 汇总。
