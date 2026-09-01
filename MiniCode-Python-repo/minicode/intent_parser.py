"""Intent Parser - Structured user intent parsing layer.

Inspired by: raw material -> clean expression -> task path -> target skill
Transforms user input into stable intent objects before routing.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from minicode.logging_config import get_logger

logger = get_logger("intent_parser")


class IntentType(str, Enum):
    CODE = "code"
    DEBUG = "debug"
    REFACTOR = "refactor"
    EXPLAIN = "explain"
    SEARCH = "search"
    REVIEW = "review"
    TEST = "test"
    DOCUMENT = "document"
    CONFIGURE = "configure"
    QUESTION = "question"
    CHAT = "chat"
    MEMORY = "memory"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class ActionType(str, Enum):
    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    EXECUTE = "execute"
    ANALYZE = "analyze"
    COMPARE = "compare"
    MERGE = "merge"
    SPLIT = "split"
    MOVE = "move"
    RENAME = "rename"
    UNKNOWN = "unknown"


_CODE_PATTERNS = [
    (r"(?:write|create|implement|add|generate)\s+(?:a|an|the)?\s*(?:function|class|method|module|component|page|api)", IntentType.CODE, ActionType.CREATE),
    (r"(?:modify|update|change|fix)\s+(?:code|file|function|class|method)", IntentType.CODE, ActionType.UPDATE),
    (r"(?:implement|complete|develop)\s+(?:feature|task|requirement)", IntentType.CODE, ActionType.CREATE),
]

_DEBUG_PATTERNS = [
    (r"(?:debug|fix|solve|resolve|troubleshoot)\s+(?:error|bug|issue|problem|exception)", IntentType.DEBUG, ActionType.ANALYZE),
    (r"(?:what|why)\s+(?:is|does)\s+(?:wrong|error|fail|broken)", IntentType.DEBUG, ActionType.ANALYZE),
]

_REFACTOR_PATTERNS = [
    (r"(?:refactor|optimize|improve|clean|simplify|restructure)\s+(?:code|structure|logic|design)", IntentType.REFACTOR, ActionType.UPDATE),
]

_EXPLAIN_PATTERNS = [
    (r"(?:explain|describe|tell|what is|how to|how does)", IntentType.EXPLAIN, ActionType.READ),
]

_SEARCH_PATTERNS = [
    (r"(?:search|find|locate|lookup)\s+(?:file|code|function|class|variable|reference)", IntentType.SEARCH, ActionType.READ),
]

_REVIEW_PATTERNS = [
    (r"(?:review|check|audit|inspect)\s+(?:code|file|implementation|design)", IntentType.REVIEW, ActionType.ANALYZE),
]

_TEST_PATTERNS = [
    (r"(?:test|verify|run|execute)\s+(?:test|code|program|script|case)", IntentType.TEST, ActionType.EXECUTE),
]

_DOCUMENT_PATTERNS = [
    (r"(?:document|comment|write)\s+(?:docs?|comment|README|documentation)", IntentType.DOCUMENT, ActionType.CREATE),
]

_CONFIGURE_PATTERNS = [
    (r"(?:configure|setup|install|init)", IntentType.CONFIGURE, ActionType.UPDATE),
]

_MEMORY_PATTERNS = [
    (r"(?:remember|memory|memorize|/memory|# remember)", IntentType.MEMORY, ActionType.CREATE),
]

_SYSTEM_PATTERNS = [
    (r"^(?:/|!)(?:exit|quit|bye|clear|reset|help|settings|config|model|mode)", IntentType.SYSTEM, ActionType.EXECUTE),
]

_ALL_PATTERNS = (
    _SYSTEM_PATTERNS + _MEMORY_PATTERNS + _CODE_PATTERNS + _DEBUG_PATTERNS +
    _REFACTOR_PATTERNS + _EXPLAIN_PATTERNS + _SEARCH_PATTERNS +
    _REVIEW_PATTERNS + _TEST_PATTERNS + _DOCUMENT_PATTERNS + _CONFIGURE_PATTERNS
)


@dataclass
class ParsedIntent:
    raw_input: str
    intent_type: IntentType
    action_type: ActionType
    confidence: float
    entities: dict[str, list[str]] = field(default_factory=dict)
    keywords: list[str] = field(default_factory=list)
    complexity_hint: str = "moderate"
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_input": self.raw_input,
            "intent_type": self.intent_type.value,
            "action_type": self.action_type.value,
            "confidence": self.confidence,
            "entities": self.entities,
            "keywords": self.keywords,
            "complexity_hint": self.complexity_hint,
            "timestamp": self.timestamp,
        }

    def is_code_related(self) -> bool:
        return self.intent_type in {
            IntentType.CODE, IntentType.DEBUG, IntentType.REFACTOR,
            IntentType.REVIEW, IntentType.TEST,
        }

    def is_read_only(self) -> bool:
        return self.action_type in {ActionType.READ, ActionType.ANALYZE}


class IntentParser:
    def __init__(self):
        self._pattern_cache: list[tuple[re.Pattern, IntentType, ActionType]] = []
        self._compile_patterns()

    def _compile_patterns(self) -> None:
        for pattern, intent, action in _ALL_PATTERNS:
            try:
                self._pattern_cache.append((re.compile(pattern, re.IGNORECASE), intent, action))
            except re.error:
                logger.warning("Invalid pattern: %s", pattern)

    def parse(self, user_input: str) -> ParsedIntent:
        if not user_input or not user_input.strip():
            return ParsedIntent(
                raw_input=user_input,
                intent_type=IntentType.UNKNOWN,
                action_type=ActionType.UNKNOWN,
                confidence=0.0,
            )

        text = user_input.strip()
        intent_type, action_type, match_confidence = self._match_patterns(text)
        entities = self._extract_entities(text)
        keywords = self._extract_keywords(text)
        complexity = self._estimate_complexity(text, intent_type, keywords)
        confidence = self._adjust_confidence(match_confidence, entities, keywords)

        return ParsedIntent(
            raw_input=text,
            intent_type=intent_type,
            action_type=action_type,
            confidence=confidence,
            entities=entities,
            keywords=keywords,
            complexity_hint=complexity,
        )

    def _match_patterns(self, text: str) -> tuple[IntentType, ActionType, float]:
        best_intent = IntentType.UNKNOWN
        best_action = ActionType.UNKNOWN
        best_score = 0.0

        for pattern, intent, action in self._pattern_cache:
            match = pattern.search(text)
            if match:
                score = 1.0 - (match.start() / max(len(text), 1)) * 0.3
                if score > best_score:
                    best_score = score
                    best_intent = intent
                    best_action = action

        return best_intent, best_action, best_score

    def _extract_entities(self, text: str) -> dict[str, list[str]]:
        entities: dict[str, list[str]] = {"files": [], "functions": [], "classes": [], "languages": []}

        file_pattern = re.compile(r"\b([\w/\\._-]+\.(?:py|js|ts|jsx|tsx|java|go|rs|cpp|c|h|md|json|yaml|yml|toml))\b", re.I)
        for m in file_pattern.finditer(text):
            if m.group(1) not in entities["files"]:
                entities["files"].append(m.group(1))

        func_pattern = re.compile(r"\b(def|fn|func|function)\s+([\w_]+)\b", re.I)
        for m in func_pattern.finditer(text):
            if m.group(2) not in entities["functions"]:
                entities["functions"].append(m.group(2))

        class_pattern = re.compile(r"\bclass\s+([\w_]+)\b", re.I)
        for m in class_pattern.finditer(text):
            if m.group(1) not in entities["classes"]:
                entities["classes"].append(m.group(1))

        lang_pattern = re.compile(r"\b(python|javascript|typescript|java|go|rust|cpp|c\+\+|react|vue)\b", re.I)
        for m in lang_pattern.finditer(text):
            lang = m.group(1).lower()
            if lang not in entities["languages"]:
                entities["languages"].append(lang)

        return entities

    def _extract_keywords(self, text: str) -> list[str]:
        stopwords = {"the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
                     "have", "has", "had", "do", "does", "did", "will", "would", "could",
                     "should", "may", "might", "must", "can", "need", "to", "of", "in",
                     "for", "on", "with", "at", "by", "from", "as", "into", "through",
                     "during", "before", "after", "above", "below", "between", "under",
                     "again", "further", "then", "once", "here", "there", "when", "where",
                     "why", "how", "all", "any", "both", "each", "few", "more", "most",
                     "other", "some", "such", "no", "nor", "not", "only", "own", "same",
                     "so", "than", "too", "very", "just", "and", "but", "if", "or",
                     "because", "until", "while", "this", "that", "these", "those",
                     "i", "me", "my", "we", "our", "you", "your", "he", "him", "his",
                     "she", "her", "it", "its", "they", "them", "their", "what", "which",
                     "who", "whom"}
        words = re.findall(r"[\w\u4e00-\u9fff]+", text.lower())
        keywords = [w for w in words if w not in stopwords and len(w) > 1]
        seen: set[str] = set()
        unique: list[str] = []
        for w in keywords:
            if w not in seen:
                seen.add(w)
                unique.append(w)
        return unique[:20]

    def _estimate_complexity(self, text: str, intent: IntentType, keywords: list[str]) -> str:
        length_score = min(len(text) / 200, 1.0)
        intent_scores = {
            IntentType.CODE: 0.6, IntentType.DEBUG: 0.5, IntentType.REFACTOR: 0.7,
            IntentType.EXPLAIN: 0.3, IntentType.SEARCH: 0.2, IntentType.REVIEW: 0.4,
            IntentType.TEST: 0.4, IntentType.DOCUMENT: 0.3, IntentType.CONFIGURE: 0.3,
            IntentType.QUESTION: 0.2, IntentType.CHAT: 0.1, IntentType.MEMORY: 0.1,
            IntentType.SYSTEM: 0.1, IntentType.UNKNOWN: 0.5,
        }
        intent_score = intent_scores.get(intent, 0.5)
        complex_keywords = {"architect", "design", "framework", "system", "platform",
                            "infrastructure", "orchestrate", "pipeline", "migrate",
                            "integrate", "refactor", "optimize", "performance"}
        keyword_score = sum(1 for k in keywords if k in complex_keywords) / max(len(keywords), 1)
        total = length_score * 0.2 + intent_score * 0.5 + keyword_score * 0.3
        if total < 0.3:
            return "simple"
        elif total < 0.6:
            return "moderate"
        return "complex"

    def _adjust_confidence(self, base: float, entities: dict, keywords: list[str]) -> float:
        confidence = base
        if any(entities.values()):
            confidence += 0.1
        if 3 <= len(keywords) <= 15:
            confidence += 0.05
        return min(1.0, confidence)


_parser: IntentParser | None = None


def get_intent_parser() -> IntentParser:
    global _parser
    if _parser is None:
        _parser = IntentParser()
    return _parser


def parse_intent(user_input: str) -> ParsedIntent:
    return get_intent_parser().parse(user_input)
