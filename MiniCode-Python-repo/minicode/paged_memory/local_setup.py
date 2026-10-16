"""Select a fresh local data root without copying remote runtime data."""
from __future__ import annotations

import os
from pathlib import Path

from minicode.config import MINI_CODE_DIR, MINI_CODE_SETTINGS_PATH, read_settings_file, save_mini_code_settings
from minicode.path_safety import is_unsafe_stored_path
from minicode.store_lock import store_lock

from .config import PagedMemoryConfig
from .sqlite_store import SqliteMemoryStore


def configure_local_storage(data_root: str) -> dict:
    root = Path(data_root).expanduser()
    if not root.is_absolute() or root.resolve() == Path(root.anchor):
        raise ValueError('data root must be an absolute directory, not a filesystem root')
    if is_unsafe_stored_path(root, boundary=Path(root.anchor)):
        raise ValueError('data root must not follow filesystem aliases')
    root = root.resolve()
    changes = {
        'memoryDataRoot': str(root), 'memoryBackend': 'sqlite', 'sessionHistoryBackend': 'sqlite',
        'memoryV2': True, 'memoryShadowWrite': False, 'artifactStoreType': 'file',
        'memorySqlitePath': '', 'artifactStoreRoot': '', 'memoryExportRoot': '',
        'memoryBackupRoot': '', 'memoryLocalSpoolRoot': '',
    }
    storage_keys = {'MINI_CODE_DATA_ROOT', 'MINI_CODE_HISTORY_BACKEND', 'MEMORY_BACKEND',
                    'MEMORY_V2_ENABLED', 'MEMORY_SHADOW_WRITE', 'MEMORY_SQLITE_PATH',
                    'ARTIFACT_STORE_TYPE', 'ARTIFACT_STORE_ROOT', 'MEMORY_EXPORT_ROOT',
                    'MEMORY_BACKUP_ROOT', 'MEMORY_LOCAL_SPOOL_ROOT'}
    with store_lock(MINI_CODE_DIR / '.web-server.lock', timeout=0):
        current = read_settings_file(MINI_CODE_SETTINGS_PATH)
        # Settings merge env maps; explicitly clear old storage overrides while
        # preserving provider credentials and every unrelated setting.
        changes['env'] = {key: '' for key in storage_keys if key in current.get('env', {})}
        config = PagedMemoryConfig.from_sources({**current, **changes}, environ={})
        # An inherited process override must not silently select a different authority.
        effective = PagedMemoryConfig.from_sources({**current, **changes})
        if any(getattr(effective, field) != getattr(config, field) for field in
               ('backend', 'sqlite_path', 'artifact_store_type', 'artifact_root', 'data_root',
                'export_root', 'backup_root', 'local_spool_root', 'enabled', 'shadow_write')):
            raise ValueError('process storage overrides conflict with local setup; remove those overrides first')
        if os.environ.get('MINI_CODE_HISTORY_BACKEND', 'sqlite') not in {'sqlite', 'auto'}:
            raise ValueError('MINI_CODE_HISTORY_BACKEND conflicts with SQLite setup')
        for directory in ('database', 'artifacts', 'exports', 'backups', 'spool', 'sessions'):
            (root / directory).mkdir(parents=True, exist_ok=True)
        marker = root / 'sessions/.history-backend.json'
        if marker.is_file():
            import json
            if json.loads(marker.read_text(encoding='utf-8')).get('backend') != 'sqlite':
                raise ValueError('destination is already bound to another history backend; select a new directory')
        store = SqliteMemoryStore(config.sqlite_path)
        try:
            if not store.health_check():
                raise RuntimeError('local database health check failed')
        finally:
            store.close()
        save_mini_code_settings(changes)
    return {'configured': True, 'data_root': str(root), 'sqlite_path': config.sqlite_path,
            'artifact_root': config.artifact_root, 'export_root': config.export_root,
            'settings_path': str(MINI_CODE_SETTINGS_PATH), 'remote_data_migrated': False}
