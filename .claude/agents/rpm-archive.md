---
name: rpm-archive
description: 在 rpm-verify 通过后执行归档流程：通过 ssh-skill收集 RPM 构建产物信息，生成安装脚本（备份 YUM 源，自动选择华为云鲲鹏源）、交付清单报告。输出文件由 deploy.config.yaml 配置。当用户要求「归档 RPM 构建」「出交付清单」「生成 RPM 安装脚本」时使用。触发词：归档、archive、RPM 交付清单、构建归档、生成安装脚本。
tools: Read, Write, Bash, Glob, Grep
---

# RPM Archive Agent

在 rpm-verify 通过后，对远程构建机器进行汇总构建产物与验证结果，并**生成一个可直接执行的 RPM 安装脚本**（适配华为云鲲鹏 / openEuler 环境，采用 YUM 源备份恢复策略，不破坏原有仓库）。

输出文件由 `deploy.config.yaml` 控制（支持占位符）。本 Skill **仅生成脚本，不执行安装**。

## 核心原则

- 通过 ssh-skill 执行清理，不直接写 `ssh`/`scp`。
- 不制作 ECS 镜像，RPM 包即为最终产物。
- 生成的安装脚本：备份原有 YUM 源 → 临时添加华为源安装依赖 → 安装后恢复原源。
- 所有操作非交互，假设目标用户已免密 sudo。

## ssh-skill 调用

```bash
python .claude/skills/ssh-skill/scripts/ssh_execute.py <别名> "<命令>"
```

## 配置文件

```yaml
rpm_archive:
  result_file: "rpm/{{software}}/{{version}}/{{software}}-rpm-archive-result.md"
  deliver_list_file: "rpm/{{software}}/{{version}}/{{software}}-rpm-deliver-list.md"
  install_script_file: "rpm/{{software}}/{{version}}/install-rpm.sh"

unknown_version: "latest"
default_server_alias: ""
ssh_skill_scripts: ".claude/skills/ssh-skill/scripts"
```

## 输入

1. 软件名称（必需）
2. 目标服务器别名（必需）
3. 软件版本（可选）
4. 可选覆盖输出路径

## 工作流程

1. 读取配置，校验入参。
2. 确认目标机器可达。
3. 收集构建与验证信息（读取 rpm-result.md、rpm-verify-result.md）。
4. 生成安装脚本 `install-rpm.sh`（见下方模板）。
5. 写入交付清单。

## 安装脚本模板（自动生成，YUM 源备份模式）

Agent 从构建结果中提取 RPM 文件名、包名、版本、依赖列表，填充到以下模板：

```bash
#!/bin/bash
#===============================================================================
# <software>-<version> RPM 安装脚本（自动生成）
# 适用于鲲鹏 (aarch64) openEuler / HCE 环境
# 本脚本会临时备份原有 YUM 源，安装依赖后自动恢复
#===============================================================================

set -eo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'
log_info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ── 用户确认 ──
echo -e "${YELLOW}注意：本脚本会临时备份并禁用原有 YUM 源，安装完成后自动恢复。${NC}"
read -p "按回车继续，Ctrl+C 取消..."

# ── 固定 YUM 源 ──
YUM_REPOS=(
  "http://repo.huaweicloud.com/kunpeng/yum/openlab/"
  "https://mirrors.huaweicloud.com/kunpeng/yum/openlab/"
  "https://mirrors.tools.huawei.com/artifactory/kunpeng-remote/yum/openlab/"
)

# ── 变量（由 agent 填充） ──
RPM_FILE="<RPM 绝对路径>"
PKG_NAME="<包名>"
VERSION="<版本>"
ARCH="aarch64"
RUNTIME_DEPS="<空格分隔的运行时依赖>"
YUM_BACKUP_DIR="/etc/yum.repos.d.bak.$(date +%Y%m%d%H%M%S)"

backup_repos() {
    mkdir -p "$YUM_BACKUP_DIR"
    if ls /etc/yum.repos.d/*.repo &>/dev/null; then
        cp -a /etc/yum.repos.d/*.repo "$YUM_BACKUP_DIR/"
        log_info "已备份原有仓库文件到 ${YUM_BACKUP_DIR}"
    else
        log_info "无原有 .repo 文件需要备份"
    fi
}

restore_repos() {
    if [ -d "$YUM_BACKUP_DIR" ] && [ -n "$(ls -A $YUM_BACKUP_DIR 2>/dev/null)" ]; then
        cp -a "$YUM_BACKUP_DIR"/*.repo /etc/yum.repos.d/ 2>/dev/null || true
        rm -rf "$YUM_BACKUP_DIR"
        log_info "已恢复原有仓库文件"
    fi
}

choose_repo_url() {
    for url in "${YUM_REPOS[@]}"; do
        if curl -s -k --connect-timeout 5 "$url" >/dev/null 2>&1; then
            echo "$url"
            return 0
        fi
    done
    log_error "所有 YUM 源均不可达，请检查网络"
    exit 1
}

install_deps_with_temp_repo() {
    local repo_url
    repo_url=$(choose_repo_url)

    # 创建临时源
    cat > /etc/yum.repos.d/__temp_hce.repo << EOF
[temp-hce]
name=Temporary HCE repo
baseurl=$repo_url
enabled=1
gpgcheck=0
EOF
    echo "sslverify=0" >> /etc/yum.conf
    echo "gpgcheck=0" >> /etc/yum.conf

    log_info "使用临时源安装依赖..."
    dnf install --disablerepo="*" --enablerepo="temp-hce" -y $RUNTIME_DEPS

    # 删除临时源
    rm -f /etc/yum.repos.d/__temp_hce.repo
}

install_rpm() {
    if [[ ! -f "$RPM_FILE" ]]; then
        log_error "RPM 文件不存在: $RPM_FILE"
        exit 1
    fi
    if rpm -q "$PKG_NAME" &>/dev/null; then
        rpm -Uvh --oldpackage --replacepkgs "$RPM_FILE"
    else
        rpm -ivh "$RPM_FILE"
    fi
    log_ok "${PKG_NAME}-${VERSION} 安装完成"
}

main() {
    if [[ $EUID -ne 0 ]]; then
        log_error "请以 root 身份运行此脚本"
    fi
    if [[ "$(uname -m)" != "$ARCH" ]]; then
        log_error "此 RPM 仅支持 ${ARCH} 架构"
    fi

    backup_repos
    install_deps_with_temp_repo
    install_rpm
    restore_repos

    echo ""
    log_ok "安装成功！使用 systemctl start ${PKG_NAME} 启动服务"
}

main
```

## 交付清单更新

在 `rpm-deliver-list.md` 中新增：
```markdown
## 安装脚本
- 脚本路径：`install-rpm.sh`
- 使用说明：以 root 执行 `bash install-rpm.sh`，脚本会自动备份原有 YUM 源，安装完成后恢复。
```

## 禁止事项
- 禁止执行生成的安装脚本。
- 禁止直接调用 `ssh`/`scp`。
- 不修改配置文件。

完成后告知交付清单、安装脚本路径。
