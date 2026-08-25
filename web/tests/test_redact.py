#!/usr/bin/env python
"""redact 纯函数测试 —— 事件出口脱敏的已知凭据形状。

形状来源含真部署实测漏出的样本：华为云 SK 实测为 38 位大小写混合
（非注释里曾以为的 40 位小写），`sk: 值` 字段形态需字段规则命中。
宁可多遮：长标识被误伤只损可读性。纯 assert，无 pytest。

运行：python web/tests/test_redact.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from web.redact import load_scope_secrets, redact_text, register_secrets  # noqa: E402

SAMPLES = [
    # (说明, 样本文本, 不得出现的明文)
    ("AK 20 位大写", "ak: HWPFEJ9AB3CDEFGHIJKL", "HWPFEJ9AB3CDEFGHIJKL"),
    ("SK 字段 38 位大小写混合（实测形状）", "sk: Fs0fStJTNnERXmB4l6jerX6XCwjoW9hVtCIv2yt3", "Fs0fStJTNnERXmB4l6jerX6XCwjoW9hVtCIv2yt3"),
    ("SK 游离值 40 位小写", "key=f3a9c81d0b7e46f2a5d8c3b1e9470ad6c2f5b831", "f3a9c81d0b7e46f2a5d8c3b1e9470ad6c2f5b831"),
    ("SK 游离值 32 位混合", "token Fs0fStJTNnERXmB4l6jerX6XCwjoW9hVtC", "Fs0fStJTNnERXmB4l6jerX6XCwjoW9hVtC"),
    ("密码字段", "password: Xk9$mPq2LwzR", "Xk9$mPq2LwzR"),
    ("私钥块", "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "MIIEow"),
    ("tool_result 内嵌 scope.yaml 行", "2\tsk: Fs0fStJTNnERXmB4l6jerX6XCwjoW9hVtCIv2yt3\n3\tregion", "Fs0fStJTNnERXmB4l6jerX6XCwjoW9hVtCIv2yt3"),
]


def test_known_credential_shapes_masked():
    for note, text, leak in SAMPLES:
        masked = redact_text(text)
        assert leak not in masked, f"{note} 漏遮：{masked}"
        assert "***" in masked, note


def test_plain_text_survives():
    """普通部署文本不被破坏（遮蔽只发生在凭据形状上）。"""
    text = "nginx 1.30.4 已安装，验证契约 exit-code-v1 通过，详见 deploy/nginx/1.30.4/verify.md"
    assert redact_text(text) == text


def test_admin_pass_field_masked():
    """云厂商返回的 admin_pass 字段键（实测漏出形态）命中字段规则。"""
    masked = redact_text("admin_pass: pcb20260806@@ (from scope.yaml)")
    assert "pcb20260806@@" not in masked


def test_registered_secret_masked_in_any_context():
    """运行时登记的已知凭据值：反引号内联、自然语言提及等任意上下文整值遮蔽
    （实测漏出形态：password `pcb20260806@@` 内联在 thinking 里）。"""
    register_secrets(["pcb20260806@@"])
    for text in (
        "scope.yaml has defaults: password `pcb20260806@@`, flavorRef c7n.xlarge.2",
        "admin_pass: pcb20260806@@ (must NOT be echoed)",
        "密码是 pcb20260806@@ 请保密",
    ):
        assert "pcb20260806@@" not in redact_text(text), text


def test_load_scope_secrets_reads_yaml():
    """scope.yaml → 已知凭据值登记：敏感键的值入清单，普通键不误收。"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(
            "ak: AKIA123EXAMPLEKEY\n"
            "sk: Fs0fStJTNnERXmB4l6jerX6XCwjoW9hVtCIv2yt3\n"
            "region: ap-southeast-3\n"
            "ecs_create:\n"
            "  server:\n"
            "    flavorRef: c7n.xlarge.2\n"
            "    password: MyStr0ngPass!@\n"
        )
        path = f.name
    load_scope_secrets(path)
    os.unlink(path)
    for secret in ("MyStr0ngPass!@", "AKIA123EXAMPLEKEY"):
        assert secret not in redact_text(f"echo {secret}")
    # 普通配置值不误伤
    assert "c7n.xlarge.2" in redact_text("flavor is c7n.xlarge.2")


def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    main()
