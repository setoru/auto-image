"""事件出口脱敏：所有进入事件流的文本共用这一层兜底。

两层防线：已知凭据的具体值（运行时从 scope.yaml 登记，任意上下文整值
遮蔽——实测凭据会以反引号内联、自然语言提及等非字段形态出现，形状
正则防不住）+ 已知凭据形状（AK/SK、密码字段、私钥块）。宁可多遮（长
哈希、长常量被误伤只损可读性），不可漏遮（凭据泄漏到浏览器不可逆）。
"""
import re
from pathlib import Path

import yaml

MASK = "***"
# 已知值最小长度：过短的普通字符串不当凭据整值遮蔽
_MIN_SECRET_LEN = 8

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
# 华为云凭据形状：AK 20 位大写字母数字；SK 实测 38 位大小写混合，
# 兜底按 32-40 位字母数字遮蔽（游离值无字段名前缀，长度是唯一线索）
_AK_RE = re.compile(r"\b[A-Z0-9]{20}\b")
_SK_RE = re.compile(r"\b[A-Za-z0-9]{32,40}\b")
# 字段键含实测见过的云厂商返回形态（admin_pass 来自 ECS 创建响应）
_SECRET_FIELD_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key"
    r"|admin[_-]?pass(?:word)?|ak|sk)\b(\s*[:=]\s*)(\S+)"
)
# scope.yaml 中视为凭据值的键（值进已知清单；vpcid/imageRef 等普通键不收）
_SECRET_KEY_RE = re.compile(r"(?i)(password|secret|token|key|credential|^ak$|^sk$)")

# 运行时已知凭据值（服务启动时从 scope.yaml 一次性加载，运行期只读）
KNOWN_SECRETS = []


def register_secrets(values):
    """登记已知凭据值；短值（普通配置误命中）与重复跳过。"""
    for value in values:
        if isinstance(value, str) and len(value) >= _MIN_SECRET_LEN and value not in KNOWN_SECRETS:
            KNOWN_SECRETS.append(value)


def load_scope_secrets(path):
    """scope.yaml → 已知凭据值登记（服务启动调用一次；文件缺失静默跳过）。"""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and _SECRET_KEY_RE.search(str(key)):
                    register_secrets([value])
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)


def redact_text(text):
    """遮蔽文本中的已知凭据值与凭据形状；非字符串原样返回。"""
    if not isinstance(text, str) or not text:
        return text
    text = _PRIVATE_KEY_RE.sub(MASK, text)
    text = _SECRET_FIELD_RE.sub(lambda m: m.group(1) + m.group(2) + MASK, text)
    text = _AK_RE.sub(MASK, text)
    text = _SK_RE.sub(MASK, text)
    for secret in KNOWN_SECRETS:
        if secret in text:
            text = text.replace(secret, MASK)
    return text
