"""流水线任务信息：执行提示词约定、JSON 解析与共用标题清洗。"""
import json

from .redact import redact_text


TITLE_MAX_CHARS = 24

TASK_INFO_EXAMPLE = {
    "title": "部署 HBase",
    "software": "hbase",
    "actual_version": None,
    "output_dir": None,
    "install_meta_file": None,
}
TASK_INFO_FIELDS = tuple(TASK_INFO_EXAMPLE)

_OPEN_TAG = "<task_info>"
_CLOSE_TAG = "</task_info>"

TASK_INFO_INSTRUCTIONS = f"""流水线任务信息：
- 每次派发流水线子 agent（包括续作）时，在现有 Agent/Task 的 prompt 字符串开头写下方 JSON 块，再写本次执行要求；派发时用本回合信息替换示例，并填写全部字段。
- title 由你结合会话整体目标与上下文拟定，使用用户语言，单行且不超过 {TITLE_MAX_CHARS} 字符；后续阶段与续聊沿用首次整体标题。批量 RPM 或多软件任务概括整个批次，单一身份字段不适用时填 null。
- software、actual_version、output_dir 只填写本次已核实的目标软件身份、实际版本、完整项目相对产物目录；未知填 null。实际版本与目录分别记录，目标系统版本、参考版本、目录名和 latest 均不代替实际版本。
- install_meta_file 是完整项目相对文件路径，仅填写你已核对属于本次安装或明确续作的机器记录；新部署 GUIDE 阶段填 null，明确续作核对原机器记录后再提供。

{_OPEN_TAG}
{json.dumps(TASK_INFO_EXAMPLE, ensure_ascii=False, indent=2)}
{_CLOSE_TAG}"""


def clean_title(raw):
    """标题输出去引号、空白与尾标点，再脱敏并限制长度。"""
    if not isinstance(raw, str):
        return None
    text = raw.strip().strip("\"'“”‘’「」『』").strip()
    text = " ".join(text.split()).rstrip(".?!。！？…")
    if not text:
        return None
    text = redact_text(text)
    return text[:TITLE_MAX_CHARS]


def parse_task_info(prompt):
    """从 prompt 的 JSON 块读取完整任务信息；缺失或形状错误返回 None。"""
    if not isinstance(prompt, str):
        return None
    _prefix, opening, remainder = prompt.partition(_OPEN_TAG)
    if not opening:
        return None
    encoded, closing, _suffix = remainder.partition(_CLOSE_TAG)
    if not closing:
        return None
    try:
        raw = json.loads(encoded)
    except (ValueError, RecursionError):
        return None
    if not isinstance(raw, dict) or any(field not in raw for field in TASK_INFO_FIELDS):
        return None
    title = clean_title(raw["title"])
    if title is None:
        return None
    info = {"title": title}
    for field in TASK_INFO_FIELDS[1:]:
        value = raw[field]
        if value is not None:
            if not isinstance(value, str):
                return None
            value = redact_text(value).strip() or None
        info[field] = value
    return info
