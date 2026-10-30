"""Fail-closed vetting proxy for the isolated agent browser (experimental).

The agent browser's Chromium runs inside a network namespace with no direct
egress (see grokbot/browser_isolation.py), so every byte it wants from the
network must pass through this proxy. The proxy is the enforcement point the
Playwright route handler cannot be: Chromium follows redirect hops without
telling Playwright, but with no direct egress every hop - redirects included -
arrives here as a fresh connection request and is vetted BEFORE anything is
sent.

Rules (mirroring the agent browser's existing policy, fail closed):

* Only CONNECT (https/wss) and absolute-form plain-http requests, ports 80/443.
* The destination host must be in the session's approved set - the same
  per-domain approval the user already grants for top-level documents, now
  enforced for every destination host, including redirect hops and
  subresources. An unapproved host is refused before any request is sent and
  the refusal is recorded so the tool can name the host to the user.
* The host must resolve to public internet addresses. Resolution happens here
  and the connection goes to the pinned IP, so Chromium never does DNS and a
  DNS-rebinding flip after the check cannot redirect the connection.
* Literal IPs are checked directly; loopback/private/LAN/reserved (including
  CGNAT 100.64/10) are refused, approved set or not.

Known limits: the proxy sees host:port per tunnel, not URLs or whether a
request is a navigation or a subresource, so policy here is host-scoped only.
HTTP/2 connection coalescing inside Chromium could in principle reuse one
tunnel for a second origin that shares IP+certificate; the namespace isolation
bounds what such reuse can reach (approved, public hosts only), but per-origin
separation inside a tunnel is not enforceable at this layer.
"""
import ipaddress
import socket
import threading
import time

from grokbot import netguard

ALLOWED_PORTS = (80, 443)
_MAX_HEADER_BYTES = 64 * 1024
_CONNECT_TIMEOUT = 15
_MAX_CONCURRENT = 128
_IDLE_TIMEOUT = 120

_BLOCKED_HOSTS = {'localhost', '0.0.0.0', '::1', '[::1]'}
_BLOCKED_SUFFIXES = ('.local', '.internal', '.localhost', '.arpa')


def _norm(host):
    return (host or '').strip().lower().rstrip('.')


class VettingProxy:
    """Threaded fail-closed forward proxy. allowed_hosts() is consulted per connection."""

    def __init__(self, allowed_hosts, resolver=None, allowed_ports=ALLOWED_PORTS,
                 ip_check=None, max_concurrent=_MAX_CONCURRENT):
        """allowed_hosts: callable returning the current set of approved hostnames.
        resolver: (host, port) -> list[ipaddress] of candidate connect targets, or [] to
        refuse. Default is netguard.resolve_public_ips (public addresses only). The
        parameter exists so tests can map fake public hostnames to a local server.
        allowed_ports defaults to 80/443 (browsing); tests pass their ephemeral port.
        ip_check: ipaddress -> True when the address is forbidden (default
        netguard._bad_ip). Every resolved IP is checked here, not trusted from the
        resolver, so a mid-session public-to-private flip is refused even if a resolver
        ever violated its contract."""
        self._allowed_hosts = allowed_hosts
        self._resolver = resolver or netguard.resolve_public_ips
        self._ip_check = ip_check or netguard._bad_ip
        self._allowed_ports = tuple(allowed_ports)
        self._denials = []
        self._lock = threading.Lock()
        self._sockets = []
        self._conns = set()
        self._threads = []
        self._sema = threading.BoundedSemaphore(max_concurrent)
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def listen_tcp(self, host='127.0.0.1', port=0):
        """Bind a TCP listener (used by tests); returns the bound port."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(64)
        sock.settimeout(0.5)
        self._sockets.append(sock)
        self._spawn(self._accept_loop, sock)
        return sock.getsockname()[1]

    def listen_unix(self, path):
        """Bind a unix-socket listener (the path the isolated relay connects to)."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path)
        sock.listen(64)
        sock.settimeout(0.5)
        self._sockets.append(sock)
        self._spawn(self._accept_loop, sock)
        return path

    def close(self):
        """Stop listening AND tear down every in-flight connection, then join workers."""
        self._closed = True
        for sock in self._sockets:
            try:
                sock.close()
            except OSError:
                pass
        self._sockets = []
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        deadline = time.time() + 5
        for t in list(self._threads):
            t.join(max(0.0, deadline - time.time()))

    def _spawn(self, fn, *args):
        self._threads = [t for t in self._threads if t.is_alive()]
        thread = threading.Thread(target=fn, args=args, daemon=True)
        thread.start()
        self._threads.append(thread)

    # -- denials -----------------------------------------------------------

    def drain_denials(self):
        """Return and clear the recorded (host, port, reason) refusals since the last call."""
        with self._lock:
            out, self._denials = self._denials, []
        return out

    def _deny(self, host, port, reason):
        with self._lock:
            self._denials.append((host, port, reason))
            if len(self._denials) > 100:
                self._denials = self._denials[-100:]

    # -- vetting -----------------------------------------------------------

    def _vet(self, host, port):
        """(pinned_ip, None) when the connection may proceed, else (None, reason)."""
        host = _norm(host)
        if not host or len(host) > 253 or any(c in host for c in '/\\@ \t'):
            return None, 'not a valid host'
        if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
            return None, 'local hostnames are not allowed'
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            if self._ip_check(literal):
                return None, 'local and private addresses are not allowed'
            if host not in self._allowed_hosts():
                return None, 'host not approved'
            return literal, None
        # Approval is checked before DNS so an unapproved host never even
        # triggers a lookup.
        if host not in self._allowed_hosts():
            return None, 'host not approved'
        try:
            ips = self._resolver(host, port)
        except Exception:
            return None, 'could not verify that host'
        if not ips:
            return None, 'host does not resolve to a public address'
        # Re-vet every resolved address here: the resolver contract is a promise,
        # this check is the enforcement. A rebinding flip to a private address is
        # refused even if it slips past a resolver.
        if any(self._ip_check(ip) for ip in ips):
            return None, 'local and private addresses are not allowed'
        return ips[0], None

    # -- connection handling ----------------------------------------------

    def _accept_loop(self, sock):
        while not self._closed:
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._spawn(self._handle, conn)

    def _read_head(self, rfile):
        """Read request line + headers; returns (request_line, headers_blob) or None."""
        data = b''
        while b'\r\n\r\n' not in data:
            chunk = rfile.read1(_MAX_HEADER_BYTES) if hasattr(rfile, 'read1') else rfile.recv(4096)
            if not chunk:
                return None
            data += chunk
            if len(data) > _MAX_HEADER_BYTES:
                return None
        return data

    def _handle(self, conn):
        if not self._sema.acquire(blocking=False):
            try:
                conn.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n')
            except OSError:
                pass
            conn.close()
            return
        with self._lock:
            self._conns.add(conn)
        try:
            self._handle_inner(conn)
        finally:
            with self._lock:
                self._conns.discard(conn)
            self._sema.release()

    def _handle_inner(self, conn):
        try:
            conn.settimeout(_CONNECT_TIMEOUT)
            rfile = conn.makefile('rb')
            head = self._read_head(rfile)
            if head is None:
                conn.close()
                return
            request_line = head.split(b'\r\n', 1)[0].decode('latin-1')
            parts = request_line.split(' ')
            if len(parts) < 2:
                conn.close()
                return
            method, target = parts[0].upper(), parts[1]
            if method == 'CONNECT':
                self._handle_connect(conn, rfile, head, target)
            elif target.lower().startswith('http://'):
                self._handle_plain(conn, head, method, target)
            else:
                conn.sendall(b'HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n')
                conn.close()
        except (OSError, ValueError):
            try:
                conn.close()
            except OSError:
                pass

    def _connect_out(self, host, port):
        """Open the upstream connection after vetting; returns (sock, error_reason)."""
        if port not in self._allowed_ports:
            self._deny(host, port, 'only ports 80 and 443 are allowed')
            return None, 'only ports 80 and 443 are allowed'
        ip, reason = self._vet(host, port)
        if ip is None:
            self._deny(host, port, reason)
            return None, reason
        try:
            upstream = socket.create_connection((str(ip), port), timeout=_CONNECT_TIMEOUT)
        except OSError:
            return None, 'could not connect'
        upstream.settimeout(_IDLE_TIMEOUT)
        with self._lock:
            self._conns.add(upstream)
        return upstream, None

    def _handle_connect(self, conn, rfile, head, target):
        host, _, port_s = target.rpartition(':')
        host = _norm(host.strip('[]'))
        try:
            port = int(port_s)
        except ValueError:
            port = 0
        upstream, reason = self._connect_out(host, port)
        if upstream is None:
            body = ('grok-bot browse proxy: %s\r\n' % reason).encode()
            conn.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nContent-Length: '
                         + str(len(body)).encode() + b'\r\n\r\n' + body)
            conn.close()
            return
        conn.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        _, _, leftover = head.partition(b'\r\n\r\n')
        if leftover:
            upstream.sendall(leftover)
        self._tunnel(conn, upstream)

    def _handle_plain(self, conn, head, method, target):
        from urllib.parse import urlsplit
        try:
            parsed = urlsplit(target)
        except ValueError:
            conn.close()
            return
        host = _norm(parsed.hostname)
        port = parsed.port or 80
        upstream, reason = self._connect_out(host, port)
        if upstream is None:
            body = ('grok-bot browse proxy: %s\r\n' % reason).encode()
            conn.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nContent-Length: '
                         + str(len(body)).encode() + b'\r\n\r\n' + body)
            conn.close()
            return
        # Rewrite absolute-form to origin-form and force a single request per
        # connection so one tunnel can never be reused for a second host.
        request_line, _, headers = head.partition(b'\r\n')
        path = (parsed.path or '/') + (('?' + parsed.query) if parsed.query else '')
        new_line = ('%s %s HTTP/1.1' % (method, path)).encode('latin-1')
        out = bytearray(new_line + b'\r\n')
        for line in headers.split(b'\r\n'):
            if line.lower().startswith((b'connection:', b'proxy-connection:', b'keep-alive:')):
                continue
            if not line:
                continue
            out += line + b'\r\n'
        _, _, leftover = head.partition(b'\r\n\r\n')
        out += b'Connection: close\r\n\r\n' + leftover
        try:
            upstream.sendall(bytes(out))
            while True:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                conn.sendall(chunk)
        except OSError:
            pass
        finally:
            self._forget(upstream)
            for s in (upstream, conn):
                try:
                    s.close()
                except OSError:
                    pass

    def _forget(self, sock):
        with self._lock:
            self._conns.discard(sock)

    def _tunnel(self, client, upstream):
        """Bidirectional byte copy with half-close: when one direction ends, the other
        still gets to flush its remaining bytes before the sockets close."""
        client.settimeout(_IDLE_TIMEOUT)

        def pump(src, dst):
            try:
                while True:
                    chunk = src.recv(65536)
                    if not chunk:
                        break
                    dst.sendall(chunk)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)         # signal EOF to the reader, keep our read side open
                except OSError:
                    pass

        t = threading.Thread(target=pump, args=(upstream, client), daemon=True)
        t.start()
        pump(client, upstream)
        self._forget(upstream)
        t.join(timeout=10)
        for s in (client, upstream):
            try:
                s.close()
            except OSError:
                pass
