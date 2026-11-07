"""Opt-in Telegram receiver. Never controls the browser or monitoring tasks."""
import asyncio
import hashlib
import secrets
import time

import aiohttp


def scope_for(token):
    return hashlib.sha256(token.encode()).hexdigest()[:24]


class TelegramFailure(Exception):
    pass


class Recipients:
    def __init__(self, storage, clock=time.time):
        self.storage, self.db, self.clock = storage, storage.db, clock
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS telegram_users (
            scope TEXT, chat TEXT, name TEXT NOT NULL, active INTEGER NOT NULL,
            source TEXT NOT NULL, PRIMARY KEY(scope,chat));
          CREATE TABLE IF NOT EXISTS telegram_invites (
            hash TEXT PRIMARY KEY, scope TEXT NOT NULL, expires REAL NOT NULL,
            consumed INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS telegram_offsets (scope TEXT PRIMARY KEY, next_id INTEGER NOT NULL);
        """)

    def migrate(self, settings):
        scope = scope_for(settings.telegram_token)
        with self.db:
            for chat in settings.telegram_chat_ids:
                self.db.execute("INSERT OR IGNORE INTO telegram_users VALUES (?,?,?,1,'legacy')",
                                (scope, chat, "Настроен вручную"))
        return scope

    def users(self, settings):
        scope = self.migrate(settings)
        rows = [dict(zip(('chat_id', 'name', 'active', 'source'), row)) for row in self.db.execute(
            "SELECT chat,name,active,source FROM telegram_users WHERE scope=? ORDER BY rowid", (scope,))]
        for row in rows:
            row['active'] = bool(row['active'] and (row['source'] != 'legacy' or row['chat_id'] in settings.telegram_chat_ids))
        return rows

    def effective(self, settings):
        return [u['chat_id'] for u in self.users(settings) if u['active'] and
                (u['source'] != 'legacy' or u['chat_id'] in settings.telegram_chat_ids)]

    def invite(self, settings):
        code = secrets.token_urlsafe(24)
        with self.db:
            self.db.execute("DELETE FROM telegram_invites WHERE expires<? OR consumed=1", (self.clock(),))
            self.db.execute("INSERT INTO telegram_invites(hash,scope,expires) VALUES (?,?,?)",
                            (hashlib.sha256(code.encode()).hexdigest(), scope_for(settings.telegram_token), self.clock()+600))
        return code

    def remove(self, settings, chat):
        self.migrate(settings)
        with self.db:
            self.db.execute("UPDATE telegram_users SET active=0 WHERE scope=? AND chat=?",
                            (scope_for(settings.telegram_token), str(chat)))

    def offset(self, scope):
        row = self.db.execute("SELECT next_id FROM telegram_offsets WHERE scope=?", (scope,)).fetchone()
        return row[0] if row else 0

    def process(self, settings, update, status):
        """State change and acknowledgement cursor commit together, before any reply."""
        scope = self.migrate(settings)
        uid = update.get('update_id')
        if not isinstance(uid, int) or uid < self.offset(scope):
            return None
        reply = None
        effective = self.effective(settings)
        with self.db:
            message = update.get('message', {})
            chat = message.get('chat', {})
            if chat.get('type') == 'private' and isinstance(chat.get('id'), int):
                chat_id = str(chat['id'])
                text = str(message.get('text', ''))[:256].split()
                command = text[0].split('@')[0] if text else ''
                active = chat_id in effective
                answer = None
                if command == '/start':
                    if active:
                        answer = 'Вы уже подписаны на уведомления Barcelona TIE.'
                    else:
                        code = text[1] if len(text) == 2 else ''
                        digest = hashlib.sha256(code.encode()).hexdigest()
                        invite = self.db.execute('SELECT hash FROM telegram_invites WHERE hash=? AND scope=? AND consumed=0 AND expires>?',
                                                 (digest, scope, self.clock())).fetchone()
                        if not invite:
                            answer = 'Для подписки нужен действующий одноразовый код: /start КОД. Получите его у администратора Home Assistant.'
                        elif len(effective) >= 2:
                            answer = 'Достигнут предел двух получателей. Обратитесь к администратору.'
                        else:
                            name = str(chat.get('username') or chat.get('first_name') or 'Пользователь')[:100]
                            self.db.execute("INSERT OR REPLACE INTO telegram_users VALUES (?,?,?,1,'registered')", (scope,chat_id,name))
                            self.db.execute('UPDATE telegram_invites SET consumed=1 WHERE hash=?', (digest,))
                            answer = 'Подписка Barcelona TIE включена. Команды: /status, /stop, /help.'
                elif command == '/stop':
                    if active:
                        self.db.execute('UPDATE telegram_users SET active=0 WHERE scope=? AND chat=?', (scope,chat_id))
                    answer = 'Подписка отключена. Мониторинг сит продолжает работать.'
                elif command == '/status':
                    answer = status() if active else 'Сначала подпишитесь с кодом приглашения: /start КОД.'
                elif command == '/help':
                    answer = '/start КОД — подписаться\n/status — состояние мониторинга\n/stop — отписаться (поиск продолжится)\n/help — справка\nОформление записи доступно только через защищённый Home Assistant.'
                if answer:
                    reply = (chat_id, answer)
            self.db.execute('INSERT OR REPLACE INTO telegram_offsets VALUES (?,?)', (scope,uid+1))
        return reply


class Receiver:
    def __init__(self, settings, storage, recipients, status):
        self.settings, self.storage, self.recipients, self.status = settings, storage, recipients, status
        self.task = None
        self.state = 'DISABLED'
        self.scope = None

    async def api(self, token, method, payload):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35), trust_env=True) as session:
            async with session.post(f'https://api.telegram.org/bot{token}/{method}', json=payload, allow_redirects=False) as r:
                if r.status == 409:
                    raise TelegramFailure('TELEGRAM_POLLING_CONFLICT')
                if r.status in (401,403):
                    raise TelegramFailure('TELEGRAM_AUTH_ERROR')
                if r.status != 200:
                    raise TelegramFailure('TELEGRAM_API_ERROR')
                data = await r.json()
                if not data.get('ok'):
                    raise TelegramFailure('TELEGRAM_API_ERROR')
                return data['result']

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    def restart(self):
        s = self.settings()
        self.storage.put('telegram_fault_' + scope_for(s.telegram_token), None)
        self.scope = None

    async def run(self):
        delay = 1
        while True:
            s = self.settings()
            scope = scope_for(s.telegram_token)
            if not (s.telegram_enabled and s.telegram_polling_enabled and s.telegram_token):
                self.state = 'DISABLED'
                self.scope = None
                await asyncio.sleep(1)
                continue
            fault = self.storage.get('telegram_fault_' + scope)
            if fault:
                self.state = fault
                self.scope = None
                await asyncio.sleep(1)
                continue
            try:
                if self.scope != scope:
                    info = await self.api(s.telegram_token, 'getWebhookInfo', {})
                    if info.get('url'):
                        raise TelegramFailure('TELEGRAM_WEBHOOK_CONFLICT')
                    self.scope = scope
                self.state = 'POLLING'
                updates = await self.api(s.telegram_token, 'getUpdates', {
                    'offset':self.recipients.offset(scope), 'timeout':25, 'limit':100, 'allowed_updates':['message']})
                current = self.settings()
                if scope_for(current.telegram_token) != scope or not current.telegram_polling_enabled:
                    self.scope = None
                    continue
                for update in updates:
                    reply = self.recipients.process(s, update, self.status)
                    if reply:
                        try:
                            await self.api(s.telegram_token, 'sendMessage', {'chat_id':reply[0], 'text':reply[1]})
                        except (TelegramFailure, aiohttp.ClientError, TimeoutError):
                            self.storage.event(None, 'TELEGRAM_REPLY_FAILED')
                delay = 1
                await asyncio.sleep(.1)
            except asyncio.CancelledError:
                raise
            except TelegramFailure as exc:
                code = str(exc)
                self.state = code
                self.storage.event(None, code)
                if code in {'TELEGRAM_POLLING_CONFLICT','TELEGRAM_WEBHOOK_CONFLICT','TELEGRAM_AUTH_ERROR'}:
                    self.storage.put('telegram_fault_' + scope, code)
                await asyncio.sleep(delay)
                delay = min(60, delay*2)
            except Exception:
                self.state = 'TELEGRAM_NETWORK_ERROR'
                self.storage.event(None, self.state)
                await asyncio.sleep(delay)
                delay = min(60, delay*2)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
