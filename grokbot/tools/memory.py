"""Memory and conversation-history tools. Available when the host attaches the stores to the context:
``ctx.extras['memory']`` (MemoryStore), ``ctx.extras['conversations']`` (ConversationStore), ``ctx.extras['owner']``."""
import json

from grokbot.tools import ToolError


def _owner(ctx):
    return ctx.extras.get('owner') or ctx.session_id


def register(registry):
    has_memory = lambda ctx: ctx.extras.get('memory') is not None  # noqa: E731
    has_history = lambda ctx: ctx.extras.get('conversations') is not None  # noqa: E731

    @registry.tool('save_memory', 'Remember a durable fact about the user, their preferences or their projects '
                   '(e.g. "prefers replies in Tagalog", "repo X deploys with Render"). Not for task progress or secrets. '
                   'Saving the same text again just refreshes it.',
                   {'type': 'object', 'properties': {
                       'text': {'type': 'string', 'description': 'One short sentence'},
                       'tags': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Optional keywords'}},
                    'required': ['text']}, category='memory', mutates=True, available=has_memory)
    def save_memory(ctx, text, tags=None):
        try:
            row = ctx.extras['memory'].save(_owner(ctx), text, tags, source='agent')
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        ctx.emit({'type': 'memory_saved', 'id': row['id'], 'text': row['text']})
        return 'Remembered (id %s): %s' % (row['id'], row['text'])

    @registry.tool('search_memory', 'Search saved memories (facts about the user and projects) by keywords.',
                   {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']},
                   category='memory', available=has_memory)
    def search_memory(ctx, query):
        rows = ctx.extras['memory'].search(_owner(ctx), query, limit=15)
        if not rows:
            return 'No memories match.'
        return '\n'.join('- [%s] %s%s' % (r['id'], r['text'], (' (' + ', '.join(r['tags']) + ')') if r['tags'] else '') for r in rows)

    @registry.tool('forget_memory', 'Delete a saved memory by id (from search_memory) when it is wrong or outdated.',
                   {'type': 'object', 'properties': {'id': {'type': 'string'}}, 'required': ['id']},
                   category='memory', mutates=True, available=has_memory)
    def forget_memory(ctx, id):
        if not ctx.extras['memory'].delete(_owner(ctx), id):
            raise ToolError('No memory with that id.')
        return 'Forgot memory ' + id + '.'

    @registry.tool('search_history', 'Search earlier conversations of this user (full-text) to recall past work, '
                   'decisions or files. Returns conversation ids with matching snippets; use read_history for detail.',
                   {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']},
                   category='memory', available=has_history)
    def search_history(ctx, query):
        rows = ctx.extras['conversations'].search(_owner(ctx), query, limit=12)
        if not rows:
            return 'No earlier conversation matches.'
        return '\n'.join('- conversation %s (%s) · entry %s: %s' % (r['conv_id'], r.get('title') or 'untitled', r['seq'], r['snippet'])
                         for r in rows)

    @registry.tool('read_history', 'Read entries of an earlier conversation by id (from search_history).',
                   {'type': 'object', 'properties': {'conversation_id': {'type': 'string'},
                                                     'offset': {'type': 'integer', 'description': 'first entry index'},
                                                     'limit': {'type': 'integer', 'description': 'entries to return (max 40)'}},
                    'required': ['conversation_id']}, category='memory', available=has_history)
    def read_history(ctx, conversation_id, offset=0, limit=20):
        store = ctx.extras['conversations']
        conv = store.get(conversation_id, _owner(ctx))
        if conv is None:
            raise ToolError('No such conversation.')
        entries = store.entries(conversation_id, _owner(ctx))
        offset = max(0, int(offset or 0)); limit = max(1, min(40, int(limit or 20)))
        chunk = entries[offset:offset + limit]
        out = ['Conversation "%s" · %d entries (showing %d-%d)' % (conv['title'], len(entries), offset, offset + len(chunk))]
        for i, e in enumerate(chunk, offset):
            out.append('%d. %s: %s' % (i, e['role'], (e.get('content') or '')[:1500]))
        return '\n'.join(out)
