"""preview_page: render a workspace HTML file in headless Chromium and return
the screenshot to the model as an image plus measured layout facts.

Off by default (GROK_PREVIEW=on). The render is confined: a loopback-only
static server rooted at the workspace, and a route guard that blocks
file://, other loopback ports, LAN and internet URLs (see grokbot/preview.py).
Read-only: it never writes to the workspace, so it needs no approval even in
on-request mode. The browser is killed when the turn ends.
"""
import base64
import dataclasses
import threading

from grokbot import config
from grokbot.preview import MAX_IMAGE_BYTES, PreviewError, PreviewSession, format_capture, resolve_html_path
from grokbot.tools import ToolError, ToolResult

_TURN_LOCAL = threading.local()

MAX_ROUNDS = 3                               # preview budget per turn (matches the UI-work prompt rule)


def _preview_enabled(ctx):
    return config.PREVIEW_ENABLED


def _session(ctx):
    session = ctx.extras.get('preview_session')
    if session is None:
        session = PreviewSession(ctx.workspace.root)
        ctx.extras['preview_session'] = session
    return session


def close_turn_session(ctx):
    """Kill the turn's Chromium + server, if any. Called by the agent loop's finally."""
    session = ctx.extras.pop('preview_session', None)
    if session is not None:
        try:
            session.close()
        except Exception:
            pass


def register(registry):
    @registry.tool('preview_page',
                   'Render a workspace HTML file in a headless browser and SEE it: returns a screenshot image '
                   '(with a vision-capable model) plus measured layout facts (horizontal overflow, element boxes, '
                   'contrast). Use after writing or editing HTML/CSS, then fix what you see and preview again - '
                   'at most %d previews per turn. Workspace files only; never renders URLs or non-HTML files.'
                   % MAX_ROUNDS,
                   {'type': 'object', 'properties': {
                       'path': {'type': 'string', 'description': 'Workspace HTML file (index.html, site/page.html) '
                                'or a folder containing index.html'},
                       'viewport': {'type': 'string', 'enum': ['desktop', 'mobile'],
                                    'description': 'desktop 1280x800 (default) or mobile 390x844'}},
                    'required': ['path']}, category='preview', parallel_safe=False, available=_preview_enabled)
    def preview_page(ctx, path, viewport='desktop'):
        try:
            rel, _ = resolve_html_path(ctx, path)
        except PreviewError as exc:
            raise ToolError(str(exc)) from exc
        used = getattr(_TURN_LOCAL, 'count', 0) if getattr(_TURN_LOCAL, 'turn', None) == ctx.turn_id else 0
        if used >= MAX_ROUNDS:
            raise ToolError('Preview budget reached (%d per turn). Report what you saw and what is still off '
                            'instead of previewing again.' % MAX_ROUNDS)
        try:
            capture = _session(ctx).capture(rel, viewport or 'desktop')
        except PreviewError as exc:
            raise ToolError(str(exc)) from exc
        _TURN_LOCAL.turn = ctx.turn_id
        _TURN_LOCAL.count = used + 1
        text = format_capture(capture, rel)
        image, mime = capture['image']
        data_url = 'data:%s;base64,%s' % (mime, base64.b64encode(image).decode('ascii'))
        note = ('\nThe screenshot is attached below as an image. Look at it before claiming the design is done.'
                if config.PREVIEW_IMAGES else
                '\n(Screenshot image disabled via GROK_PREVIEW_IMAGES=off; layout numbers only.)')
        return ToolResult('preview_page', {'path': path, 'viewport': viewport}, text + note, True, 0.0,
                          meta={'images': [{'data_url': data_url, 'name': 'preview-' + rel.replace('/', '-')}] if config.PREVIEW_IMAGES else [],
                                'preview': {'path': rel, 'viewport': viewport,
                                            'horizontal_overflow': bool(capture['layout'].get('horizontal_overflow'))}})
