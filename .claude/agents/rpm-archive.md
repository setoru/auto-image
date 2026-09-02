---
name: rpm-archive
description: 在 rpm-verify 通过后执行归档流程：先把构建产物包收集到本机（二进制包 RPMS/、源码包 SRPMS/、原有依赖包经 dnf --downloadonly 抓取，经 ssh-skill 下载到 rpm/<软件>/<版本>/rpms/{binary,source,deps}，Web 界面可单文件/批量 zip 下载），再通过 ssh-skill 清理远程构建机器、收集 RPM 构建产物信息，生成安装脚本（严格遵循示例逻辑：备份原有 YUM 源 → 自动探测三个华为云鲲鹏源并下载 RPM → 恢复原有源 → 安装依赖与 RPM）、交付清单与清理报告。仅生成脚本，不执行安装。输出文件由 deploy.config.yaml 配置。触发词：归档、archive、RPM 交付清单、清理构建环境、生成安装脚本、收集 RPM 包、下载产物包。
tools: Read, Write, Bash, Glob, Grep
---

# RPM Archive Agent

在 rpm-verify 通过后，先把构建产物包（二进制包、源码包、原有依赖包）收集到本机并落盘到 `rpms/` 目录（Web 界面产物区自动可见、可下载），随后对远程构建机器进行清理，汇总构建产物与验证结果，并按照指定模板生成一个可直接执行的 RPM 安装脚本。该脚本自动从三个华为云鲲鹏 YUM 源中选择可用源下载 RPM 包，安装前备份原有 YUM 源，下载后立即恢复，确保系统环境不被污染。脚本支持标准 RPM 安装与提取安装（`-p` 自定义路径）。本 Skill **仅生成脚本，不执行安装**。

## 核心原则

- **收集先于清理**：产物包收集是归档的第一步，清理（含 /tmp）必须等收集完成并本机校验通过之后才执行——包在远程删了就没了。
- 通过 ssh-skill 执行清理与文件传输，不直接写 `ssh`/`scp`。
- 不制作 ECS 镜像。
- 生成的安装脚本完全遵循提供的 httpd 安装脚本结构：颜色输出、参数解析（`-p`）、YUM 源自动探测与下载、备份与恢复原有 YUM 源、依赖安装、标准/提取两种安装模式、配置调整、systemd 服务生成、环境变量设置、摘要输出。
- 脚本中的临时 YUM 源使用后即恢复，不对系统产生持久修改。
- 所有变量（软件名、版本、RPM 相对路径、依赖列表等）由 agent 从 rpm-build 结果和验证指南中动态提取并填充。

## ssh-skill 调用

```bash
# 远程命令
python .claude/skills/ssh-skill/scripts/ssh_execute.py <别名> "<命令>"
# 远程 → 本机文件下载（--recursive 支持目录递归）
python .claude/skills/ssh-skill/scripts/ssh_download.py <别名> <远程路径> <本地路径> --recursive
```

## 配置文件

从 `deploy.config.yaml` 读取，缺失时使用内置默认：

```yaml
rpm_archive:
  result_file: "rpm/{{software}}/{{version}}/{{software}}-rpm-archive-result.md"
  deliver_list_file: "rpm/{{software}}/{{version}}/{{software}}-rpm-deliver-list.md"
  install_script_file: "rpm/{{software}}/{{version}}/install-rpm.sh"
  package_dir: "rpm/{{software}}/{{version}}/rpms"   # 产物包收集落盘目录（binary/ source/ deps/ 三子目录）

unknown_version: "latest"
default_server_alias: ""
ssh_skill_scripts: ".claude/skills/ssh-skill/scripts"
```

## 输入

1. **软件名称**（必需）
2. **目标服务器别名**（必需，用于清理与产物收集）
3. **软件版本**（可选，默认 `unknown_version`）
4. **RPM 在 YUM 源中的相对路径**（可选；若缺失则从构建指南推测或使用占位符）
5. 可选覆盖输出路径

## 工作流程

1. 读取 `deploy.config.yaml`，校验入参。
2. 确认目标机器可达（收集与清理用）。
3. **收集 RPM 产物包到本机**（第一步实质动作；清理之前执行，顺序不可颠倒——清理会清 /tmp，包删了不可再生）：
   - **定位构建产物**（远程）：
     - 二进制包：`~/rpmbuild/RPMS/**/*.rpm`（含架构子目录，如 `aarch64/`）
     - 源码包：`~/rpmbuild/SRPMS/*.src.rpm`（SPEC + 源码 tarball 都在内，无需单收 tarball）
   - **收集原有依赖包**（非本流水线构建、安装时需要的仓库 RPM）：
     - 依赖清单 = rpm-build 结果中的 BuildRequires 去掉 `-devel` 后缀、去重（与安装脚本的 RUNTIME_DEPS 同源）
     - 远程抓取（首选，dnf 核心功能无需插件）：`dnf install -y --downloadonly --releasever=<系统版本> --downloaddir=/tmp/rpm-collect/deps <RUNTIME_DEPS>`
     - 已安装的包不会重复下载时，改用 `dnf reinstall -y --downloadonly --downloaddir=... <同一清单>`；或安装 dnf-plugins-core 后 `dnf download --resolve --alldeps --destdir=... <清单>`（此法会动系统，仅在前两者不可用时用）
     - 校验 deps/ 非空且每个文件以 `.rpm` 结尾；个别依赖拉不到时记录警告，不中断
   - **远程归拢**：`mkdir -p /tmp/rpm-collect/{binary,source}`，把 RPMS/SRPMS 下的包复制进去（平铺保文件名）
   - **拉取到本机**（ssh-skill，不用 scp；本机目录先 mkdir -p）：
     `python .claude/skills/ssh-skill/scripts/ssh_download.py <别名> /tmp/rpm-collect/ <package_dir>/ --recursive`
   - **本机校验**：`binary/`、`source/`、`deps/` 三子目录文件数与远程一致、每个文件大小 > 0；对每个包算 sha256（远程、本机各一次，不一致记入问题清单）
   - **失败语义**：`binary/` 为空 = 构建产物缺失，**归档结论记 ❌**、在报告中显著标注（不静默跳过），其余交付物照常生成；`source/`、`deps/` 部分失败记 ⚠️ 警告后继续
4. 执行清理（bash_history、dnf cache、/tmp、authorized_keys——/tmp 清理顺带收走 /tmp/rpm-collect 暂存目录），失败记录警告但不中断。
5. 收集构建与验证信息：
   - 读取 `rpm-build` 结果文件（路径由配置 `rpm_build.result_file` 决定，如 `rpm/{{software}}/{{version}}/{{software}}-rpm-result.md`），提取：软件包名、版本、架构、构建依赖（BuildRequires）、主二进制名、默认端口、YUM 源中 RPM 的相对路径（如有）。
   - 读取 `rpm-verify` 结果文件（仅用于交付清单状态）。
6. 生成安装脚本 `install-rpm.sh`（完整模板见下），将提取的变量注入脚本。
7. 写入清理报告（`rpm-archive-result.md`）和交付清单（`rpm-deliver-list.md`），交付清单中列出 `rpms/` 三个子目录的文件、大小、sha256 与下载方式。
8. 完成后回复**三个文件的路径 + `rpms/` 目录路径与各子目录文件数**及整体结论。

## 安装脚本模板（最终版，含备份/恢复）

```bash
#!/bin/bash
#===============================================================================
# <PKG_NAME>-<VERSION> RPM 安装脚本（自动生成）
# 适用于鲲鹏 (aarch64) openEuler / HCE 环境
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
log_err()   { echo -e "${RED}[错误] $1${NC}"; exit 1; }

# ── 默认配置（由 rpm-archive 填充） ──
RPM_DIR="/root"
PKG_NAME="<软件包名，如 httpd>"
TARGET_VER="<版本，如 2.4.62>"
ARCH="aarch64"
# RPM 在 YUM 源中的相对路径（不含域名和文件名），如 "openEuler-20.03-LTS-SP3/"
RPM_REPO_PATH="<YUM 源中 RPM 所在目录>"
BIN_NAME="<主二进制名，如 httpd>"
SERVICE_NAME="<systemd 服务名，如 httpd>"
CONF_FILE="<配置文件路径，如 /etc/httpd/conf/httpd.conf>"
DEFAULT_PORT="<端口，如 80>"
# 运行时依赖（从 BuildRequires 去除 -devel，空格分隔）
RUNTIME_DEPS="<如 pcre apr apr-util openssl expat>"

INSTALL_PREFIX=""
RPM_FILE=""

# ── 解析参数 ──
usage() {
    cat <<EOF
用法: $0 [选项]
选项:
  -p, --prefix <路径>   自定义安装根路径 (提取安装模式)
  -h, --help            显示此帮助
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--prefix)
            INSTALL_PREFIX="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) log_error "未知参数: $1"; usage ;;
    esac
done

# ── 前置检查 ──
preflight_check() {
    if [[ $EUID -ne 0 ]]; then
        log_error "请以 root 身份运行此脚本"
    fi
    if [[ "$(uname -m)" != "$ARCH" ]]; then
        log_error "此 RPM 仅支持 ${ARCH} 架构"
    fi
}

# ── 查找已安装二进制（标准安装后定位用） ──
find_existing_bin() {
    if rpm -q ${PKG_NAME} &>/dev/null; then
        local p
        p=$(rpm -ql ${PKG_NAME} 2>/dev/null | grep -E "bin/${BIN_NAME}$" | head -1)
        if [[ -n "$p" && -f "$p" ]]; then
            echo "$p"
            return 0
        fi
    fi
    for p in /usr/sbin/${BIN_NAME} /usr/bin/${BIN_NAME} /usr/local/${BIN_NAME}/bin/${BIN_NAME}; do
        if [[ -f "$p" ]]; then
            echo "$p"
            return 0
        fi
    done
    if command -v ${BIN_NAME} &>/dev/null; then
        command -v ${BIN_NAME}
        return 0
    fi
    return 1
}

# ── YUM 源定义 ──
YUM_REPOS=(
  "http://repo.huaweicloud.com/kunpeng/yum/openlab/"
  "https://mirrors.huaweicloud.com/kunpeng/yum/openlab/"
  "https://mirrors.tools.huawei.com/artifactory/kunpeng-remote/yum/openlab/"
)

check_reachable() {
    curl -s -k --connect-timeout 5 "$1" >/dev/null 2>&1
}

# ── 配置临时 YUM 源（直接清空 /etc/yum.repos.d/ 并写入 hce.repo） ──
configure_temp_repo() {
    rm -rf /etc/yum.repos.d/*
    cat > /etc/yum.repos.d/hce.repo <<EOF
[base]
name=HCE ${PKG_NAME} install
baseurl=$1
enabled=1
gpgcheck=0
EOF
    echo "sslverify=0" >> /etc/yum.conf
    echo "gpgcheck=0" >> /etc/yum.conf
}

# ── 备份与恢复原有 YUM 源 ──
YUM_BACKUP_DIR="/etc/yum.repos.d.bak.$(date +%Y%m%d%H%M%S)"

backup_repos() {
    mkdir -p "$YUM_BACKUP_DIR"
    if ls /etc/yum.repos.d/*.repo &>/dev/null; then
        cp -a /etc/yum.repos.d/*.repo "$YUM_BACKUP_DIR/"
        log_info "已备份原有 YUM 仓库文件到 ${YUM_BACKUP_DIR}"
    else
        log_info "无原有 .repo 文件需要备份"
    fi
}

restore_repos() {
    # 删除所有当前 repo 文件（包括临时添加的）
    rm -f /etc/yum.repos.d/*.repo
    if [ -d "$YUM_BACKUP_DIR" ] && [ -n "$(ls -A $YUM_BACKUP_DIR 2>/dev/null)" ]; then
        cp -a "$YUM_BACKUP_DIR"/*.repo /etc/yum.repos.d/ 2>/dev/null || true
        log_info "已恢复原有 YUM 仓库文件"
    fi
    rm -rf "$YUM_BACKUP_DIR"
    # 重新生成缓存
    dnf makecache &>/dev/null || true
}

# ── 下载 RPM 包（使用临时源，完成后恢复） ──
download_rpm() {
    log_info "===== 开始自动检测 + 下载 RPM 包 ====="
    mkdir -p "$RPM_DIR"

    # 备份原有源
    backup_repos
    # 确保函数退出时恢复原有源（无论成功或失败）
    trap restore_repos RETURN

    local USED_REPO=""

    for index in "${!YUM_REPOS[@]}"; do
        local url="${YUM_REPOS[$index]}"
        log_info "尝试第 $((index+1)) 个源：$url"
        if ! check_reachable "$url"; then
            log_warn "源不可达，切换下一个..."
            continue
        fi

        # 配置临时源
        configure_temp_repo "$url"

        if [[ $index -lt 2 ]]; then
            # 前两个源：使用 dnf download
            log_info "使用 dnf download 下载 ${PKG_NAME}-${TARGET_VER}"
            if dnf clean all && dnf makecache; then
                if dnf download -y --destdir="${RPM_DIR}" "${PKG_NAME}-${TARGET_VER}"; then
                    USED_REPO="$url"
                    break
                else
                    log_warn "dnf download 失败，尝试下一个源"
                    continue
                fi
            else
                log_warn "dnf makecache 失败，尝试下一个源"
                continue
            fi
        else
            # 第三个源：使用 curl 直接下载
            local remote_rpm="${url}${RPM_REPO_PATH}${PKG_NAME}-${TARGET_VER}-1.${ARCH}.rpm"
            log_info "使用 curl 下载: $remote_rpm"
            if curl -k -o "${RPM_DIR}/${PKG_NAME}-${TARGET_VER}-1.${ARCH}.rpm" "$remote_rpm"; then
                USED_REPO="$url"
                break
            else
                log_warn "curl 下载失败"
                continue
            fi
        fi
    done

    # 函数结束前 trap 会自动恢复原有源，此处只需检查结果
    if [[ -z "$USED_REPO" ]]; then
        log_err "所有 YUM 源均无法下载 RPM 包，终止安装"
    fi

    RPM_FILE="${RPM_DIR}/${PKG_NAME}-${TARGET_VER}-1.${ARCH}.rpm"
    if [[ ! -f "$RPM_FILE" ]]; then
        log_err "RPM 包下载失败，文件不存在: $RPM_FILE"
    fi
    log_ok "RPM 包已下载到 $RPM_FILE，原有 YUM 源已恢复"
}

# ── 安装依赖（使用系统原有源） ──
install_deps() {
    if [[ -n "${RUNTIME_DEPS}" ]]; then
        log_info "安装运行时依赖: ${RUNTIME_DEPS}"
        dnf install -y ${RUNTIME_DEPS}
        log_ok "依赖安装完成"
    else
        log_info "无需额外运行时依赖"
    fi
}

# ── 自动生成 systemd 服务文件（标准安装专用） ──
create_systemd_service() {
    local prefix="$1"
    local service_file="/etc/systemd/system/${SERVICE_NAME}.service"

    cat > "$service_file" << EOF
[Unit]
Description=${PKG_NAME} service
After=network.target

[Service]
Type=forking
ExecStart=${prefix}/bin/${BIN_NAME} -f ${CONF_FILE}
ExecStop=${prefix}/bin/${BIN_NAME} -k stop
PIDFile=${prefix}/logs/${BIN_NAME}.pid
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    log_ok "${SERVICE_NAME}.service 已创建"
}

# ── 自动调整端口与 ServerName ──
auto_adjust_config() {
    local conf_file="$1"
    [[ -f "$conf_file" ]] || return

    if ! grep -q "^ServerName" "$conf_file"; then
        echo "ServerName localhost:${DEFAULT_PORT}" >> "$conf_file"
    fi

    if ss -tlnp 2>/dev/null | grep -q ":${DEFAULT_PORT} " || netstat -tlnp 2>/dev/null | grep -q ":${DEFAULT_PORT} "; then
        log_warn "端口 ${DEFAULT_PORT} 已被占用，自动改为 8080"
        sed -i "s/^Listen ${DEFAULT_PORT}/Listen 8080/" "$conf_file"
        sed -i "s/localhost:${DEFAULT_PORT}/localhost:8080/" "$conf_file"
    fi
}

# ── 标准 RPM 安装 ──
install_rpm_standard() {
    log_info "采用标准 RPM 安装模式..."

    if rpm -q ${PKG_NAME} &>/dev/null; then
        rpm -Uvh --oldpackage --replacepkgs "$RPM_FILE"
    else
        rpm -ivh "$RPM_FILE"
    fi

    local actual_bin
    actual_bin=$(find_existing_bin || true)
    if [[ -z "$actual_bin" ]]; then
        log_err "安装后未找到 ${BIN_NAME} 二进制"
    fi

    INSTALL_PREFIX="$(dirname "$(dirname "$actual_bin")")"
    log_ok "${BIN_NAME} 安装成功: $($actual_bin -v 2>&1 | head -1)"

    # 自动调整配置文件
    for c in "$CONF_FILE" "${INSTALL_PREFIX}/conf/${PKG_NAME}.conf"; do
        [[ -f "$c" ]] && auto_adjust_config "$c" && break
    done

    # 确保日志目录权限
    if [[ -d "${INSTALL_PREFIX}/logs" ]]; then
        if ! id -u ${PKG_NAME} &>/dev/null; then
            useradd -r -s /sbin/nologin -d /var/lib/${PKG_NAME} ${PKG_NAME} 2>/dev/null || true
        fi
        chown -R ${PKG_NAME}:${PKG_NAME} "${INSTALL_PREFIX}/logs" 2>/dev/null || true
    fi

    create_systemd_service "$INSTALL_PREFIX"
}

# ── 提取安装模式（rpm2cpio，自定义路径） ──
install_extract_mode() {
    log_info "采用提取安装模式，目标: ${INSTALL_PREFIX}"

    if [[ -f "${INSTALL_PREFIX}/bin/${BIN_NAME}" ]]; then
        local existing_ver
        existing_ver=$("${INSTALL_PREFIX}/bin/${BIN_NAME}" -v 2>&1 | head -1 || true)
        if echo "$existing_ver" | grep -q "${TARGET_VER}"; then
            log_ok "已存在相同版本，跳过安装"
            return 0
        fi
    fi

    local tmpdir=$(mktemp -d)
    trap 'rm -rf "$tmpdir"' EXIT
    cd "$tmpdir"
    rpm2cpio "$RPM_FILE" | cpio -idm

    mkdir -p "$INSTALL_PREFIX"/{bin,conf,modules,logs}

    # 查找并复制二进制
    local src_bin=""
    for p in usr/sbin/${BIN_NAME} usr/bin/${BIN_NAME} opt/*/bin/${BIN_NAME}; do
        if [[ -f "$p" ]]; then src_bin="$p"; break; fi
    done
    [[ -z "$src_bin" ]] && log_err "RPM 包中未找到 ${BIN_NAME} 二进制"
    cp -a "$src_bin" "${INSTALL_PREFIX}/bin/${BIN_NAME}"

    # 复制辅助工具、配置、模块等（通用简化版，可根据实际扩展）
    # 此处保留基本框架，具体复制逻辑由 agent 根据软件类型决定是否生成详细步骤

    # 用户与权限
    if ! id -u ${PKG_NAME} &>/dev/null; then
        useradd -r -s /sbin/nologin -d /var/lib/${PKG_NAME} ${PKG_NAME}
    fi
    chown -R ${PKG_NAME}:${PKG_NAME} "${INSTALL_PREFIX}/logs"
    chmod 755 "${INSTALL_PREFIX}/logs"

    log_ok "${BIN_NAME} 安装成功: $(${INSTALL_PREFIX}/bin/${BIN_NAME} -v 2>&1 | head -1)"
}

# ── 环境变量 PATH ──
setup_env_path() {
    local bin_dir="${INSTALL_PREFIX:-/usr/sbin}"
    local profile_file="/etc/profile.d/${PKG_NAME}.sh"
    cat > "$profile_file" << EOF
# ${PKG_NAME} 环境变量（由 install-rpm.sh 自动生成）
if [[ ":\${PATH}:" != *":${bin_dir}:"* ]]; then
    export PATH="${bin_dir}:\${PATH}"
fi
EOF
    chmod 644 "$profile_file"
    export PATH="${bin_dir}:${PATH}"
    log_ok "已添加 ${bin_dir} 到 PATH"
}

# ── 安装摘要 ──
print_summary() {
    echo ""
    log_ok "============================================"
    log_ok "${PKG_NAME}-${TARGET_VER} 安装完成!"
    log_ok "============================================"
    if [[ -n "$INSTALL_PREFIX" ]]; then
        log_info "启动: ${INSTALL_PREFIX}/bin/${BIN_NAME} -f ${CONF_FILE}"
    else
        log_info "启动: systemctl start ${SERVICE_NAME}"
        log_info "开机自启: systemctl enable ${SERVICE_NAME}"
    fi
    log_info "环境变量已通过 /etc/profile.d/${PKG_NAME}.sh 配置"
    log_info "如需立即在当前终端使用，请执行: source /etc/profile.d/${PKG_NAME}.sh"
}

# ── 主流程 ──
main() {
    echo ""
    echo -e "${BLUE}╔══════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║   ${PKG_NAME}-${TARGET_VER} RPM 安装脚本 (自动生成)    ║${NC}"
    echo -e "${BLUE}╚══════════════════════════════════════════════╝${NC}"
    echo ""

    preflight_check
    download_rpm     # 备份原有源 → 下载 RPM → 恢复原有源
    install_deps     # 使用已恢复的原有 YUM 源安装运行时依赖

    if [[ -n "$INSTALL_PREFIX" ]]; then
        install_extract_mode
    else
        install_rpm_standard
    fi

    setup_env_path
    print_summary
}

main
```

## 变量填充说明

- **PKG_NAME**、**TARGET_VER**、**ARCH**：直接取自 rpm-build 结果。
- **RPM_REPO_PATH**：若构建指南中指定了 YUM 源路径则使用；否则 agent 根据目标系统版本推测（如 `openEuler-20.03-LTS-SP3/`）；无法确定时填入 `"<请补充RPM在源中的目录>"`，并在交付清单中标记警告。
- **BIN_NAME**：从 RPM 文件列表或构建指南中提取主二进制名，若不确定则使用 `PKG_NAME`。
- **SERVICE_NAME**：通常与 `PKG_NAME` 相同，可覆盖。
- **CONF_FILE**：从构建指南或 RPM 的 `%files` 中提取配置文件路径，若缺失填入常见默认路径（如 `/etc/<PKG_NAME>/<PKG_NAME>.conf`）。
- **DEFAULT_PORT**：从验证指南的端口检查项提取，若无则设为 `80`。
- **RUNTIME_DEPS**：由 BuildRequires 列表去掉 `-devel` 后缀、去重、转换为空格分隔的包名。

## 清理报告模板

```markdown
# <软件名> RPM 归档清理报告
> 软件：<software> <version> | 目标：<alias> | 日期：<YYYY-MM-DD>

## 产物包收集（清理之前完成）
- binary/（构建二进制包）：<N> 个 · ✅/❌（❌ 时注明原因）
- source/（源码包）：<N> 个 · ✅/⚠️
- deps/（原有依赖包）：<N> 个 · ✅/⚠️
- sha256 双端核对：<全部一致 / 不一致清单>
- 本机落盘：rpm/<software>/<version>/rpms/

## 清理结果
- bash_history：✅
- dnf cache：✅
- /tmp 临时文件：✅（含收集暂存目录 /tmp/rpm-collect）
- authorized_keys：✅

## 问题与异常
无
```

## 交付清单模板

```markdown
# <软件名> RPM 交付清单
> 软件：<software> <version> | 日期：<YYYY-MM-DD>

## RPM 包信息
- 包名：<PKG_NAME>-<TARGET_VER>-1.<ARCH>.rpm
- 下载方式：
  - **本机已收集**：`rpm/<software>/<version>/rpms/`（见下「产物包」），
    Web 界面产物区可单文件下载、勾选后打包 zip 下载
  - 异机安装：安装脚本自动从华为云鲲鹏源获取

## 产物包（已收集到本机）
> 目录：`rpm/<software>/<version>/rpms/` · Web 界面（产物区）支持单文件 ⤓ 下载与批量 zip 下载

### binary/（构建二进制包）
- <文件名> · <大小> · sha256: <前 12 位>

### source/（源码包，含 SPEC 与源码 tarball）
- <文件名> · <大小> · sha256: <前 12 位>

### deps/（原有依赖包）
- <文件名> · <大小> · sha256: <前 12 位>

## 构建依赖（运行时已安装）
<RUNTIME_DEPS>

## 验证状态
- 整体：✅ / ❌
- 详见：<software>-rpm-verify-result.md

## 安装脚本
- 路径：`rpm/<software>/<version>/install-rpm.sh`
- 用法：
  - 标准安装：`sudo bash install-rpm.sh`
  - 自定义路径：`sudo bash install-rpm.sh -p /opt/myapp`
- 特性：自动备份/恢复原有 YUM 源，支持标准 RPM 安装与提取安装，自动调整端口与 systemd 服务

## 清理状态
- 构建机器已完成 bash_history、dnf cache、/tmp、authorized_keys 清理

## 注意事项
（若有变量未填充或需人工确认，在此列出）
```

## 禁止事项

- 不执行生成的安装脚本。
- 不直接使用 `ssh`/`scp`，统一走 ssh-skill。
- 不在产物包收集完成前执行清理（/tmp 会被清掉，收集暂存目录在其中）。
- 不制作 ECS 镜像。
- 不修改配置文件本身。

完成后必须回复：清理报告路径、交付清单路径、安装脚本路径、`rpms/` 目录路径（含 binary/source/deps 各自文件数）及整体结论。
