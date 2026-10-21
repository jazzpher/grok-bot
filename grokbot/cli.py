"""grokbot command line: interactive chat, one-shot exec (CI-friendly), and the web server.

    python -m grokbot chat                 # interactive terminal agent in the current directory
    python -m grokbot exec "fix the tests" # non-interactive: runs one turn, prints the answer, exit code 0/1
    python -m grokbot serve --port 5000    # the Flask web UI (same as python webui.py)
    python -m grokbot telegram             # Telegram gateway only (GROK_TELEGRAM_TOKEN + GROK_TELEGRAM_ALLOWED_IDS)

Provider settings come from the environment: GROK_API_KEY, GROK_API_URL (OpenAI-compatible chat URL),
GROK_MODEL. Sandbox policy flags mirror the web UI (--sandbox, --approval, --network).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import threading
from pathlib import Path

from grokbot import __version__, agent as agent_core, backup as backup_mod, config, providers, runlog
from grokbot.approvals import ApprovalBroker, SandboxPolicy, MODES, APPROVALS
from grokbot.tools import ToolContext, default_registry

DEFAULT_URL = 'https://api.x.ai/v1/chat/completions'


def provider_config(args):
    key = args.key or os.environ.get('GROK_API_KEY', '')
    url = args.url or os.environ.get('GROK_API_URL', DEFAULT_URL)
    model = args.model or os.environ.get('GROK_MODEL', 'grok-4-latest')
    if getattr(args, 'mock', False):
        return '', url, model
    if not key:
        sys.exit('No API key. Set GROK_API_KEY (or pass --key).')
    return key, url, model


def stream_for(args):
    return providers.mock_stream if getattr(args, 'mock', False) else None


def build_workspace(root):
    from agent_loop import Workspace
    return Workspace(Path(root).resolve())


class TerminalApprovals(ApprovalBroker):
    """Approval prompts on the terminal: y = allow once, a = always (this run), anything else = deny."""

    def __init__(self, auto=None):
        super().__init__(store=None, timeout=3600)
        self.auto = auto

    def request(self, ctx, tool, arguments, reason):
        policy = ctx.policy or SandboxPolicy()
        from grokbot.approvals import allow_rule_matches, allow_pattern
        for pattern in policy.allow:
            if allow_rule_matches(pattern, tool, arguments):
                return 'allow'
        if policy.approval == 'never' or self.auto == 'deny':
            print('[denied] %s: %s' % (tool, reason), file=sys.stderr)
            return 'deny'
        if self.auto == 'allow':
            return 'allow'
        if not sys.stdin.isatty():
            print('[denied: no terminal to ask] %s: %s' % (tool, reason), file=sys.stderr)
            return 'deny'
        summary = arguments.get('command') or arguments.get('path') or json.dumps(arguments)[:200]
        print('\n⚠ Approval needed for %s — %s\n   %s' % (tool, reason, summary))
        try:
            answer = input('   allow? [y]es / [a]lways / [N]o: ').strip().lower()
        except EOFError:
            answer = ''
        if answer in ('a', 'always'):
            policy.allow.append(allow_pattern(tool, arguments))
            return 'allow'
        return 'allow' if answer in ('y', 'yes') else 'deny'


class Printer:
    """Render agent events for a terminal."""

    def __init__(self, verbose=True, stream=sys.stdout):
        self.verbose = verbose
        self.out = stream
        self.in_text = False

    def __call__(self, event):
        kind = event['type']
        if event.get('agent') and kind not in ('subagent_start', 'subagent_end'):
            return self._subagent(event)
        if kind == 'context_info' and self.verbose:
            bits = []
            if event.get('memories'):
                bits.append('%d memories' % len(event['memories']))
            if event.get('skills'):
                bits.append('%d skills' % len(event['skills']))
            if event.get('project_file'):
                bits.append(event['project_file'])
            if event.get('mcp'):
                bits.append('MCP: ' + ', '.join(event['mcp']))
            if bits:
                self.out.write('· context: %s · %d tools\n' % (' · '.join(bits), event.get('tools', 0)))
        elif kind == 'subagent_start':
            self._break(); self.out.write('┌ %s: %s\n' % (event.get('agent'), (event.get('task') or '')[:100]))
        elif kind == 'subagent_end':
            self._break(); self.out.write('└ %s %s (%s steps, %s tool calls)\n' % (event.get('agent'), event.get('status'), event.get('steps'), event.get('tool_calls')))
        elif kind == 'memory_saved':
            self._break(); self.out.write('· remembered: %s\n' % event.get('text'))
        elif kind == 'text':
            self.out.write(event['delta']); self.out.flush(); self.in_text = True
        elif kind == 'reasoning' and self.verbose:
            pass
        elif kind == 'tool_start' and self.verbose:
            self._break()
            args = event.get('arguments') or {}
            label = args.get('command') or args.get('path') or args.get('query') or args.get('url') or json.dumps(args)[:120]
            self.out.write('▸ %s %s\n' % (event['name'], label)); self.out.flush()
        elif kind == 'tool_end' and self.verbose:
            out = (event.get('output') or '').rstrip()
            lines = out.splitlines()
            shown = '\n'.join('  │ ' + l for l in lines[:12])
            if len(lines) > 12:
                shown += '\n  │ … (%d more lines)' % (len(lines) - 12)
            self.out.write('%s %s (%.1fs)\n%s\n' % ('✓' if event.get('ok') else '✗', event['name'], event.get('duration', 0), shown))
        elif kind in ('status', 'retry', 'compaction'):
            self._break()
            text = event.get('text') or ('context compacted: %s → %s tokens' % (event.get('before_tokens'), event.get('after_tokens')))
            self.out.write('· %s\n' % text)
        elif kind == 'error':
            self._break(); self.out.write('error: %s\n' % event['text'])
        elif kind == 'done':
            self._break()

    def _subagent(self, event):
        """Nested sub-agent events: indented, tools only (its text becomes the delegate tool result)."""
        if not self.verbose:
            return
        kind = event['type']
        if kind == 'tool_start':
            args = event.get('arguments') or {}
            label = args.get('command') or args.get('path') or args.get('query') or args.get('url') or json.dumps(args)[:100]
            self.out.write('│ ▸ %s %s\n' % (event['name'], label))
        elif kind == 'tool_end':
            self.out.write('│ %s %s (%.1fs)\n' % ('✓' if event.get('ok') else '✗', event['name'], event.get('duration', 0)))
        elif kind in ('status', 'error'):
            self.out.write('│ · %s\n' % event.get('text'))

    def _break(self):
        if self.in_text:
            self.out.write('\n'); self.in_text = False


def make_ctx(args, workspace, approvals):
    policy = SandboxPolicy(mode=args.sandbox, approval=args.approval, network=args.network)
    return ToolContext(workspace=workspace, session_id='cli', policy=policy, approvals=approvals, stop=threading.Event(),
                       extras={'run_source': 'cli'})


def run_once(args, messages, ctx, registry, cfg, emit):
    return agent_core.run_turn(messages, registry, ctx, cfg, emit, max_steps=args.max_steps,
                               context_tokens=args.context_tokens, model_label=cfg[2], stream_fn=stream_for(args),
                               summarizer=None if args.mock else (lambda prompt: providers.complete_once(cfg, prompt, max_tokens=1200)))


def cmd_exec(args):
    cfg = provider_config(args)
    workspace = build_workspace(args.workspace)
    registry = default_registry()
    approvals = TerminalApprovals(auto='allow' if args.yes else ('deny' if not sys.stdin.isatty() else None))
    ctx = make_ctx(args, workspace, approvals)
    prompt = args.prompt if args.prompt != '-' else sys.stdin.read()
    printer = Printer(verbose=not args.quiet, stream=sys.stderr if args.json else sys.stdout)
    result = run_once(args, [{'role': 'user', 'content': prompt}], ctx, registry, cfg, printer)
    if args.json:
        print(json.dumps({'status': result.status, 'text': result.text, 'steps': result.steps,
                          'tool_calls': result.tool_calls, 'usage': result.usage}, ensure_ascii=False))
    elif printer.in_text:
        print()
    return 0 if result.status == 'completed' else 1


def cmd_chat(args):
    cfg = provider_config(args)
    workspace = build_workspace(args.workspace)
    registry = default_registry()
    approvals = TerminalApprovals(auto='allow' if args.yes else None)
    ctx = make_ctx(args, workspace, approvals)
    messages = []
    summary = ''
    print('grokbot %s · %s · sandbox %s/%s · workspace %s' % (__version__, cfg[2], ctx.policy.mode, ctx.policy.approval, workspace.root))
    print('Type /help for commands, Ctrl-D to quit.')
    while True:
        try:
            line = input('\n› ').strip()
        except (EOFError, KeyboardInterrupt):
            print(); return 0
        if not line:
            continue
        if line in ('/quit', '/exit'):
            return 0
        if line == '/help':
            print('/new  start over · /compact  summarise history · /policy <mode> [approval] · /tools · /quit'); continue
        if line == '/new':
            messages, summary = [], ''; print('(new conversation)'); continue
        if line == '/tools':
            print(registry.prompt_block(ctx)); continue
        if line.startswith('/policy'):
            parts = line.split()[1:]
            if parts and parts[0] in MODES:
                ctx.policy.mode = parts[0]
            if len(parts) > 1 and parts[1] in APPROVALS:
                ctx.policy.approval = parts[1]
            print('policy: %s / %s' % (ctx.policy.mode, ctx.policy.approval)); continue
        if line == '/compact':
            from grokbot import compaction
            messages, summary, method = compaction.compact(messages, lambda p: providers.complete_once(cfg, p, max_tokens=1200), previous_summary=summary)
            print('(compacted via %s)' % method); continue
        ctx.stop.clear()
        printer = Printer(verbose=not args.quiet)
        try:
            result = agent_core.run_turn(messages + [{'role': 'user', 'content': line}], registry, ctx, cfg, printer,
                                         max_steps=args.max_steps, context_tokens=args.context_tokens, model_label=cfg[2],
                                         previous_summary=summary, stream_fn=stream_for(args),
                                         summarizer=None if args.mock else (lambda prompt: providers.complete_once(cfg, prompt, max_tokens=1200)))
        except KeyboardInterrupt:
            ctx.stop.set(); print('\n(stopped)'); continue
        messages, summary = result.messages, result.summary
        if printer.in_text:
            print()
        if result.status != 'completed':
            print('(%s)' % result.status)


def cmd_backup(args):
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else backup_mod.default_data_dir()
    extra = {}
    if args.include_settings:
        settings = Path(__file__).resolve().parent.parent / 'settings.json'
        if settings.is_file():
            extra['settings.json'] = settings
    try:
        manifest = backup_mod.create_backup(data_dir, args.out, extra_files=extra)
    except backup_mod.BackupError as exc:
        print('Backup failed: %s' % exc, file=sys.stderr)
        return 1
    print('Backup written to %s' % manifest['out'])
    print('%d files (%d database%s: %s), %.1f KB' % (
        manifest['files'], len(manifest['databases']), 's' if len(manifest['databases']) != 1 else '',
        ', '.join(manifest['databases']) or 'none', manifest['bytes'] / 1024.0))
    print('Test the restore sometime: grokbot restore --from %s --data-dir <scratch copy>' % manifest['out'])
    return 0


def cmd_restore(args):
    data_dir = Path(args.data_dir).expanduser() if args.data_dir else backup_mod.default_data_dir()
    try:
        manifest, _ = backup_mod.read_manifest(args.from_zip)
    except backup_mod.BackupError as exc:
        print('Restore failed: %s' % exc, file=sys.stderr)
        return 1
    print('Backup from %s: %d files, databases: %s' % (manifest.get('created_iso'), manifest.get('files'),
                                                       ', '.join(manifest.get('databases') or []) or 'none'))
    print('This REPLACES the current data directory %s' % data_dir)
    print('(the current contents are kept as a .pre-restore copy). Stop the grok-bot server first.')
    if not args.yes:
        if not sys.stdin.isatty():
            print('Refusing without confirmation: pass --yes', file=sys.stderr)
            return 1
        if input('Restore now? [y/N] ').strip().lower() not in ('y', 'yes'):
            print('Aborted.')
            return 1
    try:
        summary = backup_mod.restore_backup(args.from_zip, data_dir)
    except backup_mod.BackupError as exc:
        print('Restore failed: %s' % exc, file=sys.stderr)
        return 1
    print('Restored %d files to %s' % (summary['files'], summary['restored_to']))
    if summary['previous_data_saved_at']:
        print('Previous data kept at %s (delete it once you have checked the restore).' % summary['previous_data_saved_at'])
    return 0


def cmd_runs(args):
    path = args.log or runlog.configured_path() or str(backup_mod.default_data_dir() / '.runlog.jsonl')
    entries = runlog.recent(limit=args.limit, path=path)
    if not entries:
        print('No runs logged yet. The web UI logs every run to .runlog.jsonl in the data directory.')
        return 0
    for e in entries:
        stamp = time.strftime('%m-%d %H:%M:%S', time.localtime(e.get('started', 0)))
        line = '%s %-9s %-10s %5.1fs steps=%-3d tools=%-3d %s' % (
            stamp, e.get('source', '?'), e.get('status', '?'), e.get('duration', 0.0),
            e.get('steps', 0), e.get('tool_calls', 0), (e.get('goal') or '')[:60])
        if e.get('error'):
            line += '  ERROR: ' + e['error'][:120]
        print(line)
        if args.verbose:
            for t in e.get('tools', []):
                mark = 'ok ' if t.get('ok') else 'FAIL'
                print('    [%s] %s (%.1fs)%s' % (mark, t.get('name'), t.get('duration', 0.0),
                                                 ('  ' + t.get('error', '')[:100]) if t.get('error') else ''))
    return 0


def cmd_serve(args):
    import webui
    webui.app.config.update(MOCK=args.mock, MODEL=args.model or webui.DEFAULT_MODEL)
    webui.print_startup_info()
    if args.key:
        webui.app.config['API_KEY'] = args.key
    webui.scheduler().start()
    webui.mcp_manager()
    try:
        webui.start_telegram()
    except ValueError as exc:
        print('Telegram gateway not started: %s' % exc, file=sys.stderr)
    webui.app.run(host=args.host, port=args.port, threaded=True)


def cmd_telegram(args):
    """Telegram-only mode: no HTTP server, same agent/memory/skills/schedules (owner `tg:<chat_id>`)."""
    import webui
    webui.app.config.update(MOCK=args.mock, MODEL=args.model or webui.DEFAULT_MODEL)
    if args.key:
        webui.app.config['API_KEY'] = args.key
    if args.token:
        config.TELEGRAM_TOKEN = args.token
    if args.allow:
        config.TELEGRAM_ALLOWED_IDS = {x.strip() for x in args.allow.split(',') if x.strip()}
    try:
        gateway = webui.start_telegram()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if gateway is None:
        print('Set GROK_TELEGRAM_TOKEN (or --token) and GROK_TELEGRAM_ALLOWED_IDS (or --allow).', file=sys.stderr)
        return 2
    webui.scheduler().start()
    print('Telegram gateway running for %d allowed id(s). Ctrl-C to stop.' % len(gateway.allowed))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        gateway.stop()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='grokbot', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--version', action='version', version='grokbot ' + __version__)
    sub = parser.add_subparsers(dest='command')

    def common(p):
        p.add_argument('--workspace', default='.', help='directory the agent may read/write (default: current directory)')
        p.add_argument('--key', help='API key (default: GROK_API_KEY)')
        p.add_argument('--url', help='OpenAI-compatible chat completions URL (default: GROK_API_URL or xAI)')
        p.add_argument('--model', help='model name (default: GROK_MODEL)')
        p.add_argument('--sandbox', choices=MODES, default=os.environ.get('GROK_SANDBOX_MODE', 'workspace-write'))
        p.add_argument('--approval', choices=APPROVALS, default=os.environ.get('GROK_APPROVAL', 'on-request'))
        p.add_argument('--network', action='store_true', help='allow network access inside run_command')
        p.add_argument('--yes', '-y', action='store_true', help='auto-approve every tool call (YOLO; use in throwaway sandboxes only)')
        p.add_argument('--max-steps', type=int, default=config.MAX_STEPS)
        p.add_argument('--context-tokens', type=int, default=config.DEFAULT_CONTEXT_TOKENS)
        p.add_argument('--quiet', '-q', action='store_true', help='hide tool cards, print only the answer')
        p.add_argument('--mock', action='store_true', help='offline stand-in provider (echo; "!tool name {json}" runs a tool)')

    p_chat = sub.add_parser('chat', help='interactive terminal agent'); common(p_chat)
    p_exec = sub.add_parser('exec', help='run one prompt non-interactively (CI friendly)'); common(p_exec)
    p_exec.add_argument('prompt', help='the task; "-" reads it from stdin')
    p_exec.add_argument('--json', action='store_true', help='print a JSON result object on stdout (events go to stderr)')
    p_serve = sub.add_parser('serve', help='run the web UI')
    p_serve.add_argument('--host', default='127.0.0.1')
    p_serve.add_argument('--port', type=int, default=5000)
    p_serve.add_argument('--model')
    p_serve.add_argument('--key')
    p_serve.add_argument('--mock', action='store_true')
    p_backup = sub.add_parser('backup', help='back up the data directory (workspaces + databases) to a zip')
    p_backup.add_argument('--out', default='grokbot-backup-%s.zip' % time.strftime('%Y%m%d-%H%M%S'))
    p_backup.add_argument('--data-dir', help='default: GROK_WORKSPACE_DIR or the per-user grokbot data dir')
    p_backup.add_argument('--include-settings', action='store_true', help='also include settings.json (contains provider keys)')
    p_restore = sub.add_parser('restore', help='restore a backup zip over the data directory (verified before replacing)')
    p_restore.add_argument('--from', dest='from_zip', required=True, metavar='ZIP')
    p_restore.add_argument('--data-dir', help='default: GROK_WORKSPACE_DIR or the per-user grokbot data dir')
    p_restore.add_argument('--yes', '-y', action='store_true', help='skip the confirmation prompt')
    p_runs = sub.add_parser('runs', help='show the per-run log (id, status, duration, tool calls, errors)')
    p_runs.add_argument('--limit', type=int, default=20)
    p_runs.add_argument('--log', help='path to .runlog.jsonl (default: the configured one)')
    p_runs.add_argument('--verbose', '-v', action='store_true', help='also list each tool call')
    p_tg = sub.add_parser('telegram', help='run only the Telegram gateway (no web server)')
    p_tg.add_argument('--token', help='bot token (default: GROK_TELEGRAM_TOKEN)')
    p_tg.add_argument('--allow', help='comma-separated Telegram user ids allowed to talk to the bot (default: GROK_TELEGRAM_ALLOWED_IDS)')
    p_tg.add_argument('--model')
    p_tg.add_argument('--key')
    p_tg.add_argument('--mock', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'exec':
        return cmd_exec(args)
    if args.command == 'chat':
        return cmd_chat(args)
    if args.command == 'serve':
        return cmd_serve(args)
    if args.command == 'telegram':
        return cmd_telegram(args)
    if args.command == 'backup':
        return cmd_backup(args)
    if args.command == 'restore':
        return cmd_restore(args)
    if args.command == 'runs':
        return cmd_runs(args)
    parser.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
