"""Workspace file tools: read with line ranges, exact edits, tree, glob, grep.

All paths go through ``agent_loop.Workspace.path`` (no escapes, no symlinks)
and all writes go through its atomic ``_write_bytes`` (temp + fsync + replace,
size/count limits, read-only uploads/).
"""
import difflib
import fnmatch
import json
import re

from agent_loop import ToolError as _WorkspaceError, is_upload_path
from grokbot import config
from grokbot.tools import ToolError

TEXT_SUFFIXES = {'.txt', '.md', '.py', '.js', '.ts', '.tsx', '.jsx', '.json', '.yaml', '.yml', '.toml', '.ini',
                 '.cfg', '.html', '.css', '.csv', '.xml', '.sh', '.bat', '.ps1', '.sql', '.rs', '.go', '.java',
                 '.c', '.h', '.cpp', '.hpp', '.rb', '.php', '.swift', '.kt', '.env', '.gitignore', '.cjs', '.mjs',
                 '.svelte', '.vue', '.lock', '.log', '.tex', '.r', '.m', '.scala', '.dart', '.lua', '.pl'}
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist', 'build', '.mypy_cache', '.pytest_cache'}


def _ws(ctx):
    return ctx.workspace


def _rel(ctx, path):
    return path.relative_to(ctx.workspace.root).as_posix()


def _guard(fn):
    def wrapper(ctx, **kw):
        try:
            return fn(ctx, **kw)
        except _WorkspaceError as exc:
            raise ToolError(str(exc)) from exc
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _is_binary(sample: bytes):
    if b'\x00' in sample:
        return True
    text_chars = bytes(range(32, 127)) + b'\n\r\t\b\f\x1b'
    if not sample:
        return False
    nontext = sample.translate(None, text_chars)
    return len(nontext) / len(sample) > 0.30


def _read_text(ctx, path):
    if path.suffix.lower() == '.docx':
        from agent_loop import extract_text
        try:
            return extract_text(path.name, path.read_bytes())
        except Exception as exc:  # noqa: BLE001 - document parsers raise many types
            raise ToolError('Not a readable .docx (' + type(exc).__name__ + ').') from exc
    if path.suffix.lower() == '.pdf':
        from agent_loop import extract_text
        try:
            return extract_text(path.name, path.read_bytes())
        except Exception as exc:  # noqa: BLE001
            raise ToolError('Not a readable PDF (' + type(exc).__name__ + ').') from exc
    raw = path.read_bytes()
    if _is_binary(raw[:4096]):
        raise ToolError('Binary file (%d bytes); read_file only shows text. Use run_command for binary inspection.' % len(raw))
    return raw.decode('utf-8', errors='replace')


TEST_NAME_GLOBS = ('test_*.py', 'test_*.cjs', 'test_*.js', 'test_*.ts', '*_test.py', '*_test.go',
                   'conftest.py', 'pytest.ini', 'tox.ini')


def is_test_path(rel):
    """True for a path that looks like a test file or lives in a tests/ directory."""
    rel = str(rel).replace('\\', '/').lstrip('./')
    parts = rel.split('/')
    name = parts[-1]
    if any(fnmatch.fnmatch(name.lower(), pattern) for pattern in TEST_NAME_GLOBS):
        return True
    return any(part.lower() in ('test', 'tests', '__tests__', 'spec', 'specs') for part in parts[:-1])


def _tests_guard(ctx, path):
    """Refuse to edit tests when GROK_TESTS_READ_ONLY=1 (benchmark integrity).

    Live runs showed a small model "fixing" a failing test by editing the test's
    ``except KeyError`` clause instead of the buggy code. This is opt-in because adding
    or updating tests is normal work; it covers the file tools, not shell writes.
    """
    if not config.TESTS_READ_ONLY:
        return
    rel = _rel(ctx, _ws(ctx).path(path))
    if is_test_path(rel):
        raise ToolError('Refusing to modify %s: GROK_TESTS_READ_ONLY=1 protects test files, so a run cannot '
                        'make a failing test pass by changing the test. Fix the code instead (or unset the '
                        'variable if editing tests is really the task).' % rel)


def _mutation_approval(ctx, arguments):
    policy = ctx.policy
    if policy is not None and policy.mode == 'read-only':
        return 'Sandbox is read-only; this call modifies workspace files.'
    return None


def _write_result(ctx, rel, content):
    lines = content.count('\n') + (1 if content and not content.endswith('\n') else 0)
    return 'Wrote %s (%d bytes, %d lines).' % (rel, len(content.encode('utf-8')), lines)


def register(registry):
    @registry.tool('read_file', 'Read a text file from the workspace with line numbers. Use offset/limit for large files. '
                   'Also extracts text from .docx and .pdf.',
                   {'type': 'object', 'properties': {
                       'path': {'type': 'string', 'description': 'Relative workspace path'},
                       'offset': {'type': 'integer', 'description': '1-based first line (default 1)'},
                       'limit': {'type': 'integer', 'description': 'Max lines to return (default 400, max 2000)'}},
                    'required': ['path']}, category='files')
    @_guard
    def read_file(ctx, path, offset=1, limit=400):
        p = _ws(ctx).path(path)
        if not p.is_file():
            raise ToolError('No such file: ' + path)
        if p.stat().st_size > config.MAX_READ_BYTES:
            raise ToolError('File is larger than the read limit (%d KB); use grep or run_command (head/sed) instead.' % (config.MAX_READ_BYTES // 1024))
        text = _read_text(ctx, p)
        lines = text.split('\n')
        if lines and lines[-1] == '':
            lines.pop()
        total = len(lines)
        offset = max(1, int(offset or 1))
        limit = max(1, min(int(limit or 400), 2000))
        chunk = lines[offset - 1: offset - 1 + limit]
        width = len(str(min(total, offset + len(chunk))))
        body = '\n'.join(str(offset + i).rjust(width) + '| ' + line for i, line in enumerate(chunk))
        rel = _rel(ctx, p)
        _ws(ctx).checked.add(rel)
        header = '%s (lines %d-%d of %d)' % (rel, offset, offset + len(chunk) - 1 if chunk else offset, total)
        if offset + len(chunk) - 1 < total:
            header += ' — call again with offset=%d for more' % (offset + len(chunk))
        return header + '\n' + body

    @registry.tool('write_file', 'Create or overwrite a UTF-8 text file in the workspace. For small edits to an existing file prefer edit_file. '
                   'Never use this for .docx (use write_docx).',
                   {'type': 'object', 'properties': {
                       'path': {'type': 'string'}, 'content': {'type': 'string'}},
                    'required': ['path', 'content']}, category='files', mutates=True, needs_approval=_mutation_approval)
    @_guard
    def write_file(ctx, path, content):
        _tests_guard(ctx, path)
        _ws(ctx).execute({'action': 'write_file', 'path': path, 'content': content})
        rel = _rel(ctx, _ws(ctx).path(path))
        return _write_result(ctx, rel, content)

    @registry.tool('edit_file', 'Replace an exact text snippet in an existing file. old_text must match exactly once '
                   '(include enough surrounding lines to be unique) unless replace_all is true. Returns a diff.',
                   {'type': 'object', 'properties': {
                       'path': {'type': 'string'}, 'old_text': {'type': 'string'}, 'new_text': {'type': 'string'},
                       'replace_all': {'type': 'boolean'}},
                    'required': ['path', 'old_text', 'new_text']}, category='files', mutates=True, needs_approval=_mutation_approval)
    @_guard
    def edit_file(ctx, path, old_text, new_text, replace_all=False):
        _tests_guard(ctx, path)
        p = _ws(ctx).path(path)
        if not p.is_file():
            raise ToolError('No such file: ' + path + '. Use write_file to create it.')
        if is_upload_path(_rel(ctx, p)):
            raise ToolError('Uploaded documents are read-only. Write a separate output file.')
        if p.suffix.lower() in ('.docx', '.pdf'):
            raise ToolError('edit_file works on text files only.')
        if not old_text:
            raise ToolError('old_text must not be empty.')
        before = _read_text(ctx, p)
        count = before.count(old_text)
        if count == 0:
            # Help the model recover: show the closest line.
            probe = old_text.strip().split('\n')[0][:80]
            hint = ''
            if probe:
                matches = difflib.get_close_matches(probe, [line.strip() for line in before.split('\n')], n=1, cutoff=0.6)
                if matches:
                    hint = ' Closest line: ' + json.dumps(matches[0][:160])
            raise ToolError('old_text was not found in ' + path + '. Read the file and copy the exact text.' + hint)
        if count > 1 and not replace_all:
            raise ToolError('old_text matches %d places in %s. Include more context or set replace_all=true.' % (count, path))
        after = before.replace(old_text, new_text) if replace_all else before.replace(old_text, new_text, 1)
        _ws(ctx).execute({'action': 'write_file', 'path': _rel(ctx, p), 'content': after})
        diff = list(difflib.unified_diff(before.split('\n'), after.split('\n'), fromfile=path, tofile=path, lineterm='', n=2))
        added = sum(1 for d in diff[2:] if d.startswith('+'))
        removed = sum(1 for d in diff[2:] if d.startswith('-'))
        shown = '\n'.join(diff[:80]) + ('\n…' if len(diff) > 80 else '')
        return 'Edited %s (%d replacement%s, +%d -%d lines).\n%s' % (path, count if replace_all else 1, '' if (1 if not replace_all else count) == 1 else 's', added, removed, shown)

    @registry.tool('list_files', 'List workspace files as a tree with sizes.',
                   {'type': 'object', 'properties': {
                       'path': {'type': 'string', 'description': 'Subdirectory (default: workspace root)'},
                       'depth': {'type': 'integer', 'description': 'Max depth (default 3)'}},
                    'required': []}, category='files')
    @_guard
    def list_files(ctx, path='', depth=3):
        root = _ws(ctx).root if not path or path in ('.', '/') else _ws(ctx).path(path)
        if not root.is_dir():
            raise ToolError('No such directory: ' + path)
        depth = max(1, min(int(depth or 3), 8))
        lines, count, total = [], 0, 0
        limit = 400

        def walk(d, prefix, level):
            nonlocal count, total
            try:
                entries = sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
            except OSError:
                return
            for e in entries:
                if e.is_symlink() or e.name in SKIP_DIRS and e.is_dir():
                    continue
                if count >= limit:
                    return
                count += 1
                if e.is_dir():
                    lines.append(prefix + e.name + '/')
                    if level < depth:
                        walk(e, prefix + '  ', level + 1)
                else:
                    try:
                        size = e.stat().st_size
                    except OSError:
                        size = 0
                    total += size
                    lines.append(prefix + e.name + '  (' + _human(size) + ')')
        walk(root, '', 1)
        head = (_rel(ctx, root) if root != _ws(ctx).root else '.') + '/'
        tail = ('\n…[%d entries shown; narrow with path]' % limit) if count >= limit else ''
        return head + '\n' + ('\n'.join(lines) if lines else '(empty)') + tail + '\n%d entries, %s' % (count, _human(total))

    @registry.tool('glob', 'Find files by glob pattern, e.g. "**/*.py" or "src/**/test_*.js".',
                   {'type': 'object', 'properties': {'pattern': {'type': 'string'}}, 'required': ['pattern']},
                   category='files')
    @_guard
    def glob(ctx, pattern):
        if not pattern or pattern.startswith('/') or '..' in pattern.split('/'):
            raise ToolError('Use a relative glob pattern without "..".')
        out = []
        for p in _ws(ctx).root.glob(pattern):
            if p.is_symlink() or not p.is_file():
                continue
            if any(part in SKIP_DIRS for part in p.relative_to(_ws(ctx).root).parts[:-1]):
                continue
            out.append(_rel(ctx, p))
            if len(out) >= 500:
                break
        out.sort()
        return json.dumps(out) if out else 'No files match ' + pattern

    @registry.tool('grep', 'Search file contents with a regular expression. Returns path:line: text matches.',
                   {'type': 'object', 'properties': {
                       'pattern': {'type': 'string'}, 'path': {'type': 'string', 'description': 'File or directory (default root)'},
                       'include': {'type': 'string', 'description': 'Filename glob filter, e.g. *.py'},
                       'ignore_case': {'type': 'boolean'}, 'max_results': {'type': 'integer'}},
                    'required': ['pattern']}, category='files')
    @_guard
    def grep(ctx, pattern, path='', include='', ignore_case=False, max_results=100):
        try:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise ToolError('Invalid regular expression: ' + str(exc))
        base = _ws(ctx).root if not path or path in ('.', '/') else _ws(ctx).path(path)
        if not base.exists():
            raise ToolError('No such path: ' + path)
        max_results = max(1, min(int(max_results or 100), 500))
        files = [base] if base.is_file() else sorted(p for p in base.rglob('*') if p.is_file() and not p.is_symlink())
        results, scanned = [], 0
        for p in files:
            rel = _rel(ctx, p)
            if any(part in SKIP_DIRS for part in p.relative_to(_ws(ctx).root).parts[:-1]):
                continue
            if include and not fnmatch.fnmatch(p.name, include):
                continue
            try:
                if p.stat().st_size > config.MAX_READ_BYTES:
                    continue
                raw = p.read_bytes()
            except OSError:
                continue
            if _is_binary(raw[:4096]):
                continue
            scanned += 1
            for number, line in enumerate(raw.decode('utf-8', errors='replace').split('\n'), 1):
                if rx.search(line):
                    results.append('%s:%d: %s' % (rel, number, line.strip()[:300]))
                    if len(results) >= max_results:
                        return '\n'.join(results) + '\n…[stopped at %d matches; refine the pattern]' % max_results
            if scanned >= 5000:
                break
        return '\n'.join(results) if results else 'No matches for /%s/ in %d files.' % (pattern, scanned)

    @registry.tool('copy_file', 'Copy a workspace file byte-for-byte (use for exact copies instead of retyping).',
                   {'type': 'object', 'properties': {'source_path': {'type': 'string'}, 'path': {'type': 'string'}},
                    'required': ['source_path', 'path']}, category='files', mutates=True, needs_approval=_mutation_approval)
    @_guard
    def copy_file(ctx, source_path, path):
        _tests_guard(ctx, path)
        return _ws(ctx).execute({'action': 'copy_file', 'source_path': source_path, 'path': path})

    @registry.tool('compare_files', 'Compare two workspace files byte-for-byte. Only MATCH means identical.',
                   {'type': 'object', 'properties': {'source_path': {'type': 'string'}, 'path': {'type': 'string'}},
                    'required': ['source_path', 'path']}, category='files')
    @_guard
    def compare_files(ctx, source_path, path):
        return _ws(ctx).execute({'action': 'compare_files', 'source_path': source_path, 'path': path})

    @registry.tool('write_docx', 'Write a real Word .docx document. Lines starting with "# " become headings; one paragraph per line.',
                   {'type': 'object', 'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}},
                    'required': ['path', 'content']}, category='files', mutates=True, needs_approval=_mutation_approval)
    @_guard
    def write_docx(ctx, path, content):
        _tests_guard(ctx, path)
        return _ws(ctx).execute({'action': 'write_docx', 'path': path, 'content': content})

    @registry.tool('delete_file', 'Delete a workspace file (not uploads).',
                   {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']},
                   category='files', mutates=True,
                   needs_approval=lambda ctx, a: _mutation_approval(ctx, a) or 'Deleting a file is irreversible.')
    @_guard
    def delete_file(ctx, path):
        _tests_guard(ctx, path)
        p = _ws(ctx).path(path)
        rel = _rel(ctx, p)
        if is_upload_path(rel):
            raise ToolError('Uploaded documents cannot be deleted by the agent.')
        if not p.is_file():
            raise ToolError('No such file: ' + path)
        p.unlink()
        _ws(ctx).written.discard(rel)
        _ws(ctx).checked.discard(rel)
        return 'Deleted ' + rel


def _human(size):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return ('%d %s' % (size, unit)) if unit == 'B' else ('%.1f %s' % (size, unit))
        size /= 1024.0
    return str(size)
