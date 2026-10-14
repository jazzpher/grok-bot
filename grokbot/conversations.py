"""Multiple conversations per session with full-text search.

Two views are stored per conversation:

* ``entries``  — what the user saw (user text, assistant Markdown and the tool /
                 approval / compaction events of each turn) for rendering history.
* ``context``  — the model-facing message list (possibly compacted) plus the
                 running summary, so the next turn continues where this one ended.
"""
import json
import pathlib
import secrets
import sqlite3
import threading
import time

MAX_EVENTS_PER_ENTRY = 200
MAX_EVENT_OUTPUT = 20000
TITLE_LENGTH = 60


class ConversationStore:
    def __init__(self, database):
        self.path = pathlib.Path(database)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA busy_timeout=5000')
        self.fts = False
        self._schema()

    def _schema(self):
        with self.lock:
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL, updated REAL NOT NULL, pinned INTEGER NOT NULL DEFAULT 0,
                    summary TEXT NOT NULL DEFAULT '', context TEXT NOT NULL DEFAULT '[]', model TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS conversations_owner ON conversations(owner, pinned DESC, updated DESC);
                CREATE TABLE IF NOT EXISTS conversation_entries (
                    conv_id TEXT NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
                    events TEXT NOT NULL DEFAULT '[]', created REAL NOT NULL, PRIMARY KEY (conv_id, seq));
            ''')
            try:
                self.db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS conversation_fts USING fts5(conv_id UNINDEXED, seq UNINDEXED, content)')
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    # ----- conversations -------------------------------------------------
    def create(self, owner, title=''):
        cid = secrets.token_hex(8)
        now = time.time()
        with self.lock:
            self.db.execute('INSERT INTO conversations (id, owner, title, created, updated) VALUES (?,?,?,?,?)',
                            (cid, owner, title[:200], now, now))
        return cid

    def get(self, cid, owner):
        with self.lock:
            row = self.db.execute('SELECT * FROM conversations WHERE id=? AND owner=?', (cid, owner)).fetchone()
        return dict(row) if row else None

    def list(self, owner, limit=200):
        with self.lock:
            rows = self.db.execute('''SELECT c.id, c.title, c.created, c.updated, c.pinned,
                                             (SELECT COUNT(*) FROM conversation_entries e WHERE e.conv_id=c.id) AS entries
                                      FROM conversations c WHERE owner=? ORDER BY pinned DESC, updated DESC LIMIT ?''',
                                   (owner, limit)).fetchall()
        return [dict(r) for r in rows]

    def latest(self, owner):
        """Most recently updated conversation row for ``owner`` (same shape as ``get``), or None."""
        with self.lock:
            row = self.db.execute('SELECT id FROM conversations WHERE owner=? ORDER BY updated DESC LIMIT 1', (owner,)).fetchone()
        return self.get(row['id'], owner) if row else None

    def rename(self, cid, owner, title):
        with self.lock:
            cur = self.db.execute('UPDATE conversations SET title=? WHERE id=? AND owner=?', (str(title)[:200], cid, owner))
        return cur.rowcount == 1

    def pin(self, cid, owner, pinned):
        with self.lock:
            cur = self.db.execute('UPDATE conversations SET pinned=? WHERE id=? AND owner=?', (1 if pinned else 0, cid, owner))
        return cur.rowcount == 1

    def delete(self, cid, owner):
        with self.lock:
            if not self.get(cid, owner):
                return False
            self.db.execute('BEGIN')
            try:
                self.db.execute('DELETE FROM conversation_entries WHERE conv_id=?', (cid,))
                if self.fts:
                    self.db.execute('DELETE FROM conversation_fts WHERE conv_id=?', (cid,))
                self.db.execute('DELETE FROM conversations WHERE id=? AND owner=?', (cid, owner))
                self.db.execute('COMMIT')
            except Exception:
                self.db.execute('ROLLBACK')
                raise
        return True

    # ----- entries (display log) ------------------------------------------
    def entries(self, cid, owner, limit=500):
        if not self.get(cid, owner):
            return []
        with self.lock:
            rows = self.db.execute('SELECT seq, role, content, events, created FROM conversation_entries WHERE conv_id=? ORDER BY seq DESC LIMIT ?',
                                   (cid, limit)).fetchall()
        out = []
        for r in reversed(rows):
            try:
                events = json.loads(r['events'])
            except ValueError:
                events = []
            out.append({'seq': r['seq'], 'role': r['role'], 'content': r['content'], 'events': events, 'created': r['created']})
        return out

    def append_entry(self, cid, owner, role, content, events=None):
        if not self.get(cid, owner):
            raise ValueError('Unknown conversation.')
        trimmed = []
        for e in (events or [])[-MAX_EVENTS_PER_ENTRY:]:
            e = dict(e)
            if isinstance(e.get('output'), str) and len(e['output']) > MAX_EVENT_OUTPUT:
                e['output'] = e['output'][:MAX_EVENT_OUTPUT] + '…'
            trimmed.append(e)
        now = time.time()
        with self.lock:
            self.db.execute('BEGIN')
            try:
                seq = (self.db.execute('SELECT COALESCE(MAX(seq), 0) FROM conversation_entries WHERE conv_id=?', (cid,)).fetchone()[0] or 0) + 1
                self.db.execute('INSERT INTO conversation_entries (conv_id, seq, role, content, events, created) VALUES (?,?,?,?,?,?)',
                                (cid, seq, role, content or '', json.dumps(trimmed, ensure_ascii=False), now))
                if self.fts and content:
                    self.db.execute('INSERT INTO conversation_fts (conv_id, seq, content) VALUES (?,?,?)', (cid, seq, content))
                self.db.execute('UPDATE conversations SET updated=? WHERE id=?', (now, cid))
                row = self.db.execute('SELECT title FROM conversations WHERE id=?', (cid,)).fetchone()
                if role == 'user' and row and not row['title']:
                    self.db.execute('UPDATE conversations SET title=? WHERE id=?', (auto_title(content), cid))
                self.db.execute('COMMIT')
            except Exception:
                self.db.execute('ROLLBACK')
                raise
        return seq

    # ----- model context ---------------------------------------------------
    def get_context(self, cid, owner):
        row = self.get(cid, owner)
        if not row:
            return [], ''
        try:
            return json.loads(row['context']) or [], row['summary'] or ''
        except ValueError:
            return [], row['summary'] or ''

    def set_context(self, cid, owner, messages, summary=''):
        with self.lock:
            cur = self.db.execute('UPDATE conversations SET context=?, summary=?, updated=? WHERE id=? AND owner=?',
                                  (json.dumps(messages, ensure_ascii=False), summary or '', time.time(), cid, owner))
        return cur.rowcount == 1

    def clear_context(self, cid, owner):
        return self.set_context(cid, owner, [], '')

    # ----- search -------------------------------------------------------------
    def search(self, owner, query, limit=20):
        query = (query or '').strip()
        if not query:
            return []
        with self.lock:
            if self.fts:
                safe = ' '.join('"' + term.replace('"', '') + '"' for term in query.split()[:8])
                try:
                    rows = self.db.execute('''SELECT f.conv_id, f.seq, snippet(conversation_fts, 2, '[', ']', '…', 12) AS snippet, c.title
                                              FROM conversation_fts f JOIN conversations c ON c.id=f.conv_id
                                              WHERE conversation_fts MATCH ? AND c.owner=? ORDER BY rank LIMIT ?''',
                                           (safe, owner, limit)).fetchall()
                    return [dict(r) for r in rows]
                except sqlite3.OperationalError:
                    pass
            rows = self.db.execute('''SELECT e.conv_id, e.seq, substr(e.content, 1, 160) AS snippet, c.title
                                      FROM conversation_entries e JOIN conversations c ON c.id=e.conv_id
                                      WHERE c.owner=? AND e.content LIKE ? ORDER BY e.created DESC LIMIT ?''',
                                   (owner, '%' + query + '%', limit)).fetchall()
        return [dict(r) for r in rows]

    def prune(self, age_seconds=90 * 86400):
        cutoff = time.time() - age_seconds
        with self.lock:
            old = [r['id'] for r in self.db.execute('SELECT id FROM conversations WHERE updated<? AND pinned=0', (cutoff,)).fetchall()]
            for cid in old:
                self.db.execute('DELETE FROM conversation_entries WHERE conv_id=?', (cid,))
                if self.fts:
                    self.db.execute('DELETE FROM conversation_fts WHERE conv_id=?', (cid,))
                self.db.execute('DELETE FROM conversations WHERE id=?', (cid,))
        return len(old)

    def close(self):
        with self.lock:
            self.db.close()


def auto_title(text):
    text = ' '.join(str(text or '').split())
    if len(text) <= TITLE_LENGTH:
        return text or 'New chat'
    cut = text[:TITLE_LENGTH].rsplit(' ', 1)[0]
    return (cut or text[:TITLE_LENGTH]) + '…'
