"""Token estimation and model-summarized context compaction.

When the estimated prompt exceeds ``COMPACT_RATIO`` of the model window the
older part of the conversation is replaced by a structured summary written by
the model (goal, progress, decisions, files, open questions, user preferences).
If the summary call fails we fall back to deterministic truncation with an
explicit note. Tool-call / tool-result pairs are never split.
"""
import json

from grokbot import config

SUMMARY_PROMPT = (
    'You are compacting an AI agent conversation so work can continue with less context. '
    'Write a dense, factual summary in Markdown with these sections: '
    '## Goal, ## Progress so far, ## Decisions and reasons, ## Files and artifacts (paths + what changed), '
    '## Open issues / next steps, ## User preferences and constraints. '
    'Keep exact identifiers, paths, commands, error messages and numbers. Do not invent anything. '
    'Under 700 words.'
)


def estimate_tokens(messages):
    """Cheap heuristic (~3.6 chars/token for mixed code+prose) plus per-message overhead."""
    total = 0
    for m in messages:
        total += 4
        content = m.get('content')
        if isinstance(content, str):
            total += len(content) / 3.6
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get('type') == 'image_url':
                    total += 1100      # provider-side image tokens, not the base64 length
                else:
                    total += len(json.dumps(part)) / 3.6
        if m.get('tool_calls'):
            total += len(json.dumps(m['tool_calls'])) / 3.6
    return int(total)


def needs_compaction(messages, context_tokens, ratio=None):
    return estimate_tokens(messages) > int(context_tokens * (ratio or config.COMPACT_RATIO))


def _safe_cut(messages, keep_recent):
    """Index where the kept tail starts, never splitting tool results from their call."""
    if len(messages) <= keep_recent:
        return 0
    cut = len(messages) - keep_recent
    # Prefer starting the kept tail at a user turn (a coherent exchange), looking back a bounded distance.
    j = cut
    while j > 0 and messages[j].get('role') != 'user' and cut - j < 12:
        j -= 1
    if messages[j].get('role') == 'user':
        cut = j
    # Never start on a tool result: its assistant tool_calls message must stay with it.
    while cut > 0 and messages[cut].get('role') == 'tool':
        cut -= 1
    return max(0, cut)


def transcript(messages, limit_chars=120000):
    parts = []
    for m in messages:
        role = m.get('role', '?')
        content = m.get('content')
        if isinstance(content, list):
            content = ' '.join(p.get('text', '') for p in content if isinstance(p, dict))
        if m.get('tool_calls'):
            calls = '; '.join(tc.get('function', {}).get('name', '?') + '(' + str(tc.get('function', {}).get('arguments', ''))[:300] + ')'
                              for tc in m['tool_calls'])
            parts.append('[assistant tool calls] ' + calls)
        if content:
            parts.append('[' + role + '] ' + str(content)[:4000])
    text = '\n'.join(parts)
    if len(text) > limit_chars:
        text = text[:limit_chars // 2] + '\n…[middle omitted]…\n' + text[-limit_chars // 2:]
    return text


def compact(messages, summarizer=None, keep_recent=None, previous_summary=''):
    """Return (new_messages, summary_text, method). ``summarizer(prompt_messages) -> str`` may be None."""
    keep_recent = keep_recent or config.KEEP_RECENT_MESSAGES
    cut = _safe_cut(messages, keep_recent)
    if cut == 0:
        return messages, previous_summary, 'none'
    older, recent = messages[:cut], messages[cut:]
    summary, method = '', 'truncate'
    if summarizer is not None:
        try:
            prompt = [{'role': 'system', 'content': SUMMARY_PROMPT},
                      {'role': 'user', 'content': ('Previous summary:\n' + previous_summary + '\n\n' if previous_summary else '')
                       + 'Conversation to summarize:\n' + transcript(older)}]
            summary = (summarizer(prompt) or '').strip()
            if summary:
                method = 'model'
        except Exception:  # noqa: BLE001 - any failure falls back to truncation
            summary = ''
    if not summary:
        first_user = next((m.get('content') for m in older if m.get('role') == 'user' and isinstance(m.get('content'), str)), '')
        files = sorted({tc.get('function', {}).get('name', '') for m in older for tc in (m.get('tool_calls') or [])})
        summary = ('## Goal\n' + str(first_user)[:1500] + '\n\n## Note\nEarlier messages were truncated without a model summary '
                   '(summary call failed). Tools used earlier: ' + ', '.join(f for f in files if f) + '. Re-read files before relying on them.')
    marker = {'role': 'user', 'content': '[Context compacted. Summary of the earlier conversation — treat as reliable notes, '
                                          'verify details with tools when it matters.]\n\n' + summary}
    ack = {'role': 'assistant', 'content': 'Understood. I will continue from the compacted summary.'}
    return [marker, ack, *recent], summary, method
