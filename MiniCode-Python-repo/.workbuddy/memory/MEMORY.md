# MiniCode Project Memory

## Project Overview
MiniCode is a terminal coding assistant (Python port of Claude Code concept).
- Source: `d:\Desktop\minicode\py-src\minicode\`
- Python 3.11+, uses venv at `d:\Desktop\minicode\py-src\.venv`
- Entry: `minicode.main:main`

## Key Architecture (post 2026-04-10 cleanup)
- **Core loop**: main.py → agent_loop.py → tooling.py → model adapters
- **Model adapters**: anthropic_adapter.py, openai_adapter.py (via model_registry.py)
- **MCP**: mcp.py — MCP server integration for external tools
- **TUI**: tui/ — terminal UI (chrome, input_handler, markdown, state, transcript)
- **Config**: config.py — settings loading with Claude Code compat fallback
- **Session**: session.py — persistence and resume (AutosaveManager, SessionData)
- **State**: state.py — Zustand-style store + global singleton + handle_state_command
- **Memory**: memory.py — cross-session knowledge retention
- **UserProfile**: user_profile.py — USER.md preferences system (global + project merge)
- **Permissions**: permissions.py — uses auto_mode.py for risk classification
- **Context**: context_manager.py — context window management

## Tools (52 registered)
ask_user, list_files, grep_files, read_file, write_file, modify_file, edit_file, patch_file, batch_copy, batch_move, batch_delete, run_command, web_fetch, web_search, http_request, json_format, json_parse, regex_test, regex_replace, base64_encode, base64_decode, url_encode, url_decode, current_time, timestamp_convert, hash, hmac, gzip_compress, gzip_decompress, tar_create, tar_extract, zip_create, zip_extract, csv_parse, csv_create, uuid_generate, text_sort, text_dedupe, text_join, line_count, random_string, todo_write, task, git, find_symbols, find_references, get_ast_info, code_review, file_tree, diff_viewer, test_runner, load_skill

## 2026-04-10 Cleanup (Two Rounds)
### First Round: Delete dead/island modules (18 files, ~208KB)
- `sub_agents.py` (15KB) — sub-agent framework, zero references
- `gateway.py` (18KB) — API gateway, zero references
- `async_context.py` (8KB) — async context, zero references
- `context_collector.py` (16KB) — context collector, zero references
- `cost_integration.py` (7KB) — cost integration, zero external references
- `cron_runner.py` (12KB) — scheduled tasks, not integrated
- `poly_commands.py` (13KB) — polymorphic commands, imported but never used
- `task_tracker.py` (13KB) — only referenced by poly_commands
- `tooling_enhanced.py` (22KB) — enhanced tool protocol, only referenced by cost_integration
- `governance_audit.py` (14KB) — governance audit, zero references
- `state_integration.py` — merged into state.py
- Removed half-baked tools: db_explorer, docker_helper, api_tester, notebook_edit, run_with_debug, governance_audit_tool, multi_edit

### Second Round: Additional cleanup
- Removed unused import in tty_app.py (list_background_tasks)
- Fixed skills.py Windows OSError bug
- Deleted platform_adapters.py (31KB) — complete platform adapter framework (Telegram/Discord/Slack) but never activated

### Third Round: Interactive module optimization
- tty_app.py: removed ~18 unused imports (run_agent_turn, find_matching_slash_commands, build_system_prompt, various unused chrome/render functions, etc.)

### Result after Two Rounds
- Before: 86 files, 916KB, 28 tools
- After: 67 files, 676KB, 22 tools
- Reduced: 19 files, ~240KB (-26%)

## User Preferences
- Language: Chinese (中文)
- Prefers direct action over lengthy discussion
- Values code quality over feature quantity