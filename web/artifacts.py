"""deploy/ 全量产物浏览：目录分组清单、内容读取与路径约束。

产物由 deploy 流水线按 deploy.config.yaml 约定落盘在
deploy/<software>/<version>/ 下；本模块只读浏览全部产物（含历史轮次、
重跑备份与杂项文件），不做任何按会话/阶段的过滤——侧栏产物区即
deploy/ 的镜像，约定命名的文件带阶段徽标（文件名从 config 派生，
{{software}} 占位剥去得后缀），非约定的（.v1 备份、人工杂项）无徽标
平铺。

路径约束（内容端点）：相对路径拼接 resolve 后必须仍位于产物根内且
是普通文件，否则按 404 处理——服务不是任意文件读取器。
"""
import os
from pathlib import Path

import yaml

from .normalize import ARCHIVE, GUIDE, INSTALL, VERIFY

# deploy.config.yaml 的产物文件键 → 所属阶段（config 是文件名的唯一权威源，
# 改名 / 加键只动 config：改名自动跟随，新键不在此映射即无徽标）
CONFIG_KEY_STAGES = {
    "install_file": GUIDE,
    "verify_file": GUIDE,
    "install_result_file": INSTALL,
    "install_issues_file": INSTALL,
    "install_meta_file": INSTALL,
    "verify_result_file": VERIFY,
    "verify_issues_file": VERIFY,
    "archive_result_file": ARCHIVE,
    "deploy_list_file": ARCHIVE,
    "archive_issues_file": ARCHIVE,
}


def load_file_stages(config_path):
    """deploy.config.yaml → ((后缀, 阶段), …)：{{software}} 占位剥去得匹配后缀。

    config 是流水线与 Web 的共同契约，读不到或缺键即失败（fail fast），
    不落第二套默认表——同源漂移比产物列不出来更隐蔽。
    """
    data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    suffixes = []
    for key, stage in CONFIG_KEY_STAGES.items():
        pattern = data[key]
        suffixes.append((pattern.replace("{{software}}", ""), stage))
    # 流水线汇总由 deploy skill 编排层直接落盘（不进 deploy.config.yaml）
    suffixes.append(("-pipeline-result.md", ARCHIVE))
    return tuple(suffixes)


def stage_for_file(name, file_stages):
    """产物文件名 → 所属阶段；非约定产物名返回 None（无徽标平铺）。"""
    for suffix, stage in file_stages:
        if name.endswith(suffix):
            return stage
    return None


def browse(artifact_root, file_stages):
    """全树清单：按相对根的父目录分组，组间最新落盘时间降序、组内文件名升序。

    组的排序键取组内文件 mtime 的最大值（逐文件 stat 已为 size 做，零额外
    开销；目录自身 mtime 在原位重写文件时不更新，不可靠）。根下散落的
    文件归 dir 为 "" 的组。stat 失败的文件跳过（竞态消失，如实缺席）。
    """
    groups = {}
    for dirpath, _dirnames, filenames in os.walk(artifact_root):
        rel_dir = Path(dirpath).relative_to(artifact_root).as_posix()
        if rel_dir == ".":
            rel_dir = ""
        for name in filenames:
            try:
                stat = os.stat(Path(dirpath) / name)
            except OSError:
                continue
            stage = stage_for_file(name, file_stages)
            group = groups.setdefault(rel_dir, {"mtime": 0.0, "files": []})
            group["mtime"] = max(group["mtime"], stat.st_mtime)
            group["files"].append({"name": name, "stage": stage, "size": stat.st_size})
    ordered = []
    for rel_dir in sorted(groups, key=lambda d: (-groups[d]["mtime"], d)):
        files = sorted(groups[rel_dir]["files"], key=lambda f: f["name"])
        ordered.append({"dir": rel_dir, "files": files})
    return {"groups": ordered}


def read(artifact_root, file_stages, rel_path):
    """内容端点的读取入口：约束在产物根内的普通文件，返回 (条目, 路径)；
    越界、缺失或非文件返回 None（调用方按 404 处理）。"""
    if not rel_path or Path(rel_path).is_absolute():
        return None  # 绝对路径与空串前置拒绝（Path / "/abs" 会整体替换根）
    root = Path(artifact_root).resolve()
    target = (root / rel_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    entry = {
        "dir": target.parent.relative_to(root).as_posix(),
        "name": target.name,
        "stage": stage_for_file(target.name, file_stages),
    }
    return entry, target
