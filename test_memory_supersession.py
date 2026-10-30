"""Memory supersession contract tests (bitemporal corrections).

Adopted from the group's frozen v4 draft (Tab, 2026-10-30) with these deliberate, documented deviations:
- The draft's two auto-prune retention tests are NOT adopted: automatic pruning of history is out of
  scope by the owner's boundary (no retention deletion / automatic pruning); only forget_memory deletes.
- The characterization test pinning forget_memory's old "wrong or outdated" wording is retired; the
  wording change is the fix (supersede_memory now owns the "fact changed" path).
- test_unlinked_duplicate_facts_count_against_owner_cap was renamed: its rows are unlinked duplicates,
  not superseded rows.
- Added: delete-of-superseder restores the predecessor, monotonic recorded_at via the injectable clock.

Part A (CurrentBehaviorTest) pins behavior that must NOT change. Part B (SupersessionContractTest)
pins the new contract. Run: python -m unittest test_memory_supersession -v
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from grokbot.memory import MemoryStore  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / 'm.sqlite3')

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()


class CurrentBehaviorTest(_Base):
    def test_conflicting_corrections_are_both_current_in_search(self):
        self.store.save('o', 'Dev server port is 3000')
        self.store.save('o', 'Dev server port is 8080')
        texts = sorted(m['text'] for m in self.store.search('o', 'dev server port'))
        self.assertEqual(texts, ['Dev server port is 3000', 'Dev server port is 8080'])

    def test_relevant_injects_stale_and_new_value_together(self):
        self.store.save('o', 'Dev server port is 3000')
        self.store.save('o', 'Dev server port is 8080')
        block = MemoryStore.prompt_block(self.store.relevant('o', 'which port does the dev server use'))
        self.assertIn('3000', block)
        self.assertIn('8080', block)

    def test_pinned_stale_fact_is_injected_first(self):
        old = self.store.save('o', 'Dev server port is 3000', pinned=True)
        self.store.save('o', 'Dev server port is 8080')
        rel = self.store.relevant('o', 'dev server port')
        self.assertEqual(rel[0]['id'], old['id'])

    def test_update_overwrites_in_place_and_loses_old_value(self):
        row = self.store.save('o', 'Dev server port is 3000')
        self.store.update('o', row['id'], text='Dev server port is 8080')
        everything = ' '.join(m['text'] for m in self.store.list('o'))
        self.assertNotIn('3000', everything, 'update() keeps legacy overwrite semantics; supersede for history')

    def test_dedupe_is_exact_text_only(self):
        a = self.store.save('o', 'Dev server port is 8080')
        b = self.store.save('o', 'The dev server port is 8080')
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual(self.store.count('o'), 2)

    def test_revert_to_old_text_dedupes_back_onto_original_row(self):
        first = self.store.save('o', 'Dev server port is 3000')
        self.store.save('o', 'Dev server port is 8080')
        back = self.store.save('o', 'Dev server port is 3000')
        self.assertEqual(back['id'], first['id'], 'unkeyed revert still refreshes the live row')
        self.assertEqual(self.store.count('o'), 2)

    def test_like_fallback_also_returns_both_values(self):
        self.store.fts = False   # force the LIKE branch
        self.store.save('o', 'Dev server port is 3000')
        self.store.save('o', 'Dev server port is 8080')
        self.assertEqual(len(self.store.search('o', 'dev server port')), 2)

    def test_same_text_is_one_row_regardless_of_context(self):
        a = self.store.save('o', 'Port is 8080', ['local'])
        b = self.store.save('o', 'Port is 8080', ['staging'])
        self.assertEqual(a['id'], b['id'], 'unkeyed dedup is by text only; tags are overwritten')
        self.assertEqual(self.store.get('o', a['id'])['tags'], ['staging'])


KEY = dict(subject='dev-server', environment='local', attribute='port')


class SupersessionContractTest(_Base):
    def test_explicit_supersede_hides_old_value_from_current_search(self):
        old = self.store.save('o', 'Dev server port is 3000', **KEY)
        new = self.store.save('o', 'Dev server port is 8080', supersedes=old['id'], **KEY)
        self.assertEqual([m['id'] for m in self.store.search('o', 'dev server port')], [new['id']])
        self.assertEqual(self.store.get('o', old['id'])['superseded_by'], new['id'])

    def test_history_returns_all_values_oldest_first(self):
        old = self.store.save('o', 'Dev server port is 3000', effective_from=1000.0, **KEY)
        new = self.store.save('o', 'Dev server port is 8080', effective_from=2000.0, supersedes=old['id'], **KEY)
        hist = self.store.history('o', **KEY)
        self.assertEqual([m['id'] for m in hist], [old['id'], new['id']])
        self.assertEqual(hist[0]['superseded_by'], new['id'])

    def test_different_environment_is_not_superseded(self):
        self.store.save('o', 'Dev server port is 3000 on staging', subject='dev-server', environment='staging', attribute='port')
        self.store.save('o', 'Dev server port is 8080 locally', **KEY)
        texts = sorted(m['text'] for m in self.store.search('o', 'dev server port'))
        self.assertEqual(len(texts), 2)

    def test_ambiguous_same_key_without_supersedes_only_flags_candidate(self):
        old = self.store.save('o', 'Dev server port is 3000', **KEY)
        new = self.store.save('o', 'Dev server port is 8080', **KEY)
        ids = {m['id'] for m in self.store.search('o', 'dev server port')}
        self.assertEqual(ids, {old['id'], new['id']}, 'no auto-supersede')
        self.assertEqual(self.store.get('o', new['id'])['candidate_supersedes'], old['id'])
        self.assertIsNone(self.store.get('o', old['id'])['superseded_by'])

    def test_superseded_pinned_memory_is_not_injected(self):
        old = self.store.save('o', 'Dev server port is 3000', pinned=True, **KEY)
        new = self.store.save('o', 'Dev server port is 8080', supersedes=old['id'], **KEY)
        rel = self.store.relevant('o', 'dev server port')
        self.assertNotIn(old['id'], [m['id'] for m in rel], 'filter runs before the pinned step')
        self.assertIn(new['id'], [m['id'] for m in rel])

    def test_superseded_rows_do_not_consume_top_k(self):
        prev = None
        for p in (3000, 3001, 3002, 3003):
            prev = self.store.save('o', 'Dev server port is %d' % p, supersedes=prev and prev['id'], **KEY)
        current = self.store.save('o', 'Dev server port is 8080', supersedes=prev['id'], **KEY)
        self.assertEqual([m['id'] for m in self.store.search('o', 'dev server port', limit=1)], [current['id']])
        self.assertEqual([m['id'] for m in self.store.relevant('o', 'dev server port', limit=1)], [current['id']])

    def test_supersede_is_owner_scoped(self):
        bobs = self.store.save('bob', 'Dev server port is 3000', **KEY)
        with self.assertRaises(ValueError):
            self.store.save('alice', 'Dev server port is 8080', supersedes=bobs['id'], **KEY)
        self.assertIsNone(self.store.get('bob', bobs['id'])['superseded_by'])

    def test_supersede_rejects_unknown_or_already_superseded_target(self):
        with self.assertRaises(ValueError):
            self.store.save('o', 'Dev server port is 8080', supersedes='nope', **KEY)
        old = self.store.save('o', 'Dev server port is 3000', **KEY)
        self.store.save('o', 'Dev server port is 8080', supersedes=old['id'], **KEY)
        with self.assertRaises(ValueError):
            self.store.save('o', 'Dev server port is 9090', supersedes=old['id'], **KEY)

    def test_revert_creates_new_history_entry_not_old_row(self):
        v1 = self.store.save('o', 'Dev server port is 3000', effective_from=1000.0, **KEY)
        v2 = self.store.save('o', 'Dev server port is 8080', effective_from=2000.0, supersedes=v1['id'], **KEY)
        v3 = self.store.save('o', 'Dev server port is 3000', effective_from=3000.0, supersedes=v2['id'], **KEY)
        self.assertNotEqual(v3['id'], v1['id'], 'dedup must not resurrect the superseded row')
        self.assertEqual([m['id'] for m in self.store.history('o', **KEY)], [v1['id'], v2['id'], v3['id']])
        self.assertEqual([m['id'] for m in self.store.search('o', 'dev server port')], [v3['id']])

    def test_future_effective_correction_is_not_current_yet(self):
        # Contract: current = effective_from <= now; the old value stays current until the new one activates.
        now = time.time()
        old = self.store.save('o', 'Dev server port is 3000', effective_from=now - 100, **KEY)
        new = self.store.save('o', 'Dev server port is 8080', effective_from=now + 86400, supersedes=old['id'], **KEY)
        self.assertEqual([m['id'] for m in self.store.search('o', 'dev server port')], [old['id']])
        self.assertEqual([m['id'] for m in self.store.history('o', **KEY)], [old['id'], new['id']])

    def test_backdated_correction_fixes_history_without_replacing_current(self):
        # Bitemporal: effective_from = when it became true; recorded_at = when we learned it. Keep both.
        cur = self.store.save('o', 'Dev server port is 8080', effective_from=2000.0, **KEY)
        back = self.store.save('o', 'Dev server port was 3000 until switch', effective_from=1000.0, **KEY)
        self.assertEqual([m['id'] for m in self.store.search('o', 'dev server port')], [cur['id']],
                         'an older-effective fact saved later does not become current')
        self.assertEqual([m['id'] for m in self.store.history('o', **KEY)], [back['id'], cur['id']])
        self.assertGreater(self.store.get('o', back['id'])['recorded_at'], self.store.get('o', cur['id'])['recorded_at'])

    def test_same_effective_time_ambiguous_is_not_decided_by_save_order(self):
        a = self.store.save('o', 'Dev server port is 3000', effective_from=1000.0, **KEY)
        b = self.store.save('o', 'Dev server port is 8080', effective_from=1000.0, **KEY)
        ids = {m['id'] for m in self.store.search('o', 'dev server port')}
        self.assertEqual(ids, {a['id'], b['id']}, 'both stay current; flagged, not resolved by save order')
        self.assertEqual(self.store.get('o', b['id'])['candidate_supersedes'], a['id'])

    def test_history_orders_by_effective_from_not_save_time(self):
        b = self.store.save('o', 'Dev server port is 8080', effective_from=2000.0, **KEY)
        late_saved_old = self.store.save('o', 'Dev server port was 3000 before', effective_from=1000.0, **KEY)
        self.assertEqual([m['id'] for m in self.store.history('o', **KEY)], [late_saved_old['id'], b['id']])

    def test_like_fallback_excludes_superseded(self):
        self.store.fts = False
        old = self.store.save('o', 'Dev server port is 3000', **KEY)
        new = self.store.save('o', 'Dev server port is 8080', supersedes=old['id'], **KEY)
        self.assertEqual([m['id'] for m in self.store.search('o', 'dev server port')], [new['id']])

    def test_same_text_under_different_keys_stays_separate(self):
        a = self.store.save('o', 'Port is 8080', subject='api', environment='local', attribute='port')
        b = self.store.save('o', 'Port is 8080', subject='web', environment='local', attribute='port')
        self.assertNotEqual(a['id'], b['id'], 'dedup must include the key')
        self.assertEqual(self.store.count('o'), 2)

    def test_backdated_row_stays_history_after_later_fact_is_superseded(self):
        # Regression (review HIGH): the backdate rule must hold even when the later-effective row
        # is itself superseded; otherwise the stale backdated fact resurfaces as current.
        a = self.store.save('o', 'Dev server port is 8080', effective_from=500.0, **KEY)
        b = self.store.save('o', 'Dev server port was 2000 back then', effective_from=100.0, **KEY)
        self.assertEqual([m['id'] for m in self.store.search('o', 'dev server port')], [a['id']])
        n = self.store.save('o', 'Dev server port is 9090', effective_from=900.0, supersedes=a['id'], **KEY)
        self.assertEqual([m['id'] for m in self.store.search('o', 'dev server port')], [n['id']],
                         'backdated B must not resurface when A is superseded')
        self.assertEqual([m['id'] for m in self.store.history('o', **KEY)], [b['id'], a['id'], n['id']])

    def test_keyless_memories_never_hide_each_other(self):
        # Regression (review HIGH): backdated grouping relates only fully-keyed rows; keyless facts
        # are independent and never hide one another, in search and in relevant().
        self.store.save('o', 'Uses pnpm', effective_from=2000.0)
        t = self.store.save('o', 'Prefers Taglish', effective_from=1000.0)
        ids = {m['id'] for m in self.store.search('o', 'pnpm taglish')}
        self.assertIn(t['id'], ids)
        rel = self.store.relevant('o', 'taglish pnpm')
        self.assertIn(t['id'], [m['id'] for m in rel])

    def test_partial_key_rows_never_hide_each_other(self):
        a = self.store.save('o', 'Uses pnpm for builds', subject='web', effective_from=2000.0)
        b = self.store.save('o', 'Uses yarn for builds', subject='web', effective_from=1000.0)
        ids = {m['id'] for m in self.store.search('o', 'builds')}
        self.assertEqual(ids, {a['id'], b['id']}, 'partial keys get no backdate hiding and no advisory')

    def test_pending_future_predecessor_still_counts_against_cap(self):
        # Regression (review): a predecessor whose replacement is future-dated is still the active
        # fact and must count against the budget until the replacement activates.
        from unittest import mock
        import grokbot.memory as gm
        now = time.time()
        with mock.patch.object(gm, 'MAX_MEMORIES', 3):
            old = self.store.save('o', 'Dev server port is 3000', effective_from=now - 100, **KEY)
            self.store.save('o', 'Dev server port is 8080', effective_from=now + 86400, supersedes=old['id'], **KEY)
            self.store.save('o', 'Uses pnpm')
            with self.assertRaises(ValueError):
                self.store.save('o', 'Uses cargo', ['misc'])

    def test_delete_midchain_row_refused_and_chain_preserved(self):
        a = self.store.save('o', 'Dev server port is 3000', **KEY)
        b = self.store.save('o', 'Dev server port is 8080', supersedes=a['id'], **KEY)
        c = self.store.save('o', 'Dev server port is 9090', supersedes=b['id'], **KEY)
        with self.assertRaises(ValueError):
            self.store.delete('o', b['id'])
        self.assertEqual(self.store.get('o', a['id'])['superseded_by'], b['id'])
        self.assertEqual([m['id'] for m in self.store.search('o', 'dev server port')], [c['id']])
        self.store.delete('o', c['id'])
        self.assertEqual([m['id'] for m in self.store.search('o', 'dev server port')], [b['id']],
                         'forgetting the newest value restores its predecessor')

    def test_dedup_with_conflicting_explicit_effective_from_creates_new_row(self):
        a = self.store.save('o', 'Port is 8080', effective_from=100.0, **KEY)
        b = self.store.save('o', 'Port is 8080', effective_from=200.0, **KEY)
        self.assertNotEqual(a['id'], b['id'], 'a different explicit effective_from is a new event, not a refresh')
        self.assertEqual(self.store.get('o', b['id'])['candidate_supersedes'], a['id'])
        c = self.store.save('o', 'Port is 8080', effective_from=100.0, **KEY)
        self.assertEqual(c['id'], a['id'], 'same text, key and effective time still dedupes')

    def test_update_text_on_superseded_row_refused(self):
        old = self.store.save('o', 'Dev server port is 3000', **KEY)
        self.store.save('o', 'Dev server port is 8080', supersedes=old['id'], **KEY)
        with self.assertRaises(ValueError):
            self.store.update('o', old['id'], text='Dev server port is 4040')
        upd = self.store.update('o', old['id'], pinned=True)
        self.assertTrue(upd['pinned'], 'non-text changes on history rows still allowed')

    def test_list_marks_current_and_history_rows(self):
        old = self.store.save('o', 'Dev server port is 3000', **KEY)
        new = self.store.save('o', 'Dev server port is 8080', supersedes=old['id'], **KEY)
        rows = {m['id']: m for m in self.store.list('o')}
        self.assertFalse(rows[old['id']]['is_current'])
        self.assertTrue(rows[new['id']]['is_current'])

    def test_deleting_superseder_restores_predecessor(self):
        old = self.store.save('o', 'Dev server port is 3000', **KEY)
        new = self.store.save('o', 'Dev server port is 8080', supersedes=old['id'], **KEY)
        self.store.delete('o', new['id'])
        self.assertIsNone(self.store.get('o', old['id'])['superseded_by'])
        self.assertEqual([m['id'] for m in self.store.search('o', 'dev server port')], [old['id']])

    def test_migration_backfills_old_rows_without_behavior_change(self):
        # A database created before supersession: old rows become current facts dated at `created`.
        import sqlite3
        path = Path(self.tmp.name) / 'old.sqlite3'
        db = sqlite3.connect(str(path))
        db.execute("""CREATE TABLE memories (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, text TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]',
            source TEXT NOT NULL DEFAULT 'agent', pinned INTEGER NOT NULL DEFAULT 0,
            created REAL NOT NULL, updated REAL NOT NULL, last_used REAL, uses INTEGER NOT NULL DEFAULT 0)""")
        db.execute("INSERT INTO memories(id, owner, text, tags, created, updated) VALUES ('abc123', 'o', "
                   "'Dev server port is 3000', '[]', 1000.0, 1000.0)")
        db.commit(); db.close()
        store = MemoryStore(path)
        try:
            row = store.get('o', 'abc123')
            self.assertEqual((row['effective_from'], row['recorded_at']), (1000.0, 1000.0))
            self.assertIsNone(row['superseded_by'])
            self.assertEqual([m['id'] for m in store.search('o', 'dev server port')], ['abc123'])
            new = store.save('o', 'Dev server port is 8080', supersedes='abc123',
                             subject='dev-server', environment='local', attribute='port')
            self.assertEqual([m['id'] for m in store.search('o', 'dev server port')], [new['id']])
        finally:
            store.close()

    def test_recorded_at_is_monotonic_on_a_coarse_clock(self):
        # A wall clock that never advances must still produce strictly increasing recorded_at.
        self.store._now = lambda: 1234.5
        a = self.store.save('o', 'Dev server port is 3000', **KEY)
        b = self.store.save('o', 'Dev server port is 8080', **KEY)
        self.assertGreater(self.store.get('o', b['id'])['recorded_at'], self.store.get('o', a['id'])['recorded_at'])


    def test_explicit_backdated_supersede_stays_visible(self):
        # Regression (delta-review HIGH): an explicit supersedes link dominates the backdate rule;
        # a replacement effective EARLIER than its target must not vanish together with the target.
        a = self.store.save('o', 'Dev server port is 8080', effective_from=100.0, **KEY)
        n = self.store.save('o', 'Dev server port is 9090', effective_from=50.0, supersedes=a['id'], **KEY)
        self.assertEqual([m['id'] for m in self.store.search('o', 'dev server port')], [n['id']],
                         'explicit backdated replacement must stay current')
        self.assertEqual([m['id'] for m in self.store.relevant('o', 'dev server port')], [n['id']])
        self.assertEqual([m['id'] for m in self.store.history('o', **KEY)], [n['id'], a['id']])

    def test_explicit_chain_with_unordered_effective_dates(self):
        # Regression (delta-review HIGH): chain dominance is transitive; unordered effective dates
        # inside an explicit chain must not hide the terminal row.
        a = self.store.save('o', 'Server build uses alpha', effective_from=100.0, **KEY)
        b = self.store.save('o', 'Server build uses beta', effective_from=200.0, supersedes=a['id'], **KEY)
        c = self.store.save('o', 'Server build uses gamma', effective_from=50.0, supersedes=b['id'], **KEY)
        self.assertEqual([m['id'] for m in self.store.search('o', 'server build')], [c['id']])
        self.assertEqual([m['id'] for m in self.store.relevant('o', 'server build')], [c['id']])

    def test_cross_key_supersedes_rejected(self):
        # Regression (delta-review): an explicit link across different keys would split the fact's
        # history; supersession stays within one (subject, environment, attribute) key.
        a = self.store.save('o', 'Port is 8080', **KEY)
        for kwargs in (dict(subject='other-app', environment='local', attribute='port'),
                       dict(),  # keyed target, keyless new row
                       dict(subject='dev-server', environment='local')):  # partial key
            with self.assertRaises(ValueError, msg=repr(kwargs)):
                self.store.save('o', 'Port is 9090', supersedes=a['id'], **kwargs)
        self.assertIsNone(self.store.get('o', a['id'])['superseded_by'], 'rejected saves must not link')
        k = self.store.save('o', 'Prefers Taglish')
        keyed_upgrade = self.store.save('o', 'Port is 9090', supersedes=k['id'], **KEY)
        # A fully keyless (legacy) target MAY be superseded under a new key: the new key defines
        # the chain from here; this is the migration upgrade path, not history splitting.
        self.assertEqual(self.store.get('o', k['id'])['superseded_by'], keyed_upgrade['id'])
        same = self.store.save('o', 'Port is 9091', supersedes=a['id'], **KEY)
        self.assertEqual(self.store.get('o', a['id'])['superseded_by'], same['id'])


class _Reg:
    """Captures @registry.tool registrations from grokbot.tools.memory without the real registry."""
    def __init__(self):
        self.tools = {}

    def tool(self, name, description, schema, **kw):
        def deco(fn):
            self.tools[name] = (description, fn); return fn
        return deco


class _Ctx:
    def __init__(self, store, owner='o'):
        self.extras = {'memory': store, 'owner': owner}; self.session_id = 's'; self.events = []

    def emit(self, e):
        self.events.append(e)


def _tools():
    from grokbot.tools import memory as tm
    reg = _Reg(); tm.register(reg); return reg.tools


class ToolsContractTest(_Base):
    def test_forget_on_wrong_fact_still_destroys_the_row(self):
        t = _tools(); ctx = _Ctx(self.store)
        old = self.store.save('o', 'Dev server port is 3000')
        self.store.save('o', 'Dev server port is 8080')
        t['forget_memory'][1](ctx, old['id'])
        self.assertIsNone(self.store.get('o', old['id']))
        self.assertNotIn('3000', ' '.join(m['text'] for m in self.store.list('o')))

    def test_unlinked_duplicate_facts_count_against_owner_cap(self):
        from unittest import mock
        import grokbot.memory as gm
        with mock.patch.object(gm, 'MAX_MEMORIES', 3):
            for p in (3000, 3001, 3002):
                self.store.save('o', 'Dev server port is %d' % p)
            with self.assertRaises(ValueError):
                self.store.save('o', 'Dev server port is 8080')

    def test_supersede_tool_exists_and_forget_no_longer_offers_deletion_for_changed_facts(self):
        t = _tools()
        self.assertIn('supersede_memory', t)
        self.assertNotIn('outdated', t['forget_memory'][0])
        self.assertIn('wrong', t['forget_memory'][0])

    def test_supersede_tool_keeps_old_value_in_history(self):
        t = _tools(); ctx = _Ctx(self.store)
        old = self.store.save('o', 'Dev server port is 3000', **KEY)
        t['supersede_memory'][1](ctx, id=old['id'], text='Dev server port is 8080')
        self.assertEqual([m['text'] for m in self.store.search('o', 'dev server port')], ['Dev server port is 8080'])
        self.assertEqual(len(self.store.history('o', **KEY)), 2)

    def test_supersede_tool_errors_on_unknown_id(self):
        t = _tools(); ctx = _Ctx(self.store)
        from grokbot.tools import ToolError
        with self.assertRaises(ToolError):
            t['supersede_memory'][1](ctx, id='nope', text='Dev server port is 8080')

    def test_superseded_rows_do_not_count_against_owner_cap(self):
        from unittest import mock
        import grokbot.memory as gm
        with mock.patch.object(gm, 'MAX_MEMORIES', 2):
            prev = None
            for p in (3000, 3001, 3002, 8080):
                prev = self.store.save('o', 'Dev server port is %d' % p, supersedes=prev and prev['id'], **KEY)
            self.store.save('o', 'Uses pnpm')   # 2 current rows: allowed

    def test_candidate_flagged_rows_still_count_against_owner_cap(self):
        # Ambiguous facts are active facts: flagging must not move them outside the budget.
        from unittest import mock
        import grokbot.memory as gm
        with mock.patch.object(gm, 'MAX_MEMORIES', 2):
            self.store.save('o', 'Dev server port is 3000', **KEY)
            self.store.save('o', 'Dev server port is 8080', **KEY)   # ambiguous pair: both count
            with self.assertRaises(ValueError):
                self.store.save('o', 'Uses pnpm')


if __name__ == '__main__':
    unittest.main()
