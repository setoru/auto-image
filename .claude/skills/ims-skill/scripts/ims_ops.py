#!/usr/bin/env python
"""ims_ops —— 华为云 IMS 请求构造（官方 huaweicloudsdkims.v2 SDK）。

只含纯逻辑：把 scope dict + CLI 映射成 typed CreateImageRequest。
传输层（ImsClient 调用、轮询 job）在 ims.py 编排。
"""
from __future__ import annotations

import uuid
from argparse import Namespace
from typing import Any

from huaweicloudsdkims.v2 import CreateImageRequest, CreateImageRequestBody

# create_image 制系统盘镜像——type 固定为 ECS（系统盘镜像）。
# 不做整机镜像（WholeImage，需 vault_id）；不做数据盘镜像（DataImage）。
IMAGE_TYPE_ECS = "ECS"


def _ims_create_spec(scope: dict[str, Any]) -> dict[str, Any]:
    """取 scope 的 ims_create 段（可空——不做企业项目管理时整段可省略）。"""
    ims_create = scope.get("ims_create") or {}
    if not isinstance(ims_create, dict):
        raise ValueError("scope 的 ims_create 必须是对象。")
    return ims_create


def build_create_request(scope: dict[str, Any], args: Namespace) -> CreateImageRequest:
    """合并 scope.ims_create + CLI 覆盖，返回 typed CreateImageRequest。

    - instance_id 必填（CLI --instance-id 提供，无 scope 兜底）。
    - name：CLI --image-name 覆盖；不给时自动 img-<8hex>（12 字符）。
    - description：CLI --description 可选；不给时 None（不下发）。
    - enterprise_project_id：从 scope ims_create 段取；scope 无则不下发。
    """
    instance_id = (getattr(args, "instance_id", None) or "").strip()
    if not instance_id:
        raise ValueError(
            "缺少必填字段：instance_id（请用 --instance-id 指定源 ECS ID）。"
        )

    name = getattr(args, "image_name", None) or ("img-" + uuid.uuid4().hex[:8])
    description = getattr(args, "description", None) or None

    ims_cfg = _ims_create_spec(scope)
    enterprise_project_id = (str(ims_cfg.get("enterprise_project_id") or "")).strip() or None

    body = CreateImageRequestBody(
        instance_id=instance_id,
        name=name,
        description=description,
        enterprise_project_id=enterprise_project_id,
        type=IMAGE_TYPE_ECS,
    )
    return CreateImageRequest(body=body)
