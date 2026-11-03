#!/usr/bin/env python
"""ims_ops —— 华为云 IMS 请求构造（官方 huaweicloudsdkims.v2 SDK）。

只含纯逻辑：把 scope dict + CLI 映射成 SDK 请求。
传输层（ImsClient 调用、轮询 job）在 ims.py 编排。
"""
from __future__ import annotations

import uuid
from argparse import Namespace
from typing import Any

from huaweicloudsdkims.v2 import (
    BatchAddMembersRequest,
    BatchAddMembersRequestBody,
    CopyImageCrossRegionRequest,
    CopyImageCrossRegionRequestBody,
    CreateImageRequest,
    CreateImageRequestBody,
)

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


def build_copy_cross_region_request(scope: dict[str, Any],
                                    args: Namespace) -> CopyImageCrossRegionRequest:
    """构造跨区复制请求；执行时从源镜像读取名称，预览由调用方提供源名。"""
    image_id = (args.id or "").strip()
    region = (args.region or "").strip()
    ims_copy = scope.get("ims_copy") or {}
    if not isinstance(ims_copy, dict):
        raise ValueError("scope 的 ims_copy 必须是对象。")
    agency_name = (args.agency_name or str(ims_copy.get("agency_name") or "")).strip()
    project_name = (args.project_name if args.project_name is not None else region).strip()
    missing = [name for name, value in (
        ("id", image_id), ("region", region),
        ("project_name", project_name), ("agency_name", agency_name),
    ) if not value]
    if missing:
        raise ValueError("缺少必填字段：" + ", ".join(missing)
                         + "。委托可通过 --agency-name 或 scope.ims_copy.agency_name 提供。")
    return CopyImageCrossRegionRequest(
        image_id=image_id,
        body=CopyImageCrossRegionRequestBody(
            name=args.source_image_name or None,
            description=args.description or None,
            region=region,
            project_name=project_name,
            agency_name=agency_name,
        ),
    )


def build_share_request(args: Namespace) -> BatchAddMembersRequest:
    """把一个私有镜像共享给指定账号（domains 维度）。"""
    image_id = (args.id or "").strip()
    account_id = (args.account_id or "").strip()
    if not image_id or not account_id:
        raise ValueError("缺少必填字段：id / account_id（用 --id / --account-id 提供）。")
    return BatchAddMembersRequest(body=BatchAddMembersRequestBody(
        images=[image_id], domains=[account_id],
    ))
