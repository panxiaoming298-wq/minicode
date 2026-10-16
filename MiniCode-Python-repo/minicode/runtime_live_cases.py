"""Frozen, hand-authored coding tasks for the opt-in Runtime diagnostic.

These are task inputs, not model responses or representative production evals.
All writable paths are explicit; the external checker is never in a workspace.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LiveCase:
    case_id: str
    files: tuple[tuple[str, str], ...]
    writable: tuple[str, ...]
    prompt: str
    criteria: tuple[str, ...]
    checker: str
    checker_support: tuple[str, ...] = ()

    @property
    def initial_files(self) -> dict[str, str]:
        return dict(self.files)


PAGINATION = LiveCase(
    "pagination-contract-v1",
    (
        ("pagination.py", "def paginate(items, page, page_size):\n    start = page * page_size\n    return items[start:start + page_size]\n"),
        ("SPEC.md", (
            "Fix paginate(items, page, page_size) in pagination.py. Pages are one-based.\n"
            "Return a NEW list for the requested page; out-of-range pages return [].\n"
            "Do not mutate the input list. page and page_size must be positive integers;\n"
            "reject bool, non-integers, zero and negatives with ValueError.\n"
            "Keep exactly one plain function, no annotations/defaults/decorators/imports/\n"
            "attributes/loops/comprehensions or other module statements except docstrings.\n"
            "Ordinary if/return/assignments, slices and operators are supported.\n"
            "Only these built-in calls are allowed: len, min, max, range, isinstance, int, bool, ValueError.\n"
            "Source size limit: 20 KB. Do not edit SPEC.md.\n"
        )),
    ),
    ("pagination.py",),
    "修复 pagination.py 的分页错误，并满足 SPEC.md 中的完整契约。"
    "可读文件：pagination.py、SPEC.md；只允许修改 pagination.py。"
    "通过 run_acceptance_checks 执行固定验收；不能修改检查器或引入依赖。"
    "你可以在有实际收益时使用只读助手，但这个小任务不要求委派。"
    "完成后简洁说明修改和实际验证结果。",
    ("Pagination satisfies the immutable contract and acceptance suite.",),
    "runtime_live_eval_checks.py",
)

EXPORT = LiveCase(
    "export-policies-v1",
    (
        ("batch_window.py", "def batch_window(total, cursor, limit):\n    stop = cursor + limit\n    return {'start': cursor, 'stop': stop, 'next_cursor': stop}\n"),
        ("retry_delay.py", "def retry_delay(status, attempt, max_attempts, base_ms, cap_ms, retry_after_ms):\n    if attempt > max_attempts:\n        return None\n    return min(base_ms, cap_ms)\n"),
        ("SPEC.md", (
            "Repair both export policies: batch_window.py and retry_delay.py.\n"
            "Read WINDOW_SPEC.md and RETRY_SPEC.md for the two independent interface contracts.\n"
            "Only these two Python files may change. Specifications and checker are immutable.\n"
            "Each module contains exactly its named plain function and optional docstrings.\n"
            "Use only if/return/assign/raise, scalar operators, literals, indexing and slices.\n"
            "No imports, attributes, loops, comprehensions, nested functions, decorators, annotations or defaults.\n"
            "Callable built-ins: len, min, max, range, isinstance, int, bool, ValueError.\n"
            "No dependencies. Each source file is at most 20 KB. Run run_acceptance_checks.\n"
            "The external checker tests each interface and walks complete batches using both policies.\n"
        )),
        ("WINDOW_SPEC.md", (
            "batch_window(total, cursor, limit) -> dict with EXACT keys start, stop, next_cursor.\n"
            "All inputs must be integers but NOT bool. total/cursor >= 0; limit > 0.\n"
            "Validate every argument first and raise ValueError for any invalid argument.\n"
            "start = min(cursor, total); stop = min(start + limit, total).\n"
            "next_cursor is stop if stop < total, otherwise None. Empty or exhausted input returns\n"
            "start=stop=total and next_cursor=None; never return an out-of-range index.\n"
            "Walking from cursor=0 until next_cursor=None must cover every item exactly once.\n"
        )),
        ("RETRY_SPEC.md", (
            "retry_delay(status, attempt, max_attempts, base_ms, cap_ms, retry_after_ms) -> integer milliseconds or None.\n"
            "status, attempt, max_attempts, base_ms, cap_ms must be integers but NOT bool.\n"
            "status is 100..599 inclusive; attempt/max_attempts >= 1; base_ms/cap_ms >= 0.\n"
            "retry_after_ms must be None or a nonnegative integer but NOT bool.\n"
            "Validate ALL arguments before applying any eligibility rules; invalid inputs raise ValueError.\n"
            "Only status 408, 429, 500, 502, 503, 504 is retryable. attempt is the just-failed\n"
            "1-based attempt. If attempt >= max_attempts, or status is not retryable, return None.\n"
            "Otherwise start with attempt * base_ms (linear, NOT exponential). Only for 429/503,\n"
            "a non-None retry_after_ms is a lower bound on this delay. Finally cap the delay at cap_ms,\n"
            "even when Retry-After is larger. A zero millisecond delay means retry now, NOT None.\n"
        )),
    ),
    ("batch_window.py", "retry_delay.py"),
    "修复导出服务的两项策略：batch_window.py 的游标窗口与 retry_delay.py 的重试延迟。"
    "请读 SPEC.md、WINDOW_SPEC.md、RETRY_SPEC.md 和两份现有代码，满足两个接口的完整契约。"
    "只有两份 Python 文件可以修改，不要修改规格、检查器或引入依赖。"
    "两部分的边界条件可以独立排查；是否咨询由你根据实际收益决定，不要求派出助手。"
    "主 Agent 负责修改、整合并运行 run_acceptance_checks，最终简洁报告实际结果。",
    ("Batch windows obey WINDOW_SPEC.md and cover each item exactly once.",
     "Retry delays obey RETRY_SPEC.md, including validation and zero/None semantics."),
    "runtime_multi_file_checks.py",
    ("runtime_live_eval_checks.py",),
)

CASE_IDS = (PAGINATION.case_id, EXPORT.case_id)


def get_case(case_id: str) -> LiveCase:
    for case in (PAGINATION, EXPORT):
        if case.case_id == case_id:
            return case
    raise ValueError("unknown frozen pilot case")


def case_prompt(case: LiveCase, *, consultation_probe: bool = False) -> str:
    if not consultation_probe:
        return case.prompt
    if case.case_id != EXPORT.case_id:
        raise ValueError("the consultation probe requires the independent export policies")
    return (
        "这是协作协议专项，不是自主委派收益评测。修复 batch_window.py 和 retry_delay.py，满足三份规格。"
        "请先用 consult_agents 一次提交两项独立的只读 explore 咨询："
        "第一项读取 SPEC.md、WINDOW_SPEC.md、batch_window.py，分析窗口边界及修复建议；"
        "第二项读取 SPEC.md、RETRY_SPEC.md、retry_delay.py，分析重试边界及修复建议。"
        "两项均为 required，不允许助手修改文件，也不要改用同步 task。"
        "主 Agent 等待 Runtime 交回两份结果后，亲自修改两份 Python 文件、运行 run_acceptance_checks 并整合交付。"
        "不能修改规格、检查器或引入依赖；不得把未取得的建议、失败的检查说成成功。"
    )
