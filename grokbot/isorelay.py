"""Loopback relay for the network-isolated agent browser (experimental).

Runs INSIDE the browser's network namespace, which has no external
interfaces: listens on 127.0.0.1:<port> and forwards each connection to the
vetting proxy's unix socket, which lives in a directory bind-mounted from the
parent namespace. This is the only network path the isolated Chromium has.

Standalone on purpose: no grokbot imports, so it can run by file path
(`python3 isorelay.py ...`) inside a minimal namespace.
"""
import socket
import sys
import threading

_IDLE_TIMEOUT = 120


def _pump(src, dst, done):
    try:
        while not done.is_set():
            chunk = src.recv(65536)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        done.set()
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _handle(client, sock_path):
    try:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        upstream.settimeout(15)
        upstream.connect(sock_path)
        upstream.settimeout(_IDLE_TIMEOUT)
    except OSError:
        client.close()
        return
    client.settimeout(_IDLE_TIMEOUT)
    done = threading.Event()
    t = threading.Thread(target=_pump, args=(upstream, client, done), daemon=True)
    t.start()
    _pump(client, upstream, done)
    t.join(timeout=5)
    for s in (client, upstream):
        try:
            s.close()
        except OSError:
            pass


def serve(port, sock_path):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', port))
    listener.listen(64)
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        threading.Thread(target=_handle, args=(client, sock_path), daemon=True).start()


def main(argv):
    # usage: isorelay.py <port> <unix-socket-path> [-- command args...]
    if len(argv) < 3:
        sys.stderr.write('usage: isorelay.py <port> <unix-socket> [-- command args...]\n')
        return 2
    port = int(argv[1])
    sock_path = argv[2]
    if len(argv) > 3 and argv[3] == '--':
        import os
        # The relay must be a separate process: exec replaces this one wholesale,
        # which would take a relay thread down with it. bwrap --unshare-pid makes
        # the browser the namespace init, so the relay dies with the browser.
        pid = os.fork()
        if pid == 0:
            serve(port, sock_path)
            os._exit(0)
        # Wait until the relay is actually accepting before starting the browser.
        for _ in range(100):
            try:
                probe = socket.create_connection(('127.0.0.1', port), timeout=0.2)
                probe.close()
                break
            except OSError:
                threading.Event().wait(0.05)
        os.execvp(argv[4], argv[4:])
    serve(port, sock_path)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
