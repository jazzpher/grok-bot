"""Backup/restore: consistent snapshots, verified restores, corrupt-archive rejection."""
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from grokbot import backup


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / 'data'
        (self.data / 'owner1' / 'sub').mkdir(parents=True)
        (self.data / 'owner1' / 'hello.txt').write_text('hello world', encoding='utf-8')
        (self.data / 'owner1' / 'sub' / 'note.md').write_text('# note', encoding='utf-8')
        conn = sqlite3.connect(str(self.data / '.conversations.sqlite3'))
        conn.execute('CREATE TABLE convos(id TEXT, title TEXT)')
        conn.execute("INSERT INTO convos VALUES('c1', 'first chat')")
        conn.commit()
        conn.close()
        (self.data / '.conversations.sqlite3-wal').write_bytes(b'wal-bytes')  # must be skipped
        self.zip = Path(self.tmp.name) / 'backup.zip'

    def rows(self, db):
        conn = sqlite3.connect(str(db))
        try:
            return conn.execute('SELECT * FROM convos').fetchall()
        finally:
            conn.close()

    def test_round_trip_restores_files_and_database(self):
        manifest = backup.create_backup(self.data, self.zip)
        self.assertEqual(manifest['databases'], ['.conversations.sqlite3'])
        self.assertEqual(manifest['files'], 3)  # 2 files + 1 database, WAL skipped
        # Damage the original: delete a file, change the database.
        (self.data / 'owner1' / 'hello.txt').unlink()
        conn = sqlite3.connect(str(self.data / '.conversations.sqlite3'))
        conn.execute("UPDATE convos SET title='changed'")
        conn.commit()
        conn.close()
        summary = backup.restore_backup(self.zip, self.data)
        self.assertEqual((self.data / 'owner1' / 'hello.txt').read_text(), 'hello world')
        self.assertEqual(self.rows(self.data / '.conversations.sqlite3'), [('c1', 'first chat')])
        self.assertFalse((self.data / '.conversations.sqlite3-wal').exists())
        saved = Path(summary['previous_data_saved_at'])
        self.assertTrue(saved.is_dir())
        self.assertEqual(self.rows(saved / '.conversations.sqlite3'), [('c1', 'changed')])
        self.assertFalse((saved / 'owner1' / 'hello.txt').exists())  # the damage is in the safety copy

    def test_restore_into_empty_location(self):
        summary = backup.create_backup(self.data, self.zip)
        target = Path(self.tmp.name) / 'fresh'
        out = backup.restore_backup(self.zip, target)
        self.assertEqual((target / 'owner1' / 'sub' / 'note.md').read_text(), '# note')
        self.assertIsNone(out['previous_data_saved_at'])
        self.assertTrue(summary['databases'])

    def test_rejects_non_backup_zip(self):
        plain = Path(self.tmp.name) / 'plain.zip'
        with zipfile.ZipFile(plain, 'w') as zf:
            zf.writestr('readme.txt', 'not a backup')
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(plain, Path(self.tmp.name) / 'x')

    def test_rejects_corrupt_database_inside_backup(self):
        evil = Path(self.tmp.name) / 'evil.zip'
        with zipfile.ZipFile(evil, 'w') as zf:
            zf.writestr(backup.MANIFEST, json.dumps({'format': 1, 'files': 1, 'databases': ['b.sqlite3']}))
            zf.writestr('b.sqlite3', b'this is not sqlite')
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(evil, Path(self.tmp.name) / 'y')
        self.assertFalse((Path(self.tmp.name) / 'y').exists(), 'a rejected backup must not replace anything')

    def test_rejects_path_traversal(self):
        evil = Path(self.tmp.name) / 'traversal.zip'
        with zipfile.ZipFile(evil, 'w') as zf:
            zf.writestr(backup.MANIFEST, json.dumps({'format': 1, 'files': 1, 'databases': []}))
            zf.writestr('../escape.txt', 'bad')
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(evil, Path(self.tmp.name) / 'z')

    def test_backup_of_missing_dir(self):
        with self.assertRaises(backup.BackupError):
            backup.create_backup(Path(self.tmp.name) / 'nope', self.zip)


if __name__ == '__main__':
    unittest.main()
