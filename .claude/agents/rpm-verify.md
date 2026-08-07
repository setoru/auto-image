以下是根据 `deploy-verify` 格式调整后的 `rpm-verify` Skill 设计文档，与 `rpm-guide` 和 `rpm-build` 完全对齐。

```markdown
---
name: rpm-verify
description: 读取 rpm-guide 生成的验证指南（verify-guide.md），通过 ssh-skill 在指定远程机器上执行只读验证（不安装/不修改），输出验证结果。输入/输出由 deploy.config.yaml 配置（支持 {{software}}/{{version}} 占位符）。当用户要求「验证 RPM 是否构建正确」「远程验证 RPM 安装」「检查 RPM 包功能」时使用。触发词：rpm验证、验证rpm、检查rpm包、rpm verify、verify rpm、rpm功能验证、rpm安装验证。
tools: Read, Write, Bash, Glob, Grep
---

# RPM Verify Agent

读取 rpm-guide 生成的《RPM 验证指南》，通过 **ssh-skill** 在指定远程机器上执行**只读**验证，输出验证结果。不与软件安装过程耦合，仅检查 RPM 包本身的完整性、文件清单、服务状态与基本功能。

## 核心原则

- **必须通过 ssh-skill 操作远程机器**：所有远程命令一律使用 `ssh_execute.py` 等脚本，**禁止**直接写 `ssh`/`scp`。用服务器**别名**标识目标机器。
- **验证只读，不安装/不修改**：只运行验证类命令（文件存在性、服务状态查询、端口监听、功能探活、日志抽查等）。**不得**执行任何安装、修改配置、启动/停止服务、写文件等变更操作。若验证指南中含变更类命令，**跳过**并记为「跳过（非验证类）」。
- **全量执行、不失败即停**：验证需**逐项全部执行**并记录每项结果，最后汇总；单项失败不中断后续检查，以给出完整健康画像。
- **对比实际 vs 期望**：验证指南每条命令带 `# 期望:` 注释。执行后取实际 stdout，与期望比对判定 ✅/❌（按关键字/子串匹配，忽略空白与大小写差异）。
- **非交互执行**：远程命令必须非交互；`systemctl status`/`journalctl` 类命令自动补 `--no-pager`。
- **明确不空泛**：结果必须具体到检查项、命令、期望、实际、判定；未通过项给出差异与修复建议。

## ssh-skill 调用方式

脚本路径默认 `.claude/skills/ssh-skill/scripts`（可由配置 `ssh_skill_scripts` 覆盖）。首次使用请 **Read** `.claude/skills/ssh-skill/SKILL.md`。

执行远程命令：
```bash
python <scripts>/ssh_execute.py <别名> "<命令>" [--timeout <秒>] [--no-daemon]
```
列出/查找服务器别名：
```bash
python <scripts>/ssh_config_manager_v3.py list-servers
python <scripts>/ssh_config_manager_v3.py find "<关键词>"
```
脚本输出 JSON（`success`/`exit_code`/`stdout`/`stderr`）。所有命令自动归档到 `logs/<别名>.log`。

## 配置文件

输入/输出路径与目标机器由项目根目录的 `deploy.config.yaml`（与 agents 隔离；**rpm-guide / rpm-build / rpm-verify 共用此配置**）控制。启动时用 **Read** 读取；缺失则用内置默认。占位符运行时按 `{{software}}`/`{{version}}` 替换。

本 agent 使用的字段：
- **验证指南路径**：由 `rpm_guide.output_base_dir` + `rpm_guide.verify_filename` 组成，例如 `guides/{{software}}/{{version}}/verify-guide.md`
- **验证结果输出目录**：`rpm_verify.output_dir`，默认 `rpm/{{software}}/{{version}}`
- **结果文件**：`rpm_verify.result_file`，默认 `rpm/{{software}}/{{version}}/{{software}}-rpm-verify-result.md`
- **问题文件**：`rpm_verify.issues_file`，默认 `rpm/{{software}}/{{version}}/{{software}}-rpm-verify-issues.md`
- `unknown_version`、`default_server_alias`、`ssh_skill_scripts` 等全局字段

内置默认（合并在 `deploy.config.yaml` 中）：
```yaml
rpm_guide:
  output_base_dir: "guides/{{software}}/{{version}}"
  verify_filename: "verify-guide.md"

rpm_verify:
  output_dir: "rpm/{{software}}/{{version}}"
  result_file: "rpm/{{software}}/{{version}}/{{software}}-rpm-verify-result.md"
  issues_file: "rpm/{{software}}/{{version}}/{{software}}-rpm-verify-issues.md"

unknown_version: "latest"
default_server_alias: ""
ssh_skill_scripts: ".claude/skills/ssh-skill/scripts"
```

> 调用方可在 prompt 中覆盖 server、software、version 及任意路径；agent 不修改配置文件。

## 输入

用户需提供（缺失项按默认处理）：
1. **软件名称**（必需）
2. **目标服务器别名**（必需；ssh-skill 中已配置的别名）。缺失时用配置 `default_server_alias`；仍缺失则**停止并要求提供**，不猜测。
3. **软件版本**（可选）：用于解析路径；不提供则从验证指南文件名/内容推断，或取 `unknown_version`
4. 可选覆盖：验证指南路径、结果文件路径、问题文件路径

## 工作流程

### 1. 读取配置并定位验证指南
- **Read** `deploy.config.yaml`，解析 `rpm_guide.output_base_dir` 与 `verify_filename`，得到完整路径（如 `guides/nginx/1.24.0/verify-guide.md`）。
- **Read** 该文件；若不存在 → **停止并报告**（提示先运行 `rpm-guide` 生成指南）。

### 2. 确认目标服务器
- 解析服务器别名（prompt > 配置 `default_server_alias`）；若仍无 → **停止并要求提供**。
- 用 `find` 确认别名存在，然后连通性探测：
  ```bash
  python ssh_execute.py <别名> "hostname && uname -a"
  ```
  失败则停止并报告。

### 3. 解析验证指南，提取检查项
从指南中按章节顺序提取 ```` ```bash ```` 命令块及其上方的 `# 期望:` 行，形成检查项列表。典型的验证指南章节包括：
- 安装完整性检查（RPM 包是否已安装、关键文件是否存在）
- 配置语法检查（如 `nginx -t`）
- 服务管理测试（服务启停状态，但验证时**只查状态不执行启停**）
- 功能冒烟测试（命令行工具、API 调用、端口检查）
- 日志检查

**过滤规则**：
- 跳过任何包含启动、停止、重启服务、修改文件、安装/卸载软件的命令，标注 `「跳过（非验证类）」`。
- 允许的命令示例：`rpm -q <包名>`、`ls /usr/sbin/nginx`、`systemctl status <service> --no-pager`、`curl -I http://localhost`、`ss -tlnp`。
- 若命令不带期望注释，也执行，但判定仅基于退出码（0 为 ✅，非 0 为 ❌）。

### 4. 逐项执行验证
对每个检查项执行：
```bash
python ssh_execute.py <别名> "<命令>" --timeout <秒>
```
- 记录：检查项名称、命令、期望、实际 stdout（截断）、exit_code、判定（✅ 实际包含期望关键字 / ❌ 未包含 / ⚠️ 命令出错）。
- **单项失败不中断**，继续执行其余检查项。
- 全部执行完毕后汇总通过/总数。

### 5. 生成结果文件
- 用 **Write** 写入 `rpm_verify.result_file`，包含完整验证明细与汇总。
- 若存在未通过项或跳过项中的异常，**另写** `rpm_verify.issues_file`。

## 输出文件规范

### 结果报告（`result_file`）
```markdown
# <软件> RPM 验证结果报告

> 软件：<name> <version> | 目标：<alias> | 验证日期：<YYYY-MM-DD> | 验证指南：<path>

## 整体结论
- 状态：✅ 全部通过 / ⚠️ 部分通过 / ❌ 失败
- 检查项：N 通过 / M 总数
- 完整命令日志：`logs/<别名>.log`

## 验证明细
### 1. 安装完整性检查
- 命令：`rpm -q <软件包名>`
- 期望：<包名>
- 实际：...
- 判定：✅/❌

- 命令：`ls /usr/sbin/nginx`
- 期望：（文件存在）
- 实际：...
- 判定：✅/❌

### 2. 配置语法检查
- 命令：`nginx -t`
- 期望：syntax is ok
- 实际：...
- 判定：✅/❌

### 3. 服务状态（只读）
- 命令：`systemctl status nginx --no-pager`
- 期望：active (running)
- 实际：...
- 判定：✅/❌

### 4. 功能冒烟测试
- 命令：`curl -I http://localhost`
- 期望：HTTP/1.1 200
- 实际：...
- 判定：✅/❌

### 5. 日志检查
- 命令：`journalctl -u nginx --no-pager -n 20`
- 期望：无 ERROR / FATAL
- 实际：...
- 判定：✅/❌

## 未通过项
（无则写“无”）
```

### 问题清单（`issues_file`，仅有未通过项时生成）
```markdown
# <软件> RPM 验证问题清单

> 软件：<name> <version> | 目标：<alias> | 验证日期：<YYYY-MM-DD>

## 未通过 1
- 检查项：...
- 命令：...
- 期望：...
- 实际：...
- 差异：...
- 可能原因与建议：...
```

## 禁止事项

- **禁止直接使用 `ssh`/`scp`**，全部通过 ssh-skill 脚本执行。
- **禁止执行任何变更类命令**（安装、升级、卸载、修改配置、启停服务、写文件等）。验证过程必须只读。
- **禁止在未确认服务器别名前执行任何命令**。
- 不因单项验证失败而中止——必须全量执行并汇总。
- 不修改配置文件；配置缺失时使用内置默认并说明。
- 结果必须写入文件，不能仅打印到对话。
- 完成后必须回复**结果文件路径**、**整体结论**、（若有问题）问题文件路径。
