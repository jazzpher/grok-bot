"""Bounded, read-only model catalogs. A listing is not a chat/quota test."""
import hashlib
import hmac
import json
import secrets
import threading
import time
from urllib.parse import urljoin, urlparse

import requests

# Explicit endpoints, not derived from chat URLs. NVIDIA hosted catalog is best-effort.
MODEL_URLS = {
    'gemini': 'https://generativelanguage.googleapis.com/v1beta/openai/models',
    'nvidia': 'https://integrate.api.nvidia.com/v1/models',
    'openrouter': 'https://openrouter.ai/api/v1/models',
    'groq': 'https://api.groq.com/openai/v1/models',
    'cerebras': 'https://api.cerebras.ai/v1/models',
    'mistral': 'https://api.mistral.ai/v1/models',
    'deepseek': 'https://api.deepseek.com/models',
    'xai': 'https://api.x.ai/v1/models',
}
MAX_PAGES, MAX_MODELS, MAX_BYTES = 5, 2000, 3_000_000
CACHE_TTL = 300
_cache, _lock, _salt = {}, threading.Lock(), secrets.token_bytes(32)


class DiscoveryError(ValueError):
    pass


def clear_cache():
    with _lock:
        _cache.clear()


def origin(url):
    p = urlparse(url)
    return p.scheme, p.hostname, p.port or 443


def same_catalog(base, candidate):
    """Pagination cannot change the origin, path, credentials or fragment."""
    try:
        a, b = urlparse(base), urlparse(candidate)
        return (origin(base) == origin(candidate) and a.path == b.path
                and not b.username and not b.password and not b.fragment)
    except ValueError:
        return False


def discover(key, endpoint, provider='', refresh=False):
    fingerprint = hmac.new(_salt, key.encode(), hashlib.sha256).digest()
    cache_key = (provider, endpoint, fingerprint)
    with _lock:
        cached = _cache.get(cache_key)
        if not refresh and cached and time.monotonic() - cached[0] < CACHE_TTL:
            return dict(cached[1], cached=True)
    models, seen_ids, seen_pages = [], set(), set()
    url, params, total = endpoint, {}, 0
    started = time.monotonic()
    if provider == 'openrouter':
        params = {'offset': 0, 'limit': 500}
    for page in range(MAX_PAGES):
        if time.monotonic() - started > 20:
            raise DiscoveryError('Model listing took too long. Enter a model manually.')
        signature = (url, json.dumps(params, sort_keys=True))
        if signature in seen_pages:
            raise DiscoveryError('Model listing repeated a page. Enter a model manually.')
        seen_pages.add(signature)
        # No redirects, including same-origin ones: never forward a credential speculatively.
        try:
            with requests.get(url, params=params, headers={'Authorization': 'Bearer ' + key},
                              timeout=(5, 8), stream=True, allow_redirects=False) as res:
                if 300 <= res.status_code < 400:
                    raise DiscoveryError('Model listing redirected; not followed. Enter a model manually.')
                if res.status_code in (401, 403):
                    raise DiscoveryError('The provider denied model listing. Check the key or enter a model manually.')
                if res.status_code == 429:
                    raise DiscoveryError('Model listing is rate limited. Try Refresh later or enter a model manually.')
                if res.status_code != 200:
                    raise DiscoveryError('Model listing is unavailable (HTTP %d). Enter a model manually.' % res.status_code)
                body = bytearray()
                for chunk in res.iter_content(8192):
                    total += len(chunk)
                    if total > MAX_BYTES or time.monotonic() - started > 20:
                        raise DiscoveryError('Model listing exceeded its size/time limit. Enter a model manually.')
                    body.extend(chunk)
                data = json.loads(body)
        except DiscoveryError:
            raise
        except (requests.RequestException, ValueError, TypeError):
            # Provider bodies/exception strings can contain credentials, URLs or HTML.
            raise DiscoveryError('Could not read the model list. Check your connection or enter a model manually.') from None
        rows = data.get('data') if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise DiscoveryError('The provider returned an unsupported model list. Enter a model manually.')
        for row in rows:
            mid = row.get('id') if isinstance(row, dict) else None
            if not isinstance(mid, str) or not mid.strip() or len(mid) > 256 or any(ord(c) < 32 for c in mid):
                continue
            # Only filter when the provider explicitly declares chat unsupported.
            caps = row.get('capabilities')
            if isinstance(caps, dict) and caps.get('completion_chat') is False:
                continue
            if mid not in seen_ids:
                if len(models) >= MAX_MODELS:
                    raise DiscoveryError('Too many models to list safely. Enter a model manually.')
                seen_ids.add(mid)
                models.append(mid)
        params = {}
        links = data.get('links')
        next_link = links.get('next') if isinstance(links, dict) else None
        token = data.get('nextPageToken')
        if next_link:
            if not isinstance(next_link, str) or len(next_link) > 2048:
                raise DiscoveryError('Unsafe model pagination. Enter a model manually.')
            next_url = urljoin(endpoint, next_link)
            if not same_catalog(endpoint, next_url):
                raise DiscoveryError('Unsafe model pagination. Enter a model manually.')
            url = next_url
        elif token:
            if not isinstance(token, str) or len(token) > 2048:
                raise DiscoveryError('Unsupported model pagination. Enter a model manually.')
            url, params = endpoint, {'pageToken': token}
        elif data.get('has_more') is True:
            last = data.get('last_id')
            if not isinstance(last, str) or not last or len(last) > 256:
                raise DiscoveryError('Unsupported model pagination. Enter a model manually.')
            url, params = endpoint, {'after': last}
        else:
            result = {'models': sorted(models), 'cached': False,
                      'message': ('Choose a model or type one manually. Listed does not mean chat support or free quota.'
                                  if models else 'No models returned. Enter a model manually.')}
            if provider == 'nvidia':
                result['message'] += ' NVIDIA hosted listing is best-effort, not verified with your key.'
            elif not provider:
                result['message'] += ' Custom/Token Harbor listing is best-effort.'
            with _lock:
                if len(_cache) >= 64:
                    _cache.clear()
                _cache[cache_key] = (time.monotonic(), result)
            return result
    raise DiscoveryError('Model listing exceeded its page limit. Enter a model manually.')
