"""Network isolation for the agent browser (experimental, Linux).

Chromium is launched inside a bubblewrap (bwrap) user+network namespace that
has no external interfaces: direct egress is impossible by construction, so
no browser feature - redirects, QUIC, service workers, proxy bypasses - can
reach the network without going through the vetting proxy
(grokbot/vetproxy.py). Inside the namespace a small relay (grokbot/isorelay.py)
offers the proxy on loopback; it forwards to the proxy's unix socket in a
bind-mounted directory.

Isolation is MANDATORY: when bwrap is unavailable the session refuses to
start rather than fall back to an unproxied browser.
"""
import os
import shutil
import stat
import tempfile

from browser_tools import BrowserError

WRAPPER_PREFIX = 'grok-browser-iso-'
RELAY_PORT = 17373


_AVAILABLE_CACHE = None

# Namespace flags every isolated launch runs with. --new-session: the browser is not a
# session leader and cannot acquire a controlling terminal; --cap-drop ALL: no effective
# capabilities inside; --unshare-ipc: host SysV IPC/POSIX queues are not visible.
_FLAGS = ('--unshare-net', '--unshare-pid', '--unshare-ipc', '--cap-drop', 'ALL',
          '--new-session', '--die-with-parent')

# Directories an operator-configured Chromium executable must NOT live in directly:
# mounting one of these read-only would expose a broad host tree (all of /opt, a home
# root, ...), which defeats the minimal-filesystem point of the sandbox.
_BROAD_RUNTIME_DIRS = frozenset(('/', '/home', '/root', '/opt', '/usr', '/usr/local',
                                 '/usr/lib', '/usr/lib64', '/bin', '/sbin', '/etc',
                                 '/var', '/var/lib', '/srv', '/mnt', '/media', '/tmp'))


def _runtime_mount_dir(chromium_path):
    """The read-only dir bound for the Chromium runtime: the executable's own real
    directory, and never a broad parent. A misconfigured GROK_BROWSER_EXECUTABLE is
    rejected, not widened."""
    chromium_dir = os.path.dirname(os.path.realpath(chromium_path))
    home = os.path.realpath(os.path.expanduser('~'))
    if (chromium_dir in _BROAD_RUNTIME_DIRS or chromium_dir == home
            or os.path.dirname(chromium_dir) == '/home'
            or len(chromium_dir.strip('/').split('/')) < 2):
        raise BrowserError(
            'Refusing to mount a broad directory (%s) into the browser sandbox: the '
            'Chromium executable must live in its own directory (e.g. '
            '/usr/lib/chromium/chrome or /opt/google/chrome/chrome). Fix '
            'GROK_BROWSER_EXECUTABLE or unset it to use the Playwright-managed browser.'
            % chromium_dir)
    return chromium_dir


def _base_mounts():
    """The minimal read-only runtime: system libraries plus the few /etc files Chromium
    and glibc actually read - never wholesale /etc (host config and any world-readable
    secrets must not be visible)."""
    mounts = ['--ro-bind /usr /usr', '--symlink usr/lib /lib', '--symlink usr/lib64 /lib64',
              '--symlink usr/bin /bin', '--symlink usr/sbin /sbin']
    for etc in ('/etc/fonts', '/etc/ssl/certs', '/etc/ca-certificates', '/etc/nsswitch.conf',
                '/etc/host.conf', '/etc/hosts', '/etc/resolv.conf', '/etc/passwd',
                '/etc/group', '/etc/localtime'):
        if os.path.exists(etc):
            mounts.append('--ro-bind %s %s' % (etc, etc))
    return mounts


def _base_args():
    """Flags + mounts + fixed tmpfs layout shared by the capability probe and the real
    wrapper, so available() proves the PRODUCTION setup works, not a generic bwrap."""
    return (' '.join(_FLAGS) + ' ' + ' '.join(_base_mounts()) +
            ' --dev /dev --tmpfs /dev/shm --proc /proc --tmpfs /tmp')


def available(refresh=False):
    """True when bubblewrap can ACTUALLY launch the production sandbox setup here -
    same namespace flags, cap drop, /etc allowlist and tmpfs layout the browser gets,
    not just an executable lookup - and cached per process."""
    global _AVAILABLE_CACHE
    if _AVAILABLE_CACHE is not None and not refresh:
        return _AVAILABLE_CACHE
    import shlex
    import subprocess
    path = shutil.which('bwrap')
    ok = False
    if path:
        try:
            ok = subprocess.run([path] + shlex.split(_base_args()) +
                                ['--tmpfs', '/run', '/usr/bin/true'],
                                capture_output=True, timeout=15).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
    _AVAILABLE_CACHE = ok
    return ok


class BrowserIsolation:
    """Owns the proxy socket directory and the Chromium wrapper script."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix=WRAPPER_PREFIX)
        self.proxy_sock = os.path.join(self.dir, 'proxy.sock')
        self.wrapper_path = os.path.join(self.dir, 'chromium-isolated.sh')

    def write_wrapper(self, chromium_path, extra_args):
        """Write the launcher Playwright uses as executable_path: bwrap + relay + Chromium.

        Namespace contents: no network except loopback (relay to the vetting proxy);
        a MINIMAL read-only filesystem (runtime libraries, the few /etc files Chromium
        and glibc actually read, the Chromium install dir, the relay script - the rest
        of the host filesystem simply is not there); a fresh minimal /dev; private /tmp
        and /run, so host path-based unix sockets and host files are unreachable; a
        FRESH /proc (bwrap --proc mounts a new procfs: only namespace processes are
        visible); no capabilities; isolated IPC. This is NOT a security boundary against
        a fully compromised renderer - it removes network paths and host data, it does
        not sandbox the process."""
        import shlex
        isorelay = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'isorelay.py')
        chromium_dir = _runtime_mount_dir(chromium_path)
        mounts = []
        if not (chromium_dir + '/').startswith('/usr/'):
            mounts.append('--ro-bind %s %s' % (shlex.quote(chromium_dir), shlex.quote(chromium_dir)))
        mounts.append('--ro-bind %s %s' % (shlex.quote(isorelay), shlex.quote(isorelay)))
        args = ' '.join(shlex.quote(a) for a in extra_args)
        script = (
            '#!/bin/sh\n'
            '# Chromium in a network namespace with no egress; its only network path is the\n'
            '# loopback relay to the vetting proxy. See grokbot/browser_isolation.py.\n'
            'exec bwrap ' + _base_args() + ' ' + ' '.join(mounts) +
            ' --ro-bind %s %s --tmpfs /run '
            '/usr/bin/env python3 %s %d %s -- %s %s "$@"\n'
            % (shlex.quote(self.dir), shlex.quote(self.dir),
               shlex.quote(isorelay), RELAY_PORT, shlex.quote(self.proxy_sock),
               shlex.quote(chromium_path), args)
        )
        with open(self.wrapper_path, 'w') as fh:
            fh.write(script)
        os.chmod(self.wrapper_path, stat.S_IRWXU)
        return self.wrapper_path

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)
