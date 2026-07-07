#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
命令执行日志记录器（按服务器归档）

为 ssh-skill 提供持久化的命令执行归档：每条通过 ssh_execute.py / ssh_cluster.py
执行的命令都追加记录到「该服务器对应的文件」中，包含执行了什么命令、返回是啥。

存储方式（人类可读纯文本，直接打开即可查看，无需任何命令）：
    <项目根目录>/logs/<alias>.log
（项目根目录 = git 仓库根；非 git 仓库时为当前目录，且会避开 .claude 目录）
每条命令以分隔块的形式追加到对应服务器的 .log 文件末尾。

设计要点：
- 一台服务器一个文件，方便直接翻阅某台机器的全部命令历史
- 文件名按别名安全转义（非法字符替换为 _）
- 基于文件大小的滚动（避免无限增长），保留固定数量的历史文件（.1 .2 ...）
- stdout/stderr 超长时截断并在文件中标注
- 任何记录失败都不影响真正的命令执行（全部 try/except 包裹）

环境变量：
    SSH_SKILL_LOG_DIR            日志目录（默认 <项目根目录>/logs）
    SSH_SKILL_LOG_MAX_BYTES      单文件大小上限，超过则滚动（默认 10MB）
    SSH_SKILL_LOG_BACKUP_COUNT   保留的历史滚动文件数量（默认 5）
    SSH_SKILL_LOG_TRUNCATE_BYTES 单条 stdout/stderr 截断阈值（默认 64KB）
    SSH_SKILL_LOG_DISABLE        置为任意非空值则完全关闭记录

公开 API：
    log_command(alias, command, result=None, *, mode=None, duration_ms=None,
                error=None, source="ssh_execute")
    log_command_error(alias, command, error, *, mode=None, source="ssh_execute")
"""

import json
import os
import datetime

# ---- 配置常量（在函数体内引用，避免默认参数在 import 时被冻结）----
# 默认日志目录相对名：放在项目根目录（git 仓库根）下，而非 .claude 内
_DEFAULT_LOG_DIR_NAME = "logs"


def _project_root():
    """返回项目根目录。

    优先级：
      1. 当前工作目录向上查找的第一个含 .git 的目录（git 仓库根）
      2. 非 git 仓库：从当前工作目录向上跳过最末尾的 .claude 目录后返回
    这样日志会落在项目根下（如 <project>/logs），而不是嵌在 .claude/ 里。
    """
    cwd = os.getcwd()
    d = cwd
    for _ in range(64):
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent

    # 非 git 仓库：避开最末尾的 .claude 目录
    base = cwd
    while os.path.basename(base) == ".claude":
        base = os.path.dirname(base)
    return base


def _cfg(name, default):
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val


def _max_bytes():
    try:
        return int(_cfg("SSH_SKILL_LOG_MAX_BYTES", 10 * 1024 * 1024))
    except ValueError:
        return 10 * 1024 * 1024


def _backup_count():
    try:
        return int(_cfg("SSH_SKILL_LOG_BACKUP_COUNT", 5))
    except ValueError:
        return 5


def _truncate_bytes():
    try:
        return int(_cfg("SSH_SKILL_LOG_TRUNCATE_BYTES", 64 * 1024))
    except ValueError:
        return 64 * 1024


def _disabled():
    return bool((_cfg("SSH_SKILL_LOG_DISABLE", "") or "").strip())


def get_log_dir():
    """返回日志目录，按需创建。

    默认为「项目根目录下的 logs」（自动识别 git 仓库根；非 git 仓库时为当前
    目录的 logs，且会跳过末尾的 .claude 目录）。即日志落在 <project>/logs，
    而非 <project>/.claude/logs。
    可用环境变量 SSH_SKILL_LOG_DIR 覆盖（支持 ~ 和相对/绝对路径，相对路径
    基于当前工作目录解析）。
    """
    configured = _cfg("SSH_SKILL_LOG_DIR", "")
    if configured:
        log_dir = os.path.expanduser(configured)
        # 相对路径相对于当前工作目录解析
        if not os.path.isabs(log_dir):
            log_dir = os.path.join(os.getcwd(), log_dir)
    else:
        # 默认：<项目根目录>/logs
        log_dir = os.path.join(_project_root(), _DEFAULT_LOG_DIR_NAME)
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        # 目录创建失败时退回系统临时目录，确保尽量能写
        import tempfile
        log_dir = os.path.join(tempfile.gettempdir(), "ssh-skill-logs")
        try:
            os.makedirs(log_dir, exist_ok=True)
        except OSError:
            pass
    return log_dir


def _sanitize_alias(alias):
    """把别名转成安全的文件名：仅保留字母数字、点、下划线、减号，其余替换为 _。"""
    if not alias:
        return "unknown"
    safe = []
    for ch in str(alias):
        if ch.isalnum() or ch in (".", "_", "-"):
            safe.append(ch)
        else:
            safe.append("_")
    name = "".join(safe).strip("._-")
    return name or "unknown"


def get_log_path(alias):
    """返回指定别名对应的日志文件完整路径。"""
    return os.path.join(get_log_dir(), _sanitize_alias(alias) + ".log")


def _truncate(text, limit):
    """截断超长文本，返回 (截断后的文本, 是否截断)。"""
    if text is None:
        return None, False
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            text = repr(text)
    if limit and len(text.encode("utf-8", errors="replace")) > limit:
        kept = text
        while len(kept.encode("utf-8", errors="replace")) > limit - 64 and len(kept) > 0:
            kept = kept[: max(0, int(len(kept) * 0.9))]
        omitted = len(text) - len(kept)
        return kept + "\n...[truncated, ~%d chars omitted]" % omitted, True
    return text, False


def _rotate_if_needed(log_path):
    """文件超过上限时滚动：<alias>.log -> <alias>.log.1 -> .2 ..."""
    max_bytes = _max_bytes()
    backup_count = _backup_count()
    try:
        if not os.path.exists(log_path):
            return
        if os.path.getsize(log_path) < max_bytes:
            return

        oldest = "%s.%d" % (log_path, backup_count)
        if os.path.exists(oldest):
            os.remove(oldest)

        for i in range(backup_count - 1, 0, -1):
            src = "%s.%d" % (log_path, i)
            if os.path.exists(src):
                os.rename(src, "%s.%d" % (log_path, i + 1))

        os.rename(log_path, "%s.1" % log_path)
    except OSError:
        pass


def _now_iso():
    """当前本地时间的 ISO8601 字符串（秒级）。"""
    return datetime.datetime.now().replace(microsecond=0).isoformat(sep=" ")


# 分隔线
_BAR = "=" * 80
_SUB = "-" * 80


def _format_entry(alias, command, result, mode, duration_ms, error, source):
    """把一条命令格式化为人类可读的文本块。"""
    result = result or {}
    limit = _truncate_bytes()

    stdout, stdout_trunc = _truncate(result.get("stdout"), limit)
    stderr, stderr_trunc = _truncate(result.get("stderr"), limit)

    success = bool(result.get("success", False))
    exit_code = result.get("exit_code", None)

    # 头部：时间 | 别名 | 模式 | 耗时 | 来源
    mode_str = mode or "-"
    dur_str = ("%dms" % duration_ms) if duration_ms is not None else "-"
    header = "%s | %s | %s | %s | %s" % (_now_iso(), alias, mode_str, dur_str, source)

    lines = [_BAR, header, "$ " + command, _SUB]

    if error and not result:
        # 完全没拿到结果的失败（抛异常）
        lines.append("FAILED: %s" % error)
        lines.append(_SUB)
        lines.append("[stdout] (empty)")
        lines.append("[stderr] (empty)")
    else:
        # 正常结果（可能成功也可能非零退出）
        if success:
            status = "success"
        else:
            status = "failed"
        exit_str = ("exit %s" % exit_code) if exit_code is not None else "exit ?"
        lines.append("%s (%s)" % (exit_str, status))
        if error:
            lines.append("error: %s" % error)
        lines.append(_SUB)

        lines.append("[stdout]")
        if stdout:
            lines.append(stdout)
            if stdout_trunc:
                lines.append("...[stdout truncated]")
        else:
            lines.append("(empty)")
        lines.append(_SUB)

        lines.append("[stderr]")
        if stderr:
            lines.append(stderr)
            if stderr_trunc:
                lines.append("...[stderr truncated]")
        else:
            lines.append("(empty)")

    lines.append(_BAR)
    # 末尾空行分隔下一条
    lines.append("")
    return "\n".join(lines)


def _append_text(path, text):
    """把文本块追加写入文件（带滚动 + 并发 flock）。"""
    _rotate_if_needed(path)
    try:
        with open(path, "a", encoding="utf-8") as f:
            try:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
    except OSError:
        pass


def log_command(alias, command, result=None, *, mode=None, duration_ms=None,
                error=None, source="ssh_execute"):
    """
    记录一条命令执行日志（追加到该服务器的 .log 文件）。

    参数：
        alias:        服务器别名（决定写入哪个文件）
        command:      实际执行的命令
        result:       执行结果字典，期望含 success / exit_code / stdout / stderr
                      （没有时用于记录未能拿到结果的失败）
        mode:         执行模式标注，如 daemon / direct / native-fallback / cluster / health-check
        duration_ms:  执行耗时（毫秒）
        error:        异常信息字符串（用于记录抛异常的失败）
        source:       调用来源，ssh_execute / ssh_cluster
    """
    if _disabled():
        return
    try:
        entry = _format_entry(alias, command, result, mode, duration_ms, error, source)
        _append_text(get_log_path(alias), entry)
    except Exception:
        # 兜底：任何意外都不抛，绝不影响命令执行
        pass


def log_command_error(alias, command, error, *, mode=None, source="ssh_execute"):
    """便捷方法：记录一条未拿到正常结果的失败命令。"""
    log_command(alias, command, result=None, mode=mode, error=error, source=source)
