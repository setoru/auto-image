"""产物发现与查看：目录发现（install-meta.json 驱动）、按阶段解锁的清单、内容读取。

产物目录的发现不靠猜：INSTALL 阶段开始后才扫描 artifact_root 下的
`*-install-meta.json`（mtime 晚于本次 run 创建的取最新），meta.json 所在
目录即 output_dir——meta.json 内并无目录字段（其 `path` 字段是 ECS 创建
路径 create/existing），目录由 deploy.config.yaml 的「meta 文件位于
output_dir 下」约定决定。此前清单为空，GUIDE 产物在目录被发现后一并可见。

阶段解锁：产物文件名从 deploy.config.yaml 派生（{{software}} 占位剥去得
后缀），文件归入所属阶段，清单只含 run 已进入阶段的文件——同软件同版本
重跑时旧一轮的后续阶段产物仍在目录里，但本次 run 未到达，不展示；匹配
不上约定后缀的文件（.v1 重跑备份、人工杂项）不展示；verify 未通过时
ARCHIVE 产物自然缺席（门禁在 skill 层，此处如实呈现）。

路径约束（内容端点）：名字必须出现在清单中，且拼接 resolve 后仍位于
output_dir 内；两者任一不满足按 404 处理——服务不是任意文件读取器。
"""
import os
from pathlib import Path

import yaml

# 阶段推进顺序（解锁判定：文件所属阶段须已被 run 进入）
STAGE_ORDER = {"GUIDE": 0, "INSTALL": 1, "VERIFY": 2, "ARCHIVE": 3}

# deploy.config.yaml 的产物文件键 → 所属阶段（config 是文件名的唯一权威源，
# 改名 / 加键只动 config：改名自动跟随，新键不在此映射即不列出）
CONFIG_KEY_STAGES = {
    "install_file": "GUIDE",
    "verify_file": "GUIDE",
    "install_result_file": "INSTALL",
    "install_issues_file": "INSTALL",
    "install_meta_file": "INSTALL",
    "verify_result_file": "VERIFY",
    "verify_issues_file": "VERIFY",
    "archive_result_file": "ARCHIVE",
    "deploy_list_file": "ARCHIVE",
    "archive_issues_file": "ARCHIVE",
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
    suffixes.append(("-pipeline-result.md", "ARCHIVE"))
    return tuple(suffixes)


def stage_for_file(name, file_stages):
    """产物文件名 → 所属阶段；非约定产物名返回 None（兜底不展示）。"""
    for suffix, stage in file_stages:
        if name.endswith(suffix):
            return stage
    return None


def discover_output_dir(artifact_root, started_after):
    """扫描全部 *-install-meta.json，取 mtime 晚于 started_after 的最新一个，
    返回其所在目录；无命中返回 None（旧一轮的 meta 不算本次 run 的产物目录）。"""
    latest = None
    for meta in Path(artifact_root).glob("**/*-install-meta.json"):
        mtime = os.stat(meta).st_mtime
        if mtime > started_after and (latest is None or mtime > latest[0]):
            latest = (mtime, meta.parent)
    return latest[1] if latest else None


def snapshot(run, artifact_root, file_stages):
    """产物清单：发现（惰性，一次 run 只发现一次）+ 按当前阶段解锁过滤。"""
    # INSTALL 开始前不扫描：GUIDE 阶段的目录归属尚不可判定；扫描而无命中同样空清单
    if run.output_dir is None and STAGE_ORDER.get(run.stage, -1) >= STAGE_ORDER["INSTALL"]:
        run.output_dir = discover_output_dir(artifact_root, run.created_at)
    if run.output_dir is None:
        return {"output_dir": None, "files": []}
    return {"output_dir": str(run.output_dir), "files": _unlocked_files(run.output_dir, run.stage, file_stages)}


def find(run, artifact_root, file_stages, name):
    """内容端点的读取入口：清单命中后拼接并校验仍在产物目录内，
    返回 (条目, 路径)；未命中或越界返回 None（调用方按 404 处理）。"""
    snap = snapshot(run, artifact_root, file_stages)  # 与清单同一发现与解锁路径，无第二套判定
    entry = next((f for f in snap["files"] if f["name"] == name), None)
    if entry is None:
        return None
    target = (run.output_dir / name).resolve()
    if not target.is_relative_to(run.output_dir.resolve()):
        return None
    return entry, target


def _unlocked_files(output_dir, stage, file_stages):
    unlocked = STAGE_ORDER.get(stage, -1)
    files = []
    try:
        paths = sorted(os.listdir(output_dir))
    except OSError:
        return files  # 目录被移动/删除：清单如实为空
    for name in paths:
        file_stage = stage_for_file(name, file_stages)
        if file_stage is None or STAGE_ORDER[file_stage] > unlocked:
            continue
        try:
            size = os.stat(output_dir / name).st_size
        except OSError:
            continue  # 竞态消失的文件跳过
        files.append({"name": name, "stage": file_stage, "size": size})
    return files
