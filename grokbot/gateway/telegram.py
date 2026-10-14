"""Telegram gateway: chat with the same agent (same memory, skills, conversations, policy) from Telegram.

Fail-closed: the bot refuses to start without an allowlist of Telegram user ids (GROK_TELEGRAM_ALLOWED_IDS) and
ignores every update from anyone else. Long polling only (no public webhook needed). Approvals arrive as inline
buttons; replies stream by editing a draft message. Plain text is sent (no Markdown parsing) so provider output
can never break formatting or inject entities.
"""
from __future__ import annotations

import json
import logging
import threading
import time

import requests

log = logging.getLogger('grokbot.telegram')
MAX_MESSAGE = 4000
EDIT_INTERVAL = 2.0
HELP = ('Commands:\n/new – start a new conversation\n/stop – stop the current reply\n'
        '/policy [read-only|workspace-write|full-access] [on-request|never] – view or set the sandbox policy\n'
        '/status – what is running\n/help – this text\n\nAnything else is sent to the agent.')


class TelegramGateway:
    def __init__(self, token, allowed_ids, runner, *, approvals=None, policy_store=None, api_base='https://api.telegram.org',
                 session=None):
        if not token:
            raise ValueError('Telegram token missing.')
        allowed = {str(x).strip() for x in (allowed_ids or []) if str(x).strip()}
        if not allowed:
            raise ValueError('Refusing to start the Telegram gateway without GROK_TELEGRAM_ALLOWED_IDS (fail closed).')
        self.token = token
        self.allowed = allowed
        self.runner = runner            # runner(owner, text, emit, stop) -> object with .status/.text
        self.approvals = approvals
        self.policy_store = policy_store
        self.base = api_base.rstrip('/') + '/bot' + token + '/'
        self.http = session or requests.Session()
        self.offset = 0
        self.chats = {}
        self.lock = threading.Lock()
        self._stop = threading.Event()

    # ------------------------------------------------------------------ Telegram API ---------------------------
    def api(self, method, timeout=35, **params):
        try:
            res = self.http.post(self.base + method, json=params, timeout=timeout)
            data = res.json()
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError('Telegram API %s failed: %s' % (method, type(exc).__name__)) from exc
        if not isinstance(data, dict) or not data.get('ok'):
            raise RuntimeError('Telegram API %s: %s' % (method, (data or {}).get('description', 'error') if isinstance(data, dict) else 'bad response'))
        return data.get('result')

    def send(self, chat_id, text, reply_markup=None):
        text = text or '(empty reply)'
        last_id = None
        for i in range(0, len(text), MAX_MESSAGE):
            params = {'chat_id': chat_id, 'text': text[i:i + MAX_MESSAGE], 'disable_web_page_preview': True}
            if reply_markup and i + MAX_MESSAGE >= len(text):
                params['reply_markup'] = reply_markup
            result = self.api('sendMessage', **params)
            last_id = result.get('message_id') if isinstance(result, dict) else None
        return last_id

    def edit(self, chat_id, message_id, text, reply_markup=None):
        params = {'chat_id': chat_id, 'message_id': message_id, 'text': (text or '…')[:MAX_MESSAGE], 'disable_web_page_preview': True}
        if reply_markup is not None:
            params['reply_markup'] = reply_markup
        try:
            self.api('editMessageText', **params)
        except RuntimeError as exc:
            if 'not modified' not in str(exc):
                log.debug('edit failed: %s', exc)

    # ------------------------------------------------------------------ dispatch -------------------------------
    def allowed_user(self, obj):
        user = (obj or {}).get('from') or {}
        return str(user.get('id', '')) in self.allowed

    def chat_state(self, chat_id):
        with self.lock:
            return self.chats.setdefault(str(chat_id), {'lock': threading.Lock(), 'stop': threading.Event(), 'busy': False})

    @staticmethod
    def owner_for(chat_id):
        return 'tg:' + str(chat_id)

    def handle_update(self, update):
        if not isinstance(update, dict):
            return
        if 'callback_query' in update:
            return self._handle_callback(update['callback_query'])
        message = update.get('message') or update.get('edited_message')
        if not isinstance(message, dict):
            return
        chat_id = (message.get('chat') or {}).get('id')
        if chat_id is None or not self.allowed_user(message) or str(chat_id) not in self.allowed:
            log.info('ignored update from non-allowlisted chat %s', chat_id)
            return
        text = message.get('text')
        if not isinstance(text, str) or not text.strip():
            self.send(chat_id, 'Send text (documents and photos are not supported in Telegram yet).')
            return
        text = text.strip()
        if text.startswith('/'):
            return self._command(chat_id, text)
        state = self.chat_state(chat_id)
        if state['busy']:
            self.send(chat_id, 'Still working on the previous message. Send /stop to cancel it.')
            return
        threading.Thread(target=self._run, args=(chat_id, text), daemon=True, name='tg-' + str(chat_id)).start()

    def _command(self, chat_id, text):
        parts = text.split()
        cmd = parts[0].lower().split('@')[0]
        owner = self.owner_for(chat_id)
        if cmd in ('/start', '/help'):
            self.send(chat_id, 'grok-bot is listening.\n\n' + HELP)
        elif cmd == '/new':
            self.runner.new_conversation(owner) if hasattr(self.runner, 'new_conversation') else None
            self.send(chat_id, 'New conversation started.')
        elif cmd == '/stop':
            state = self.chat_state(chat_id)
            state['stop'].set()
            if self.approvals is not None:
                self.approvals.cancel_all(owner)
            self.send(chat_id, 'Stop requested.' if state['busy'] else 'Nothing is running.')
        elif cmd == '/status':
            state = self.chat_state(chat_id)
            self.send(chat_id, 'Working on a reply…' if state['busy'] else 'Idle.')
        elif cmd == '/policy':
            if self.policy_store is None:
                self.send(chat_id, 'Policy store unavailable.')
                return
            policy = self.policy_store.load(owner)
            from grokbot.approvals import MODES, APPROVALS
            for arg in parts[1:]:
                if arg in MODES:
                    policy.mode = arg
                elif arg in APPROVALS:
                    policy.approval = arg
                else:
                    self.send(chat_id, 'Unknown value ' + arg + '. ' + HELP)
                    return
            if len(parts) > 1:
                self.policy_store.save(owner, policy)
            self.send(chat_id, 'Policy: ' + policy.label() + (' · network' if policy.network else ''))
        else:
            self.send(chat_id, 'Unknown command.\n\n' + HELP)

    def _handle_callback(self, query):
        if not isinstance(query, dict) or not self.allowed_user(query):
            return
        data = query.get('data') or ''
        message = query.get('message') or {}
        chat_id = (message.get('chat') or {}).get('id')
        try:
            self.api('answerCallbackQuery', callback_query_id=query.get('id'))
        except RuntimeError:
            pass
        if not data.startswith('ap|') or self.approvals is None or chat_id is None:
            return
        try:
            _, request_id, decision = data.split('|', 2)
        except ValueError:
            return
        ok = self.approvals.resolve(request_id, decision, self.owner_for(chat_id))
        base = message.get('text') or 'Approval'
        self.edit(chat_id, message.get('message_id'), base + '\n\n→ ' + (decision if ok else 'expired'), reply_markup={'inline_keyboard': []})

    # ------------------------------------------------------------------ a turn ----------------------------------
    def _run(self, chat_id, text):
        state = self.chat_state(chat_id)
        owner = self.owner_for(chat_id)
        with state['lock']:
            state['busy'] = True
            state['stop'].clear()
            draft = {'id': None, 'text': '', 'last_edit': 0.0, 'status': ''}

            def flush(force=False):
                now = time.monotonic()
                if not force and now - draft['last_edit'] < EDIT_INTERVAL:
                    return
                body = draft['text'] or draft['status'] or '…'
                if draft['id'] is None:
                    draft['id'] = self.send(chat_id, body[:MAX_MESSAGE])
                else:
                    self.edit(chat_id, draft['id'], body[:MAX_MESSAGE])
                draft['last_edit'] = now

            def emit(event):
                kind = event.get('type')
                if kind == 'text':
                    draft['text'] += event.get('delta', '')
                    flush()
                elif kind == 'tool_start':
                    args = event.get('arguments') or {}
                    label = args.get('command') or args.get('path') or args.get('query') or args.get('url') or ''
                    draft['status'] = '⚙ ' + event.get('name', 'tool') + ' ' + str(label)[:80]
                    if not draft['text']:
                        flush()
                elif kind == 'approval_required':
                    args = event.get('arguments') or {}
                    summary = args.get('command') or args.get('path') or json.dumps(args)[:300]
                    keyboard = {'inline_keyboard': [[
                        {'text': '✅ Allow', 'callback_data': 'ap|%s|allow' % event['id']},
                        {'text': '♾ Always', 'callback_data': 'ap|%s|always' % event['id']},
                        {'text': '⛔ Deny', 'callback_data': 'ap|%s|deny' % event['id']}]]}
                    self.send(chat_id, 'Approval needed: %s\n%s\n\n%s' % (event.get('tool'), event.get('reason', ''), summary), reply_markup=keyboard)
                elif kind in ('error',):
                    draft['status'] = '⚠ ' + event.get('text', '')[:200]
                    flush(force=True)
            try:
                result = self.runner(owner, text, emit, state['stop'])
                final = (getattr(result, 'text', '') or draft['text'] or '').strip()
                status = getattr(result, 'status', 'completed')
                if status == 'stopped':
                    final = (final + '\n\n(stopped)').strip()
                elif status == 'max_steps':
                    final = (final + '\n\n(step budget reached — say "continue")').strip()
                elif status == 'error' and not final:
                    final = 'The agent hit an error. Check the web UI for details.'
                if draft['id'] is not None:
                    self.edit(chat_id, draft['id'], final[:MAX_MESSAGE] or '(empty reply)')
                    if len(final) > MAX_MESSAGE:
                        self.send(chat_id, final[MAX_MESSAGE:])
                else:
                    self.send(chat_id, final or '(empty reply)')
            except Exception as exc:  # noqa: BLE001
                log.exception('telegram turn failed')
                try:
                    self.send(chat_id, 'Internal error: ' + type(exc).__name__)
                except RuntimeError:
                    pass
            finally:
                state['busy'] = False

    # ------------------------------------------------------------------ polling ---------------------------------
    def poll_once(self, timeout=25):
        params = {'offset': self.offset, 'timeout': timeout, 'allowed_updates': ['message', 'callback_query']}
        updates = self.api('getUpdates', timeout=timeout + 10, **params)
        for update in updates or []:
            self.offset = max(self.offset, int(update.get('update_id', 0)) + 1)
            try:
                self.handle_update(update)
            except Exception:  # noqa: BLE001
                log.exception('update handling failed')
        return len(updates or [])

    def run_forever(self):
        log.info('Telegram gateway polling (allowlist: %d ids)', len(self.allowed))
        backoff = 1
        while not self._stop.is_set():
            try:
                self.poll_once()
                backoff = 1
            except RuntimeError as exc:
                log.warning('%s; retrying in %ds', exc, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)

    def start(self):
        thread = threading.Thread(target=self.run_forever, daemon=True, name='grokbot-telegram')
        thread.start()
        return thread

    def stop(self):
        self._stop.set()
