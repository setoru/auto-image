#!/usr/bin/env python
"""ims_client —— 华为云 IMS 客户端构造 + scope/凭证解析。

凭证优先级：HUAWEICLOUD_SDK_* 环境变量 > scope.yaml。
与 ecs_client 同模式，但不跨 skill import——独立维护。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from huaweicloudsdkcore.auth.credentials import BasicCredentials
from huaweicloudsdkcore.client import ClientBuilder
from huaweicloudsdkcore.region.region import Region
from huaweicloudsdkims.v2 import ImsClient

IMS_ENDPOINT_TEMPLATE = "ims.{region}.myhuaweicloud.com"
SCRIPT_DIR = Path(__file__).resolve().parent
# 仓库根 = .claude/skills/ims-skill/scripts/ 起 4 级上 —— 共享 scope.yaml 所在。
REPO_ROOT = SCRIPT_DIR.parents[3]
DEFAULT_SCOPE_PATH = REPO_ROOT / "scope.yaml"

# SDK 原生环境变量名（env 最高优先级）
ENV_AK = "HUAWEICLOUD_SDK_AK"
ENV_SK = "HUAWEICLOUD_SDK_SK"
ENV_REGION = "HUAWEICLOUD_SDK_REGION"
ENV_PROJECT_ID = "HUAWEICLOUD_SDK_PROJECT_ID"


@dataclass
class Credentials:
    """华为云凭证（project_id 可空——缺省时 SDK 按 region 自动推导）。"""

    ak: str
    sk: str
    region: str
    project_id: str


def load_scope_config(scope_path: Path) -> dict[str, Any]:
    with open(scope_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    if not isinstance(cfg, dict):
        raise ValueError("scope 文件顶层必须是对象。")
    if isinstance(cfg.get("default"), dict):  # 兼容 {default: {...}} 包裹
        cfg = cfg["default"]
    return cfg


def resolve_credentials(scope: dict[str, Any]) -> Credentials:
    """优先级：HUAWEICLOUD_SDK_* 环境变量 > scope。

    project_id 可选：缺省时交给 SDK 按 region 自动推导。
    """
    ak = (os.getenv(ENV_AK) or str(scope.get("ak", ""))).strip()
    sk = (os.getenv(ENV_SK) or str(scope.get("sk", ""))).strip()
    region = (os.getenv(ENV_REGION) or str(scope.get("region", ""))).strip()
    project_id = (os.getenv(ENV_PROJECT_ID) or str(scope.get("project_id", ""))).strip()
    missing = [k for k, v in (("ak", ak), ("sk", sk), ("region", region)) if not v]
    if missing:
        raise ValueError(
            "缺少凭证/区域：" + ", ".join(missing)
            + "。请在 scope.yaml 提供，或设置环境变量 "
            "HUAWEICLOUD_SDK_AK / _SK / _REGION（env 优先）。"
        )
    return Credentials(ak=ak, sk=sk, region=region, project_id=project_id)


def build_client(creds: Credentials) -> ImsClient:
    """官方 ImsClient（系统盘镜像 API）。"""
    endpoint = f"https://{IMS_ENDPOINT_TEMPLATE.replace('{region}', creds.region)}"
    credentials = BasicCredentials(creds.ak, creds.sk, creds.project_id) if creds.project_id else BasicCredentials(creds.ak, creds.sk)
    return (
        ClientBuilder(ImsClient)
        .with_credentials(credentials)
        .with_region(Region(id=creds.region, endpoint=endpoint))
        .build()
    )
