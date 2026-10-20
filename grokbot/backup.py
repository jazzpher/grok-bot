"""Backup and restore of the grok-bot data directory (workspaces + SQLite stores).

``create_backup`` writes a zip: every ``*.sqlite3`` database is snapshotted through the
SQLite backup API (consistent even while the server is running), everything else is copied
as-is, plus a manifest so ``restore_backup`` can verify what it is about to load.

``restore_backup`` verifies the archive first (zip readable, every database passes
``PRAGMA integrity_check``), extracts into a staging directory next to the target, then
swaps directories: the current data dir is kept at ``<name>.pre-restore-<timestamp>``
instead of being deleted, so a bad restore is itself undoable.
"""
import json
import os
import pathlib
import shutil
import sqlite3
import time
import zipfile

MANIFEST = 'grokbot-backup.json'
FORMAT_VERSION = 1
SKIP_SUFFIXES = ('.sqlite3-wal', '.sqlite3-shm', '.sqlite3-journal')


class BackupError(Exception):
    pass


def default_data_dir():
    """Where workspaces, the session secret and the SQLite stores live by default.

    A fixed per-user directory, not the current working directory: starting the app
    from another folder must not open a different (empty) set of conversations.
    """
    env = os.environ.get('GROK_WORKSPACE_DIR', '').strip()
    if env:
        return pathlib.Path(env).expanduser()
    if os.name == 'nt':
        base = pathlib.Path(os.environ.get('LOCALAPPDATA') or pathlib.Path.home() / 'AppData' / 'Local')
        return base / 'grokbot' / 'workspaces'
    base = pathlib.Path(os.environ.get('XDG_DATA_HOME', '').strip() or pathlib.Path.home() / '.local' / 'share')
    return base / 'grokbot' / 'workspaces'


def _snapshot_db(src, dst):
    """Consistent copy of a live SQLite database via the backup API."""
    source = sqlite3.connect('file:%s?mode=ro' % src, uri=True)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _check_db(path):
    try:
        conn = sqlite3.connect('file:%s?mode=ro' % path, uri=True)
        try:
            rows = conn.execute('PRAGMA integrity_check').fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError('Database %s is not a readable SQLite database (%s).' % (path.name, exc))
    if not rows or rows[0][0] != 'ok':
        raise BackupError('Database %s failed its integrity check: %s' % (path.name, rows[:1]))


def create_backup(data_dir, out_path, extra_files=None):
    """Zip ``data_dir`` (plus optional extra files like settings.json) to ``out_path``.

    Returns a summary dict. Databases are snapshotted through SQLite so the backup is
    consistent even while the server is writing; the server's own pre-restore safety
    copies are skipped.
    """
    data_dir = pathlib.Path(data_dir)
    if not data_dir.is_dir():
        raise BackupError('Data directory not found: %s' % data_dir)
    out_path = pathlib.Path(out_path)
    staging = out_path.parent / ('.backup-staging-%d' % os.getpid())
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        files = 0
        databases = []
        for path in sorted(data_dir.rglob('*')):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(data_dir)
            if any(part.endswith('.pre-restore') or '.pre-restore-' in part for part in rel.parts):
                continue
            if path.name.endswith(SKIP_SUFFIXES) or path.name == MANIFEST:
                continue
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == '.sqlite3':
                _snapshot_db(path, dest)
                databases.append(rel.as_posix())
            else:
                shutil.copyfile(path, dest)
            files += 1
        for label, extra in (extra_files or {}).items():
            extra = pathlib.Path(extra)
            if extra.is_file():
                shutil.copyfile(extra, staging / label)
                files += 1
        manifest = {'format': FORMAT_VERSION, 'created': time.time(),
                    'created_iso': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                    'files': files, 'databases': databases}
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        for rel in databases:
            _check_db(staging / rel)
        if out_path.exists():
            out_path.unlink()
        with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(staging.rglob('*')):
                if path.is_file():
                    zf.write(path, path.relative_to(staging).as_posix())
        manifest['out'] = str(out_path)
        manifest['bytes'] = out_path.stat().st_size
        return manifest
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def read_manifest(zip_path):
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            if MANIFEST not in names:
                raise BackupError('%s is not a grok-bot backup (no %s inside).' % (zip_path, MANIFEST))
            bad = zf.testzip()
            if bad:
                raise BackupError('Backup archive is corrupt (first bad file: %s).' % bad)
            return json.loads(zf.read(MANIFEST).decode('utf-8')), names
    except zipfile.BadZipFile:
        raise BackupError('Backup archive is corrupt (not a readable zip): %s' % zip_path)


def restore_backup(zip_path, data_dir, keep_old=True):
    """Restore ``zip_path`` over ``data_dir``. Verifies BEFORE touching anything, then swaps
    directories so the previous data survives at ``<name>.pre-restore-<timestamp>``.
    Returns a summary dict with the safety-copy location."""
    zip_path = pathlib.Path(zip_path)
    data_dir = pathlib.Path(data_dir)
    manifest, _ = read_manifest(zip_path)
    if manifest.get('format') != FORMAT_VERSION:
        raise BackupError('Unsupported backup format: %r' % manifest.get('format'))
    staging = data_dir.parent / ('.restore-staging-%d' % os.getpid())
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                # Path-traversal guard: only plain relative paths may be extracted.
                name = info.filename
                if name.startswith(('/', '\\')) or '..' in pathlib.PurePosixPath(name).parts:
                    raise BackupError('Unsafe path inside backup: %r' % name)
            zf.extractall(staging)
        for rel in manifest.get('databases', []):
            _check_db(staging / rel)
        target_exists = data_dir.exists()
        stamp = time.strftime('%Y%m%d-%H%M%S')
        old_copy = data_dir.parent / (data_dir.name + '.pre-restore-' + stamp)
        if target_exists:
            os.rename(data_dir, old_copy)
        try:
            os.rename(staging, data_dir)
        except Exception:
            if target_exists:
                os.rename(old_copy, data_dir)   # roll back: never leave no data dir behind
            raise
        if not keep_old and old_copy.exists():
            shutil.rmtree(old_copy, ignore_errors=True)
        return {'restored_to': str(data_dir), 'files': manifest.get('files'),
                'databases': manifest.get('databases'),
                'previous_data_saved_at': str(old_copy) if target_exists and old_copy.exists() else None,
                'backup_created': manifest.get('created_iso')}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
