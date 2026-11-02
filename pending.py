"""Saved human requests, retried through the original message and current permissions."""
import asyncio
from datetime import datetime
import hashlib
import json
import time
from types import SimpleNamespace

import discord
from openai import APIError


QUEUED_NOTICE = 'I have saved your request and will finish it when I can. You do not need to ask again.'


def temporary_chat_error(exc):
    status = getattr(exc, 'status_code', None)
    return isinstance(exc, APIError) and (status is None or status in {408, 409, 429} or status >= 500)


def message_stamp(message):
    value = [message.content, [str(item.id) for item in getattr(message, 'attachments', [])],
             [str(item.id) for item in getattr(message, 'stickers', [])],
             str(getattr(getattr(message, 'reference', None), 'message_id', ''))]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


class RequestQueue:
    def __init__(self, bot):
        self.bot, self.db = bot, bot.db
        self.notice_db = self.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS request_queue (
            request_id TEXT NOT NULL, job TEXT NOT NULL, channel_id TEXT NOT NULL,
            user_id TEXT NOT NULL, guild_id TEXT, stamp TEXT NOT NULL,
            tool TEXT NOT NULL, arguments TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
            due_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, result TEXT, source TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (request_id, job))''')
        if 'notice_due' not in {row[1] for row in self.db.execute('PRAGMA table_info(request_queue)')}:
            self.db.execute('ALTER TABLE request_queue ADD COLUMN notice_due REAL')
            self.db.execute("UPDATE request_queue SET notice_due=? WHERE state IN ('pending','running')", (time.time() + 300,))
        self.db.commit()

    def add(self, request, *, tool='', arguments='', delay=30):
        if request.author.bot or not self.bot.social.access(request, check_schedule=False):
            return False
        source = ''
        if getattr(request, 'interaction', None) or getattr(request, 'queue_anchor', None):
            if not getattr(request, 'queue_anchor', None):
                return False
            source = json.dumps({'anchor': str(request.queue_anchor), 'content': request.content,
                                 'created_at': request.created_at.isoformat()})
        job = hashlib.sha256((tool + arguments).encode()).hexdigest() if tool else ''
        previous = self.db.execute('SELECT notice_due FROM request_queue WHERE request_id=? LIMIT 1', (str(request.id),)).fetchone()
        self.db.execute('''INSERT OR IGNORE INTO request_queue
            (request_id,job,channel_id,user_id,guild_id,stamp,tool,arguments,due_at,source,notice_due)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (str(request.id), job, str(request.channel.id), str(request.author.id),
            str(request.guild.id) if request.guild else None, message_stamp(request), tool, arguments, time.time() + delay, source,
            previous[0] if previous else time.time() + 300))
        self.db.commit()
        row = self.db.execute('SELECT state FROM request_queue WHERE request_id=? AND job=?', (str(request.id), job)).fetchone()
        return row[0] in {'pending', 'running'}

    def answered(self, request, channel_id=None):
        self.db.execute('UPDATE request_queue SET notice_due=NULL WHERE request_id=?', (str(request.id),))
        self.db.commit()
        self.notice_db.execute('INSERT OR REPLACE INTO social_state VALUES (?,?)',
                               ('queue-response:' + str(channel_id or request.channel.id), str(time.time())))
        self.notice_db.commit()

    async def notices(self, now):
        if not self.bot.is_ready() or not self.bot.accepting_messages or not self.bot.social.available(now):
            return
        columns = ('request_id', 'channel_id', 'user_id', 'guild_id', 'stamp', 'source')
        rows = self.db.execute('SELECT DISTINCT ' + ','.join(columns) +
            " FROM request_queue WHERE state IN ('pending','running') AND notice_due<=? ORDER BY notice_due LIMIT 20", (now,)).fetchall()
        for row in rows:
            item = dict(zip(columns, row))
            try:
                request = await self.original_message(item)
                if not self.bot.social.response_allowed(request):
                    continue
                # Shared across accounts, persistent across restarts, and reserved before sending.
                recent = self.notice_db.execute('SELECT max(CAST(value AS REAL)) FROM social_state WHERE name IN (?,?)',
                    ('queue-response:' + item['channel_id'], 'queue-notice:' + item['channel_id'])).fetchone()[0] or 0
                if now - recent < 300:
                    continue
                claimed = self.db.execute("UPDATE request_queue SET notice_due=NULL WHERE request_id=? "
                    "AND state IN ('pending','running') AND notice_due<=?", (item['request_id'], now))
                self.db.commit()
                if not claimed.rowcount:
                    continue
                self.notice_db.execute('INSERT OR REPLACE INTO social_state VALUES (?,?)',
                                       ('queue-notice:' + item['channel_id'], str(now)))
                self.notice_db.commit()
                await self.bot.deliver(request, QUEUED_NOTICE, [])
            except (discord.NotFound, discord.Forbidden, ValueError, PermissionError):
                self.db.execute('UPDATE request_queue SET notice_due=NULL WHERE request_id=?', (item['request_id'],))
                self.db.commit()
            except (discord.HTTPException, OSError, asyncio.TimeoutError) as exc:
                self.bot.features.record_status('queue_notice', {'ok': False, 'error': self.bot.features.clean_error(exc)})

    def recover(self):
        # Action receipts prevent repeating a completed or unconfirmed operation after a restart.
        self.db.execute("UPDATE request_queue SET state='pending' WHERE state='running'")
        self.db.execute("UPDATE request_queue SET state='uncertain', result='Delivery was interrupted; check Discord.' WHERE state='delivering'")
        self.db.commit()

    async def original_message(self, item):
        channel = self.bot.get_channel(int(item['channel_id'])) or await self.bot.fetch_channel(int(item['channel_id']))
        source = json.loads(item['source']) if item['source'] else None
        request = await channel.fetch_message(int(source['anchor'] if source else item['request_id']))
        if source:
            if request.author.id != self.bot.bot_id:
                raise PermissionError('The saved slash-command reply is no longer available.')
            request = SimpleNamespace(id=int(item['request_id']), channel=channel, guild=request.guild,
                author=SimpleNamespace(id=int(item['user_id']), bot=False), content=source['content'],
                created_at=datetime.fromisoformat(source['created_at']), to_reference=request.to_reference,
                reference=None, mentions=[], attachments=[], embeds=[], queue_anchor=int(source['anchor']))
        guild_id = str(request.guild.id) if request.guild else None
        if (str(request.author.id) != item['user_id'] or request.author.bot or guild_id != item['guild_id']
                or (guild_id is not None and guild_id != str(self.bot.server_id)) or message_stamp(request) != item['stamp']):
            raise ValueError('The original request was changed or no longer belongs to this conversation.')
        if request.guild:
            # Fetch rather than trust the member captured when the request was queued.
            request.author = await request.guild.fetch_member(request.author.id)
            permissions = channel.permissions_for(request.author)
            if not permissions.view_channel or not permissions.read_message_history:
                raise PermissionError('The requester can no longer read this channel.')
        elif request.author.id != self.bot.features.owner_id:
            raise PermissionError('Queued direct messages are owner-only.')
        elif source:
            request.author = await self.bot.fetch_user(request.author.id)
        return request

    async def tick(self, now):
        columns = ('request_id', 'job', 'channel_id', 'user_id', 'guild_id', 'stamp', 'tool', 'arguments', 'attempts', 'source')
        rows = self.db.execute('SELECT ' + ','.join(columns) +
                               " FROM request_queue WHERE state='pending' AND due_at<=? ORDER BY due_at,rowid LIMIT 20", (now,)).fetchall()
        for row in rows:
            if self.bot.active_users or not self.bot.accepting_messages or not self.bot.social.available():
                return
            item = dict(zip(columns, row))
            key = (item['request_id'], item['job'])
            claimed = self.db.execute("UPDATE request_queue SET state='running',attempts=attempts+1 WHERE request_id=? AND job=? AND state='pending'", key)
            self.db.commit()
            if not claimed.rowcount:
                continue
            state, result = 'pending', 'Waiting for availability.'
            delay = min(3600, 30 * 2 ** min(item['attempts'], 7))
            request = None
            owns_slot = False
            try:
                request = await self.original_message(item)
                if not self.bot.social.access(request):
                    continue
                # A human can have started another request while Discord was being fetched.
                if self.bot.active_users:
                    continue
                self.bot.social.admitted[str(request.id)] = False
                if not item['tool']:
                    turn = self.db.execute('SELECT state FROM turns WHERE request_id=?', (item['request_id'],)).fetchone()
                    actions = self.db.execute('SELECT 1 FROM actions WHERE request_id=?', (item['request_id'],)).fetchone()
                    if turn and turn[0] == 'complete':
                        state, result = 'complete', 'Already handled.'
                    elif actions or (turn and turn[0] not in {'queued', 'started'}):
                        state, result = 'uncertain', 'An earlier attempt has recorded actions or an unconfirmed outcome; it will not be repeated.'
                    else:
                        self.db.execute('DELETE FROM turns WHERE request_id=?', (item['request_id'],))
                        self.db.commit()
                        state = await self.bot.process_message(request, queued=True)
                        result = 'Waiting to retry.' if state == 'pending' else 'Request handled.'
                else:
                    self.bot.active_users.add(request.author.id)
                    owns_slot = True
                    outcome = await self.bot.execute(item['arguments'], request, tool=item['tool'], retry=True)
                    result = json.dumps(outcome)
                    if outcome.get('retry_after') and not outcome.get('outcome_may_be_uncertain'):
                        delay = max(delay, outcome['retry_after'])
                        continue
                    # Summarising can be retried safely: the action result is already saved.
                    text, turn, receipts = await self.bot.converse(request, automatic='queued_result', completed_result=outcome)
                    if not self.bot.social.response_allowed(request):
                        continue
                    self.db.execute("UPDATE request_queue SET state='delivering',result=? WHERE request_id=? AND job=?", (result, *key))
                    self.db.commit()
                    state = 'uncertain'
                    delivered = await self.bot.deliver(request, text, receipts)
                    state = 'complete' if delivered else 'pending' if delivered is False else 'uncertain'
                    if state == 'complete':
                        previous = self.db.execute('SELECT messages FROM turns WHERE request_id=?', (item['request_id'],)).fetchone()
                        messages = json.loads(previous[0]) + [turn[-1]] if previous else turn
                        self.db.execute('''INSERT INTO turns (request_id,channel_id,author_id,messages,state) VALUES (?,?,?,?,'complete')
                            ON CONFLICT(request_id) DO UPDATE SET messages=excluded.messages,state='complete' ''',
                            (item['request_id'], item['channel_id'], item['user_id'], self.bot.features.redact(json.dumps(messages))))
            except (discord.NotFound, discord.Forbidden, ValueError, PermissionError) as exc:
                state, result = 'cancelled', self.bot.features.clean_error(exc)
            except (APIError, discord.HTTPException, OSError, asyncio.TimeoutError) as exc:
                result = self.bot.features.clean_error(exc)
                if isinstance(exc, APIError) and not temporary_chat_error(exc):
                    state = 'cancelled'
            except Exception as exc:
                state, result = 'uncertain', self.bot.features.clean_error(exc)
            finally:
                if owns_slot:
                    self.bot.active_users.discard(request.author.id)
                self.db.execute('UPDATE request_queue SET state=?,due_at=?,result=? WHERE request_id=? AND job=?',
                                (state, now + delay, result, *key))
                self.db.commit()
                self.bot.features.record_status('queue', {'ok': state == 'complete', 'request_id': item['request_id'], 'state': state})
