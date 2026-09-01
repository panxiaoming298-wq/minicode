"""Domain classifier for memory relevance filtering.

Derives active domains from file extensions, task intent keywords,
and user-provided domain tags. Used by the memory injection pipeline
to boost relevant memories and suppress cross-domain noise.

域分类并非硬加权（hard ×1.5），而是软混合（soft blend）：
  final_score = bm25 * 0.7 + domain_jaccard * 0.3
"""
from __future__ import annotations

import re
from collections import defaultdict
from enum import Enum


class DomainType(str, Enum):
    FRONTEND = "frontend"
    BACKEND = "backend"
    DATABASE = "database"
    DEVOPS = "devops"
    TESTING = "testing"
    SECURITY = "security"
    DATA_SCIENCE = "data_science"
    MOBILE = "mobile"
    EMBEDDED = "embedded"
    GENERAL = "general"


# ── File extension → domain mapping ────────────────────────────────

FILE_EXT_DOMAIN_MAP: dict[str, list[tuple[DomainType, float]]] = {
    # Frontend
    ".tsx": [(DomainType.FRONTEND, 1.0)],
    ".jsx": [(DomainType.FRONTEND, 1.0)],
    ".vue": [(DomainType.FRONTEND, 1.0)],
    ".svelte": [(DomainType.FRONTEND, 1.0)],
    ".css": [(DomainType.FRONTEND, 0.9)],
    ".scss": [(DomainType.FRONTEND, 0.9)],
    ".less": [(DomainType.FRONTEND, 0.9)],
    ".html": [(DomainType.FRONTEND, 0.8)],
    ".htm": [(DomainType.FRONTEND, 0.8)],
    # Backend
    ".py": [(DomainType.BACKEND, 0.7), (DomainType.DATA_SCIENCE, 0.3)],
    ".go": [(DomainType.BACKEND, 1.0)],
    ".rs": [(DomainType.BACKEND, 1.0)],
    ".java": [(DomainType.BACKEND, 0.9)],
    ".kt": [(DomainType.BACKEND, 0.7), (DomainType.MOBILE, 0.6)],
    ".rb": [(DomainType.BACKEND, 0.9)],
    ".php": [(DomainType.BACKEND, 0.9)],
    ".cs": [(DomainType.BACKEND, 0.8)],
    ".scala": [(DomainType.BACKEND, 0.8)],
    ".ex": [(DomainType.BACKEND, 0.8)],
    ".exs": [(DomainType.BACKEND, 0.8)],
    # Database
    ".sql": [(DomainType.DATABASE, 1.0)],
    ".prisma": [(DomainType.DATABASE, 0.9)],
    ".graphql": [(DomainType.DATABASE, 0.5), (DomainType.BACKEND, 0.5)],
    # DevOps
    ".tf": [(DomainType.DEVOPS, 1.0)],
    ".tfvars": [(DomainType.DEVOPS, 0.9)],
    ".yml": [(DomainType.DEVOPS, 0.6), (DomainType.BACKEND, 0.2)],
    ".yaml": [(DomainType.DEVOPS, 0.6), (DomainType.BACKEND, 0.2)],
    "dockerfile": [(DomainType.DEVOPS, 1.0)],
    "Dockerfile": [(DomainType.DEVOPS, 1.0)],
    ".dockerignore": [(DomainType.DEVOPS, 0.7)],
    "Makefile": [(DomainType.DEVOPS, 0.7)],
    # Testing
    ".test.ts": [(DomainType.TESTING, 1.0), (DomainType.FRONTEND, 0.3)],
    ".test.tsx": [(DomainType.TESTING, 1.0), (DomainType.FRONTEND, 0.3)],
    ".test.js": [(DomainType.TESTING, 1.0), (DomainType.FRONTEND, 0.3)],
    ".spec.ts": [(DomainType.TESTING, 1.0), (DomainType.FRONTEND, 0.3)],
    ".spec.py": [(DomainType.TESTING, 1.0), (DomainType.BACKEND, 0.3)],
    ".test.py": [(DomainType.TESTING, 1.0), (DomainType.BACKEND, 0.3)],
    "_test.go": [(DomainType.TESTING, 1.0), (DomainType.BACKEND, 0.3)],
    # Security
    ".pem": [(DomainType.SECURITY, 0.9)],
    ".env": [(DomainType.SECURITY, 0.5)],
    ".env.example": [(DomainType.SECURITY, 0.3)],
    # Data Science
    ".ipynb": [(DomainType.DATA_SCIENCE, 1.0)],
    ".r": [(DomainType.DATA_SCIENCE, 0.9)],
    ".pkl": [(DomainType.DATA_SCIENCE, 0.6)],
    ".parquet": [(DomainType.DATA_SCIENCE, 0.5)],
    # Mobile
    ".swift": [(DomainType.MOBILE, 1.0)],
    ".m": [(DomainType.MOBILE, 0.8)],
    ".mm": [(DomainType.MOBILE, 0.8)],
    # Embedded
    ".c": [(DomainType.EMBEDDED, 0.6), (DomainType.BACKEND, 0.3)],
    ".cpp": [(DomainType.EMBEDDED, 0.5), (DomainType.BACKEND, 0.4)],
    ".h": [(DomainType.EMBEDDED, 0.5), (DomainType.BACKEND, 0.3)],
}

# ── Intent keyword → domain mapping ────────────────────────────────

INTENT_KW_DOMAIN_MAP: dict[str, list[tuple[DomainType, float]]] = {
    # Frontend keywords
    "react": [(DomainType.FRONTEND, 1.0)],
    "vue": [(DomainType.FRONTEND, 1.0)],
    "component": [(DomainType.FRONTEND, 0.8)],
    "css": [(DomainType.FRONTEND, 1.0)],
    "style": [(DomainType.FRONTEND, 0.7)],
    "ui": [(DomainType.FRONTEND, 0.9)],
    "ux": [(DomainType.FRONTEND, 0.9)],
    "browser": [(DomainType.FRONTEND, 0.8)],
    "dom": [(DomainType.FRONTEND, 0.8)],
    "render": [(DomainType.FRONTEND, 0.7)],
    "template": [(DomainType.FRONTEND, 0.6)],
    # Backend keywords
    "api": [(DomainType.BACKEND, 0.9)],
    "endpoint": [(DomainType.BACKEND, 0.9)],
    "server": [(DomainType.BACKEND, 0.9)],
    "http": [(DomainType.BACKEND, 0.7)],
    "grpc": [(DomainType.BACKEND, 0.9)],
    "middleware": [(DomainType.BACKEND, 0.8)],
    "queue": [(DomainType.BACKEND, 0.7)],
    "worker": [(DomainType.BACKEND, 0.6)],
    "cron": [(DomainType.BACKEND, 0.6)],
    "cache": [(DomainType.BACKEND, 0.5)],
    # Database keywords
    "database": [(DomainType.DATABASE, 1.0)],
    "migration": [(DomainType.DATABASE, 1.0)],
    "schema": [(DomainType.DATABASE, 0.9)],
    "query": [(DomainType.DATABASE, 0.9)],
    "sql": [(DomainType.DATABASE, 1.0)],
    "orm": [(DomainType.DATABASE, 0.8)],
    "index": [(DomainType.DATABASE, 0.6)],
    "transaction": [(DomainType.DATABASE, 0.8)],
    # DevOps keywords
    "deploy": [(DomainType.DEVOPS, 1.0)],
    "ci": [(DomainType.DEVOPS, 1.0)],
    "cd": [(DomainType.DEVOPS, 1.0)],
    "pipeline": [(DomainType.DEVOPS, 0.9)],
    "docker": [(DomainType.DEVOPS, 1.0)],
    "kubernetes": [(DomainType.DEVOPS, 1.0)],
    "k8s": [(DomainType.DEVOPS, 1.0)],
    "infrastructure": [(DomainType.DEVOPS, 0.9)],
    "terraform": [(DomainType.DEVOPS, 1.0)],
    "monitor": [(DomainType.DEVOPS, 0.7)],
    # Testing keywords
    "test": [(DomainType.TESTING, 0.8)],
    "debug": [(DomainType.TESTING, 0.5), (DomainType.GENERAL, 0.5)],
    "mock": [(DomainType.TESTING, 0.7)],
    "stub": [(DomainType.TESTING, 0.6)],
    "assert": [(DomainType.TESTING, 0.7)],
    "coverage": [(DomainType.TESTING, 0.8)],
    # Security keywords
    "auth": [(DomainType.SECURITY, 0.8), (DomainType.BACKEND, 0.4)],
    "token": [(DomainType.SECURITY, 0.7), (DomainType.BACKEND, 0.4)],
    "jwt": [(DomainType.SECURITY, 0.9), (DomainType.BACKEND, 0.3)],
    "oauth": [(DomainType.SECURITY, 0.9)],
    "permission": [(DomainType.SECURITY, 0.7)],
    "encrypt": [(DomainType.SECURITY, 0.9)],
    "hash": [(DomainType.SECURITY, 0.6)],
    "csrf": [(DomainType.SECURITY, 0.9)],
    "xss": [(DomainType.SECURITY, 0.9)],
}

# ── Domain-based search tokens ──────────────────────────────────────

DOMAIN_SEARCH_TOKENS: dict[DomainType, list[str]] = {
    DomainType.FRONTEND: ["react", "vue", "component", "css", "html", "ui", "dom"],
    DomainType.BACKEND: ["api", "server", "endpoint", "http", "middleware", "queue"],
    DomainType.DATABASE: ["sql", "schema", "query", "migration", "orm", "index"],
    DomainType.DEVOPS: ["deploy", "docker", "ci", "pipeline", "kubernetes", "infrastructure"],
    DomainType.TESTING: ["test", "mock", "assert", "coverage", "debug"],
    DomainType.SECURITY: ["auth", "token", "encrypt", "permission", "oauth"],
    DomainType.DATA_SCIENCE: ["model", "train", "data", "pipeline", "notebook"],
    DomainType.MOBILE: ["ios", "android", "swift", "kotlin", "mobile"],
    DomainType.EMBEDDED: ["c", "firmware", "microcontroller", "embedded", "hardware"],
    DomainType.GENERAL: [],
}


def classify(
    current_files: list[str] | None = None,
    intent_text: str = "",
    user_domains: list[str] | None = None,
) -> list[tuple[DomainType, float]]:
    """Derive active domains from file extensions, intent keywords, and user tags.

    File extension signal: weight 0.6
    Intent keyword signal: weight 0.4
    User-provided domain: weight 1.0 (overrides everything)

    Returns sorted list of (domain, confidence) for scores > 0.2.
    """
    scores: defaultdict[DomainType, float] = defaultdict(float)

    # Signal 1: File extensions
    if current_files:
        for fpath in current_files:
            fname = fpath.lower()
            # Exact match first
            for ext, mappings in FILE_EXT_DOMAIN_MAP.items():
                if fname.endswith(ext) or fname == ext.lower():
                    for domain, confidence in mappings:
                        scores[domain] = max(scores[domain], confidence * 0.6)
        # Also check file basename for special files
        for fpath in current_files:
            basename = fpath.split("/")[-1] if "/" in fpath else fpath.split("\\")[-1] if "\\" in fpath else fpath
            for special in ["dockerfile", "makefile", ".dockerignore", ".env"]:
                if basename.lower() == special.lower():
                    for domain, confidence in FILE_EXT_DOMAIN_MAP.get(special, []):
                        scores[domain] = max(scores[domain], confidence * 0.6)

    # Signal 2: Intent keywords
    if intent_text:
        intent_lower = intent_text.lower()
        for keyword, mappings in INTENT_KW_DOMAIN_MAP.items():
            if re.search(r'\b' + re.escape(keyword) + r'\b', intent_lower):
                for domain, confidence in mappings:
                    scores[domain] = max(scores[domain], confidence * 0.4)

    # Signal 3: User-provided domains (overrides)
    if user_domains:
        for ud in user_domains:
            try:
                domain = DomainType(ud.lower())
                scores[domain] = max(scores[domain], 1.0)
            except ValueError:
                pass

    # Filter by threshold and sort
    result = sorted(
        [(d, min(1.0, s)) for d, s in scores.items() if s > 0.2],
        key=lambda x: x[1],
        reverse=True,
    )

    if not result:
        result = [(DomainType.GENERAL, 0.5)]

    return result


def get_active_domain_values(
    current_files: list[str] | None = None,
    intent_text: str = "",
    user_domains: list[str] | None = None,
) -> list[str]:
    """Convenience: return domain value strings for the active domains."""
    return [d.value for d, _ in classify(current_files, intent_text, user_domains)]
