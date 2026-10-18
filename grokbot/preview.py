"""Workspace page previews: a loopback-only static server plus a headless
Chromium screenshot/layout pass, so the model can *see* the HTML it writes.

Deliberately separate from the public-web browser tools (browser_tools.py):

* Off by default (``GROK_PREVIEW=on``), independent of ``GROK_BROWSER_ENABLED``.
* The static server binds a random 127.0.0.1 port, serves files only from
  inside the workspace, and rejects any request whose Host header is not that
  exact loopback port (DNS-rebinding guard).
* The page's own network is confined the same way: a route guard allows
  requests only to the preview server itself plus inline data:/blob:/about:
  URLs. ``file://``, other loopback ports, LAN hosts and the public internet
  are all aborted, per request (including each redirect hop).
* Chromium starts on the first preview of a turn and is killed when the turn
  ends (the agent loop closes the session in its ``finally``). One browser,
  headless; each capture uses a fresh browser context so no state carries over.
"""
import concurrent.futures
import functools
import http.server
import json
import socketserver
import threading
import urllib.parse
from pathlib import Path

from grokbot import config as _cfg

MAX_PAGE_BYTES = 5 * 1024 * 1024          # single asset cap served by the preview server
SHOT_MAX_EDGE = 4000                       # screenshot pixel cap per side
HOSTS = ('127.0.0.1', 'localhost')

VIEWPORTS = {
    'desktop': {'width': 1280, 'height': 800},
    'mobile': {'width': 390, 'height': 844},
}
MAX_SHOT_HEIGHT = 2000                     # full-page screenshots are clipped to this height
MAX_IMAGE_BYTES = int(1.5 * 1024 * 1024)   # PNG above this is retaken as JPEG
JPEG_QUALITY = 80
FONT_SETTLE_MS = 300
NAV_TIMEOUT_MS = 15000


class PreviewError(ValueError):
    """A preview refused or failed; the message is safe to show to the model."""


def _split_host(value):
    """(hostname, port) from a Host header or netloc; port is None when absent/unparseable."""
    host = (value or '').strip()
    if host.startswith('['):                              # [::1]:8080
        end = host.find(']')
        if end < 0:
            return host, None
        name = host[1:end]
        rest = host[end + 1:]
        if rest.startswith(':') and rest[1:].isdigit():
            return name, int(rest[1:])
        return name, None
    if host.count(':') == 1:
        name, _, port = host.partition(':')
        return name, int(port) if port.isdigit() else None
    return host, None


def preview_netloc_allowed(url, port):
    """True only for http(s) URLs targeting this preview server (127.0.0.1/localhost
    on our bound port, no credentials). Everything else - file://, other loopback
    ports, LAN IPs, public hosts - is refused. Relative URLs in a page resolve to
    the server and reach this guard already absolute."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ('http', 'https'):
        return False
    if parsed.username or parsed.password:
        return False
    host, url_port = _split_host(parsed.netloc)
    if url_port is None:
        url_port = 443 if parsed.scheme == 'https' else 80
    return host.lower() in HOSTS and url_port == port


def host_header_allowed(value, port):
    """True when a request's Host header names exactly this preview server."""
    host, host_port = _split_host(value)
    return host.lower() in HOSTS and host_port == port


_SAFE_INLINE = ('data', 'blob', 'about')


def request_allowed(url, port):
    """Route-guard decision for one page request (navigation, subresource, redirect hop)."""
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme in _SAFE_INLINE:
        return True
    return preview_netloc_allowed(url, port)


def _route_guard(port):
    def guard(route):
        try:
            allowed = request_allowed(route.request.url, port)
        except Exception:
            allowed = False
        try:
            route.continue_() if allowed else route.abort('blockedbyclient')
        except Exception:
            pass
    return guard


class _PreviewHandler(http.server.SimpleHTTPRequestHandler):
    """Serves GET/HEAD from the workspace root only; Host-header and size guarded."""

    def log_message(self, *args):                          # keep logs quiet
        pass

    def _safe_path(self):
        """Workspace-confined Path for the request, or None when it escapes."""
        raw = urllib.parse.urlsplit(self.path).path
        parts = []
        for piece in raw.split('/'):
            if piece == '..':
                return None
            if piece in ('', '.'):
                continue
            parts.append(urllib.parse.unquote(piece))
        candidate = (self.server.root / Path(*parts)) if parts else self.server.root
        try:
            if candidate.is_symlink():
                return None
            resolved = candidate.resolve()
            if resolved != self.server.root and self.server.root not in resolved.parents:
                return None
        except OSError:
            return None
        return resolved

    def _serve(self, head_only=False):
        if not host_header_allowed(self.headers.get('Host'), self.server.port):
            self.send_error(403)
            return
        target = self._safe_path()
        if target is None:
            self.send_error(403)
            return
        if target.is_dir():
            target = target / 'index.html'
        if target.is_symlink() or not target.is_file():
            self.send_error(404)
            return
        try:
            size = target.stat().st_size
            if size > MAX_PAGE_BYTES:
                self.send_error(413)
                return
            body = None if head_only else target.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', self.guess_type(str(target)))
        self.send_header('Content-Length', str(size))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy',
                         "default-src 'self' data: blob:; style-src 'self' 'unsafe-inline' data: blob:")
        self.end_headers()
        if body is not None:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self):
        self._serve()

    def do_HEAD(self):
        self._serve(head_only=True)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class PreviewServer:
    """Loopback static server rooted at the workspace; starts on first use."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self._server = None
        self._thread = None
        self.port = None

    def _ensure(self):
        if self._server is not None:
            return self.port
        server = _Server(('127.0.0.1', 0), _PreviewHandler)
        server.root = self.root
        server.port = server.server_address[1]
        self._thread = threading.Thread(target=server.serve_forever, name='grokbot-preview-http', daemon=True)
        self._thread.start()
        self._server = server
        self.port = server.port
        return self.port

    def url_for(self, rel):
        port = self._ensure()
        quoted = '/'.join(urllib.parse.quote(piece) for piece in Path(rel).parts)
        return 'http://127.0.0.1:%d/%s' % (port, quoted)

    def close(self):
        server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        self._thread = None
        self.port = None


# In-page measurement (Phase B): numbers the model cannot get from the pixels alone.
_LAYOUT_JS = """() => {
  const out = {title: document.title || '', url: location.href,
               scroll_width: document.documentElement.scrollWidth,
               client_width: document.documentElement.clientWidth,
               scroll_height: document.documentElement.scrollHeight,
               element_count: document.getElementsByTagName('*').length,
               body_background: ''};
  try { out.body_background = getComputedStyle(document.body).backgroundColor || ''; } catch (e) {}
  out.horizontal_overflow = out.scroll_width > out.client_width + 1;
  const boxes = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  let node, count = 0;
  while ((node = walker.nextNode()) && boxes.length < 20 && count < 4000) {
    count++;
    const r = node.getBoundingClientRect();
    if (!r || r.width < 1 || r.height < 1) continue;
    const style = getComputedStyle(node);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    let text = '';
    for (const child of node.childNodes) {
      if (child.nodeType === 3 && child.textContent.trim()) { text = child.textContent.trim().slice(0, 60); break; }
    }
    const isText = !!text || ['IMG', 'SVG', 'CANVAS', 'VIDEO', 'IFRAME', 'INPUT', 'BUTTON'].includes(node.tagName);
    if (!isText) continue;
    let depth = 0, p = node;
    while (p.parentElement) { depth++; p = p.parentElement; }
    boxes.push({tag: node.tagName.toLowerCase(), depth, text,
                x: Math.round(r.x + window.scrollX), y: Math.round(r.y + window.scrollY),
                w: Math.round(r.width), h: Math.round(r.height)});
  }
  out.boxes = boxes;
  // Best-effort contrast pass over visible text (WCAG AA: 4.5:1 normal, 3:1 large).
  function lum(c) {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
  }
  function parseRgb(s) {
    const m = s && s.match(/rgba?\\(([^)]+)\\)/);
    if (!m) return null;
    const parts = m[1].split(',').map(Number);
    if (parts.length < 3 || parts.some(Number.isNaN)) return null;
    return {rgb: parts.slice(0, 3), alpha: parts.length > 3 ? parts[3] : 1};
  }
  function effectiveBg(el) {
    let p = el;
    while (p && p !== document.documentElement) {
      const bg = parseRgb(getComputedStyle(p).backgroundColor);
      if (bg && bg.alpha > 0.9) return bg.rgb;
      p = p.parentElement;
    }
    return [255, 255, 255];
  }
  const contrast = [];
  const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let t, checked = 0;
  while ((t = tw.nextNode()) && contrast.length < 10 && checked < 1500) {
    if (!t.textContent || !t.textContent.trim()) continue;
    const el = t.parentElement;
    if (!el) continue;
    const r = el.getBoundingClientRect();
    if (!r || r.width < 1 || r.height < 1) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    const fg = parseRgb(style.color);
    if (!fg || fg.alpha < 0.3) continue;
    checked++;
    const bg = effectiveBg(el);
    const l1 = lum(fg.rgb), l2 = lum(bg);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    const px = parseFloat(style.fontSize) || 16;
    const bold = parseInt(style.fontWeight, 10) >= 700;
    const need = (px >= 18.66 || (px >= 14 && bold)) ? 3 : 4.5;
    if (ratio < need) {
      contrast.push({text: t.textContent.trim().slice(0, 40), ratio: Math.round(ratio * 100) / 100,
                     fg: style.color, bg: 'rgb(' + bg.join(',') + ')'});
    }
  }
  out.contrast_failures = contrast;
  return out;
}"""


def _on_worker(fn):
    """Run a PreviewSession method on its own worker thread (Playwright pins to its start thread)."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        if getattr(self, '_worker', None) is None:
            self._worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='grokbot-preview')
        return self._worker.submit(fn, self, *args, **kwargs).result()
    return wrapper


class PreviewSession:
    """One headless Chromium confined to a PreviewServer; killed at turn end."""

    def __init__(self, workspace_root):
        self.server = PreviewServer(workspace_root)
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._worker = None

    def _ensure_browser(self):
        if self._browser is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise PreviewError('preview_page needs Playwright installed: '
                               'pip install playwright && playwright install chromium.') from exc
        try:
            self.server._ensure()
            self._pw = sync_playwright().start()
            kwargs = {'headless': _cfg.BROWSER_HEADLESS}
            if _cfg.BROWSER_EXECUTABLE:
                kwargs['executable_path'] = _cfg.BROWSER_EXECUTABLE
            if _cfg.BROWSER_ARGS:
                kwargs['args'] = list(_cfg.BROWSER_ARGS)
            self._browser = self._pw.chromium.launch(**kwargs)
        except PreviewError:
            self._close()
            raise
        except Exception as exc:
            self._close()
            raise PreviewError('Chromium could not start (' + type(exc).__name__ + '). '
                               'Check the Playwright installation: pip install playwright '
                               '&& playwright install chromium.') from exc

    @_on_worker
    def capture(self, rel_path, viewport='desktop'):
        """Screenshot + layout numbers for one workspace HTML file.

        Returns {'url', 'viewport', 'layout', 'image': (bytes, mime)}.
        A fresh browser context per capture means no cookies or storage state
        carries between previews; the route guard is re-attached per context."""
        size = VIEWPORTS.get(viewport)
        if size is None:
            raise PreviewError('viewport must be one of: ' + ', '.join(sorted(VIEWPORTS)))
        self._ensure_browser()
        url = self.server.url_for(rel_path)
        old_context = self._context
        try:
            self._context = self._browser.new_context(viewport=size)
            self._context.route('**/*', _route_guard(self.server.port))
            self._page = self._context.new_page()
            page = self._page
            page.set_default_timeout(NAV_TIMEOUT_MS)
            page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        except Exception as exc:
            raise PreviewError('Could not open a preview page (' + type(exc).__name__ + ').') from exc
        finally:
            if old_context is not None:
                try:
                    old_context.close()
                except Exception:
                    pass
        try:
            page.goto(url, wait_until='load', timeout=NAV_TIMEOUT_MS)
        except Exception as exc:
            raise PreviewError('The page did not load within %d seconds (%s).'
                               % (NAV_TIMEOUT_MS // 1000, type(exc).__name__)) from exc
        try:
            page.wait_for_load_state('networkidle', timeout=5000)
        except Exception:
            pass                                          # load already fired; quiet pages time out here
        page.wait_for_timeout(FONT_SETTLE_MS)             # let webfonts/late layout settle
        if page.url != url and not preview_netloc_allowed(page.url, self.server.port):
            raise PreviewError('The page navigated away from the preview server; blocked.')
        try:
            layout = page.evaluate(_LAYOUT_JS)
        except Exception as exc:
            raise PreviewError('Could not measure the page (%s).' % type(exc).__name__) from exc
        height = max(1, min(int(layout.get('scroll_height') or size['height']), MAX_SHOT_HEIGHT))
        clip = {'x': 0, 'y': 0, 'width': size['width'], 'height': height}
        try:
            png = page.screenshot(type='png', clip=clip)
        except Exception as exc:
            raise PreviewError('Could not capture the screenshot (%s).' % type(exc).__name__) from exc
        image, mime = png, 'image/png'
        if len(png) > MAX_IMAGE_BYTES:
            try:
                jpg = page.screenshot(type='jpeg', quality=JPEG_QUALITY, clip=clip)
                if len(jpg) < len(png):
                    image, mime = jpg, 'image/jpeg'
            except Exception:
                pass
        return {'url': url, 'viewport': dict(size, name=viewport), 'layout': layout, 'image': (image, mime)}

    def close(self):
        worker = getattr(self, '_worker', None)
        if worker is None:
            return self._close()
        try:
            return worker.submit(self._close).result()
        finally:
            worker.shutdown(wait=True)
            self._worker = None

    def _close(self):
        for obj in (self._page, self._context, self._browser):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._page = self._context = self._browser = self._pw = None
        try:
            self.server.close()
        except Exception:
            pass
        return 'Preview closed.'


def format_capture(capture, rel_path):
    """Short text that accompanies the screenshot: what + where + measured flags."""
    layout = capture['layout']
    vp = capture['viewport']
    lines = ['PREVIEW %s (%dx%d viewport, page clipped at %dpx height)' % (rel_path, vp['width'], vp['height'], MAX_SHOT_HEIGHT),
             'URL: ' + capture['url'],
             'TITLE: ' + (layout.get('title') or '').strip()[:200],
             'horizontal_overflow: %s (scrollWidth %s vs clientWidth %s)' % (
                 'YES' if layout.get('horizontal_overflow') else 'no',
                 layout.get('scroll_width'), layout.get('client_width')),
             'elements: %s · body background: %s' % (layout.get('element_count'), layout.get('body_background') or '?')]
    fails = layout.get('contrast_failures') or []
    if fails:
        lines.append('contrast (best-effort, below WCAG AA): ' + '; '.join(
            '"%s" ratio %s (%s on %s)' % (f['text'], f['ratio'], f['fg'], f['bg']) for f in fails[:5]))
    boxes = layout.get('boxes') or []
    if boxes:
        lines.append('top boxes (tag depth x,y w,h text):')
        for b in boxes[:20]:
            lines.append('  %s d%d %d,%d %dx%d %s' % (b['tag'], b['depth'], b['x'], b['y'], b['w'], b['h'], b['text']))
    lines.append('LAYOUT_JSON: ' + json.dumps({k: v for k, v in layout.items() if k != 'boxes'},
                                              ensure_ascii=False)[:2000])
    return '\n'.join(lines)


def resolve_html_path(ctx, path):
    """Resolve the tool argument to an existing workspace HTML file: (rel path, absolute Path)."""
    from agent_loop import ToolError as _WorkspaceError
    if not isinstance(path, str) or not path.strip():
        raise PreviewError('preview_page needs a workspace path, for example index.html or site/page.html.')
    try:
        target = ctx.workspace.path(path.strip())
    except _WorkspaceError as exc:
        raise PreviewError(str(exc)) from exc
    if target.is_dir():
        index = target / 'index.html'
        if index.is_file() and not index.is_symlink():
            target = index
        else:
            raise PreviewError(path + ' is a folder without an index.html.')
    if target.suffix.lower() not in ('.html', '.htm'):
        raise PreviewError('preview_page only renders .html files (' + (target.suffix or 'no extension')
                           + ' is not HTML). Not rendering it.')
    if not target.is_file():
        raise PreviewError('No such file: ' + path)
    try:
        if target.stat().st_size > MAX_PAGE_BYTES:
            raise PreviewError('That file is larger than the preview limit (%d KB).' % (MAX_PAGE_BYTES // 1024))
    except OSError as exc:
        raise PreviewError('Could not read ' + path + '.') from exc
    rel = target.relative_to(ctx.workspace.root).as_posix()
    return rel, target
