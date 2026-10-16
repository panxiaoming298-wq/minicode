"""Resolve explicit review instructions locally, never from model/tool text.

This deliberately recognizes bounded directives, rather than interpreting every
mention of review as a preference. Ambiguous prose leaves admission unchanged.
"""

import re

from ..Dto.ReviewDirective import ReviewDirectiveRequest


_REVIEW_ZH = r"(?:审查|评审|审核|复核|审阅|验证|验收|核验)"
_AGENT_ZH = r"(?:子\s*agent|sub[\s-]*agent|agent|智能体)"
_TARGET = re.compile(
    rf"独立(?:的)?\s*(?:子\s*agent\s*)?{_REVIEW_ZH}"
    rf"|{_REVIEW_ZH}\s*{_AGENT_ZH}"
    rf"|{_AGENT_ZH}\s*(?:独立(?:的)?\s*)?{_REVIEW_ZH}"
    r"|\bindependent\s+(?:code\s+)?(?:review|verification|audit)\b"
    r"|\b(?:review(?:er)?|verification|audit)\s+(?:sub[\s-]*)?agents?\b"
    r"|\bfinal\s+verifier\b", re.I)
_DISABLE_ZH = r"(?:关闭|关掉|禁用|停用|取消|跳过|省略|停止)"
_ENABLE_ZH = r"(?:开启|打开|启用|安排|进行|执行|使用|要求|需要)"
_NEGATE_ZH = r"(?:不要|不得|不能|不应|不允许|无需|无须|不需要|不适用|不使用|不用|禁止|请勿|勿|别|不)"
_DISABLE_EN = r"(?:disable|skip|omit|cancel|stop|turn\s+off)"
_ENABLE_EN = r"(?:enable|require|perform|run|use|turn\s+on)"
_NEGATE_EN = r"(?:do\s+not|don['’]t|never|must\s+not)"
_ACTION = re.compile(
    rf"(?P<keep>{_NEGATE_ZH}\s*(?:再|直接)?\s*{_DISABLE_ZH}|\b{_NEGATE_EN}\s+(?:ever\s+)?{_DISABLE_EN}\b)"
    rf"|(?P<negated>{_NEGATE_ZH}\s*(?:{_ENABLE_ZH})?|\b{_NEGATE_EN}\s+{_ENABLE_EN}\b)"
    rf"|(?P<off>{_DISABLE_ZH}|\b{_DISABLE_EN}\b|\b(?:without|no)\b)"
    rf"|(?P<on>{_ENABLE_ZH}|恢复|保留|保持|必须|强制|让|请|\b{_ENABLE_EN}\b|\b(?:keep|retain|restore|request)\b)", re.I)
_MENTION = re.compile(
    r"解释|说明|讲解|讨论|总结|比较|介绍|探讨|含义|意思|区别|示例|例如|比如|文案|按钮|提示词"
    r"|如果|假如|假设|是否|会不会|是不是|请问|问一下|有人|他说|她说|用户要求|^(?:当(?!前)|若)"
    r"|\b(?:explain|describe|discuss|meaning|example|quote|wording|button|if|when|unless|whether|suppose)\b"
    r"|\b(?:he|she|they)\s+(?:said|says)\b", re.I)
_FEATURE = re.compile(
    r"(?:实现|开发|支持|添加|写入|写上|显示|包含)\s*(?:一个|可以|能够)?\s*$"
    r"|\b(?:implement|support|add|display|print|write)\s+"
    r"(?:(?:a|the)\s+(?:command|feature|option|ability|way)\s+(?:to\s+)?)?$", re.I)
_FEATURE_SUFFIX = re.compile(r"^\s*(?:的)?(?:按钮|文案|示例|提示词|指令解析)")
_QUESTION = re.compile(r"什么意思|是什么意思|是什么|是吗|对吗|怎么办|怎么|如何"
                       r"|(?:的|有什么)(?:作用|影响|区别|好处|优缺点|必要性|原因|说明)"
                       r"|\b(?:what|why|how|does|would\s+it)\b", re.I)
_REQUEST_QUESTION = re.compile(r"请|麻烦|帮我|帮忙|能帮|可以帮"
                               r"|\b(?:please|can\s+you|could\s+you|would\s+you)\b", re.I)
_QUOTED = re.compile(r'"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’|(?<!\w)\x27[^\x27\n]*\x27(?!\w)|`[^`\n]*`')
_BOLD = re.compile(r"(\*\*|__)([^\n]*?)\1")
_MODE = re.compile(r"(?<![A-Za-z0-9_])(?:standard|deep|ultra)(?![A-Za-z0-9_])", re.I)
_COMMAND_JOIN = re.compile(r"并且|并|同时|以及|和|而|但|\b(?:and|while|whereas)\b", re.I)
_HEADER_PREFIX = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)、]\s*)?")
_SCOPE = re.compile(
    r"(?P<conversation>本次(?:对话|会话)|(?:整个|本|这(?:一)?(?:个|次)|当前)(?:对话|会话)"
    r"|以后|今后|之后|后续|接下来|一直|始终|长期|持续|每(?:一)?次|所有(?:后续)?任务"
    r"|\b(?:from\s+now\s+on|going\s+forward|always|every\s+(?:run|turn|task)"
    r"|all\s+(?:runs|turns|tasks)|(?:this|current|entire)\s+(?:conversation|chat|session))\b)"
    r"|(?P<run>本次|这(?:一)?次|此次|本轮|这(?:一)?轮"
    r"|(?:本|当前(?:这(?:个|次|一(?:个|次|项)))?|这(?:一)?(?:个|项)|此次|这(?:一)?次)(?:任务|运行|请求)"
    r"|单次|一次性|(?:仅|只)(?:限)?一次"
    r"|\b(?:(?:this|current)\s+(?:run|turn|task|request)|once|one[- ](?:off|time))\b)", re.I)


def _scope_hint(text: str) -> str | None:
    hints = list(_SCOPE.finditer(text))
    return hints[-1].lastgroup if hints else None


def _scope_only(text: str) -> str | None:
    text = _HEADER_PREFIX.sub("", text)
    text = re.sub(r"^\s*(\*\*|__)(.*?)\1\s*$", r"\2", text)
    scope = _scope_hint(text)
    if scope and re.fullmatch(
        r"\s*(?:(?:仅限|只限|只针对|仅针对|仅对|只对|只在|仅在|对于|仅|只|限|在|的|模式|任务|运行|请求|都|内|中|里|而已|生效|有效|for|only|just|and|和|与|[&/])\s*)*[:：]?\s*",
        _SCOPE.sub("", _MODE.sub("", text)), re.I,
    ):
        return scope
    return None


def _feature_request(text: str) -> bool:
    # Scope words between an implementation verb and the quoted feature's
    # action do not turn that feature request into an instruction to execute it.
    unscoped = _SCOPE.sub("", text)
    unscoped = re.sub(r"(?:仅限|只限|只针对|仅针对|只在|仅在|仅|只)\s*$", "", unscoped)
    return bool(_FEATURE.search(unscoped))


def _review_clauses(text: str):
    # A comma-separated scope header/qualifier belongs to the directive beside
    # it: 本次，关闭审查agent / 关闭审查agent，仅限本次.
    message_scope, message_modes = "conversation", ()
    for sentence in re.split(r"[\n;；。.！!]|但是|不过|然后|\bbut\b|\bthen\b", text, flags=re.I):
        # A standalone heading governs following lines in this human message.
        # Comma qualifiers below stay local instead of becoming headings.
        heading = _scope_only(sentence)
        if heading:
            message_scope, message_modes = heading, tuple(_MODE.findall(sentence))
            continue
        clauses = re.split(r"[,，]", sentence)
        inherited_scope, inherited_modes = message_scope, message_modes
        for index, clause in enumerate(clauses):
            header = _scope_only(clause)
            if header:
                inherited_scope = header
                inherited_modes = tuple(_MODE.findall(clause)) or message_modes
                continue
            qualifier = _scope_only(clauses[index + 1]) if index + 1 < len(clauses) else None
            qualifier_modes = tuple(_MODE.findall(clauses[index + 1])) if qualifier else ()
            yield clause, inherited_scope, qualifier, qualifier_modes or inherited_modes


def _target_contexts(clause: str, targets):
    previous_end = 0
    for index, target in enumerate(targets):
        next_start = targets[index + 1].start() if index + 1 < len(targets) else len(clause)
        suffix = clause[target.end():next_start]
        prefix = clause[previous_end:target.start()]
        next_prefix = target.end()
        if index + 1 < len(targets):
            joins = list(_COMMAND_JOIN.finditer(suffix))
            if joins:
                join = joins[-1]
                next_prefix = target.end() + join.end()
                suffix = suffix[:join.start()]
        yield prefix, suffix
        previous_end = next_prefix


class ResolveReviewDirective:
    def execute(self, request: ReviewDirectiveRequest) -> bool | None:
        # Do not turn code samples, quoted instructions or Markdown blockquotes
        # into authority. Removing them also preserves unrelated real directives.
        text = re.sub(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?(?:^\s*\1[^\n]*(?:\n|$)|\Z)", " ", request.text)
        text = re.sub(r"(?m)^\s*>.*$", " ", text)
        text = _QUOTED.sub(" ", text)
        text = _BOLD.sub(r"\2", text)
        preferences: dict[str, bool | None] = {"conversation": None, "run": None}
        for clause, inherited_scope, qualifier, inherited_modes in _review_clauses(text):
            targets = list(_TARGET.finditer(clause))
            if targets:
                first_prefix = clause[:targets[0].start()]
                first_actions = list(_ACTION.finditer(first_prefix))
                if (_MENTION.search(first_prefix) or _feature_request(first_prefix)
                    or (first_actions and _feature_request(first_prefix[:first_actions[-1].start()]))):
                    continue
            for prefix, suffix in _target_contexts(clause, targets):
                modes = _MODE.findall(prefix) or _MODE.findall(suffix) or inherited_modes
                if modes and request.mode not in {mode.lower() for mode in modes}:
                    continue
                if (_MENTION.search(prefix) or _feature_request(prefix)
                    or _FEATURE_SUFFIX.search(suffix) or _QUESTION.search(prefix + suffix)):
                    continue
                if re.search(r"[?？]", clause) and not _REQUEST_QUESTION.search(prefix):
                    continue
                # Mode/action/scope belong to this target's local command.
                scope = qualifier or _scope_hint(prefix + suffix) or inherited_scope
                if scope == "run" and not request.include_run_scoped:
                    continue
                setting = re.match(r"\s*(?:[:：=]|设为|设置为)\s*", suffix)
                if setting:
                    action = _ACTION.match(suffix, setting.end())
                    if action:
                        preferences[scope] = action.lastgroup in {"keep", "on"}
                        continue
                actions = list(_ACTION.finditer(prefix))
                if actions:
                    action = actions[-1]
                    before, bridge = prefix[:action.start()], prefix[action.end():]
                    if len(bridge) > 48 or _feature_request(before):
                        continue
                    if re.search(r"(?:不是|并非|并不是|没有要求)\s*(?:要|要求)?$", before):
                        continue
                    # Negation can govern an intervening pronoun/helper verb:
                    # 不需要你安排独立审查 / don't use an agent to perform review.
                    negated = any(a.lastgroup == "negated" for a in actions[:-1])
                    preferences[scope] = action.lastgroup in {"keep", "on"} and not negated
                else:
                    # Also accept a direct setting: 独立审查：关闭.
                    setting = re.match(r"\s*(?:[:：=]|设为|设置为)?\s*", suffix)
                    action = _ACTION.match(suffix, setting.end())
                    if action and not _MENTION.search(prefix) and not _feature_request(prefix):
                        preferences[scope] = action.lastgroup in {"keep", "on"}
        # A one-run exception overrides the conversation setting for this run,
        # regardless of the order in which those two scopes were stated.
        if request.include_run_scoped and preferences["run"] is not None:
            return preferences["run"]
        return preferences["conversation"]
