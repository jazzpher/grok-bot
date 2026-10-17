"""Command execution backends.

* ``docker``      — one ephemeral container per command, workspace mounted at
                    /workspace, no network by default, memory/CPU/pid limits.
                    Strong isolation; requires the docker CLI and a pulled image.
* ``subprocess``  — runs on the host as the server user inside the workspace
                    directory with a scrubbed environment, CPU/file-size/process
                    rlimits (POSIX), a process group that is killed on timeout and
                    a bounded output buffer. **Not** a security boundary: it cannot
                    block network or writes outside the workspace. The approval
                    heuristics below exist because of that gap.
* ``off``         — run_command is unavailable.
"""
import dataclasses
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from typing import Optional

from grokbot import config

READ_ONLY_BINARIES = {'ls', 'cat', 'head', 'tail', 'wc', 'grep', 'rg', 'find', 'fd', 'tree', 'file', 'stat', 'du', 'df',
                      'pwd', 'echo', 'printf', 'env', 'which', 'whoami', 'date', 'uname', 'sort', 'uniq', 'cut', 'awk',
                      'sed', 'diff', 'jq', 'less', 'more', 'strings', 'xxd', 'hexdump', 'md5sum', 'sha256sum', 'basename',
                      'dirname', 'readlink', 'type', 'true', 'false', 'test', 'nproc', 'id'}
READ_ONLY_GIT = {'status', 'log', 'diff', 'show', 'branch', 'blame', 'ls-files', 'rev-parse', 'remote', 'tag', 'describe', 'shortlog', 'grep'}
SHELL_META = re.compile(r'(>>?|\||;|&&|\|\||`|\$\(|<\()')
NETWORK_BINARIES = {'curl', 'wget', 'ssh', 'scp', 'sftp', 'nc', 'ncat', 'netcat', 'telnet', 'ftp', 'rsync', 'ping', 'nmap',
                    'pip', 'pip3', 'npm', 'npx', 'pnpm', 'yarn', 'bun', 'cargo', 'go', 'gem', 'composer', 'apt', 'apt-get',
                    'brew', 'conda', 'uv', 'poetry', 'docker', 'kubectl', 'gh', 'aws', 'gcloud', 'az'}
NETWORK_GIT = {'clone', 'fetch', 'pull', 'push', 'ls-remote', 'submodule'}
DESTRUCTIVE = [
    (re.compile(r'(^|[\s;&|])sudo\b'), 'uses sudo'),
    (re.compile(r'(^|[\s;&|])rm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\b'), 'recursive force delete'),
    (re.compile(r'(^|[\s;&|])rm\s+-[a-zA-Z]*r'), 'recursive delete'),
    (re.compile(r'\b(mkfs|fdisk|parted|diskutil)\b'), 'disk formatting/partitioning'),
    (re.compile(r'(^|[\s;&|])dd\s+'), 'raw disk write (dd)'),
    (re.compile(r'\b(shutdown|reboot|halt|poweroff)\b'), 'power control'),
    (re.compile(r'\bchmod\s+(-R\s+)?[0-7]*777\b'), 'world-writable permissions'),
    (re.compile(r'\b(chown|chmod)\s+-R\s+\S+\s+/(\s|$)'), 'recursive permission change at /'),
    (re.compile(r'(curl|wget)[^|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b'), 'pipes a download into a shell'),
    (re.compile(r'\bgit\s+push\s+.*(--force|-f)\b'), 'force push'),
    (re.compile(r'\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--\s+\.)'), 'discards uncommitted work'),
    (re.compile(r':\(\)\s*\{\s*:\|:&\s*\};:'), 'fork bomb'),
    (re.compile(r'\b(kill|pkill|killall)\s+(-9\s+)?-1\b'), 'kills all processes'),
    (re.compile(r'>\s*/dev/(sd|nvme|disk)'), 'raw device write'),
    (re.compile(r'\bcrontab\s+-r\b'), 'erases crontab'),
]

# Refused outright, in every policy mode (even full-access + never). Not a complete list: it only catches the
# unmistakably catastrophic forms. Everything else relies on the approval gate and the sandbox backend.
HARDLINE = [
    (re.compile(r'(^|[\s;&|])rm\s+(-[a-zA-Z]+\s+)*(/|~|\$HOME|/\*|~/\*)(\s|$)', re.I), 'deletes the filesystem root or home folder'),
    (re.compile(r'\bmkfs(\.\w+)?\b'), 'formats a disk'),
    (re.compile(r'\bdd\b[^\n]*\bof=/dev/'), 'writes raw data to a disk device'),
    (re.compile(r'>\s*/dev/(sd|nvme|disk|hd)'), 'writes raw data to a disk device'),
    (re.compile(r':\(\)\s*\{\s*:\|:&\s*\};:'), 'is a fork bomb'),
    (re.compile(r'(^|[\s;&|])format\s+[a-z]:', re.I), 'formats a drive'),
    (re.compile(r'(^|[\s;&|])(del|erase)\s+[^\n]*(/s)[^\n]*\b[a-z]:[\\/]*(\s|$)', re.I), 'deletes a whole drive'),
    (re.compile(r'(^|[\s;&|])(rd|rmdir)\s+[^\n]*(/s)[^\n]*\b[a-z]:[\\/]*(\s|$)', re.I), 'deletes a whole drive'),
    (re.compile(r'remove-item\s+[^\n]*-recurse[^\n]*\b[a-z]:\\?(\s|$)', re.I), 'deletes a whole drive'),
    (re.compile(r'\b(diskpart|bcdedit|cipher\s+/w)\b', re.I), 'changes disks or the boot configuration'),
    (re.compile(r'(curl|wget)[^|\n]*\|\s*(sudo\s+)?(ba|z|da)?sh\b'), 'pipes a download into a shell'),
    (re.compile(r'(iwr|invoke-webrequest|irm|invoke-restmethod)[^|\n]*\|\s*(iex|invoke-expression)\b', re.I), 'pipes a download into PowerShell'),
]


def hardline_reason(command):
    """Reason string for commands that are always refused, else None."""
    for pattern, reason in HARDLINE:
        if pattern.search(command):
            return 'Refused: this command ' + reason + '. It is blocked in every policy mode.'
    return None


@dataclasses.dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: Optional[int]
    duration: float
    timed_out: bool = False
    truncated: bool = False
    backend: str = ''
    isolated: bool = False

    def render(self):
        parts = []
        if self.stdout:
            parts.append(self.stdout.rstrip('\n'))
        if self.stderr:
            parts.append('[stderr]\n' + self.stderr.rstrip('\n'))
        status = 'exit code %s' % self.exit_code if self.exit_code is not None else 'no exit code'
        if self.timed_out:
            status = 'TIMED OUT after %.0fs (process group killed)' % self.duration
        tail = '[%s · %.1fs · %s%s%s]' % (status, self.duration, self.backend,
                                         '' if self.isolated else ' · no isolation',
                                         ' · output truncated' if self.truncated else '')
        return ('\n'.join(parts) + '\n' if parts else '(no output)\n') + tail


def tokens(command):
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def destructive_reason(command):
    for pattern, reason in DESTRUCTIVE:
        if pattern.search(command):
            return 'Command ' + reason + '.'
    return None


def is_read_only_command(command):
    if SHELL_META.search(command):
        return False
    toks = tokens(command)
    if not toks:
        return False
    head = os.path.basename(toks[0])
    if head == 'git':
        return len(toks) > 1 and toks[1] in READ_ONLY_GIT
    if head in ('python', 'python3') and len(toks) >= 3 and toks[1] == '-c' and 'open(' not in toks[2] and 'import os' not in toks[2]:
        return False  # Python one-liners can do anything; keep them under approval in read-only mode.
    return head in READ_ONLY_BINARIES


def uses_network(command):
    toks = tokens(command)
    if not toks:
        return False
    for i, tok in enumerate(toks):
        head = os.path.basename(tok)
        if head in NETWORK_BINARIES:
            return True
        if head == 'git' and i + 1 < len(toks) and toks[i + 1] in NETWORK_GIT:
            return True
    return bool(re.search(r'https?://', command))


# Windows-style references. shlex (POSIX mode) eats backslashes, so these scan the raw command text.
WIN_DRIVE = re.compile(r'(?<![A-Za-z0-9_])[A-Za-z]:(?![A-Za-z0-9_/\\.-]*://)')   # C:\x, C:/x and drive-relative C:x
WIN_UNC = re.compile(r'(^|[\s"\'=(])(\\\\|//)[^\s/\\]')                     # \\server\share, //server/share
WIN_ENV = re.compile(r'%[A-Za-z_]+%|\$env:|\$\{?env:', re.I)                    # %USERPROFILE%, $env:APPDATA
WIN_UP = re.compile(r'(^|[\s"\'=\\/])\.\.([\\/]|$|\s|"|\')')                     # ..\x, ..\
WIN_ROOT = re.compile(r'(^|[\s"\'=])\\(?![\\])[^\s]')                              # \Windows\x (drive root of current drive)


def escapes_workspace(command, root):
    """Best-effort: absolute paths outside the workspace, ~ or cd .. style escapes, Windows drive/UNC paths."""
    root = str(root) if re.match(r'^[A-Za-z]:[\\/]', str(root)) else str(pathlib.Path(root).resolve())
    if WIN_UNC.search(command) or WIN_ENV.search(command) or WIN_UP.search(command) or WIN_ROOT.search(command):
        return True
    for m in WIN_DRIVE.finditer(command):
        rest = command[m.start():]
        # A path inside the workspace root is fine (Windows hosts: root looks like C:\\Users\\x\\ws).
        norm = rest.replace('\\', '/').lower()
        if not norm.startswith(root.replace('\\', '/').lower() + '/') and norm.rstrip('"\' ') != root.replace('\\', '/').lower():
            return True
    for tok in tokens(command):
        if tok.startswith('~'):
            return True
        if tok.startswith('/') and not tok.startswith(root) and not tok.startswith('/dev/null') and not tok.startswith('/workspace'):
            if re.match(r'^/(usr|bin|sbin|lib|opt|etc|proc|sys|tmp|var|home|root|Users|mnt|media|srv|boot|dev)(/|$)', tok) or tok == '/':
                return True
        if tok in ('..', '../') or tok.startswith('../') or '/../' in tok:
            return True
    return False


class SubprocessBackend:
    name = 'subprocess'
    isolated = False

    def available(self):
        return True

    def run(self, command, cwd, timeout, network=False, read_only=False):
        env = {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'), 'HOME': str(cwd), 'LANG': 'C.UTF-8',
               'LC_ALL': 'C.UTF-8', 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1', 'TERM': 'dumb',
               'TMPDIR': str(cwd), 'GROKBOT_SANDBOX': 'subprocess', 'NO_COLOR': '1', 'CI': '1'}
        for key in ('SYSTEMROOT', 'SystemRoot', 'COMSPEC', 'PATHEXT', 'WINDIR', 'TEMP', 'TMP'):
            if key in os.environ:
                env[key] = os.environ[key]
        if network:
            env['GROKBOT_NETWORK'] = '1'
        posix = os.name == 'posix'
        preexec = None
        if posix:
            def preexec():
                try:
                    import resource
                    cpu = int(timeout) + 5
                    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
                    resource.setrlimit(resource.RLIMIT_FSIZE, (config.MAX_FILE_BYTES * 4, config.MAX_FILE_BYTES * 4))
                    try:
                        soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
                        limit = 256 if hard == resource.RLIM_INFINITY else min(256, hard)
                        resource.setrlimit(resource.RLIMIT_NPROC, (limit, hard))
                    except (ValueError, OSError):
                        pass
                except Exception:  # noqa: BLE001 - never block execution on rlimit support
                    pass
        shell = ['/bin/sh', '-c', command] if posix else ['cmd', '/d', '/s', '/c', command]
        started = time.monotonic()
        popen_kwargs = {'preexec_fn': preexec, 'start_new_session': True} if posix else \
            {'creationflags': getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)}
        proc = subprocess.Popen(shell, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, **popen_kwargs)
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill(proc)
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                out, err = b'', b''
        duration = time.monotonic() - started
        stdout, t1 = _bound(out)
        stderr, t2 = _bound(err, limit=config.COMMAND_MAX_OUTPUT // 4)
        return CommandResult(stdout, stderr, None if timed_out else proc.returncode, duration, timed_out, t1 or t2, self.name, False)

    @staticmethod
    def _kill(proc):
        try:
            if os.name == 'posix':
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass


class DockerBackend:
    name = 'docker'
    isolated = True

    def __init__(self, image=None):
        self.image = image or config.SANDBOX_IMAGE

    def available(self):
        return shutil.which('docker') is not None

    def run(self, command, cwd, timeout, network=False, read_only=False):
        name = 'grokbot-' + str(int(time.time() * 1000)) + '-' + str(os.getpid())
        mount = str(pathlib.Path(cwd).resolve()) + ':/workspace' + (':ro' if read_only else ':rw')
        args = ['docker', 'run', '--rm', '--name', name, '--network', 'bridge' if network else 'none',
                '--memory', config.SANDBOX_MEMORY, '--cpus', config.SANDBOX_CPUS, '--pids-limit', '256',
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                # Read-only root filesystem (as docs/SECURITY.md promises); /tmp stays
                # available as a bounded, noexec tmpfs; only the workspace mount is writable.
                '--read-only', '--tmpfs', '/tmp:rw,noexec,nosuid,size=256m',
                '-v', mount, '-w', '/workspace',
                '-e', 'HOME=/workspace', '-e', 'TERM=dumb', '-e', 'NO_COLOR=1', '-e', 'CI=1', '-e', 'PYTHONDONTWRITEBYTECODE=1']
        if os.name == 'posix':
            args += ['--user', '%d:%d' % (os.getuid(), os.getgid())]
        # The command's own budget is enforced inside the container with coreutils/busybox `timeout` (SIGKILL,
        # exit 137); the host-side limit below only adds start-up grace (image pull, daemon latency).
        wrapper = ('if command -v timeout >/dev/null 2>&1; then exec timeout -s KILL "$0" sh -c "$1"; '
                   'else exec sh -c "$1"; fi')
        args += [self.image, 'sh', '-c', wrapper, str(int(timeout)), command]
        started = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout + 10)
            out, err, code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            subprocess.run(['docker', 'kill', name], capture_output=True, timeout=15)
            out, err, code = exc.stdout or b'', exc.stderr or b'', None
        duration = time.monotonic() - started
        if code in (124, 137) and duration >= timeout - 0.5 and not timed_out:
            timed_out = True   # killed by the in-container `timeout`
        stdout, t1 = _bound(out)
        stderr, t2 = _bound(err, limit=config.COMMAND_MAX_OUTPUT // 4)
        if code == 125 and b'docker' in (err or b'').lower():
            stderr = 'Docker could not start the sandbox container (is the daemon running and the image "%s" pulled?).\n' % self.image + stderr
        return CommandResult(stdout, stderr, code, duration, timed_out, t1 or t2, self.name + ':' + self.image, True)


class DisabledBackend:
    name = 'off'
    isolated = False

    def available(self):
        return False

    def run(self, *a, **k):
        raise RuntimeError('Command execution is disabled (GROK_SANDBOX_BACKEND=off).')


def _bound(raw, limit=None):
    limit = limit or config.COMMAND_MAX_OUTPUT
    text = (raw or b'').decode('utf-8', errors='replace')
    if len(text) <= limit:
        return text, False
    head = limit * 2 // 3
    return text[:head] + '\n…[%d characters omitted]…\n' % (len(text) - limit) + text[-(limit - head):], True


_BACKENDS = {'docker': DockerBackend, 'subprocess': SubprocessBackend, 'off': DisabledBackend}


def get_backend(name=None):
    name = name or config.resolve_sandbox_backend()
    backend = _BACKENDS.get(name, SubprocessBackend)()
    if name == 'docker' and not backend.available():
        backend = SubprocessBackend()
    return backend


def describe():
    backend = get_backend()
    return {'backend': backend.name, 'isolated': backend.isolated, 'image': getattr(backend, 'image', None),
            'python': sys.executable, 'timeout': config.COMMAND_TIMEOUT}
