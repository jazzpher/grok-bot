"""Outbound URL safety shared by provider settings and the fetch_url tool.

A URL is acceptable when it is a public internet address: no userinfo, no
private/loopback/link-local/multicast/reserved targets (by literal IP or by
DNS resolution). The HTTP client re-resolves DNS at request time, so this is
best-effort hardening against SSRF, not a guarantee against hostile rebinding.
``resolve_pinned`` returns the vetted IPs so callers can pin them when needed.
"""
import ipaddress
import socket
from urllib.parse import urlparse

MAX_URL_LENGTH = 2048


def _bad_ip(ip):
    # not is_global also covers ranges the is_private list misses, notably 100.64.0.0/10 (carrier-grade NAT, used by
    # Tailscale for every device on a tailnet), 192.0.0.0/24 and the IPv6 special-purpose blocks.
    return (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified)


def resolve_public_ips(host, port):
    """Return a list of public IPs for host, or an empty list when unverifiable/unsafe."""
    try:
        return [] if _bad_ip(ipaddress.ip_address(host)) else [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        return []
    ips = []
    for info in infos:
        try:
            ips.append(ipaddress.ip_address(info[4][0]))
        except (ValueError, IndexError, TypeError):
            continue
    if not ips or any(_bad_ip(ip) for ip in ips):
        return []
    return ips


def public_url_error(url, schemes=('https',), max_length=200, what='Custom endpoints'):
    """None when url is a safe public address, else a short plain-language reason."""
    if not isinstance(url, str) or len(url) > max_length:
        return 'That URL is too long.' if isinstance(url, str) else 'That URL is not valid.'
    try:
        p = urlparse(url)
    except ValueError:
        return 'That URL is not valid.'
    if p.scheme not in schemes:
        return what + ' must use ' + ' or '.join(schemes) + '.'
    if p.username or p.password:
        return 'URLs with a username or password are not allowed.'
    host = p.hostname
    if not host:
        return 'That URL is not valid.'
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return None if not _bad_ip(literal) else what + ' must be public internet addresses.'
    try:
        port = p.port or (443 if p.scheme == 'https' else 80)
    except ValueError:
        return 'That URL is not valid.'
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        return 'Could not verify that host. Pick a listed provider or a public https endpoint.'
    ips = []
    for info in infos:
        try:
            ips.append(ipaddress.ip_address(info[4][0]))
        except (ValueError, IndexError, TypeError):
            pass
    if not ips:
        return 'Could not verify that host. Pick a listed provider or a public https endpoint.'
    if any(_bad_ip(ip) for ip in ips):
        return what + ' must be public internet addresses.'
    return None
