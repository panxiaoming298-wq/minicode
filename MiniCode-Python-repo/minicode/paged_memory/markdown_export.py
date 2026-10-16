"""One-way, human-readable projections. Generated files are never import sources."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Mapping

from .config import PagedMemoryConfig

EXPORT_MARKER = '<!-- minicode:generated-memory-export:v1 -->'


def is_generated_export(text: str) -> bool:
    return EXPORT_MARKER in text[:8192]


def is_export_read(call: Mapping, output: str, *, root: str, cwd: Path) -> bool:
    if is_generated_export(output):
        return True
    inputs = call.get('input') or call.get('arguments') or {}
    if not isinstance(inputs, Mapping):
        return False
    for key in ('path', 'file_path', 'filePath', 'file'):
        value = inputs.get(key)
        if not isinstance(value, str) or not value:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = cwd / path
        if path.resolve().is_relative_to(Path(root).resolve()):
            return True
    return False


def _time(value) -> str:
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat() if value else '-'


def _block(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    fence = '```'
    while fence in text:
        fence += '`'
    return f'{fence}\n{text}\n{fence}'


def render_markdown(config: PagedMemoryConfig, *, tenant_id: str, task_id: str | None = None,
                    workspace_id: str | None = None) -> dict:
    """Read one consistent projection without creating files or changing storage."""
    if config.backend != 'sqlite':
        raise ValueError('Markdown export requires the SQLite authority')
    database = Path(config.sqlite_path).resolve()
    lines = [EXPORT_MARKER, '# MiniCode 记忆查看副本',
             '> 本文件由 SQLite 单向导出。修改不会回写，下次导出将覆盖；请在应用中修改记忆。',
             '> 人工规则请维护在项目 AGENTS.md 等源文件中；本文件不参与自动记忆采集。',
             f'- 导出时间：{datetime.now(timezone.utc).isoformat()}',
             f'- 数据库：{database}', f'- 租户：{tenant_id}']
    connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    count = 0
    try:
        connection.execute('BEGIN')
        query = "SELECT * FROM agent_task WHERE tenant_id=? AND workspace_id NOT LIKE 'history:%'"
        args = [tenant_id]
        if task_id is not None:
            query += ' AND id=?'
            args.append(task_id)
        if workspace_id is not None:
            query += ' AND workspace_id=?'
            args.append(workspace_id)
        tasks = connection.execute(query + ' ORDER BY workspace_id, created_at, id', args).fetchall()
        for task in tasks:
            params = (tenant_id, task['id'])
            lines += [f"## 任务 {task['id']}", f"- 项目：{task['workspace_id']}",
                      f"- 状态：{task['status']}；更新时间：{_time(task['updated_at'])}", _block(task['goal'])]
            state = connection.execute('SELECT * FROM agent_task_state WHERE tenant_id=? AND task_id=?', params).fetchone()
            if state:
                lines += [f"### 运行状态（版本 {state['version']}）", _block({key: state[key] for key in
                          ('phase', 'current_step_id', 'pending_steps', 'completed_steps', 'open_errors')})]
            for row in connection.execute('SELECT * FROM agent_decision WHERE tenant_id=? AND task_id=? AND status<>? ORDER BY created_at,id', (*params, 'TOMBSTONED')):
                lines += [f"### 决策 {row['decision_key']}（{row['status']}）",
                          f"- ID：{row['id']}；来源事件：{row['source_event_id']}；更新时间：{_time(row['updated_at'])}",
                          _block(row['decision_text'])]
            for row in connection.execute('SELECT * FROM agent_memory WHERE tenant_id=? AND task_id=? AND status<>? ORDER BY created_at,id', (*params, 'TOMBSTONED')):
                count += 1
                lines += [f"### {row['title']}（{row['memory_type']} / {row['status']}）",
                          f"- ID：{row['id']}；更新时间：{_time(row['updated_at'])}",
                          f"- 来源事件：{row['source_event_id']}；原文文件：{row['artifact_id'] or '-'}",
                          f"- 原文范围：{row['source_unit']} {row['source_start']}:{row['source_end']}；哈希：{row['content_hash']}",
                          _block(row['preview_text'])]
        if not tasks:
            lines.append('当前范围没有任务或记忆。')
    finally:
        connection.close()
    return {'content': '\n\n'.join(lines) + '\n', 'task_count': len(tasks),
            'memory_count': count, 'read_only_projection': True}


def export_markdown(config: PagedMemoryConfig, *, tenant_id: str, task_id: str | None = None,
                    workspace_id: str | None = None) -> dict:
    projection = render_markdown(config, tenant_id=tenant_id, task_id=task_id, workspace_id=workspace_id)
    output_root = Path(config.export_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    scope = json.dumps([tenant_id, task_id, workspace_id], ensure_ascii=False)
    destination = output_root / ('memory-' + hashlib.sha256(scope.encode()).hexdigest()[:20] + '.md')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n', dir=output_root,
                                         prefix='.export-', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(projection['content'])
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {'path': str(destination), **{key: value for key, value in projection.items() if key != 'content'}}
