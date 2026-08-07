#!/usr/bin/env python
"""ims-skill 测试 —— 两条缝。

- 缝 A：请求构造（scope dict + CLI → CreateImageRequest），纯逻辑。
- 缝 B：命令行入口的 dry-run 端到端（argv → stdout JSON），不触网。

纯 assert，无 pytest（契合 ecs-skill / ims-skill 最小依赖调性）。
运行：python .claude/skills/ims-skill/tests/test_ims_ops.py
"""
import contextlib
import io
import json
import os
import pathlib
import socket
import sys
import tempfile
from argparse import Namespace

import yaml
from huaweicloudsdkims.v2 import ImageInfo, JobEntities, ListImagesResponse, ShowJobResponse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import ims  # noqa: E402
from ims_client import DEFAULT_SCOPE_PATH, resolve_credentials  # noqa: E402
from ims_ops import IMAGE_TYPE_ECS, build_create_request  # noqa: E402


# ---- 固件 ----
def make_args(**kw):
    """create 子命令的 CLI namespace 固件，默认只给 instance_id。"""
    base = dict(
        instance_id="i-0001",
        image_name=None,
        description=None,
        dry_run=False,
    )
    base.update(kw)
    return Namespace(**base)


def make_scope(**ims_overrides):
    """最小 scope：顶层占位凭证 + 可选 ims_create 段。"""
    scope = {
        "ak": "AK-PLACEHOLDER", "sk": "SK-PLACEHOLDER",
        "region": "ap-southeast-3",
    }
    if ims_overrides:
        scope["ims_create"] = ims_overrides
    return scope


# ---- 缝 A：请求构造（build_create_request 纯逻辑）----
def test_instance_id_required():
    """不给 instance_id → ValueError 并点名缺失字段。"""
    try:
        build_create_request(make_scope(), make_args(instance_id=""))
        raise AssertionError("缺 instance_id 应抛 ValueError")
    except ValueError as e:
        assert "instance_id" in str(e).lower(), f"报错应提及 instance_id，实得：{e}"


def test_instance_id_carried_to_body():
    """instance_id 从 CLI 接到请求体。"""
    req = build_create_request(make_scope(), make_args(instance_id="abc-123"))
    assert req.body.instance_id == "abc-123", f"instance_id 实得 {req.body.instance_id!r}"


def test_image_name_auto_generated():
    """不给 --image-name → 自动 img-<8hex>（12 字符）。"""
    req = build_create_request(make_scope(), make_args())
    name = req.body.name
    assert name and name.startswith("img-"), f"应自动 img- 前缀，实得 {name!r}"
    assert len(name) == 12, f"应为 img-+8hex=12 字符，实得 {len(name)}"


def test_image_name_cli_overrides():
    """--image-name 显式给出时直接使用，不自动生成。"""
    req = build_create_request(make_scope(), make_args(image_name="my-image"))
    assert req.body.name == "my-image", f"name 实得 {req.body.name!r}"


def test_description_optional():
    """不给 --description → None（不下发）。"""
    req = build_create_request(make_scope(), make_args())
    assert req.body.description is None, f"description 应为 None，实得 {req.body.description!r}"


def test_description_carried_to_body():
    """--description 从 CLI 接到请求体。"""
    req = build_create_request(make_scope(), make_args(description="from ecs i-0001"))
    assert req.body.description == "from ecs i-0001", \
        f"description 实得 {req.body.description!r}"


def test_enterprise_project_id_from_scope():
    """enterprise_project_id 从 scope ims_create 段取。"""
    scope = make_scope(enterprise_project_id="eps-xxx")
    req = build_create_request(scope, make_args())
    assert req.body.enterprise_project_id == "eps-xxx", \
        f"enterprise_project_id 实得 {req.body.enterprise_project_id!r}"


def test_enterprise_project_id_absent_when_scope_lacks_it():
    """scope 无 ims_create 段 → enterprise_project_id 为 None（不下发）。"""
    req = build_create_request(make_scope(), make_args())  # 无 ims_create
    assert req.body.enterprise_project_id is None, \
        f"无 ims_create 时 enterprise_project_id 应为 None，实得 {req.body.enterprise_project_id!r}"


def test_enterprise_project_id_empty_string_means_none():
    """scope ims_create.enterprise_project_id 留空 → None（不下发空串）。"""
    scope = make_scope(enterprise_project_id="")
    req = build_create_request(scope, make_args())
    assert req.body.enterprise_project_id is None, \
        f"空串应视为不下发，实得 {req.body.enterprise_project_id!r}"


def test_image_type_always_ecs():
    """type 固定为 ECS（系统盘镜像），不暴露给 CLI。"""
    req = build_create_request(make_scope(), make_args())
    assert req.body.type == IMAGE_TYPE_ECS, \
        f"type 应为 {IMAGE_TYPE_ECS!r}，实得 {req.body.type!r}"
    assert IMAGE_TYPE_ECS == "ECS"


# ---- 缝 B 固件：dry-run 端到端（argv → stdout JSON，全程不触网）----
# 占位凭证：--dry-run 在构造客户端之前就返回，凭证只需通过必填校验，不会被使用。
PLACEHOLDER_CREDENTIALS = {
    "ak": "AK-PLACEHOLDER", "sk": "SK-PLACEHOLDER",
    "region": "ap-southeast-3", "project_id": "PROJECT-PLACEHOLDER",
}


def make_cli_scope(**ims_overrides):
    """缝 B 的 scope：占位凭证 + 可选 ims_create 段。"""
    scope = dict(PLACEHOLDER_CREDENTIALS)
    if ims_overrides:
        scope["ims_create"] = ims_overrides
    return scope


@contextlib.contextmanager
def _isolate_env(*prefixes):
    """临时屏蔽以 prefixes 开头的环境变量，结束后恢复。"""
    saved = {}
    for k in list(os.environ):
        for pref in prefixes:
            if k.startswith(pref):
                saved[k] = os.environ.pop(k)
                break
    try:
        yield
    finally:
        os.environ.update(saved)


@contextlib.contextmanager
def _no_network():
    """把建连出口换成抛错：dry-run 若哪天开始触网，测试立刻炸而不是静默发真请求。

    是警报而非沙箱：只挡 socket 层的两个出口，不保证拦下一切。
    """
    real_socket, real_connect = socket.socket, socket.create_connection

    def _forbidden(*_a, **_kw):
        raise AssertionError("dry-run 不应发起任何网络请求")

    socket.socket, socket.create_connection = _forbidden, _forbidden
    try:
        yield
    finally:
        socket.socket, socket.create_connection = real_socket, real_connect


def run_cli(scope, argv):
    """用临时 scope 文件驱动 ims.main，捕获 stdout 解析为 JSON。返回 (退出码, JSON)。

    argparse 拒绝未知参数 / required 缺失时以 SystemExit(2) 退出、stdout 为空 ——
    此时返回 (code, None)。
    """
    with tempfile.TemporaryDirectory() as tmp:
        scope_path = os.path.join(tmp, "scope.yaml")
        with open(scope_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(scope, fh, allow_unicode=True)
        out, err = io.StringIO(), io.StringIO()
        try:
            with _isolate_env("HUAWEICLOUD_SDK_"), _no_network(), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ims.main(["--scope", scope_path, *argv])
        except SystemExit as e:  # argparse 拒绝未知参数或 required 缺失
            code = e.code
    raw = out.getvalue()
    if not raw.strip():
        return code, None
    try:
        return code, json.loads(raw)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"stdout 应为可解析的纯 JSON（{e}），实得：{raw!r}；stderr：{err.getvalue()!r}"
        )


def dry_run_body(*flags):
    """跑一次 create --dry-run，返回请求体的 body 段。"""
    _, payload = run_cli(
        make_cli_scope(),
        ["create", "--instance-id", "i-0001", "--dry-run", *flags],
    )
    body = ((payload.get("request") or {}).get("body")) or {}
    assert isinstance(body, dict), f"输出应含 request.body，实得：{payload!r}"
    return body


# ---- 缝 B：命令行入口的 dry-run 端到端 ----
def test_dry_run_contract():
    """create --dry-run：stdout 是纯 JSON、标记 dry_run、退出码 0。"""
    code, payload = run_cli(
        make_cli_scope(),
        ["create", "--instance-id", "i-0001", "--dry-run"],
    )
    assert code == 0, f"退出码应为 0，实得 {code!r}"
    assert payload.get("ok") is True, f"ok 应为 True，实得 {payload.get('ok')!r}"
    assert payload.get("action") == "create", f"action 实得 {payload.get('action')!r}"
    assert payload.get("dry_run") is True, f"dry_run 应为 True，实得 {payload.get('dry_run')!r}"
    assert payload.get("region") == "ap-southeast-3", \
        f"region 实得 {payload.get('region')!r}"


def test_dry_run_instance_id_reaches_request():
    """--instance-id 从命令行接到请求体的 instance_id 上。"""
    body = dry_run_body()
    assert body.get("instance_id") == "i-0001", \
        f"instance_id 实得 {body.get('instance_id')!r}"


def test_dry_run_image_name_reaches_request():
    """--image-name 从命令行接到请求体的 name 上。"""
    body = dry_run_body("--image-name", "web-img")
    assert body.get("name") == "web-img", f"name 实得 {body.get('name')!r}"


def test_dry_run_auto_generated_image_name_has_prefix():
    """不给 --image-name 时请求体里有自动生成的 img- 前缀名。"""
    body = dry_run_body()
    name = body.get("name") or ""
    assert name.startswith("img-"), f"应自动 img- 前缀，实得 {name!r}"
    assert len(name) == 12, f"应为 12 字符，实得 {len(name)}"


def test_dry_run_description_reaches_request():
    """--description 从命令行接到请求体。"""
    body = dry_run_body("--description", "daily backup")
    assert body.get("description") == "daily backup", \
        f"description 实得 {body.get('description')!r}"


def test_dry_run_description_absent_when_not_given():
    """不给 --description 时请求体不含该字段（None 被 _jsonable 过滤）。"""
    body = dry_run_body()
    assert "description" not in body, \
        f"不给时不应下发 description，实得 {body!r}"


def test_dry_run_enterprise_project_id_from_scope():
    """scope 的 ims_create.enterprise_project_id 走到请求体。"""
    _, payload = run_cli(
        make_cli_scope(enterprise_project_id="eps-001"),
        ["create", "--instance-id", "i-0001", "--dry-run"],
    )
    body = (payload.get("request") or {}).get("body") or {}
    assert body.get("enterprise_project_id") == "eps-001", \
        f"enterprise_project_id 实得 {body.get('enterprise_project_id')!r}"


def test_dry_run_type_is_ecs():
    """请求体的 type 固定为 ECS。"""
    body = dry_run_body()
    assert body.get("type") == "ECS", f"type 实得 {body.get('type')!r}"


def test_dry_run_placeholder_credentials_pass():
    """占位凭证（AK/SK/region）能通过必填校验（dry-run 在构造客户端前返回）。"""
    code, payload = run_cli(
        make_cli_scope(),
        ["create", "--instance-id", "i-0001", "--dry-run"],
    )
    assert code == 0, f"占位凭证应通过校验，退出码 0，实得 {code!r}"
    assert payload.get("ok") is True


def test_dry_run_no_network():
    """全程不触网（socket 层设陷阱，意外触网即报错）。"""
    # run_cli 内置 _no_network 陷阱；若 dry-run 触网，_forbidden 会抛 AssertionError。
    code, _ = run_cli(
        make_cli_scope(),
        ["create", "--instance-id", "i-0001", "--dry-run"],
    )
    assert code == 0


def test_dry_run_without_project_id():
    """scope 不含 project_id 时 dry-run 正常输出、退出码 0。"""
    scope = make_cli_scope()
    del scope["project_id"]
    code, payload = run_cli(scope, ["create", "--instance-id", "i-0001", "--dry-run"])
    assert code == 0, f"退出码应为 0，实得 {code!r}"
    assert payload.get("ok") is True


def test_create_requires_instance_id_flag():
    """不传 --instance-id 时 argparse 报 required 缺失（退出码 2）。"""
    code, _ = run_cli(
        make_cli_scope(),
        ["create", "--dry-run"],
    )
    assert code == 2, f"缺 --instance-id 应退出码 2，实得 {code!r}"


# ---- DEFAULT_SCOPE_PATH 指向仓库根 ----
def test_default_scope_path_at_repo_root():
    """DEFAULT_SCOPE_PATH 指向仓库根 scope.yaml（与 ecs-skill 共享）。"""
    resolved = DEFAULT_SCOPE_PATH.resolve()
    assert resolved.name == "scope.yaml", f"文件名应为 scope.yaml，实得 {resolved.name!r}"
    skill_dir = pathlib.Path(__file__).resolve().parent.parent  # .claude/skills/ims-skill/
    assert resolved.parent != skill_dir, \
        f"scope.yaml 不应在 skill 目录 ({skill_dir})，实得 {resolved}"
    assert (resolved.parent / ".claude").is_dir(), \
        f"scope.yaml 应在仓库根（含 .claude/ 的目录），实得父目录 {resolved.parent}"


# ---- 缝 C：poll_job 轮询逻辑（mock client，不触网）----
def _job_resp(status, *, image_id=None, process_percent=None, fail_reason=None,
              error_code=None):
    """构造一个 ShowJobResponse mock。"""
    entities = JobEntities(
        image_id=image_id,
        process_percent=process_percent,
    ) if (image_id or process_percent is not None) else None
    return ShowJobResponse(
        status=status,
        job_id="j-fake",
        entities=entities,
        fail_reason=fail_reason,
        error_code=error_code,
    )


class FakeImsClient:
    """最小 mock ImsClient——按脚本返回 show_job / list_images 响应。

    responses: list of ShowJobResponse（依次返回）。
    images: list of ImageInfo（list_images 恒返回此列表）。
    call_count: show_job 被调了几次。
    list_images_calls: list_images 被调了几次。
    sleep_patch: 若 True，patch ims.time.sleep 为 no-op（测试不真睡）。
    """

    def __init__(self, responses, images=None):
        self._responses = list(responses)
        self._images = list(images) if images else []
        self.call_count = 0
        self.list_images_calls = 0

    def show_job(self, req):
        self.call_count += 1
        if self.call_count <= len(self._responses):
            return self._responses[self.call_count - 1]
        return self._responses[-1]  # 超出后重复最后一个

    def list_images(self, req):
        self.list_images_calls += 1
        return ListImagesResponse(images=list(self._images))


@contextlib.contextmanager
def _patch_sleep():
    """把 ims.time.sleep 替换为 no-op，测试不真睡。"""
    real = ims.time.sleep
    ims.time.sleep = lambda _s: None
    try:
        yield
    finally:
        ims.time.sleep = real


def test_poll_job_success_immediate():
    """job 已 SUCCESS → 立即返回 image_id，不轮询。"""
    client = FakeImsClient([_job_resp("SUCCESS", image_id="img-123")])
    trace = []
    with _patch_sleep():
        status, image_id, resp = ims.poll_job(
            client, job_id="j-fake", timeout=30, interval=1, trace=trace,
        )
    assert status == "SUCCESS", f"status 应为 SUCCESS，实得 {status!r}"
    assert image_id == "img-123", f"image_id 实得 {image_id!r}"
    assert client.call_count == 1, f"应只调一次 show_job，实得 {client.call_count}"
    assert len(trace) == 1, f"trace 应有 1 条，实得 {len(trace)}"


def test_poll_job_running_then_success():
    """RUNNING → RUNNING → SUCCESS：中间报进度，最终拿到 image_id。"""
    client = FakeImsClient([
        _job_resp("RUNNING", process_percent=30.0),
        _job_resp("RUNNING", process_percent=80.0),
        _job_resp("SUCCESS", image_id="img-456"),
    ])
    trace = []
    with _patch_sleep():
        status, image_id, resp = ims.poll_job(
            client, job_id="j-fake", timeout=60, interval=1, trace=trace,
        )
    assert status == "SUCCESS", f"status 应为 SUCCESS，实得 {status!r}"
    assert image_id == "img-456", f"image_id 实得 {image_id!r}"
    assert client.call_count == 3, f"应调 3 次 show_job，实得 {client.call_count}"
    assert len(trace) == 3, f"trace 应有 3 条，实得 {len(trace)}"
    assert trace[0]["process_percent"] == 30.0, \
        f"trace[0] 百分比实得 {trace[0].get('process_percent')!r}"


def test_poll_job_init_status():
    """INIT 状态也应继续轮询（不视为终态）。"""
    client = FakeImsClient([
        _job_resp("INIT", process_percent=0.0),
        _job_resp("SUCCESS", image_id="img-789"),
    ])
    trace = []
    with _patch_sleep():
        status, image_id, resp = ims.poll_job(
            client, job_id="j-fake", timeout=30, interval=1, trace=trace,
        )
    assert status == "SUCCESS", f"INIT 后应轮询到 SUCCESS，实得 {status!r}"
    assert image_id == "img-789"


def test_poll_job_fail():
    """FAIL 状态 → 返回 (FAIL, None)，带 fail_reason。"""
    client = FakeImsClient([
        _job_resp("FAIL", fail_reason="ECS状态不允许", error_code="IMG.0001"),
    ])
    trace = []
    with _patch_sleep():
        status, image_id, resp = ims.poll_job(
            client, job_id="j-fake", timeout=30, interval=1, trace=trace,
        )
    assert status == "FAIL", f"status 应为 FAIL，实得 {status!r}"
    assert image_id is None, f"FAIL 时 image_id 应为 None，实得 {image_id!r}"
    assert getattr(resp, "fail_reason", None) == "ECS状态不允许"


def test_poll_job_timeout():
    """超时（timeout=0 立即到期）→ 返回 (TIMEOUT, None)。"""
    client = FakeImsClient([_job_resp("RUNNING", process_percent=50.0)])
    trace = []
    with _patch_sleep():
        status, image_id, resp = ims.poll_job(
            client, job_id="j-fake", timeout=0, interval=1, trace=trace,
        )
    assert status == "TIMEOUT", f"status 应为 TIMEOUT，实得 {status!r}"
    assert image_id is None, f"超时时 image_id 应为 None，实得 {image_id!r}"


def test_poll_job_stderr_progress():
    """RUNNING 时向 stderr 输出进度百分比。"""
    client = FakeImsClient([
        _job_resp("RUNNING", process_percent=42.0),
        _job_resp("SUCCESS", image_id="img-out"),
    ])
    trace = []
    err = io.StringIO()
    with _patch_sleep(), contextlib.redirect_stderr(err):
        ims.poll_job(client, job_id="j-fake", timeout=30, interval=1, trace=trace)
    err_text = err.getvalue()
    assert "42" in err_text, f"stderr 应含进度 42%，实得 {err_text!r}"
    assert "j-fake" in err_text, f"stderr 应含 job_id，实得 {err_text!r}"


# ---- 缝 D：cmd_show 三条路径（mock client，不触网）----
def _image(img_id="img-001", name="web-img", status="active", size=40,
           os_type="Linux", disk_format="qcow2", min_disk=40):
    """构造一个 ImageInfo mock。"""
    return ImageInfo(
        id=img_id, name=name, status=status, size=size,
        os_type=os_type, disk_format=disk_format, min_disk=min_disk,
    )


@contextlib.contextmanager
def _capture_stdout():
    """捕获 stdout（emit 写入处），返回 StringIO。"""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        yield out


def _run_show_helper(fn, client, **kw):
    """调用 _show_by_* helper，返回 (退出码, stdout JSON)。"""
    with _capture_stdout() as out:
        code = fn(client, **kw)
    raw = out.getvalue()
    payload = json.loads(raw) if raw.strip() else None
    return code, payload


# -- --id 路径 --
def test_show_by_id_found():
    """--id：list_images 返回镜像 → ok=True，image 含契约字段。"""
    img = _image(img_id="img-001", name="web", status="active")
    client = FakeImsClient([], images=[img])
    code, payload = _run_show_helper(ims._show_by_id, client, image_id="img-001")
    assert code == 0, f"找到镜像应退出码 0，实得 {code}"
    assert payload["ok"] is True
    image = payload["image"]
    assert image["id"] == "img-001", f"id 实得 {image['id']!r}"
    assert image["name"] == "web", f"name 实得 {image['name']!r}"
    assert image["status"] == "active"
    assert image["os_type"] == "Linux"
    assert image["disk_format"] == "qcow2"
    assert image["size"] == 40
    assert image["min_disk"] == 40


def test_show_by_id_not_found():
    """--id：未找到 → ok=False，error 含「未找到该镜像」，退出码 1。"""
    client = FakeImsClient([], images=[])
    code, payload = _run_show_helper(ims._show_by_id, client, image_id="img-404")
    assert code == 1, f"未找到应退出码 1，实得 {code}"
    assert payload["ok"] is False
    assert "未找到该镜像" in payload["error"], f"error 实得 {payload['error']!r}"


def test_show_by_id_api_error():
    """--id：API 异常 → ok=False + 退出码 1，stdout 仍是纯 JSON。"""
    class BoomClient:
        def list_images(self, req):
            raise RuntimeError("network down")
    code, payload = _run_show_helper(ims._show_by_id, BoomClient(), image_id="img-x")
    assert code == 1
    assert payload["ok"] is False
    assert "list_images" in payload["error"]


# -- --name 路径 --
def test_show_by_name_found():
    """--name：list_images 返回匹配 → ok=True，images 列表。"""
    imgs = [_image(img_id="img-a", name="web"), _image(img_id="img-b", name="web")]
    client = FakeImsClient([], images=imgs)
    code, payload = _run_show_helper(ims._show_by_name, client, name="web")
    assert code == 0, f"找到应退出码 0，实得 {code}"
    assert payload["ok"] is True
    images = payload["images"]
    assert len(images) == 2, f"应返回 2 个匹配，实得 {len(images)}"
    assert images[0]["id"] == "img-a"


def test_show_by_name_not_found():
    """--name：未找到 → ok=False + 退出码 1。"""
    client = FakeImsClient([], images=[])
    code, payload = _run_show_helper(ims._show_by_name, client, name="nope")
    assert code == 1
    assert payload["ok"] is False
    assert "未找到该镜像" in payload["error"]


# -- --job-id 路径：SUCCESS --
def test_show_by_job_id_success_returns_image():
    """--job-id SUCCESS：取 image_id 再查 list_images → ok=True 含完整镜像信息。"""
    img = _image(img_id="img-from-job", name="result", status="active")
    client = FakeImsClient(
        [_job_resp("SUCCESS", image_id="img-from-job")],
        images=[img],
    )
    code, payload = _run_show_helper(ims._show_by_job_id, client, job_id="j-1")
    assert code == 0, f"SUCCESS 应退出码 0，实得 {code}"
    assert payload["ok"] is True
    assert payload["status"] == "SUCCESS"
    assert payload["job_id"] == "j-1"
    assert payload["image"]["id"] == "img-from-job"
    assert client.call_count == 1, "应调 1 次 show_job"
    assert client.list_images_calls == 1, "应调 1 次 list_images"


def test_show_by_job_id_success_but_no_image_id():
    """--job-id SUCCESS 但 entities 无 image_id → ok=False。"""
    client = FakeImsClient(
        [_job_resp("SUCCESS", image_id=None)],
        images=[],
    )
    code, payload = _run_show_helper(ims._show_by_job_id, client, job_id="j-2")
    assert code == 1, f"无 image_id 应退出码 1，实得 {code}"
    assert payload["ok"] is False


def test_show_by_job_id_success_image_not_found():
    """--job-id SUCCESS，image_id 有值但 list_images 返回空 → ok=False。"""
    client = FakeImsClient(
        [_job_resp("SUCCESS", image_id="img-gone")],
        images=[],
    )
    code, payload = _run_show_helper(ims._show_by_job_id, client, job_id="j-3")
    assert code == 1
    assert payload["ok"] is False
    assert "未找到该镜像" in payload["error"]


# -- --job-id 路径：RUNNING --
def test_show_by_job_id_running():
    """--job-id RUNNING：返回 ok=False, status=RUNNING, process_percent, hint。"""
    client = FakeImsClient([_job_resp("RUNNING", process_percent=67.0)])
    code, payload = _run_show_helper(ims._show_by_job_id, client, job_id="j-run")
    assert code == 1, f"RUNNING 应退出码 1，实得 {code}"
    assert payload["ok"] is False
    assert payload["status"] == "RUNNING"
    assert payload["process_percent"] == 67.0
    assert "hint" in payload
    assert "j-run" in payload["hint"], f"hint 应含 job_id，实得 {payload['hint']!r}"


def test_show_by_job_id_init_treated_as_running():
    """--job-id INIT：视为 RUNNING（仍在进行中）。"""
    client = FakeImsClient([_job_resp("INIT", process_percent=0.0)])
    code, payload = _run_show_helper(ims._show_by_job_id, client, job_id="j-init")
    assert code == 1
    assert payload["status"] == "RUNNING"


# -- --job-id 路径：FAIL --
def test_show_by_job_id_fail():
    """--job-id FAIL：返回 ok=False, status=FAIL, fail_reason。"""
    client = FakeImsClient([
        _job_resp("FAIL", fail_reason="磁盘空间不足", error_code="IMG.0001"),
    ])
    code, payload = _run_show_helper(ims._show_by_job_id, client, job_id="j-fail")
    assert code == 1, f"FAIL 应退出码 1，实得 {code}"
    assert payload["ok"] is False
    assert payload["status"] == "FAIL"
    assert payload["fail_reason"] == "磁盘空间不足"
    assert payload.get("error_code") == "IMG.0001"


def test_show_by_job_id_api_error():
    """--job-id：show_job API 异常 → ok=False + 退出码 1，stdout 纯 JSON。"""
    class BoomClient:
        def show_job(self, req):
            raise RuntimeError("timeout")
        def list_images(self, req):
            raise AssertionError("不应调 list_images")
    code, payload = _run_show_helper(ims._show_by_job_id, BoomClient(), job_id="j-x")
    assert code == 1
    assert payload["ok"] is False
    assert "show_job" in payload["error"]


# -- CLI 层：互斥参数组 --
def test_show_requires_one_flag():
    """show 不给任何标志 → argparse 报 required 缺失（退出码 2）。"""
    code, _ = run_cli(make_cli_scope(), ["show"])
    assert code == 2, f"缺标志应退出码 2，实得 {code!r}"


def test_show_flags_mutually_exclusive():
    """show 同时给 --id 和 --name → argparse 报互斥（退出码 2）。"""
    code, _ = run_cli(
        make_cli_scope(),
        ["show", "--id", "img-1", "--name", "web"],
    )
    assert code == 2, f"互斥冲突应退出码 2，实得 {code!r}"


def test_show_stdout_always_json_when_found():
    """--id 路径的 stdout 可解析为 JSON（纯 JSON 契约）。"""
    img = _image()
    client = FakeImsClient([], images=[img])
    code, payload = _run_show_helper(ims._show_by_id, client, image_id="img-001")
    assert isinstance(payload, dict), f"stdout 应为 JSON 对象，实得 {type(payload)}"


# ---- 缝 E：cmd_create 失败契约（mock client，不触网）----
@contextlib.contextmanager
def _patch_build_client(client):
    """把 ims.build_client 替换为返回固定 mock client（cmd_create 用）。"""
    real = ims.build_client
    ims.build_client = lambda _creds: client
    try:
        yield
    finally:
        ims.build_client = real


def test_create_api_exception_has_status_and_hint():
    """create_image 抛异常 → 输出 JSON 含 status=FAIL + hint（spec 失败契约）。"""
    class BoomClient:
        def create_image(self, req):
            raise RuntimeError("IMS.0001 quota exceeded")
    with _patch_build_client(BoomClient()):
        code, payload = run_cli(
            make_cli_scope(),
            ["create", "--instance-id", "i-0001", "--image-name", "boom-img"],
        )
    assert code == 1, f"异常应退出码 1，实得 {code}"
    assert payload["ok"] is False
    assert payload["status"] == "FAIL", f"status 应为 FAIL，实得 {payload.get('status')!r}"
    assert "hint" in payload, f"失败契约应含 hint，实得 {payload!r}"
    assert payload["job_id"] == "", f"无 job_id 时应为空串，实得 {payload.get('job_id')!r}"
    assert payload["region"] == "ap-southeast-3", \
        f"region 应在失败输出中，实得 {payload.get('region')!r}"


def test_create_empty_job_id_has_status_and_hint():
    """create_image 返回空 job_id → 输出 JSON 含 status=FAIL + hint（spec 失败契约）。"""
    from huaweicloudsdkims.v2 import CreateImageResponse

    class EmptyJobClient:
        def create_image(self, req):
            return CreateImageResponse(job_id=None)
        def show_job(self, req):
            raise AssertionError("不应调 show_job")
        def list_images(self, req):
            raise AssertionError("不应调 list_images")
    with _patch_build_client(EmptyJobClient()), _patch_sleep():
        code, payload = run_cli(
            make_cli_scope(),
            ["create", "--instance-id", "i-0001", "--image-name", "empty-job"],
        )
    assert code == 1, f"空 job_id 应退出码 1，实得 {code}"
    assert payload["ok"] is False
    assert payload["status"] == "FAIL", f"status 应为 FAIL，实得 {payload.get('status')!r}"
    assert "hint" in payload, f"失败契约应含 hint，实得 {payload!r}"
    assert payload["job_id"] == "", f"无 job_id 时应为空串，实得 {payload.get('job_id')!r}"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"✓ {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"✗ {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"✗ {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    sys.exit(1 if failed else 0)
