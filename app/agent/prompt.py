from __future__ import annotations


def build_react_instructions() -> str:
    return (
        "你是代码审查 Agent。你可以调用工具，但必须遵守：\n"
        "- 你每次回复必须是“纯 JSON”\n"
        '- 如果要调用工具：{"kind":"action","call":{"name":"...","args":{...}}}\n'
        '- 如果要结束：{"kind":"final","answer":"..."}\n'
        "- 不要输出 markdown，不要输出解释性文字。\n"
        "可用工具：get_diff_chunk, find_risky_pattern, calc_python_complexity\n"
    )


