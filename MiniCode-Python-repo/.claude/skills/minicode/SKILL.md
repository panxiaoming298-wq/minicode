# MiniCode — Cybernetic AI Coding Agent

Terminal-first AI coding assistant with closed-loop self-regulation via
engineering cybernetics (15+ controllers: PID ×4, Kalman ×5).

## Quick Start

```bash
python -m minicode.main
```

Mock mode (no API key):
```bash
MINI_CODE_MODEL_MODE=mock python -m minicode.main
```

## Core Capabilities

### Self-Regulating Agent
MiniCode auto-regulates during coding tasks:
- **Context overflow** → auto-compaction (PID-controlled, 4-phase progressive)
- **Tool errors** → auto-healing (8 fault types with recovery strategies)
- **Cost spikes** → budget PID tightens token allocation
- **Agent oscillation** → feedback PID dampens, reduces concurrency
- **Task stalling** → progress controller suggests strategy changes
- **Degraded performance** → signals model upgrade, boosts token budget

### Memory That Learns
- Cross-session memory with 3-layer retrieval pipeline
- Domain-aware search (auto-detects frontend/backend/database/devops)
- LLM-curated memory injection (top-15 → curated top-3 + conflict detection)
- Background curator agent consolidates, validates, and links memories
- Multi-tier storage: WORKING → SHORT_TERM → LONG_TERM → ARCHIVAL

### Terminal Experience
- TUI with real-time transcript, diff coloring, permission prompts
- 30 built-in tools (file ops, code search, git, web, testing, batch)
- 26 discoverable skills
- MCP server integration
- Session persistence with autosave

## Slash Commands

| Command | Description |
|---------|-------------|
| `/help` | Show all commands |
| `/memory` | Memory system status (tiers, domains, insights) |
| `/context` | Context window usage |
| `/cybernetics` | Controller health dashboard |
| `/skills` | List discoverable skills |
| `/config-paths` | Show config file locations |
| `/permissions` | Show permission store location |
| `/mcp` | List MCP servers and tools |
| `/exit` | Save session and exit |

## Configuration

`~/.mini-code/settings.json`:
```json
{
  "model": "claude-sonnet-4-20250514",
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "your-token"
  }
}
```

Environment variables:
- `MINICODE_MODEL_TIMEOUT` — API timeout in seconds (default: 60)
- `MINICODE_TOOL_TIMEOUT` — Tool execution timeout (default: 120)
- `MINI_CODE_MODEL_MODE=mock` — Run without API key

## Architecture

```
User Input → Intent Parser → Task Object → Pipeline Plan → Agent Loop
                                                              │
              ┌───────────────────────────────────────────────┤
              │                                               │
         Sense (sensors) → Control (PID×4, Kalman×5) → Act (tools, budget)
              │                                               │
              └─────────── Feedback (dual-PID) ←──────────────┘
```

## Memory Pipeline

```
Task + Files → DomainClassifier → BM25 + SparseVector(RRF) → Value(rel×fresh×util)
  → LLM Reranker (top-15 → top-3 + summary) → Spreading Activation → Inject
```

Ablation: P@3 0.35→0.72, Noise 65%→7% (80 memories × 20 queries)

## Testing

```bash
pytest  # 737 passed, 2 skipped
```
