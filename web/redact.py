"""事件出口脱敏：所有进入事件流的文本共用这一层正则兜底。

已知凭据形状（AK/SK、密码字段、私钥块）匹配即遮蔽；宁可多遮（长哈希、
长常量被误伤只损可读性），不可漏遮（凭据泄漏到浏览器不可逆）。
"""
import re

MASK = "***"

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
# 华为云凭据形状：AK 20 位大写字母数字，SK 40 位小写字母数字
_AK_RE = re.compile(r"\b[A-Z0-9]{20}\b")
_SK_RE = re.compile(r"\b[a-z0-9]{40}\b")
_SECRET_FIELD_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\b(\s*[:=]\s*)(\S+)"
)


def redact_text(text):
    """遮蔽文本中的已知凭据形状；非字符串原样返回。"""
    if not isinstance(text, str) or not text:
        return text
    text = _PRIVATE_KEY_RE.sub(MASK, text)
    text = _SECRET_FIELD_RE.sub(lambda m: m.group(1) + m.group(2) + MASK, text)
    text = _AK_RE.sub(MASK, text)
    text = _SK_RE.sub(MASK, text)
    return text
