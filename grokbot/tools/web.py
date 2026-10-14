"""web_search and fetch_url.

fetch_url re-validates every redirect hop against the SSRF guard, bounds bytes
and characters, and converts HTML to readable text with the stdlib parser (no
new dependencies). web_search has pluggable backends: Tavily / Brave (API key),
SearXNG (self-hosted URL) or a best-effort DuckDuckGo HTML fallback.
"""
import html
import json
import re
from html.parser import HTMLParser
from urllib.parse import quote_plus, urljoin, urlparse

import requests

from grokbot import config, netguard
from grokbot.tools import ToolError

USER_AGENT = 'Mozilla/5.0 (compatible; grok-bot/0.2; +https://github.com/jazzpher/grok-bot)'
BLOCK_TAGS = {'p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'pre', 'blockquote', 'section',
              'article', 'header', 'footer', 'table', 'ul', 'ol', 'dd', 'dt', 'hr', 'figcaption', 'main', 'aside', 'nav'}
SKIP_TAGS = {'script', 'style', 'noscript', 'svg', 'iframe', 'canvas', 'template', 'head'}
HEADING = {'h1': '# ', 'h2': '## ', 'h3': '### ', 'h4': '#### ', 'h5': '##### ', 'h6': '###### '}


class _TextExtractor(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.parts = []
        self.skip = 0
        self.pre = 0
        self.href = None
        self.title = ''
        self._in_title = False
        self.list_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == 'title':
            self._in_title = True
        if tag in HEADING:
            self.parts.append('\n\n' + HEADING[tag])
        elif tag == 'li':
            self.parts.append('\n' + '  ' * max(0, self.list_depth - 1) + '- ')
        elif tag in ('ul', 'ol'):
            self.list_depth += 1
            self.parts.append('\n')
        elif tag == 'pre':
            self.pre += 1
            self.parts.append('\n```\n')
        elif tag == 'code' and not self.pre:
            self.parts.append('`')
        elif tag == 'a':
            href = dict(attrs).get('href')
            if href and not href.startswith(('javascript:', '#', 'mailto:')):
                self.href = urljoin(self.base, href)
        elif tag in ('td', 'th'):
            self.parts.append(' | ')
        elif tag == 'img':
            alt = dict(attrs).get('alt')
            if alt:
                self.parts.append('[image: ' + alt.strip() + ']')
        elif tag in BLOCK_TAGS:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == 'title':
            self._in_title = False
        if tag in ('ul', 'ol'):
            self.list_depth = max(0, self.list_depth - 1)
        if tag == 'pre':
            self.pre = max(0, self.pre - 1)
            self.parts.append('\n```\n')
        elif tag == 'code' and not self.pre:
            self.parts.append('`')
        elif tag == 'a' and self.href:
            self.parts.append(' (' + self.href + ')')
            self.href = None
        elif tag in BLOCK_TAGS or tag in HEADING:
            self.parts.append('\n')

    def handle_data(self, data):
        if self.skip:
            return
        if self._in_title:
            self.title += data
        if self.pre:
            self.parts.append(data)
        else:
            text = re.sub(r'\s+', ' ', data)
            if text.strip():
                self.parts.append(text)

    def text(self):
        out = ''.join(self.parts)
        out = re.sub(r'[ \t]+\n', '\n', out)
        out = re.sub(r'\n{3,}', '\n\n', out)
        return out.strip()


def html_to_text(raw, base=''):
    parser = _TextExtractor(base)
    try:
        parser.feed(raw)
        parser.close()
    except Exception:  # noqa: BLE001 - tolerate broken HTML
        pass
    title = re.sub(r'\s+', ' ', html.unescape(parser.title)).strip()
    return title, parser.text()


def _check(url):
    err = netguard.public_url_error(url, schemes=('http', 'https'), max_length=netguard.MAX_URL_LENGTH, what='Fetched URLs')
    if err:
        raise ToolError(err)


def fetch(url, max_bytes=None, timeout=None, max_redirects=5):
    """Download a public URL with manual, re-validated redirects. Returns (final_url, content_type, bytes)."""
    max_bytes = max_bytes or config.FETCH_MAX_BYTES
    timeout = timeout or config.FETCH_TIMEOUT
    current = url
    for _ in range(max_redirects + 1):
        _check(current)
        try:
            res = requests.get(current, headers={'User-Agent': USER_AGENT, 'Accept': 'text/html,application/xhtml+xml,application/json,text/plain,application/pdf;q=0.8,*/*;q=0.5',
                                                 'Accept-Language': 'en,*;q=0.5'},
                               timeout=(min(timeout, 10), timeout), stream=True, allow_redirects=False)
        except requests.RequestException as exc:
            raise ToolError('Could not fetch ' + current + ': ' + type(exc).__name__) from exc
        with res:
            if 300 <= res.status_code < 400 and res.headers.get('Location'):
                current = urljoin(current, res.headers['Location'])
                continue
            if res.status_code >= 400:
                raise ToolError('HTTP %d from %s' % (res.status_code, urlparse(current).hostname or current))
            body = bytearray()
            for chunk in res.iter_content(16384):
                body.extend(chunk)
                if len(body) > max_bytes:
                    break
            return current, res.headers.get('Content-Type', ''), bytes(body[:max_bytes]), len(body) > max_bytes
    raise ToolError('Too many redirects.')


def _decode(raw, content_type):
    match = re.search(r'charset=([\w-]+)', content_type or '', re.I)
    for enc in ([match.group(1)] if match else []) + ['utf-8', 'cp1252', 'latin-1']:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode('utf-8', errors='replace')


def register(registry):
    @registry.tool('fetch_url', 'Fetch a public web page or file and return its readable text (HTML converted to text, '
                   'JSON/plain text as-is, PDF text extracted). Use after web_search to read a source.',
                   {'type': 'object', 'properties': {
                       'url': {'type': 'string'},
                       'max_chars': {'type': 'integer', 'description': 'Limit returned text (default %d)' % config.FETCH_MAX_CHARS},
                       'offset': {'type': 'integer', 'description': 'Character offset to continue reading long pages'}},
                    'required': ['url']}, category='web')
    def fetch_url(ctx, url, max_chars=None, offset=0):
        if not isinstance(url, str) or not url.strip():
            raise ToolError('url is required.')
        url = url.strip()
        if '://' not in url:
            url = 'https://' + url
        final, ctype, raw, cut = fetch(url)
        max_chars = max(500, min(int(max_chars or config.FETCH_MAX_CHARS), 200000))
        offset = max(0, int(offset or 0))
        lower = (ctype or '').lower()
        title = ''
        if 'pdf' in lower or raw[:5] == b'%PDF-':
            try:
                from documents import extract_text
                text = extract_text('page.pdf', raw)
            except Exception as exc:  # noqa: BLE001
                raise ToolError('Could not extract PDF text (' + type(exc).__name__ + ').') from exc
        elif 'html' in lower or raw.lstrip()[:1] == b'<':
            title, text = html_to_text(_decode(raw, ctype), final)
        elif 'json' in lower:
            text = _decode(raw, ctype)
            try:
                text = json.dumps(json.loads(text), indent=1, ensure_ascii=False)
            except ValueError:
                pass
        else:
            if b'\x00' in raw[:4096]:
                raise ToolError('Binary content (%s, %d bytes); fetch_url returns text only.' % (ctype or 'unknown type', len(raw)))
            text = _decode(raw, ctype)
        total = len(text)
        piece = text[offset:offset + max_chars]
        header = (title + '\n' if title else '') + 'Source: ' + final + '\n'
        footer = ''
        if offset + max_chars < total:
            footer = '\n…[%d of %d characters shown; call again with offset=%d]' % (offset + len(piece), total, offset + max_chars)
        elif cut:
            footer = '\n…[download truncated at %d bytes]' % config.FETCH_MAX_BYTES
        return header + piece + footer

    @registry.tool('web_search', 'Search the web. Returns numbered results with title, URL and snippet; use fetch_url to read one. '
                   'Cite sources by URL in your answer.',
                   {'type': 'object', 'properties': {
                       'query': {'type': 'string', 'maxLength': 400},
                       'max_results': {'type': 'integer', 'description': '1-10, default 6'}},
                    'required': ['query']}, category='web', available=lambda ctx: search_backend() != 'off')
    def web_search(ctx, query, max_results=6):
        query = (query or '').strip()
        if not query:
            raise ToolError('query is required.')
        max_results = max(1, min(int(max_results or 6), 10))
        backend = search_backend()
        results = SEARCHERS[backend](query, max_results)
        if not results:
            return 'No results from ' + backend + ' for: ' + query
        lines = ['Search backend: ' + backend]
        for i, r in enumerate(results[:max_results], 1):
            lines.append('[%d] %s\n    %s\n    %s' % (i, r.get('title') or '(untitled)', r.get('url', ''), (r.get('snippet') or '').strip()[:400]))
        return '\n'.join(lines)


def search_backend():
    if config.SEARCH_BACKEND != 'auto':
        return config.SEARCH_BACKEND
    if config.TAVILY_API_KEY:
        return 'tavily'
    if config.BRAVE_API_KEY:
        return 'brave'
    if config.SEARXNG_URL:
        return 'searxng'
    return 'duckduckgo'


def _search_tavily(query, n):
    try:
        res = requests.post('https://api.tavily.com/search', json={'api_key': config.TAVILY_API_KEY, 'query': query, 'max_results': n,
                                                                   'include_answer': False}, timeout=20)
        res.raise_for_status()
        data = res.json()
    except (requests.RequestException, ValueError) as exc:
        raise ToolError('Tavily search failed: ' + type(exc).__name__) from exc
    return [{'title': r.get('title'), 'url': r.get('url'), 'snippet': r.get('content')} for r in data.get('results', [])]


def _search_brave(query, n):
    try:
        res = requests.get('https://api.search.brave.com/res/v1/web/search', params={'q': query, 'count': n},
                           headers={'X-Subscription-Token': config.BRAVE_API_KEY, 'Accept': 'application/json'}, timeout=20)
        res.raise_for_status()
        data = res.json()
    except (requests.RequestException, ValueError) as exc:
        raise ToolError('Brave search failed: ' + type(exc).__name__) from exc
    return [{'title': r.get('title'), 'url': r.get('url'), 'snippet': r.get('description')}
            for r in (data.get('web') or {}).get('results', [])]


def _search_searxng(query, n):
    base = config.SEARXNG_URL.rstrip('/')
    try:
        res = requests.get(base + '/search', params={'q': query, 'format': 'json'}, timeout=20, headers={'User-Agent': USER_AGENT})
        res.raise_for_status()
        data = res.json()
    except (requests.RequestException, ValueError) as exc:
        raise ToolError('SearXNG search failed: ' + type(exc).__name__) from exc
    return [{'title': r.get('title'), 'url': r.get('url'), 'snippet': r.get('content')} for r in data.get('results', [])[:n]]


_DDG_RESULT = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?(?:<a[^>]+class="result__snippet"[^>]*>(.*?)</a>|<td[^>]*class="result__snippet"[^>]*>(.*?)</td>)?', re.S)


def _search_duckduckgo(query, n):
    try:
        res = requests.get('https://html.duckduckgo.com/html/', params={'q': query}, timeout=20,
                           headers={'User-Agent': USER_AGENT, 'Accept-Language': 'en'})
        res.raise_for_status()
        page = res.text
    except requests.RequestException as exc:
        raise ToolError('DuckDuckGo search failed: ' + type(exc).__name__ + '. Configure TAVILY_API_KEY, BRAVE_API_KEY or SEARXNG_URL for a reliable backend.') from exc
    results = []
    for href, title, snip_a, snip_b in _DDG_RESULT.findall(page):
        url = html.unescape(href)
        if 'uddg=' in url:
            from urllib.parse import parse_qs, unquote
            qs = parse_qs(urlparse(url).query)
            url = unquote(qs.get('uddg', [url])[0])
        if url.startswith('//'):
            url = 'https:' + url
        strip = lambda s: html.unescape(re.sub(r'<[^>]+>', '', s or '')).strip()
        results.append({'title': strip(title), 'url': url, 'snippet': strip(snip_a or snip_b)})
        if len(results) >= n:
            break
    return results


SEARCHERS = {'tavily': _search_tavily, 'brave': _search_brave, 'searxng': _search_searxng, 'duckduckgo': _search_duckduckgo,
             'off': lambda q, n: []}
