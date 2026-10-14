"""run_command: shell execution through the configured sandbox backend + policy."""
from grokbot import config, sandbox
from grokbot.approvals import SandboxPolicy
from grokbot.tools import ToolError


def _policy(ctx):
    return ctx.policy or SandboxPolicy()


def _backend(ctx):
    backend = ctx.extras.get('sandbox_backend')
    if backend is None:
        backend = sandbox.get_backend()
        ctx.extras['sandbox_backend'] = backend
    return backend


def shell_available(ctx):
    return config.resolve_sandbox_backend() != 'off'


def command_approval(ctx, arguments):
    """Reason string when the command crosses the sandbox boundary, else None."""
    command = str(arguments.get('command', ''))
    policy = _policy(ctx)
    if policy.yolo:
        return None
    destructive = sandbox.destructive_reason(command)
    if destructive:
        return destructive
    if policy.mode == 'read-only':
        return None if sandbox.is_read_only_command(command) else 'Sandbox is read-only; only inspection commands run without approval.'
    backend = _backend(ctx)
    if policy.mode == 'workspace-write':
        if not backend.isolated:
            if sandbox.escapes_workspace(command, ctx.workspace.root):
                return 'Command references paths outside the workspace (subprocess backend cannot confine it).'
            if not policy.network and sandbox.uses_network(command):
                return 'Command appears to use the network, which is off in this policy.'
        return None
    return None


def register(registry):
    @registry.tool('run_command',
                   'Run a shell command in the workspace sandbox and return stdout/stderr/exit code. Use it to run code, '
                   'tests, git, package managers and file inspection. Commands run with cwd = workspace root; long or '
                   'interactive commands are killed at the timeout. Prefer non-interactive flags (e.g. -y, --no-input).',
                   {'type': 'object', 'properties': {
                       'command': {'type': 'string', 'maxLength': 8000},
                       'timeout': {'type': 'integer', 'description': 'Seconds (default %d, max %d)' % (config.COMMAND_TIMEOUT, config.COMMAND_TIMEOUT * 5)},
                       'description': {'type': 'string', 'description': 'Short human-readable purpose shown in the UI'}},
                    'required': ['command']}, category='shell', mutates=True,
                   needs_approval=command_approval, available=shell_available)
    def run_command(ctx, command, timeout=None, description=''):
        command = command.strip()
        if not command:
            raise ToolError('command must not be empty.')
        policy = _policy(ctx)
        backend = _backend(ctx)
        timeout = max(1, min(int(timeout or config.COMMAND_TIMEOUT), config.COMMAND_TIMEOUT * 5))
        ctx.emit({'type': 'command_start', 'command': command, 'description': description, 'backend': backend.name})
        try:
            result = backend.run(command, ctx.workspace.root, timeout, network=policy.network or policy.mode == 'full-access',
                                 read_only=policy.mode == 'read-only')
        except RuntimeError as exc:
            raise ToolError(str(exc)) from exc
        except OSError as exc:
            raise ToolError('Could not start the command: ' + str(exc)) from exc
        if ctx.workspace is not None:
            # Files may have changed under us; do not pretend read-back state is current.
            ctx.workspace.checked.clear()
        return result.render()
