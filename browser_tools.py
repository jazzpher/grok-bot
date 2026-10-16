"""Optional Playwright-backed browser tools for the agent.

Read-mostly web access with a deliberately bounded surface: open, read,
click, type, close. Playwright is an optional dependency; without it the
tools fail with an install hint instead of crashing the harness. Page text
is untrusted data and is truncated before it reaches the model.
"""
import ipaddress
import urllib.parse

from grokbot import config as _cfg

_BLOCKED_HOSTS = {'localhost', '0.0.0.0', '::1', '[::1]'}
_BLOCKED_SUFFIXES = ('.local', '.internal', '.localhost', '.arpa')


class BrowserError(ValueError):
    """A browser tool refused or failed; safe to report to the model."""


def check_url(url):
    """Validate scheme and host before handing the URL to a browser."""
    if not isinstance(url, str) or not url.strip():
        raise BrowserError('browse_open needs a URL in url.')
    url = url.strip()
    if len(url) > 2000:
        raise BrowserError('URL must be at most 2000 characters.')
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise BrowserError('Invalid URL.') from exc
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise BrowserError('Only http and https URLs are supported.')
    host = (parsed.hostname or '').strip().lower().rstrip('.')
    if not host or host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
        raise BrowserError('Local and private addresses are not reachable from browser tools.')
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        raise BrowserError('Local and private addresses are not reachable from browser tools.')
    return url


def _check_selector(selector):
    if not isinstance(selector, str) or not selector.strip():
        raise BrowserError('This action needs a nonempty CSS or text selector.')
    if len(selector) > 500:
        raise BrowserError('Selector must be at most 500 characters.')
    return selector.strip()


def page_summary(title, url, text, limit=None):
    """Render a bounded page summary for the model."""
    if limit is None:
        limit = _cfg.BROWSER_TEXT_LIMIT
    text = (text or '').strip()
    note = ''
    if len(text) > limit:
        note = '\n(Text truncated to %d of %d characters.)' % (limit, len(text))
        text = text[:limit]
    return 'TITLE: ' + (title or '').strip()[:300] + '\nURL: ' + (url or '')[:2000] + '\n\n' + text + note


class BrowserSession:
    """One headless Chromium page per Workspace, created on first use."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._page = None

    def _ensure(self):
        if self._page is not None:
            return self._page
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError('Browser tools need Playwright installed: '
                               'pip install playwright && playwright install chromium.') from exc
        try:
            self._pw = sync_playwright().start()
            kwargs = {'headless': _cfg.BROWSER_HEADLESS}
            if _cfg.BROWSER_EXECUTABLE:
                kwargs['executable_path'] = _cfg.BROWSER_EXECUTABLE
            if _cfg.BROWSER_ARGS:
                kwargs['args'] = list(_cfg.BROWSER_ARGS)
            self._browser = self._pw.chromium.launch(**kwargs)
            self._page = self._browser.new_page()
            self._page.set_default_timeout(_cfg.BROWSER_TIMEOUT * 1000)
            self._page.set_default_navigation_timeout(_cfg.BROWSER_TIMEOUT * 1000)
        except Exception as exc:
            self.close()
            raise BrowserError('Browser could not start (' + type(exc).__name__ + '). '
                               'Check the Playwright installation: pip install playwright '
                               '&& playwright install chromium.') from exc
        return self._page

    def _body_text(self):
        page = self._page
        try:
            return page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception as exc:
            raise BrowserError('Could not read the page (' + type(exc).__name__ + ').') from exc

    def _summary(self):
        page = self._page
        try:
            title = page.title()
            url = page.url
        except Exception as exc:
            raise BrowserError('Could not read the page (' + type(exc).__name__ + ').') from exc
        return page_summary(title, url, self._body_text())

    def open(self, url):
        page = self._ensure()
        try:
            page.goto(check_url(url), wait_until='domcontentloaded')
        except BrowserError:
            raise
        except Exception as exc:
            raise BrowserError('Page did not load within %d seconds (%s).' % (_cfg.BROWSER_TIMEOUT, type(exc).__name__)) from exc
        return self._summary()

    def text(self):
        self._ensure()
        return self._summary()

    def click(self, selector):
        page = self._ensure()
        try:
            page.click(_check_selector(selector))
        except Exception as exc:
            raise BrowserError('Element not found or not clickable (%s).' % type(exc).__name__) from exc
        return self._summary()

    def type(self, selector, value):
        page = self._ensure()
        if not isinstance(value, str) or not value:
            raise BrowserError('browse_type needs the text to type in content.')
        try:
            page.fill(_check_selector(selector), value)
        except Exception as exc:
            raise BrowserError('Field not found or not fillable (%s).' % type(exc).__name__) from exc
        return self._summary()

    def close(self):
        for obj in (self._page, self._browser, self._pw):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        self._page = self._browser = self._pw = None
        return 'Browser closed.'
