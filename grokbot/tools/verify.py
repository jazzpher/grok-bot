"""verify_visual: deterministic pixel-diff between a ground truth and the agent's output.

Always available: comparing two workspace images needs no network and no extra
flags (Pillow only). The ground truth can also be rendered on the spot: a
workspace HTML file (``.html`` suffix, via the confined GROK_PREVIEW renderer)
or a public URL (via the GROK_BROWSER_ENABLED session, same URL guards as
browse). The output side accepts a workspace image or HTML file.

This is the verifier in "you build the verifier, the agent iterates": it returns
a number, never an opinion. The agent fixes the output and verifies again until
the mismatch is within threshold; it must not claim the work is done before that.
"""
from grokbot import config
from grokbot.tools import ToolError
from grokbot.visual import (MAX_IGNORE_REGIONS, VisualVerifyError, diff_images, is_html_name,
                            validate_ignore, validate_threshold, validate_tolerance)

MAX_NOTE = 300
VIEWPORTS = ('desktop', 'mobile')


def _image_bytes(ctx, rel):
    p = ctx.workspace.path(rel)
    from grokbot.visual import MAX_IMAGE_BYTES
    if not p.is_file() or p.stat().st_size > MAX_IMAGE_BYTES:
        raise ToolError('Image missing or exceeds the %d MB limit: %s'
                        % (MAX_IMAGE_BYTES // (1024 * 1024), rel))
    return p.read_bytes()


def _render_html(ctx, rel, viewport):
    if not config.PREVIEW_ENABLED:
        raise ToolError('Rendering HTML needs GROK_PREVIEW=on (and pip install playwright && '
                        'playwright install chromium). Compare against an image file instead, or ask '
                        'the owner to enable the preview renderer.')
    from grokbot.preview import PreviewError, resolve_html_path
    from grokbot.tools.preview import _session
    try:
        resolved, _ = resolve_html_path(ctx, rel)
        capture = _session(ctx).capture(resolved, viewport)
    except PreviewError as exc:
        raise ToolError(str(exc)) from exc
    return capture['image'][0]


def _render_url(ctx, url):
    if not config.BROWSER_ENABLED:
        raise ToolError('Rendering a URL needs GROK_BROWSER_ENABLED=on (and Playwright). Compare '
                        'against a workspace image or HTML file instead, or ask the owner to enable browsing.')
    from browser_tools import BrowserError, BrowserSession
    session = getattr(ctx.workspace, 'browser', None)
    if session is None:
        session = BrowserSession()
        try:
            ctx.workspace.browser = session
        except AttributeError:
            pass
    try:
        session.open(url)
        return session.shot()
    except BrowserError as exc:
        raise ToolError(str(exc)) from exc


def _side(ctx, name, viewport, role):
    """Resolve one side of the comparison to image bytes."""
    if role == 'source_url':
        return _render_url(ctx, name), name
    if is_html_name(name):
        return _render_html(ctx, name, viewport), name + ' (rendered)'
    return _image_bytes(ctx, name), name


def register(registry):
    @registry.tool('verify_visual',
                   'Verify visual output with a deterministic pixel diff (a number, not an opinion). '
                   'Compares your output against a ground truth and returns the mismatch percent plus a '
                   'highlighted diff image saved next to the output. PASS means mismatch <= threshold; on '
                   'FAIL, fix the output and verify again - do not claim the work is done before it passes. '
                   'Sides: path is your output (workspace image, or .html file rendered headlessly); the '
                   'ground truth is source_path (workspace image or .html) or url (public page, needs '
                   'GROK_BROWSER_ENABLED=on). Rendering HTML needs GROK_PREVIEW=on.',
                   {'type': 'object', 'properties': {
                       'path': {'type': 'string', 'description': 'Your output: workspace image (out.png) or '
                                'HTML file (page.html, rendered headlessly)'},
                       'source_path': {'type': 'string', 'description': 'Ground truth: workspace image or HTML '
                                       'file. Exactly one of source_path / url.'},
                       'url': {'type': 'string', 'description': 'Ground truth: public URL rendered headlessly '
                               '(GROK_BROWSER_ENABLED=on). Exactly one of source_path / url.'},
                       'threshold': {'type': 'number', 'description': 'Maximum allowed mismatch ratio 0-1 '
                                     '(default 0.01 = 1%). PASS when mismatch <= threshold.'},
                       'tolerance': {'type': 'integer', 'description': 'Per-channel pixel difference ignored '
                                     '0-255 (default 0 = exact; raise slightly for font-rendering noise).'},
                       'ignore': {'type': 'array', 'items': {'type': 'array', 'items': {'type': 'integer'}},
                                  'description': 'Up to %d [x, y, width, height] rectangles excluded from the '
                                  'comparison, e.g. a clock or avatar area' % MAX_IGNORE_REGIONS},
                       'viewport': {'type': 'string', 'enum': list(VIEWPORTS),
                                    'description': 'Render viewport for HTML sides: desktop 1280x800 (default) '
                                    'or mobile 390x844'},
                       'note': {'type': 'string', 'description': 'Documented exemption: what known-irrelevant '
                                'differences the threshold/tolerance/ignore cover (e.g. "font antialiasing")'}},
                    'required': ['path']},
                   category='verify', mutates=True, parallel_safe=False)
    def verify_visual(ctx, path, source_path=None, url=None, threshold=0.01, tolerance=0,
                      ignore=None, viewport='desktop', note=None):
        try:
            threshold = validate_threshold(threshold)
            tolerance = validate_tolerance(tolerance)
            rects = validate_ignore(ignore)
        except VisualVerifyError as exc:
            raise ToolError(str(exc)) from exc
        if viewport not in VIEWPORTS:
            raise ToolError('viewport must be one of: ' + ', '.join(VIEWPORTS))
        if (source_path is None) == (url is None):
            raise ToolError('Supply exactly one ground truth: source_path or url.')
        if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE):
            raise ToolError('note must be text up to %d characters.' % MAX_NOTE)
        try:
            ctx.workspace.path(path)
            if source_path is not None:
                ctx.workspace.path(source_path)
        except Exception as exc:
            raise ToolError(str(exc)) from exc
        out_bytes, out_label = _side(ctx, path, viewport, 'output')
        if url is not None:
            truth_bytes, truth_label = _side(ctx, url, viewport, 'source_url')
        else:
            truth_bytes, truth_label = _side(ctx, source_path, viewport, 'source')
        diff_rel = path.rsplit('.', 1)[0] + '.diff.png'
        diff_abs = ctx.workspace.path(diff_rel)
        try:
            result = diff_images(truth_bytes, out_bytes, tolerance=tolerance, ignore=rects,
                                 diff_path=diff_abs)
        except VisualVerifyError as exc:
            raise ToolError(str(exc)) from exc
        passed = result['mismatch_ratio'] <= threshold
        verdict = ('%s pixel diff: %.4f%% mismatch (threshold %.4f%%), %d of %d pixels differ, '
                   '%s vs %s, sizes %s vs %s. Highlighted diff saved: %s (red pixels differ%s).'
                   % ('PASS' if passed else 'FAIL', result['mismatch_percent'], threshold * 100,
                      result['different_pixels'], result['compared_pixels'], out_label, truth_label,
                      'x'.join(map(str, result['output_size'])),
                      'x'.join(map(str, result['ground_truth_size'])), diff_rel,
                      '' if result['ignored_pixels'] == 0 else
                      '; %d ignored pixels excluded' % result['ignored_pixels']))
        if note:
            verdict += ' Exemption note: ' + note
        if passed:
            return verdict + ' This side is verified; state the threshold when you report it.'
        return (verdict + ' Fix the output and run verify_visual again; do not claim the work matches '
                'the ground truth until this passes or the owner relaxes the threshold.')
