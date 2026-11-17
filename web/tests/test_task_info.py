#!/usr/bin/env python3
"""任务信息 JSON 与共用标题清洗断言。

运行：python web/tests/test_task_info.py
"""
import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from web.task_info import (  # noqa: E402
    TASK_INFO_EXAMPLE,
    TASK_INFO_FIELDS,
    TASK_INFO_INSTRUCTIONS,
    TITLE_MAX_CHARS,
    parse_task_info,
)


def task_prompt(info):
    return f"<task_info>\n{json.dumps(info, ensure_ascii=False)}\n</task_info>\n执行本次阶段。"


def test_confirmed_identity_and_paths_are_read_independently():
    info = {
        "title": "  “部署 HBase。”  ",
        "software": " hbase ",
        "actual_version": " 2.4.18 ",
        "output_dir": " deploy/HBase-old/latest ",
        "install_meta_file": " deploy/HBase-old/latest/machine-meta.json ",
    }
    assert parse_task_info(task_prompt(info)) == {
        "title": "部署 HBase",
        "software": "hbase",
        "actual_version": "2.4.18",
        "output_dir": "deploy/HBase-old/latest",
        "install_meta_file": "deploy/HBase-old/latest/machine-meta.json",
    }


def test_prompt_environment_does_not_supply_missing_identity():
    info = {**TASK_INFO_EXAMPLE, "software": None}
    prompt = "一键部署 Apache HBase，使用 EulerOS 2.0 ARM64。\n" + task_prompt(info)
    parsed = parse_task_info(prompt)
    assert parsed == info
    assert parsed["actual_version"] is None and parsed["output_dir"] is None


def test_batch_identity_can_remain_null():
    info = {field: None for field in TASK_INFO_FIELDS}
    info["title"] = "制作 5 个 RPM 包"
    assert parse_task_info(task_prompt(info)) == info


def test_blank_optional_values_remain_unknown():
    info = {field: " \t" for field in TASK_INFO_FIELDS}
    info["title"] = "安装 Docker"
    parsed = parse_task_info(task_prompt(info))
    assert parsed == {field: "安装 Docker" if field == "title" else None for field in TASK_INFO_FIELDS}


def test_titles_are_redacted_before_length_limit():
    secret = "Xk9$mPq2LwzR"
    info = {**TASK_INFO_EXAMPLE, "title": f"部署 password: {secret} " + "x" * 100}
    title = parse_task_info(task_prompt(info))["title"]
    assert secret not in title and "***" in title
    assert len(title) == TITLE_MAX_CHARS


def test_rules_contain_a_valid_complete_example():
    assert parse_task_info(TASK_INFO_INSTRUCTIONS) == TASK_INFO_EXAMPLE


def test_missing_keys_and_incorrect_types_are_not_task_info():
    for field in TASK_INFO_FIELDS:
        missing = {key: value for key, value in TASK_INFO_EXAMPLE.items() if key != field}
        assert parse_task_info(task_prompt(missing)) is None, field
        for bad in (False, 1, [], {}):
            assert parse_task_info(task_prompt({**TASK_INFO_EXAMPLE, field: bad})) is None, (field, bad)
    for bad_title in (None, "", "  ", '""', "。！？"):
        assert parse_task_info(task_prompt({**TASK_INFO_EXAMPLE, "title": bad_title})) is None


def test_non_object_json_and_missing_or_damaged_blocks_are_ignored():
    for raw in (None, [], "部署 HBase", 1):
        assert parse_task_info(task_prompt(raw)) is None
    for prompt in (None, [], {}, "执行本次阶段", "<task_info>{broken}</task_info>",
                   "<task_info>{}", "{} </task_info>"):
        assert parse_task_info(prompt) is None


def test_additional_fields_are_not_exposed():
    assert parse_task_info(task_prompt({**TASK_INFO_EXAMPLE, "description": "补充说明"})) == TASK_INFO_EXAMPLE


def test_oversized_number_does_not_escape_the_parser():
    encoded = ('{"title":"部署 HBase","software":"hbase","actual_version":'
               + "7" * 5000 + ',"output_dir":null,"install_meta_file":null}')
    assert parse_task_info("<task_info>" + encoded + "</task_info>") is None


def test_import_does_not_load_sdk_or_title_sessions():
    subprocess.run(
        [sys.executable, "-B", "-c", "import sys; import web.task_info; "
         "assert 'web.sdk' not in sys.modules; assert 'web.title' not in sys.modules; "
         "assert 'claude_agent_sdk' not in sys.modules"],
        cwd=PROJECT_ROOT, check=True,
    )


def main():
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for fn in tests:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    main()
