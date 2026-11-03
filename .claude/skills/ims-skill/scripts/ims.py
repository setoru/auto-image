#!/usr/bin/env python
"""华为云 IMS skill 入口 —— 制镜像、跨区复制、账号共享与查询。

官方 huaweicloudsdkims.v2 SDK。纯 JSON 输出（stdout 只出 JSON），
--dry-run 杠杆，logs/ 归档。
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any

from huaweicloudsdkims.v2 import ListImagesRequest, ShowJobProgressRequest

from ims_client import DEFAULT_SCOPE_PATH, build_client, load_scope_config, resolve_credentials
from ims_ops import build_copy_cross_region_request, build_create_request, build_share_request

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
DEFAULT_POLL_TIMEOUT = 1800  # 30 分钟——镜像创建慢于 ECS（40GB 盘约 2-5 分钟）
DEFAULT_POLL_INTERVAL = 10   # 秒
DEFAULT_COPY_TIMEOUT = 3600
DEFAULT_COPY_POLL_INTERVAL = 30
CROSS_REGION_COPY_JOB = "imsCrossRegionCopyImageJob"
ADD_IMAGE_MEMBERS_JOB = "imsAddImageMembersJob"
COPY_COST_NOTICE = "跨区域复制按 OBS 跨区域复制功能收取流量费。"
IMS_ERROR_HINTS = {
    "IMG.0149": "已达到跨区复制并发限制，请等待已有任务完成后再处理。",
    "IMG.0151": "检查委托是否具备源区域与目的区域 IAM 项目的 IMS 跨区复制权限。",
}


# ----------------------------------------------------------------------------
# 模型序列化（--dry-run 用：递归把 typed model 转成可 JSON 的 dict）
# ----------------------------------------------------------------------------
def _jsonable(o: Any) -> Any:
    if hasattr(o, "to_dict"):
        return _jsonable(o.to_dict())
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items() if v is not None}
    if isinstance(o, list):
        return [_jsonable(x) for x in o]
    return o


# ----------------------------------------------------------------------------
# 输出 / 日志
# ----------------------------------------------------------------------------
def emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def new_log_path(name: str) -> Path:
    """本次运行的日志路径：一次运行一个文件。"""
    return LOGS_DIR / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.json"


def write_log(path: Path, payload: dict[str, Any]) -> str:
    """归档日志到文件。"""
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_jsonable(payload), fh, ensure_ascii=False, indent=2)
        return str(path)
    except OSError:
        return ""


# ----------------------------------------------------------------------------
# IMS 操作（传输层）
# ----------------------------------------------------------------------------
def show_job(client, *, job_id: str) -> Any:
    """查 job 状态。"""
    return client.show_job_progress(ShowJobProgressRequest(job_id=job_id))


def list_images(client, *, image_id: str | None = None,
                name: str | None = None) -> Any:
    """查镜像列表（可按 id 或 name 过滤）。"""
    kwargs: dict[str, Any] = {}
    if image_id:
        kwargs["id"] = image_id
    if name:
        kwargs["name"] = name
    return client.list_images(ListImagesRequest(**kwargs))


def poll_job(client, *, job_id: str, timeout: int, interval: int,
             trace: list[dict[str, Any]]) -> tuple[str, str | None, Any]:
    """轮询 show_job 直到 SUCCESS/FAIL 或超时。

    - INIT / RUNNING → 向 stderr 报 process_percent，继续轮询。
    - SUCCESS → 从 entities.image_id 取镜像 ID，返回。
    - FAIL    → 返回 None image_id + job_resp（含 fail_reason）。
    - 超时    → 返回 ("TIMEOUT", None, last_resp)。

    返回 (status, image_id, job_resp)。
    """
    deadline = time.monotonic() + timeout
    last_resp = None
    while time.monotonic() < deadline:
        resp = show_job(client, job_id=job_id)
        last_resp = resp
        status = str(getattr(resp, "status", "UNKNOWN") or "UNKNOWN").upper()
        entities = getattr(resp, "entities", None)
        image_id = getattr(entities, "image_id", None) if entities else None
        process_percent = getattr(entities, "process_percent", None) if entities else None
        trace.append({
            "t": time.strftime("%H:%M:%S"),
            "status": status,
            "process_percent": process_percent,
        })
        if status == "SUCCESS":
            return status, image_id, resp
        if status == "FAIL":
            return status, None, resp
        # INIT / RUNNING → 报进度到 stderr（stdout 仍保持纯 JSON）
        pct_str = f"{process_percent}%" if process_percent is not None else "?%"
        print(f"[ims] job={job_id} status={status} {pct_str}",
              file=sys.stderr, flush=True)
        time.sleep(interval)
    return "TIMEOUT", None, last_resp


# ----------------------------------------------------------------------------
# 命令
# ----------------------------------------------------------------------------
def cmd_create(args: argparse.Namespace) -> int:
    scope = load_scope_config(Path(args.scope))
    creds = resolve_credentials(scope)
    req = build_create_request(scope, args)
    image_name = req.body.name
    instance_id = req.body.instance_id

    # --dry-run：仅打印解析后的请求，不调 API（确认杠杆）
    if args.dry_run:
        emit({
            "ok": True, "action": "create", "dry_run": True, "region": creds.region,
            "request": _jsonable(req),
        })
        return 0

    client = build_client(creds)
    log_file = new_log_path(image_name)

    # 调 create_image → 拿 job_id
    try:
        resp = client.create_image(req)
    except Exception as exc:
        result: dict[str, Any] = {
            "ok": False, "action": "create", "status": "FAIL",
            "image_name": image_name, "instance_id": instance_id,
            "job_id": "", "region": creds.region,
            "error": f"create_image 调用失败：{exc}",
            "hint": "检查 instance_id 是否有效、配额是否充足后重试。",
        }
        result["log"] = write_log(log_file, {
            "action": "create", "image_name": image_name, "instance_id": instance_id,
            "region": creds.region, "request": _jsonable(req), "error": str(exc),
        })
        emit(result)
        return 1

    job_id = getattr(resp, "job_id", "") or ""
    trace: list[dict[str, Any]] = []
    log_payload: dict[str, Any] = {
        "action": "create", "image_name": image_name, "instance_id": instance_id,
        "region": creds.region, "request": _jsonable(req),
        "create_response": _jsonable(resp), "poll_trace": trace,
    }

    if not job_id:
        log_path = write_log(log_file, {**log_payload, "final": {
            "ok": False, "error": "未返回 job_id",
        }})
        emit({"ok": False, "action": "create", "status": "FAIL",
              "image_name": image_name, "instance_id": instance_id,
              "job_id": "", "region": creds.region,
              "error": "create_image 未返回 job_id",
              "hint": "检查 instance_id 是否有效、配额是否充足后重试。",
              "log": log_path})
        return 1

    # job_id 先落盘 + stderr 报告：此后若进程被中断，show --job-id 可恢复。
    # stdout 保持纯 JSON——提示只走 stderr。
    write_log(log_file, {**log_payload, "final": {
        "ok": False, "job_id": job_id, "status": "SUBMITTED",
        "note": "已提交但轮询未完成；若进程在此中断，用 `show --job-id` 复查。",
    }})
    print(f"[ims] submitted job_id={job_id} image={image_name}"
          f"（轮询中；中断请用 show --job-id 复查）",
          file=sys.stderr, flush=True)

    # 轮询 job → SUCCESS 时取 image_id
    status, image_id, job_resp = poll_job(
        client, job_id=job_id, timeout=args.timeout,
        interval=args.poll_interval, trace=trace,
    )

    # 超时时 job 实际仍在 RUNNING——status 输出 RUNNING（spec 契约），超时信息放 error。
    out_status = "RUNNING" if status == "TIMEOUT" else status
    result = {
        "ok": status == "SUCCESS", "action": "create",
        "image_name": image_name, "image_id": image_id or "",
        "instance_id": instance_id, "status": out_status,
        "job_id": job_id, "region": creds.region,
    }

    if status == "TIMEOUT":
        result["error"] = f"轮询超时（{args.timeout}s）：job 仍在运行"
        result["hint"] = f"用 `ims.py show --job-id {job_id}` 复查。"
    elif status == "FAIL":
        fail_reason = getattr(job_resp, "fail_reason", None) or ""
        error_code = getattr(job_resp, "error_code", None) or ""
        detail = f"：{fail_reason}" if fail_reason else ""
        result["error"] = f"job 失败{detail}"
        if error_code:
            result["error_code"] = error_code
        result["hint"] = f"用 `ims.py show --job-id {job_id}` 查看详情。"

    log_payload["final"] = result
    result["log"] = write_log(log_file, log_payload)
    emit(result)
    return 0 if result["ok"] else 1


def _job_query_command(job_id: str, *, region: str | None = None,
                       scope: str | None = None) -> str:
    command = "ims.py"
    if scope:
        command += " --scope " + shlex.quote(scope)
    command += " show --job-id " + shlex.quote(job_id)
    if region:
        command += " --region " + shlex.quote(region)
    return command


def _run_job_command(client, *, args: argparse.Namespace, request: Any,
                     operation: str, result: dict[str, Any], log_name: str,
                     image_id_field: str | None = None) -> int:
    """复制与共享复用提交、身份归档和轮询；共享成功无需新镜像 ID。"""
    job_region = result.get("source_region") or result["region"]
    log_file = new_log_path(log_name)
    trace: list[dict[str, Any]] = []
    log_payload = {**result, "request": _jsonable(request), "poll_trace": trace}
    result.update(ok=False, job_id="", status="SUBMITTED")

    try:
        response = getattr(client, operation)(request)
    except Exception as exc:
        result.update(status="FAIL", error=f"{operation} 调用失败：{exc}")
        error_code = getattr(exc, "error_code", None) or ""
        if error_code:
            result["error_code"] = error_code
        result["hint"] = IMS_ERROR_HINTS.get(error_code, "检查请求参数和 IMS 权限。")
    else:
        log_payload["submit_response"] = _jsonable(response)
        job_id = getattr(response, "job_id", "") or ""
        result["job_id"] = job_id
        if not job_id:
            result.update(status="FAIL", error=f"{operation} 未返回 job_id")
        else:
            query = _job_query_command(job_id, region=job_region, scope=args.scope)
            result["hint"] = f"用 `{query}` 复查。"
            write_log(log_file, {**log_payload, "final": dict(result)})
            print(f"[ims] submitted job_id={job_id}（中断后用 {query} 复查）",
                  file=sys.stderr, flush=True)
            try:
                status, image_id, job_response = poll_job(
                    client, job_id=job_id, timeout=args.timeout,
                    interval=args.poll_interval, trace=trace,
                )
            except Exception as exc:
                result["status"] = trace[-1]["status"] if trace else "SUBMITTED"
                result["error"] = f"show_job_progress 调用失败：{exc}"
                error_code = getattr(exc, "error_code", None) or ""
                if error_code:
                    result["error_code"] = error_code
            else:
                if status == "TIMEOUT":
                    result["status"] = trace[-1]["status"] if trace else "SUBMITTED"
                    result["error"] = f"轮询超时（{args.timeout}s）：云任务未确认完成"
                else:
                    result["status"] = status
                    result["ok"] = status == "SUCCESS"
                    if status == "FAIL":
                        fail_reason = getattr(job_response, "fail_reason", None) or ""
                        error_code = getattr(job_response, "error_code", None) or ""
                        result.update(error=f"job 失败：{fail_reason}", fail_reason=fail_reason)
                        if error_code:
                            result["error_code"] = error_code
                        if error_code in IMS_ERROR_HINTS:
                            result["hint"] = IMS_ERROR_HINTS[error_code] + f" 用 `{query}` 复查。"
                    elif image_id_field:
                        result[image_id_field] = image_id or ""
                        if not image_id:
                            result.update(ok=False, error="job 已成功但未返回 image_id")
                    if result["ok"]:
                        result.pop("hint", None)

    log_payload["final"] = dict(result)
    result["log"] = write_log(log_file, log_payload)
    emit(result)
    return 0 if result["ok"] else 1


def cmd_copy_cross_region(args: argparse.Namespace) -> int:
    scope = load_scope_config(Path(args.scope))
    creds = resolve_credentials(scope)
    request = build_copy_cross_region_request(scope, args)
    result = {
        "action": "copy-cross-region", "source_region": creds.region,
        "source_image_id": request.image_id, "destination_region": request.body.region,
        "destination_image_id": "", "image_name": request.body.name or "",
    }
    if args.dry_run:
        if not request.body.name:
            raise ValueError("不触网预览需 --source-image-name；先用 show --id 查询源镜像名。")
        emit({**result, "ok": True, "dry_run": True,
              "request": _jsonable(request), "cost_notice": COPY_COST_NOTICE})
        return 0
    client = build_client(creds)
    try:
        response = list_images(client, image_id=request.image_id)
        images = getattr(response, "images", None) or []
        if not images:
            raise ValueError("未找到源镜像")
        source_name = getattr(images[0], "name", None)
        if not source_name:
            raise ValueError("源镜像未返回名称")
    except Exception as exc:
        result.update(ok=False, status="FAIL", job_id="", error=f"查询源镜像名称失败：{exc}")
        error_code = getattr(exc, "error_code", None)
        if error_code:
            result["error_code"] = error_code
        result["log"] = write_log(
            new_log_path(f"copy-{request.image_id}-{request.body.region}"), dict(result),
        )
        emit(result)
        return 1
    request.body.name = source_name
    result["image_name"] = source_name
    print(f"[ims] {COPY_COST_NOTICE}", file=sys.stderr, flush=True)
    return _run_job_command(
        client, args=args, request=request,
        operation="copy_image_cross_region", result=result,
        log_name=f"copy-{request.image_id}-{request.body.region}", image_id_field="destination_image_id",
    )


def cmd_share(args: argparse.Namespace) -> int:
    scope = load_scope_config(Path(args.scope))
    creds = resolve_credentials(scope)
    request = build_share_request(args)
    region = (args.region if args.region is not None else creds.region).strip()
    if not region:
        raise ValueError("region 不能为空。")
    result = {
        "action": "share", "region": region,
        "image_id": request.body.images[0], "account_id": request.body.domains[0],
    }
    if args.dry_run:
        emit({**result, "ok": True, "dry_run": True, "request": _jsonable(request)})
        return 0
    return _run_job_command(
        build_client(creds, region=region), args=args, request=request,
        operation="batch_add_members", result=result, log_name=f"share-{result['image_id']}",
    )


# ----------------------------------------------------------------------------
# show 命令
# ----------------------------------------------------------------------------
def _summarize_image(img: Any) -> dict[str, Any]:
    """镜像对象 → 精简 dict（输出契约：id/name/status/size/os_type/disk_format/min_disk）。"""
    return {
        "id": getattr(img, "id", "") or "",
        "name": getattr(img, "name", "") or "",
        "status": getattr(img, "status", "") or "",
        "size": getattr(img, "size", 0) or 0,
        "os_type": getattr(img, "os_type", "") or "",
        "disk_format": getattr(img, "disk_format", "") or "",
        "min_disk": getattr(img, "min_disk", 0) or 0,
    }


def cmd_show(args: argparse.Namespace) -> int:
    """查询镜像 / job —— 三条互斥路径（--id / --name / --job-id）。

    输出恒纯 JSON（stdout）：
    - --id：list_images(id=...) → 镜像详情或「未找到」。
    - --name：list_images(name=...) → 匹配镜像列表或「未找到」。
    - --job-id：查询任务；create 成功返回镜像详情，copy 成功返回目的镜像 ID，
      share 成功返回任务状态；RUNNING 返回进度，FAIL 返回失败原因。
    """
    scope = load_scope_config(Path(args.scope))
    creds = resolve_credentials(scope)
    client = build_client(creds, region=args.region)

    if args.id:
        return _show_by_id(client, image_id=args.id)
    if args.name:
        return _show_by_name(client, name=args.name)
    if args.job_id:
        return _show_by_job_id(client, job_id=args.job_id,
                               region=args.region or creds.region, scope=args.scope)
    # argparse mutually exclusive group required=True 保证不会到这里
    emit({"ok": False, "action": "show", "error": "需要 --id、--name 或 --job-id 之一"})
    return 1


def _show_by_id(client, *, image_id: str) -> int:
    try:
        resp = list_images(client, image_id=image_id)
    except Exception as exc:
        emit({"ok": False, "action": "show", "id": image_id,
              "error": f"list_images 调用失败：{exc}"})
        return 1
    images = getattr(resp, "images", None) or []
    if not images:
        emit({"ok": False, "action": "show", "id": image_id,
              "error": "未找到该镜像"})
        return 1
    emit({"ok": True, "action": "show", "image": _summarize_image(images[0])})
    return 0


def _show_by_name(client, *, name: str) -> int:
    try:
        resp = list_images(client, name=name)
    except Exception as exc:
        emit({"ok": False, "action": "show", "name": name,
              "error": f"list_images 调用失败：{exc}"})
        return 1
    images = getattr(resp, "images", None) or []
    if not images:
        emit({"ok": False, "action": "show", "name": name,
              "error": "未找到该镜像"})
        return 1
    emit({"ok": True, "action": "show",
          "images": [_summarize_image(img) for img in images]})
    return 0


def _show_by_job_id(client, *, job_id: str, region: str | None = None,
                    scope: str | None = None) -> int:
    try:
        job_resp = show_job(client, job_id=job_id)
    except Exception as exc:
        emit({"ok": False, "action": "show", "job_id": job_id,
              "error": f"show_job_progress 调用失败：{exc}"})
        return 1

    status = str(getattr(job_resp, "status", "UNKNOWN") or "UNKNOWN").upper()
    entities = getattr(job_resp, "entities", None)
    image_id = getattr(entities, "image_id", None) if entities else None
    process_percent = getattr(entities, "process_percent", None) if entities else None
    job_type = getattr(job_resp, "job_type", None)

    if status == "SUCCESS":
        if job_type == ADD_IMAGE_MEMBERS_JOB:
            emit({"ok": True, "action": "show", "job_id": job_id,
                  "status": "SUCCESS", "job_type": job_type})
            return 0
        if not image_id:
            emit({"ok": False, "action": "show", "job_id": job_id,
                  "status": "SUCCESS", "error": "job 已成功但未返回 image_id"})
            return 1
        if job_type == CROSS_REGION_COPY_JOB:
            emit({"ok": True, "action": "show", "job_id": job_id,
                  "status": "SUCCESS", "job_type": job_type,
                  "destination_image_id": image_id})
            return 0
        try:
            resp = list_images(client, image_id=image_id)
        except Exception as exc:
            emit({"ok": False, "action": "show", "job_id": job_id,
                  "status": "SUCCESS",
                  "error": f"list_images 调用失败：{exc}"})
            return 1
        images = getattr(resp, "images", None) or []
        if not images:
            emit({"ok": False, "action": "show", "job_id": job_id,
                  "status": "SUCCESS", "error": "未找到该镜像"})
            return 1
        emit({"ok": True, "action": "show", "job_id": job_id,
              "status": "SUCCESS", "image": _summarize_image(images[0])})
        return 0

    if status in ("RUNNING", "INIT"):
        emit({"ok": False, "action": "show", "job_id": job_id,
              "status": "RUNNING", "process_percent": process_percent,
              "hint": "任务仍在运行，请稍后用 `"
                      + _job_query_command(job_id, region=region, scope=scope) + "` 复查。"})
        return 1

    if status == "FAIL":
        fail_reason = getattr(job_resp, "fail_reason", None) or ""
        result: dict[str, Any] = {
            "ok": False, "action": "show", "job_id": job_id,
            "status": "FAIL", "fail_reason": fail_reason,
        }
        error_code = getattr(job_resp, "error_code", None) or ""
        if error_code:
            result["error_code"] = error_code
        if error_code in IMS_ERROR_HINTS:
            result["hint"] = IMS_ERROR_HINTS[error_code]
        emit(result)
        return 1

    # 未知状态
    emit({"ok": False, "action": "show", "job_id": job_id,
          "status": status, "error": f"未知 job 状态：{status}"})
    return 1


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="华为云 IMS skill：制镜像、跨区复制、账号共享与查询。纯 JSON 输出。",
    )
    parser.add_argument("--scope", default=str(DEFAULT_SCOPE_PATH), help="scope.yaml 路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="把一台 ECS 制作为系统盘镜像")
    p_create.add_argument("--instance-id", dest="instance_id", required=True,
                          help="源 ECS 实例 ID（必填）")
    p_create.add_argument("--image-name", dest="image_name", help="镜像名称；不给则自动 img-<rand>")
    p_create.add_argument("--description", help="镜像描述（可选）")
    p_create.add_argument("--dry-run", dest="dry_run", action="store_true",
                          help="仅打印解析后的请求，不调 API")
    p_create.add_argument("--timeout", type=int, default=DEFAULT_POLL_TIMEOUT,
                          help=f"轮询 job 超时秒（默认 {DEFAULT_POLL_TIMEOUT}）")
    p_create.add_argument("--poll-interval", dest="poll_interval", type=int,
                          default=DEFAULT_POLL_INTERVAL,
                          help=f"轮询间隔秒（默认 {DEFAULT_POLL_INTERVAL}）")
    p_create.set_defaults(func=cmd_create)

    p_copy = sub.add_parser("copy-cross-region", help="把私有系统盘镜像复制到另一区域")
    p_copy.add_argument("--id", required=True, help="源镜像 ID")
    p_copy.add_argument("--region", required=True, help="目的区域 ID；源区域由凭证配置提供")
    p_copy.add_argument("--project-name", help="目的 IAM 项目名；默认取目的区域 ID")
    p_copy.add_argument("--agency-name", help="IMS 委托名；默认取 scope.ims_copy.agency_name")
    p_copy.add_argument("--source-image-name", help="不触网预览时提供的源镜像名；执行时自动查询并沿用源名")
    p_copy.add_argument("--description", help="镜像描述（可选）")
    p_copy.add_argument("--dry-run", action="store_true", help="仅打印请求与费用提示，不调 API")
    p_copy.add_argument("--timeout", type=int, default=DEFAULT_COPY_TIMEOUT,
                        help=f"等待 job 的秒数（默认 {DEFAULT_COPY_TIMEOUT}），超时后可继续查询")
    p_copy.add_argument("--poll-interval", type=int, default=DEFAULT_COPY_POLL_INTERVAL,
                        help=f"轮询间隔秒（默认 {DEFAULT_COPY_POLL_INTERVAL}）")
    p_copy.set_defaults(func=cmd_copy_cross_region)

    p_share = sub.add_parser("share", help="把私有镜像共享给同区域的指定账号")
    p_share.add_argument("--id", required=True, help="镜像 ID")
    p_share.add_argument("--account-id", required=True, help="接收账号 ID（domains 维度）")
    p_share.add_argument("--region", help="镜像所在区域；默认取凭证配置")
    p_share.add_argument("--dry-run", action="store_true", help="仅打印请求，不调 API")
    p_share.add_argument("--timeout", type=int, default=DEFAULT_POLL_TIMEOUT,
                         help=f"等待 job 的秒数（默认 {DEFAULT_POLL_TIMEOUT}）")
    p_share.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL,
                         help=f"轮询间隔秒（默认 {DEFAULT_POLL_INTERVAL}）")
    p_share.set_defaults(func=cmd_share)

    p_show = sub.add_parser("show", help="查询镜像（按 ID / 名称 / job_id）")
    target = p_show.add_mutually_exclusive_group(required=True)
    target.add_argument("--id", help="镜像 ID")
    target.add_argument("--name", help="镜像名称（精确匹配）")
    target.add_argument("--job-id", dest="job_id", help="制镜像、复制或共享任务 job_id")
    p_show.add_argument("--region", help="查询所在区域；查 job 时用提交区域，查镜像时用镜像区域")
    p_show.set_defaults(func=cmd_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "error": "cancelled"}, ensure_ascii=False), file=sys.stderr)
        return 130
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
